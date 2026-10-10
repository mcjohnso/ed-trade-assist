"""Spansh HTTP client for EDTradeAssist - the FALLBACK market source.

Ardent (edta_ardent.py) is the primary source. This one is asked only when Ardent
is failing or has stopped ingesting, which it did for over a day in October 2026:
every row it served was 28 hours old while Spansh was minutes behind the game.

    POST /api/stations/search
        {"filters": {"market": [{"name": ..., "supply": {...}}], "distance": ...,
                     "market_updated_at": ..., "type": ..., "has_large_pad": ...},
         "sort": [{"distance": {"direction": "asc"}}],
         "size": ..., "reference_system": ...}

Rows come back NORMALISED TO ARDENT'S ROW SHAPE, so edta_core ranks them without
knowing where they came from.

DIRECTION. Unlike Ardent, Spansh names prices from the PLAYER's point of view:
`buy_price` is what you pay and `supply` is what the station has. They map onto
Ardent's `buyPrice` and `stock` by coincidence of spelling, not by convention.
(Verified live 2026-10-10 against a journal MarketBuy.)

Upstream behaviours worth knowing, each verified live 2026-10-10:
  * The response carries each station's ENTIRE market, about 123 KB a station,
    and a `fields` projection is ignored. That is why PAGE_SIZE is small and why
    every filter that can run server-side does. An empty answer is under 1 KB.
  * Results can be sorted by distance server-side, so the nearest matches are the
    ones returned - no ring expansion is needed here.
  * An unknown reference system is a bare 400 {"error": "Invalid request"}, the
    same answer a malformed filter gets. An unknown commodity is 200 with no rows.
  * Commodities go by their display name ("Computer Components"), not the symbol.
  * The `market_updated_at` range filter is honoured.

Nothing here raises: every call returns a SearchResult.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from edta_ardent import (BAD_REQUEST, DEFAULT_CACHE_TTL, DEFAULT_MIN_INTERVAL,
                         DEFAULT_TIMEOUT, HTTP, NETWORK, NOT_FOUND, OK, SearchResult,
                         _RateLimiter)

logger = logging.getLogger(__name__)

BASE_URL = "https://spansh.co.uk"
SEARCH_PATH = "/api/stations/search"

#: Player buys -> Spansh's own player-side names. See the module docstring.
PRICE_FIELD = "buy_price"
VOLUME_FIELD = "supply"

#: Stations per request. Each one costs ~123 KB, so this is deliberately small;
#: the distance sort means these are the nearest matches, not an arbitrary few.
PAGE_SIZE = 5

MAX_DISTANCE_LY = 500.0

#: Everything Spansh calls a station EXCEPT fleet carriers ("Drake-Class Carrier")
#: and the two construction depots, which buy but never sell. An allowlist because
#: the filter has no negation; a station type Frontier adds later is missed until
#: it is listed here. Values from /api/stations/field_values/type.
STATION_TYPES: Tuple[str, ...] = (
    "Asteroid base", "Coriolis Starport", "Dodec Starport", "Mega ship",
    "Ocellus Starport", "Orbis Starport", "Outpost", "Planetary Outpost",
    "Planetary Port", "Settlement", "Surface Settlement",
)

_SUPPLY_CEILING = 999999999
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def normalise_row(station: Dict[str, Any], commodity_name: str) -> Optional[Dict[str, Any]]:
    """One Spansh station -> one Ardent-shaped row, or None when the station's
    market does not list the commodity after all."""
    if not isinstance(station, dict):
        return None
    wanted = (commodity_name or "").casefold()
    entry = next((item for item in station.get("market") or ()
                  if isinstance(item, dict)
                  and (item.get("commodity") or "").casefold() == wanted), None)
    if entry is None:
        return None

    if station.get("large_pads"):
        pad = 3
    elif station.get("medium_pads"):
        pad = 2
    elif station.get("small_pads"):
        pad = 1
    else:
        pad = 3 if station.get("has_large_pad") else None

    return {
        "commodityName": entry.get("commodity"),
        "systemName": station.get("system_name"),
        "stationName": station.get("name"),
        "marketId": station.get("market_id"),
        "systemAddress": station.get("system_id64"),
        "stationType": station.get("type"),
        "systemX": station.get("system_x"),
        "systemY": station.get("system_y"),
        "systemZ": station.get("system_z"),
        "distance": station.get("distance"),
        "distanceToArrival": station.get("distance_to_arrival"),
        "maxLandingPadSize": pad,
        "stock": entry.get(VOLUME_FIELD),
        "buyPrice": entry.get(PRICE_FIELD),
        "updatedAt": station.get("market_updated_at"),
    }


class SpanshClient:
    def __init__(self, user_agent: str, timeout: float = DEFAULT_TIMEOUT,
                 min_interval: float = DEFAULT_MIN_INTERVAL,
                 cache_ttl: float = DEFAULT_CACHE_TTL,
                 base_url: str = BASE_URL):
        self.user_agent = user_agent
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.cache_ttl = cache_ttl
        self._limiter = _RateLimiter(min_interval)
        self._cache: Dict[Tuple, Tuple[float, SearchResult]] = {}
        self._cache_lock = threading.Lock()
        self._session = None

    # --- transport ----------------------------------------------------------

    def _get_session(self):
        """Lazy, and None when requests is absent - same reasoning as Ardent's."""
        if self._session is not None:
            return self._session
        try:
            import requests
        except ImportError:
            logger.debug("requests unavailable; using urllib")
            return None
        session = requests.Session()
        session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        })
        self._session = session
        return session

    def _post(self, url: str, body: Dict[str, Any]) -> Tuple[int, str]:
        """(status, body). Raises only transport errors."""
        session = self._get_session()
        if session is not None:
            response = session.post(url, json=body, timeout=self.timeout)
            return response.status_code, response.text

        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Content-Type": "application/json",
            })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, ""

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    # --- the search ---------------------------------------------------------

    def build_body(self, system: str, commodity_name: str, max_distance: float,
                   min_volume: int, large_pad_only: bool, updated_within_h: float,
                   size: int, now: datetime) -> Dict[str, Any]:
        since = now - timedelta(hours=float(updated_within_h))
        filters: Dict[str, Any] = {
            "market": [{
                "name": commodity_name,
                "supply": {"value": [int(min_volume), _SUPPLY_CEILING], "comparison": "<=>"},
            }],
            "distance": {"min": 0, "max": round(float(max_distance), 1)},
            "type": {"value": list(STATION_TYPES)},
            "market_updated_at": {
                "value": [since.strftime(_STAMP), (now + timedelta(days=1)).strftime(_STAMP)],
                "comparison": "<=>",
            },
        }
        if large_pad_only:
            filters["has_large_pad"] = {"value": True}
        return {
            "filters": filters,
            "sort": [{"distance": {"direction": "asc"}}],
            "size": int(size),
            "page": 0,
            "reference_system": system,
        }

    def stations_selling(self, system: str, commodity_name: str, max_distance: float,
                         min_volume: int, large_pad_only: bool, updated_within_h: float,
                         size: int = PAGE_SIZE, now: Optional[datetime] = None,
                         use_cache: bool = True) -> SearchResult:
        """The nearest markets around `system` that will sell `commodity_name` to
        you and were updated in the last `updated_within_h` hours."""
        system = (system or "").strip()
        commodity_name = (commodity_name or "").strip()
        if not system or not commodity_name:
            return SearchResult(BAD_REQUEST, message="Sell system and commodity are required.")
        if max_distance > MAX_DISTANCE_LY:
            return SearchResult(
                BAD_REQUEST,
                message="Spansh searches are kept within {:.0f} ly.".format(MAX_DISTANCE_LY))

        # The window's start moves with the clock, so it stays out of the key.
        key = (system.casefold(), commodity_name.casefold(), round(float(max_distance), 1),
               int(min_volume), bool(large_pad_only), float(updated_within_h), int(size))
        if use_cache:
            with self._cache_lock:
                entry = self._cache.get(key)
            if entry is not None and time.monotonic() - entry[0] <= self.cache_ttl:
                result = entry[1]
                return SearchResult(kind=result.kind, rows=result.rows,
                                    message=result.message, url=result.url, cached=True)

        body = self.build_body(system, commodity_name, max_distance, min_volume,
                               large_pad_only, updated_within_h, size,
                               now or datetime.now(timezone.utc))
        result = self._request(body, commodity_name)
        if result.kind in (OK, NOT_FOUND):
            with self._cache_lock:
                self._cache[key] = (time.monotonic(), result)
        return result

    def _request(self, body: Dict[str, Any], commodity_name: str) -> SearchResult:
        url = self.base_url + SEARCH_PATH
        self._limiter.wait()
        try:
            status, text = self._post(url, body)
        except Exception as exc:
            logger.debug("Spansh request failed: %s", exc)
            return SearchResult(NETWORK, message="Could not reach Spansh: {}".format(exc), url=url)

        if status == 400:
            # Spansh does not say which part it rejected. The body is built here
            # from validated values, so the system name is the likely culprit.
            return SearchResult(NOT_FOUND, url=url,
                                message="Spansh rejected the search - it may not know that system.")
        if status >= 400:
            return SearchResult(HTTP, url=url, message="Spansh returned HTTP {}.".format(status))

        try:
            payload = json.loads(text)
        except ValueError as exc:
            return SearchResult(HTTP, url=url,
                                message="Spansh sent a malformed response: {}".format(exc))

        stations = payload.get("results") if isinstance(payload, dict) else None
        rows: List[Dict[str, Any]] = []
        for station in stations or ():
            row = normalise_row(station, commodity_name)
            if row is not None:
                rows.append(row)
        return SearchResult(OK, rows=rows, url=url)


