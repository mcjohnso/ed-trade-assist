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
  `edta_ardent.py` (and `edta_spansh.py`) imports requests lazily and falls back to `urllib` so the module still works (and
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

## Spansh is the fallback, not the primary

`edta_spansh.py` is asked only by `search_with_fallback()` (`edta_session.py`), and only when Ardent
errors, finds nothing, or looks stalled — the newest row it returned, **qualifying or not**, is older
than `STALE_AFTER_H`. That last case is real: Ardent stopped ingesting for over a day on 2026-10-09
and served 28-hour-old rows with a straight face. Spansh's candidate replaces Ardent's only when its
`rank_key` is strictly better; if Spansh fails, Ardent's outcome (or error) stands untouched.

An earlier version of this file said Spansh could not do the job. Re-tested live 2026-10-10:

- Commodity plus minimum supply in one filter **works**:
  `"market": [{"name": "Gold", "supply": {"value": [min, max], "comparison": "<=>"}}]`.
- The `market_updated_at` range filter **is honoured**.
- It sorts by distance server-side, so there are no rings on this path. `search_spansh()` widens the
  *recency* window a freshness band at a time instead and stops at the first band with a usable
  market, which keeps "age outranks jumps" exact.
- It still returns each station's **entire market, ~123 KB a station**, and ignores `fields`. This is
  why it is not the primary and why `PAGE_SIZE` is 5. An empty answer is under 1 KB, so the widening
  is cheap until it hits.
- Prices are player-side: `buy_price` is what you pay (checked against a journal `MarketBuy`). The
  opposite convention from Ardent — `normalise_row()` maps them onto Ardent's row shape so
  `edta_core` never knows the source.
- Unknown system is a bare 400, the same as a malformed filter. Unknown commodity is `200` with no
  rows. Commodities go by display name; all but three of ours (two rares and Limpets) match exactly.
- A list of commodities is asked **one commodity per request** (`search_spansh` loops, the market
  filter takes one). Each is filtered at `spansh_min_supply()` - the 1/N share of the requirement
  that at least one commodity must reach - and `normalise_rows()` then reads every listed commodity
  out of each station's market, so the combined total is exact. Discovery is not: a market that only
  qualifies on the combination can sit outside every commodity's nearest five.
- Carriers are excluded with a station-type **allowlist** (the filter has no negation), so a station
  type Frontier adds later is invisible to the fallback until it is added to `STATION_TYPES`.
- Its data runs up to about an hour behind the game, so the one-hour band often comes back empty.

## The session ledger uses two clocks, deliberately

`Ledger` (`edta_session.py`) accumulates realised profit for one Start-to-Stop session. Two different
clocks are in play and they must not be merged:

- **Bookkeeping uses the journal entry's own `timestamp`**, parsed with `core.parse_updated_at`. It is
  used only to reject replays: EDMC re-reads the tail of the current journal when it loads, so a Start
  pressed while that is draining would otherwise bank a sale from forty minutes ago. `REPLAY_SLACK`
  is the tolerance.
- **The elapsed display uses wall clock**, injected as `now`. It has to. The journal falls silent
  while docked — that is why `_schedule_heartbeat` exists at all — so a journal-clocked stopwatch
  would freeze exactly when the player is sitting looking at the number.

They never meet in one subtraction: `started_at` comes from the button press, so elapsed is wall−wall,
and the journal clock is only ever *compared* against it. `start()`, `stop()` and `display_lines()`
all take `now=None` defaulting to `datetime.now(timezone.utc)`, the same seam as `rank_candidates` and
`search_best`. `load.py` passes nothing and holds no clock of its own.

**Profit is realised, never derived.** It comes from `MarketSell`, and the cost basis is the game's own
`AvgPricePaid` — which handles a hold bought at several prices and correctly subtracts a cost incurred
before the session started. Do not compute it from `Cargo` deltas times `sell_price`: that is exactly
the estimate `_refresh_sell_price` refuses to make. Do not feed `MarketSell.SellPrice` back into
`Session.sell_price` either; that field is documented as coming from Market.json and drives the
*forecast* line, whereas a sale price is per-transaction and can reflect demand saturation.

**The cost-basis fallback is seeded at the turnaround, and it has to be.** `_on_docked` sets
`self.target = None` when you dock in the sell system, and docking strictly precedes selling — so a
basis read from `self.target` inside `_on_market_sell` would always find `None`. The TO_SELL flip is
the last moment it is alive. `MarketBuy` overrides it when seen.

`ledger.runs` increments **only when `state == TO_SELL`** on docking home. Any other state means we
were not carrying cargo back: the first Start, or a re-dock while still shopping.

**Nothing draws on the in-game overlay unless a run is in progress.** `redraw()` clears rather than
shows when the colour role is `"off"`, and that is the only thing keeping the stop summary out of the
game. The summary rides on `session.message`, and redraw's `if not lines and state.message` fallback
feeds the panel *and* the overlay — so a single `clear()` at Stop does not settle it: `on_journal`
returns a `REDRAW` for every event while stopped, and each one would re-post the summary for its full
8 s TTL. `smoke_test.py` fires three events after Stop and asserts the overlay stays blank.

## Ranking

Order: age band, round-trip jumps, orbital before surface, arrival-distance band (Ls), list order,
price.

**Round-trip time outranks margin.** That is the owner's stated priority (2026-10-10): the tiers
that cost time - jumps, orbital/surface, arrival distance - come before anything about which
commodity or what price. Do not move list order or price above them.

**The landing pad is a filter and does not rank.** v0.3.0 made a bigger pad win; the owner reversed
that on 2026-10-10: configured for M, an M pad is all that matters, and an L-pad station must not
beat an M-pad one. Do not reintroduce it as "strictly easier to land on".

Orbital-before-surface is a **rank, not a filter**, and it is decided by `SURFACE_STATION_TYPES` in
`edta_core.py`, which holds both upstreams' spellings (`CraterPort` / `Planetary Port`). A type
missing from it counts as orbital.

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

## A list of commodities is one market, judged on its combined stock

`SearchSpec.commodity_ardent` / `commodity_display` are the **first-listed** commodity and `also`
holds the rest; read `spec.commodities` for all of them and `spec.label` for display. With `also`
empty every code path is the single-commodity one it always was - same requests, same lines - and
`MultiCommodity.test_a_single_commodity_run_is_described_exactly_as_before` pins that.

- Upstream rows are per commodity, so `core.candidates_from_rows()` merges a market's rows into one
  `Candidate` whose `stock` is the combined figure. That is all the supply filter needed.
- **List order is fill order** (`core.fill_plan`), and it is deliberately not re-sorted by price:
  cheap is not profitable across different commodities and sell prices are unknown at search time.
- List order ranks markets **only as a tiebreak** (`core.list_preference`): below every tier that
  costs time, above price. It is above price because `Candidate.buy_price` is the tonne-weighted
  cost of the planned hold, which compares unlike things across commodities - without the list tier
  the price tier simply picked whichever market sold the cheapest commodity listed (seen live: a
  hold of Computer Components at 440 cr/t beating Superconductors, the first choice).
- With several commodities Ardent is asked with `minVolume=1` (`load.py::_fetch_for`), because any
  lot may be the one that tips a market over. Measured near Sol: under 3% of rows hold less than
  100 t, so a per-commodity floor would not relieve the row cap anyway - and a floor would silently
  under-count the total.
- The pool in `search_best()` is keyed by market **and** commodity. Keyed by market alone, the
  second commodity's row would overwrite the first.
- The ledger keeps a cost basis per commodity (`basis_by_commodity`), seeded from `target.lots` at
  the turnaround for the same reason the single one is.

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
python tests/test_core.py        # 153 tests, no EDMC, no network
python smoke_test.py             # stubs EDMC, drives a full cycle (needs a display)
python tools/live_check.py Sol Gold --cargo 720 --laden 40   # hits the real API
python tools/live_check.py Sol Gold --source spansh          # the fallback on its own
python tools/live_check.py Sol "Superconductors, Semiconductors" --cargo 720   # a list
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

Note that a search is now up to `MAX_RINGS + 1` requests **per listed commodity** rather than one,
so a worst-case single-commodity search (nothing qualifies anywhere) costs about six seconds and six
requests, and a five-commodity list five times that. `MAX_COMMODITIES` exists for this reason. In
the bubble a list usually stops at the third ring, where the row cap bites. That is the price of not
being lied to by the row cap, but it is the reason to keep `MAX_RINGS` small and to stop expanding
as soon as something fresh turns up.

The Spansh fallback adds up to one request per commodity per freshness band (six bands by default)
on top of that, under
the same one-a-second limit and 300 s cache, and only when Ardent is failing or stalled. Spansh's
cost is bytes rather than requests: do not raise `PAGE_SIZE`.
