# EDTradeAssist

An EDMC plugin for repetitive point-to-point hauling: you fix one commodity and one sell system,
and the plugin picks the buy location for every run, puts the system name you need on the clipboard
at each leg, and shows the run on the in-game overlay.

The loop is: **dock → paste → fly → buy → paste → fly → sell → repeat**, with no window switching.

## Install

1. Download the release zip and extract it into EDMC's plugins folder, so you end up with
   `plugins/EDTradeAssist/load.py`:
   - Windows: `%LOCALAPPDATA%\EDMarketConnector\plugins`
   - macOS: `~/Library/Application Support/EDMarketConnector/plugins`
   - Linux: `~/.local/share/EDMarketConnector/plugins`
2. Restart EDMC.

The in-game overlay is optional and comes from a separate plugin —
[EDMCOverlay](https://github.com/inorton/EDMCOverlay) or
[EDMCModernOverlay](https://github.com/SweetJonnySauce/EDMCModernOverlay). Without one, everything
still shows in the EDMC window.

## Using it

Open **Setup** in the EDMC panel and fill in the run:

| Field | Notes |
|---|---|
| Sell system | Where you sell. **Here** fills in your current system. |
| Commodity | Any spelling — `Gold`, `gold`, `Consumer Technology`. A typo is rejected with suggestions. |
| Cargo / pad | Hold size in tonnes, and the smallest pad your ship can use. **Ship** fills the hold from your loadout. |
| Jump range | Unladen and laden, in ly. **Ship** fills unladen; laden is yours to enter — the journal never reports it. |

Press **Start**. From then on:

- Docking anywhere in the **sell system** plans the next run and copies the **buy system**.
- Docking at the **buy station** copies the **sell system** back.

Paste into the galaxy map search box each time. **Re-search** re-runs the current leg (ignoring the
cache) if you want a different answer; **Copy system** puts the current target back on the clipboard.

## What the session is earning

Once you have sold something, a line appears showing what the session has actually made:

```
Session: 1h12m - 4 runs - 18.4M cr - 15.3M cr/hr
```

This is *realised* profit, taken from the game's own sale records — not the forecast on the line above
it, which is the price gap times your hold size. Pressing **Stop** leaves the totals in the EDMC
panel.

Worth knowing what the number does and does not mean:

- **Only the commodity you configured counts.** Selling incidental side cargo does not move it, so
  the rate measures the route you set up rather than one lucky unrelated sale.
- **The clock is wall time since Start.** A break, or an hour parked at a station, dilutes the rate —
  it is a session average including all your dithering, not a throughput figure. Comparing two routes
  with it is only fair if you flew both without interruption.
- **A "run" is a completed round trip**: cargo carried home and docked. It counts arrivals, not
  tonnes, so a partial fill still counts as one.
- **`? cr/hr` for the first five minutes.** Extrapolating an hour from one sale forty seconds in gives
  a number in the billions, so it waits until the figure means something.
- **A `~` on the money** means one sale did not report what you paid for the cargo, and the price at
  the buy station was used instead.
- **Stop, and restarting EDMC, both reset it.** Your run parameters persist across a restart, so the
  panel can look like the same session continuing when the totals have gone back to zero.

## How a buy location is chosen

Hard requirements first — never a fleet carrier, at least 1.5× your hold in stock, a pad you can
land on, and data no older than the age limit. What survives is then ranked:

1. **Data age band** — 1 h, 6 h, 24 h, 3 d, 7 d
2. **Round-trip jumps** — unladen out, laden back
3. **Arrival-distance band** — 100, 500, 1000, 2500, 5000 Ls
4. **Landing pad** — a bigger pad wins; asking for M does not mean preferring M
5. **Price you pay** — cheapest, as the final tiebreak

**Supply never ranks.** Once a market holds enough to fill your hold, more of it is worth nothing —
400,000 t is no better than 500 t. It is a filter, not a score.

**Raw distance never ranks either.** Jumps are what distance actually costs, so two systems reachable
in the same number of jumps are equally far away.

Age and arrival distance are ranked in *bands* rather than raw, because a timestamp or an Ls value
with six decimal places would decide every comparison on its own and leave the tiers below it unable
to affect anything.

### How far it looks

The search **starts at one laden jump and doubles outward**, stopping as soon as it finds something
recent. The jumps-per-leg setting (default 10) is the *ceiling*, not the starting point.

This matters more than it sounds. Ardent's market endpoint stops at 1000 rows and has no sort
parameter, so a single wide request comes back as an arbitrary 1000 of the matches — and the nearest
markets are routinely not among them. Searching outward keeps the near neighbourhood complete, which
is the part the ranking cares about most. If the outermost ring does hit that cap, the panel says so.

If nothing qualifies at any radius, the plugin then considers older data as a last resort — and tells
you it did.

Fleet carriers are never used: their stock moves without warning and the carrier can jump away
between the data being uploaded and you arriving.

## Where the data comes from

[Ardent Insight](https://ardent-insight.com), which aggregates market uploads from commanders via
EDDN. Everything it reports was true when somebody last docked there and uploaded — the age shown
next to each result is how much to trust it. Jump counts are estimates from straight-line distance
and a 0.85 derate; no route is plotted.

## Settings

The prefs tab holds the knobs you set once: search radius in jumps per leg, supply headroom, maximum
data age, the ranking bands (age, arrival distance), and whether the overlay draws, where, and at
what text size — `small` by default, with `normal`, `large` and `huge` available.

## Licence

MIT. See LICENSE.
