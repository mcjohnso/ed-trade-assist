"""Pure-logic tests for EDTradeAssist. No EDMC, no tkinter, no network.

Run with:   python tests/test_core.py

The fixture in tests/fixtures/ is a real Ardent /nearby/exports response (Gold
near Sol, captured 2026-09-05), trimmed to twelve rows. Times are anchored to the
newest row in the fixture rather than to the wall clock, so these tests do not
rot as the capture ages.
"""

from __future__ import annotations

import dataclasses
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
import edta_spansh                      # noqa: E402

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
        for module in (core, edta_commodities, edta_ardent, edta_spansh, edta_overlay,
                       session_mod):
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

    def test_arrival_outranks_price(self):
        self.assertEqual(
            self.order(
                self.row("Deep", distanceToArrival=4000, buyPrice=1),
                self.row("Shallow", distanceToArrival=50, buyPrice=99000)),
            ["Shallow", "Deep"])

    def test_the_pad_is_a_filter_and_never_ranks(self):
        # Configured for M, an M pad is all that matters. A station that also
        # has an L pad has earned nothing by it, so the tier below decides -
        # here, the cheaper one.
        ranked = core.rank_candidates(
            [self.row("Large", maxLandingPadSize=3, buyPrice=45000),
             self.row("Medium", maxLandingPadSize=2, buyPrice=40000)],
            spec(min_pad=2), now=self.now)
        self.assertEqual([c.station for c in ranked], ["Medium", "Large"])
        keys = {core.rank_key(c, spec(min_pad=2))[:-1] for c in ranked}
        self.assertEqual(len(keys), 1, "pad size leaked into the ranking")

    def test_a_pad_that_is_too_small_is_still_excluded(self):
        ranked = core.rank_candidates(
            [self.row("Small", maxLandingPadSize=1, buyPrice=1),
             self.row("Medium", maxLandingPadSize=2)],
            spec(min_pad=2), now=self.now)
        self.assertEqual([c.station for c in ranked], ["Medium"])

    def test_tier7_price_is_reachable(self):
        self.assertEqual(
            self.order(self.row("Dear", buyPrice=60000), self.row("Cheap", buyPrice=40000)),
            ["Cheap", "Dear"])

    def test_orbital_outranks_arrival_distance_and_price(self):
        # A surface port costs a glide and an approach. With age and jumps
        # level, the orbital station wins - even from much deeper in the system
        # and against a far better price on the ground.
        self.assertEqual(
            self.order(self.row("GroundShallow", stationType="CraterPort",
                                distanceToArrival=50, buyPrice=1),
                       self.row("OrbitDeep", distanceToArrival=4000, buyPrice=99000)),
            ["OrbitDeep", "GroundShallow"])

    def test_orbital_does_not_outrank_jumps(self):
        self.assertEqual(
            self.order(self.row("OrbitFar", distance=300),
                       self.row("GroundNear", stationType="CraterPort", distance=20)),
            ["GroundNear", "OrbitFar"])

    def test_arrival_distance_still_decides_between_two_orbital_stations(self):
        # Moving it below orbital/surface must not leave it unreachable.
        self.assertEqual(
            self.order(self.row("Deep", distanceToArrival=4000, buyPrice=1),
                       self.row("Shallow", distanceToArrival=50, buyPrice=99000)),
            ["Shallow", "Deep"])

    def test_surface_is_recognised_in_both_upstreams_spellings(self):
        for station_type in ("CraterPort", "CraterOutpost", "OnFootSettlement",
                             "Planetary Port", "Planetary Outpost", "Surface Settlement"):
            with self.subTest(station_type=station_type):
                self.assertEqual(
                    self.order(self.row("Ground", stationType=station_type, buyPrice=1),
                               self.row("Orbit", buyPrice=99000)),
                    ["Orbit", "Ground"])

    def test_two_surface_ports_still_fall_through_to_price(self):
        # The tier must not swallow the ones below it.
        self.assertEqual(
            self.order(self.row("DearGround", stationType="CraterPort", buyPrice=60000),
                       self.row("CheapGround", stationType="CraterPort", buyPrice=40000)),
            ["CheapGround", "DearGround"])

    def test_surface_ports_are_ranked_lower_not_removed(self):
        self.assertEqual(self.order(self.row("OnlyGround", stationType="CraterPort")),
                         ["OnlyGround"])

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


