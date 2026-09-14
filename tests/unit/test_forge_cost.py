"""Forge costs from the game's screens and the client cost routine.

The player opened the forge list at Construction 1 and reported all ten
offered items, then opened the complete per-tier lists reachable from the
Construction school and reported the cost at every path level the base game
uses. These tests encode those readings, not a formula fitted to them.
"""
import sqlite3
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.orders.orders_2h import find_order_blocks
from dom6_assistant.reference import forge_cost as FC
from dom6_assistant.agent.read_tools import (
    forge_cost_for_commander,
    forgeable_item_ids,
)
from dom6_assistant.file_reader.formats import trn as T


REFERENCE = Path("knowledge/reference/reference.sqlite3")


@pytest.fixture(scope="module")
def items():
    if not REFERENCE.exists():
        pytest.skip("reference database absent")
    conn = sqlite3.connect(REFERENCE)
    conn.row_factory = sqlite3.Row
    return conn


def _item(items, item_id):
    row = items.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        pytest.skip(f"item {item_id} absent from the reference data")
    return row


#: Everything Bretaigne was offered at Construction 1, all at path level 1.
#: Three Fire and seven Astral, spanning weapon, shield, helm, armour and
#: miscellaneous — which is what rules out a per-item or per-slot cost.
LEVEL_ONE_SCREEN = [
    (1, "fire"), (309, "fire"), (317, "fire"),
    (5, "astral"), (6, "astral"), (7, "astral"), (162, "astral"),
    (186, "astral"), (232, "astral"), (313, "astral"),
]


@pytest.mark.parametrize(("item_id", "path"), LEVEL_ONE_SCREEN)
def test_every_level_one_item_on_the_screen_cost_five(items, item_id, path):
    assert FC.forge_cost(_item(items, item_id)).listed == {path: 5}


def test_the_level_curve_is_the_client_lookup_table():
    assert FC.GEMS_BY_PATH_LEVEL == {
        0: 0, 1: 5, 2: 10, 3: 15, 4: 25, 5: 40,
        6: 60, 7: 80, 8: 100, 9: 120, 10: 140,
    }


def test_a_two_path_item_pays_each_path_in_full(items):
    """Sword of Justice is Fire 3 / Astral 3 and the screen read 15 and 15.

    A combined or averaged total would have shown something else, so this is
    the reading that establishes paths are charged independently.
    """
    assert FC.forge_cost(_item(items, 100)).listed == {"fire": 15, "astral": 15}


def test_the_national_rebate_is_one_gem_per_path(items):
    """The binary distinguishes the rule that level-one orders could not."""
    row = _item(items, 100)
    ours = FC.forge_cost(row, nation_id=61)
    assert ours.rebated is True
    assert ours.charged == {"fire": 14, "astral": 14}
    assert ours.listed == {"fire": 15, "astral": 15}


def test_another_nation_pays_full_price(items):
    other = FC.forge_cost(_item(items, 100), nation_id=76)
    assert other.rebated is False
    assert other.charged == other.listed == {"fire": 15, "astral": 15}


def test_the_rebate_fields_name_nation_ids(items):
    """The link the player deduced had to exist, already in the scraped data.

    They reported Sword of Justice flagged as reduced-cost for Marignon; the
    item's `nationrebate1` is 61, which is MA Marignon's nation id.
    """
    assert 61 in FC.rebate_nations(_item(items, 100))
    assert 61 not in FC.rebate_nations(_item(items, 1))


def test_every_item_in_the_game_is_costable(items):
    """529 items, no refusals, and no path level outside the observed table.

    If the game ever ships an item needing level 8, this fails rather than
    inventing a price for it.
    """
    costed = 0
    for row in items.execute("SELECT * FROM items"):
        cost = FC.forge_cost(row, nation_id=61)
        assert cost.listed and all(v > 0 for v in cost.listed.values())
        costed += 1
    assert costed > 500


