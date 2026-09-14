"""Exact pretender availability, design-point, and blessing rules for 6.36."""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping

from dom6_assistant.file_reader.formats.pretender import Awakening, PATH_NAMES, SCALE_NAMES
from dom6_assistant.reference.pretender_chassis_costs import INTRINSIC_CHASSIS_COSTS

PATH_COLUMNS = ("F", "A", "W", "E", "S", "D", "N", "G", "B")
STARTING_POINTS = {
    Awakening.AWAKE: 450,
    Awakening.DORMANT: 600,
    Awakening.IMPRISONED: 800,
}

# Nation attributes read literally by the client routines.
ATTR_HOME_REALM = 289
ATTR_CHEAP_GOD_20 = 314
ATTR_CHEAP_GOD_40 = 315
ATTR_BLESS_POINTS = 329
ATTR_BLESS_PATH_BASE = 330
ATTR_SCALE_BASE = 490
# One per scale, in SCALE_NAMES order, holding that nation's scale-limit
# modifier. The sign picks the direction: Yomi's 640 = -1 is the "Turmoil
# limit +1" its nation screen shows, not an Order limit.
ATTR_SCALE_LIMIT_BASE = 640
ATTR_TEMPERATURE_PREFERENCE = 705

# The realm/addition/exclusion tables form a candidate superset.  The client
# eligibility predicate at 0x4bc060 rejects these remaining 43 pairs in 6.36
# (national chassis restrictions and living/undead/aquatic god constraints).
# This set is the complete delta over every candidate pair for nations 5..134.
_CLIENT_REJECTED_PAIRS = {
    (10, 2848), (11, 2848), (12, 2848), (13, 2848),
    (57, 2848), (58, 2848), (60, 2848),
    (61, 179), (61, 180), (61, 872), (61, 2848), (61, 3888), (61, 3895),
    (62, 179), (62, 180), (62, 872), (62, 2848), (62, 3888), (62, 3895),
    (100, 2848), (101, 2848),
    (103, 179), (103, 180), (103, 872), (103, 2444), (103, 2755),
    (103, 2756), (103, 2848), (103, 3888), (103, 3895),
    (123, 179), (123, 180), (123, 872), (123, 2848), (123, 3888), (123, 3895),
    (124, 2848),
    (125, 248), (125, 294), (125, 973), (125, 2440), (125, 2847), (125, 2855),
}


class PretenderDesignError(ValueError):
    pass


@dataclass(frozen=True)
class Chassis:
    id: int
    name: str
    start_dominion: int
    new_path_cost: int
    base_paths: dict[str, int]
    minimum_awakening: Awakening
    intrinsic_costs: tuple[int, int, int]


@dataclass(frozen=True)
class DesignCost:
    point_pool: int
    chassis: int
    dominion: int
    paths: int
    scales: int

    @property
    def spent(self) -> int:
        return self.chassis + self.dominion + self.paths + self.scales

    @property
    def remaining(self) -> int:
        return self.point_pool - self.spent


@dataclass(frozen=True)
class Blessing:
    id: int
    name: str
    path: str
    required_level: int
    secondary_path: str | None = None
    secondary_level: int = 0
    scale_requirement: tuple[str, str, int] | None = None
    stackable: bool = False

    @property
    def cost(self) -> int:
        # The client uses the primary path requirement as blessing-point cost.
        return self.required_level


@dataclass(frozen=True)
class BlessingSelection:
    selected: tuple[Blessing, ...]
    points_available: int

    @property
    def points_spent(self) -> int:
        return sum(blessing.cost for blessing in self.selected)

    @property
    def points_remaining(self) -> int:
        return self.points_available - self.points_spent


