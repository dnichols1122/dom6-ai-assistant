"""Leadership capacity: chassis value plus worn items.

The item term is not optional, and treating it as such produced a false
counterexample. Nergash's chassis carries `leader 10` and no undead leadership
at all, yet the game handed him 125 Longdead — because his company equips him
with a Crown of Bones, which grants 150 undead leadership and is named in the
mercenary table's own `item1` column.
"""
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.reference.leadership import (
    ADVANCED_FORMATION_MIN_LEADERSHIP,
    available_formations, leadership_for, troop_pool,
)

REFERENCE = Path("knowledge/reference/reference.sqlite3")
NERGASH_CHASSIS, LONGDEAD, CROSSBOWMAN = 310, 195, 218
CROWN_OF_BONES = 200


@pytest.fixture(scope="module")
def ref():
    if not REFERENCE.exists():
        pytest.skip("reference database absent")
    conn = sqlite3.connect(REFERENCE)
    conn.row_factory = sqlite3.Row
    return conn


def _unit(ref, unit_id):
    return ref.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()


def _item(ref, item_id):
    return ref.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def test_the_bare_chassis_could_not_lead_the_company(ref):
    """This is what made it look like the model was wrong."""
    bare = leadership_for(_unit(ref, NERGASH_CHASSIS))
    assert bare.undead == 0
    assert 125 > bare.undead


def test_the_crown_explains_the_whole_gap(ref):
    """150 undead leadership, and the company arrived with 125 Longdead."""
    crowned = leadership_for(_unit(ref, NERGASH_CHASSIS),
                             [_item(ref, CROWN_OF_BONES)])
    assert crowned.undead == 150
    assert crowned.normal == 10          # unchanged; the crown grants no normal
    assert 125 <= crowned.undead
    assert crowned.from_items == {"Crown of Bones (undead)": 150}


def test_the_mercenary_table_names_the_item(ref):
    """The evidence was in the data all along, in the company's own record."""
    row = ref.execute(
        "SELECT bossname, com, item1 FROM mercenary WHERE id=62").fetchone()
    assert row["com"] == NERGASH_CHASSIS
    assert row["item1"] == "Crown of Bones"


def test_troops_draw_on_the_pool_matching_what_they_are(ref):
    assert troop_pool(_unit(ref, LONGDEAD)) == "undead"
    assert troop_pool(_unit(ref, CROSSBOWMAN)) == "normal"


def test_items_stack_across_pools(ref):
    """Crown of the Shah grants all three at once."""
    shah = _item(ref, 225)
    combined = leadership_for(_unit(ref, CROSSBOWMAN), [shah])
    assert combined.normal == 50 + 150
    assert combined.undead == 50
    assert combined.magic == 50


def test_a_commander_with_no_items_is_just_the_chassis(ref):
    plain = leadership_for(_unit(ref, 440))          # Paladin
    assert plain.normal == 100
    assert plain.from_items == {}


@pytest.mark.parametrize(
    ("experience", "bonus"),
    [(0, 0), (15, 25), (50, 75), (100, 125), (200, 175), (400, 225)],
)
def test_experience_adds_regular_leadership(ref, experience, bonus):
    base = _unit(ref, 219)  # Swordsman, regular leadership 50
    result = leadership_for(base, experience=experience)
    assert result.normal == 50 + bonus
    assert result.from_experience == bonus
    assert result.undead == int(base["undeadleader"] or 0)


def test_final_normal_leadership_unlocks_advanced_formations():
    assert ADVANCED_FORMATION_MIN_LEADERSHIP == 80
    assert available_formations(79) == {"box", "skirmish"}
    assert available_formations(80) == {
        "line", "sparse_line", "double_line", "box", "skirmish",
    }
