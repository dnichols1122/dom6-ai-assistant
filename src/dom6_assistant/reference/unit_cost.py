"""The recruitment price of a unit, computed the way the game shows it.

**Why this has to be computed at all.** The scraped data has no price column.
`basecost` is a *modifier*, not a cost: it reads 10010 for both a Crossbowman
at 10 gold and a High Inquisitor at 285. dom6inspector derives the displayed
number from the unit's stats at render time and never stores it, so the only
alternatives were to observe every price in game one unit at a time, or to port
the derivation. This is the port.

Ported from `scripts/DMI/MUnit.js` in larzm42/dom6inspector — the same public
repository the CSVs came from — specifically `MUnit.autocalc` and the mount
block that follows it. The shape:

    cost      = the largest of {leadership, paths, priest, spy}, plus half of
                each of the other three, for COMMANDERS; zero for troops
    goldcost  = cost + special_cost + (basecost - 10000)
    ...then   x0.9 if slow to recruit, x1.3 if sacred, x1.4 for a commander,
              a further x1.01 rounded up if the commander is mounted,
              plus the mount's own basecost, and rounded down to a multiple
              of five at each step

The rounding is where most of the character is: `round` floors to a multiple
of 5, but `roundIfNeeded` leaves anything at or below 30 alone, which is why
cheap troops have exact prices and expensive commanders land on fives.

**Treat the gold result as close, not exact.** The player reports the inspector is
occasionally off by around five gold against the game, and this reproduces the
inspector rather than the game. `observed_unit_costs` — prices read from the
recruitment screen or a `.2h` queue — always wins where we have one.

Resource and recruitment-point costs are different.  The reference database
contains `binary_unit_costs`, extracted by calling the game's own calculators
in the 6.35 executable.  Those values are exact for that base-game build.  The
ported resource formula below remains useful for an unknown/modded unit, but it
is only a fallback because special attributes are not represented completely
in the inspector CSV.
"""
from __future__ import annotations

import sqlite3
from typing import Any

#: Gold per point of troop leadership. Not linear, and not interpolated —
#: a value outside this table is left uncosted rather than guessed.
LEADERSHIP_COST = {0: 10, 10: 15, 20: 20, 30: 20, 40: 30, 50: 30, 60: 30,
                   75: 30, 80: 60, 100: 60, 120: 80, 150: 100, 160: 100,
                   200: 150}

#: The first (highest) magic path costs more than each subsequent one.
PATH_COST_FIRST = {1: 30, 2: 90, 3: 150, 4: 210, 5: 270}
PATH_COST_REST = {1: 20, 2: 60, 3: 100, 4: 140, 5: 180}

PRIEST_COST = {1: 20, 2: 40, 3: 100, 4: 140}

PATHS = ("F", "A", "W", "E", "S", "D", "N", "G", "B")

#: Below this, `basecost` is the price outright rather than a modifier.
AUTOCALC_THRESHOLD = 9000

#: A sacred unit's mount is priced as sacred too — the x1.3 applies to the
#: mount's contribution as well as the rider's.
#:
#: This replaced a flat "+10 when mounted", which fitted the Knight of the
#: Chalice and the Paladin and then broke on the Royal Guard: it is mounted,
#: costs 50, and computed 60 with the surcharge. The three together separate
#: the hypotheses, because the Royal Guard is the only one that is mounted and
#: NOT sacred:
#:
#:     Knight of the Chalice   sacred, mount 35   26 + 35*1.3 = 71.5 -> 70
#:     Paladin                 sacred, mount 40  165 + 40*1.3 = 217  -> 215
#:     Royal Guard             plain,  mount 30   20 + 30     = 50   -> 50
#:
#: A flat +10 on sacred-and-mounted fits these three equally well and is not
#: distinguishable on this data; it is rejected only because multiplying is
#: what the rest of the formula does with `holy`, and an arbitrary constant
#: that happens to fit two samples is how the last version went wrong.
SACRED_MULTIPLIER = 1.3


