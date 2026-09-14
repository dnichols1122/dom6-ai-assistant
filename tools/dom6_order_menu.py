#!/usr/bin/env python3
"""Select a commander by sidebar slot, open its order menu, capture it.

Usage: grab_menu.py <slot 0-11> <outname> [hover_y]

Slots run left-to-right, top-to-bottom through the two-column commander
sidebar. If hover_y is given, the mouse is parked on that row of the menu
first so the game renders its description at the bottom of the screen — which
is how a greyed-out order explains why it is blocked.
"""
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(".")
OUT = Path("/tmp/claude-1000/-mnt-SSD2Ext4-dom6-server-hoster/"
           "a0dab02c-5483-46cf-b080-d76ced3d7f26/scratchpad/shots")
COLS = (788, 880)
ROWS = (158, 264, 370, 476, 582, 688)
SLOTS = [(x, y) for y in ROWS for x in COLS]


def run(*args):
    subprocess.run(["xdotool", *args], check=False)


def main() -> int:
    slot = int(sys.argv[1])
    name = sys.argv[2]
    hover_y = int(sys.argv[3]) if len(sys.argv) > 3 else None
    x, y = SLOTS[slot]

    # DO NOT press Escape to clear a stale menu. Escape with nothing open brings
    # up the Options dialog, which contains "Quit without saving", "Redo turn from
    # scratch" and "Become AI controlled" — and the portrait click that follows
    # then lands inside it. That is a far worse failure than the one it was meant
    # to prevent.
    #
    # The menu is dismissed by clicking its own Cancel entry instead, which this
    # tool does after capturing. Callers must let it finish rather than
    # interleaving their own clicks: an order menu opens over the sidebar, so
    # clicking a portrait while one is up selects whatever order sits at those
    # coordinates. Both failure modes are silent.

    run("mousemove", str(x), str(y))
    time.sleep(0.4)
    run("click", "1")
    time.sleep(0.9)
    run("key", "space")
    time.sleep(1.4)
    if hover_y is not None:
        run("mousemove", "820", str(hover_y))
        time.sleep(1.3)
    dest = OUT / f"{name}.png"
    subprocess.run(["python3", str(REPO / "tools" / "dom6_view.py"), str(dest)],
                   cwd=REPO, capture_output=True)
    # Dismiss with Escape, NOT with a click at a fixed position.
    #
    # An earlier version clicked what it assumed was the Cancel row. The panel
    # follows the commander's portrait, so its rows move: on Dapamort's menu that
    # coordinate landed on "Demolish Lab" and issued the order. The sidebar then
    # read "Demolish Lab" where it had read "Patrol", and nothing failed — the
    # laboratory would simply have been gone after the next turn.
    #
    # Escape is position-independent, and it is safe *here* specifically because
    # a menu is guaranteed open at this point. Pressing it with nothing open
    # raises the Options dialog, which is why it must not be used speculatively
    # to clear a menu that may not exist.
    run("key", "Escape")
    time.sleep(0.6)
    print(f"captured slot {slot} -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