def test_a_path_level_beyond_the_client_table_is_refused(items):
    row = dict(_item(items, 1))
    row["mainlevel"] = 11
    with pytest.raises(FC.ForgeCostUnknown, match="only levels"):
        FC.forge_cost(row)


def test_item_specific_path_multipliers_precede_smith_bonuses(items):
    five_elements = FC.forge_cost(_item(items, 138))
    assert five_elements.listed == {"fire": 2, "water": 2}
    assert five_elements.charged == five_elements.listed

    soul_contract = FC.forge_cost(_item(items, 348))
    assert soul_contract.listed == {"blood": 75, "fire": 5}


def test_fixed_and_percentage_bonuses_follow_client_rounding(items):
    fire_sword = _item(items, 1)
    assert FC.forge_cost(
        fire_sword, forge_bonus_percent=20).charged == {"fire": 4}
    assert FC.forge_cost(
        fire_sword, fixed_forge_bonus=2).charged == {"fire": 3}
    assert FC.forge_cost(
        fire_sword, forge_bonus_percent=20,
        fixed_forge_bonus=2).charged == {"fire": 2}
    assert FC.forge_cost(
        fire_sword, forge_bonus_percent=-20).charged == {"fire": 6}


def test_fixed_bonus_is_split_primary_first_on_two_path_items(items):
    cost = FC.forge_cost(_item(items, 138), fixed_forge_bonus=3)
    # This item has an adjusted base of two per path. Fixed 3 splits 2/1,
    # then each path is clamped to the one-gem minimum.
    assert cost.charged == {"fire": 1, "water": 1}

    cost = FC.forge_cost(_item(items, 169), fixed_forge_bonus=3)
    assert cost.charged == {"astral": 13, "earth": 9}


def test_positive_percentage_bonus_is_capped_at_eighty(items):
    cost = FC.forge_cost(_item(items, 169), forge_bonus_percent=500)
    assert cost.forge_bonus_percent == 500
    assert cost.charged == {"astral": 3, "earth": 2}


def test_noforgebonus_and_artifacts_suppress_smithing_not_rebate(items):
    contract = FC.forge_cost(
        _item(items, 348), forge_bonus_percent=20, fixed_forge_bonus=4)
    assert contract.bonuses_suppressed
    assert contract.charged == contract.listed == {"blood": 75, "fire": 5}

    artifact = FC.forge_cost(
        _item(items, 283), forge_bonus_percent=20, fixed_forge_bonus=4)
    assert artifact.bonuses_suppressed
    assert artifact.charged == artifact.listed


def test_yearning_halves_each_path_rounding_up(items):
    row = dict(_item(items, 100))
    row["constlevel"] = 7
    cost = FC.forge_cost(row, yearning=True)
    assert cost.listed == {"fire": 8, "astral": 8}
    assert cost.charged == cost.listed


def _forge_context(items, *, globals=(), site_ids=(), item_states=None):
    province = SimpleNamespace(site_ids=site_ids)
    view = SimpleNamespace(
        nation_id=61,
        parsed=SimpleNamespace(
            global_effects=list(globals),
            research_levels=[0, 0, 0, 9, 0, 0, 0],
            item_states=item_states,
        ),
        province=lambda _province_id: province,
    )
    return SimpleNamespace(
        reference_db=items,
        game_db=sqlite3.connect(":memory:"),
        view=view,
        h2_path=None,
        game_id=1,
        turn=1,
    )


def test_chassis_percentage_bonus_is_consumed_by_dynamic_port(items):
    ctx = _forge_context(items)
    titan = SimpleNamespace(type_id=1230, commander_id=1, province_id=None)
    cost, details = forge_cost_for_commander(ctx, titan, _item(items, 1))
    assert cost.charged == {"fire": 4}
    assert details["combined_percentage_before_cap"] == 20
    assert any("Titan of the Forge" in source for source in details["sources"])