def _round5(value: float) -> int:
    """Floor to a multiple of five, as the game displays prices."""
    return 5 * int(value // 5)


def _round5_up(value: float) -> int:
    return 5 * -(-int(value) // 5) if value % 5 == 0 else 5 * (int(value // 5) + 1)


def _round_if_needed(value: float) -> int:
    """Cheap things keep their exact price; anything over 30 lands on a five."""
    return _round5(value) if int(value) > 30 else int(value // 1)


def _int(row: Any, key: str) -> int:
    try:
        value = row[key]
    except (IndexError, KeyError):
        return 0
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


#: Which magic path each bit of a random-path mask selects. Verified against
#: the Grand Master, whose mask of 3456 is 2048+1024+256+128 — Astral, Earth,
#: Air and Fire — and whose price only comes out right with those four.
PATH_MASK_BITS = {128: 0, 256: 1, 512: 2, 1024: 3, 2048: 4,
                  4096: 5, 8192: 6, 16384: 7, 32768: 8}


def _paths_cost(levels_by_path: list[int]) -> float | None:
    """Price a set of path levels: the highest costs more than the rest."""
    levels = sorted((lv for lv in levels_by_path if lv > 0), reverse=True)
    total: float = 0
    for index, level in enumerate(levels):
        table = PATH_COST_FIRST if index == 0 else PATH_COST_REST
        if level not in table:
            return None
        total += table[level]
    return total


def _random_paths(unit: Any) -> list[list[int]]:
    """Every path spread a guaranteed random could produce.

    Returns one list of levels per possible outcome, or an empty list when the
    unit has no random at 100% chance — a random that only *might* happen is
    not priced at all, matching the game's own display.
    """
    options: list[list[int]] = []
    base = [_int(unit, p) for p in PATHS]
    for slot in range(1, 7):
        chance = _int(unit, f"rand{slot}")
        if chance != 100:
            continue
        levels = _int(unit, f"link{slot}") or 1
        mask = _int(unit, f"mask{slot}")
        for bit, index in PATH_MASK_BITS.items():
            if mask & bit:
                option = list(base)
                option[index] += levels
                options.append(option)
    return options


def compute_gold_cost(unit: Any, *, is_commander: bool,
                      mount: Any | None = None) -> int | None:
    """The displayed recruitment price, or None if it cannot be computed.

    `is_commander` matters more than anything else in the formula: a troop
    contributes nothing from leadership, paths, priesthood or spycraft, so its
    whole price is the `basecost` modifier. Ours comes from the nation lists —
    a unit in a `*_leader_types_by_nation` table is a commander.

    Returns None rather than a number when `leader` is a value the game's own
    table does not cover, because interpolating it would invent a price.
    """
    basecost = _int(unit, "basecost")
    if basecost <= AUTOCALC_THRESHOLD:
        return _round_if_needed(basecost)

    # `if (o.leader)` in JavaScript, where the CSV value is a STRING — so "0"
    # is truthy and a leader of 0 still costs 10. Reading it as a Python int
    # made that branch fall through and under-priced every Scout and Assassin
    # by exactly the 10 it should have contributed.
    has_leader = str(unit["leader"]) not in ("", "None") and unit["leader"] is not None
    leader = _int(unit, "leader")
    if has_leader and leader not in LEADERSHIP_COST:
        return None
    ldr_cost: float = LEADERSHIP_COST.get(leader, 0) if has_leader else 0
    ldr_cost += 10 * _int(unit, "inspirational")
    if _int(unit, "sailingshipsize") > 0:
        ldr_cost += 0.5 * ldr_cost

    base_paths = [_int(unit, p) for p in PATHS]
    randoms = _random_paths(unit)
    if randoms:
        # A guaranteed random is priced as three-quarters of its best possible
        # outcome and a quarter of its worst, rather than as an average. The
        # Grand Master is the case that shows it: F3 S2 with one guaranteed
        # random among Fire, Air, Earth and Astral gives a best of 270 (F4 S2)
        # and a worst of 230 (a new path at level 1), and .75/.25 of those is
        # exactly the 260 that produces his 520 gold.
        costs = [_paths_cost(option) for option in randoms]
        if any(c is None for c in costs):
            return None
        paths_cost: float = max(costs) * 0.75 + min(costs) * 0.25
    else:
        computed = _paths_cost(base_paths)
        if computed is None:
            return None
        paths_cost = computed
    research = _int(unit, "researchbonus")
    if paths_cost > 0 and research > 0:
        paths_cost += research * 5
    if research < 0:
        paths_cost -= 5
    if _int(unit, "fixforgebonus"):
        paths_cost += paths_cost * (_int(unit, "fixforgebonus") / 100)

    holiness = _int(unit, "H")
    if holiness and holiness not in PRIEST_COST:
        return None
    priest_cost = PRIEST_COST.get(holiness, 0) if holiness else 0

    spy_cost = 0
    if _int(unit, "spy") > 0:
        spy_cost += 40
    if _int(unit, "assassin") > 0:
        spy_cost += 40
    if _int(unit, "seduce") > 0 or _int(unit, "succubus") > 0:
        spy_cost += 60

    # The largest of the four counts in full and the rest at half — a commander
    # who is both a priest and a mage is not charged twice over for it.
    ranked = sorted([ldr_cost, paths_cost, priest_cost, spy_cost], reverse=True)
    cost = (ranked[0] + ranked[1] / 2 + ranked[2] / 2 + ranked[3] / 2
            if is_commander else 0)

    special = 0
    if _int(unit, "stealthy") > 0 and is_commander:
        special += 5
    if _int(unit, "autohealer") > 0 and is_commander:
        special += 50
    if _int(unit, "autodishealer") > 0 and is_commander:
        special += 20

    gold: float = int(cost + special) + basecost - 10000

    # `rt == 2` is the game's slow-to-recruit flag, which discounts the unit.
    if _int(unit, "rt") == 2 and is_commander:
        gold *= 0.9
    if _int(unit, "holy") > 0:
        gold *= 1.3

    mounted = _int(unit, "mountmnr") > 0
    if not is_commander:
        gold = _round_if_needed(gold)
    elif mounted:
        gold = _round5(gold * 1.4)
        gold = _round5_up(gold * 1.01)
    else:
        gold = _round5(gold * 1.4)

    if mounted and mount is not None:
        mount_base = _int(mount, "basecost")
        contribution = (mount_base - 10000 if mount_base > 1000 else mount_base)
        if _int(unit, "holy") > 0:
            contribution *= SACRED_MULTIPLIER
        gold = _round5(gold + contribution)
        gold = _round_if_needed(gold) if not is_commander else _round5(gold)
    return int(gold)


def gold_cost_for(reference: sqlite3.Connection, unit_id: int, *,
                  is_commander: bool) -> int | None:
    """Look a unit up and price it. Returns None if it cannot be computed."""
    reference.row_factory = sqlite3.Row
    unit = reference.execute("SELECT * FROM units WHERE id=?",
                             (unit_id,)).fetchone()
    if unit is None:
        return None
    mount = None
    if _int(unit, "mountmnr") > 0:
        mount = reference.execute("SELECT * FROM units WHERE id=?",
                                  (_int(unit, "mountmnr"),)).fetchone()
    return compute_gold_cost(unit, is_commander=is_commander, mount=mount)


def compute_resource_cost(unit: Any, weapons: list[Any],
                          armours: list[Any]) -> int:
    """A unit's resource cost — what a province's Resources are spent on.

    Ported from the same `MUnit.js`. Gold buys the unit; resources are the
    separate budget that actually caps how many a province can build in a turn,
    and they come almost entirely from equipment:

        rcost = the unit's own rcost, plus each weapon's and each armour's
                rcost scaled by ressize/3

    Only the LAST armour of each type counts, which is what the source does —
    a unit listing two body armours wears one.

    `ressize` defaults to 3, making the scaling a no-op for an ordinary human;
    a larger creature pays proportionally more for the same equipment.
    """
    total = float(_int(unit, "rcost"))
    ressize = _int(unit, "ressize") or 3
    for weapon in weapons:
        total += _int(weapon, "rcost") * ressize / 3
    by_type: dict[Any, Any] = {}
    for armour in armours:
        by_type[armour["type"] if "type" in armour.keys() else id(armour)] = armour
    for armour in by_type.values():
        total += _int(armour, "rcost") * ressize / 3
    # The source treats an absurd total as a marker rather than a number.
    return 1 if total > 60000 else int(total)


def resource_cost_for(reference: sqlite3.Connection, unit_id: int) -> int | None:
    """Look a unit up and price it in resources.

    Base-game values come from the executable's own `0x4627d0` calculator.
    Fall back to the inspector-data port for a database without that extraction
    (or for a modded unit absent from it).
    """
    reference.row_factory = sqlite3.Row
    try:
        exact = reference.execute(
            "SELECT resources FROM binary_unit_costs WHERE id=?",
            (unit_id,)).fetchone()
    except sqlite3.OperationalError:
        exact = None
    if exact is not None:
        return int(exact["resources"])
    unit = reference.execute("SELECT * FROM units WHERE id=?",
                             (unit_id,)).fetchone()
    if unit is None:
        return None
    weapons, armours = [], []
    for slot in range(1, 8):
        wid = _int(unit, f"wpn{slot}")
        if wid:
            row = reference.execute("SELECT * FROM weapons WHERE id=?",
                                    (wid,)).fetchone()
            if row is not None:
                weapons.append(row)
    for slot in range(1, 5):
        aid = _int(unit, f"armor{slot}")
        if aid:
            row = reference.execute("SELECT * FROM armors WHERE id=?",
                                    (aid,)).fetchone()
            if row is not None:
                armours.append(row)
    own = compute_resource_cost(unit, weapons, armours)
    mount_id = _int(unit, "mountmnr")
    if mount_id > 0:
        mount = resource_cost_for(reference, mount_id)
        if mount is not None:
            own += mount
    return own


def recruitment_point_cost_for(reference: sqlite3.Connection,
                               unit_id: int) -> int | None:
    """The full unit's recruitment-point cost, including its mount.

    Extracted from the game's full-unit wrapper, which calls the
    autocalculator at `0x462420` and adds an associated mount's cost.  There is
    deliberately no guessed fallback: the autocalculation also calls the
    sizeable combat-value routine at `0x4546c0`, and returning ``None`` for an
    unknown, modded, or stateful unit is safer than planning a queue with an
    invented cost.  Great Hag (586) proved that distinction matters: identical
    6.36 calls returned 60, 60, 61, 62, 60, so its extracted CSV cell is NULL.
    """
    reference.row_factory = sqlite3.Row
    try:
        row = reference.execute(
            "SELECT recruitment_points FROM binary_unit_costs WHERE id=?",
            (unit_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row["recruitment_points"] is None:
        return None
    return int(row["recruitment_points"])