class MultiCommodity(unittest.TestCase):
    """A run may list several commodities. A market is then judged on what it
    holds of all of them together, and the hold is filled in list order."""

    def setUp(self):
        self.now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.spec = spec(commodity_ardent="superconductors",
                         commodity_display="Superconductors",
                         also=(("semiconductors", "Semiconductors"),
                               ("computercomponents", "Computer Components")))

    def iso(self, **delta):
        return (self.now - timedelta(**delta)).isoformat().replace("+00:00", "Z")

    def row(self, commodity, stock, market=1, **kw):
        base = {
            "commodityName": commodity, "systemName": "Sys {}".format(market),
            "stationName": "Port {}".format(market), "marketId": market,
            "stationType": "Coriolis", "maxLandingPadSize": 3, "stock": stock,
            "buyPrice": 1000, "distance": 20, "distanceToArrival": 200,
            "updatedAt": self.iso(minutes=10),
        }
        base.update(kw)
        return base

    def rank(self, *rows):
        return core.rank_candidates(rows, self.spec, now=self.now)

    def test_the_spec_keeps_the_list_in_order(self):
        self.assertEqual([display for _, display in self.spec.commodities],
                         ["Superconductors", "Semiconductors", "Computer Components"])
        self.assertEqual(self.spec.label, "Superconductors +2")
        self.assertEqual(spec().label, "Gold")

    def test_combined_stock_qualifies_where_no_single_lot_does(self):
        # 1,080 t needed; no one lot reaches it, the three together do.
        ranked = self.rank(self.row("superconductors", 400),
                           self.row("semiconductors", 400),
                           self.row("computercomponents", 400))
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].stock, 1200)
        self.assertEqual(len(ranked[0].lots), 3)

    def test_a_market_short_on_the_combined_total_is_rejected(self):
        self.assertEqual(self.rank(self.row("superconductors", 500),
                                   self.row("semiconductors", 500)), [])

    def test_one_commodity_alone_can_still_fill_the_hold(self):
        ranked = self.rank(self.row("semiconductors", 5000))
        self.assertEqual([lot.display for lot in ranked[0].lots], ["Semiconductors"])
        self.assertEqual(ranked[0].plan[0][1], 720)

    def test_stock_is_never_pooled_across_different_markets(self):
        self.assertEqual(self.rank(self.row("superconductors", 600, market=1),
                                   self.row("semiconductors", 600, market=2)), [])

    def test_the_hold_is_filled_in_list_order_not_arrival_or_price_order(self):
        ranked = self.rank(self.row("computercomponents", 5000, buyPrice=500),
                           self.row("semiconductors", 300, buyPrice=900),
                           self.row("superconductors", 200, buyPrice=6000))
        self.assertEqual([(lot.display, tonnes) for lot, tonnes in ranked[0].plan],
                         [("Superconductors", 200), ("Semiconductors", 300),
                          ("Computer Components", 220)])

    def test_the_price_of_a_mixed_hold_is_the_cost_of_the_plan(self):
        ranked = self.rank(self.row("superconductors", 360, buyPrice=6000),
                           self.row("semiconductors", 5000, buyPrice=1000))
        self.assertEqual(ranked[0].buy_price, 3500)       # half the hold at each price

    def test_all_else_equal_the_first_listed_commodity_wins_over_a_cheaper_one(self):
        # Without this tier the price tiebreak would simply pick whichever
        # market sells the cheapest commodity on the list.
        self.assertEqual([c.station for c in self.rank(
            self.row("computercomponents", 5000, market=1, buyPrice=440),
            self.row("superconductors", 5000, market=2, buyPrice=6000))],
            ["Port 2", "Port 1"])

    def test_more_of_the_first_listed_commodity_wins(self):
        self.assertEqual([c.station for c in self.rank(
            self.row("superconductors", 200, market=1),
            self.row("semiconductors", 5000, market=1),
            self.row("superconductors", 600, market=2),
            self.row("semiconductors", 5000, market=2))], ["Port 2", "Port 1"])

    def test_list_order_never_outranks_anything_that_costs_time(self):
        # Each loser here is the first-listed commodity; each winner is the
        # last-listed one at a market that is quicker to use.
        first = dict(market=1)
        last = dict(market=2)
        for label, slower, quicker in (
                ("jumps", dict(distance=300), dict(distance=20)),
                ("arrival", dict(distanceToArrival=4000), dict(distanceToArrival=50)),
                ("surface", dict(stationType="CraterPort"), dict(stationType="Coriolis"))):
            with self.subTest(tier=label):
                ranked = self.rank(self.row("superconductors", 5000, **first, **slower),
                                   self.row("computercomponents", 5000, **last, **quicker))
                self.assertEqual([c.station for c in ranked], ["Port 2", "Port 1"])

    def test_list_order_decides_before_a_bigger_pad_could(self):
        # The pad does not rank at all, so the first-listed commodity on an M
        # pad beats the last-listed one on an L.
        ranked = core.rank_candidates(
            [self.row("computercomponents", 5000, market=1, maxLandingPadSize=3),
             self.row("superconductors", 5000, market=2, maxLandingPadSize=2)],
            dataclasses.replace(self.spec, min_pad=2), now=self.now)
        self.assertEqual([c.station for c in ranked], ["Port 2", "Port 1"])

    def test_price_still_decides_between_identical_plans(self):
        self.assertEqual([c.station for c in self.rank(
            self.row("superconductors", 5000, market=1, buyPrice=6000),
            self.row("superconductors", 5000, market=2, buyPrice=5000))],
            ["Port 2", "Port 1"])

    def test_a_single_commodity_has_no_list_tier(self):
        single = spec()
        candidate = core.rank_candidates(
            [self.row("gold", 5000)], single, now=self.now)[0]
        self.assertEqual(core.list_preference(candidate, single), ())

    def test_the_existing_tiers_still_decide_between_mixed_markets(self):
        near = [self.row("superconductors", 600, market=1, distance=20),
                self.row("semiconductors", 600, market=1, distance=20)]
        far = [self.row("superconductors", 600, market=2, distance=300, buyPrice=1),
               self.row("semiconductors", 600, market=2, distance=300, buyPrice=1)]
        self.assertEqual([c.station for c in self.rank(*(far + near))], ["Port 1", "Port 2"])

    def test_the_oldest_reading_speaks_for_the_market(self):
        ranked = self.rank(self.row("superconductors", 600),
                           self.row("semiconductors", 600, updatedAt=self.iso(hours=30)))
        self.assertAlmostEqual(ranked[0].age_seconds, 30 * 3600.0)

    def test_an_empty_or_unpriced_lot_contributes_nothing(self):
        ranked = self.rank(self.row("superconductors", 2000),
                           self.row("semiconductors", 0, updatedAt=self.iso(days=6)),
                           self.row("computercomponents", 900, buyPrice=0))
        self.assertEqual([lot.display for lot in ranked[0].lots], ["Superconductors"])
        self.assertAlmostEqual(ranked[0].age_seconds, 600.0)

    def test_a_commodity_that_was_not_listed_is_not_counted(self):
        self.assertEqual(self.rank(self.row("superconductors", 600),
                                   self.row("gold", 5000)), [])

    def test_the_display_names_spansh_sends_are_recognised(self):
        ranked = self.rank(self.row("Superconductors", 600),
                           self.row("Computer Components", 600))
        self.assertEqual([lot.ardent for lot in ranked[0].lots],
                         ["superconductors", "computercomponents"])

    def test_the_description_lists_what_to_load(self):
        ranked = self.rank(self.row("superconductors", 500, buyPrice=6741),
                           self.row("computercomponents", 9000, buyPrice=549))
        lines = core.describe_candidate(ranked[0], self.spec)
        self.assertEqual(lines[0], "BUY Superconductors +2 @ Port 1")
        self.assertEqual(lines[2], "500 t Superconductors - 6,741 cr/t (500 t in stock)")
        self.assertEqual(lines[3], "220 t Computer Components - 549 cr/t (9,000 t in stock)")
        self.assertIn("10m ago", lines[4])

    def test_the_forecast_needs_a_real_sell_price_for_every_planned_lot(self):
        plan = self.rank(self.row("superconductors", 500, buyPrice=6000),
                         self.row("semiconductors", 9000, buyPrice=1000))[0].plan
        self.assertEqual(core.plan_profit_lines(plan, {"superconductors": 7000}), [])
        self.assertEqual(
            core.plan_profit_lines(plan, {"superconductors": 7000, "semiconductors": 1500}),
            ["+847 cr/t avg - +610,000 cr/full hold"])

    def test_a_single_commodity_run_is_described_exactly_as_before(self):
        single = spec()
        row = self.row("gold", 5000, buyPrice=45000)
        lines = core.describe_candidate(
            core.rank_candidates([row], single, now=self.now)[0], single)
        self.assertEqual(lines, [
            "BUY Gold @ Port 1",
            "Sys 1 - 20.0 ly - ~1+1 jumps (est)",
            "5,000 t - 45,000 cr/t - 10m ago",
            "L pad - Coriolis - 200 Ls",
        ])