def test_world_and_province_forge_modifiers_are_combined(items):
    ancient_forge = SimpleNamespace(spell_id=1095, caster_nation_id=61)
    dampening = SimpleNamespace(spell_id=1369, caster_nation_id=99)
    ctx = _forge_context(
        items, globals=(ancient_forge, dampening), site_ids=(428, 641))
    ordinary = SimpleNamespace(type_id=126, commander_id=1, province_id=10)
    cost, details = forge_cost_for_commander(ctx, ordinary, _item(items, 1))
    # +20 own global, the strongest +20 Construction site (not both), and
    # -20 Elemental Dampening.
    assert details["combined_percentage_before_cap"] == 20
    assert cost.charged == {"fire": 4}
    assert len(details["sources"]) == 3


def test_live_yearning_state_halves_cost_and_existing_artifact_is_withheld(items):
    states = [T.ITEM_STATE_UNMADE] * T.ITEM_STATE_COUNT
    states[451] = T.ITEM_STATE_YEARNING
    ctx = _forge_context(items, item_states=tuple(states))
    forger = SimpleNamespace(
        type_id=126, commander_id=1, province_id=None,
        paths={"D": 4},
    )
    cost, details = forge_cost_for_commander(
        ctx, forger, _item(items, 451))
    assert cost.listed == cost.charged == {"death": 5}
    assert cost.yearning is True
    assert details["artifact_state"]["status"] == "yearning"
    assert "yearning artifact" in details["sources"][-1]
    assert 451 in forgeable_item_ids(ctx, forger)

    states[451] = 1
    ctx.view.parsed.item_states = tuple(states)
    assert 451 not in forgeable_item_ids(ctx, forger)


# -- the .2h side: what a forge order actually reserves --------------------

SNAPSHOTS = Path("knowledge/snapshots")
BRETAIGNE = 126


def test_a_forge_order_stores_item_and_cost_where_rituals_store_theirs():
    """Bretaigne set to forge a Fire Sword, one controlled turn-30 save.

    The order reuses the ritual layout exactly: the shared +116 parameter
    holds the item id rather than a spell id, and +120 holds the gem cost.
    """
    path = SNAPSHOTS / "t30-forge-firesword" / "mid_marignon.2h"
    if not path.exists():
        pytest.skip("forge snapshot absent")
    data = path.read_bytes()
    block = find_order_blocks(data)[BRETAIGNE]

    assert block.order_code == 11
    assert struct.unpack_from("<H", data, block.name_end + 116)[0] == 1
    assert struct.unpack_from("<I", data, block.name_end + 120)[0] == 5


def test_the_reservation_matches_the_modelled_cost():
    """Fire fell 39 -> 34 for a 5-gem item: the pool carries the reservation.

    This is the same national remaining-gem array rituals write, so a forge
    writer has to reconcile it the same way or the two silently disagree.
    """
    before = SNAPSHOTS / "t30-merc-bid-two-companies" / "mid_marignon.2h"
    after = SNAPSHOTS / "t30-forge-firesword" / "mid_marignon.2h"
    if not (before.exists() and after.exists()):
        pytest.skip("forge snapshots absent")
    gems_before = dict(zip(H2.GEM_PATHS, H2.gem_remaining(before.read_bytes())))
    gems_after = dict(zip(H2.GEM_PATHS, H2.gem_remaining(after.read_bytes())))

    assert gems_before["fire"] - gems_after["fire"] == 5
    assert {k: v for k, v in gems_before.items() if k != "fire"} == {
        k: v for k, v in gems_after.items() if k != "fire"}


