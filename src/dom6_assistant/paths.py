"""Where Dominions 6 keeps its files, on each platform it runs on.

This was `Path.home() / ".dominions6" / "savedgames"` in half a dozen places,
which is right on Linux and macOS and wrong on Windows -- where most Dominions
players are. A wrong save root is quiet: the watcher simply never sees a turn,
and the assistant answers from nothing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def default_save_root() -> Path:
    """The directory Dominions 6 writes saved games into.

    Windows puts it under the roaming profile; Linux and macOS both use a
    dotfile directory in the home folder. ``DOM6_SAVE_ROOT`` overrides all of
    them, which is how a Steam Proton prefix or an unusual install is handled
    without a code change.
    """
    override = os.environ.get("DOM6_SAVE_ROOT")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Dominions6" / "savedgames"
    return Path.home() / ".dominions6" / "savedgames"


def default_newlords_dir() -> Path:
    """Where a created pretender is saved for the game to find."""
    return default_save_root() / "newlords"


def describe_save_root() -> str:
    """A line for the docs and for error messages, naming the platform."""
    root = default_save_root()
    if os.environ.get("DOM6_SAVE_ROOT"):
        return f"{root} (from DOM6_SAVE_ROOT)"
    system = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")
    return f"{root} ({system})"
