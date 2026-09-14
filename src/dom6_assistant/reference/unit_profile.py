"""Extracted unit stats, traits and equipment, from the reference data alone.

The live-turn ``lookup_unit`` tool and the pregame pretender chassis tools need
the same picture of a unit, but they cannot share a context: a pretender
session has no game, so no observed recruitment costs and no ``PlayerView``.
The field lists and the extraction live here so the two callers cannot drift
apart, and so neither restates which columns matter.

Nothing here reads a save file. Every value is static reference data, which is
what makes it legal for a pregame session to see.
"""
from __future__ import annotations

import sqlite3
from typing import Any

RESISTANCE_FIELDS = (
    "slashres",
    "bluntres",
    "pierceres",
    "shockres",
    "fireres",
    "coldres",
    "poisonres",
    "acidres",
    "voidsanity",
    "darkvision",
)
MOVEMENT_FIELDS = (
    "ap",
    "flying",
    "float",
    "teleport",
    "immobile",
    "noriverpass",
    "forestsurvival",
    "mountainsurvival",
    "wastesurvival",
    "swampsurvival",
    "cavesurvival",
    "aquatic",
    "amphibian",
    "pooramphibian",
    "stealthy",
    "sailingshipsize",
    "sailingmaxunitsize",
    "swimming",
    "snowmove",
    "wintermove",
)
ABILITY_FIELDS = (
    "holy",
    "inquisitor",
    "inanimate",
    "undead",
    "demon",
    "magicbeing",
    "stonebeing",
    "animal",
    "coldblood",
    "female",
    "illusion",
    "spy",
    "assassin",
    "patience",
    "seduce",
    "succubus",
    "corrupt",
    "heal",
    "immortal",
    "domimmortal",
    "reinc",
    "noheal",
    "neednoteat",
    "homesick",
    "undisciplined",
    "formationfighter",
    "slave",
    "standard",
    "inspirational",
    "taskmaster",
    "beastmaster",
    "bodyguard",
    "invulnerable",
    "blind",
    "animalawe",
    "awe",
    "fear",
    "berserk",
    "cold",
    "heat",
    "fireshield",
    "banefireshield",
    "damagerev",
    "poisoncloud",
    "diseasecloud",
    "regeneration",
    "ethereal",
    "trample",
    "stormpower",
    "forgebonus",
    "fixforgebonus",
    "mastersmith",
    "autohealer",
    "autodishealer",
    "alch",
    "insane",
    "shatteredsoul",
    "pillagebonus",
    "patrolbonus",
    "castledef",
    "siegebonus",
    "incprovdef",
    "supplybonus",
    "researchbonus",
    "drainimmune",
    "douse",
    "heretic",
    "elegist",
    "shapechange",
    "unique",
    "fixedresearch",
    "bloodvengeance",
    "bringeroffortune",
    "reanimator",
    "reanimator_2",
    "spreaddom",
    "spiritsight",
    "truesight",
    "stunimmunity",
    "woundfend",
    "norange",
)

# The short list shown for triage. These are the traits that change what a
# chassis is *for* rather than how well it does a thing: whether it can be
# killed permanently, whether it can go in the water, whether it can be seen.
SUMMARY_FLAG_FIELDS = (
    "immortal",
    "domimmortal",
    "unique",
    "undead",
    "demon",
    "magicbeing",
    "inanimate",
    "holy",  # the sacred flag; there is no separate "sacred" column
    "flying",
    "aquatic",
    "amphibian",
    "pooramphibian",
    "immobile",
    "stealthy",
    "blind",
    "awe",
    "fear",
    "trample",
    "ethereal",
    "regeneration",
    "invulnerable",
    "neednoteat",
    "noheal",
    "heat",
    "cold",
    "forgebonus",
    "mastersmith",
    "researchbonus",
    "inspirational",
    "reanimator",
    "spreaddom",
    "shapechange",
)

