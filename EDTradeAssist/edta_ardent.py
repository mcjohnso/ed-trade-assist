"""Ardent Insight HTTP client for EDTradeAssist.

One endpoint does the whole job:

    GET /v2/system/name/{system}/commodity/name/{commodity}/nearby/exports
        ?maxDistance=&minVolume=&fleetCarriers=0&maxDaysAgo=

DIRECTION - the easiest thing here to get backwards. Ardent names its endpoints
from the STATION's point of view. A station that *exports* a commodity is selling
it to you, so the player buying maps to /exports and the price you pay is
`buyPrice`. (/imports + `sellPrice` is the player selling, which this plugin does
not query.) Both are pinned in ENDPOINT/PRICE_FIELD below and asserted in tests.

Upstream behaviours worth knowing, each handled explicitly below:
  * maxDistance is SILENTLY clamped to 500 ly - asking for more returns 200 with a
    quietly narrower answer, so we never ask for more.
  * 404 means the SYSTEM is unknown. A misspelled commodity does NOT 404 - it
    returns 200 with an empty list, indistinguishable from "nothing matched".
    (Verified live 2026-09-05: /system/name/Zzzznotasystem/... -> 404, while
    /commodity/name/notacommodity/... -> 200 [].) That is why the commodity is
    validated against the bundled FDevIDs table BEFORE any search is sent.
  * There is no sort parameter and no row limit: /nearby caps at 1000 rows
    server-side, so any ordering is over the fetched window and we say so.

Nothing here raises: every call returns a SearchResult, so a worker thread cannot
be killed by an upstream hiccup.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ardent-insight.com"

#: Player buys -> the station exports. See the module docstring.
ENDPOINT = "exports"
PRICE_FIELD = "buyPrice"
VOLUME_FIELD = "stock"

#: Ardent clamps anything larger, quietly.
MAX_DISTANCE_LY = 500.0

#: /nearby/* stops at this many rows server-side; beyond it the window is partial.
ROW_CAP = 1000

DEFAULT_TIMEOUT = 20.0
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_CACHE_TTL = 300.0

OK = "ok"
NOT_FOUND = "not_found"
NETWORK = "network"
HTTP = "http"
BAD_REQUEST = "bad_request"


@dataclass
class SearchResult:
    """Rows in, or a reason there are none. `kind` is what to branch on."""

    kind: str = OK
    rows: List[Dict[str, Any]] = field(default_factory=list)
    message: str = ""
    url: str = ""
    cached: bool = False
    window_capped: bool = False

    @property
    def ok(self) -> bool:
        return self.kind == OK


class _RateLimiter:
    """A minimum interval between requests. Ardent publishes no rate limit, and
    absence of a published limit is not permission."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