# IDs, order, path requirements, and secondary requirements come from the
# 0xc0-byte client records at file RVA 0x1a0e720 + id*0xc0. This intentionally
# does not use reference.sqlite3's historical `seq` column: additions such as
# Awareness and Heroism changed the shipped ordering.
_BLESSING_ROWS = (
    (1, "Superior Morale", 0, 1, -1, 0),
    (2, "Wasteland Survival", 0, 1, 5, 1),
    (3, "Fire Resistance", 0, 2, -1, 0),
    (4, "Attack Skill", 0, 2, -1, 0),
    (5, "Inspirational Presence", 0, 3, -1, 0),
    (6, "Righteous Wrath", 0, 4, -1, 0),
    (7, "Death Explosion", 0, 5, -1, 0),
    (8, "Heat Aura", 0, 5, -1, 0),
    (9, "Fire Shield", 0, 6, -1, 0),
    (10, "Flaming Weapons", 0, 7, -1, 0),
    (11, "Unbearable Splendour", 0, 8, 4, 4),
    (12, "Precision", 1, 1, -1, 0),
    (13, "Shock Resistance", 1, 2, -1, 0),
    (14, "Farshot", 1, 2, -1, 0),
    (15, "Swiftness", 1, 4, -1, 0),
    (16, "Storm Flight", 1, 4, -1, 0),
    (17, "Wind Walker", 1, 5, -1, 0),
    (18, "Weightlessness", 1, 5, 3, 1),
    (19, "Air Shield", 1, 6, -1, 0),
    (20, "Thunder Weapons", 1, 7, -1, 0),
    (21, "Charged Bodies", 1, 8, -1, 0),
    (22, "Flight", 1, 9, -1, 0),
    (23, "Winter's Gift", 2, 1, -1, 0),
    (24, "Swamp Survival", 2, 1, 6, 1),
    (25, "Cold Resistance", 2, 2, -1, 0),
    (26, "Swimming", 2, 2, -1, 0),
    (27, "Defence Skill", 2, 2, -1, 0),
    (28, "Chill Aura", 2, 5, -1, 0),
    (29, "Slowing Weapons", 2, 5, -1, 0),
    (30, "Vitriol Weapons", 2, 6, 0, 2),
    (31, "Water Breathing", 2, 6, -1, 0),
    (32, "Frost Mist Weapons", 2, 7, -1, 0),
    (33, "Quickness", 2, 9, -1, 0),
    (34, "Mountain Survival", 3, 1, -1, 0),
    (35, "Reinvigoration", 3, 2, -1, 0),
    (36, "Reconstruction", 3, 5, -1, 0),
    (37, "Strength of the Earth", 3, 2, -1, 0),
    (38, "Unbreakable", 3, 4, -1, 0),
    (39, "Resilience of the Earth", 3, 6, -1, 0),
    (40, "Larger", 3, 4, 6, 3),
    (41, "Hard Skin", 3, 6, -1, 0),
    (42, "Fortitude", 3, 7, -1, 0),
    (43, "Arcane Command", 4, 1, -1, 0),
    (44, "Magic Resistance", 4, 2, -1, 0),
    (45, "Spirit Sight", 4, 3, 5, 1),
    (46, "Solar Weapons", 4, 3, 0, 1),
    (47, "Far Caster", 4, 4, -1, 0),
    (48, "Arcane Finesse", 4, 4, -1, 0),
    (49, "Magic Weapons", 4, 5, -1, 0),
    (50, "Twist Fate", 4, 6, -1, 0),
    (51, "Fateweaving", 4, 7, -1, 0),
    (52, "Etherealness", 4, 8, -1, 0),
    (53, "Undying", 5, 1, -1, 0),
    (54, "Undead Command", 5, 1, -1, 0),
    (55, "Half Dead", 5, 2, -1, 0),
    (56, "Mending Bones", 5, 3, -1, 0),
    (57, "Withering Weapons", 5, 4, -1, 0),
    (58, "Stygian Flesh", 5, 5, -1, 0),
    (59, "Reforming Flesh", 5, 6, -1, 0),
    (60, "Reanimators", 5, 7, -1, 0),
    (61, "Death Weapons", 5, 8, -1, 0),
    (62, "Fear", 5, 9, -1, 0),
    (63, "Resilient", 6, 1, -1, 0),
    (64, "Low Light Vision", 6, 1, -1, 0),
    (65, "Poison Resistance", 6, 2, -1, 0),
    (66, "Forest Survival", 6, 2, -1, 0),
    (67, "Unaging", 6, 3, -1, 0),
    (68, "Poison Weapons", 6, 4, -1, 0),
    (69, "Recuperation", 6, 5, -1, 0),
    (70, "Berserker", 6, 5, -1, 0),
    (71, "Barkskin", 6, 6, -1, 0),
    (72, "Regeneration", 6, 7, -1, 0),
    (73, "Undreaming", 7, 1, -1, 0),
    (74, "Quiet Stride", 7, 2, -1, 0),
    (75, "True Sight", 7, 3, -1, 0),
    (76, "Blur", 7, 3, -1, 0),
    (77, "Obfuscate", 7, 6, -1, 0),
    (78, "Awe", 7, 6, 0, 2),
    (79, "Displacement", 7, 7, -1, 0),
    (80, "Dread", 7, 8, -1, 0),
    (81, "Luck", 7, 8, -1, 0),
    (82, "Strong Vitae", 8, 1, -1, 0),
    (83, "Strength of the Flesh", 8, 2, -1, 0),
    (84, "Strong Blood", 8, 3, -1, 0),
    (85, "Enchanted Blood", 8, 4, -1, 0),
    (86, "Blood Surge", 8, 4, -1, 0),
    (87, "Blood Bond", 8, 5, -1, 0),
    (88, "Unholy Weapons", 8, 6, -1, 0),
    (89, "Blood Vengeance", 8, 7, -1, 0),
    (90, "Vampiric Weapons", 8, 8, 5, 4),
    (91, "Awareness", 1, 3, -1, 0),
    (92, "Heroism", 7, 1, -1, 0),
)