CORE_STAT_FIELDS = (
    ("hp", "hp"),
    ("protection", "prot"),
    ("morale", "mor"),
    ("magic_resistance", "mr"),
    ("strength", "str"),
    ("attack", "att"),
    ("defence", "def"),
    ("precision", "prec"),
    ("encumbrance", "enc"),
    ("size", "size"),
    # The game's unit card labels these Map Move and Combat Speed. `ap` also
    # appears in movement_and_survival, where lookup_unit has always reported
    # it; it is repeated here because the card shows it beside the stats.
    ("map_move", "mapmove"),
    ("combat_speed", "ap"),
)

# The pretender designer gives an unmodified scale a -2..+2 window. A nation
# or chassis modifier shifts that whole window: -1 means -3..+1, not merely an
# extra bad-side endpoint. These are the chassis-side fields.
SCALE_LIMIT_COLUMNS = (
    ("order", "moreorder"),
    ("productivity", "moreprod"),
    ("heat", "moreheat"),
    ("growth", "moregrowth"),
    ("luck", "moreluck"),
    ("magic", "moremagic"),
)


def nonzero(row: sqlite3.Row, fields: tuple[str, ...]) -> dict[str, Any]:
    """Selected non-zero extracted fields, without manufacturing booleans."""
    keys = set(row.keys())
    return {
        field: row[field]
        for field in fields
        if field in keys and row[field] not in (None, 0, "")
    }


def _resolve(conn: sqlite3.Connection, table: str, ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    names = {
        int(row["id"]): row["name"]
        for row in conn.execute(
            f"SELECT id, name FROM {table} WHERE id IN ({marks})", ids
        )
    }
    return [{"id": value, "name": names.get(value)} for value in ids]


def core_stats(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        label: row[column]
        for label, column in CORE_STAT_FIELDS
        if column in keys and row[column] is not None
    }


def scale_limits(row: sqlite3.Row) -> dict[str, int]:
    """The chassis half of the scale-limit rule, as stored."""
    keys = set(row.keys())
    return {
        scale: row[column]
        for scale, column in SCALE_LIMIT_COLUMNS
        if column in keys and row[column]
    }


def description(conn: sqlite3.Connection, unit_id: int) -> str | None:
    """The card text the game shows for this unit, or None if absent."""
    try:
        row = conn.execute(
            "SELECT description FROM unit_descriptions WHERE id=?", (int(unit_id),)
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row is not None and row[0] else None


def summary_traits(row: sqlite3.Row) -> dict[str, Any]:
    """The traits worth seeing before opening a chassis in full."""
    return nonzero(row, SUMMARY_FLAG_FIELDS)


def full_profile(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """Everything the reference data records about one unit."""
    keys = set(row.keys())
    weapon_ids = [
        int(row[f"wpn{i}"]) for i in range(1, 8) if f"wpn{i}" in keys and row[f"wpn{i}"]
    ]
    armor_ids = [
        int(row[f"armor{i}"]) for i in range(1, 5) if f"armor{i}" in keys and row[f"armor{i}"]
    ]
    profile: dict[str, Any] = {
        "stats": core_stats(row),
        "weapons": _resolve(conn, "weapons", weapon_ids),
        "armor": _resolve(conn, "armors", armor_ids),
        "resistances": nonzero(row, RESISTANCE_FIELDS),
        "movement_and_survival": nonzero(row, MOVEMENT_FIELDS),
        "abilities": nonzero(row, ABILITY_FIELDS),
        "leadership_reference_values": nonzero(
            row, ("leader", "undeadleader", "magicleader")
        ),
    }
    limits = scale_limits(row)
    if limits:
        profile["scale_limit_modifiers"] = limits
    equipment_slots = nonzero(
        row, ("hand", "head", "body", "foot", "misc")
    )
    if equipment_slots:
        profile["equipment_slots"] = equipment_slots
    age = nonzero(row, ("startage", "maxage"))
    if age:
        profile["age"] = age
    return profile
