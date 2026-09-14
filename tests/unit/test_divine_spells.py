"""Divine spell eligibility, confirmed against the game's own list.

Filtering on Holy level alone offers 25 spells where the game offers 10, which
is why this was parked. The God Path Restriction is what was missing.
"""
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.reference import divine_spells as DV

REFERENCE = Path("knowledge/reference/reference.sqlite3")
MARIGNON = 61
SUGAAR = {"F": 6, "A": 6}

#: Read off the game's Divine list for Magnino, a Holy 3 High Inquisitor.
SCREEN_CASTABLE = {"Blessing", "Ashes to Ashes", "Sermon of Courage",
                   "Smite Demon", "Holy Word", "Holy Avenger",
                   "Divine Blessing", "Heavenly Fire"}
SCREEN_GREYED = {"Fanaticism", "Divine Channeling"}


@pytest.fixture(scope="module")
def ref():
    if not REFERENCE.exists():
        pytest.skip("reference database absent")
    conn = sqlite3.connect(REFERENCE)
    conn.row_factory = sqlite3.Row
    return conn


def test_reproduces_the_screen_exactly(ref):
    spells = DV.divine_spells(ref, MARIGNON, 3, SUGAAR)
    castable = {s.name for s in spells if s.castable}
    greyed = {s.name for s in spells if not s.castable}
    assert castable == SCREEN_CASTABLE
    assert greyed == SCREEN_GREYED


def test_holy_level_only_would_overlist(ref):
    """The failure this exists to prevent, measured.

    Every Divine spell our nation is not excluded from, filtered by Holy level
    alone, is far more than the game offers.
    """
    naive = [row for row in ref.execute(
        "SELECT id, pathlevel1 FROM spells WHERE school=?", (DV.DIVINE_SCHOOL,))
        if int(row["pathlevel1"] or 0) <= 3]
    assert len(naive) > 2 * len(SCREEN_CASTABLE | SCREEN_GREYED)


def test_a_lower_priest_sees_the_same_list_with_fewer_castable(ref):
    """The list is national; the Holy level only gates casting."""
    low = DV.divine_spells(ref, MARIGNON, 1, SUGAAR)
    high = DV.divine_spells(ref, MARIGNON, 3, SUGAAR)
    assert {s.name for s in low} == {s.name for s in high}
    assert {s.name for s in low if s.castable} == {"Blessing", "Ashes to Ashes"}


def test_the_god_path_selects_one_spell_per_elemental_set(ref):
    """A Fire god gets the Fire pair; an Air god gets the Air pair."""
    fire = {s.name for s in DV.divine_spells(ref, MARIGNON, 9, {"F": 4})}
    air = {s.name for s in DV.divine_spells(ref, MARIGNON, 9, {"A": 4})}
    assert "Ashes to Ashes" in fire and "Heavenly Fire" in fire
    assert "Sacred Wind" in air and "Heavenly Strike" in air
    assert not (fire & {"Sacred Wind", "Heavenly Strike"})
    assert not (air & {"Ashes to Ashes", "Heavenly Fire"})
    # the unrestricted eight are common to both
    assert "Blessing" in fire and "Blessing" in air


def test_minus_one_is_a_real_god_path_not_a_placeholder(ref):
    """Smite carries god path -1 and the game does not offer it to us.

    Treating -1 as "no restriction" put Smite on the list. It evidently
    selects a god with no magic path; only the ABSENCE of the attribute means
    unrestricted.
    """
    names = {s.name for s in DV.divine_spells(ref, MARIGNON, 9, SUGAAR)}
    assert "Smite" not in names
    assert "Banishment" not in names        # god path [-1, 8]


def test_pathless_god_receives_minus_one_spells(ref):
    spells = DV.divine_spells(ref, MARIGNON, 9, {})
    by_name = {spell.name: spell for spell in spells}

    assert by_name["Smite"].god_path == "pathless"
    assert by_name["Banishment"].god_path == "pathless"
    assert "Ashes to Ashes" not in by_name

    blood = {
        spell.name: spell
        for spell in DV.divine_spells(ref, MARIGNON, 9, {"B": 5})
    }
    assert blood["Banishment"].god_path == "B"


def test_unholy_variants_are_excluded_by_nation(ref):
    ours = {s.name for s in DV.divine_spells(ref, MARIGNON, 9, SUGAAR)}
    assert not any(name.startswith("Unholy") for name in ours)


def test_equal_highest_paths_use_confirmed_gem_path_priority(ref):
    """Controlled pretender-builder changes prove F > A > W ... > B."""
    assert DV.dominant_god_path({"W": 4}) == 2
    assert DV.dominant_god_path({"A": 4, "W": 4}) == 1
    assert DV.dominant_god_path({"F": 4, "A": 4, "W": 4}) == 0
    assert DV.dominant_god_path({"F": 3, "A": 4, "W": 4}) == 1
    assert DV.dominant_god_path({"F": 3, "A": 3, "W": 4}) == 2
    assert DV.dominant_god_path({"F": 4, "A": 4, "W": 4, "B": 4}) == 0
    assert DV.dominant_god_path({"F": 4, "A": 4, "W": 4, "B": 5}) == 8
    # Check the tail of the same priority order, not just its first three.
    assert DV.dominant_god_path(dict.fromkeys("ESDNGB", 4)) == 3
    assert DV.dominant_god_path({}) is None


def test_worn_equipment_raises_the_effective_holy_level(ref):
    """The Divine list has entries at Holy 4 and 5 that no ordinary priest of
    ours reaches unaided, and five base-game items grant H+1.

    Crown of the Shah lifts a Holy 3 priest to Fanaticism, and the spell is
    flagged so the difference between the chassis and the kit stays visible.
    """
    shah = ref.execute("SELECT * FROM items WHERE id=225").fetchone()
    assert shah["H"] == 1

    bare = DV.divine_spells(ref, MARIGNON, 3, SUGAAR)
    kitted = DV.divine_spells(ref, MARIGNON, 3, SUGAAR, item_rows=[shah])

    assert "Fanaticism" not in {s.name for s in bare if s.castable}
    fanaticism = next(s for s in kitted if s.name == "Fanaticism")
    assert fanaticism.castable and fanaticism.needs_equipment
    # Divine Channeling needs Holy 5 and is still out of reach at 3 + 1
    assert not next(s for s in kitted if s.name == "Divine Channeling").castable


def test_a_spell_within_the_base_level_is_not_flagged_as_equipment(ref):
    shah = ref.execute("SELECT * FROM items WHERE id=225").fetchone()
    kitted = DV.divine_spells(ref, MARIGNON, 3, SUGAAR, item_rows=[shah])
    blessing = next(s for s in kitted if s.name == "Blessing")
    assert blessing.castable and not blessing.needs_equipment


def test_holy_from_items_counts_only_the_holy_column(ref):
    """Crown of the Shah also grants leadership; that must not leak in here."""
    shah = ref.execute("SELECT * FROM items WHERE id=225").fetchone()
    assert DV.holy_from_items([shah]) == 1
    assert DV.holy_from_items([]) == 0