_STACKABLE = {
    "Superior Morale", "Fire Resistance", "Attack Skill", "Precision",
    "Shock Resistance", "Farshot", "Awareness", "Defence Skill",
    "Reinvigoration", "Strength of the Earth", "Arcane Command",
    "Magic Resistance", "Undying", "Undead Command", "Resilient",
    "Poison Resistance", "Strong Vitae", "Strength of the Flesh", "Heroism",
}

# The scale clauses are separate fields/effects in the records rather than
# magic-path requirements. User-facing scales are signed goodward: Growth +,
# Magic +, Luck +, Order +, while Cold is negative Heat.
_SCALE_REQUIREMENTS: dict[str, tuple[str, str, int]] = {
    "Winter's Gift": ("heat", "<=", -1),
    "Quickness": ("magic", ">=", 1),
    "Fateweaving": ("luck", "<=", -1),
    "Etherealness": ("magic", ">=", 2),
    "Half Dead": ("growth", "<=", -2),
    "Fear": ("order", "<=", -1),
    "Unaging": ("magic", ">=", 1),
    "Poison Weapons": ("growth", "<=", -1),
    "Luck": ("luck", ">=", 2),
}

BLESSINGS: dict[int, Blessing] = {
    blessing_id: Blessing(
        id=blessing_id,
        name=name,
        path=PATH_NAMES[path],
        required_level=required,
        secondary_path=(PATH_NAMES[secondary] if secondary >= 0 else None),
        secondary_level=secondary_required,
        scale_requirement=_SCALE_REQUIREMENTS.get(name),
        stackable=name in _STACKABLE,
    )
    for blessing_id, name, path, required, secondary, secondary_required in _BLESSING_ROWS
}


def _attributes(conn: sqlite3.Connection, nation_id: int, attribute: int) -> list[int]:
    return [
        int(row[0])
        for row in conn.execute(
            "SELECT raw_value FROM attributes_by_nation "
            "WHERE nation_number=? AND attribute=?",
            (nation_id, attribute),
        )
    ]


