"""Stub-EDMC smoke test for EDTradeAssist.

EDMC's runtime modules (config, myNotebook, theme, edmcoverlay) do not exist
outside EDMC, so load.py cannot normally be imported. This fakes them, then drives
the plugin through a whole haul cycle the way EDMC would - catching import errors,
Tk layout mistakes and broken wiring without launching the game.

The upstream client is replaced with one that serves tests/fixtures/, so this
never touches the network.

Needs a display for tkinter, so run it locally:

    python smoke_test.py
"""

from __future__ import annotations

import json
import sys
import time
import types
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_DIR = ROOT / "EDTradeAssist"
sys.path.insert(0, str(PLUGIN_DIR))

# --- fake `config` -----------------------------------------------------------

config_mod = types.ModuleType("config")
config_mod.appname = "EDMarketConnector"
config_mod.appversion = "6.1.2"


class _Config:
    def __init__(self):
        self.store = {}

    def get_str(self, k, *, default=""):
        return self.store.get(k, default)

    def get_int(self, k, *, default=0):
        return int(self.store.get(k, default))

    def get_bool(self, k, *, default=False):
        return bool(self.store.get(k, default))

    def get_list(self, k, *, default=None):
        return self.store.get(k, default if default is not None else [])

    def set(self, k, v):
        self.store[k] = v

    def delete(self, k, *, suppress=False):
        self.store.pop(k, None)

    shutting_down = False


config_mod.config = _Config()
config_mod.default_journal_dir = ""
sys.modules["config"] = config_mod

# --- fake `myNotebook` -------------------------------------------------------

nb = types.ModuleType("myNotebook")
nb.Frame = tk.Frame
nb.Label = tk.Label
nb.Entry = tk.Entry
nb.Checkbutton = tk.Checkbutton
nb.Button = tk.Button
nb.Notebook = tk.Frame
sys.modules["myNotebook"] = nb

# --- fake `theme` ------------------------------------------------------------

theme_mod = types.ModuleType("theme")
theme_mod.theme = types.SimpleNamespace(update=lambda w: None)
sys.modules["theme"] = theme_mod

# --- fake `edmcoverlay` ------------------------------------------------------

overlay_mod = types.ModuleType("edmcoverlay")
SENT = []


class Overlay:
    def __init__(self, *a, **k):
        pass

    def connect(self):
        pass

    def send_message(self, msgid, text, color, x, y, ttl=4, size="normal"):
        SENT.append((msgid, text, color, size))

    def send_shape(self, *a, **k):
        pass

    def send_raw(self, msg):
        pass


overlay_mod.Overlay = Overlay
sys.modules["edmcoverlay"] = overlay_mod

# --- drive the plugin --------------------------------------------------------

import edta_ardent                                          # noqa: E402
import load                                                 # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ardent_gold_near_sol.json"
with open(FIXTURE, "r", encoding="utf-8") as handle:
    ROWS = json.load(handle)


class FakeClient:
    """Serves the fixture instead of Ardent. Records every call so the smoke test
    can assert that carriers are never requested."""

    def __init__(self):
        self.calls = []

    def nearby_exports(self, system, commodity, max_distance, min_volume,
                       max_days_ago=None, use_cache=True):
        self.calls.append((system, commodity, max_distance, min_volume, max_days_ago))
        return edta_ardent.SearchResult(edta_ardent.OK, rows=ROWS)

    def clear_cache(self):
        pass

    def close(self):
        pass


