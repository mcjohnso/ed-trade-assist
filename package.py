#!/usr/bin/env python3
"""Build a distributable zip of an EDMC plugin.

Drop this at the root of a plugin repo whose plugin lives in its own subfolder
(the folder containing ``load.py``). It auto-detects that folder, reads its
``__version__``/``VERSION``, and writes::

    dist/<PluginFolder>-v<version>.zip

with the plugin folder as the top-level entry, so users extract it straight
into EDMC's ``plugins/`` directory. Python caches are excluded.

Usage:
    python package.py
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"

_EXCLUDE_DIRS = {"__pycache__", ".git", ".github"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def find_plugin_dir() -> Path:
    """The single subfolder of ROOT that contains a load.py."""
    candidates = sorted({p.parent for p in ROOT.glob("*/load.py")})
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        sys.exit("No <subfolder>/load.py found — this script expects the plugin in its own subfolder.")
    sys.exit(f"Multiple plugin folders found ({[c.name for c in candidates]}); leave only one.")


def read_version(load_py: Path) -> str:
    text = load_py.read_text(encoding="utf-8")
    match = re.search(r'^(?:__version__|VERSION)\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        sys.exit(f"No __version__ / VERSION string found in {load_py}")
    return match.group(1)


def build() -> Path:
    plugin_dir = find_plugin_dir()
    version = read_version(plugin_dir / "load.py")
    DIST_DIR.mkdir(exist_ok=True)
    out_path = DIST_DIR / f"{plugin_dir.name}-v{version}.zip"
    if out_path.exists():
        out_path.unlink()

    file_count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(plugin_dir.rglob("*")):
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            if path.suffix in _EXCLUDE_SUFFIXES:
                continue
            if path.is_file():
                # arcname like "<PluginFolder>/load.py"
                zf.write(path, path.relative_to(ROOT).as_posix())
                file_count += 1

    print(f"Built {out_path.relative_to(ROOT)}  ({file_count} files, v{version})")
    return out_path


if __name__ == "__main__":
    build()
