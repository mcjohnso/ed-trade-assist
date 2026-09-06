"""Pure logic for EDTradeAssist: ranking, jump maths, formatting.

No EDMC imports, no network, no tkinter - everything in here is a function of its
arguments so tests/test_core.py can exercise it without the game or a plugin host.

The ranking order implemented by rank_key() is, in priority order:

    1. supply          (hard FILTER only: stock >= margin x cargo capacity)
    2. landing pad     (hard filter: maxLandingPadSize >= requested)
    3. data freshness  (bucketed)
    4. round-trip jumps
    5. distance to arrival (bucketed)
    6. landing pad size, largest first
    7. buy price       (final tiebreak)

Supply never ranks. Once a market holds enough to fill the hold, more of it is
worth nothing - a market with 400,000 t is no better than one with 500 t.

Distance never ranks either: jumps are the cost that is actually paid, and two
systems reachable in the same number of jumps are equally far away in the only
sense that matters.

Freshness and arrival distance are BUCKETED on purpose. `updatedAt` is unique to
the second and `distanceToArrival` carries six decimal places, so ranking either
one raw makes it a total order that no later tier can ever reach - price would be
dead code and jumps would never break a freshness tie.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --- constants ---------------------------------------------------------------

#: Derating applied to a ship's maximum jump range to estimate real jump counts.
#: Stars are not placed conveniently; 0.85 is the usual figure inside the bubble.
JUMP_EFFICIENCY = 0.85

#: Ardent hard-clamps maxDistance at 500 ly and returns 200 with a quietly
#: narrower answer if you ask for more, so we never ask for more.
MAX_RADIUS_LY = 500.0
MIN_RADIUS_LY = 10.0

#: Freshness bucket edges, in hours. A row older than max_age_days is excluded.
#: The first band is narrow: a commodity being actively hauled can drain a market
#: within the hour, so an hour-old reading is genuinely less trustworthy than a
#: ten-minute-old one. Inside a band, fewer jumps wins.
FRESHNESS_EDGES_H: Tuple[float, ...] = (1.0, 6.0, 24.0, 72.0, 168.0)

#: Distance-to-arrival bucket edges, in light seconds.
LS_EDGES: Tuple[float, ...] = (100.0, 500.0, 1000.0, 2500.0, 5000.0)

_PAD_TO_INT = {"S": 1, "M": 2, "L": 3}
_INT_TO_PAD = {1: "S", 2: "M", 3: "L"}

#: Station types excluded outright. Fleet carrier stock moves without warning and
#: the carrier can jump away between the upload and your arrival.
EXCLUDED_STATION_TYPES = frozenset({"fleetcarrier"})


# --- small conversions -------------------------------------------------------

def pad_to_int(pad: Any) -> Optional[int]:
    """'L'/'M'/'S' or 1/2/3 -> 1/2/3. None when unknown."""
    if isinstance(pad, bool):
        return None
    if isinstance(pad, (int, float)):
        value = int(pad)
        return value if value in _INT_TO_PAD else None
    if isinstance(pad, str):
        return _PAD_TO_INT.get(pad.strip().upper()[:1])
    return None


def pad_to_str(pad: Any) -> str:
    value = pad_to_int(pad)
    return _INT_TO_PAD.get(value, "?") if value else "?"


def parse_updated_at(value: Any) -> Optional[datetime]:
    """Parse Ardent's ISO-8601 updatedAt ('2026-09-05T19:16:08.000Z')."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def distance_ly(a: Sequence[float], b: Sequence[float]) -> float:
    """Straight-line distance between two (x, y, z) points, in light years."""
    return math.dist((a[0], a[1], a[2]), (b[0], b[1], b[2]))


def estimate_jumps(distance: float, jump_range: float,
                   efficiency: float = JUMP_EFFICIENCY) -> int:
    """Rough jump count for a distance. An ESTIMATE - it knows nothing about the
    actual star field, so label it as one wherever it is shown."""
    if distance <= 0:
        return 0
    effective = jump_range * efficiency
    if effective <= 0:
        return 0
    return max(1, math.ceil(distance / effective))


def round_trip_jumps(distance: float, unladen_range: float,
                     laden_range: float) -> Tuple[int, int]:
    """(out, back) - empty on the way to the buy station, full on the way back."""
    return (estimate_jumps(distance, unladen_range),
            estimate_jumps(distance, laden_range))


