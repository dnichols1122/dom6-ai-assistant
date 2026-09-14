#!/usr/bin/env python3
"""Roll turns in the open Dominions game, dismissing what blocks the next one.

`E` ends the turn, but the game then opens the Messages window, and while any
dialog is up the next `E` is swallowed. A naive loop therefore rolls exactly one
turn and then silently stalls, which is what happened before this existed.

Dismissal is done by clicking the dialog's "Exit", whose vertical position moves
with the number of messages. Rather than guess it, this clicks each candidate
row and re-checks, then confirms success the only way that actually matters:
the turn number in the .trn advanced. Nothing here trusts a keypress to have
landed — the file is the oracle.

Usage:
    python tools/dom6_roll.py [count]        # default 1
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
from pathlib import Path

WINDOW_NAME = "^Dominions 6$"
SAVE = Path.home() / ".dominions6" / "savedgames" / "example_game"
TRN = SAVE / "mid_marignon.trn"

# Dialogs are centred in the window; "Exit" sits at the bottom of the panel and
# shifts down as more messages are listed. These are window-relative y values
# covering the range seen so far.
EXIT_X = 597
EXIT_YS = (495, 523, 551, 579, 607, 467, 439)


def window_id() -> int:
    out = subprocess.run(["xdotool", "search", "--name", WINDOW_NAME],
                         capture_output=True, text=True).stdout.split()
    if not out:
        raise SystemExit("Dominions window not found")
    return int(out[0])


def window_origin(wid: int) -> tuple[int, int]:
    out = subprocess.run(["xdotool", "getwindowgeometry", "--shell", str(wid)],
                         capture_output=True, text=True).stdout
    got = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    return int(got["X"]), int(got["Y"])


def turn_number() -> int:
    return struct.unpack_from("<I", TRN.read_bytes(), 0x0E)[0]


def dismiss(wid: int) -> None:
    """Click every plausible Exit position. Harmless if none is there.

    Clicking a spot with no dialog lands on the map and at worst selects a
    province, which the next roll does not care about.
    """
    ox, oy = window_origin(wid)
    for y in EXIT_YS:
        subprocess.run(["xdotool", "mousemove", str(ox + EXIT_X), str(oy + y)])
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"])
        time.sleep(0.5)


def roll_once(wid: int, timeout_s: int = 90) -> int | None:
    """End the turn. Returns the new turn number, or None if it never advanced."""
    start_turn = turn_number()
    start_mtime = os.path.getmtime(TRN)
    subprocess.run(["xdotool", "windowactivate", "--sync", str(wid)])
    time.sleep(0.6)
    subprocess.run(["xdotool", "key", "e"])

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2)
        if os.path.getmtime(TRN) != start_mtime and turn_number() != start_turn:
            time.sleep(2.5)          # let the host finish writing
            return turn_number()
    return None


def main(argv: list[str]) -> int:
    count = int(argv[1]) if len(argv) > 1 else 1
    wid = window_id()
    rolled = 0
    for _ in range(count):
        dismiss(wid)                 # clear anything left from the previous turn
        new = roll_once(wid)
        if new is None:
            print(f"stalled at turn {turn_number()} after {rolled} roll(s)",
                  file=sys.stderr)
            return 1
        print(f"rolled -> turn {new}")
        subprocess.run(["bash", "tools/snapshot.sh", f"t{new}"],
                       capture_output=True)
        rolled += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
