"""Session state machine for EDTradeAssist.

The haul is a two-stop loop, and the plugin's whole job is to know which stop you
are between:

    dock in the sell system  -> search -> TO_BUY   (buy system on the clipboard)
    dock at the buy station  ->           TO_SELL  (sell system on the clipboard)

No tkinter, no EDMC, no network in here: journal events go in, Effects come out,
and load.py performs them on the Tk thread. search_best() takes a `fetch`
callable so the worker thread can run the escalation without touching session
state. That split is what makes tests/test_core.py able to drive a whole cycle.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import edta_commodities
import edta_core as core

# --- states ------------------------------------------------------------------

OFF = "off"
SEARCHING = "searching"
TO_BUY = "to_buy"
TO_SELL = "to_sell"
NO_RESULTS = "no_results"
ERROR = "error"

# --- effects load.py performs ------------------------------------------------

SEARCH = "search"
CLIPBOARD = "clipboard"
REDRAW = "redraw"


@dataclass(frozen=True)
class Effect:
    kind: str
    payload: Any = None


# --- parameters --------------------------------------------------------------

@dataclass
class Params:
    """Raw text from the panel, before validation."""

    sell_system: str = ""
    commodity: str = ""
    cargo_capacity: str = ""
    unladen_range: str = ""
    laden_range: str = ""
    min_pad: str = "L"
    # Tuning, from the prefs tab. Carried through validate() so a run started
    # from the panel uses the configured bands, not the code defaults.
    supply_margin: float = 1.5
    jumps_per_leg: int = 10
    max_age_days: int = 7
    freshness_edges_h: Tuple[float, ...] = core.FRESHNESS_EDGES_H
    ls_edges: Tuple[float, ...] = core.LS_EDGES


@dataclass
class Validation:
    ok: bool
    spec: Optional[core.SearchSpec] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _positive_number(text: str, label: str, errors: List[str],
                     integer: bool = False) -> Optional[float]:
    raw = (text or "").strip().replace(",", "")
    if not raw:
        errors.append("{} is required.".format(label))
        return None
    try:
        value = float(raw)
    except ValueError:
        errors.append("{} must be a number.".format(label))
        return None
    if value <= 0:
        errors.append("{} must be greater than zero.".format(label))
        return None
    return int(value) if integer else value


def validate(params: Params) -> Validation:
    """Turn panel text into a SearchSpec, or into messages explaining why not."""
    errors: List[str] = []
    warnings: List[str] = []

    sell_system = (params.sell_system or "").strip()
    if not sell_system:
        errors.append("Sell system is required.")

    commodity = None
    resolution = edta_commodities.resolve(params.commodity)
    if resolution.ok:
        commodity = resolution.commodity
        if commodity.rare:
            warnings.append(
                "{} is a rare commodity - supply is allocation-limited and "
                "usually far below a full hold.".format(commodity.name))
    else:
        message = resolution.error
        if resolution.suggestions:
            message += " Did you mean: {}?".format(", ".join(resolution.suggestions))
        errors.append(message)

    capacity = _positive_number(params.cargo_capacity, "Cargo capacity", errors, integer=True)
    unladen = _positive_number(params.unladen_range, "Unladen jump range", errors)
    laden = _positive_number(params.laden_range, "Laden jump range", errors)

    pad = core.pad_to_int(params.min_pad)
    if pad is None:
        errors.append("Minimum pad must be S, M or L.")

    if unladen and laden and laden > unladen:
        warnings.append("Laden range is greater than unladen - check the two fields.")

    if errors:
        return Validation(False, errors=errors, warnings=warnings)

    spec = core.SearchSpec(
        commodity_ardent=commodity.ardent,
        commodity_display=commodity.name,
        sell_system=sell_system,
        cargo_capacity=int(capacity),
        unladen_range=float(unladen),
        laden_range=float(laden),
        min_pad=int(pad),
        supply_margin=float(params.supply_margin),
        jumps_per_leg=int(params.jumps_per_leg),
        max_age_days=int(params.max_age_days),
        freshness_edges_h=tuple(params.freshness_edges_h),
        ls_edges=tuple(params.ls_edges),
    )
    return Validation(True, spec=spec, warnings=warnings)


# --- the search escalation ---------------------------------------------------

@dataclass
class Attempt:
    radius: float
    max_days: int
    rows: int
    kept: int


@dataclass
class Outcome:
    """What a worker-thread search came back with."""

    candidate: Optional[core.Candidate] = None
    alternatives: List[core.Candidate] = field(default_factory=list)
    attempts: List[Attempt] = field(default_factory=list)
    relaxed: List[str] = field(default_factory=list)
    error_kind: str = ""
    error_message: str = ""
    window_capped: bool = False
    #: How far out the search actually had to look.
    searched_ly: float = 0.0

    @property
    def ok(self) -> bool:
        return self.candidate is not None


#: How many rings the search may fetch. Each is one upstream request.
MAX_RINGS = 5


def escalation(spec: core.SearchSpec) -> List[Tuple[float, int]]:
    """(radius, max_days) steps, NEAREST FIRST, plus a final stale-data attempt.

    See core.rings() for why the search starts small: a single wide request runs
    into Ardent's unsorted 1000-row cap and can lose the nearest markets
    entirely. Accepting older data is the last resort, after distance.
    """
    steps = [(radius, spec.max_age_days) for radius in core.rings(spec, MAX_RINGS)]
    stale_days = max(spec.max_age_days, 30)
    if stale_days > spec.max_age_days:
        steps.append((steps[-1][0], stale_days))
    return steps


def _row_key(row: Dict[str, Any]) -> Any:
    return row.get("marketId") or (row.get("systemName"), row.get("stationName"))


def search_best(spec: core.SearchSpec, fetch: Callable[[float, int], Any],
                now=None) -> Outcome:
    """Search outward until something recent qualifies.

    Rows ACCUMULATE across rings, deduplicated by market. A wide ring may come
    back truncated by the upstream row cap, but the near rings before it were
    complete, so the nearest markets are always in the pool being ranked.

    Expansion stops as soon as the best candidate so far is in the freshest age
    band. A merely acceptable near option does not stop the search, because age
    outranks distance - but a good one does, and that is the common case.

    Safe to call from a worker thread: it touches no session state.
    """
    outcome = Outcome()
    steps = escalation(spec)
    pool: Dict[Any, Dict[str, Any]] = {}
    first_days = steps[0][1]

    for radius, max_days in steps:
        result = fetch(radius, max_days)
        if result is None:
            outcome.error_kind = "network"
            outcome.error_message = "No response from Ardent."
            return outcome
        if not getattr(result, "ok", False):
            outcome.error_kind = result.kind
            outcome.error_message = result.message
            return outcome

        for row in result.rows or ():
            pool[_row_key(row)] = row
        ranked = core.rank_candidates(list(pool.values()), spec, now=now)
        outcome.attempts.append(Attempt(radius, max_days, len(result.rows), len(ranked)))
        outcome.window_capped = result.window_capped
        outcome.searched_ly = radius

        if ranked:
            outcome.candidate = ranked[0]
            outcome.alternatives = ranked[1:6]
            if max_days > first_days:
                outcome.relaxed.append("accepted data up to {} days old".format(max_days))
            if _is_freshest(ranked[0], spec):
                break
        if result.window_capped:
            # Beyond here the upstream window is truncated and unsorted, so a
            # wider ring cannot be trusted to add anything better.
            break

    return outcome


def _is_freshest(candidate: core.Candidate, spec: core.SearchSpec) -> bool:
    age_hours = (None if candidate.age_seconds is None
                 else candidate.age_seconds / 3600.0)
    return core.bucket_of(age_hours, spec.freshness_edges_h) == 0


# --- the session -------------------------------------------------------------

@dataclass
class Session:
    """Where we are in the haul loop. Mutated only on the Tk thread."""

    state: str = OFF
    #: Whether a run is in progress. Deliberately NOT `state != OFF`: a rejected
    #: Start leaves an ERROR message on screen while the run is still stopped,
    #: and the Start/Stop button has to read "Start" in that situation.
    active: bool = False
    spec: Optional[core.SearchSpec] = None
    params: Params = field(default_factory=Params)
    target: Optional[core.Candidate] = None
    outcome: Optional[Outcome] = None
    message: str = ""
    generation: int = 0
    current_system: str = ""
    docked_at: str = ""
    cargo: int = 0
    sell_price: Optional[int] = None
    note: str = ""

    # --- lifecycle ----------------------------------------------------------

    def start(self, params: Params) -> Tuple[Validation, List[Effect]]:
        result = validate(params)
        if not result.ok:
            self.state = ERROR
            self.active = False
            self.message = " ".join(result.errors)
            return result, [Effect(REDRAW)]
        self.active = True
        self.params = params
        self.spec = result.spec
        self.target = None
        self.outcome = None
        self.note = ""
        self.sell_price = None
        return result, self.begin_search()

    def stop(self) -> List[Effect]:
        self.state = OFF
        self.active = False
        self.target = None
        self.outcome = None
        self.message = ""
        self.note = ""
        self.generation += 1          # abandon any search in flight
        return [Effect(REDRAW)]

    @property
    def running(self) -> bool:
        return self.active

    # --- searching ----------------------------------------------------------

    def begin_search(self) -> List[Effect]:
        if self.spec is None:
            return []
        self.generation += 1
        self.state = SEARCHING
        self.message = "Searching within {:.0f} ly of {}...".format(
            self.spec.radius_ly, self.spec.sell_system)
        return [Effect(REDRAW), Effect(SEARCH, (self.generation, self.spec))]

    def apply_outcome(self, generation: int, outcome: Outcome) -> List[Effect]:
        """Called on the Tk thread with a worker's result. A stale generation is
        a search the user has already superseded - drop it silently."""
        if generation != self.generation or not self.active:
            return []
        self.outcome = outcome
        if outcome.error_kind:
            self.state = ERROR
            self.message = outcome.error_message
            return [Effect(REDRAW)]
        if not outcome.ok:
            self.state = NO_RESULTS
            self.message = self._no_results_message(outcome)
            return [Effect(REDRAW)]

        self.target = outcome.candidate
        self.state = TO_BUY
        self.message = ""
        return [Effect(CLIPBOARD, outcome.candidate.system), Effect(REDRAW)]

    def _no_results_message(self, outcome: Outcome) -> str:
        if self.spec is None:
            return "Nothing found."
        widest = max((a.radius for a in outcome.attempts), default=self.spec.radius_ly)
        widest = max(widest, outcome.searched_ly)
        return ("No market within {:.0f} ly of {} has {:,} t of {} on a {} pad. "
                "Try a smaller hold, a smaller pad, or a different sell system.".format(
                    widest, self.spec.sell_system, self.spec.min_volume,
                    self.spec.commodity_display, core.pad_to_str(self.spec.min_pad)))

    # --- journal ------------------------------------------------------------

    def on_journal(self, entry: Dict[str, Any], state: Dict[str, Any]) -> List[Effect]:
        """One journal event. Returns the effects load.py should perform."""
        event = (entry or {}).get("event")
        if not event:
            return []

        if isinstance(state, dict):
            capacity = state.get("CargoCapacity")
            cargo = state.get("Cargo")
            if isinstance(cargo, dict):
                self.cargo = sum(int(v or 0) for v in cargo.values())
            if capacity and self.spec is None:
                self.params.cargo_capacity = str(capacity)

        if event in ("FSDJump", "Location", "CarrierJump"):
            self.current_system = entry.get("StarSystem") or self.current_system
            self._maybe_capture_sell_coords(entry)

        if not self.running:
            return [Effect(REDRAW)]

        if event == "Docked" or (event in ("Location", "CarrierJump") and entry.get("Docked")):
            return self._on_docked(entry)

        if event in ("Undocked", "FSDJump", "SupercruiseEntry", "Cargo", "Loadout"):
            if event == "Undocked":
                self.docked_at = ""
            return [Effect(REDRAW)]

        return []

    def _maybe_capture_sell_coords(self, entry: Dict[str, Any]) -> None:
        """While standing in the sell system the journal hands us its exact
        coordinates, which beats Ardent's whole-light-year distance."""
        if self.spec is None or self.spec.sell_coords is not None:
            return
        system = entry.get("StarSystem")
        star_pos = entry.get("StarPos")
        if not system or not star_pos or len(star_pos) != 3:
            return
        if system.casefold() != self.spec.sell_system.casefold():
            return
        self.spec = dataclasses.replace(
            self.spec, sell_coords=(float(star_pos[0]), float(star_pos[1]), float(star_pos[2])))

    def _on_docked(self, entry: Dict[str, Any]) -> List[Effect]:
        system = (entry.get("StarSystem") or "").strip()
        station = (entry.get("StationName") or "").strip()
        market_id = entry.get("MarketID")
        self.current_system = system or self.current_system
        self.docked_at = station

        if self.spec is None:
            return [Effect(REDRAW)]

        # Arrived at the sell system: this leg is done, plan the next one.
        if system.casefold() == self.spec.sell_system.casefold():
            self.note = ""
            self.target = None
            self.sell_price = None
            return self.begin_search()

        # Arrived at the buy location: turn around.
        if self.state == TO_BUY and self.target is not None:
            same_market = (market_id is not None and self.target.market_id is not None
                           and int(market_id) == int(self.target.market_id))
            same_system = system.casefold() == self.target.system.casefold()
            if same_market or same_system:
                self.note = "" if same_market else (
                    "Docked at {} rather than {}.".format(station, self.target.station))
                self.state = TO_SELL
                return [Effect(CLIPBOARD, self.spec.sell_system), Effect(REDRAW)]

        return [Effect(REDRAW)]

    # --- presentation -------------------------------------------------------

    def clipboard_text(self) -> str:
        """What a manual 'copy again' should put on the clipboard."""
        if self.state == TO_BUY and self.target is not None:
            return self.target.system
        if self.state == TO_SELL and self.spec is not None:
            return self.spec.sell_system
        return ""

    def distance_home(self) -> float:
        return self.target.distance if self.target is not None else 0.0

    def display_lines(self) -> Tuple[List[str], str]:
        """(lines, colour-role) for the overlay and the EDMC panel. Both render
        the same content - the panel is the fallback when no overlay exists."""
        if self.state == OFF:
            return ([], "off")
        if self.spec is None:
            return (["Trade Assist: parameters incomplete"], "warn")
        if self.state == SEARCHING:
            return (["Trade Assist: {}".format(self.message)], "buy")
        if self.state in (NO_RESULTS, ERROR):
            return (["Trade Assist: {}".format(self.message)], "warn")

        if self.state == TO_BUY and self.target is not None:
            lines = core.describe_candidate(self.target, self.spec)
            lines.extend(core.profit_lines(self.target.buy_price, self.sell_price,
                                           self.spec.cargo_capacity))
            lines.append("Copied \"{}\" - paste into the galaxy map".format(self.target.system))
            caveats = list(self.outcome.relaxed) if self.outcome else []
            if self.outcome and self.outcome.window_capped:
                # The near rings were complete; only the outermost one hit
                # Ardent's unsorted row cap, so distant options may be missing.
                caveats.append("options beyond {:.0f} ly may be incomplete".format(
                    self.outcome.searched_ly))
            if caveats:
                lines.append("(" + "; ".join(caveats) + ")")
            return (lines, "buy")

        if self.state == TO_SELL:
            lines = core.describe_return(self.spec, self.distance_home(), self.cargo)
            lines.extend(core.profit_lines(
                self.target.buy_price if self.target else 0,
                self.sell_price, self.spec.cargo_capacity))
            lines.append("Copied \"{}\" - paste into the galaxy map".format(self.spec.sell_system))
            if self.note:
                lines.append(self.note)
            return (lines, "sell")

        return ([], "off")

    def status_line(self) -> str:
        labels = {
            OFF: "Stopped",
            SEARCHING: "Searching...",
            TO_BUY: "Fly to buy location",
            TO_SELL: "Fly to sell system",
            NO_RESULTS: "No market qualified",
            ERROR: "Error",
        }
        return labels.get(self.state, self.state)


