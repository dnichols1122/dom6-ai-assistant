"""The ported recruitment-cost formula, against prices read off the game.

Every case here is a price the player or a `.2h` queue actually showed, which
is the only reason to trust a port of somebody else's JavaScript at all. The
two known misses are asserted as misses rather than quietly excluded: a test
suite that drops its failures is how an approximation gets mistaken for a fact.
"""
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.reference.unit_cost import (
    compute_gold_cost,
    gold_cost_for,
    recruitment_point_cost_for,
)

REF = Path("knowledge/reference/reference.sqlite3")

#: unit id -> (name, price seen in game, is a commander)
OBSERVED = {
    218: ("Crossbowman", 10, False),
    219: ("Swordsman", 10, False),
    426: ("Scout", 35, True),
    3638: ("Falconeer", 40, True),
    148: ("Friar", 60, True),
    134: ("Royal Guard", 50, False),
    3010: ("Architect", 60, True),
    225: ("Initiate", 65, True),
    428: ("Assassin", 80, True),
    2107: ("Troubadour", 110, True),
    149: ("Inquisitor", 190, True),
    224: ("Witch Hunter", 260, True),
    222: ("High Inquisitor", 285, True),
    223: ("Grand Master", 520, True),
}

#: The three mounted units, which together separate two hypotheses that a
#: smaller sample could not. Two are sacred and one is not.
MOUNTED = {135: ("Knight of the Chalice", 70, False),   # sacred, mount 35
           440: ("Paladin", 215, True),                 # sacred, mount 40
           134: ("Royal Guard", 50, False)}             # plain,  mount 30


@pytest.fixture
def ref():
    if not REF.exists():
        pytest.skip("reference database absent")
    conn = sqlite3.connect(REF)
    conn.row_factory = sqlite3.Row
    return conn


def test_binary_cost_extraction_covers_every_base_unit(ref):
    units = ref.execute("SELECT count(*) FROM units").fetchone()[0]
    costs = ref.execute(
        "SELECT count(DISTINCT id) FROM binary_unit_costs").fetchone()[0]
    assert costs == units == 4091


def test_stateful_recruitment_point_cost_is_not_reported_as_exact(ref):
    """Great Hag's native 6.36 calculator varies on identical calls."""
    assert recruitment_point_cost_for(ref, 586) is None


@pytest.mark.parametrize("unit_id", sorted(OBSERVED))
def test_computed_price_matches_the_game(ref, unit_id):
    name, expected, is_commander = OBSERVED[unit_id]
    got = gold_cost_for(ref, unit_id, is_commander=is_commander)
    assert got == expected, f"{name}: computed {got}, game says {expected}"


@pytest.mark.parametrize("unit_id", sorted(MOUNTED))
def test_mounted_units_price_their_mount_correctly(ref, unit_id):
    """A sacred unit's mount is priced as sacred; a plain one's is not.

    An earlier version added a flat ten to every mounted unit. That fitted the
    two sacred ones and broke the Royal Guard, which is mounted, plain, and
    costs 50 — it computed 60. The Royal Guard is the whole reason this is a
    multiplier rather than a constant.
    """
    name, expected, is_commander = MOUNTED[unit_id]
    got = gold_cost_for(ref, unit_id, is_commander=is_commander)
    assert got == expected, f"{name}: computed {got}, game says {expected}"


def test_a_plain_mounted_unit_gets_no_sacred_bonus_on_its_mount(ref):
    """The case that falsified the flat surcharge, stated on its own."""
    guard = ref.execute("SELECT * FROM units WHERE id=134").fetchone()
    destrier = ref.execute("SELECT * FROM units WHERE id=3897").fetchone()
    assert guard["holy"] in (None, "", 0)
    # 20 from basecost, plus the Destrier's 30 at face value.
    assert compute_gold_cost(guard, is_commander=False, mount=destrier) == 50


def test_the_sacred_multiplier_reaches_the_mount(ref):
    """The rider and the mount are both priced as sacred."""
    from dom6_assistant.reference import unit_cost as uc
    assert uc.SACRED_MULTIPLIER == 1.3
    knight = ref.execute("SELECT * FROM units WHERE id=135").fetchone()
    destrier = ref.execute("SELECT * FROM units WHERE id=3582").fetchone()
    # 20 base, x1.3 sacred = 26, then + 35*1.3 for the Destrier = 71.5,
    # floored to a multiple of five = 70. Asserted as that chain rather than
    # as a difference, because the floor eats some of it.
    assert compute_gold_cost(knight, is_commander=False, mount=None) == 26
    assert compute_gold_cost(knight, is_commander=False, mount=destrier) == 70