class MultiCommoditySearch(unittest.TestCase):
    """The upstream answers for one commodity at a time, so the search asks once
    per listed commodity per ring and merges what comes back."""

    def setUp(self):
        self.now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.spec = spec(commodity_ardent="superconductors",
                         commodity_display="Superconductors",
                         also=(("semiconductors", "Semiconductors"),))

    def row(self, commodity, stock, market=1, **kw):
        base = {
            "commodityName": commodity, "systemName": "Sys {}".format(market),
            "stationName": "Port {}".format(market), "marketId": market,
            "stationType": "Coriolis", "maxLandingPadSize": 3, "stock": stock,
            "buyPrice": 1000, "distance": 20, "distanceToArrival": 200,
            "updatedAt": (self.now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        }
        base.update(kw)
        return base

    def ok(self, rows, capped=False):
        return edta_ardent.SearchResult(edta_ardent.OK, rows=rows, window_capped=capped)

    def test_each_ring_asks_once_per_commodity_in_list_order(self):
        calls = []

        def fetch(commodity, radius, days):
            calls.append((commodity, radius))
            return self.ok([self.row(commodity, 600)])

        outcome = session_mod.search_best(self.spec, fetch, now=self.now)
        ring = self.spec.first_ring_ly
        self.assertEqual(calls, [("superconductors", ring), ("semiconductors", ring)])
        self.assertEqual(outcome.candidate.stock, 1200)
        self.assertEqual(outcome.attempts[0].rows, 2)

    def test_a_single_commodity_still_costs_one_request_a_ring(self):
        calls = []

        def fetch(commodity, radius, days):
            calls.append(commodity)
            return self.ok([])

        session_mod.search_best(spec(), fetch, now=self.now)
        self.assertEqual(len(calls), len(session_mod.escalation(spec())))
        self.assertEqual(set(calls), {"gold"})

    def test_lots_found_in_different_rings_still_add_up(self):
        # Rings accumulate per commodity: a later ring that happens to omit a
        # lot (the unsorted row cap) must not take it back out of the total.
        seen = []

        def fetch(commodity, radius, days):
            seen.append(radius)
            first = radius == self.spec.first_ring_ly
            if commodity == "superconductors":
                return self.ok([self.row(commodity, 600)] if first else [])
            return self.ok([] if first else [self.row(commodity, 600)])

        outcome = session_mod.search_best(self.spec, fetch, now=self.now)
        self.assertGreater(len(set(seen)), 1)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.candidate.stock, 1200)

    def test_a_cap_on_any_one_commodity_stops_the_expansion(self):
        calls = []

        def fetch(commodity, radius, days):
            calls.append((commodity, radius))
            return self.ok([], capped=commodity == "superconductors")

        outcome = session_mod.search_best(self.spec, fetch, now=self.now)
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(outcome.window_capped)

    def test_an_error_on_a_later_commodity_is_an_error(self):
        def fetch(commodity, radius, days):
            if commodity == "semiconductors":
                return edta_ardent.SearchResult(edta_ardent.NETWORK, message="down")
            return self.ok([self.row(commodity, 5000)])

        outcome = session_mod.search_best(self.spec, fetch, now=self.now)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, edta_ardent.NETWORK)

    def test_spansh_is_asked_per_commodity_for_its_share_of_the_requirement(self):
        # Pigeonhole: 1,080 t across two commodities means at least 540 t of one.
        self.assertEqual(session_mod.spansh_min_supply(self.spec), 540)
        self.assertEqual(session_mod.spansh_min_supply(spec()), 1080)
        three = spec(also=(("silver", "Silver"), ("palladium", "Palladium")))
        self.assertEqual(session_mod.spansh_min_supply(three), 360)

    def test_spansh_rows_for_one_market_merge_across_its_commodity_queries(self):
        stamp = (self.now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
        station = spansh_station(market_updated_at=stamp, type="Coriolis Starport", market=[
            {"commodity": "Superconductors", "buy_price": 6741, "supply": 600},
            {"commodity": "Semiconductors", "buy_price": 900, "supply": 600},
        ])
        rows = edta_spansh.normalise_rows(station, ["Superconductors", "Semiconductors"])
        calls = []

        def fetch(name, hours):
            calls.append((name, hours))
            return self.ok(rows)

        outcome = session_mod.search_spansh(self.spec, fetch, now=self.now)
        self.assertEqual(calls, [("Superconductors", 1.0), ("Semiconductors", 1.0)])
        self.assertEqual(outcome.candidate.stock, 1200)       # not 2,400
        self.assertEqual(outcome.source, "Spansh")


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

    def test_a_comma_separated_list_is_kept_in_the_order_typed(self):
        result = session_mod.validate(self.good(
            commodity="Superconductors, semiconductors ,Computer Components"))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.spec.commodities,
                         (("superconductors", "Superconductors"),
                          ("semiconductors", "Semiconductors"),
                          ("computercomponents", "Computer Components")))

    def test_a_single_commodity_has_no_extras(self):
        self.assertEqual(session_mod.validate(self.good()).spec.also, ())

    def test_one_typo_in_a_list_is_reported_with_suggestions(self):
        result = session_mod.validate(self.good(commodity="Gold, Platnum, Silver"))
        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn("Platinum", result.errors[0])

    def test_duplicates_and_stray_commas_are_ignored(self):
        result = session_mod.validate(self.good(commodity="Gold,, gold, $gold_name;, Silver,"))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual([d for _, d in result.spec.commodities], ["Gold", "Silver"])

    def test_the_list_is_capped(self):
        result = session_mod.validate(self.good(
            commodity="Gold, Silver, Palladium, Platinum, Tritium, Bauxite"))
        self.assertFalse(result.ok)
        self.assertTrue(any("at most" in e for e in result.errors), result.errors)
        self.assertLessEqual(session_mod.MAX_COMMODITIES, 5)

    def test_only_commas_is_still_no_commodity(self):
        result = session_mod.validate(self.good(commodity=" , "))
        self.assertFalse(result.ok)
        self.assertTrue(any("No commodity" in e for e in result.errors), result.errors)


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

        def fetch(commodity, radius, days):
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

        def fetch(commodity, radius, days):
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

        def fetch(commodity, radius, days):
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

        def fetch(commodity, radius, days):
            calls.append(radius)
            return edta_ardent.SearchResult(edta_ardent.OK, rows=[stale])

        outcome = session_mod.search_best(spec(min_pad=1), fetch, now=now)
        self.assertTrue(outcome.ok)
        self.assertGreater(len(calls), 1, "a stale near hit must not end the search")

    def test_a_relaxed_result_says_so(self):
        rows = load_fixture()
        now = fixture_now(rows)
        calls = []

        def fetch(commodity, radius, days):
            calls.append((radius, days))
            hit = days > 7
            return edta_ardent.SearchResult(edta_ardent.OK, rows=rows if hit else [])

        outcome = session_mod.search_best(spec(min_pad=1), fetch, now=now)
        self.assertTrue(outcome.ok)
        self.assertTrue(any("days old" in note for note in outcome.relaxed), outcome.relaxed)

    def test_an_upstream_error_is_not_reported_as_nothing_found(self):
        def fetch(commodity, radius, days):
            return edta_ardent.SearchResult(edta_ardent.NOT_FOUND, message="no such system")

        outcome = session_mod.search_best(spec(), fetch)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, edta_ardent.NOT_FOUND)
        self.assertEqual(len(outcome.attempts), 0)