def bucket_of(value: Optional[float], edges: Sequence[float]) -> int:
    """Index of the first edge `value` falls under; len(edges) when it exceeds
    them all. A missing value sorts last, never first - unknown is not fresh."""
    if value is None:
        return len(edges)
    for index, edge in enumerate(edges):
        if value < edge:
            return index
    return len(edges)


# --- formatting --------------------------------------------------------------

def human_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "age unknown"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return "%ds ago" % int(seconds)
    minutes = seconds / 60.0
    if minutes < 90:
        return "%dm ago" % int(round(minutes))
    hours = minutes / 60.0
    if hours < 36:
        return "%.1fh ago" % hours
    return "%.1fd ago" % (hours / 24.0)


def human_ls(ls: Optional[float]) -> str:
    if ls is None:
        return "? Ls"
    if ls < 1000:
        return "{:,.0f} Ls".format(ls)
    return "{:,.1f}k Ls".format(ls / 1000.0)


def human_duration(seconds: Optional[float]) -> str:
    """A SPAN of time, not an age. human_age() cannot be reused for this: its
    ' ago' suffix is hard-coded and its bands are tuned for staleness.

    The minor unit is zero-padded so the field keeps its width and the overlay
    line does not shuffle sideways every time a minute ticks over.
    """
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    if minutes < 60:
        return "{}m".format(minutes)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "{}h{:02d}m".format(hours, minutes)
    days, hours = divmod(hours, 24)
    return "{}d{:02d}h".format(days, hours)


def human_credits(value: Optional[float]) -> str:
    """Compact credits, because the session line carries four fields.

    Negatives carry a '-' but positives carry no '+', unlike profit_lines()
    below: that signs a per-run DELTA, where the sign is the whole decision.
    These are totals and rates, where a leading plus is just noise. human_ls()
    is the precedent for spelling the magnitude with a suffix.
    """
    if value is None:
        return "? cr"
    magnitude = abs(float(value))
    sign = "-" if value < 0 else ""
    if magnitude < 1_000_000:
        return "{}{:,.0f} cr".format(sign, magnitude)
    if magnitude < 1_000_000_000:
        return "{}{:,.1f}M cr".format(sign, magnitude / 1_000_000.0)
    return "{}{:,.2f}B cr".format(sign, magnitude / 1_000_000_000.0)


def thousands(value: Optional[float]) -> str:
    return "?" if value is None else "{:,}".format(int(value))


# --- the search request ------------------------------------------------------

@dataclass(frozen=True)
class SearchSpec:
    """Everything the ranking needs, resolved from the session parameters."""

    commodity_ardent: str
    commodity_display: str
    sell_system: str
    cargo_capacity: int
    unladen_range: float
    laden_range: float
    min_pad: int = 3
    supply_margin: float = 1.5
    jumps_per_leg: int = 10
    max_age_days: int = 7
    freshness_edges_h: Tuple[float, ...] = FRESHNESS_EDGES_H
    ls_edges: Tuple[float, ...] = LS_EDGES
    sell_coords: Optional[Tuple[float, float, float]] = None

    @property
    def min_volume(self) -> int:
        """Supply a candidate must hold. The margin exists because the number is
        crowd-sourced: a market holding exactly your hold size when it was last
        uploaded may be partly sold out by the time you dock."""
        return max(1, math.ceil(self.cargo_capacity * self.supply_margin))

    @property
    def radius_ly(self) -> float:
        """The FURTHEST the search will ever look, derived from the laden jump
        range. The search does not start here - see rings()."""
        raw = self.jumps_per_leg * self.laden_range * JUMP_EFFICIENCY
        return round(min(MAX_RADIUS_LY, max(MIN_RADIUS_LY, raw)), 1)

    @property
    def first_ring_ly(self) -> float:
        """One laden jump. Where the search starts."""
        return round(max(MIN_RADIUS_LY, self.laden_range * JUMP_EFFICIENCY), 1)


