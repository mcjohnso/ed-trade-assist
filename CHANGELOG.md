# Changelog

All notable changes to EDTradeAssist. Versions follow [SemVer](https://semver.org/);
`__version__` in `EDTradeAssist/load.py` is authoritative and the git tag must match it.

## [0.3.0] - 2026-09-05

### Fixed
- **The search could silently miss the nearest markets entirely.** Ardent's `/nearby` endpoint stops
  at 1000 rows and has no sort parameter, so a single wide request returns an arbitrary 1000 of the
  matches. With a wide radius the nearest markets were routinely not among them. Reported for
  Ega/Palladium at 38/27 ly: the plugin recommended a station 65 ly away (6 jumps, 1019 Ls) while a
  qualifying market sat 30 ly away (3 jumps, 6 minutes old).

  The search now **starts at one laden jump and doubles outward**, accumulating results across rings
  and stopping as soon as it finds something recent. The jumps-per-leg setting is now the ceiling
  rather than the starting radius. When the outermost ring does hit the row cap, the panel says
  "options beyond N ly may be incomplete" instead of implying an optimum.

### Changed
- **Raw distance no longer ranks — only jumps do.** Two systems the same number of jumps away are
  equally far; the distance-band tier added in 0.2.0 is gone.
- **The first age band is back to 1 hour** (from 6). A commodity being actively hauled can drain a
  market within the hour, so an hour-old reading really is less trustworthy than a ten-minute one.
- **A bigger landing pad now wins, all else equal.** Asking for a minimum of M no longer treats an
  M-pad station and an L-pad station as interchangeable.
- Ranking order is now: age band → round-trip jumps → arrival-distance band → landing pad → price.

### Notes
- Supply has never been a ranking input, only a filter, and still is not — a market with 400,000 t
  ranks no better than one with 500 t once both can fill the hold.
- Settings loses the distance-band width knob, which no longer exists.

## [0.2.0] - 2026-09-05

### Changed
- **Among recent data, the nearer system now wins.** Data age says how much to trust the supply
  figure, not how good the market is, so the first age band is now **6 hours** rather than 1 — a
  30-second-old reading and a 15-minute-old one rank equally and distance decides between them.
- **New ranking tier: distance in ly**, in 10 ly bands, immediately after round-trip jumps. Without
  it a long jump range made 20 ly and 65 ly both "one jump each way", and the farther system could
  win on arrival distance or price. Full order is now age band → round-trip jumps → distance band →
  arrival-distance band → price.
- **The overlay draws at the `small` font size** by default, selectable in Settings
  (small / normal / large / huge). If an overlay server rejects the size, it falls back to `normal`
  once rather than dropping the block.

### Fixed
- Custom ranking bands set in Settings were ignored by a run started from the panel until the
  Settings dialog was next closed — `validate()` built the spec from code defaults instead of the
  configured values.
- Settings no longer persists a band value identical to the code's default. Opening and closing the
  Settings tab once used to pin whatever the defaults were that day, so a later change to them (like
  this release's) would silently not apply.

### Added
- Settings: distance band width (ly) and overlay text size.

## [0.1.1] - 2026-09-05

### Fixed
- **The plugin failed to load in EDMC**: `ModuleNotFoundError: No module named 'difflib'`.
  EDMC ships a frozen Python whose `library.zip` carries only the stdlib modules EDMC itself uses,
  and `difflib` is not among them. Commodity "did you mean" suggestions now use an edit-distance
  function written out in `edta_commodities.py` instead.

### Added
- `FrozenRuntimeImports` in the test suite: every import in the plugin is checked against the set of
  modules the frozen runtime provides, and — when EDMC is installed on the machine running the tests
  — against its actual `library.zip`. This is the guard against repeating the above with a different
  module.

## [0.1.0] - 2026-09-05

First release.

### Added
- Session parameters in the EDMC panel: sell system, commodity, cargo capacity, unladen and laden
  jump range, minimum pad, with **Here** and **Ship** prefill buttons.
- Automatic buy-location search on docking in the sell system, and clipboard hand-off at both legs
  so the system name can be pasted straight into the galaxy map.
- Ranking by data-age band, round-trip jumps, arrival-distance band, then price paid. Hard filters:
  no fleet carriers, 1.5× cargo capacity in stock, pad size, maximum data age.
- Search radius derived from the laden jump range, widening on an empty result before it will
  consider older data — and saying which it did.
- In-game overlay via EDMCOverlay / EDMCModernOverlay, with the EDMC panel as a full fallback.
- Preferences tab for search radius, supply headroom, maximum data age, ranking bands and overlay
  position.
- Profit per tonne when the game's own `Market.json` supplies a real sell price at the sell system.

### Notes
- Market data comes from [Ardent Insight](https://ardent-insight.com). Jump counts are estimates
  from straight-line distance with a 0.85 derate; no route is plotted.