def spansh_station(**overrides):
    """One station as Spansh's /api/stations/search returns it, trimmed."""
    base = {
        "name": "Thorn Constructions", "system_name": "HR 5518", "market_id": 4359005955,
        "system_id64": 83986944778, "type": "Planetary Outpost",
        "system_x": 84.15625, "system_y": 238.78125, "system_z": 266.625,
        "distance": 55.1, "distance_to_arrival": 608.0,
        "large_pads": 2, "medium_pads": 0, "small_pads": 4, "has_large_pad": True,
        "market_updated_at": "2026-10-10T12:24:41Z",
        "market": [
            {"commodity": "Gold", "buy_price": 45000, "sell_price": 44000,
             "supply": 5000, "demand": 1},
            {"commodity": "Silver", "buy_price": 4000, "sell_price": 3900,
             "supply": 9, "demand": 1},
        ],
    }
    base.update(overrides)
    return base


class SpanshContract(unittest.TestCase):
    def test_a_station_becomes_an_ardent_shaped_row(self):
        row = edta_spansh.normalise_row(spansh_station(), "gold")
        # Spansh prices are already player-side: buy_price is what you pay.
        self.assertEqual(row["buyPrice"], 45000)
        self.assertEqual(row["stock"], 5000)
        self.assertEqual(row["maxLandingPadSize"], 3)
        now = core.parse_updated_at("2026-10-10T13:00:00Z")
        candidate = core.candidate_from_row(row, spec(), now=now)
        self.assertEqual((candidate.station, candidate.system),
                         ("Thorn Constructions", "HR 5518"))
        self.assertEqual(candidate.market_id, 4359005955)
        self.assertAlmostEqual(candidate.age_seconds, 35 * 60 + 19)
        self.assertTrue(core.passes_filters(candidate, spec()))

    def test_pad_size_comes_from_the_pad_counts(self):
        medium = spansh_station(large_pads=0, medium_pads=3, has_large_pad=False)
        small = spansh_station(large_pads=0, medium_pads=0, has_large_pad=False)
        self.assertEqual(edta_spansh.normalise_row(medium, "Gold")["maxLandingPadSize"], 2)
        self.assertEqual(edta_spansh.normalise_row(small, "Gold")["maxLandingPadSize"], 1)

    def test_a_station_without_the_commodity_yields_no_row(self):
        self.assertIsNone(edta_spansh.normalise_row(spansh_station(), "Palladium"))

    def test_the_request_filters_and_sorts_server_side(self):
        captured = {}

        class Recorder(edta_spansh.SpanshClient):
            def _request(self, body, commodity_name):
                captured.update(body)
                return edta_ardent.SearchResult(edta_ardent.OK, rows=[])

        now = core.parse_updated_at("2026-10-10T13:00:00Z")
        Recorder(user_agent="test").stations_selling("Sol", "Gold", 340, 1080, True, 6, now=now)
        filters = captured["filters"]
        self.assertEqual(captured["reference_system"], "Sol")
        self.assertEqual(captured["sort"], [{"distance": {"direction": "asc"}}])
        self.assertEqual(captured["size"], edta_spansh.PAGE_SIZE)
        self.assertEqual(filters["market"][0]["name"], "Gold")
        self.assertEqual(filters["market"][0]["supply"]["value"][0], 1080)
        self.assertEqual(filters["distance"]["max"], 340)
        self.assertEqual(filters["market_updated_at"]["value"][0], "2026-10-10T07:00:00Z")
        self.assertEqual(filters["has_large_pad"], {"value": True})
        # Carriers and construction depots are kept out by the type allowlist.
        self.assertNotIn("Drake-Class Carrier", filters["type"]["value"])
        self.assertFalse([t for t in filters["type"]["value"] if "Depot" in t])

    def test_one_station_yields_a_row_for_each_listed_commodity_it_stocks(self):
        rows = edta_spansh.normalise_rows(spansh_station(), ["Silver", "Palladium", "gold"])
        self.assertEqual([(row["commodityName"], row["stock"]) for row in rows],
                         [("Silver", 9), ("Gold", 5000)])
        self.assertEqual({row["marketId"] for row in rows}, {4359005955})

    def test_extra_commodities_are_read_out_but_not_filtered_on(self):
        captured = {}

        class Recorder(edta_spansh.SpanshClient):
            def _request(self, body, commodity_names):
                captured["body"] = body
                captured["names"] = list(commodity_names)
                return edta_ardent.SearchResult(edta_ardent.OK, rows=[])

        Recorder(user_agent="test").stations_selling(
            "Sol", "Silver", 340, 540, True, 6, also_names=["Gold", "Silver"])
        self.assertEqual(captured["names"], ["Silver", "Gold"])
        market = captured["body"]["filters"]["market"]
        self.assertEqual([entry["name"] for entry in market], ["Silver"])
        self.assertEqual(market[0]["supply"]["value"][0], 540)

    def test_a_page_stays_small(self):
        # Every station costs ~123 KB; a casual bump here is megabytes a search.
        self.assertLessEqual(edta_spansh.PAGE_SIZE, 5)

    def test_radius_is_refused_past_500_ly(self):
        result = edta_spansh.SpanshClient(user_agent="test").stations_selling(
            "Sol", "Gold", 501, 100, True, 1)
        self.assertEqual(result.kind, edta_ardent.BAD_REQUEST)