def _self_test() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)

    def iso(when):
        return when.isoformat().replace("+00:00", "Z")

    def row(**kw):
        base = {
            "commodityName": "gold", "systemName": "Veren's Stop",
            "stationName": "Duncan's Den", "stationType": "Orbis",
            "maxLandingPadSize": 3, "stock": 500000, "buyPrice": 45595,
            "distance": 33, "distanceToArrival": 10, "marketId": 123456,
            "updatedAt": iso(now - timedelta(minutes=6)),
        }
        base.update(kw)
        # Distinct markets need distinct ids, or the search pool dedupes them.
        if "stationName" in kw and "marketId" not in kw:
            base["marketId"] = abs(hash(kw["stationName"])) % 10_000_000
        return base

    good = Params(sell_system="Sol", commodity="Gold", cargo_capacity="720",
                  unladen_range="50", laden_range="40", min_pad="L")

    # Validation rejects, and explains.
    bad = validate(Params(sell_system="", commodity="Golde", cargo_capacity="x",
                          unladen_range="0", laden_range="", min_pad="Q"))
    assert not bad.ok and len(bad.errors) >= 5, bad.errors
    assert any("Gold" in e for e in bad.errors), bad.errors

    checked = validate(good)
    assert checked.ok and checked.spec.commodity_ardent == "gold"

    # The search starts NEAR and works outward, accepting stale data only last.
    spec = checked.spec
    steps = escalation(spec)
    radii = [step[0] for step in steps]
    assert radii == sorted(radii), radii
    assert steps[0] == (spec.first_ring_ly, 7), steps[0]
    assert steps[-1] == (spec.radius_ly, 30), steps[-1]
    assert len(steps) <= MAX_RINGS + 1

    class FakeResult:
        def __init__(self, rows, kind="ok", message="", capped=False):
            self.rows, self.kind, self.message = rows, kind, message
            self.window_capped = capped
            self.ok = kind == "ok"

    # A fresh hit in the first ring stops the search there - one request.
    calls = []

    def near_hit(radius, days):
        calls.append(radius)
        return FakeResult([row()])

    hit = search_best(spec, near_hit, now=now)
    assert hit.ok and hit.candidate.station == "Duncan's Den" and hit.relaxed == []
    assert calls == [spec.first_ring_ly], calls

    # Nothing anywhere -> an outcome that is not an error, just empty.
    empty = search_best(spec, lambda r, d: FakeResult([]), now=now)
    assert not empty.ok and not empty.error_kind and len(empty.attempts) == len(steps)

    # A near ring holding only STALE stock does not stop the search: age
    # outranks distance, so a fresh market further out has to be considered.
    stale_row = row(stationName="StaleNear", distance=5,
                    updatedAt=iso(now - timedelta(days=2)))
    fresh_row = row(stationName="FreshFar", distance=300)
    seen = []

    def stale_then_fresh(radius, days):
        seen.append(radius)
        return FakeResult([stale_row] if len(seen) == 1 else [stale_row, fresh_row])

    widened = search_best(spec, stale_then_fresh, now=now)
    assert len(seen) > 1, seen
    assert widened.candidate.station == "FreshFar", widened.candidate.station
    assert widened.searched_ly == seen[-1]

    # Rows accumulate across rings, so a truncated wide window cannot lose a
    # market that a complete near ring already found. This is the v0.2.0 bug.
    near_only = row(stationName="NearComplete", distance=30,
                    updatedAt=iso(now - timedelta(days=2)), marketId=1)
    far_only = row(stationName="FarTruncated", distance=200,
                   updatedAt=iso(now - timedelta(days=3)), marketId=2)
    # The wide ring does NOT contain the near market - exactly how Ardent behaves
    # once its row cap bites.
    merged = search_best(spec, lambda r, d: FakeResult(
        [near_only] if r == spec.first_ring_ly else [far_only],
        capped=r != spec.first_ring_ly), now=now)
    stations = [merged.candidate.station] + [c.station for c in merged.alternatives]
    assert "NearComplete" in stations, stations
    assert merged.candidate.station == "NearComplete", merged.candidate.station
    assert merged.window_capped is True

    # Upstream failure surfaces as an error, not as "nothing found".
    failed = search_best(spec, lambda r, d: FakeResult([], "not_found", "no such system"),
                         now=now)
    assert not failed.ok and failed.error_kind == "not_found"

    # --- a full cycle -------------------------------------------------------
    session = Session()
    validation, effects = session.start(good)
    assert validation.ok and session.state == SEARCHING
    assert [e.kind for e in effects] == [REDRAW, SEARCH]
    generation = effects[1].payload[0]

    effects = session.apply_outcome(generation, hit)
    assert session.state == TO_BUY
    assert effects[0].kind == CLIPBOARD and effects[0].payload == "Veren's Stop"

    # A stale worker result from a superseded search is ignored.
    assert session.apply_outcome(generation - 1, empty) == []
    assert session.state == TO_BUY

    # Docking at the planned buy station turns us around.
    effects = session.on_journal(
        {"event": "Docked", "StarSystem": "Veren's Stop",
         "StationName": "Duncan's Den", "MarketID": 123456}, {})
    assert session.state == TO_SELL
    assert effects[0].kind == CLIPBOARD and effects[0].payload == "Sol"
    assert session.note == ""

    # Docking back in the sell system starts the next search.
    effects = session.on_journal(
        {"event": "Docked", "StarSystem": "Sol", "StationName": "Abraham Lincoln",
         "MarketID": 128016384}, {"CargoCapacity": 720})
    assert session.state == SEARCHING and effects[-1].kind == SEARCH

    # A different station in the buy system still advances, and says so.
    session.apply_outcome(effects[-1].payload[0], hit)
    session.on_journal({"event": "Docked", "StarSystem": "Veren's Stop",
                        "StationName": "Somewhere Else", "MarketID": 999}, {})
    assert session.state == TO_SELL and "rather than" in session.note

    # The sell system's own coordinates are picked up from the journal.
    session.spec = dataclasses.replace(session.spec, sell_coords=None)
    session.on_journal({"event": "FSDJump", "StarSystem": "Sol",
                        "StarPos": [0.0, 0.0, 0.0]}, {})
    assert session.spec.sell_coords == (0.0, 0.0, 0.0)

    lines, role = session.display_lines()
    assert role == "sell" and any("paste into the galaxy map" in line for line in lines)

    assert session.clipboard_text() == "Sol"
    session.stop()
    assert session.state == OFF and session.display_lines() == ([], "off")

    print("edta_session self-test: OK")


if __name__ == "__main__":
    _self_test()