def rings(spec: SearchSpec, limit: int = 5) -> List[float]:
    """Radii to search, NEAREST FIRST, doubling out to spec.radius_ly.

    Searching the full radius in one request is what broke v0.2.0. Ardent's
    /nearby endpoint stops at 1000 rows and has no sort parameter, so a wide
    search returns an ARBITRARY 1000 of the matches - and the nearest markets are
    routinely not among them. Observed live: Ega/Palladium at 229 ly returned a
    truncated window whose closest row was 40 ly, while the same search at 92 ly
    came back complete and held a qualifying market at 30 ly.

    Starting small and expanding keeps the near neighbourhood complete, which is
    the part the ranking cares about most.
    """
    ceiling = spec.radius_ly
    out: List[float] = []
    radius = min(spec.first_ring_ly, ceiling)
    while len(out) < limit:
        value = round(min(radius, ceiling), 1)
        if value not in out:
            out.append(value)
        if value >= ceiling:
            break
        radius = radius * 2.0
    return out


def widen(radius: float) -> float:
    """Next radius to try. Returns the same value once the upstream cap is
    reached, so callers can detect 'cannot widen further'."""
    return round(min(MAX_RADIUS_LY, radius * 2.0), 1)


# --- candidates --------------------------------------------------------------

@dataclass
class Candidate:
    """One ranked buy location, built from an Ardent /nearby/exports row."""

    system: str
    station: str
    market_id: Optional[int] = None
    system_address: Optional[int] = None
    station_type: str = ""
    coords: Optional[Tuple[float, float, float]] = None
    distance: float = 0.0
    stock: int = 0
    buy_price: int = 0
    pad: Optional[int] = None
    ls: Optional[float] = None
    updated_at: Optional[datetime] = None
    age_seconds: Optional[float] = None
    jumps_out: int = 0
    jumps_back: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def jumps_total(self) -> int:
        return self.jumps_out + self.jumps_back

    @property
    def pad_label(self) -> str:
        return pad_to_str(self.pad)


def candidate_from_row(row: Dict[str, Any], spec: SearchSpec,
                       now: Optional[datetime] = None) -> Optional[Candidate]:
    """Build a Candidate from one Ardent row, or None if the row is unusable."""
    if not isinstance(row, dict):
        return None
    # Settlements and outposts with no market come back with a null commodity.
    if row.get("commodityName") in (None, ""):
        return None
    system = (row.get("systemName") or "").strip()
    station = (row.get("stationName") or "").strip()
    if not system or not station:
        return None

    now = now or datetime.now(timezone.utc)
    updated = parse_updated_at(row.get("updatedAt"))
    age = (now - updated).total_seconds() if updated else None

    coords = None
    if all(row.get(k) is not None for k in ("systemX", "systemY", "systemZ")):
        coords = (float(row["systemX"]), float(row["systemY"]), float(row["systemZ"]))

    # Prefer an exact distance computed from coordinates; Ardent's own `distance`
    # is rounded to whole light years.
    if coords and spec.sell_coords:
        distance = distance_ly(spec.sell_coords, coords)
    else:
        distance = float(row.get("distance") or 0.0)

    out, back = round_trip_jumps(distance, spec.unladen_range, spec.laden_range)

    return Candidate(
        system=system,
        station=station,
        market_id=_int_or_none(row.get("marketId")),
        system_address=_int_or_none(row.get("systemAddress")),
        station_type=(row.get("stationType") or "").strip(),
        coords=coords,
        distance=distance,
        stock=int(row.get("stock") or 0),
        buy_price=int(row.get("buyPrice") or 0),
        pad=pad_to_int(row.get("maxLandingPadSize")),
        ls=_float_or_none(row.get("distanceToArrival")),
        updated_at=updated,
        age_seconds=age,
        jumps_out=out,
        jumps_back=back,
        raw=row,
    )


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_excluded_station(candidate: Candidate) -> bool:
    return candidate.station_type.replace(" ", "").casefold() in EXCLUDED_STATION_TYPES


def passes_filters(candidate: Candidate, spec: SearchSpec) -> bool:
    """Hard filters, in the stated priority order: carriers, supply, pad, age."""
    if is_excluded_station(candidate):
        return False
    if candidate.stock < spec.min_volume:
        return False
    if candidate.pad is None or candidate.pad < spec.min_pad:
        return False
    if candidate.buy_price <= 0:
        return False
    if candidate.age_seconds is None or candidate.age_seconds > spec.max_age_days * 86400.0:
        return False
    return True