def _canonical_chassis(conn: sqlite3.Connection, monster_id: int) -> int:
    row = conn.execute("SELECT shapechange FROM units WHERE id=?", (monster_id,)).fetchone()
    alternate = int(row[0] or 0) if row else 0
    if alternate <= 0:
        return monster_id
    reverse = conn.execute("SELECT shapechange FROM units WHERE id=?", (alternate,)).fetchone()
    if reverse and int(reverse[0] or 0) == monster_id:
        # The client's 0x4532d0 canonicalizer selects the lower member for all
        # reciprocal pretender shape pairs in 6.36.
        return min(monster_id, alternate)
    return monster_id


def available_chassis(conn: sqlite3.Connection, nation_id: int) -> list[Chassis]:
    """Chassis in the nation's realm/addition list minus explicit exclusions."""

    realms = set(_attributes(conn, nation_id, ATTR_HOME_REALM))
    candidates = {
        int(row[0])
        for row in conn.execute(
            "SELECT DISTINCT monster_number FROM realms WHERE realm IN "
            f"({','.join('?' for _ in realms)})",
            tuple(sorted(realms)),
        )
    } if realms else set()
    candidates.update(
        int(row[0])
        for row in conn.execute(
            "SELECT monster_number FROM pretender_types_by_nation WHERE nation_number=?",
            (nation_id,),
        )
    )
    excluded = {
        _canonical_chassis(conn, int(row[0]))
        for row in conn.execute(
            "SELECT monster_number FROM unpretender_types_by_nation WHERE nation_number=?",
            (nation_id,),
        )
    }
    canonical = {
        _canonical_chassis(conn, monster_id) for monster_id in candidates
    } - excluded
    canonical = {
        monster_id for monster_id in canonical
        if (nation_id, monster_id) not in _CLIENT_REJECTED_PAIRS
    }

    out: list[Chassis] = []
    columns = ",".join(PATH_COLUMNS)
    for monster_id in sorted(canonical):
        costs = INTRINSIC_CHASSIS_COSTS.get(monster_id)
        if costs is None:
            continue
        row = conn.execute(
            f"SELECT name,startdom,pathcost,minprison,{columns} FROM units WHERE id=?",
            (monster_id,),
        ).fetchone()
        if row is None or row[1] is None:
            continue
        base_paths = {
            name: int(value)
            for name, value in zip(PATH_NAMES, row[4:13])
            if value not in (None, "") and int(value) > 0
        }
        out.append(Chassis(
            id=monster_id,
            name=str(row[0]),
            start_dominion=int(row[1]),
            new_path_cost=max(10, int(row[2] or 0)),
            base_paths=base_paths,
            minimum_awakening=Awakening(int(row[3] or 0)),
            intrinsic_costs=costs,
        ))
    return out


def get_chassis(conn: sqlite3.Connection, nation_id: int, chassis_id: int) -> Chassis:
    by_id = {chassis.id: chassis for chassis in available_chassis(conn, nation_id)}
    try:
        return by_id[chassis_id]
    except KeyError as exc:
        raise PretenderDesignError(
            f"Chassis {chassis_id} is not available to nation {nation_id}"
        ) from exc


def chassis_cost(
    conn: sqlite3.Connection,
    nation_id: int,
    chassis: Chassis,
    awakening: Awakening,
) -> int:
    cost = chassis.intrinsic_costs[int(awakening)]
    if chassis.id in _attributes(conn, nation_id, ATTR_CHEAP_GOD_20):
        cost -= 20
    if chassis.id in _attributes(conn, nation_id, ATTR_CHEAP_GOD_40):
        cost -= 40
    return min(1000, max(0, cost))


def dominion_cost(start: int, selected: int) -> int:
    if not 1 <= selected <= 10:
        raise PretenderDesignError(f"Dominion must be 1..10, got {selected}")
    difference = selected - start
    magnitude = abs(difference)
    points = 7 * magnitude * (magnitude + 1) // 2
    return points if difference >= 0 else -points