def test_every_forge_order_ever_saved_agrees_with_the_model(items):
    """Every forge order in every snapshot, checked against `charged`.

    This asserted `listed` until a rebated order existed to tell the two
    apart: the orders from turns 15-30 are all unrebated, where listed and
    charged are equal, so either assertion passed. Holy Scourge at turn 33
    reserves 4 against a listed 5 and separates them.
    """
    seen = 0
    for path in sorted(SNAPSHOTS.rglob("mid_marignon.2h")):
        data = path.read_bytes()
        try:
            blocks = find_order_blocks(data)
        except Exception:
            continue
        for block in blocks.values():
            if block.order_code != 11:
                continue
            item_id = struct.unpack_from("<H", data, block.name_end + 116)[0]
            stored_primary = struct.unpack_from(
                "<I", data, block.name_end + 120)[0]
            stored_secondary = struct.unpack_from(
                "<I", data, block.name_end + 124)[0]
            row = items.execute("SELECT * FROM items WHERE id=?",
                                (item_id,)).fetchone()
            assert row is not None, f"{path}: forge of unknown item {item_id}"
            cost = FC.forge_cost(row, nation_id=61)
            charged = list(cost.charged.values())
            assert stored_primary == charged[0], path
            assert stored_secondary == (charged[1] if len(charged) == 2 else 0), path
            assert stored_primary + stored_secondary == cost.total_charged, path
            seen += 1
    assert seen >= 6


def test_a_rebated_order_reserves_the_discounted_cost():
    """Turn 33, two mages, two items, each writing its own field.

    Holy Scourge is Fire 1 and rebated to us: its cost page shows 5 and the
    order reserves 4. Mercybrand is Fire 2, Marignon-only but NOT rebated, and
    reserves the flat 10. Fire fell 46 -> 32 across the pair, exactly 4 + 10.

    This is the reading the writer depends on. Reserving the listed price
    instead would leave the gem pool 20% adrift on every national item, and
    the file would still look entirely valid.
    """
    before = SNAPSHOTS / "t33-construction3-clean" / "mid_marignon.2h"
    after = SNAPSHOTS / "t33-forge-rebate" / "mid_marignon.2h"
    if not (before.exists() and after.exists()):
        pytest.skip("Construction 3 forge snapshots absent")
    data = after.read_bytes()
    blocks = find_order_blocks(data)

    holy = blocks[126]
    assert struct.unpack_from("<H", data, holy.name_end + 116)[0] == 11
    assert struct.unpack_from("<I", data, holy.name_end + 120)[0] == 4

    mercy = blocks[314]
    assert struct.unpack_from("<H", data, mercy.name_end + 116)[0] == 135
    assert struct.unpack_from("<I", data, mercy.name_end + 120)[0] == 10

    fire_before = dict(zip(H2.GEM_PATHS, H2.gem_remaining(before.read_bytes())))["fire"]
    fire_after = dict(zip(H2.GEM_PATHS, H2.gem_remaining(data)))["fire"]
    assert fire_before - fire_after == 14


def test_the_reserved_cost_is_what_the_model_calls_charged(items):
    """The model reproduces both reservations, and the pair discriminates.

    Holy Scourge's listed and charged differ (5 vs 4), so a model returning
    the wrong one fails here; Mercybrand's agree, so it cannot mask a mistake.
    """
    holy = FC.forge_cost(_item(items, 11), nation_id=61)
    assert (holy.total_listed, holy.total_charged) == (5, 4)
    assert holy.rebated is True

    mercy = FC.forge_cost(_item(items, 135), nation_id=61)
    assert (mercy.total_listed, mercy.total_charged) == (10, 10)
    assert mercy.rebated is False


def test_restricted_and_rebated_are_independent(items):
    """Marignon-only is not Marignon-discounted, and the pair proves it.

    Mercybrand is restricted to nation 61 and pays full price; Holy Scourge is
    unrestricted and discounted. Treating one flag as implying the other would
    misprice both.
    """
    mercy = _item(items, 135)
    assert mercy["restricted1"] == 61
    assert 61 not in FC.rebate_nations(mercy)

    holy = _item(items, 11)
    assert not holy["restricted1"]
    assert 61 in FC.rebate_nations(holy)


def test_nation_restrictions_are_enforced_independently(items):
    mercy = _item(items, 135)
    crown = _item(items, 225)

    assert FC.restricted_nations(mercy) == {61}
    assert FC.nation_can_forge(mercy, 61)
    assert not FC.nation_can_forge(mercy, 105)
    assert FC.restricted_nations(crown) == {105}
    assert not FC.nation_can_forge(crown, 61)
