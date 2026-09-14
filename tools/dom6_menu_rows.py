#!/usr/bin/env python3
"""Find the clickable rows of an open order menu, by pixel analysis.

Clicking a menu row from a remembered coordinate is how this project nearly
demolished a laboratory: the panel follows the commander's portrait, so the same
screen position is a different order for a different commander. Reading the panel
out of the image each time removes the guess.

The panel is a dark rounded rectangle on the left of the screen. Text rows inside
it are bright pixels on that dark ground, so rows are found by scanning for
horizontal bands with enough light pixels, then merging adjacent scanlines.

Output is one line per row: index and window y.

**This is a helper, not an oracle.** On Dapamort's menu it finds 13 of roughly 18
rows: short labels like "Defend" render too few bright pixels to clear the
threshold reliably, and greyed-out entries are dimmer still. Raising sensitivity
picks up the red section-header bars instead, which is a different wrong answer.

So a caller must NOT map row index to label by position. Use this to locate a row
whose y is then confirmed against a capture before clicking. Getting this wrong
is not a failed click — it is a different order, silently issued, which is how a
dismiss click at a remembered coordinate once queued "Demolish Lab".

Usage: dom6_menu_rows.py <capture.png>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

PANEL_X0, PANEL_X1 = 20, 270        # window x range the panel occupies
# Calibrated against a real menu rather than guessed. Sampling every 4th pixel
# across the panel on a known text row ("Patrol Province") gives 9 pixels above
# 120 per channel and a peak near 200; an empty gap one row below gives 0 above
# 120 and peaks at 142. So 120 separates text from background cleanly, and a
# threshold of 3 catches short labels like "Defend", which only registers 4.
MIN_BRIGHT = 3
BRIGHT = 120


def rows(path: Path) -> list[tuple[int, int]]:
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    x1 = min(PANEL_X1, w)
    bands: list[tuple[int, int]] = []
    start = None
    for y in range(h):
        count = 0
        for x in range(PANEL_X0, x1, 4):
            r, g, b = px[x, y]
            if r > BRIGHT and g > BRIGHT and b > BRIGHT:
                count += 1
        if count >= MIN_BRIGHT:
            if start is None:
                start = y
        else:
            if start is not None and y - start >= 4:
                bands.append((start, y))
            start = None
    if start is not None:
        bands.append((start, h))
    return [(i, (a + b) // 2) for i, (a, b) in enumerate(bands)]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: dom6_menu_rows.py <capture.png>", file=sys.stderr)
        return 1
    found = rows(Path(sys.argv[1]))
    if not found:
        print("no menu rows found — is a menu actually open?", file=sys.stderr)
        return 2
    for i, y in found:
        print(f"{i}\t{y}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