def paths_cost(chassis: Chassis, selected: Mapping[str, int]) -> int:
    unknown = set(selected) - set(PATH_NAMES)
    if unknown:
        raise PretenderDesignError(f"Unknown paths: {sorted(unknown)}")
    total = 0
    for path in PATH_NAMES:
        base = chassis.base_paths.get(path, 0)
        final = int(selected.get(path, 0))
        if not 0 <= final <= 10:
            raise PretenderDesignError(f"{path} must be 0..10, got {final}")
        if final < base:
            raise PretenderDesignError(
                f"{chassis.name} starts with {path} {base}; it cannot be reduced to {final}"
            )
        if final == base:
            continue
        if base == 0:
            total += chassis.new_path_cost
        for level in range(max(base, 1) + 1, final + 1):
            total += 8 * (level - base)
    return total


def scales_cost(
    conn: sqlite3.Connection,
    nation_id: int,
    selected: Mapping[str, int],
) -> int:
    unknown = set(selected) - set(SCALE_NAMES)
    if unknown:
        raise PretenderDesignError(f"Unknown scales: {sorted(unknown)}")
    values = {name: int(selected.get(name, 0)) for name in SCALE_NAMES}
    for name, value in values.items():
        # A coarse sanity bound only. What is actually legal depends on the
        # nation and chassis and is enforced by validate_scale_limits; the
        # widest any pair reaches is 4, from a national +2.
        if not -4 <= value <= 4:
            raise PretenderDesignError(f"Scale {name} must be -4..4, got {value}")

    total = 0
    for index, name in enumerate(SCALE_NAMES):
        if name == "heat":
            continue
        baselines = _attributes(conn, nation_id, ATTR_SCALE_BASE + index)
        raw_baseline = baselines[0] if baselines else 0
        raw_selected = -values[name]
        difference = min(3, raw_selected - raw_baseline)
        total -= 40 * difference

    raw_temperature = -values["heat"]
    preferences = _attributes(conn, nation_id, ATTR_TEMPERATURE_PREFERENCE)
    preference = preferences[0] if preferences else 0
    if preference and raw_temperature * preference < 0:
        distance = abs(preference)  # crossing neutral yields no extra refund
    else:
        distance = abs(raw_temperature - preference)
    total -= 40 * min(3, distance)
    return total


def design_cost(
    conn: sqlite3.Connection,
    nation_id: int,
    chassis_id: int,
    awakening: Awakening | int,
    dominion: int,
    paths: Mapping[str, int],
    scales: Mapping[str, int],
) -> DesignCost:
    awakening = Awakening(awakening)
    chassis = get_chassis(conn, nation_id, chassis_id)
    if awakening < chassis.minimum_awakening:
        raise PretenderDesignError(
            f"{chassis.name} requires at least {chassis.minimum_awakening.name.lower()}"
        )
    validate_scale_limits(conn, nation_id, chassis, scales)
    result = DesignCost(
        point_pool=STARTING_POINTS[awakening],
        chassis=chassis_cost(conn, nation_id, chassis, awakening),
        dominion=dominion_cost(chassis.start_dominion, int(dominion)),
        paths=paths_cost(chassis, paths),
        scales=scales_cost(conn, nation_id, scales),
    )
    if result.remaining < 0:
        raise PretenderDesignError(
            f"Design overspends by {-result.remaining} points ({result.spent}/"
            f"{result.point_pool})"
        )
    return result


# The chassis half of the same rule, one column per scale.
CHASSIS_SCALE_LIMIT_COLUMNS = {
    "order": "moreorder", "productivity": "moreprod", "heat": "moreheat",
    "growth": "moregrowth", "luck": "moreluck", "magic": "moremagic",
}