class Fallback(unittest.TestCase):
    """Spansh is asked only when Ardent is failing or stalled, and its answer is
    taken only when it ranks better."""

    def setUp(self):
        self.rows = load_fixture()
        self.now = fixture_now(self.rows)
        self.target = spec(min_pad=1)

    def stamp(self, **ago):
        return (self.now - timedelta(**ago)).isoformat().replace("+00:00", "Z")

    def ardent_row(self, **overrides):
        return {**self.rows[0], "stock": 999999, "maxLandingPadSize": 3, **overrides}

    def spansh_row(self, **overrides):
        row = edta_spansh.normalise_row(
            spansh_station(market_updated_at=self.stamp(minutes=20)), "Gold")
        row.update(overrides)
        return row

    def run_search(self, ardent, spansh):
        self.spansh_calls = []

        def fallback(commodity, hours):
            self.spansh_calls.append(hours)
            return spansh(commodity, hours)

        return session_mod.search_with_fallback(self.target, ardent, fallback, now=self.now)

    def ok(self, rows):
        return edta_ardent.SearchResult(edta_ardent.OK, rows=rows)

    def test_a_fresh_ardent_answer_never_touches_spansh(self):
        outcome = self.run_search(lambda c, r, d: self.ok(self.rows),
                                  lambda c, h: self.ok([self.spansh_row()]))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.source, "Ardent")
        self.assertEqual(self.spansh_calls, [])

    def test_a_stalled_ardent_is_replaced_by_a_fresher_spansh_market(self):
        # The October 2026 outage: every Ardent row frozen at the same hour.
        stalled = [self.ardent_row(marketId=1, updatedAt=self.stamp(hours=28)),
                   self.ardent_row(marketId=2, updatedAt=self.stamp(hours=29))]
        outcome = self.run_search(lambda c, r, d: self.ok(stalled),
                                  lambda c, h: self.ok([self.spansh_row()]))
        self.assertEqual(outcome.source, "Spansh")
        self.assertEqual(outcome.candidate.station, "Thorn Constructions")
        self.assertIn("Ardent data stale - using Spansh", outcome.relaxed)

    def test_one_fresh_unusable_row_proves_ardent_is_alive(self):
        # Staleness is judged on every row returned, not just the qualifying
        # ones: a just-updated market with no stock still shows Ardent ingesting.
        rows = [self.ardent_row(marketId=1, updatedAt=self.stamp(hours=28)),
                self.ardent_row(marketId=2, stock=0, updatedAt=self.stamp(minutes=5))]
        outcome = self.run_search(lambda c, r, d: self.ok(rows),
                                  lambda c, h: self.ok([self.spansh_row()]))
        self.assertEqual(outcome.source, "Ardent")
        self.assertEqual(self.spansh_calls, [])

    def test_an_ardent_error_falls_back(self):
        outcome = self.run_search(
            lambda c, r, d: edta_ardent.SearchResult(edta_ardent.NETWORK, message="down"),
            lambda c, h: self.ok([self.spansh_row()]))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.source, "Spansh")
        self.assertIn("Ardent unavailable - using Spansh", outcome.relaxed)

    def test_an_empty_ardent_falls_back(self):
        outcome = self.run_search(lambda c, r, d: self.ok([]),
                                  lambda c, h: self.ok([self.spansh_row()]))
        self.assertEqual(outcome.source, "Spansh")
        self.assertIn("Ardent found nothing - using Spansh", outcome.relaxed)

    def test_a_failing_spansh_leaves_the_ardent_outcome_alone(self):
        stalled = [self.ardent_row(marketId=1, updatedAt=self.stamp(hours=28))]
        outcome = self.run_search(
            lambda c, r, d: self.ok(stalled),
            lambda c, h: edta_ardent.SearchResult(edta_ardent.NETWORK, message="down"))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.source, "Ardent")
        self.assertFalse([note for note in outcome.relaxed if "Spansh" in note])

    def test_both_failing_reports_the_ardent_error(self):
        outcome = self.run_search(
            lambda c, r, d: edta_ardent.SearchResult(edta_ardent.NOT_FOUND, message="no such system"),
            lambda c, h: edta_ardent.SearchResult(edta_ardent.NOT_FOUND, message="spansh says no"))
        self.assertEqual(outcome.error_message, "no such system")

    def test_spansh_must_rank_better_to_win(self):
        stalled = [self.ardent_row(marketId=1, updatedAt=self.stamp(hours=28))]
        older = self.spansh_row(updatedAt=self.stamp(days=5))
        outcome = self.run_search(lambda c, r, d: self.ok(stalled), lambda c, h: self.ok([older]))
        self.assertEqual(outcome.source, "Ardent")

    def test_the_recency_window_widens_a_band_at_a_time_and_stops_at_a_hit(self):
        windows = session_mod.spansh_windows(self.target)
        self.assertEqual(windows, [1.0, 6.0, 24.0, 72.0, 168.0])
        hit = self.spansh_row(updatedAt=self.stamp(hours=10))
        outcome = self.run_search(lambda c, r, d: self.ok([]),
                                  lambda c, h: self.ok([hit] if h >= 24 else []))
        self.assertTrue(outcome.ok)
        self.assertEqual(self.spansh_calls, [1.0, 6.0, 24.0])

    def test_windows_never_reach_past_the_oldest_acceptable_data(self):
        self.assertEqual(session_mod.spansh_windows(spec(max_age_days=2)),
                         [1.0, 6.0, 24.0, 48.0])

    def test_rows_that_fail_the_local_filters_do_not_stop_the_widening(self):
        # Spansh can only filter large pads server-side, so a window can come
        # back non-empty and still hold nothing usable.
        small = self.spansh_row(maxLandingPadSize=1)
        good = self.spansh_row(marketId=7, stationName="Bigger",
                               updatedAt=self.stamp(hours=3))
        self.target = spec(min_pad=2)
        outcome = self.run_search(lambda c, r, d: self.ok([]),
                                  lambda c, h: self.ok([small] if h < 6 else [small, good]))
        self.assertEqual(outcome.candidate.station, "Bigger")
        self.assertEqual(self.spansh_calls, [1.0, 6.0])

    def test_without_a_fallback_the_ardent_outcome_is_returned(self):
        outcome = session_mod.search_with_fallback(
            self.target, lambda c, r, d: self.ok([]), None, now=self.now)
        self.assertFalse(outcome.ok)