def test_a_troop_is_priced_only_by_its_basecost_modifier(ref):
    """Leadership, paths and priesthood cost a commander and not a troop.

    The Knight of the Chalice has leadership 50 and is still a troop, so none
    of it counts — which is what makes `is_commander` the most important input
    to the whole formula.
    """
    unit = ref.execute("SELECT * FROM units WHERE id=218").fetchone()
    assert compute_gold_cost(unit, is_commander=False) == 10


def test_the_same_unit_costs_more_as_a_commander(ref):
    unit = ref.execute("SELECT * FROM units WHERE id=149").fetchone()
    as_troop = compute_gold_cost(unit, is_commander=False)
    as_commander = compute_gold_cost(unit, is_commander=True)
    assert as_commander > as_troop


def test_a_leader_value_of_zero_still_costs(ref):
    """"0" is a truthy string in the JavaScript this was ported from.

    Reading it as a Python integer skipped the branch and under-priced every
    Scout and Assassin by exactly the 10 leadership should have contributed.
    """
    scout = ref.execute("SELECT * FROM units WHERE id=426").fetchone()
    assert int(scout["leader"]) == 0
    assert compute_gold_cost(scout, is_commander=True) == 35


def test_a_guaranteed_random_path_is_weighted_not_averaged(ref):
    """Three quarters of the best outcome, one quarter of the worst.

    The Grand Master's F3 S2 plus one random among Fire, Air, Earth and Astral
    gives a best of 270 and a worst of 230; .75/.25 of those is 260, and only
    that produces his real price of 520. An average would give 250 and 500.
    """
    unit = ref.execute("SELECT * FROM units WHERE id=223").fetchone()
    assert compute_gold_cost(unit, is_commander=True) == 520


def test_an_unknown_leadership_value_returns_none_rather_than_guessing(ref):
    """The table is not linear, so interpolating it would invent a price."""
    unit = dict(ref.execute("SELECT * FROM units WHERE id=149").fetchone())
    unit["leader"] = 137                      # not a value the game's table has
    assert compute_gold_cost(unit, is_commander=True) is None


#: What each of the eight Marignon fort troops costs in RESOURCES, returned by
#: the game's own 0x4627d0 calculator. Mounted units include their mount's
#: equipment, which the old inspector port omitted.
RESOURCE_COSTS = {218: 8, 219: 23, 220: 22, 221: 20,
                  133: 24, 217: 4, 134: 66, 135: 58}

RECRUITMENT_POINT_COSTS = {218: 9, 219: 9, 220: 9, 221: 9,
                           133: 18, 217: 5, 134: 51, 135: 51}


@pytest.mark.parametrize("unit_id", sorted(RESOURCE_COSTS))
def test_resource_cost_is_stable(ref, unit_id):
    from dom6_assistant.reference.unit_cost import resource_cost_for
    assert resource_cost_for(ref, unit_id) == RESOURCE_COSTS[unit_id]


@pytest.mark.parametrize("unit_id", sorted(RECRUITMENT_POINT_COSTS))
def test_recruitment_point_cost_matches_the_binary(ref, unit_id):
    assert recruitment_point_cost_for(ref, unit_id) == (
        RECRUITMENT_POINT_COSTS[unit_id])


def test_the_resource_cap_predicts_what_the_palisade_refused(ref):
    """The Obsidian Waste's cap comes out exactly right.

    113 resources; the first six queued units cost 101, and the seventh would
    have made 167. It was left in the queue, which is what the game did.
    """
    from dom6_assistant.reference.unit_cost import resource_cost_for
    order = [218, 219, 220, 221, 133, 217, 134, 135]
    built = sum(resource_cost_for(ref, u) for u in order[:6])
    assert built == 101
    assert built + resource_cost_for(ref, 134) > 113


def test_resources_explain_what_the_castle_refused(ref):
    """Marignon can build seven for 167; the eighth reaches 225 against 206."""
    from dom6_assistant.reference.unit_cost import resource_cost_for
    order = [218, 219, 220, 221, 133, 217, 134, 135]
    built = sum(resource_cost_for(ref, u) for u in order[:7])
    assert built == 167
    assert built + resource_cost_for(ref, 135) == 225
    assert built <= 206 < built + resource_cost_for(ref, 135)
