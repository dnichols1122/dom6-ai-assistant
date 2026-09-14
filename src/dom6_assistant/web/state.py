"""Module-level server state for the single-user web session.

This is intentionally simple — dom6-assistant is a local single-user tool.
A threading.Lock guards writes since slow memory scans run in thread-pool threads.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()

# Scan state — updated after each scan/narrow
scan: dict = {
    "addresses": [],   # full list of int addresses
    "val_type": "int32",
    "value": None,
}

# Locked game state address — set once the user identifies their gold address
game_state_address: int | None = None

# Scan progress — updated during a running scan
progress: dict = {
    "running": False,
    "scanned_bytes": 0,
    "total_bytes": 0,
}


def set_game_state_address(addr: int) -> None:
    with _lock:
        global game_state_address
        game_state_address = addr


def get_game_state_address() -> int | None:
    with _lock:
        return game_state_address


def update_scan(addresses: list[int], val_type: str, value: int) -> None:
    with _lock:
        scan["addresses"] = addresses
        scan["val_type"] = val_type
        scan["value"] = value


def get_scan() -> dict:
    with _lock:
        return dict(scan)


def set_progress(running: bool, scanned: int = 0, total: int = 0) -> None:
    with _lock:
        progress["running"] = running
        progress["scanned_bytes"] = scanned
        progress["total_bytes"] = total


def update_progress(scanned: int, total: int) -> None:
    with _lock:
        progress["scanned_bytes"] = scanned
        progress["total_bytes"] = total


def get_progress() -> dict:
    with _lock:
        return dict(progress)
