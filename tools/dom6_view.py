#!/usr/bin/env python3
"""Capture the Dominions 6 window, masking anything stacked on top of it.

Dominions is an OpenGL window, so `import -window` returns a blank pixmap and
the only workable capture is of the root window cropped to the game's geometry.
That grabs whatever pixels are physically on screen, which is how a private
Discord conversation got captured once already.

The first fix required the game to be the *active* window. That was the wrong
test. Focus is a proxy for "nothing is in front", and the two come apart: with
the game side by side with an editor, a RustDesk window sat over the game's
top-right corner at (1620,0)-(1920,490) while the game was perfectly focusable.
A focused capture would still have photographed a remote desktop.

So the real test is occlusion. This reads the X stacking order, takes every
window *above* Dominions, intersects each with the game's rectangle, and paints
those regions black. Windows below it are irrelevant — the game covers them.

The unmasked screenshot is never written to disk. `import` streams PNG to
stdout, masking happens in memory, and only the masked image is saved.

Exit codes: 1 no window, 3 capture looks blank.
"""
from __future__ import annotations

import io
import re
import subprocess
import sys

from PIL import Image


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def geometry(wid: int) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) for a window id, or None if it has gone away."""
    out = sh("xdotool", "getwindowgeometry", "--shell", str(wid))
    got = dict(re.findall(r"^(\w+)=(-?\d+)$", out, re.M))
    if not {"X", "Y", "WIDTH", "HEIGHT"} <= got.keys():
        return None
    return (int(got["X"]), int(got["Y"]), int(got["WIDTH"]), int(got["HEIGHT"]))


def stacking() -> list[int]:
    """Window ids bottom to top."""
    out = sh("xprop", "-root", "_NET_CLIENT_LIST_STACKING")
    return [int(h, 16) for h in re.findall(r"0x[0-9a-f]+", out)]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: dom6_view.py <output.png>", file=sys.stderr)
        return 1
    out_path = sys.argv[1]

    found = sh("xdotool", "search", "--name", "^Dominions 6$").split()
    if not found:
        print("dominions window not found", file=sys.stderr)
        return 1
    wid = int(found[0])
    geo = geometry(wid)
    if geo is None:
        print("dominions window has no geometry", file=sys.stderr)
        return 1
    gx, gy, gw, gh = geo

    order = stacking()
    # Anything not in the stacking list is treated as above, which errs toward
    # masking rather than toward exposing.
    above = order[order.index(wid) + 1:] if wid in order else order

    occluders: list[tuple[int, int, int, int, str]] = []
    for other in above:
        og = geometry(other)
        if og is None:
            continue
        ox, oy, ow, oh = og
        # Intersect in screen space, then shift into image space.
        ix0, iy0 = max(gx, ox), max(gy, oy)
        ix1, iy1 = min(gx + gw, ox + ow), min(gy + gh, oy + oh)
        if ix0 < ix1 and iy0 < iy1:
            name = sh("xdotool", "getwindowname", str(other)).strip()
            occluders.append((ix0 - gx, iy0 - gy, ix1 - gx, iy1 - gy, name))

    png = subprocess.run(
        ["import", "-window", "root", "-crop", f"{gw}x{gh}+{gx}+{gy}",
         "+repage", "png:-"],
        capture_output=True).stdout
    if len(png) < 20000:
        print(f"capture looks blank ({len(png)} bytes)", file=sys.stderr)
        return 3

    img = Image.open(io.BytesIO(png)).convert("RGB")
    for x0, y0, x1, y1, _name in occluders:
        img.paste((0, 0, 0), (x0, y0, x1, y1))
    img.save(out_path)

    note = ""
    if occluders:
        note = "; masked " + ", ".join(
            f"{n or '?'} [{x0},{y0}-{x1},{y1}]" for x0, y0, x1, y1, n in occluders)
    print(f"captured {gw}x{gh} -> {out_path}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