def scale_limits(
    conn: sqlite3.Connection,
    nation_id: int,
    chassis: Chassis,
) -> dict[str, dict[str, int]]:
    """The legal range of every scale for this nation and chassis.

    An unmodified scale spans -2..+2.  A scale-limit modifier shifts that
    entire five-step window: -1 means -3..+1 and +1 means -1..+3.  This is
    important for chassis such as Dagon: its ``moreprod = -1`` does not merely
    permit Sloth 3, it also caps Productivity at 1.

    Nation and chassis modifiers in the same direction do not add; the
    stronger shift wins.  If they point in opposite directions, each supplies
    the endpoint in its direction.
    """
    row = conn.execute(
        "SELECT " + ",".join(CHASSIS_SCALE_LIMIT_COLUMNS[name] for name in SCALE_NAMES)
        + " FROM units WHERE id=?", (chassis.id,)
    ).fetchone()
    limits: dict[str, dict[str, int]] = {}
    for index, name in enumerate(SCALE_NAMES):
        national = _attributes(conn, nation_id, ATTR_SCALE_LIMIT_BASE + index)
        nation = national[0] if national else 0
        chassis_value = int(row[index] or 0) if row is not None else 0
        positive = max(0, nation, chassis_value)
        negative = min(0, nation, chassis_value)
        if positive and negative:
            # Opposing explicit limits supply one endpoint each.  For example,
            # MA Marignon's Order +1 and the Serpent's Turmoil +1 yield -3..+3.
            minimum_modifier = negative
            maximum_modifier = positive
        else:
            # One direction (or no modifier) shifts the whole window.  Taking
            # the strongest value also makes equal nation/chassis modifiers
            # non-additive.
            minimum_modifier = maximum_modifier = positive or negative
        limits[name] = {
            "minimum": -2 + minimum_modifier,
            "maximum": 2 + maximum_modifier,
            "nation_modifier": nation,
            "chassis_modifier": chassis_value,
        }
    return limits


def validate_scale_limits(
    conn: sqlite3.Connection,
    nation_id: int,
    chassis: Chassis,
    selected: Mapping[str, int],
) -> None:
    limits = scale_limits(conn, nation_id, chassis)
    for name in SCALE_NAMES:
        value = int(selected.get(name, 0))
        bound = limits[name]
        if not bound["minimum"] <= value <= bound["maximum"]:
            raise PretenderDesignError(
                f"{name} {value:+d} is outside the legal range "
                f"{bound['minimum']}..{bound['maximum']} for this nation and "
                f"chassis (a scale-limit modifier shifts the whole -2..+2 "
                f"window; nation and chassis modifiers in the same direction "
                f"do not add)"
            )


def cost_tables(
    conn: sqlite3.Connection,
    nation_id: int,
    chassis: Chassis,
    awakening: Awakening,
) -> dict[str, Any]:
    """Every price a design decision can carry, for one chassis and awakening.

    Produced by calling the same cost functions ``design_cost`` uses rather
    than by restating their arithmetic, so a price quoted here cannot drift
    away from what ``evaluate_pretender`` actually charges.

    Three things make a stated formula insufficient on its own, and all three
    are already baked into these numbers: paths and Dominion escalate rather
    than costing a flat rate per level; the scale refund is capped three steps
    from the national baseline; and for 54 nations an all-neutral scale set is
    *not* free -- 52 because neutral is itself a deviation from the temperature
    they prefer, and Ermor and Lemuria because they start at Growth -3.
    """
    neutral = {name: 0 for name in SCALE_NAMES}
    neutral_total = scales_cost(conn, nation_id, neutral)
    limits = scale_limits(conn, nation_id, chassis)

    # Only legal values are priced. Offering a cost for a scale this nation and
    # chassis cannot reach would invite a design the game refuses.
    scales: dict[str, dict[int, int]] = {}
    for name in SCALE_NAMES:
        bound = limits[name]
        scales[name] = {
            value: scales_cost(conn, nation_id, {**neutral, name: value}) - neutral_total
            for value in range(bound["minimum"], bound["maximum"] + 1)
        }

    paths: dict[str, dict[str, Any]] = {}
    for path in PATH_NAMES:
        base = chassis.base_paths.get(path, 0)
        levels = {}
        previous = 0
        for level in range(base, 11):
            total = paths_cost(chassis, {**chassis.base_paths, path: level})
            levels[level] = {
                "total": total,
                "cost_from_previous_level": total - previous,
            }
            previous = total
        for level, price in levels.items():
            following = levels.get(level + 1)
            price["cost_to_next_level"] = (
                following["cost_from_previous_level"] if following else None
            )
        paths[path] = {
            "starting_level": base,
            "opening_cost": chassis.new_path_cost if base == 0 else 0,
            "by_level": levels,
        }

    bless_flat = sum(_attributes(conn, nation_id, ATTR_BLESS_POINTS))
    bless_paths = {
        path: sum(_attributes(conn, nation_id, ATTR_BLESS_PATH_BASE + index))
        for index, path in enumerate(PATH_NAMES)
    }

    return {
        "point_pool": STARTING_POINTS[awakening],
        "chassis": {
            name: chassis_cost(conn, nation_id, chassis, value)
            for name, value in (
                ("awake", Awakening.AWAKE),
                ("dormant", Awakening.DORMANT),
                ("imprisoned", Awakening.IMPRISONED),
            )
            if value >= chassis.minimum_awakening
        },
        "dominion": {
            "starting_value": chassis.start_dominion,
            "by_value": {
                value: dominion_cost(chassis.start_dominion, value)
                for value in range(1, 11)
            },
        },
        "paths": paths,
        "scales": {
            "all_neutral_cost": neutral_total,
            "delta_by_value": scales,
            "legal_range": limits,
        },
        "bless_points": {
            "national_flat_bonus": bless_flat,
            "national_path_modifiers": {
                path: modifier for path, modifier in bless_paths.items() if modifier
            },
            "points_from_path_level": {
                path: {
                    level: max(0, level + bless_paths[path] - 1)
                    for level in range(1, 11)
                }
                for path in PATH_NAMES
            },
        },
    }