class SessionLedger(unittest.TestCase):
    """The realised-profit arithmetic on its own - no Session, no fixture.

    The theoretical profit line answers "what should this run be worth". This
    answers "what has it actually paid", which is a different number and the one
    a cr/hr figure has to be built from.
    """

    def setUp(self):
        self.start = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.ledger = session_mod.Ledger(started_at=self.start)

    def test_profit_accumulates_across_sales(self):
        self.ledger.record_sale(39_600_000, 720, 45595)
        self.ledger.record_sale(20_000_000, 400, 45000)
        self.assertEqual(self.ledger.credits,
                         (39_600_000 - 45595 * 720) + (20_000_000 - 45000 * 400))
        self.assertEqual(self.ledger.tonnes, 1120)
        self.assertEqual(self.ledger.sales, 2)

    def test_the_rate_is_credits_per_hour(self):
        self.ledger.record_sale(12_000_000, 100, 0, no_cost_basis=True)
        rate = self.ledger.per_hour(self.start + timedelta(hours=3))
        self.assertAlmostEqual(rate, 4_000_000.0)

    def test_zero_elapsed_time_does_not_divide_by_zero(self):
        self.ledger.record_sale(5_000_000, 100, 1000)
        self.assertIsNone(self.ledger.per_hour(self.start))
        line = core.session_lines(self.ledger.elapsed_seconds(self.start),
                                  self.ledger.runs, self.ledger.credits)[0]
        self.assertIn("? cr/hr", line)

    def test_a_short_session_does_not_extrapolate(self):
        """One hold sold a minute in would otherwise read as billions an hour."""
        self.ledger.record_sale(18_400_000, 720, 100)
        line = core.session_lines(60.0, 1, self.ledger.credits)[0]
        self.assertIn("? cr/hr", line)

    def test_a_missing_avg_price_paid_falls_back_to_the_buy_price(self):
        self.ledger.buy_price_basis = 45595
        self.ledger.record_sale(39_600_000, 720, None)
        self.assertEqual(self.ledger.credits, 39_600_000 - 45595 * 720)
        self.assertTrue(self.ledger.estimated)

    def test_a_zero_avg_price_paid_is_treated_as_absent(self):
        """Not as free cargo - a zero here means the journal did not say."""
        self.ledger.buy_price_basis = 1000
        self.ledger.record_sale(5_000_000, 100, 0)
        self.assertEqual(self.ledger.credits, 5_000_000 - 100_000)
        self.assertTrue(self.ledger.estimated)

    def test_no_cost_basis_at_all_counts_the_whole_sale(self):
        self.ledger.record_sale(5_000_000, 100, None)
        self.assertEqual(self.ledger.credits, 5_000_000)
        self.assertTrue(self.ledger.estimated)

    def test_stolen_cargo_has_no_cost_and_is_not_charged_one(self):
        """Subtracting a price never paid would print a loss that never happened."""
        self.ledger.buy_price_basis = 45595
        self.ledger.record_sale(5_000_000, 100, None, no_cost_basis=True)
        self.assertEqual(self.ledger.credits, 5_000_000)
        self.assertFalse(self.ledger.estimated)

    def test_a_clock_stepping_backwards_does_not_go_negative(self):
        self.assertEqual(self.ledger.elapsed_seconds(self.start - timedelta(hours=1)), 0.0)

    def test_a_ledger_that_never_started_has_no_elapsed_time(self):
        blank = session_mod.Ledger()
        self.assertIsNone(blank.elapsed_seconds(self.start))
        self.assertEqual(blank.lines(self.start), [])

    def test_a_sale_from_before_the_session_is_a_replay(self):
        self.assertTrue(self.ledger.is_replay(self.start - timedelta(minutes=40)))
        self.assertFalse(self.ledger.is_replay(self.start - timedelta(seconds=5)))
        self.assertFalse(self.ledger.is_replay(self.start + timedelta(minutes=40)))
        self.assertFalse(self.ledger.is_replay(None))