def rank_key(candidate: Candidate, spec: SearchSpec) -> Tuple[int, int, int, int, int]:
    """The lexicographic sort key. Lower is better at every position."""
    age_hours = None if candidate.age_seconds is None else candidate.age_seconds / 3600.0
    return (
        bucket_of(age_hours, spec.freshness_edges_h),   # 3. freshness
        candidate.jumps_total,                          # 4. round-trip jumps
        bucket_of(candidate.ls, spec.ls_edges),         # 5. distance to arrival
        -(candidate.pad or 0),                          # 6. bigger pad wins
        candidate.buy_price,                            # 7. price paid
    )


def rank_candidates(rows: Iterable[Dict[str, Any]], spec: SearchSpec,
                    now: Optional[datetime] = None) -> List[Candidate]:
    """Rows in, ranked candidates out. Rejected rows are simply absent."""
    now = now or datetime.now(timezone.utc)
    kept = []
    for row in rows or ():
        candidate = candidate_from_row(row, spec, now=now)
        if candidate is not None and passes_filters(candidate, spec):
            kept.append(candidate)
    kept.sort(key=lambda c: rank_key(c, spec))
    return kept


# --- presentation ------------------------------------------------------------

def describe_candidate(candidate: Candidate, spec: SearchSpec) -> List[str]:
    """The full picture, as lines. Used by both the overlay and the EDMC panel -
    the panel is the fallback when no overlay is installed, so it must not be a
    shortened version of this."""
    return [
        "BUY {} @ {}".format(spec.commodity_display, candidate.station),
        "{} - {:.1f} ly - ~{}+{} jumps (est)".format(
            candidate.system, candidate.distance, candidate.jumps_out, candidate.jumps_back),
        "{} t - {} cr/t - {}".format(
            thousands(candidate.stock), thousands(candidate.buy_price),
            human_age(candidate.age_seconds)),
        "{} pad - {} - {}".format(
            candidate.pad_label, candidate.station_type or "station", human_ls(candidate.ls)),
    ]


def describe_return(spec: SearchSpec, distance: float,
                    cargo: Optional[int] = None) -> List[str]:
    laden = estimate_jumps(distance, spec.laden_range)
    lines = [
        "SELL {} @ {}".format(spec.commodity_display, spec.sell_system),
        "{:.1f} ly - ~{} jumps laden (est)".format(distance, laden),
    ]
    if cargo is not None:
        lines.append("Hold: {}/{} t".format(thousands(cargo), thousands(spec.cargo_capacity)))
    return lines


def profit_lines(buy_price: int, sell_price: Optional[int],
                 cargo_capacity: int) -> List[str]:
    """Profit is only shown when a real sell price is known - it is read from the
    game's own Market.json while docked in the sell system, never guessed."""
    if not sell_price or sell_price <= 0:
        return []
    per_tonne = sell_price - buy_price
    return ["{:+,} cr/t - {:+,} cr/full hold".format(per_tonne, per_tonne * cargo_capacity)]


#: Below this, extrapolating an hourly rate is fantasy - a full hold sold forty
#: seconds after Start would read as a billion an hour. The rate shows as
#: "? cr/hr" until enough time has passed for it to mean anything.
RATE_MIN_SECONDS = 300.0


def session_lines(elapsed_seconds: Optional[float], runs: int, credits: int,
                  estimated: bool = False) -> List[str]:
    """The realised total for the session so far, as at most one line.

    Primitives in, strings out - the same split as profit_lines() above: core
    decides how a figure is spelled, the session decides whether to ask. The
    caller gates on having made a sale at all; this only refuses to divide by a
    length of time it cannot use.
    """
    if elapsed_seconds is None:
        return []
    mark = "~" if estimated else ""
    fields = [human_duration(elapsed_seconds)]
    if runs:
        fields.append("1 run" if runs == 1 else "{} runs".format(runs))
    fields.append(mark + human_credits(credits))
    if elapsed_seconds >= RATE_MIN_SECONDS:
        rate = credits * 3600.0 / elapsed_seconds
        fields.append(mark + human_credits(rate) + "/hr")
    else:
        fields.append(human_credits(None) + "/hr")
    return ["Session: " + " - ".join(fields)]


# --- self-test ---------------------------------------------------------------

