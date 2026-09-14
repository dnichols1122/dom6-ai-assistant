"""Reader for Dominions 6 `.map` files.

The .map is plain text, which makes it the cheapest source of truth in the
whole project: terrain and the province graph can be read off directly instead
of being decoded out of the .trn and hoped about. It is also how the terrain
offset in the .trn was settled — the file lists `#terrain <id> <bitmask>` for
every province, so a candidate offset either reproduces all 99 values or it
does not.

Division of labour with the .trn:

    .map   static  terrain, province graph, border types
    .trn   dynamic ownership, population, scales, units, gems

Both are needed. Border types in particular appear only here: a link can be a
mountain pass or a river crossing, which blocks ground movement while flyers
pass freely, and that is invisible in the .trn.

Random maps are written next to the save as `__randommap_<game>.map`, with a
companion `__under_<game>.map` for the underworld.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ``#neighbourspec <a> <b> <flags>`` is a bit mask.  The 6.26 map manual lists
# the first four bits; the map editor also emits Bridge and the purely visual
# Border Mountains bit.  Random maps commonly combine the latter with a real
# movement restriction (33 = mountain pass + border mountains, for example).
BORDER_TYPES = {
    1: "mountain pass",
    2: "river",
    4: "impassable",
    8: "road",
    16: "bridge",
    32: "border mountains",
}


def border_names(flags: int) -> list[str]:
    """Expand a combined ``#neighbourspec`` mask without hiding unknown bits."""
    names = [name for bit, name in BORDER_TYPES.items() if flags & bit]
    known = sum(bit for bit in BORDER_TYPES if flags & bit)
    if unknown := flags & ~known:
        names.append(f"unknown bits {unknown}")
    return names

_RE = {
    "terrain":       re.compile(r"^#terrain\s+(\d+)\s+(\d+)", re.M),
    "neighbour":     re.compile(r"^#neighbour\s+(\d+)\s+(\d+)", re.M),
    "neighbourspec": re.compile(r"^#neighbourspec\s+(\d+)\s+(\d+)\s+(\d+)", re.M),
    "landname":      re.compile(r'^#landname\s+(\d+)\s+"([^"]*)"', re.M),
    "description":   re.compile(r'^#description\s+"([^"]*)"', re.M),
    "mapsize":       re.compile(r"^#mapsize\s+(\d+)\s+(\d+)", re.M),
    "saildist":      re.compile(r"^#saildist\s+(\d+)", re.M),
}


@dataclass
class MapFile:
    path: Path
    terrain: dict[int, int] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=dict)
    neighbours: dict[int, set[int]] = field(default_factory=dict)
    borders: dict[tuple[int, int], int] = field(default_factory=dict)
    description: str = ""
    width: int = 0
    height: int = 0
    sail_distance: int = 2

    @property
    def province_count(self) -> int:
        return len(self.terrain)

    def border(self, a: int, b: int) -> int:
        """Border flags between two provinces; 0 = ordinary open border."""
        return self.borders.get((min(a, b), max(a, b)), 0)


def parse(path: str | Path) -> MapFile:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    m = MapFile(path=p)

    for mo in _RE["terrain"].finditer(text):
        m.terrain[int(mo[1])] = int(mo[2])
    for mo in _RE["landname"].finditer(text):
        m.names[int(mo[1])] = mo[2]
    for mo in _RE["neighbour"].finditer(text):
        a, b = int(mo[1]), int(mo[2])
        m.neighbours.setdefault(a, set()).add(b)
        m.neighbours.setdefault(b, set()).add(a)
    for mo in _RE["neighbourspec"].finditer(text):
        a, b, f = int(mo[1]), int(mo[2]), int(mo[3])
        m.borders[(min(a, b), max(a, b))] = f
    if (mo := _RE["description"].search(text)):
        m.description = mo[1]
    if (mo := _RE["mapsize"].search(text)):
        m.width, m.height = int(mo[1]), int(mo[2])
    if (mo := _RE["saildist"].search(text)):
        m.sail_distance = int(mo[1])
    return m


def find_for_save(save_dir: str | Path) -> list[Path]:
    """.map files belonging to a save directory, surface first.

    A random game writes `__randommap_<game>.map` and `__under_<game>.map`; the
    underworld one is listed second so callers that want "the" map get the
    surface.
    """
    d = Path(save_dir)
    surface = sorted(d.glob("__randommap_*.map"))
    under = sorted(d.glob("__under_*.map"))
    return [*surface, *under, *(p for p in sorted(d.glob("*.map"))
                                if p not in surface and p not in under)]