class SessionProfitFromJournal(unittest.TestCase):
    """The same ledger, driven through the real state machine by real events."""

    def setUp(self):
        self.rows = load_fixture()
        self.now = fixture_now(self.rows)
        self.session = session_mod.Session()
        params = session_mod.Params(sell_system="Sol", commodity="Gold",
                                    cargo_capacity="200", unladen_range="50",
                                    laden_range="40", min_pad="M")
        validation, self.effects = self.session.start(params, now=self.now)
        self.assertTrue(validation.ok)

    def stamp(self, minutes=1):
        return (self.now + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")

    def search_and_apply(self, effects):
        """Run the search the session just asked for, and hand back the answer."""
        generation, search_spec = effects[-1].payload
        outcome = session_mod.search_best(
            search_spec,
            lambda c, r, d: edta_ardent.SearchResult(edta_ardent.OK, rows=self.rows),
            now=self.now)
        self.session.apply_outcome(generation, outcome)
        return self.session.target

    def dock_at_buy(self, target):
        self.session.on_journal(
            {"event": "Docked", "StarSystem": target.system,
             "StationName": target.station, "MarketID": target.market_id}, {})
        self.assertEqual(self.session.state, session_mod.TO_SELL)

    def reach_to_sell(self):
        """Search, then dock at the buy station - laden and headed home."""
        target = self.search_and_apply(self.effects)
        self.dock_at_buy(target)
        return target

    def dock_home(self):
        return self.session.on_journal(
            {"event": "Docked", "StarSystem": "Sol", "StationName": "Abraham Lincoln",
             "MarketID": 128016384}, {})

    def sell(self, minutes=30, **overrides):
        entry = {"event": "MarketSell", "timestamp": self.stamp(minutes),
                 "Type": "gold", "Count": 200, "SellPrice": 55000,
                 "TotalSale": 11_000_000, "AvgPricePaid": 45595}
        entry.update(overrides)
        return self.session.on_journal(entry, {})

    def test_a_marketsell_of_the_tracked_commodity_adds_profit(self):
        self.reach_to_sell()
        self.dock_home()
        effects = self.sell()
        self.assertEqual(self.session.ledger.credits, 11_000_000 - 45595 * 200)
        self.assertIn(session_mod.REDRAW, [e.kind for e in effects])

    def test_a_sale_of_an_untracked_commodity_is_ignored(self):
        self.reach_to_sell()
        self.dock_home()
        self.sell(Type="silver")
        self.assertEqual(self.session.ledger.credits, 0)
        self.assertEqual(self.session.ledger.sales, 0)

    def test_the_journal_spelling_of_the_commodity_still_matches(self):
        """The journal writes '$gold_name;' where Ardent writes 'gold'."""
        self.reach_to_sell()
        self.dock_home()
        self.sell(Type="$gold_name;")
        self.assertEqual(self.session.ledger.sales, 1)

    def test_a_run_is_counted_when_the_cargo_gets_home(self):
        self.reach_to_sell()
        self.dock_home()
        self.assertEqual(self.session.ledger.runs, 1)

    def test_re_docking_in_the_sell_system_does_not_count_a_run(self):
        """Only arriving laden counts. Starting here, or re-docking while still
        shopping, is not a completed round trip."""
        self.dock_home()
        self.dock_home()
        self.assertEqual(self.session.ledger.runs, 0)

    def test_a_sale_without_avgpricepaid_uses_the_price_paid_at_the_buy_station(self):
        """self.target is already None by sale time, so the basis has to have
        been seeded at the turnaround."""
        target = self.reach_to_sell()
        self.dock_home()
        self.assertIsNone(self.session.target)
        self.sell(AvgPricePaid=None)
        self.assertEqual(self.session.ledger.credits, 11_000_000 - target.buy_price * 200)
        self.assertTrue(self.session.ledger.estimated)

    def test_a_marketbuy_is_preferred_over_the_planned_price(self):
        self.reach_to_sell()
        self.session.on_journal({"event": "MarketBuy", "Type": "gold", "Count": 200,
                                 "BuyPrice": 40000}, {})
        self.dock_home()
        self.sell(AvgPricePaid=None)
        self.assertEqual(self.session.ledger.credits, 11_000_000 - 40000 * 200)

    def test_a_sale_timestamped_before_the_session_started_is_ignored(self):
        """EDMC replays the tail of the journal when it loads."""
        self.reach_to_sell()
        self.dock_home()
        self.sell(minutes=-40)
        self.assertEqual(self.session.ledger.sales, 0)

    def test_a_sale_while_stopped_is_ignored(self):
        self.session.stop()
        self.sell()
        self.assertEqual(self.session.ledger.sales, 0)

    def test_no_session_line_before_the_first_sale(self):
        self.reach_to_sell()
        lines, _ = self.session.display_lines(now=self.now + timedelta(hours=1))
        self.assertFalse([line for line in lines if line.startswith("Session:")], lines)

    def session_line(self, now):
        lines, role = self.session.display_lines(now=now)
        found = [line for line in lines if line.startswith("Session:")]
        self.assertEqual(len(found), 1, lines)
        return found[0], lines, role

    def test_the_session_line_appears_on_every_leg_after_the_first_sale(self):
        """Docking home leaves the session SEARCHING, so TO_SELL is only reached
        again on the next lap - which is a second leg worth driving properly."""
        self.reach_to_sell()
        effects = self.dock_home()
        self.sell()
        later = self.now + timedelta(minutes=72)

        self.assertEqual(self.session.state, session_mod.SEARCHING)
        line, _, role = self.session_line(later)
        self.assertEqual(role, "buy")
        self.assertTrue(line.startswith("Session: 1h12m - 1 run - "), line)

        target = self.search_and_apply(effects)
        self.assertEqual(self.session.state, session_mod.TO_BUY)
        line, lines, role = self.session_line(later)
        self.assertEqual(role, "buy")
        # The paste instruction is the call to action and stays below it.
        self.assertLess(lines.index(line),
                        next(i for i, text in enumerate(lines) if "paste into" in text), lines)

        self.dock_at_buy(target)
        line, lines, role = self.session_line(later)
        self.assertEqual(role, "sell")
        self.assertLess(lines.index(line),
                        next(i for i, text in enumerate(lines) if "paste into" in text), lines)

    def test_a_second_lap_counts_a_second_run(self):
        self.reach_to_sell()
        effects = self.dock_home()
        self.sell()
        self.dock_at_buy(self.search_and_apply(effects))
        self.dock_home()
        self.sell(minutes=60)
        self.assertEqual(self.session.ledger.runs, 2)
        self.assertEqual(self.session.ledger.sales, 2)
        self.assertEqual(self.session.ledger.tonnes, 400)

    def test_stopping_reports_a_summary_and_resets(self):
        self.reach_to_sell()
        self.dock_home()
        self.sell()
        earned = self.session.ledger.credits
        self.session.stop(now=self.now + timedelta(hours=2))
        self.assertEqual(self.session.state, session_mod.OFF)
        self.assertTrue(self.session.message.startswith("Session ended: "),
                        self.session.message)
        self.assertIn("2h00m", self.session.message)
        self.assertIn("1 run", self.session.message)
        self.assertEqual(self.session.ledger.credits, 0)
        self.assertGreater(earned, 0)

    def test_a_fresh_start_forgets_the_previous_session(self):
        self.reach_to_sell()
        self.dock_home()
        self.sell()
        self.session.start(self.session.params, now=self.now + timedelta(hours=3))
        self.assertEqual(self.session.ledger.credits, 0)
        self.assertEqual(self.session.ledger.runs, 0)
        self.assertEqual(self.session.ledger.started_at, self.now + timedelta(hours=3))

    def test_a_rejected_start_does_not_start_a_clock(self):
        session = session_mod.Session()
        validation, _ = session.start(session_mod.Params(sell_system="", commodity="Golde"),
                                      now=self.now)
        self.assertFalse(validation.ok)
        self.assertIsNone(session.ledger.started_at)


class MixedHoldSession(unittest.TestCase):
    """A run listing two commodities, through the real state machine."""

    def setUp(self):
        self.now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.session = session_mod.Session()
        params = session_mod.Params(sell_system="Sol", commodity="Gold, Silver",
                                    cargo_capacity="200", unladen_range="50",
                                    laden_range="40", min_pad="M")
        validation, effects = self.session.start(params, now=self.now)
        self.assertTrue(validation.ok)
        generation, search_spec = effects[-1].payload
        stamp = (self.now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

        def fetch(commodity, radius, days):
            stock, price = {"gold": (120, 45000), "silver": (400, 4000)}[commodity]
            return edta_ardent.SearchResult(edta_ardent.OK, rows=[{
                "commodityName": commodity, "systemName": "Veren's Stop",
                "stationName": "Duncan's Den", "stationType": "Orbis", "marketId": 77,
                "maxLandingPadSize": 3, "stock": stock, "buyPrice": price,
                "distance": 33, "distanceToArrival": 10, "updatedAt": stamp}])

        self.session.apply_outcome(
            generation, session_mod.search_best(search_spec, fetch, now=self.now))
        self.assertEqual(self.session.state, session_mod.TO_BUY)

    def dock(self, system, station, market_id):
        return self.session.on_journal(
            {"event": "Docked", "StarSystem": system, "StationName": station,
             "MarketID": market_id}, {})

    def sell(self, commodity, count, total, **overrides):
        entry = {"event": "MarketSell", "Type": commodity, "Count": count,
                 "timestamp": (self.now + timedelta(minutes=30)).isoformat().replace(
                     "+00:00", "Z"),
                 "TotalSale": total}
        entry.update(overrides)
        return self.session.on_journal(entry, {})

    def test_the_buy_block_names_both_lots_in_list_order(self):
        lines, role = self.session.display_lines(now=self.now)
        self.assertEqual(role, "buy")
        self.assertEqual(lines[0], "BUY Gold +1 @ Duncan's Den")
        self.assertEqual(lines[2], "120 t Gold - 45,000 cr/t (120 t in stock)")
        self.assertEqual(lines[3], "80 t Silver - 4,000 cr/t (400 t in stock)")

    def test_sales_of_every_listed_commodity_are_booked(self):
        self.dock("Veren's Stop", "Duncan's Den", 77)
        self.dock("Sol", "Abraham Lincoln", 128016384)
        self.sell("gold", 120, 6_000_000, AvgPricePaid=45000)
        self.sell("$silver_name;", 80, 400_000, AvgPricePaid=4000)
        self.sell("palladium", 5, 999_999, AvgPricePaid=1)
        self.assertEqual(self.session.ledger.sales, 2)
        self.assertEqual(self.session.ledger.tonnes, 200)
        self.assertEqual(self.session.ledger.credits,
                         (6_000_000 - 45000 * 120) + (400_000 - 4000 * 80))

    def test_a_missing_avgpricepaid_uses_that_commoditys_own_planned_price(self):
        """Not the other commodity's, and not the blended price of the hold."""
        self.dock("Veren's Stop", "Duncan's Den", 77)
        self.dock("Sol", "Abraham Lincoln", 128016384)
        self.sell("silver", 80, 400_000)
        self.assertEqual(self.session.ledger.credits, 400_000 - 4000 * 80)
        self.sell("gold", 120, 6_000_000)
        self.assertEqual(self.session.ledger.credits,
                         (400_000 - 4000 * 80) + (6_000_000 - 45000 * 120))
        self.assertTrue(self.session.ledger.estimated)

    def test_a_marketbuy_overrides_only_its_own_commodity(self):
        self.dock("Veren's Stop", "Duncan's Den", 77)
        self.session.on_journal({"event": "MarketBuy", "Type": "silver", "Count": 80,
                                 "BuyPrice": 3500}, {})
        self.dock("Sol", "Abraham Lincoln", 128016384)
        self.sell("silver", 80, 400_000)
        self.sell("gold", 120, 6_000_000)
        self.assertEqual(self.session.ledger.credits,
                         (400_000 - 3500 * 80) + (6_000_000 - 45000 * 120))

    def test_the_forecast_waits_for_every_sell_price(self):
        def forecast():
            lines, _ = self.session.display_lines(now=self.now)
            return [line for line in lines if "cr/full hold" in line]

        self.assertEqual(forecast(), [])
        self.session.sell_prices = {"gold": 50000}
        self.assertEqual(forecast(), [])
        self.session.sell_prices = {"gold": 50000, "silver": 5000}
        self.assertEqual(forecast(), ["+3,400 cr/t avg - +680,000 cr/full hold"])
        self.dock("Veren's Stop", "Duncan's Den", 77)
        self.assertEqual(forecast(), ["+3,400 cr/t avg - +680,000 cr/full hold"])

    def test_the_return_leg_and_status_use_the_short_label(self):
        self.dock("Veren's Stop", "Duncan's Den", 77)
        lines, role = self.session.display_lines(now=self.now)
        self.assertEqual(role, "sell")
        self.assertEqual(lines[0], "SELL Gold +1 @ Sol")


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
            lambda c, r, d: edta_ardent.SearchResult(edta_ardent.OK, rows=self.rows),
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
            search_spec, lambda c, r, d: edta_ardent.SearchResult(edta_ardent.OK, rows=[]))
        state.apply_outcome(generation, outcome)
        self.assertEqual(state.state, session_mod.NO_RESULTS)
        self.assertIn("1,080", state.message)
        self.assertIn("L pad", state.message)
        self.assertIn("t of Gold on", state.message)

    def test_a_list_is_named_in_full_and_as_a_combined_requirement(self):
        state = session_mod.Session()
        params = session_mod.Params(sell_system="Sol",
                                    commodity="Gold, Silver, Palladium",
                                    cargo_capacity="720", unladen_range="50",
                                    laden_range="40", min_pad="L")
        _, effects = state.start(params)
        generation, search_spec = effects[-1].payload
        outcome = session_mod.search_best(
            search_spec, lambda c, r, d: edta_ardent.SearchResult(edta_ardent.OK, rows=[]))
        state.apply_outcome(generation, outcome)
        self.assertEqual(state.state, session_mod.NO_RESULTS)
        self.assertIn("1,080 t of Gold, Silver or Palladium combined on a L pad",
                      state.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
