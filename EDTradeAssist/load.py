"""EDTradeAssist - a targeted commodity-hauling assistant for EDMC.

You fix a commodity and a sell system; the plugin picks the buy location for each
run, puts the system name you need on the clipboard at each leg, and shows the run
on the in-game overlay. Dock, paste, fly, buy, paste, fly, sell, repeat.

Market data comes from Ardent Insight (see edta_ardent.py). The ranking and the
state machine are in edta_core.py / edta_session.py, both free of EDMC and tkinter
so they can be tested on their own.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import threading
import tkinter as tk
from typing import Any, Dict, List, Optional

from config import appname, config
from theme import theme

import edta_ardent
import edta_commodities
import edta_core as core
import edta_overlay
import edta_session as session_mod
import edta_settings

__version__ = "0.4.0"
plugin_version = __version__

PLUGIN_NAME = "EDTradeAssist"
DISPLAY_NAME = "Trade Assist"

logger = logging.getLogger("{}.{}".format(appname, os.path.basename(os.path.dirname(__file__))))

SEARCH_DONE = "<<EdtaSearchDone>>"

#: Status.json goes quiet while docked, so the overlay is refreshed on a timer of
#: our own rather than waiting for the game to say something.
HEARTBEAT_MS = 3000

#: How often the Tk thread checks for a finished search. See _schedule_heartbeat.
POLL_MS = 250

# Per-run parameters. They live in the main panel but persist so a run survives a
# restart of EDMC. Prefixed because the config store is shared with every plugin.
CFG_SELL_SYSTEM = "edta_sell_system"
CFG_COMMODITY = "edta_commodity"
CFG_CARGO = "edta_cargo_capacity"
CFG_UNLADEN = "edta_unladen_range"
CFG_LADEN = "edta_laden_range"
CFG_MIN_PAD = "edta_min_pad"

PAD_CHOICES = ("L", "M", "S")

_TK_COLORS = {"buy": "#e08000", "sell": "#207a20", "warn": "#c00000", "off": None}


class Plugin:
    def __init__(self) -> None:
        self.session = session_mod.Session()
        self.client = edta_ardent.ArdentClient(
            user_agent="{}/{} (+https://github.com/mcjohnso/ed-trade-assist)".format(
                PLUGIN_NAME, __version__))
        self.overlay = edta_overlay.OverlayManager()
        self.frame: Optional[tk.Frame] = None
        self.tasks: "queue.Queue" = queue.Queue()
        self.results: "queue.Queue" = queue.Queue()
        self.worker: Optional[threading.Thread] = None

        # Live facts from the journal, used by the prefill buttons.
        self.current_system = ""
        self.cargo_capacity = 0
        self.max_jump_range = 0.0

        # Widgets, wired up in plugin_app().
        self.vars: Dict[str, tk.StringVar] = {}
        self.status_var: Optional[tk.StringVar] = None
        self.detail_var: Optional[tk.StringVar] = None
        self.detail_label: Optional[tk.Label] = None
        self.setup_frame: Optional[tk.Frame] = None
        self.setup_button: Optional[tk.Button] = None
        self.run_button: Optional[tk.Button] = None
        self.copy_button: Optional[tk.Button] = None
        self.research_button: Optional[tk.Button] = None
        self.setup_open = True
        self.default_fg: Optional[str] = None
        #: A one-off message from the plugin itself (bad preference, start-time
        #: warning) rather than from the session's own state.
        self.notice = ""

    # --- worker -------------------------------------------------------------

    def start_worker(self) -> None:
        self.worker = threading.Thread(target=self._work, name="EDTradeAssist", daemon=True)
        self.worker.start()

    def _work(self) -> None:
        while True:
            task = self.tasks.get()
            try:
                if task is None:
                    return
                generation, spec = task
                outcome = session_mod.search_best(spec, self._fetch_for(spec))
                self.results.put((generation, outcome))
                self._wake_ui()
            except Exception:
                logger.exception("search worker failed")
            finally:
                self.tasks.task_done()

    def _fetch_for(self, spec: core.SearchSpec):
        def fetch(radius: float, max_days: int):
            return self.client.nearby_exports(
                spec.sell_system, spec.commodity_ardent, radius, spec.min_volume,
                max_days_ago=max_days)
        return fetch

    def _wake_ui(self) -> None:
        """The only Tk-safe call from a worker thread."""
        frame = self.frame
        if frame is None:
            return
        try:
            if config.shutting_down:
                return
        except Exception:
            pass
        try:
            frame.event_generate(SEARCH_DONE, when="tail")
        except Exception:
            pass

    # --- effects ------------------------------------------------------------

    def perform(self, effects: List[session_mod.Effect]) -> None:
        for effect in effects or ():
            if effect.kind == session_mod.SEARCH:
                self.tasks.put(effect.payload)
            elif effect.kind == session_mod.CLIPBOARD:
                self.copy_to_clipboard(effect.payload)
            elif effect.kind == session_mod.REDRAW:
                self.redraw()

    def copy_to_clipboard(self, text: str) -> bool:
        """Tk owns the clipboard. The trailing update() is required on Windows or
        the contents vanish as soon as EDMC loses focus - which is immediately,
        since the next thing you do is alt-tab into the game."""
        widget = self.frame
        if widget is None or not text:
            return False
        try:
            widget.clipboard_clear()
            widget.clipboard_append(text)
            widget.update()
        except tk.TclError:
            logger.debug("clipboard unavailable")
            return False
        return True

    # --- rendering ----------------------------------------------------------

    def redraw(self) -> None:
        if self.status_var is None:
            return
        state = self.session
        self.status_var.set("{}{}".format(
            state.status_line(),
            "" if not state.spec else "  -  {} to {}".format(
                state.spec.commodity_display, state.spec.sell_system)))

        lines, role = state.display_lines()
        if not lines and state.message:
            lines = [state.message]
        if self.notice:
            lines = list(lines) + [self.notice]
        self.detail_var.set("\n".join(lines))
        if self.detail_label is not None:
            colour = _TK_COLORS.get(role) or self.default_fg
            try:
                self.detail_label.configure(foreground=colour)
            except tk.TclError:
                pass

        running = state.running
        if self.run_button is not None:
            self.run_button.configure(text="Stop" if running else "Start")
        for button, enabled in ((self.copy_button, bool(state.clipboard_text())),
                                (self.research_button, running)):
            if button is not None:
                button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

        if edta_settings.overlay_enabled():
            self.overlay.set_position(*edta_settings.overlay_position())
            self.overlay.set_size(edta_settings.overlay_size())
            self.overlay.set_enabled(True)
            colour = {"buy": edta_overlay.COLOR_BUY, "sell": edta_overlay.COLOR_SELL}.get(
                role, edta_overlay.COLOR_WARN)
            self.overlay.show(lines, colour)
        else:
            self.overlay.set_enabled(False)

    # --- parameters ---------------------------------------------------------

    def params_from_panel(self) -> session_mod.Params:
        knobs = edta_settings.tuning()
        return session_mod.Params(
            sell_system=self.vars["sell_system"].get(),
            commodity=self.vars["commodity"].get(),
            cargo_capacity=self.vars["cargo"].get(),
            unladen_range=self.vars["unladen"].get(),
            laden_range=self.vars["laden"].get(),
            min_pad=self.vars["pad"].get(),
            supply_margin=knobs["supply_margin"],
            jumps_per_leg=knobs["jumps_per_leg"],
            max_age_days=knobs["max_age_days"],
            freshness_edges_h=tuple(knobs["freshness_edges_h"]),
            ls_edges=tuple(knobs["ls_edges"]),
        )

    def save_params(self) -> None:
        for key, name in ((CFG_SELL_SYSTEM, "sell_system"), (CFG_COMMODITY, "commodity"),
                          (CFG_CARGO, "cargo"), (CFG_UNLADEN, "unladen"),
                          (CFG_LADEN, "laden"), (CFG_MIN_PAD, "pad")):
            config.set(key, self.vars[name].get().strip())

    def load_params(self) -> Dict[str, str]:
        return {
            "sell_system": config.get_str(CFG_SELL_SYSTEM, default=""),
            "commodity": config.get_str(CFG_COMMODITY, default=""),
            "cargo": config.get_str(CFG_CARGO, default=""),
            "unladen": config.get_str(CFG_UNLADEN, default=""),
            "laden": config.get_str(CFG_LADEN, default=""),
            "pad": config.get_str(CFG_MIN_PAD, default="") or "L",
        }

    def apply_tuning(self) -> None:
        """Push prefs-tab changes onto a run already in progress."""
        if self.session.spec is None:
            return
        knobs = edta_settings.tuning()
        self.session.spec = dataclasses.replace(
            self.session.spec,
            supply_margin=knobs["supply_margin"],
            jumps_per_leg=knobs["jumps_per_leg"],
            max_age_days=knobs["max_age_days"],
            freshness_edges_h=tuple(knobs["freshness_edges_h"]),
            ls_edges=tuple(knobs["ls_edges"]),
        )


this: Optional[Plugin] = None


# --- EDMC callbacks ----------------------------------------------------------

def plugin_start3(plugin_dir: str) -> str:
    global this
    this = Plugin()
    this.start_worker()
    logger.info("%s %s started", PLUGIN_NAME, __version__)
    return DISPLAY_NAME


def plugin_stop() -> None:
    if this is None:
        return
    this.overlay.stop()
    this.tasks.put(None)
    if this.worker is not None and this.worker.is_alive():
        this.worker.join(timeout=5)
    this.client.close()


def plugin_app(parent):
    frame = tk.Frame(parent)
    frame.columnconfigure(1, weight=1)
    this.frame = frame

    this.status_var = tk.StringVar(master=frame, value="Stopped")
    this.detail_var = tk.StringVar(master=frame, value="")

    header = tk.Label(frame, text=DISPLAY_NAME)
    header.grid(row=0, column=0, sticky=tk.W)
    tk.Label(frame, textvariable=this.status_var, anchor=tk.W).grid(
        row=0, column=1, sticky=tk.EW, padx=(6, 0))
    this.setup_button = tk.Button(frame, text="Setup", command=_toggle_setup)
    this.setup_button.grid(row=0, column=2, sticky=tk.E)
    this.run_button = tk.Button(frame, text="Start", command=_toggle_run)
    this.run_button.grid(row=0, column=3, sticky=tk.E, padx=(4, 0))

    this.setup_frame = _build_setup(frame)
    this.setup_frame.grid(row=1, column=0, columnspan=4, sticky=tk.EW, pady=(4, 0))

    this.detail_label = tk.Label(frame, textvariable=this.detail_var, anchor=tk.W,
                                 justify=tk.LEFT, font=("TkFixedFont", 8))
    this.detail_label.grid(row=2, column=0, columnspan=4, sticky=tk.EW, pady=(4, 0))

    actions = tk.Frame(frame)
    actions.grid(row=3, column=0, columnspan=4, sticky=tk.W)
    this.research_button = tk.Button(actions, text="Re-search", command=_research,
                                     state=tk.DISABLED)
    this.research_button.grid(row=0, column=0)
    this.copy_button = tk.Button(actions, text="Copy system", command=_copy_again,
                                 state=tk.DISABLED)
    this.copy_button.grid(row=0, column=1, padx=(4, 0))

    frame.bind(SEARCH_DONE, _on_search_done)

    theme.update(frame)
    this.default_fg = this.detail_label.cget("foreground")
    this.redraw()
    _schedule_heartbeat(frame)
    return frame


def _build_setup(parent) -> tk.Frame:
    setup = tk.Frame(parent)
    setup.columnconfigure(1, weight=1)
    saved = this.load_params()

    def make_var(name: str) -> tk.StringVar:
        var = tk.StringVar(master=setup, value=saved.get(name, ""))
        this.vars[name] = var
        return var

    tk.Label(setup, text="Sell system").grid(row=0, column=0, sticky=tk.W)
    system_row = tk.Frame(setup)
    system_row.grid(row=0, column=1, sticky=tk.EW)
    system_row.columnconfigure(0, weight=1)
    tk.Entry(system_row, textvariable=make_var("sell_system")).grid(row=0, column=0, sticky=tk.EW)
    tk.Button(system_row, text="Here", command=_fill_here).grid(row=0, column=1, padx=(4, 0))

    tk.Label(setup, text="Commodity").grid(row=1, column=0, sticky=tk.W)
    tk.Entry(setup, textvariable=make_var("commodity")).grid(row=1, column=1, sticky=tk.EW)

    tk.Label(setup, text="Cargo / pad").grid(row=2, column=0, sticky=tk.W)
    cargo_row = tk.Frame(setup)
    cargo_row.grid(row=2, column=1, sticky=tk.W)
    tk.Entry(cargo_row, textvariable=make_var("cargo"), width=6).grid(row=0, column=0)
    tk.Label(cargo_row, text="t   pad").grid(row=0, column=1, padx=(2, 4))
    pad_var = make_var("pad")
    if pad_var.get() not in PAD_CHOICES:
        pad_var.set("L")
    tk.OptionMenu(cargo_row, pad_var, *PAD_CHOICES).grid(row=0, column=2)

    tk.Label(setup, text="Jump range").grid(row=3, column=0, sticky=tk.W)
    jump_row = tk.Frame(setup)
    jump_row.grid(row=3, column=1, sticky=tk.W)
    tk.Label(jump_row, text="unladen").grid(row=0, column=0)
    tk.Entry(jump_row, textvariable=make_var("unladen"), width=6).grid(row=0, column=1, padx=(2, 6))
    tk.Label(jump_row, text="laden").grid(row=0, column=2)
    tk.Entry(jump_row, textvariable=make_var("laden"), width=6).grid(row=0, column=3, padx=(2, 4))
    tk.Button(jump_row, text="Ship", command=_fill_ship).grid(row=0, column=4)

    return setup


def plugin_prefs(parent, cmdr, is_beta):
    return edta_settings.build(parent, cmdr, is_beta)


def prefs_changed(cmdr, is_beta) -> None:
    if this is None:
        return
    problems = edta_settings.save()
    this.apply_tuning()
    this.notice = " ".join(problems)
    this.redraw()


def journal_entry(cmdr, is_beta, system, station, entry, state):
    if this is None:
        return None
    try:
        this.current_system = system or this.current_system
        if isinstance(state, dict):
            this.cargo_capacity = state.get("CargoCapacity") or this.cargo_capacity
        event = entry.get("event")
        if event == "Loadout" and entry.get("MaxJumpRange"):
            this.max_jump_range = float(entry["MaxJumpRange"])
        if event in ("Docked", "Market"):
            _refresh_sell_price(entry)
        this.perform(this.session.on_journal(entry, state or {}))
    except Exception:
        logger.exception("journal_entry failed for %s", (entry or {}).get("event"))
    return None


# --- panel actions -----------------------------------------------------------

def _toggle_setup() -> None:
    this.setup_open = not this.setup_open
    if this.setup_open:
        this.setup_frame.grid()
    else:
        this.setup_frame.grid_remove()


def _toggle_run() -> None:
    this.notice = ""
    if this.session.running:
        this.perform(this.session.stop())
        this.overlay.clear()
        return
    this.save_params()
    validation, effects = this.session.start(this.params_from_panel())
    if not validation.ok:
        # The session stays stopped and shows why, so the button still reads
        # "Start" and the next click retries rather than stopping.
        this.perform(effects)
        return
    this.notice = " ".join(validation.warnings)
    if validation.warnings:
        logger.info("start warnings: %s", "; ".join(validation.warnings))
    this.setup_open = False
    this.setup_frame.grid_remove()
    this.perform(effects)


def _research() -> None:
    if this.session.running:
        this.client.clear_cache()
        this.perform(this.session.begin_search())


def _copy_again() -> None:
    this.copy_to_clipboard(this.session.clipboard_text())


def _fill_here() -> None:
    if this.current_system:
        this.vars["sell_system"].set(this.current_system)


def _fill_ship() -> None:
    if this.cargo_capacity:
        this.vars["cargo"].set(str(int(this.cargo_capacity)))
    if this.max_jump_range:
        this.vars["unladen"].set("{:g}".format(round(this.max_jump_range, 2)))


def _on_search_done(_event=None) -> None:
    while True:
        try:
            generation, outcome = this.results.get_nowait()
        except queue.Empty:
            return
        this.perform(this.session.apply_outcome(generation, outcome))


def _schedule_heartbeat(widget) -> None:
    """One Tk-thread timer doing two jobs.

    It drains the worker's results queue, and periodically re-sends the overlay
    (whose messages expire on a TTL, and the game stops producing events entirely
    while you sit at a station).

    The drain is what actually guarantees a finished search reaches the UI.
    event_generate is the fast path, but it is a cross-thread Tk call and raises
    outright when the app is not running a mainloop; without this poll a result
    that failed to wake the UI would sit in the queue forever.
    """
    counter = {"ticks": 0}
    per_beat = max(1, HEARTBEAT_MS // POLL_MS)

    def beat():
        try:
            if config.shutting_down:
                return
        except Exception:
            pass
        try:
            if this is not None:
                _on_search_done()
                counter["ticks"] += 1
                if counter["ticks"] % per_beat == 0 and this.session.running:
                    this.redraw()
            widget.after(POLL_MS, beat)
        except tk.TclError:
            pass

    widget.after(POLL_MS, beat)


# --- the sell price, read from the game's own files --------------------------

def _refresh_sell_price(entry: Dict[str, Any]) -> None:
    """When docked in the sell system, take the real sell price from Market.json.

    The sell location is a system rather than a station, so nothing is queried
    upstream for it - but while you are standing in a market the game writes the
    true price to disk, and that is worth showing. Absent or stale, we simply do
    not show profit rather than estimating it.
    """
    state = this.session
    if state.spec is None:
        return
    system = (entry.get("StarSystem") or this.current_system or "")
    if system.casefold() != state.spec.sell_system.casefold():
        return
    market_id = entry.get("MarketID")
    market = _read_market_json(market_id)
    if not market:
        return
    key = state.spec.commodity_ardent
    for item in market.get("Items") or ():
        name = (item.get("Name") or "").strip()
        if _names_match(name, key):
            price = int(item.get("SellPrice") or 0)
            state.sell_price = price if price > 0 else None
            return


def _names_match(journal_name: str, ardent_name: str) -> bool:
    """Market.json spells commodities '$gold_name;'; Ardent spells them 'gold'."""
    return edta_commodities.same_commodity(journal_name, ardent_name)


def _read_market_json(market_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """Market.json is rewritten by the game on every Market event; the journal
    event itself carries no items."""
    directory = (config.get_str("journaldir", default="")
                 or getattr(config, "default_journal_dir", "")
                 or os.path.join(os.path.expanduser("~"), "Saved Games",
                                 "Frontier Developments", "Elite Dangerous"))
    path = os.path.join(directory, "Market.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:   # game files may carry a BOM
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    # Guard against a stale file left behind by the previous station.
    if market_id is not None and data.get("MarketID") not in (None, market_id):
        return None
    return data
