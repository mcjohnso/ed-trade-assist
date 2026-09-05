# ed-trade-assist

Source repo for **EDTradeAssist**, an [EDMC](https://github.com/EDCD/EDMarketConnector) plugin that
runs the loop for targeted commodity hauling: one commodity, one sell system, and a freshly chosen
buy location every run, with the system name you need on the clipboard at each leg.

The shippable plugin is [`EDTradeAssist/`](EDTradeAssist/) — see
[its README](EDTradeAssist/README.md) for installation and use.

## Layout

```
EDTradeAssist/            the plugin (this is what gets zipped)
  load.py                 EDMC callbacks, the panel, the worker thread
  edta_session.py         state machine + search escalation
  edta_core.py            pure: ranking, buckets, jump maths, formatting
  edta_ardent.py          Ardent Insight client
  edta_commodities.py     name resolution (+ generated edta_commodities.json)
  edta_overlay.py         EDMCOverlay / EDMCModernOverlay output
  edta_settings.py        preferences tab
package.py                builds dist/EDTradeAssist-v<version>.zip
smoke_test.py             stubs EDMC and drives a whole haul cycle
tests/test_core.py        pure-logic tests over a real Ardent capture
tools/build_commodities.py  regenerates the commodity table from EDCD/FDevIDs
```

Every helper is `edta_`-prefixed and imported top-level. EDMC puts every installed plugin's folder
on one shared `sys.path`, so a generic `overlay.py` or `utils.py` would silently lose to whichever
plugin imported first. Relative imports do not work either — EDMC imports `load.py` directly, not as
a package.

`edta_core.py` and `edta_session.py` import nothing from EDMC, tkinter or the network, which is what
lets the test suite drive a whole dock-buy-sell cycle in-process.

## Developing

```bash
python -m compileall -q .        # CI cannot import load.py; this catches syntax errors
python tests/test_core.py        # pure logic, no EDMC, no network
python smoke_test.py             # stubs EDMC, drives the plugin (needs a display)
python package.py                # dist/EDTradeAssist-v<version>.zip
```

To test against the live API rather than the fixture:

```bash
python tools/live_check.py "Sol" "Gold" --cargo 720 --laden 40
```

`__version__` in `EDTradeAssist/load.py` is the single source of truth for the version, and the git
tag must match it (`v1.2.0` ↔ `"1.2.0"`); `release.yml` builds and publishes the zip on a `v*` tag.

## Data source

[Ardent Insight](https://ardent-insight.com) — one GET per search returns supply, price, pad size,
coordinates and a timestamp. Spansh is deliberately not used: its station search cannot combine a
commodity with a minimum supply in one filter, it has no working recency filter, and it returns each
station's entire market. See the notes at the top of `edta_ardent.py`.
