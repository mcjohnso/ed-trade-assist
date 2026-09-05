"""Pure-logic tests for EDTradeAssist. No EDMC, no tkinter, no network.

Run with:   python tests/test_core.py

The fixture in tests/fixtures/ is a real Ardent /nearby/exports response (Gold
near Sol, captured 2026-09-05), trimmed to twelve rows. Times are anchored to the
newest row in the fixture rather than to the wall clock, so these tests do not
rot as the capture ages.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "EDTradeAssist"))

import edta_ardent                      # noqa: E402
import edta_commodities                 # noqa: E402
import edta_core as core                # noqa: E402
import edta_overlay                     # noqa: E402
import edta_session as session_mod      # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ardent_gold_near_sol.json"


def load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def fixture_now(rows):
    """A moment just after the freshest row, so ages are deterministic."""
    stamps = [core.parse_updated_at(row.get("updatedAt")) for row in rows]
    stamps = [stamp for stamp in stamps if stamp]
    return max(stamps) + timedelta(minutes=1)


def spec(**overrides):
    base = dict(commodity_ardent="gold", commodity_display="Gold", sell_system="Sol",
                cargo_capacity=720, unladen_range=50.0, laden_range=40.0, min_pad=3)
    base.update(overrides)
    return core.SearchSpec(**base)


class SelfTests(unittest.TestCase):
    """Each module carries its own self-test; run them here too so one command
    covers everything."""

    def test_module_self_tests(self):
        for module in (core, edta_commodities, edta_ardent, edta_overlay, session_mod):
            with self.subTest(module=module.__name__):
                module._self_test()


class FrozenRuntimeImports(unittest.TestCase):
    """EDMC ships a FROZEN Python. Its library.zip carries only the stdlib
    modules EDMC itself uses - `difflib`, for one, is absent - and a plugin that
    imports a missing module fails to load outright, with the error visible only
    in EDMC's debug log. "Standard library only" is necessary but NOT sufficient.

    This walks every import in the plugin and checks it against what the frozen
    runtime really provides. Adding an import that is not on the list is a
    deliberate decision that has to be verified against a real EDMC install.
    """

    #: Verified present in EDMC 6.x's library.zip, or a C builtin compiled into
    #: python313.dll (math, time, itertools and friends are not .pyc entries).
    STDLIB_IN_EDMC = {
        "__future__", "abc", "argparse", "collections", "copy", "csv", "dataclasses",
        "datetime", "enum", "functools", "io", "itertools", "json", "logging", "math",
        "os", "queue", "random", "re", "statistics", "sys", "threading", "time",
        "tkinter", "types", "typing", "urllib", "zipfile",
    }
    #: Provided by EDMC at runtime, or by the user's overlay plugin.
    EDMC_PROVIDED = {
        "config", "myNotebook", "theme", "ttkHyperlinkLabel", "edmc_data", "monitor",
        "companion", "plug", "edmcoverlay",
    }
    #: Packaged with EDMC.
    BUNDLED_THIRD_PARTY = {"requests", "urllib3"}

    def plugin_sources(self):
        return sorted((ROOT / "EDTradeAssist").glob("*.py"))

    def imported_modules(self, path):
        import ast
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
        return found

    def test_every_import_exists_in_the_frozen_runtime(self):
        allowed = (self.STDLIB_IN_EDMC | self.EDMC_PROVIDED | self.BUNDLED_THIRD_PARTY)
        for path in self.plugin_sources():
            for module in sorted(self.imported_modules(path)):
                if module.startswith("edta_"):
                    continue                      # our own, shipped alongside
                with self.subTest(file=path.name, module=module):
                    self.assertIn(module, allowed,
                                  "{} imports {!r}, which is not known to exist in "
                                  "EDMC's frozen Python. Verify against a real "
                                  "install before allowing it.".format(path.name, module))

    def test_difflib_stays_out(self):
        # The specific module that broke the first release.
        for path in self.plugin_sources():
            self.assertNotIn("difflib", self.imported_modules(path), path.name)

    def test_against_a_real_edmc_install_when_one_is_present(self):
        """Opportunistic: if EDMC is installed here, check the pure-Python
        imports against its actual library.zip rather than against the list."""
        import zipfile
        candidates = [Path(r"C:/Program Files (x86)/EDMarketConnector/library.zip"),
                      Path(r"C:/Program Files/EDMarketConnector/library.zip")]
        library = next((p for p in candidates if p.exists()), None)
        if library is None:
            self.skipTest("no local EDMC install to check against")

        with zipfile.ZipFile(library) as archive:
            names = set(archive.namelist())

        def present(module):
            return ("{}.pyc".format(module) in names
                    or "{}/__init__.pyc".format(module) in names)

        # C builtins live in python313.dll, not the zip, so they are exempt.
        builtins_in_dll = {"math", "time", "itertools", "sys", "abc", "io", "types"}
        for path in self.plugin_sources():
            for module in sorted(self.imported_modules(path)):
                if (module.startswith("edta_") or module in self.EDMC_PROVIDED
                        or module in builtins_in_dll):
                    continue
                with self.subTest(file=path.name, module=module):
                    self.assertTrue(present(module),
                                    "{} imports {!r}, absent from {}".format(
                                        path.name, module, library))


class UpstreamContract(unittest.TestCase):
    """The direction inversion is the single easiest thing here to get backwards,
    so it is pinned rather than left to a comment."""

    def test_player_buying_maps_to_exports(self):
        self.assertEqual(edta_ardent.ENDPOINT, "exports")
        self.assertEqual(edta_ardent.PRICE_FIELD, "buyPrice")
        self.assertEqual(edta_ardent.VOLUME_FIELD, "stock")

    def test_radius_never_exceeds_the_upstream_clamp(self):
        # Ardent silently clamps at 500 ly; asking for more must be refused, not
        # relayed as a quietly narrower answer.
        self.assertEqual(spec(laden_range=500.0, jumps_per_leg=60).radius_ly,
                         core.MAX_RADIUS_LY)
        client = edta_ardent.ArdentClient(user_agent="test")
        result = client.nearby_exports("Sol", "gold", 501, 100)
        self.assertEqual(result.kind, edta_ardent.BAD_REQUEST)

    def test_carriers_are_never_requested(self):
        captured = {}

        class Recorder(edta_ardent.ArdentClient):
            def _request(self, path, params):
                captured.update(params)
                captured["path"] = path
                return edta_ardent.SearchResult(edta_ardent.OK, rows=[])

        Recorder(user_agent="test").nearby_exports("Sol", "gold", 50, 100, max_days_ago=7)
        self.assertEqual(captured["fleetCarriers"], 0)
        self.assertTrue(captured["path"].endswith("/nearby/exports"))


class Buckets(unittest.TestCase):
    def test_missing_values_sort_last(self):
        self.assertEqual(core.bucket_of(None, (1, 2, 3)), 3)

    def test_edges_are_exclusive_upper_bounds(self):
        self.assertEqual(core.bucket_of(0.99, (1, 6)), 0)
        self.assertEqual(core.bucket_of(1.0, (1, 6)), 1)
        self.assertEqual(core.bucket_of(99.0, (1, 6)), 2)

    def test_the_first_age_band_is_an_hour(self):
        # A commodity being actively hauled drains fast, so an hour-old reading
        # is genuinely less trustworthy than a ten-minute-old one. Inside the
        # band, jumps decide.
        edges = core.FRESHNESS_EDGES_H
        self.assertEqual(edges[0], 1.0)
        for minutes in (0.5, 15, 59):
            self.assertEqual(core.bucket_of(minutes / 60.0, edges), 0, minutes)
        self.assertEqual(core.bucket_of(1.5, edges), 1)


class JumpMaths(unittest.TestCase):
    def test_zero_distance_is_zero_jumps(self):
        self.assertEqual(core.estimate_jumps(0, 40), 0)

    def test_any_distance_costs_at_least_one_jump(self):
        self.assertEqual(core.estimate_jumps(0.1, 500), 1)

    def test_derating_is_applied(self):
        # 100 ly at 40 ly range is 2.5 raw, but 100 / (40 * 0.85) = 2.94 -> 3.
        self.assertEqual(core.estimate_jumps(100, 40), 3)

    def test_laden_leg_costs_more_than_the_empty_one(self):
        out, back = core.round_trip_jumps(200, 50.0, 30.0)
        self.assertLess(out, back)

    def test_a_zero_range_does_not_divide_by_zero(self):
        self.assertEqual(core.estimate_jumps(100, 0), 0)


class RankingOnRealRows(unittest.TestCase):
    def setUp(self):
        self.rows = load_fixture()
        self.now = fixture_now(self.rows)

    def test_fixture_has_the_shape_we_rely_on(self):
        row = self.rows[0]
        for field in ("commodityName", "stationName", "systemName", "stationType",
                      "maxLandingPadSize", "stock", "buyPrice", "distance",
                      "distanceToArrival", "updatedAt", "marketId"):
            self.assertIn(field, row)

    def test_pad_filter_excludes_smaller_pads(self):
        # maxLandingPadSize is an int: 1=S, 2=M, 3=L.
        large = core.rank_candidates(self.rows, spec(min_pad=3), now=self.now)
        medium = core.rank_candidates(self.rows, spec(min_pad=2), now=self.now)
        self.assertTrue(all(c.pad == 3 for c in large))
        self.assertGreater(len(medium), len(large))

    def test_supply_margin_is_enforced(self):
        strict = spec(cargo_capacity=720, supply_margin=1.5)
        for candidate in core.rank_candidates(self.rows, strict, now=self.now):
            self.assertGreaterEqual(candidate.stock, strict.min_volume)

    def test_carriers_are_dropped_even_if_upstream_sends_them(self):
        carrier = dict(self.rows[0])
        carrier.update(stationType="FleetCarrier", stationName="X Relay",
                       maxLandingPadSize=3, stock=999999)
        ranked = core.rank_candidates(self.rows + [carrier], spec(), now=self.now)
        self.assertNotIn("X Relay", [c.station for c in ranked])

    def test_stale_rows_are_excluded(self):
        much_later = self.now + timedelta(days=400)
        self.assertEqual(core.rank_candidates(self.rows, spec(), now=much_later), [])

    def test_ranking_is_deterministic(self):
        first = [c.station for c in core.rank_candidates(self.rows, spec(min_pad=1),
                                                         now=self.now)]
        second = [c.station for c in core.rank_candidates(list(reversed(self.rows)),
                                                          spec(min_pad=1), now=self.now)]
        self.assertEqual(first, second)


class RankingPriorityOrder(unittest.TestCase):
    """Each tier must actually decide something, or the ones below it are dead."""

    def setUp(self):
        self.now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.spec = spec()

    def row(self, name, **kw):
        base = {
            "commodityName": "gold", "systemName": "Sys " + name, "stationName": name,
            "stationType": "Coriolis", "maxLandingPadSize": 3, "stock": 9000,
            "buyPrice": 45000, "distance": 20, "distanceToArrival": 200,
            "updatedAt": self.iso(minutes=10),
        }
        base.update(kw)
        return base

    def iso(self, **delta):
        return (self.now - timedelta(**delta)).isoformat().replace("+00:00", "Z")

    def test_tier3_uses_bands_not_raw_timestamps(self):
        # Two readings minutes apart are equally trustworthy, so the tiers below
        # decide - here, the cheaper one.
        self.assertEqual(
            self.order(self.row("JustNow", updatedAt=self.iso(seconds=30), buyPrice=50000),
                       self.row("Recent", updatedAt=self.iso(minutes=15), buyPrice=40000)),
            ["Recent", "JustNow"])

    def order(self, *rows):
        return [c.station for c in core.rank_candidates(rows, self.spec, now=self.now)]

    def test_tier3_freshness_outranks_everything_below(self):
        self.assertEqual(
            self.order(
                self.row("Stale", updatedAt=self.iso(hours=30), distance=1,
                         distanceToArrival=5, buyPrice=1),
                self.row("Fresh", distance=300, distanceToArrival=9000, buyPrice=99000)),
            ["Fresh", "Stale"])

    def test_tier4_jumps_outrank_arrival_and_price(self):
        self.assertEqual(
            self.order(
                self.row("Far", distance=300, distanceToArrival=5, buyPrice=1),
                self.row("Near", distance=20, distanceToArrival=9000, buyPrice=99000)),
            ["Near", "Far"])

    def test_tier5_arrival_outranks_pad_and_price(self):
        self.assertEqual(
            self.order(
                self.row("Deep", distanceToArrival=4000, buyPrice=1),
                self.row("Shallow", distanceToArrival=50, buyPrice=99000)),
            ["Shallow", "Deep"])

    def test_tier6_a_bigger_pad_wins(self):
        # Asking for M does not mean preferring M: an L pad is strictly easier.
        ranked = core.rank_candidates(
            [self.row("Medium", maxLandingPadSize=2, buyPrice=40000),
             self.row("Large", maxLandingPadSize=3, buyPrice=45000)],
            spec(min_pad=2), now=self.now)
        self.assertEqual([c.station for c in ranked], ["Large", "Medium"])

    def test_tier7_price_is_reachable(self):
        self.assertEqual(
            self.order(self.row("Dear", buyPrice=60000), self.row("Cheap", buyPrice=40000)),
            ["Cheap", "Dear"])

    def test_supply_is_a_filter_never_a_ranking(self):
        # 400x the stock is worth nothing once both can fill the hold.
        self.assertEqual(
            self.order(self.row("Huge", stock=400000, buyPrice=50000),
                       self.row("Enough", stock=1100, buyPrice=40000)),
            ["Enough", "Huge"])

    def test_raw_distance_does_not_rank(self):
        # Same jump count -> equally far. The cheaper one wins even at 30 ly
        # against 21 ly.
        self.assertEqual(
            self.order(self.row("Nearer", distance=21, buyPrice=50000),
                       self.row("Further", distance=30, buyPrice=40000)),
            ["Further", "Nearer"])

    def test_fewer_jumps_beats_slightly_fresher(self):
        """30s at 65 ly loses to 15min at 20 ly, because the jumps differ."""
        ranked = core.rank_candidates(
            [self.row("Far65", distance=65, distanceToArrival=100, buyPrice=40000,
                      updatedAt=self.iso(seconds=30)),
             self.row("Near20", distance=20, distanceToArrival=400, buyPrice=45000,
                      updatedAt=self.iso(minutes=15))],
            spec(unladen_range=50.0, laden_range=40.0), now=self.now)
        self.assertEqual([c.station for c in ranked], ["Near20", "Far65"])

    def test_a_genuinely_stale_reading_still_loses(self):
        # The point of the age bands is trust in the supply figure, so a two-day
        # -old reading does not get to win on being close.
        self.assertEqual(
            self.order(
                self.row("StaleNear", distance=5, updatedAt=self.iso(days=2)),
                self.row("FreshFar", distance=200, updatedAt=self.iso(minutes=20))),
            ["FreshFar", "StaleNear"])

    def test_same_arrival_band_lets_price_decide(self):
        # 50 Ls and 90 Ls are both in the first band, so price breaks the tie
        # even though one is nominally closer.
        self.assertEqual(
            self.order(self.row("NearerDearer", distanceToArrival=50, buyPrice=60000),
                       self.row("FartherCheaper", distanceToArrival=90, buyPrice=40000)),
            ["FartherCheaper", "NearerDearer"])


class CommodityNames(unittest.TestCase):
    def test_every_spelling_resolves_to_the_ardent_name(self):
        for spelling in ("Gold", "gold", "$gold_name;", " GOLD "):
            self.assertEqual(edta_commodities.resolve(spelling).commodity.ardent, "gold")

    def test_multiword_names_lose_their_spaces(self):
        self.assertEqual(edta_commodities.resolve("Consumer Technology").commodity.ardent,
                         "consumertechnology")

    def test_a_typo_is_an_error_with_suggestions(self):
        result = edta_commodities.resolve("Platnum")
        self.assertFalse(result.ok)
        self.assertIn("Platinum", result.suggestions)

    def test_unknown_names_never_resolve_silently(self):
        # A misspelled commodity does NOT 404 upstream - it returns 200 with an
        # empty list - so this local check is the only thing standing between a
        # typo and "no market sells this".
        self.assertFalse(edta_commodities.resolve("qwertyuiop").ok)


class Validation(unittest.TestCase):
    def good(self, **kw):
        base = dict(sell_system="Sol", commodity="Gold", cargo_capacity="720",
                    unladen_range="50", laden_range="40", min_pad="L")
        base.update(kw)
        return session_mod.Params(**base)

    def test_a_complete_form_validates(self):
        result = session_mod.validate(self.good())
        self.assertTrue(result.ok)
        self.assertEqual(result.spec.min_volume, 1080)

    def test_every_missing_field_is_reported_at_once(self):
        result = session_mod.validate(session_mod.Params())
        self.assertFalse(result.ok)
        self.assertGreaterEqual(len(result.errors), 5)

    def test_non_numeric_entries_are_rejected(self):
        self.assertFalse(session_mod.validate(self.good(cargo_capacity="lots")).ok)
        self.assertFalse(session_mod.validate(self.good(laden_range="-3")).ok)

    def test_rare_commodities_warn_but_are_allowed(self):
        result = session_mod.validate(self.good(commodity="Lavian Brandy"))
        self.assertTrue(result.ok)
        self.assertTrue(any("rare" in w for w in result.warnings))

    def test_laden_greater_than_unladen_warns(self):
        result = session_mod.validate(self.good(unladen_range="30", laden_range="40"))
        self.assertTrue(result.ok)
        self.assertTrue(any("Laden" in w for w in result.warnings))


class Escalation(unittest.TestCase):
    def test_the_search_starts_near_and_works_outward(self):
        target = spec()
        steps = session_mod.escalation(target)
        radii = [step[0] for step in steps]
        self.assertEqual(radii, sorted(radii))
        self.assertEqual(radii[0], target.first_ring_ly)
        self.assertLessEqual(radii[-1], target.radius_ly)
        self.assertEqual(steps[0][1], 7)
        self.assertEqual(steps[-1][1], 30)      # stale data only as a last resort

    def test_escalation_is_bounded(self):
        self.assertLessEqual(len(session_mod.escalation(spec(laden_range=8.0))),
                             session_mod.MAX_RINGS + 1)

    def test_the_first_ring_is_never_the_whole_radius(self):
        # The Ega/Palladium case: a 27 ly laden range and 10 jumps per leg gives
        # a 229 ly ceiling, far past the point where the upstream row cap bites.
        target = spec(laden_range=27.0, unladen_range=38.0)
        self.assertAlmostEqual(target.radius_ly, 229.5)
        self.assertLess(session_mod.escalation(target)[0][0], 30.0)

    def test_a_fresh_near_hit_stops_after_one_request(self):
        rows = load_fixture()
        now = fixture_now(rows)
        calls = []

        def fetch(radius, days):
            calls.append(radius)
            return edta_ardent.SearchResult(edta_ardent.OK, rows=rows)

        outcome = session_mod.search_best(spec(min_pad=1), fetch, now=now)
        self.assertTrue(outcome.ok)
        self.assertEqual(len(calls), 1, calls)

    def test_a_truncated_wide_window_cannot_lose_a_near_market(self):
        """The v0.2.0 bug, in miniature.

        Ardent's /nearby stops at 1000 rows with no sort, so a wide request
        returns an arbitrary subset that routinely omits the nearest markets.
        Observed live for Ega/Palladium: at 229 ly the closest row returned was
        40 ly, while the same search at 92 ly came back complete and held a
        qualifying market at 30 ly. Rings must accumulate, not replace.
        """
        rows = load_fixture()
        now = fixture_now(rows)
        near = dict(rows[0], stationName="NearComplete", systemName="Near",
                    marketId=111, distance=5, stock=999999, maxLandingPadSize=3,
                    distanceToArrival=50)
        far = dict(rows[0], stationName="FarTruncated", systemName="Far",
                   marketId=222, distance=200, stock=999999, maxLandingPadSize=3,
                   distanceToArrival=50)
        target = spec(min_pad=1)

        def fetch(radius, days):
            # The near market appears ONLY in the first, complete ring.
            if radius == target.first_ring_ly:
                return edta_ardent.SearchResult(edta_ardent.OK, rows=[near])
            return edta_ardent.SearchResult(edta_ardent.OK, rows=[far])

        outcome = session_mod.search_best(target, fetch, now=now)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.candidate.station, "NearComplete")

    def test_expansion_stops_at_a_capped_window(self):
        rows = load_fixture()
        now = fixture_now(rows)
        calls = []

        def fetch(radius, days):
            calls.append(radius)
            return edta_ardent.SearchResult(
                edta_ardent.OK, rows=[dict(rows[0], stock=999999,
                                           updatedAt="2020-01-01T00:00:00.000Z")],
                window_capped=True)

        session_mod.search_best(spec(min_pad=1), fetch, now=now)
        self.assertEqual(len(calls), 1, calls)

    def test_a_stale_only_result_keeps_looking(self):
        rows = load_fixture()
        now = fixture_now(rows)
        stale = dict(rows[0], marketId=1, stock=999999, maxLandingPadSize=3,
                     updatedAt=(now - timedelta(days=2)).isoformat().replace("+00:00", "Z"))
        calls = []

        def fetch(radius, days):
            calls.append(radius)
            return edta_ardent.SearchResult(edta_ardent.OK, rows=[stale])

        outcome = session_mod.search_best(spec(min_pad=1), fetch, now=now)
        self.assertTrue(outcome.ok)
        self.assertGreater(len(calls), 1, "a stale near hit must not end the search")

    def test_a_relaxed_result_says_so(self):
        rows = load_fixture()
        now = fixture_now(rows)
        calls = []

        def fetch(radius, days):
            calls.append((radius, days))
            hit = days > 7
            return edta_ardent.SearchResult(edta_ardent.OK, rows=rows if hit else [])

        outcome = session_mod.search_best(spec(min_pad=1), fetch, now=now)
        self.assertTrue(outcome.ok)
        self.assertTrue(any("days old" in note for note in outcome.relaxed), outcome.relaxed)

    def test_an_upstream_error_is_not_reported_as_nothing_found(self):
        def fetch(radius, days):
            return edta_ardent.SearchResult(edta_ardent.NOT_FOUND, message="no such system")

        outcome = session_mod.search_best(spec(), fetch)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, edta_ardent.NOT_FOUND)
        self.assertEqual(len(outcome.attempts), 0)


class FullCycle(unittest.TestCase):
    """Dock in the sell system, fly out, dock at the buy station, come back."""

    def setUp(self):
        self.rows = load_fixture()
        self.now = fixture_now(self.rows)
        self.session = session_mod.Session()
        params = session_mod.Params(sell_system="Sol", commodity="Gold",
                                    cargo_capacity="200", unladen_range="50",
                                    laden_range="40", min_pad="M")
        validation, self.effects = self.session.start(params)
        self.assertTrue(validation.ok)

    def run_search(self):
        generation, search_spec = self.effects[-1].payload
        outcome = session_mod.search_best(
            search_spec,
            lambda r, d: edta_ardent.SearchResult(edta_ardent.OK, rows=self.rows),
            now=self.now)
        return generation, outcome

    def test_search_result_puts_the_buy_system_on_the_clipboard(self):
        generation, outcome = self.run_search()
        effects = self.session.apply_outcome(generation, outcome)
        self.assertEqual(self.session.state, session_mod.TO_BUY)
        clipboard = [e for e in effects if e.kind == session_mod.CLIPBOARD]
        self.assertEqual(clipboard[0].payload, outcome.candidate.system)

    def test_docking_at_the_buy_station_flips_the_clipboard_to_the_sell_system(self):
        generation, outcome = self.run_search()
        self.session.apply_outcome(generation, outcome)
        target = self.session.target
        effects = self.session.on_journal(
            {"event": "Docked", "StarSystem": target.system,
             "StationName": target.station, "MarketID": target.market_id}, {})
        self.assertEqual(self.session.state, session_mod.TO_SELL)
        self.assertEqual([e.payload for e in effects if e.kind == session_mod.CLIPBOARD],
                         ["Sol"])

    def test_docking_back_in_the_sell_system_starts_the_next_search(self):
        generation, outcome = self.run_search()
        self.session.apply_outcome(generation, outcome)
        effects = self.session.on_journal(
            {"event": "Docked", "StarSystem": "Sol", "StationName": "Abraham Lincoln",
             "MarketID": 128016384}, {})
        self.assertEqual(self.session.state, session_mod.SEARCHING)
        self.assertEqual(effects[-1].kind, session_mod.SEARCH)

    def test_docking_elsewhere_changes_nothing(self):
        generation, outcome = self.run_search()
        self.session.apply_outcome(generation, outcome)
        effects = self.session.on_journal(
            {"event": "Docked", "StarSystem": "Somewhere Unrelated",
             "StationName": "Random Port", "MarketID": 1}, {})
        self.assertEqual(self.session.state, session_mod.TO_BUY)
        self.assertEqual([e.kind for e in effects], [session_mod.REDRAW])

    def test_a_superseded_search_result_is_discarded(self):
        generation, outcome = self.run_search()
        self.session.begin_search()                     # user hit Re-search
        self.assertEqual(self.session.apply_outcome(generation, outcome), [])
        self.assertEqual(self.session.state, session_mod.SEARCHING)

    def test_stopping_clears_the_run(self):
        generation, outcome = self.run_search()
        self.session.apply_outcome(generation, outcome)
        self.session.stop()
        self.assertEqual(self.session.state, session_mod.OFF)
        self.assertEqual(self.session.clipboard_text(), "")

    def test_the_sell_systems_own_coordinates_are_preferred(self):
        self.session.on_journal(
            {"event": "FSDJump", "StarSystem": "Sol", "StarPos": [0.0, 0.0, 0.0]}, {})
        self.assertEqual(self.session.spec.sell_coords, (0.0, 0.0, 0.0))
        self.effects = self.session.begin_search()      # re-plan with the coordinates
        generation, outcome = self.run_search()
        self.session.apply_outcome(generation, outcome)
        # Ardent reports whole light years; with coordinates we compute the real
        # distance from Sol's own position.
        target = self.session.target
        expected = core.distance_ly((0.0, 0.0, 0.0), target.coords)
        self.assertAlmostEqual(target.distance, expected, places=9)
        self.assertNotEqual(round(target.distance, 6), round(expected))

    def test_the_panel_and_overlay_get_the_same_lines(self):
        generation, outcome = self.run_search()
        self.session.apply_outcome(generation, outcome)
        lines, role = self.session.display_lines()
        self.assertEqual(role, "buy")
        self.assertTrue(any("paste into the galaxy map" in line for line in lines))
        self.assertTrue(any("est" in line for line in lines))       # jumps are estimates


class NoResults(unittest.TestCase):
    def test_the_message_explains_what_to_change(self):
        state = session_mod.Session()
        params = session_mod.Params(sell_system="Sol", commodity="Gold",
                                    cargo_capacity="720", unladen_range="50",
                                    laden_range="40", min_pad="L")
        _, effects = state.start(params)
        generation, search_spec = effects[-1].payload
        outcome = session_mod.search_best(
            search_spec, lambda r, d: edta_ardent.SearchResult(edta_ardent.OK, rows=[]))
        state.apply_outcome(generation, outcome)
        self.assertEqual(state.state, session_mod.NO_RESULTS)
        self.assertIn("1,080", state.message)
        self.assertIn("L pad", state.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