def blessing_points(
    conn: sqlite3.Connection,
    nation_id: int,
    paths: Mapping[str, int],
) -> int:
    total = sum(_attributes(conn, nation_id, ATTR_BLESS_POINTS))
    for index, path in enumerate(PATH_NAMES):
        modifier = sum(_attributes(conn, nation_id, ATTR_BLESS_PATH_BASE + index))
        total += max(0, int(paths.get(path, 0)) + modifier - 1)
    return max(0, total)


def _scale_satisfies(value: int, operator: str, required: int) -> bool:
    if operator == ">=":
        return value >= required
    if operator == "<=":
        return value <= required
    raise AssertionError(operator)


def validate_blessings(
    conn: sqlite3.Connection,
    nation_id: int,
    paths: Mapping[str, int],
    scales: Mapping[str, int],
    blessing_ids: tuple[int, ...] | list[int],
) -> BlessingSelection:
    selected: list[Blessing] = []
    for blessing_id in blessing_ids:
        try:
            blessing = BLESSINGS[int(blessing_id)]
        except KeyError as exc:
            raise PretenderDesignError(f"Unknown blessing id {blessing_id}") from exc
        if int(paths.get(blessing.path, 0)) < blessing.required_level:
            raise PretenderDesignError(
                f"{blessing.name} requires {blessing.path} {blessing.required_level}"
            )
        if (
            blessing.secondary_path is not None
            and int(paths.get(blessing.secondary_path, 0)) < blessing.secondary_level
        ):
            raise PretenderDesignError(
                f"{blessing.name} also requires {blessing.secondary_path} "
                f"{blessing.secondary_level}"
            )
        if blessing.scale_requirement is not None:
            scale, operator, required = blessing.scale_requirement
            value = int(scales.get(scale, 0))
            if not _scale_satisfies(value, operator, required):
                raise PretenderDesignError(
                    f"{blessing.name} requires {scale} {operator} {required}"
                )
        selected.append(blessing)

    for blessing_id, count in Counter(int(value) for value in blessing_ids).items():
        if count > 1 and not BLESSINGS[blessing_id].stackable:
            raise PretenderDesignError(f"{BLESSINGS[blessing_id].name} is not stackable")

    selection = BlessingSelection(
        selected=tuple(selected),
        points_available=blessing_points(conn, nation_id, paths),
    )
    if selection.points_remaining < 0:
        raise PretenderDesignError(
            f"Blessings overspend by {-selection.points_remaining} points"
        )
    return selection