class ArdentClient:
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
        """requests is bundled with EDMC; build the session lazily so importing
        this module never depends on it. Returns None when requests is absent,
        in which case we fall back to urllib (see _fetch)."""
        if self._session is not None:
            return self._session
        try:
            import requests
            from requests.adapters import HTTPAdapter
            try:
                from urllib3.util.retry import Retry
            except ImportError:                               # pragma: no cover
                from requests.packages.urllib3.util.retry import Retry
        except ImportError:
            logger.debug("requests unavailable; using urllib")
            return None

        session = requests.Session()
        session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        })
        retry = Retry(total=2, backoff_factor=1,
                      status_forcelist=[500, 502, 503, 504],
                      allowed_methods=["GET"], raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._session = session
        return session

    def _fetch(self, url: str, params: Dict[str, Any]) -> Tuple[int, str, str]:
        """(status, body, final_url). Raises only transport errors, which
        _request turns into a SearchResult."""
        session = self._get_session()
        if session is not None:
            response = session.get(url, params=params, timeout=self.timeout)
            return response.status_code, response.text, response.url

        # stdlib fallback - keeps the plugin working, and lets the live check in
        # tools/ run outside EDMC where requests may not be installed.
        import urllib.error
        import urllib.request

        full = url + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(full, headers={
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8"), response.geturl()
        except urllib.error.HTTPError as exc:
            return exc.code, "", full

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    # --- cache --------------------------------------------------------------

    def _cache_get(self, key: Tuple) -> Optional[SearchResult]:
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            fetched_at, result = entry
            if time.monotonic() - fetched_at > self.cache_ttl:
                self._cache.pop(key, None)
                return None
        clone = SearchResult(kind=result.kind, rows=result.rows, message=result.message,
                             url=result.url, cached=True,
                             window_capped=result.window_capped)
        return clone

    def _cache_put(self, key: Tuple, result: SearchResult) -> None:
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), result)

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    # --- the search ---------------------------------------------------------

    def nearby_exports(self, system: str, commodity: str, max_distance: float,
                       min_volume: int, max_days_ago: Optional[int] = None,
                       use_cache: bool = True) -> SearchResult:
        """Markets near `system` that will sell `commodity` to you."""
        system = (system or "").strip()
        commodity = (commodity or "").strip()
        if not system or not commodity:
            return SearchResult(BAD_REQUEST, message="Sell system and commodity are required.")
        if max_distance > MAX_DISTANCE_LY:
            # Refuse rather than relay a quietly narrower answer.
            return SearchResult(
                BAD_REQUEST,
                message="Ardent caps the search radius at {:.0f} ly.".format(MAX_DISTANCE_LY))

        path = "/v2/system/name/{}/commodity/name/{}/nearby/{}".format(
            urllib.parse.quote(system, safe=""),
            urllib.parse.quote(commodity, safe=""),
            ENDPOINT)
        params = {
            "maxDistance": round(float(max_distance), 1),
            "minVolume": int(min_volume),
            "fleetCarriers": 0,          # carriers are excluded outright
        }
        if max_days_ago is not None:
            params["maxDaysAgo"] = int(max_days_ago)

        key = (path, tuple(sorted(params.items())))
        if use_cache:
            hit = self._cache_get(key)
            if hit is not None:
                return hit

        result = self._request(path, params)
        if result.kind in (OK, NOT_FOUND):
            self._cache_put(key, result)
        return result

    def _request(self, path: str, params: Dict[str, Any]) -> SearchResult:
        url = self.base_url + path
        self._limiter.wait()
        try:
            status, body, final_url = self._fetch(url, params)
        except Exception as exc:
            logger.debug("Ardent request failed: %s", exc)
            return SearchResult(NETWORK, message="Could not reach Ardent: {}".format(exc), url=url)

        if status == 404:
            # Only an unknown system 404s; see the module docstring.
            return SearchResult(
                NOT_FOUND, url=final_url,
                message="Ardent has never heard of that system. Check the spelling.")
        if status >= 400:
            return SearchResult(HTTP, url=final_url,
                                message="Ardent returned HTTP {}.".format(status))

        try:
            payload = json.loads(body)
        except ValueError as exc:
            return SearchResult(HTTP, url=final_url,
                                message="Ardent sent a malformed response: {}".format(exc))

        rows = payload if isinstance(payload, list) else []
        return SearchResult(OK, rows=rows, url=final_url,
                            window_capped=len(rows) >= ROW_CAP)


def _self_test() -> None:
    client = ArdentClient(user_agent="EDTradeAssist-selftest/0")

    assert ENDPOINT == "exports" and PRICE_FIELD == "buyPrice"

    too_far = client.nearby_exports("Sol", "gold", 600, 100)
    assert too_far.kind == BAD_REQUEST and "500" in too_far.message, too_far

    for bad in (("", "gold"), ("Sol", "")):
        assert client.nearby_exports(bad[0], bad[1], 50, 100).kind == BAD_REQUEST

    limiter = _RateLimiter(0.25)
    limiter.wait()                       # first call never waits
    started = time.perf_counter()
    limiter.wait()                       # second call is spaced out
    waited = time.perf_counter() - started
    assert waited >= 0.2, waited          # clock granularity, not a real 0.25

    # Cache round trip, without any network.
    key = ("k", ())
    client._cache_put(key, SearchResult(OK, rows=[{"a": 1}]))
    hit = client._cache_get(key)
    assert hit is not None and hit.cached and hit.rows == [{"a": 1}]
    client.clear_cache()
    assert client._cache_get(key) is None

    print("edta_ardent self-test: OK")


if __name__ == "__main__":
    _self_test()
