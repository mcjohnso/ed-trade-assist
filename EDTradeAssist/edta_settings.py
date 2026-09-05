"""Preferences tab for EDTradeAssist.

The per-run parameters (sell system, commodity, hold, jump ranges, pad) live in
the main EDMC panel, because they change every session. This tab holds only the
knobs you set once: how far to search, how much supply headroom to insist on, how
old is too old, and where the overlay sits.

Layout is GRID ONLY - EDMC's nb.Frame already manages its children with grid, and
mixing pack() anywhere inside raises TclError.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

import myNotebook as nb
from config import config

import edta_core as core
import edta_overlay

logger = logging.getLogger(__name__)

# EDMC's themed entry is `EntryMenu` in current versions and `Entry` in older ones.
_Entry = getattr(nb, "Entry", None) or getattr(nb, "EntryMenu", None) or tk.Entry

# --- config keys (the store is shared across every installed plugin) ---------

CFG_OVERLAY = "edta_overlay_enabled"
CFG_OVERLAY_X = "edta_overlay_x"
CFG_OVERLAY_Y = "edta_overlay_y"
CFG_OVERLAY_SIZE = "edta_overlay_size"
CFG_JUMPS_PER_LEG = "edta_jumps_per_leg"
CFG_SUPPLY_MARGIN = "edta_supply_margin"
CFG_MAX_AGE_DAYS = "edta_max_age_days"
CFG_FRESHNESS_EDGES = "edta_freshness_edges"
CFG_LS_EDGES = "edta_ls_edges"

DEFAULT_JUMPS_PER_LEG = 10
DEFAULT_SUPPLY_MARGIN = 1.5
DEFAULT_MAX_AGE_DAYS = 7

_vars: Dict[str, Any] = {}


# --- typed config access -----------------------------------------------------

def _get_float(key: str, fallback: float) -> float:
    """config has no float accessor, so floats are stored as strings."""
    raw = config.get_str(key, default="")
    if not raw:
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _parse_edges(raw: str, fallback: Tuple[float, ...]) -> Tuple[float, ...]:
    try:
        values = tuple(sorted(float(part) for part in raw.split(",") if part.strip()))
    except ValueError:
        return fallback
    return values if values else fallback


def _format_edges(edges: Tuple[float, ...]) -> str:
    return ", ".join(("{:g}".format(edge) for edge in edges))


def overlay_enabled() -> bool:
    return config.get_bool(CFG_OVERLAY, default=True)


def overlay_position() -> Tuple[int, int]:
    return (config.get_int(CFG_OVERLAY_X, default=0) or 20,
            config.get_int(CFG_OVERLAY_Y, default=0) or 40)


def overlay_size() -> str:
    size = config.get_str(CFG_OVERLAY_SIZE, default="")
    return size if size in edta_overlay.SIZES else edta_overlay.DEFAULT_SIZE


def tuning() -> Dict[str, Any]:
    """The knobs, as the Session/SearchSpec want them."""
    return {
        "jumps_per_leg": config.get_int(CFG_JUMPS_PER_LEG, default=0) or DEFAULT_JUMPS_PER_LEG,
        "supply_margin": _get_float(CFG_SUPPLY_MARGIN, DEFAULT_SUPPLY_MARGIN),
        "max_age_days": config.get_int(CFG_MAX_AGE_DAYS, default=0) or DEFAULT_MAX_AGE_DAYS,
        "freshness_edges_h": _parse_edges(
            config.get_str(CFG_FRESHNESS_EDGES, default=""), core.FRESHNESS_EDGES_H),
        "ls_edges": _parse_edges(config.get_str(CFG_LS_EDGES, default=""), core.LS_EDGES),
    }


# --- the tab -----------------------------------------------------------------

def build(parent, cmdr: Optional[str], is_beta: bool):
    frame = nb.Frame(parent)
    frame.columnconfigure(1, weight=1)
    current = tuning()
    row = 0

    def label(text: str, r: int, column: int = 0, **kw):
        nb.Label(frame, text=text).grid(row=r, column=column, padx=8, pady=2,
                                        sticky=tk.W, **kw)

    def entry(key: str, value: str, r: int, width: int = 8):
        var = tk.StringVar(master=frame, value=value)
        _vars[key] = var
        _Entry(frame, textvariable=var, width=width).grid(
            row=r, column=1, padx=8, pady=2, sticky=tk.W)
        return var

    nb.Label(frame, text="Search").grid(row=row, column=0, padx=8, pady=(8, 2), sticky=tk.W)
    row += 1

    label("Maximum search radius (jumps per leg)", row)
    entry(CFG_JUMPS_PER_LEG, str(current["jumps_per_leg"]), row)
    row += 1
    label("Supply headroom (x cargo capacity)", row)
    entry(CFG_SUPPLY_MARGIN, "{:g}".format(current["supply_margin"]), row)
    row += 1
    label("Ignore market data older than (days)", row)
    entry(CFG_MAX_AGE_DAYS, str(current["max_age_days"]), row)
    row += 1

    nb.Label(
        frame,
        text=("The search starts at one laden jump and doubles outward, stopping "
              "as soon as it finds something recent. This setting is the furthest "
              "it will ever look: jumps-per-leg x laden jump range, capped at "
              "500 ly (Ardent's limit). Fleet carriers are never used as a buy "
              "location."),
        wraplength=380, justify=tk.LEFT,
    ).grid(row=row, column=0, columnspan=2, padx=8, pady=(2, 8), sticky=tk.W)
    row += 1

    nb.Label(frame, text="Ranking buckets").grid(row=row, column=0, padx=8, pady=(8, 2),
                                                 sticky=tk.W)
    row += 1
    label("Data age bands (hours)", row)
    entry(CFG_FRESHNESS_EDGES, _format_edges(current["freshness_edges_h"]), row, width=20)
    row += 1
    label("Arrival distance bands (Ls)", row)
    entry(CFG_LS_EDGES, _format_edges(current["ls_edges"]), row, width=20)
    row += 1

    nb.Label(
        frame,
        text=("Candidates are ranked by age band, then round-trip jumps, then "
              "arrival-distance band, then landing pad (bigger wins), then "
              "price.\n\n"
              "Supply and raw distance never rank: supply only has to be enough, "
              "and jumps are what distance actually costs. Bands exist so the "
              "lower priorities are reachable at all - a raw timestamp or Ls "
              "value is unique enough to decide every comparison by itself."),
        wraplength=380, justify=tk.LEFT,
    ).grid(row=row, column=0, columnspan=2, padx=8, pady=(2, 8), sticky=tk.W)
    row += 1

    nb.Label(frame, text="Overlay").grid(row=row, column=0, padx=8, pady=(8, 2), sticky=tk.W)
    row += 1

    overlay_var = tk.BooleanVar(master=frame, value=overlay_enabled())
    _vars[CFG_OVERLAY] = overlay_var
    nb.Checkbutton(frame, text="Show the run on the in-game overlay",
                   variable=overlay_var).grid(row=row, column=0, columnspan=2,
                                              padx=8, pady=2, sticky=tk.W)
    row += 1

    pos_x, pos_y = overlay_position()
    label("Overlay position (x, y)", row)
    holder = nb.Frame(frame)
    holder.grid(row=row, column=1, padx=8, pady=2, sticky=tk.W)
    x_var = tk.StringVar(master=frame, value=str(pos_x))
    y_var = tk.StringVar(master=frame, value=str(pos_y))
    _vars[CFG_OVERLAY_X] = x_var
    _vars[CFG_OVERLAY_Y] = y_var
    _Entry(holder, textvariable=x_var, width=5).grid(row=0, column=0)
    _Entry(holder, textvariable=y_var, width=5).grid(row=0, column=1, padx=(4, 0))
    row += 1

    label("Overlay text size", row)
    size_var = tk.StringVar(master=frame, value=overlay_size())
    _vars[CFG_OVERLAY_SIZE] = size_var
    ttk.Combobox(frame, textvariable=size_var, values=list(edta_overlay.SIZES),
                 state="readonly", width=8).grid(row=row, column=1, padx=8, pady=2,
                                                 sticky=tk.W)
    row += 1

    nb.Label(
        frame,
        text=("The overlay needs EDMCOverlay or EDMCModernOverlay installed "
              "separately. Without it everything still shows in the EDMC window."),
        wraplength=380, justify=tk.LEFT,
    ).grid(row=row, column=0, columnspan=2, padx=8, pady=(2, 8), sticky=tk.W)

    return frame


def save() -> List[str]:
    """Persist the tab. Returns messages about anything that was rejected."""
    problems: List[str] = []

    def read(key: str) -> str:
        var = _vars.get(key)
        if var is None:
            return ""
        try:
            return str(var.get()).strip()
        except tk.TclError:
            return ""

    def save_int(key: str, label: str, minimum: int, maximum: int, default: int) -> None:
        raw = read(key)
        if not raw:
            return
        try:
            value = int(float(raw))
        except ValueError:
            problems.append("{} must be a number; kept the previous value.".format(label))
            return
        if not minimum <= value <= maximum:
            problems.append("{} must be between {} and {}.".format(label, minimum, maximum))
            return
        config.set(key, value)

    save_int(CFG_JUMPS_PER_LEG, "Maximum search radius (jumps per leg)", 1, 60,
             DEFAULT_JUMPS_PER_LEG)
    save_int(CFG_MAX_AGE_DAYS, "Maximum data age", 1, 365, DEFAULT_MAX_AGE_DAYS)
    save_int(CFG_OVERLAY_X, "Overlay x", 0, 4000, 20)
    save_int(CFG_OVERLAY_Y, "Overlay y", 0, 4000, 40)

    margin_raw = read(CFG_SUPPLY_MARGIN)
    if margin_raw:
        try:
            margin = float(margin_raw)
            if margin < 1.0:
                problems.append("Supply headroom below 1.0 would accept markets that "
                                "cannot fill the hold; kept the previous value.")
            else:
                config.set(CFG_SUPPLY_MARGIN, str(margin))
        except ValueError:
            problems.append("Supply headroom must be a number.")

    for key, fallback, label in ((CFG_FRESHNESS_EDGES, core.FRESHNESS_EDGES_H, "Data age bands"),
                                 (CFG_LS_EDGES, core.LS_EDGES, "Arrival distance bands")):
        raw = read(key)
        if not raw:
            continue
        edges = _parse_edges(raw, ())
        if not edges:
            problems.append("{} must be a comma-separated list of numbers.".format(label))
            continue
        if tuple(edges) == tuple(fallback):
            # _forget_defaults: storing a value identical to the code's default
            # would freeze it. The tab prefills from the defaults, so merely
            # opening and closing Settings once would otherwise pin whatever the
            # bands happened to be that day, and a later tuning of them in a new
            # release would silently not apply.
            config.delete(key)
            continue
        config.set(key, _format_edges(edges))

    size_var = _vars.get(CFG_OVERLAY_SIZE)
    if size_var is not None:
        try:
            size = str(size_var.get()).strip()
        except tk.TclError:
            size = ""
        if size in edta_overlay.SIZES:
            config.set(CFG_OVERLAY_SIZE, size)

    overlay_var = _vars.get(CFG_OVERLAY)
    if overlay_var is not None:
        try:
            config.set(CFG_OVERLAY, bool(overlay_var.get()))
        except tk.TclError:
            pass

    return problems