def _self_test() -> None:
    from datetime import timedelta

    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)

    def iso(when):
        return when.isoformat().replace("+00:00", "Z")

    def row(**kw):
        base = {
            "commodityName": "gold", "systemName": "Test", "stationName": "Port",
            "stationType": "Coriolis", "maxLandingPadSize": 3, "stock": 5000,
            "buyPrice": 45000, "distance": 20, "distanceToArrival": 300,
            "updatedAt": iso(now - timedelta(minutes=10)),
        }
        base.update(kw)
        return base

    spec = SearchSpec(commodity_ardent="gold", commodity_display="Gold",
                      sell_system="Sol", cargo_capacity=720,
                      unladen_range=50.0, laden_range=40.0)

    assert spec.min_volume == 1080, spec.min_volume
    assert spec.radius_ly == 340.0, spec.radius_ly
    assert widen(340.0) == 500.0 and widen(500.0) == 500.0

    assert pad_to_int("l") == 3 and pad_to_int(2) == 2 and pad_to_int("x") is None
    assert pad_to_str(1) == "S" and pad_to_str(None) == "?"
    assert estimate_jumps(0, 50) == 0 and estimate_jumps(1, 50) == 1
    assert estimate_jumps(100, 50) == 3          # 100 / (50*0.85) = 2.35 -> 3
    assert round_trip_jumps(100, 50, 25) == (3, 5)
    assert bucket_of(None, (1, 2)) == 2 and bucket_of(0.5, (1, 2)) == 0

    # Carriers are excluded even if the upstream filter missed them.
    assert rank_candidates([row(stationType="FleetCarrier")], spec, now=now) == []
    # Not enough supply for 1.5x the hold.
    assert rank_candidates([row(stock=1079)], spec, now=now) == []
    # Pad too small.
    assert rank_candidates([row(maxLandingPadSize=2)], spec, now=now) == []
    # Too stale.
    assert rank_candidates([row(updatedAt=iso(now - timedelta(days=8)))], spec, now=now) == []
    # No market on the row at all.
    assert rank_candidates([row(commodityName=None)], spec, now=now) == []

    def order(rows):
        return [c.station for c in rank_candidates(rows, spec, now=now)]

    # Tier 3: freshness bucket beats everything below it.
    fresher = row(stationName="Fresh", distance=200, distanceToArrival=9000, buyPrice=99000)
    staler = row(stationName="Stale", distance=1, distanceToArrival=10, buyPrice=1,
                 updatedAt=iso(now - timedelta(hours=30)))
    assert order([staler, fresher]) == ["Fresh", "Stale"]

    # Tier 4: inside one freshness bucket, fewer round-trip jumps wins.
    near = row(stationName="Near", distance=20, distanceToArrival=9000, buyPrice=99000)
    far = row(stationName="Far", distance=300, distanceToArrival=10, buyPrice=1)
    assert order([far, near]) == ["Near", "Far"]

    # Tier 5: same freshness and jumps -> closer arrival wins, price ignored.
    close_ls = row(stationName="Close", distanceToArrival=50, buyPrice=99000)
    far_ls = row(stationName="FarLs", distanceToArrival=4000, buyPrice=1)
    assert order([far_ls, close_ls]) == ["Close", "FarLs"]

    # Tier 6: all else equal, a bigger pad wins. Asking for M does not mean
    # preferring M - an L pad station is strictly easier to use.
    med_spec = SearchSpec(commodity_ardent="gold", commodity_display="Gold",
                          sell_system="Sol", cargo_capacity=720, unladen_range=50.0,
                          laden_range=40.0, min_pad=2)
    med = row(stationName="Medium", maxLandingPadSize=2, buyPrice=40000)
    large = row(stationName="Large", maxLandingPadSize=3, buyPrice=45000)
    assert [c.station for c in rank_candidates([med, large], med_spec, now=now)] == \
        ["Large", "Medium"]

    # Tier 7: everything above equal -> cheapest wins. Proves price is reachable.
    assert order([row(stationName="Dear", buyPrice=60000),
                  row(stationName="Cheap", buyPrice=40000)]) == ["Cheap", "Dear"]

    # Supply is a filter, never a ranking: a market with 400x the stock is no
    # better once both can fill the hold.
    assert order([row(stationName="Huge", stock=400000, buyPrice=50000),
                  row(stationName="Enough", stock=1100, buyPrice=40000)]) == ["Enough", "Huge"]

    # Raw distance does not rank; jumps do. These two are the same number of
    # jumps apart, so the cheaper one wins despite being further.
    assert order([row(stationName="Nearer", distance=21, buyPrice=50000),
                  row(stationName="Further", distance=30, buyPrice=40000)]) == ["Further", "Nearer"]

    # Rings start at one laden jump and double out to the configured ceiling.
    assert spec.first_ring_ly == 34.0, spec.first_ring_ly
    assert rings(spec) == [34.0, 68.0, 136.0, 272.0, 340.0], rings(spec)
    tight = SearchSpec(commodity_ardent="gold", commodity_display="Gold",
                       sell_system="Sol", cargo_capacity=1, unladen_range=38.0,
                       laden_range=27.0, jumps_per_leg=10)
    assert rings(tight) == [22.9, 45.8, 91.6, 183.2, 229.5], rings(tight)
    assert rings(tight)[-1] == tight.radius_ly
    # A short ceiling collapses to a single ring rather than repeating it.
    assert rings(SearchSpec(commodity_ardent="g", commodity_display="G", sell_system="S",
                            cargo_capacity=1, unladen_range=10.0, laden_range=10.0,
                            jumps_per_leg=1)) == [MIN_RADIUS_LY]

    # Exact coordinates beat Ardent's rounded integer distance when we have them.
    spec_xyz = SearchSpec(commodity_ardent="gold", commodity_display="Gold",
                          sell_system="Sol", cargo_capacity=720, unladen_range=50.0,
                          laden_range=40.0, sell_coords=(0.0, 0.0, 0.0))
    got = rank_candidates([row(systemX=3.0, systemY=4.0, systemZ=0.0, distance=99)],
                          spec_xyz, now=now)
    assert abs(got[0].distance - 5.0) < 1e-9, got[0].distance

    assert human_age(30) == "30s ago" and human_age(3600).endswith("m ago")
    assert human_ls(50) == "50 Ls" and human_ls(1500) == "1.5k Ls"
    assert profit_lines(40000, 0, 720) == []
    assert profit_lines(40000, 45000, 720) == ["+5,000 cr/t - +3,600,000 cr/full hold"]

    assert human_duration(None) == "?"
    assert human_duration(-5) == "0m" and human_duration(0) == "0m"
    assert human_duration(59) == "0m" and human_duration(90) == "1m"
    assert human_duration(3599) == "59m" and human_duration(3600) == "1h00m"
    assert human_duration(4320) == "1h12m" and human_duration(5430) == "1h30m"
    assert human_duration(86400) == "1d00h" and human_duration(183600) == "2d03h"

    assert human_credits(None) == "? cr"
    assert human_credits(0) == "0 cr" and human_credits(42000) == "42,000 cr"
    assert human_credits(-1500) == "-1,500 cr"
    assert human_credits(18_400_000) == "18.4M cr"
    assert human_credits(-18_400_000) == "-18.4M cr"
    assert human_credits(1_050_000_000) == "1.05B cr"

    # The approved overlay line, exactly.
    assert session_lines(4320.0, 4, 18_400_000) == [
        "Session: 1h12m - 4 runs - 18.4M cr - 15.3M cr/hr"], session_lines(4320.0, 4, 18_400_000)
    assert session_lines(None, 0, 0) == []
    # Start pressed mid-haul: no completed round trip yet, so no runs field.
    assert "runs" not in session_lines(4320.0, 0, 18_400_000)[0]
    assert session_lines(3600.0, 1, 1_000_000)[0].count(" - 1 run - ") == 1
    # Too early to extrapolate, and the zero case cannot divide by zero.
    assert session_lines(0.0, 1, 500_000) == ["Session: 0m - 1 run - 500,000 cr - ? cr/hr"]
    assert "? cr/hr" in session_lines(60.0, 1, 500_000)[0]
    assert session_lines(3600.0, 1, 1_000_000, estimated=True)[0].count("~") == 2

    lines = describe_candidate(rank_candidates([row()], spec, now=now)[0], spec)
    assert lines[0] == "BUY Gold @ Port" and "est" in lines[1]

    print("edta_core self-test: OK")


if __name__ == "__main__":
    _self_test()
