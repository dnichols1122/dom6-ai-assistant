"""Listen on the named pipe written by trn_hook.so and parse .trn data live.

The hook writes one message per .trn file closed:
    [4]  magic   "TRN!"
    [2]  uint16  nation name length
    [N]  bytes   nation name (no null)
    [4]  uint32  data length
    [M]  bytes   raw .trn content

Usage (blocking — waits for the next end-turn):
    python -m dom6_assistant.file_reader.trn_pipe_reader

Or import and call read_one() / read_loop() from your own code.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Callable

PIPE_PATH = Path("/tmp/dom6_trn_hook.pipe")


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------

def _read_exactly(fd: int, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            raise EOFError("pipe closed before full message received")
        buf += chunk
    return buf


def read_one() -> tuple[str, bytes]:
    """Block until one .trn message arrives on the pipe.

    Returns (nation_name, trn_bytes).
    """
    PIPE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PIPE_PATH.exists():
        os.mkfifo(PIPE_PATH)

    fd = os.open(str(PIPE_PATH), os.O_RDONLY)   # blocks until writer opens
    try:
        magic = _read_exactly(fd, 4)
        if magic != b"TRN!":
            raise ValueError(f"Bad magic: {magic!r}")
        nlen,  = struct.unpack("<H", _read_exactly(fd, 2))
        nation = _read_exactly(fd, nlen).decode()
        dlen,  = struct.unpack("<I", _read_exactly(fd, 4))
        data   = _read_exactly(fd, dlen)
        return nation, data
    finally:
        os.close(fd)


def read_loop(callback: Callable[[str, bytes], None]) -> None:
    """Call *callback(nation, trn_bytes)* for every end-turn, forever."""
    while True:
        nation, data = read_one()
        callback(nation, data)


# ---------------------------------------------------------------------------
# Default callback — parse and print known fields
# ---------------------------------------------------------------------------

def _default_callback(nation: str, data: bytes) -> None:
    from dom6_assistant.file_reader.formats.trn import parse_bytes   # lazy import
    print(f"\n=== end-turn received: {nation} ({len(data):,} bytes) ===")
    try:
        state = parse_bytes(data, nation_name=nation)
        print(f"  turn:      {state.turn}")
        g = state.player_gems
        if g:
            labels = ["fire", "air", "water", "earth", "astral",
                      "death", "nature", "glamour", "blood"]
            vals   = [g.fire, g.air, g.water, g.earth, g.astral,
                      g.death, g.nature, g.glamour, g.blood]
            gem_str = "  gems:      " + "  ".join(
                f"{label}={value}"
                for label, value in zip(labels, vals)
                if value
            )
            print(gem_str or "  gems:      (all zero)")
        else:
            print("  gems:      (not found)")
    except Exception as exc:
        print(f"  [parse error: {exc}]")


if __name__ == "__main__":
    print(f"Listening on {PIPE_PATH} — end a turn in-game to receive data…")
    read_loop(_default_callback)
