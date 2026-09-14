"""How many troops a commander can lead.

Base leadership comes from the unit type; **worn items add to it**, and that
term is not optional. Nergash's chassis carries `leader 10` and no undead
leadership at all, yet the game handed him 125 Longdead — because the
mercenary company equips him with a Crown of Bones, which grants `ldr_u 150`.

That looked like a counterexample refuting the whole model and was recorded as
one. It was an unmodelled item, and the mercenary table names it outright in
its `item1` column.

Troops draw on the pool matching what they are: undead against undead
leadership, magic beings against magic leadership, everything else against
normal.
"""
from __future__ import annotations

from dataclasses import dataclass

from dom6_assistant.reference.research_rate import experience_stars

#: Item columns granting each kind of leadership.
ITEM_COLUMNS = {"normal": "ldr_n", "undead": "ldr_u", "magic": "ldr_m"}

#: Unit columns for the base value.
UNIT_COLUMNS = {"normal": "leader", "undead": "undeadleader",
                "magic": "magicleader"}

# Experience adds regular leadership at the same five star thresholds used by
# combat stats/research: +25, then +75/+125/+175/+225 total.
EXPERIENCE_LEADERSHIP_BY_STARS = (0, 25, 75, 125, 175, 225)

ADVANCED_FORMATION_MIN_LEADERSHIP = 80
BASIC_FORMATIONS = frozenset({"box", "skirmish"})
ADVANCED_FORMATIONS = frozenset({
    "line", "double_line", "sparse_line", "box", "skirmish",
})


@dataclass(frozen=True)
class Leadership:
    """Capacity by pool, and where each part came from."""
    normal: int
    undead: int
    magic: int
    from_items: dict[str, int]
    from_experience: int = 0

    def capacity(self, pool: str) -> int:
        return getattr(self, pool)


def troop_pool(unit_row) -> str:
    """Which leadership pool a troop consumes."""
    def flag(name):
        try:
            return bool(unit_row[name])
        except (IndexError, KeyError):
            return False
    if flag("undead"):
        return "undead"
    if flag("magicbeing"):
        return "magic"
    return "normal"


def leadership_for(unit_row, item_rows=(), *, experience: int = 0) -> Leadership:
    """Final leadership from chassis, worn items and experience."""
    def unit_value(pool):
        try:
            return int(unit_row[UNIT_COLUMNS[pool]] or 0)
        except (IndexError, KeyError, TypeError):
            return 0

    totals = {pool: unit_value(pool) for pool in UNIT_COLUMNS}
    from_items: dict[str, int] = {}
    for item in item_rows:
        for pool, column in ITEM_COLUMNS.items():
            try:
                bonus = int(item[column] or 0)
            except (IndexError, KeyError, TypeError):
                bonus = 0
            if bonus:
                totals[pool] += bonus
                from_items[f"{item['name']} ({pool})"] = bonus
    stars = experience_stars(experience)
    experience_bonus = EXPERIENCE_LEADERSHIP_BY_STARS[stars]
    totals["normal"] += experience_bonus
    return Leadership(normal=totals["normal"], undead=totals["undead"],
                      magic=totals["magic"], from_items=from_items,
                      from_experience=experience_bonus)


def available_formations(normal_leadership: int) -> frozenset[str]:
    """Client formations unlocked by final regular leadership."""
    return (
        ADVANCED_FORMATIONS
        if int(normal_leadership) >= ADVANCED_FORMATION_MIN_LEADERSHIP
        else BASIC_FORMATIONS
    )
