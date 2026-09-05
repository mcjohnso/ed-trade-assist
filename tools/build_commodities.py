#!/usr/bin/env python3
"""Regenerate EDTradeAssist/edta_commodities.json from EDCD/FDevIDs.

The plugin ships a bundled commodity table so it can validate what you type and
translate it into the name Ardent expects, without a network round trip at
startup. Run this when Frontier adds commodities:

    python tools/build_commodities.py

It pulls the authoritative CSVs from FDevIDs. Pass --from-csv DIR to build from
local copies instead (e.g. ed-mcp's vendored ones) when offline.
"""

import argparse
import csv
import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAW = "https://raw.githubusercontent.com/EDCD/FDevIDs/master/{}.csv"
DATASETS = (("commodity", False), ("rare_commodity", True))
OUT = Path(__file__).resolve().parent.parent / "EDTradeAssist" / "edta_commodities.json"
UA = "EDTradeAssist-build/1.0 (+https://github.com/EDCD/FDevIDs consumer)"


def fetch(dataset: str) -> str:
    req = urllib.request.Request(RAW.format(dataset), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8-sig")


def read_local(directory: Path, dataset: str) -> str:
    return (directory / f"{dataset}.csv").read_text(encoding="utf-8-sig")


def ardent_name(symbol: str) -> str:
    """Ardent's commodity name is the FDev symbol, lowercased.

    Verified against live Ardent rows: symbol 'Gold' -> 'gold'. Ardent has no
    spaces or punctuation in these, matching the symbol exactly.
    """
    return symbol.strip().lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-csv", type=Path, default=None,
                    help="directory holding commodity.csv / rare_commodity.csv")
    args = ap.parse_args()

    rows = []
    seen = set()
    sources = []
    for dataset, is_rare in DATASETS:
        text = read_local(args.from_csv, dataset) if args.from_csv else fetch(dataset)
        sources.append(str(args.from_csv / f"{dataset}.csv") if args.from_csv else RAW.format(dataset))
        for rec in csv.DictReader(io.StringIO(text)):
            symbol = (rec.get("symbol") or "").strip()
            name = (rec.get("name") or "").strip()
            if not symbol or not name:
                continue
            key = re.sub(r"[^a-z0-9]", "", symbol.casefold())
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "symbol": symbol,
                "name": name,
                "category": (rec.get("category") or "").strip(),
                "ardent": ardent_name(symbol),
                "rare": is_rare,
            })

    if len(rows) < 200:
        print(f"refusing to write: only {len(rows)} rows, upstream looks wrong", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: r["name"].casefold())
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "commodities": rows,
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    rare = sum(1 for r in rows if r["rare"])
    print(f"wrote {OUT} - {len(rows)} commodities ({rare} rare)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