def _self_test() -> None:
    assert PRICE_FIELD == "buy_price" and VOLUME_FIELD == "supply"
    assert "Drake-Class Carrier" not in STATION_TYPES

    station = {
        "name": "Thorn Constructions", "system_name": "HR 5518", "market_id": 4359005955,
        "system_id64": 83986944778, "type": "Planetary Outpost",
        "system_x": 84.15625, "system_y": 238.78125, "system_z": 266.625,
        "distance": 55.1, "distance_to_arrival": 608.0,
        "large_pads": 2, "medium_pads": 0, "small_pads": 4, "has_large_pad": True,
        "market_updated_at": "2026-10-10T12:24:41Z",
        "market": [
            {"commodity": "Computer Components", "buy_price": 549, "sell_price": 509,
             "supply": 125320, "demand": 1},
            {"commodity": "Superconductors", "buy_price": 6741, "sell_price": 6595,
             "supply": 596, "demand": 1},
        ],
    }
    row = normalise_row(station, "superconductors")
    assert row["buyPrice"] == 6741 and row["stock"] == 596, row
    assert row["maxLandingPadSize"] == 3 and row["marketId"] == 4359005955, row
    assert row["updatedAt"] == "2026-10-10T12:24:41Z"
    assert normalise_row(station, "Gold") is None

    client = SpanshClient(user_agent="EDTradeAssist-selftest/0")
    assert client.stations_selling("", "Gold", 50, 100, True, 1).kind == BAD_REQUEST
    assert client.stations_selling("Sol", "Gold", 600, 100, True, 1).kind == BAD_REQUEST

    now = datetime(2026, 10, 10, 13, 0, 0, tzinfo=timezone.utc)
    body = client.build_body("Sol", "Gold", 80, 545, True, 6, PAGE_SIZE, now)
    assert body["reference_system"] == "Sol" and body["size"] == PAGE_SIZE
    assert body["sort"] == [{"distance": {"direction": "asc"}}]
    assert body["filters"]["market"][0]["supply"]["value"][0] == 545
    assert body["filters"]["market_updated_at"]["value"][0] == "2026-10-10T07:00:00Z"
    assert body["filters"]["has_large_pad"] == {"value": True}
    assert "has_large_pad" not in client.build_body(
        "Sol", "Gold", 80, 545, False, 6, PAGE_SIZE, now)["filters"]

    print("edta_spansh self-test: OK")


if __name__ == "__main__":
    _self_test()
