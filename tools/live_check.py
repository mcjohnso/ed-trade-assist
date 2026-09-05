#!/usr/bin/env python3
"""Run one real search against Ardent and print the ranking, outside EDMC.

This is the check that the fixture-based tests cannot make: that the live API
still answers the shape we parse, and that the ranking picks something sensible.

    python tools/live_check.py Sol Gold --cargo 720 --laden 40

Add --raw to dump the top rows as they arrived, for comparing against
ardent-insight.com or another tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "EDTradeAssist"))

import edta_ardent                      # noqa: E402
import edta_core as core                # noqa: E402
import edta_session as session_mod      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sell_system")
    ap.add_argument("commodity")
    ap.add_argument("--cargo", type=int, default=720)
    ap.add_argument("--unladen", type=float, default=50.0)
    ap.add_argument("--laden", type=float, default=40.0)
    ap.add_argument("--pad", default="L")
    ap.add_argument("--jumps-per-leg", type=int, default=10)
    ap.add_argument("--max-age-days", type=int, default=7)
    ap.add_argument("--raw", action="store_true", help="dump the top rows verbatim")
    args = ap.parse_args()

    validation = session_mod.validate(session_mod.Params(
        sell_system=args.sell_system, commodity=args.commodity,
        cargo_capacity=str(args.cargo), unladen_range=str(args.unladen),
        laden_range=str(args.laden), min_pad=args.pad,
        jumps_per_leg=args.jumps_per_leg, max_age_days=args.max_age_days))
    if not validation.ok:
        for error in validation.errors:
            print("error:", error)
        return 1
    for warning in validation.warnings:
        print("warning:", warning)

    spec = validation.spec
    client = edta_ardent.ArdentClient(user_agent="EDTradeAssist-livecheck/0.1.0")
    print("{} -> {} within {:.0f} ly, >= {:,} t, {} pad, <= {} days old".format(
        spec.sell_system, spec.commodity_display, spec.radius_ly, spec.min_volume,
        core.pad_to_str(spec.min_pad), spec.max_age_days))

    seen = {}

    def fetch(radius, max_days):
        result = client.nearby_exports(spec.sell_system, spec.commodity_ardent,
                                       radius, spec.min_volume, max_days_ago=max_days)
        seen["result"] = result
        print("  fetch radius={:.0f} max_days={} -> {} ({} rows{})".format(
            radius, max_days, result.kind, len(result.rows),
            ", window capped" if result.window_capped else ""))
        return result

    outcome = session_mod.search_best(spec, fetch)

    if outcome.error_kind:
        print("upstream error [{}]: {}".format(outcome.error_kind, outcome.error_message))
        return 2
    if not outcome.ok:
        print("nothing qualified after {} attempt(s).".format(len(outcome.attempts)))
        return 3

    if outcome.relaxed:
        print("relaxed: " + "; ".join(outcome.relaxed))
    print("\nBEST:")
    for line in core.describe_candidate(outcome.candidate, spec):
        print("  " + line)

    if outcome.alternatives:
        print("\nrunners-up:")
        for candidate in outcome.alternatives:
            print("  {:<28} {:<18} {:>9,} t {:>8,} cr {:>10} {:>10}".format(
                candidate.station[:28], candidate.system[:18], candidate.stock,
                candidate.buy_price, core.human_ls(candidate.ls),
                core.human_age(candidate.age_seconds)))

    if args.raw:
        print("\nraw rows:")
        print(json.dumps(seen["result"].rows[:3], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
