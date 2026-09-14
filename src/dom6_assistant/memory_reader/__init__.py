"""Memory-based live game state reader.

On Linux: reads /proc/<pid>/mem.  Windows is not a supported target.

Usage:
    from dom6_assistant.memory_reader import attach

    reader = attach()          # finds dom6, raises if not running
    addrs  = reader.scan_int32(42)          # scan for value 42
    addrs  = reader.narrow(addrs, 43)       # keep addresses now holding 43
    print(reader.hexdump(addrs[0]))
"""

from __future__ import annotations

from typing import Optional

import psutil

from dom6_assistant.memory_reader._linux import LinuxMemoryReader, MemoryRegion

# Preferred names first — we want the actual binary, not the shell launcher.
# psutil reports the process 'name' as the executable basename.
_DOM6_NAMES_PREFERRED = ["dom6_amd64", "dom6"]
_DOM6_NAMES_FALLBACK = ["dom6.sh"]


def find_process() -> Optional[psutil.Process]:
    """Return the dom6 process if it is running, else None.

    Prefers the actual game binary (dom6_amd64) over shell launcher scripts.
    """
    candidates: dict[str, psutil.Process] = {}
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            name = proc.info["name"]
            if name in _DOM6_NAMES_PREFERRED or name in _DOM6_NAMES_FALLBACK:
                candidates[name] = proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    for preferred in _DOM6_NAMES_PREFERRED:
        if preferred in candidates:
            return candidates[preferred]
    for fallback in _DOM6_NAMES_FALLBACK:
        if fallback in candidates:
            return candidates[fallback]
    return None


def attach() -> LinuxMemoryReader:
    """Return a reader attached to the running dom6 process.

    Raises:
        RuntimeError: if dom6 is not currently running.
    """
    proc = find_process()
    if proc is None:
        raise RuntimeError(
            "Dominions 6 is not running. Start the game then retry."
        )
    return LinuxMemoryReader(proc)


__all__ = ["attach", "find_process", "LinuxMemoryReader", "MemoryRegion"]