def pump(root, predicate, timeout=10.0):
    """Spin the Tk event loop until `predicate` holds. The worker thread wakes the
    UI with event_generate, which only fires while events are being processed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    root = tk.Tk()
    root.withdraw()

    name = load.plugin_start3(str(PLUGIN_DIR))
    print("plugin_start3 ->", name)

    frame = load.plugin_app(root)
    print("plugin_app OK")

    prefs = load.plugin_prefs(root, "CMDR Test", False)
    load.prefs_changed("CMDR Test", False)
    print("plugin_prefs / prefs_changed OK")
    assert prefs is not None

    plugin = load.this
    plugin.client = FakeClient()

    # The commander is sitting in Sol with a 200 t hold.
    load.journal_entry("CMDR Test", False, "Sol", "Abraham Lincoln",
                       {"event": "Location", "StarSystem": "Sol", "Docked": True,
                        "StationName": "Abraham Lincoln", "MarketID": 128016384,
                        "StarPos": [0.0, 0.0, 0.0]},
                       {"CargoCapacity": 200, "Cargo": {}})
    load.journal_entry("CMDR Test", False, "Sol", "Abraham Lincoln",
                       {"event": "Loadout", "MaxJumpRange": 48.5}, {"CargoCapacity": 200})

    # Fill the panel the way the prefill buttons would, then Start.
    load._fill_here()
    load._fill_ship()
    assert plugin.vars["sell_system"].get() == "Sol", plugin.vars["sell_system"].get()
    assert plugin.vars["cargo"].get() == "200", plugin.vars["cargo"].get()
    assert plugin.vars["unladen"].get() == "48.5", plugin.vars["unladen"].get()
    plugin.vars["commodity"].set("Gold")
    plugin.vars["laden"].set("40")
    plugin.vars["pad"].set("M")
    print("prefill buttons OK")

    # A bad commodity must fail loudly rather than searching for nothing.
    plugin.vars["commodity"].set("Galde")
    load._toggle_run()
    assert plugin.session.state == "error", plugin.session.state
    assert "Gold" in plugin.session.message, plugin.session.message
    print("bad commodity rejected with suggestions OK")

    plugin.vars["commodity"].set("Gold")
    load._toggle_run()
    assert pump(root, lambda: plugin.session.state == "to_buy"), (
        "search never completed; state={}".format(plugin.session.state))
    target = plugin.session.target
    print("search OK -> {} in {} ({:,} t at {:,} cr)".format(
        target.station, target.system, target.stock, target.buy_price))

    assert plugin.client.calls, "no search was issued"
    system, commodity, radius, min_volume, max_days = plugin.client.calls[0]
    assert (system, commodity) == ("Sol", "gold"), plugin.client.calls[0]
    assert min_volume == 300, min_volume            # 200 t hold x 1.5 headroom
    assert radius <= edta_ardent.MAX_DISTANCE_LY, radius

    assert target.station_type != "FleetCarrier"
    assert target.pad >= 2, target.pad
    assert target.stock >= min_volume, target.stock

    assert root.clipboard_get() == target.system, root.clipboard_get()
    print("clipboard holds the buy system OK")

    assert SENT and SENT[-1][1].startswith("BUY Gold @"), SENT[-1]
    assert "paste into the galaxy map" in SENT[-1][1]
    assert SENT[-1][3] == "small", SENT[-1]
    print("overlay shows the buy block at size={} OK".format(SENT[-1][3]))

    # Dock at the buy station: the clipboard must flip to the sell system.
    load.journal_entry("CMDR Test", False, target.system, target.station,
                       {"event": "Docked", "StarSystem": target.system,
                        "StationName": target.station, "MarketID": target.market_id},
                       {"CargoCapacity": 200, "Cargo": {"gold": 200}})
    assert plugin.session.state == "to_sell", plugin.session.state
    assert root.clipboard_get() == "Sol", root.clipboard_get()
    assert SENT[-1][1].startswith("SELL Gold @ Sol"), SENT[-1]
    print("docked at buy station -> clipboard flipped to Sol OK")

    # Dock back in the sell system: the next search starts on its own.
    before = len(plugin.client.calls)
    load.journal_entry("CMDR Test", False, "Sol", "Abraham Lincoln",
                       {"event": "Docked", "StarSystem": "Sol",
                        "StationName": "Abraham Lincoln", "MarketID": 128016384},
                       {"CargoCapacity": 200, "Cargo": {}})
    assert pump(root, lambda: plugin.session.state == "to_buy"
                and len(plugin.client.calls) > before)
    print("docked in the sell system -> next run planned OK ({} searches)".format(
        len(plugin.client.calls)))

    # Manual actions.
    load._copy_again()
    assert root.clipboard_get() == plugin.session.target.system
    load._toggle_setup()
    load._toggle_setup()
    load._research()
    assert pump(root, lambda: plugin.session.state == "to_buy")
    print("copy / setup toggle / re-search OK")

    load._toggle_run()
    assert plugin.session.state == "off"
    print("stop OK")

    load.plugin_stop()
    print("plugin_stop OK")

    root.destroy()
    print("SMOKE TEST: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
