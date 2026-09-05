# Working on ed-trade-assist

An EDMC plugin. Read `EDTradeAssist/README.md` for what it does; this file is the things that will
bite you.

## Constraints

- **"Standard library only" is necessary but NOT sufficient.** EDMC ships a *frozen* Python (3.13 as
  of EDMC 6.x — not 3.11), and its `library.zip` contains only the stdlib modules EDMC itself uses.
  `difflib` is absent, and importing it made v0.1.0 fail to load with nothing visible in the UI —
  the error appears only in `%LOCALAPPDATA%\EDMarketConnector\logs\EDMarketConnector-debug.log`.
  Before adding any import, check it against a real install:

  ```python
  import zipfile
  z = zipfile.ZipFile(r"C:/Program Files (x86)/EDMarketConnector/library.zip")
  print("difflib.pyc" in z.namelist())
  ```

  `FrozenRuntimeImports` in `tests/test_core.py` enforces this automatically. C builtins (`math`,
  `time`, `itertools`) live in `python313.dll` rather than the zip, so they are exempt.
- You cannot `pip install` into a user's EDMC. `requests` and `urllib3` *are* bundled;
  `edta_ardent.py` imports requests lazily and falls back to `urllib` so the module still works (and
  stays testable) without it.
- A partial `__pycache__` in the installed plugin folder is a useful diagnostic: the modules that
  compiled tell you how far the import chain got before it died.
- **Do not rename the `edta_`-prefixed helper modules.** EDMC adds every installed plugin's folder to
  one shared `sys.path`, so a generic `overlay.py` / `utils.py` would collide with another plugin's
  same-named module — first import wins, silently. Import them top-level (`import edta_core`);
  package-relative imports (`from . import core`) fail, because EDMC imports `load.py` directly.
- **The overlay message id is `tradeasst_block`, deliberately NOT `edta_`.** This looks like an
  oversight next to the rule above and is not: EDMCModernOverlay applies a position-normalising
  group transform to any message id matching a registered plugin group, and an unregistered prefix
  is what keeps the x/y we send honoured. Module prefixes and overlay id prefixes are different
  namespaces solving different problems.
- **Never create a folder named `config/`** inside the plugin — it shadows EDMC's own `config`
  module. (A sibling project did this and had to rename to `plugin_config/`.)
- `__version__` in `load.py` is the single source of truth. The git tag must match it.
- All EDMC callbacks run on the main Tk thread. The only Tk-safe call from a worker thread is
  `widget.event_generate(...)`, and even that raises when there is no mainloop — which is why
  `_schedule_heartbeat` polls the results queue as the actual guarantee.

## Architecture

`edta_core.py` and `edta_session.py` import nothing from EDMC, tkinter or the network. Keep it that
way: it is what lets `tests/test_core.py` drive a whole dock-buy-sell cycle in-process, and what
makes the ranking arguable in isolation. `search_best(spec, fetch)` takes a fetch callable precisely
so the worker thread can run the escalation without touching session state.

`load.py` performs `Effect`s the session hands back (`SEARCH`, `CLIPBOARD`, `REDRAW`) and owns every
Tk and EDMC interaction.

## Upstream traps (all verified live, all handled in code)

- **Direction inversion.** Ardent names endpoints from the *station's* perspective. The player
  buying maps to `/exports` and the price you pay is `buyPrice`. Getting this backwards produces a
  plausible-looking answer that is completely wrong.
- **404 means unknown SYSTEM only.** A misspelled *commodity* returns `200 []`, indistinguishable
  from "nothing matched" — which is why the commodity is validated against the bundled FDevIDs table
  before any request is sent.
- **`maxDistance` is silently clamped to 500 ly.** Never send more; refuse instead.
- **No sort parameter, and `/nearby/*` stops at 1000 rows.** The returned window is an arbitrary
  subset, not the nearest ones — this is the trap that produced v0.3.0's bug fix. See "The search
  expands outward" below.
- `maxLandingPadSize` is an int (1=S, 2=M, 3=L), unrelated to Spansh's `has_large_pad` boolean.
- Rows with `commodityName: null` are settlements with no market.

**Spansh is deliberately not used.** Its `marketplace` filter cannot combine a commodity with a
minimum supply (both clause shapes return zero rows), it has no working recency filter (timestamp
ranges are silently discarded), and it returns each station's entire market — ~100 KB per station.

## Ranking

Order: age band, round-trip jumps, arrival-distance band (Ls), landing pad (bigger wins), price.

**Supply and raw distance deliberately do not rank.** Supply only has to be enough - a market with
400,000 t is not better than one with 500 t once both fill the hold. Distance is spent as jumps, so
two systems the same number of jumps away are equally far.

Age and arrival distance are ranked in **bands**, not raw. A timestamp is unique to the second and
`distanceToArrival` carries six decimals, so either one ranked directly becomes a total order and
every tier below it is unreachable. If you change the tiers, keep a test that proves each one can
still decide an outcome (`RankingPriorityOrder` in `tests/test_core.py`).

Band values configured in Settings are carried through `Params` into the spec at `validate()` time.
`save()` deletes a stored band that equals the code default, so tuning a default in a new release
actually reaches users who have opened the Settings tab.

## The search expands outward - do not "simplify" it back

`core.rings()` starts at one laden jump and doubles to the configured ceiling; `search_best()`
accumulates rows across rings, deduplicated by `marketId`.

Fetching the full radius in one request looks equivalent and is not. Ardent's `/nearby` stops at
1000 rows and has **no sort parameter**, so a wide request returns an arbitrary 1000 of the matches
and routinely omits the nearest markets. Measured on Ega/Palladium (v0.2.0's bug report): at 229 ly
the closest row in the returned window was 40 ly and the plugin recommended a 65 ly station; the
same search at 92 ly came back complete and contained a qualifying market at **30 ly**.

Two rules follow, both covered by tests in `Escalation`:

- Rings **accumulate**; a later truncated ring must never displace a complete earlier one.
- Expansion stops early only when the best candidate so far is in the freshest age band. A stale
  near hit must keep the search going, because age outranks jumps.

## Checks

```bash
python -m compileall -q .        # CI cannot import load.py
python tests/test_core.py        # 59 tests, no EDMC, no network
python smoke_test.py             # stubs EDMC, drives a full cycle (needs a display)
python tools/live_check.py Sol Gold --cargo 720 --laden 40   # hits the real API
python package.py                # dist/EDTradeAssist-v<version>.zip
```

Regenerate the commodity table when Frontier adds commodities:
`python tools/build_commodities.py`.

## Releasing

Repo: <https://github.com/mcjohnso/ed-trade-assist> (`main`). CI compile-checks every module and
runs the suite on each push; a `v*` tag builds the zip and publishes a GitHub Release.

```bash
# 1. bump __version__ in EDTradeAssist/load.py, add a CHANGELOG section
git commit -am "Release vX.Y.Z"
git tag vX.Y.Z && git push origin main --tags
gh run watch --exit-status && gh release view vX.Y.Z
```

The tag must match `__version__` or the zip name and the release will not line up. Pushing the tag
publishes immediately — push the branch alone first if you want to land code without releasing.

## Etiquette

Ardent publishes no rate limit; absence of one is not permission. The client self-limits to one
request a second and caches for 300 s, and a search only happens on docking or an explicit
Re-search.

Note that a search is now up to `MAX_RINGS + 1` requests rather than one, so a worst-case search
(nothing qualifies anywhere) costs about six seconds and six requests. That is the price of not
being lied to by the row cap, but it is the reason to keep `MAX_RINGS` small and to stop expanding
as soon as something fresh turns up.
