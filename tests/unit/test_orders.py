"""Tests for the .2h order table.

Every value here came from a controlled experiment against the running game,
one field changed per save. They are regression pins for the write path, which
is the part of this project where a mistake is silent: a wrong order does not
error, it resolves into a turn that did something else.
"""
from pathlib import Path

import pytest

from dom6_assistant.orders import (OrdersEditor, OrderTableNotLocated,
                                   find_order_blocks, read_orders)
from dom6_assistant.orders.orders_2h import (
    OFF_DESTINATION, OFF_ORDER_CODE, ORDER_CODES, RITUAL_ORDER_CODES,
    read_ritual_fields)
from dom6_assistant.orders import orders_2h as O
from dom6_assistant.file_reader.formats import units as U
from ..conftest import require_corpus

SNAP = Path(__file__).resolve().parents[2] / "knowledge" / "snapshots"
T1 = SNAP / "t1-orders" / "mid_marignon.2h"
T3 = SNAP / "t3-patrol-seduce" / "mid_marignon.2h"
T3B = SNAP / "t3-baseline" / "mid_marignon.2h"

DAPAMORT, ESTORGANT, BRUISE = 116, 182, 308

needs_snap = pytest.mark.skipif(not T1.exists(), reason="order snapshots absent")
needs_t3 = pytest.mark.skipif(not T3.exists(), reason="turn 3 snapshots absent")
TROOP_ASSIGNMENT = (
    SNAP / "t30-ritual-augury-copper-canyons" / "mid_marignon.2h")
NEW_SQUAD = SNAP / "t30-troop-assignment-bruise" / "mid_marignon.2h"
CAELUM_ATTACHED = SNAP / "example_game_2" / "t3-auto" / "early_caelum.2h"
CAELUM_PARTIAL_GARRISON = (
    SNAP / "example_game_2" / "t3-auto-2" / "early_caelum.2h")
CAELUM_EMPTY_SQUAD = (
    SNAP / "example_game_2" / "t3-auto-3" / "early_caelum.2h")


@needs_snap
def test_orders_are_addressed_by_commander_id():
    """Blocks are keyed by commander id, not by position in a table.

    The table's offset moves as the file grows -- turn 1 is 11,804 bytes and
    turn 3 is 12,262 -- so a hardcoded position wrote to arbitrary bytes. Each
    block carries its commander id, so orders are addressed by who they belong
    to and nothing depends on slot ordering.
    """
    by_id = {o.commander_id: o for o in read_orders(T1)}
    assert by_id[ESTORGANT].order_name == "sneak"
    assert by_id[ESTORGANT].destination == 86
    assert by_id[DAPAMORT].order_name == "defend"


@needs_t3
def test_reads_turn_3_where_the_old_offset_failed():
    by_id = {o.commander_id: o for o in read_orders(T3)}
    assert by_id[DAPAMORT].order_name == "patrol"
    assert by_id[ESTORGANT].order_name == "seduce"


@needs_t3
def test_instill_uprising_before_it_was_changed():
    """The baseline save captured Instill Uprising, worth 34."""
    by_id = {o.commander_id: o for o in read_orders(T3B)}
    assert by_id[ESTORGANT].order_code == ORDER_CODES["instill_uprising"] == 34


def test_dual_human_priest_orders_have_client_verified_names():
    source = SNAP / "example_game" / "t12-auto" / "mid_ermor.2h"
    if not source.exists():
        pytest.skip("dual-human priest-order snapshot absent")
    by_id = {order.commander_id: order for order in read_orders(source)}
    assert (by_id[131].order_name, by_id[131].destination) == (
        "reanimate_warriors", 0)
    assert (by_id[94].order_name, by_id[94].destination) == (
        "reanimate_lictors", 0)
    assert (by_id[3].order_name, by_id[3].destination) == (
        "claim_throne", 0)


def test_turn_70_ermor_controls_fix_the_complete_h1_to_h3_reanimation_run():
    root = SNAP / "example_game"
    snapshots = {
        "t70-auto-2": {94: "reanimate_ghouls", 131: "reanimate_ghouls",
                       181: "reanimate_ghouls"},
        "t70-auto-3": {94: "reanimate_soulless", 131: "reanimate_soulless",
                       181: "reanimate_soulless"},
        "t70-auto-4": {94: "reanimate_warriors", 131: "reanimate_warriors",
                       181: "reanimate_warriors"},
        "t70-auto-5": {94: "reanimate_horsemen", 131: "reanimate_horsemen",
                       181: "reanimate_warriors"},
        "t70-auto-6": {94: "reanimate_lictors", 131: "reanimate_horsemen",
                       181: "reanimate_warriors"},
    }
    if not all((root / name / "mid_ermor.2h").exists()
               for name in snapshots):
        pytest.skip("turn-70 Reanimation controls absent")
    for name, expected in snapshots.items():
        by_id = {
            order.commander_id: order
            for order in read_orders(root / name / "mid_ermor.2h")
        }
        assert {commander_id: by_id[commander_id].order_name
                for commander_id in expected} == expected


def test_call_god_client_order_has_exact_code_and_nation_parameter():
    source = SNAP / "example_game" / "t46-auto-6" / "mid_ermor.2h"
    if not source.exists():
        pytest.skip("controlled Call God snapshot absent")
    femur = find_order_blocks(source.read_bytes())[131]
    assert O.CALL_GOD_ORDER_CODE == O.ORDER_CODES["call_god"] == 14
    assert (femur.order_name, femur.parameter_kind, femur.parameter) == (
        "call_god", O.PARAM_NATION, 54)
    assert str(femur) == "Femur: call_god -> nation 54"


def test_call_god_writer_matches_client_authored_semantic_bytes(tmp_path):
    before = SNAP / "example_game" / "t46-auto-5" / "mid_ermor.2h"
    after = SNAP / "example_game" / "t46-auto-6" / "mid_ermor.2h"
    if not before.exists() or not after.exists():
        pytest.skip("controlled Call God snapshots absent")
    work = tmp_path / "mid_ermor.2h"
    work.write_bytes(before.read_bytes())
    editor = OrdersEditor(work)
    editor.set_order(131, "call_god", 54)
    # The final two bytes are the client trailer, deliberately not forged by
    # our editor. Everything before it is identical to the client-authored save.
    assert bytes(editor.data)[:-2] == after.read_bytes()[:-2]


def test_attack_current_province_is_a_named_fixed_local_target():
    source = (
        SNAP / "example_game" / "t17-auto-2" / "mid_marignon.2h")
    if not source.exists():
        pytest.skip("dual-human local-attack snapshot absent")
    by_id = {order.commander_id: order for order in read_orders(source)}
    assert (by_id[12].order_name, by_id[12].parameter_kind,
            by_id[12].destination) == (
        "attack_current_province", O.PARAM_CURRENT_PROVINCE, 22)
    assert (by_id[129].order_name, by_id[129].destination) == (
        "attack_current_province", 10)


@needs_t3
def test_blocks_are_variable_length():
    """Block size is 210 + len(name), so fields anchor to the name end.

    Dapamort to Estorgant is 218 bytes, Estorgant to Sugaar 219, Sugaar to
    Turgis 216 -- exactly their name lengths. A fixed stride put the order code
    one byte out for every commander whose name differed in length.
    """
    blocks = find_order_blocks(T3.read_bytes())
    for cid in (DAPAMORT, ESTORGANT, 297):
        b = blocks[cid]
        assert b.name_end - b.offset == 4 + len(b.commander_name) + 1


@needs_t3
def test_write_touches_only_two_bytes(tmp_path):
    work = tmp_path / "mid_marignon.2h"
    work.write_bytes(T3.read_bytes())
    e = OrdersEditor(work)
    e.set_order(BRUISE, "move", 91)
    changed = e.changed_bytes()
    assert len(changed) == 2                     # destination low byte + code
    assert changed[1] - changed[0] == OFF_ORDER_CODE - OFF_DESTINATION
    e.save()
    assert {o.commander_id: o for o in read_orders(work)}[BRUISE].destination == 91


@needs_t3
def test_unverified_order_is_refused():
    """Guessing a code produces a turn that silently does the wrong thing.

    The example used to be blood_hunt, which has since been verified (code 8) —
    so the test started passing an order that now exists and stopped testing
    anything. Picked a name that is still genuinely unknown, and kept a note
    because this will recur every time the vocabulary grows.
    """
    e = OrdersEditor(T3)
    with pytest.raises(ValueError, match="not verified"):
        e.set_order(DAPAMORT, "form_communion", 93)


@needs_t3
def test_no_parameter_order_rejects_a_parameter():
    e = OrdersEditor(T3)
    with pytest.raises(ValueError, match="no parameter"):
        e.set_order(DAPAMORT, "patrol", 93)


@pytest.mark.parametrize(
    ("snapshot", "commander_id", "order", "parameter"),
    [
        ("t15-misc", BRUISE, "empowerment", 0),       # Fire path index
        ("t15-misc", 314, "forge_magic_item", 1),    # Fire Sword
        ("t17", 314, "forge_magic_item", 186),        # Enchanted Helmet
        ("t18-singleround", BRUISE, "forge_magic_item", 309),
        ("t15-misc", 307, "build_palisades", 1),
    ],
)
def test_strategic_parameter_types_match_controlled_saves(
        snapshot, commander_id, order, parameter):
    path = SNAP / snapshot / "mid_marignon.2h"
    if not path.exists():
        pytest.skip(f"{snapshot} snapshot absent")
    block = find_order_blocks(path.read_bytes())[commander_id]
    assert block.order_name == order
    assert block.parameter == parameter


@needs_t3
def test_empowerment_accepts_fire_path_zero(tmp_path):
    """Zero is Fire here, not a missing destination sentinel."""
    work = tmp_path / T3.name
    work.write_bytes(T3.read_bytes())
    editor = OrdersEditor(work)
    editor.set_order(BRUISE, "empowerment", 0)
    editor.save(backup=False)
    block = find_order_blocks(work.read_bytes())[BRUISE]
    assert (block.order_code, block.parameter) == (5, 0)


@needs_t3
def test_palisades_supplies_its_verified_fixed_parameter(tmp_path):
    work = tmp_path / T3.name
    work.write_bytes(T3.read_bytes())
    editor = OrdersEditor(work)
    editor.set_order(BRUISE, "build_palisades")
    editor.save(backup=False)
    block = find_order_blocks(work.read_bytes())[BRUISE]
    assert (block.order_code, block.parameter) == (20, 1)


def test_ritual_fields_match_three_controlled_saves():
    cases = [
        ("t26-ritual-distill-gold-once", 759, 10, None, False, 9),
        ("t26-ritual-distill-gold-monthly", 759, 10, None, True, 60),
        ("t30-ritual-augury-copper-canyons", 1283, 2, 86, False, 9),
    ]
    for tag, spell, cost, province, monthly, code in cases:
        path = SNAP / tag / "mid_marignon.2h"
        if not path.exists():
            pytest.skip(f"{tag} snapshot absent")
        data = path.read_bytes()
        block = find_order_blocks(data)[297]
        ritual = read_ritual_fields(data, block.name_end)
        assert ritual is not None
        assert (ritual.spell_id, ritual.gem_cost, ritual.target_province,
                ritual.monthly, block.order_code) == (
                    spell, cost, province, monthly, code)


def test_ritual_editor_reproduces_every_semantic_augury_byte(tmp_path):
    before = SNAP / "t30-ritual-before-augury" / "mid_marignon.2h"
    observed = (SNAP / "t30-ritual-augury-copper-canyons"
                / "mid_marignon.2h")
    if not before.exists() or not observed.exists():
        pytest.skip("ritual snapshots absent")
    work = tmp_path / "mid_marignon.2h"
    work.write_bytes(before.read_bytes())
    editor = OrdersEditor(work)
    editor.set_ritual(297, 1283, 2, 86)
    block = find_order_blocks(before.read_bytes())[297]
    semantic = set(range(block.name_end + 116, block.name_end + 136)) | {
        block.name_end + OFF_ORDER_CODE}
    got, want = bytes(editor.data), observed.read_bytes()
    assert all(got[offset] == want[offset] for offset in semantic)
    assert editor.data[block.name_end + OFF_ORDER_CODE] == (
        RITUAL_ORDER_CODES["cast_ritual"])


def test_transport_ritual_editor_writes_recipient_runtime_handle(tmp_path):
    observed = SNAP / "t35-teleport-five-gems" / "mid_marignon.2h"
    if not observed.exists():
        pytest.skip("transport ritual snapshot absent")
    data = observed.read_bytes()
    blocks = find_order_blocks(data)
    caster, recipient = 126, 87
    recipient_runtime = int.from_bytes(
        data[blocks[recipient].name_end:blocks[recipient].name_end + 4],
        "little")

    work = tmp_path / observed.name
    work.write_bytes(data)
    editor = OrdersEditor(work)
    editor.set_ritual(
        caster, 1288, 5, 98,
        target_commander_runtime_index=recipient_runtime)
    fields = read_ritual_fields(bytes(editor.data), blocks[caster].name_end)

    assert fields is not None
    assert fields.target_commander_runtime_index == recipient_runtime
    assert bytes(editor.data)[blocks[caster].name_end + 132:
                              blocks[caster].name_end + 136] == b"\xff" * 4


@needs_t3
@pytest.mark.parametrize(
    ("order", "code"),
    [("build_laboratory", 18), ("demolish_fort", 44),
     ("demolish_laboratory", 45)],
)
def test_binary_decoded_local_orders_write_parameter_zero(tmp_path, order, code):
    work = tmp_path / T3.name
    work.write_bytes(T3.read_bytes())
    editor = OrdersEditor(work)
    editor.set_order(BRUISE, order)
    editor.save(backup=False)
    block = find_order_blocks(work.read_bytes())[BRUISE]
    assert (block.order_code, block.parameter) == (code, 0)


@needs_t3
def test_hide_is_the_stationary_form_of_code_two(tmp_path):
    work = tmp_path / T3.name
    work.write_bytes(T3.read_bytes())
    editor = OrdersEditor(work)
    editor.set_order(BRUISE, "hide")
    editor.save(backup=False)
    block = find_order_blocks(work.read_bytes())[BRUISE]
    assert (block.order_code, block.parameter, block.order_name) == (2, 0, "hide")


def test_fortress_save_has_both_halves_of_the_construction_order():
    from dom6_assistant.file_reader.formats import h2
    path = SNAP / "t25-fort-upgrade-fortress" / "mid_marignon.2h"
    if not path.exists():
        pytest.skip("Fortress controlled save absent")
    data = path.read_bytes()
    block = find_order_blocks(data)[307]
    assert (block.order_code, block.parameter, block.order_name) == (
        20, 2, "upgrade_fortress")
    assert h2.fort_construction(data, 98, [86, 93, 98]) == 2


def test_incomplete_fortress_order_is_pinned_as_negative_evidence():
    from dom6_assistant.file_reader.formats import h2
    path = SNAP / "t25-fort-upgrade-wrong-parameter2" / "mid_marignon.2h"
    if not path.exists():
        pytest.skip("failed Fortress save absent")
    data = path.read_bytes()
    block = find_order_blocks(data)[307]
    assert (block.order_code, block.parameter) == (20, 2)
    assert h2.fort_construction(data, 98, [86, 93, 98]) == 0


@needs_t3
def test_fortress_upgrade_low_level_fields(tmp_path):
    work = tmp_path / T3.name
    work.write_bytes(T3.read_bytes())
    editor = OrdersEditor(work)
    editor.set_order(BRUISE, "upgrade_fortress")
    editor.save(backup=False)
    block = find_order_blocks(work.read_bytes())[BRUISE]
    assert (block.order_code, block.parameter, block.order_name) == (
        20, 2, "upgrade_fortress")


@needs_t3
def test_unknown_commander_raises():
    e = OrdersEditor(T3)
    with pytest.raises(OrderTableNotLocated):
        e.set_order(99999, "patrol")


@needs_t3
def test_trailer_is_left_alone(tmp_path):
    work = tmp_path / "mid_marignon.2h"
    work.write_bytes(T3.read_bytes())
    before = T3.read_bytes()[-2:]
    e = OrdersEditor(work)
    e.set_order(BRUISE, "move", 98)
    e.save(backup=False)
    assert work.read_bytes()[-2:] == before


def test_squad_formations_are_a_per_squad_array():
    """Formations sit at name_end+200, one byte per squad in squad order.

    Established by changing one squad at a time. Turgis leads Pikeneers then
    Crossbowmen: setting the Pikeneers to Double Line moved +200, and setting
    the Crossbowmen moved +201. A single per-commander value could not do that.
    """
    from dom6_assistant.orders.orders_2h import FORMATION_CODES
    seq = [("t3-temple", ["line", "box"]),
           ("t3-doubleline", ["double_line", "box"]),
           ("t3-both-doubleline", ["double_line", "double_line"])]
    for tag, expected in seq:
        f = SNAP / tag / "mid_marignon.2h"
        if not f.exists():
            pytest.skip(f"{tag} snapshot absent")
        turgis = find_order_blocks(f.read_bytes())[307]
        assert turgis.formations[:2] == [FORMATION_CODES[e] for e in expected], tag


def test_sparse_line_formation_is_code_two():
    source = (
        SNAP / "example_game" / "t18-auto-2" / "mid_marignon.2h")
    if not source.exists():
        pytest.skip("dual-human sparse-line snapshot absent")
    tomaso = find_order_blocks(source.read_bytes())[85]
    assert tomaso.formations[2] == O.FORMATION_CODES["sparse_line"] == 2


@needs_t3
def test_there_are_exactly_five_formation_codes():
    assert O.FORMATION_CODES == {
        "box": 0,
        "line": 1,
        "sparse_line": 2,
        "skirmish": 3,
        "double_line": 4,
    }
    editor = OrdersEditor(T3)
    with pytest.raises(ValueError, match="one of"):
        editor.set_formation(307, 0, 5)


def test_battlefield_placement_grid():
    """Placement is two signed arrays over -12..+12, centre at (0, 0).

    Pinned from three moves of the same squad, each changing only its own
    bytes: north, then west, then bottom-right.
    """
    from dom6_assistant.orders.orders_2h import OFF_PLACE_X, OFF_PLACE_Y
    import struct
    cases = [("t3-placement", 0, -12), ("t3-west", -12, 0),
             ("t3-bottomright", 12, 12)]
    for tag, want_x, want_y in cases:
        f = SNAP / tag / "mid_marignon.2h"
        if not f.exists():
            pytest.skip(f"{tag} snapshot absent")
        data = f.read_bytes()
        b = find_order_blocks(data)[116]          # Dapamort, one squad
        x = struct.unpack_from("<b", data, b.name_end + OFF_PLACE_X)[0]
        y = struct.unpack_from("<b", data, b.name_end + OFF_PLACE_Y)[0]
        assert (x, y) == (want_x, want_y), tag


def test_battlefield_placement_writer_changes_only_selected_coordinates(
        tmp_path):
    import struct
    source = SNAP / "t15-battleorders" / "mid_marignon.2h"
    if not source.exists():
        pytest.skip("battle-order snapshot absent")
    path = tmp_path / source.name
    path.write_bytes(source.read_bytes())
    before = path.read_bytes()
    editor = OrdersEditor(path)
    block = editor.blocks[308]
    editor.set_placement(308, 1, -9, 7)
    editor.save(backup=False)
    after = path.read_bytes()
    assert struct.unpack_from("<b", after, block.name_end + 177)[0] == -9
    assert struct.unpack_from("<b", after, block.name_end + 182)[0] == 7
    assert [i for i in range(len(after)) if before[i] != after[i]] == [
        block.name_end + 177, block.name_end + 182]


def test_occupied_squad_slots_ignore_stale_combat_arrays(tmp_path):
    """Bruise lost slot 0, but its stance/target/position bytes survived.

    The +4 slot-id records are authoritative and sparse: turn 18 has slots
    [0,1], while turn 23 has only [1]. Counting nonzero stance bytes falsely
    reported two squads and let the writer target an empty slot.
    """
    from dom6_assistant.orders.orders_2h import (
        read_squad_orders, read_squad_slots)
    old = SNAP / "t18-gems" / "mid_marignon.2h"
    current = SNAP / "t23-quiet" / "mid_marignon.2h"
    if not old.exists() or not current.exists():
        pytest.skip("sparse squad snapshots absent")
    old_data = old.read_bytes()
    old_end = find_order_blocks(old_data)[308].name_end
    assert [row["slot"] for row in read_squad_slots(old_data, old_end)] == [0, 1]

    data = current.read_bytes()
    end = find_order_blocks(data)[308].name_end
    assert data[end + 186] != 0              # stale slot-0 stance survives
    assert [row["slot"] for row in read_squad_slots(data, end)] == [1]
    assert [row["slot"] for row in read_squad_orders(data, end)] == [1]

    path = tmp_path / current.name
    path.write_bytes(data)
    editor = OrdersEditor(path)
    with pytest.raises(ValueError, match="occupied slots are \\[1\\]"):
        editor.set_placement(308, 0, -8, 6)
    editor.set_placement(308, 1, -8, 6)


@pytest.mark.parametrize(
    ("stem", "nation_id", "marker"),
    [("mid_ermor", 54, 15), ("mid_marignon", 61, 17)],
)
def test_squad_handle_marker_is_player_specific(stem, nation_id, marker):
    path = SNAP / "example_game" / "t47-auto" / f"{stem}.2h"
    if not path.exists():
        pytest.skip("controlled dual-human turn 47 snapshot absent")
    assert O.infer_squad_marker(path.read_bytes(), nation_id) == marker


def test_mounted_units_occupy_two_records():
    """Moving one Knight of the Chalice transfers two records, not one.

    Knights ride Destriers and the mount is stored separately, which is why two
    Knights took four records while three Pikeneers took three. A writer
    reassigning a mounted unit must move both.
    """
    import struct
    f = SNAP / "t3-knight-split" / "mid_marignon.2h"
    if not f.exists():
        pytest.skip("knight-split snapshot absent")
    data = f.read_bytes()
    bruise = find_order_blocks(data)[308]
    token = struct.unpack_from("<I", data, bruise.name_end + 4)[0]
    hits = [i for i in range(len(data) - 4)
            if struct.unpack_from("<I", data, i)[0] == token]
    units = [h for h in hits if h != bruise.name_end + 4]
    assert len(units) == 2                      # rider and mount
    assert units[1] - units[0] == 173            # one unit-record stride


def test_block_scan_rejects_misaligned_junk_names():
    """A one-byte-early read decodes padding as "O" and still ends in a name.

    "GMOOOOOOOLOOOOzNOOClodius" is all letters and passes a naive test, so the
    scan now requires the shape of a name. Only genuine commanders and the
    province-name table survive.
    """
    from dom6_assistant.orders.orders_2h import _plausible_name
    assert _plausible_name("Dapamort")
    assert _plausible_name("The Obsidian Waste")
    assert _plausible_name("Tukulti'ninurta")
    assert not _plausible_name("GMOOOOOOOLOOOOzNOOClodius")
    assert not _plausible_name("OMarignon")
    assert not _plausible_name("oswald")


def test_valid_ids_filters_out_the_province_table():
    """Province names share the commander block's shape and must be filtered.

    The province table is u32 id + XOR name, exactly like the commander table,
    so provinces appear as commanders unless the caller supplies the real ids.
    """
    t5 = SNAP / "t5" / "mid_marignon.2h"
    if not t5.exists():
        pytest.skip("turn 5 snapshot absent")
    data = t5.read_bytes()
    unfiltered = find_order_blocks(data)
    assert 93 in unfiltered and unfiltered[93].commander_name == "Marignon"
    real = {116, 297, 307, 308, 309, 314}
    filtered = find_order_blocks(data, valid_ids=real)
    assert set(filtered) == real


def test_battle_orders_decode_against_the_players_setup():
    """One save with a different value in every field, checked against what was set.

    The player configured each commander and squad distinctly and described the
    result, so this is a controlled experiment rather than a fit: every code below
    is pinned by a stated intent, not by looking plausible.

        Sugaar     5 squads: Attack archers, Fire archers, Hold and Fire archers,
                   Fire archers and keep distance, Guard Commander
        Bretaigne  5 squads all Attack, targeting rear, closest, fliers,
                   cavalry, large monsters
        Bruise     2 squads: Hold and Attack large monsters, Hold and Attack fliers
    """
    from dom6_assistant.orders.orders_2h import (
        SQUAD_STANCE_CODES, SQUAD_TARGET_CODES, find_order_blocks,
        read_squad_orders,
    )
    path = Path("knowledge/snapshots/t15-battleorders/mid_marignon.2h")
    if not path.exists():
        pytest.skip("battle-order snapshot absent")
    data = path.read_bytes()
    blocks = find_order_blocks(data)
    S, T = SQUAD_STANCE_CODES, SQUAD_TARGET_CODES

    sugaar = read_squad_orders(data, blocks[297].name_end)
    assert [q["stance"] for q in sugaar] == [
        S["attack"], S["fire"], S["hold_and_fire"],
        S["fire_and_keep_distance"], S["guard_commander"]]
    assert [q["target"] for q in sugaar[:4]] == [T["archers"]] * 4

    bretaigne = read_squad_orders(data, blocks[126].name_end)
    assert {q["stance"] for q in bretaigne} == {S["attack"]}
    assert [q["target"] for q in bretaigne] == [
        T["rearmost"], T["closest"], T["fliers"], T["cavalry"], T["large_monsters"]]

    bruise = read_squad_orders(data, blocks[308].name_end)
    assert [(q["stance"], q["target"]) for q in bruise] == [
        (S["hold_and_attack"], T["large_monsters"]),
        (S["hold_and_attack"], T["fliers"])]


def test_queued_spells_are_reference_spell_ids():
    """Positive slots at +136 are spell ids, checked against the spell table.

    Sugaar was set to cast Summon Hawk, Summon Storm Power and Air Shield in that
    order; Urraca to cast Fire Flies. The slots hold the ids those names resolve
    to, which is what makes this a decode rather than a coincidence — the values
    were predicted by name before being looked up.
    """
    import sqlite3
    from dom6_assistant.orders.orders_2h import find_order_blocks, read_spell_queue
    path = Path("knowledge/snapshots/t15-battleorders/mid_marignon.2h")
    if not path.exists():
        pytest.skip("battle-order snapshot absent")
    names = {r[0]: r[1] for r in sqlite3.connect(
        "knowledge/reference/reference.sqlite3").execute("SELECT id, name FROM spells")}
    blocks = find_order_blocks(path.read_bytes())
    data = path.read_bytes()

    sugaar = read_spell_queue(data, blocks[297].name_end)
    assert [names[s] for s in sugaar if s > 0] == [
        "Summon Hawk", "Summon Storm Power", "Air Shield"]
    assert sugaar[3:] == [-1, -1], "unused slots must read as empty"
    assert names[read_spell_queue(data, blocks[9].name_end)[0]] == "Fire Flies"


def test_commander_own_battle_order_at_198():
    """+198 is the commander's own battle order and +199 its target.

    Ten commanders were each given a different personal order and every one of
    them moved this byte. It was briefly recorded as "not in the order block",
    which was wrong for an avoidable reason: the search that concluded it
    excluded offsets 176-206 as squad arrays, and +198 is inside that window.
    Excluding a range and then concluding a field is absent from it is a mistake
    the test now pins shut.
    """
    from dom6_assistant.orders.orders_2h import (
        STANCE_CODES, TARGET_CODES_V2, find_order_blocks, read_own_battle_order)
    path = Path("knowledge/snapshots/t15-battleorders/mid_marignon.2h")
    if not path.exists():
        pytest.skip("battle-order snapshot absent")
    data = path.read_bytes()
    blocks = find_order_blocks(data)
    S, T = STANCE_CODES, TARGET_CODES_V2
    expected = {
        9:   (S["cast_spells"], 0),
        17:  (S["stay_behind_troops"], 0),
        86:  (S["advance_and_cast_spells"], T["archers"]),
        87:  (S["retreat"], 0),
        110: (S["attack"], T["archers"]),
        116: (S["attack"], T["cavalry"]),
        126: (S["attack"], T["fliers"]),
        235: (S["attack"], T["large_monsters"]),
        297: (S["attack"], T["closest"]),
        308: (S["attack"], T["rearmost"]),
    }
    for cid, want in expected.items():
        assert read_own_battle_order(data, blocks[cid].name_end) == want, cid


def test_commander_and_squad_stances_share_one_table():
    """Retreat is 7 whether a commander or a squad is doing it.

    That shared value is the evidence the two are one vocabulary rather than two
    that happen to overlap, which is what licenses using STANCE_CODES for both.
    """
    from dom6_assistant.orders.orders_2h import (
        STANCE_CODES, find_order_blocks, read_own_battle_order, read_squad_orders)
    path = Path("knowledge/snapshots/t15-battleorders/mid_marignon.2h")
    if not path.exists():
        pytest.skip("battle-order snapshot absent")
    data = path.read_bytes()
    blocks = find_order_blocks(data)
    # Floredee's own order is Retreat; Guarlan's single squad is also Retreat.
    assert read_own_battle_order(data, blocks[87].name_end)[0] == STANCE_CODES["retreat"]
    assert read_squad_orders(data, blocks[314].name_end)[0]["stance"] == STANCE_CODES["retreat"]


def test_carried_gems_are_nine_bytes_in_path_order():
    """+165 holds nine consecutive bytes, one per path, in F A W E S D N G B order.

    Three saves with different gem loadouts all decode exactly. The single-gem
    save is the one that matters: only astral, and it lands at index 4, which is
    where the path order puts it.

    Two earlier readings of this field were wrong in the same way. The nation only
    ever held fire, astral and blood — indices 0, 4 and 8, evenly spaced four
    apart — so the array looked like it had a stride of 4 with slots 0, 1 and 2.
    A stride inferred from evenly spaced observations is the spacing of the
    sample, not the stride of the array.
    """
    from dom6_assistant.orders.orders_2h import find_order_blocks, read_carried_gems
    SUGAAR = 297
    cases = {
        "t17-equipped": {"fire": 3, "astral": 2, "blood": 2},
        "t18-gems":     {"fire": 3, "astral": 3, "blood": 3},
        "t18-astralonly": {"astral": 1},
    }
    seen = 0
    for snap, want in cases.items():
        path = Path(f"knowledge/snapshots/{snap}/mid_marignon.2h")
        if not path.exists():
            continue
        data = path.read_bytes()
        assert read_carried_gems(data, find_order_blocks(data)[SUGAAR].name_end) == want, snap
        seen += 1
    require_corpus(seen, 2, "gem-carrying snapshots")
    # The decisive case must be present: a lone astral pearl at index 4.
    lone = Path("knowledge/snapshots/t18-astralonly/mid_marignon.2h")
    if lone.exists():
        data = lone.read_bytes()
        ne = find_order_blocks(data)[SUGAAR].name_end
        assert [data[ne + 165 + i] for i in range(9)] == [0, 0, 0, 0, 1, 0, 0, 0, 0]


def test_carried_gem_writer_uses_the_named_path_byte(tmp_path):
    from dom6_assistant.orders.orders_2h import read_carried_gems
    source = Path("knowledge/snapshots/t18-gems/mid_marignon.2h")
    if not source.exists():
        pytest.skip("gem snapshot absent")
    path = tmp_path / source.name
    path.write_bytes(source.read_bytes())
    editor = OrdersEditor(path)
    editor.set_carried_gems(297, "astral", 1)
    editor.save(backup=False)
    assert read_carried_gems(path.read_bytes(),
                             editor.blocks[297].name_end) == {
        "fire": 3, "astral": 1, "blood": 3}


def test_single_round_order_codes():
    """One order per commander in a single save maps all five labels.

    Set deliberately so no two commanders shared a code, which is what makes the
    mapping unambiguous — the earlier attempt had all five on one commander and
    could not be read positionally.

    It also explains that earlier queue. Urraca's [244, -3, 0, -5, -6] was five
    orders, not the four described: Fire Flies, hold, hold-or-cast, attack, fly
    attack. Five slots never fitted four labels because one had been left out of
    the description, not because the encoding was odd.
    """
    import sqlite3
    from dom6_assistant.orders.orders_2h import (
        SINGLE_ROUND_CODES, describe_single_round, find_order_blocks,
        read_spell_queue,
    )
    path = Path("knowledge/snapshots/t18-singleround/mid_marignon.2h")
    if not path.exists():
        pytest.skip("snapshot absent")
    data = path.read_bytes()
    blocks = find_order_blocks(data)
    S = SINGLE_ROUND_CODES
    expected = {
        9:   S["hold_one_turn"],
        86:  S["hold_or_cast_a_spell"],
        110: S["fly_attack_one_turn"],
        116: S["attack_one_turn"],
    }
    for cid, code in expected.items():
        assert read_spell_queue(data, blocks[cid].name_end)[0] == code, cid
    # Floredee cast a specific spell: the slot holds the spell's own id.
    blessing = sqlite3.connect("knowledge/reference/reference.sqlite3").execute(
        "SELECT id FROM spells WHERE name='Blessing'").fetchone()[0]
    assert read_spell_queue(data, blocks[87].name_end)[0] == blessing
    names = {blessing: "Blessing"}
    assert describe_single_round(blessing, names) == "cast Blessing"


def test_single_round_writer_replaces_and_pads_the_whole_queue(tmp_path):
    from dom6_assistant.orders.orders_2h import (
        SINGLE_ROUND_CODES, read_spell_queue)
    source = Path("knowledge/snapshots/t18-gems/mid_marignon.2h")
    if not source.exists():
        pytest.skip("gem snapshot absent")
    path = tmp_path / source.name
    path.write_bytes(source.read_bytes())
    editor = OrdersEditor(path)
    editor.set_spell_queue(9, [244, SINGLE_ROUND_CODES["hold_one_turn"]])
    editor.save(backup=False)
    assert read_spell_queue(path.read_bytes(), editor.blocks[9].name_end) == [
        244, SINGLE_ROUND_CODES["hold_one_turn"], -1, -1, -1]


def test_equipment_slots_map_to_item_types():
    """Six items of six types on one commander fix the original six offsets.

    Bruise carries a Fire Sword (1-h wpn), Enchanted Shield (shield), Enchanted
    Helmet (helm), Enchanted Ring Mail Armor (armor), Pendant of Courage and Ring
    of Fire (both misc). Each lands at a fixed offset, and the spacing is
    irregular — 2, 22, 6, 4, 2 — so this is not a contiguous array.

    An earlier reading treated it as a u32 array, which worked while only two
    items were equipped because the neighbouring halves were zero. The third item
    exposed it at once: slot 0 read 10616833, which is 0x00A20001 — two u16s, 1
    and 162, the sword and the shield.
    """
    import sqlite3
    from dom6_assistant.orders.orders_2h import find_order_blocks, read_equipment
    path = Path("knowledge/snapshots/t19-equipment/mid_marignon.2h")
    if not path.exists():
        pytest.skip("snapshot absent")
    data = path.read_bytes()
    names = {r[0]: r[1] for r in sqlite3.connect(
        "knowledge/reference/reference.sqlite3").execute("SELECT id, name FROM items")}
    eq = read_equipment(data, find_order_blocks(data)[308].name_end)
    assert {k: names[v] for k, v in eq.items()} == {
        "weapon": "Fire Sword",
        "shield": "Enchanted Shield",
        "helm":   "Enchanted Helmet",
        "armor":  "Enchanted Ring Mail Armor",
        "misc1":  "Pendant of Courage",
        "misc2":  "Ring of Fire",
    }
    # A commander with only ordinary starting gear carries nothing here.
    assert read_equipment(data, find_order_blocks(data)[307].name_end) == {}


def test_ranged_and_boots_slots_match_the_turn_44_controls():
    """Clean, crossbow-only and shoes-only saves isolate both new offsets."""
    from dom6_assistant.file_reader.formats import h2 as H2
    from dom6_assistant.orders.orders_2h import find_order_blocks, read_equipment

    root = Path("knowledge/snapshots")
    paths = [root / name / "mid_marignon.2h" for name in (
        "t44-auto-4", "t44-auto-5", "t44-auto-6")]
    if not all(path.exists() for path in paths):
        pytest.skip("turn-44 equipment controls absent")
    clean, cross, shoes = (path.read_bytes() for path in paths)
    end = find_order_blocks(clean)[59].name_end

    assert read_equipment(clean, end) == {}
    assert read_equipment(cross, end) == {"ranged": 143}
    assert read_equipment(shoes, end) == {"boots": 288}
    assert H2.read_item_stash(clean) == [1, 135, 143, 288]
    assert H2.read_item_stash(cross) == [1, 135, 288]
    assert H2.read_item_stash(shoes) == [1, 135, 143]


@pytest.mark.parametrize(
    ("slot", "item_id", "target_name"),
    [("ranged", 143, "t44-auto-5"), ("boots", 288, "t44-auto-6")],
)
def test_equipment_transfer_reproduces_the_client_save(
        tmp_path, slot, item_id, target_name):
    """The slot and treasury mutation match the client byte-for-byte."""
    from dom6_assistant.orders.orders_2h import OrdersEditor

    root = Path("knowledge/snapshots")
    source = root / "t44-auto-4" / "mid_marignon.2h"
    target = root / target_name / "mid_marignon.2h"
    if not source.exists() or not target.exists():
        pytest.skip("turn-44 equipment controls absent")
    work = tmp_path / "mid_marignon.2h"
    work.write_bytes(source.read_bytes())
    editor = OrdersEditor(work)
    editor.transfer_equipment(59, slot, item_id)

    # The final byte is the client's changing save trailer, which no writer
    # needs to reproduce; every semantic byte must otherwise be identical.
    assert bytes(editor.data)[:-1] == target.read_bytes()[:-1]


@pytest.mark.parametrize(
    ("source_name", "slot"),
    [("t44-auto-5", "ranged"), ("t44-auto-6", "boots")],
)
def test_clearing_new_equipment_slots_restores_the_client_treasury(
        tmp_path, source_name, slot):
    from dom6_assistant.orders.orders_2h import OrdersEditor

    root = Path("knowledge/snapshots")
    source = root / source_name / "mid_marignon.2h"
    target = root / "t44-auto-4" / "mid_marignon.2h"
    if not source.exists() or not target.exists():
        pytest.skip("turn-44 equipment controls absent")
    work = tmp_path / "mid_marignon.2h"
    work.write_bytes(source.read_bytes())
    editor = OrdersEditor(work)
    editor.transfer_equipment(59, slot, 0)

    assert bytes(editor.data)[:-1] == target.read_bytes()[:-1]


@pytest.mark.skipif(not TROOP_ASSIGNMENT.exists(),
                    reason="turn 30 troop snapshot absent")
def test_assign_unattached_troop_changes_only_its_warband_token(tmp_path):
    """Pikeneer #800 joins Bruise's already-existing Pikeneer squad."""
    work = tmp_path / TROOP_ASSIGNMENT.name
    work.write_bytes(TROOP_ASSIGNMENT.read_bytes())
    before = work.read_bytes()
    unit = next(u for u in O.read_h2_units(before, 61)
                if u.instance_id == 800 and not u.is_mount)

    editor = OrdersEditor(work)
    outcome = editor.assign_troops(61, [(800, BRUISE, 1)])
    after = bytes(editor.data)

    assert outcome["moved"] == [{
        "instance_id": 800, "type_id": 221, "source_token": None,
        "target_token": (17 << 16) | 57831, "records": 1,
    }]
    assert outcome["cleared_slots"] == []
    assert [i for i, (old, new) in enumerate(zip(before, after)) if old != new] == [
        unit.offset - 4, unit.offset - 3, unit.offset - 2, unit.offset - 1]


@pytest.mark.skipif(not TROOP_ASSIGNMENT.exists(),
                    reason="turn 30 troop snapshot absent")
def test_moving_last_troop_clears_only_authoritative_source_slot(tmp_path):
    """Dapamort's stale battle arrays survive when his real slot empties."""
    work = tmp_path / TROOP_ASSIGNMENT.name
    work.write_bytes(TROOP_ASSIGNMENT.read_bytes())
    before = work.read_bytes()
    blocks = O.find_order_blocks(before)
    source_slot = blocks[DAPAMORT].name_end + O.OFF_SQUAD_SLOTS

    editor = OrdersEditor(work)
    outcome = editor.assign_troops(61, [(790, BRUISE, 1)])
    after = bytes(editor.data)

    assert outcome["cleared_slots"] == [{
        "commander_id": DAPAMORT, "slot": 0,
        "token": (17 << 16) | 46043,
    }]
    assert after[source_slot:source_slot + 4] == b"\xff" * 4
    # Stance, target, formation and placement are deliberately not scrubbed.
    assert (after[blocks[DAPAMORT].name_end + O.OFF_STANCE:
                  blocks[DAPAMORT].name_end + O.OFF_STANCE + 5]
            == before[blocks[DAPAMORT].name_end + O.OFF_STANCE:
                      blocks[DAPAMORT].name_end + O.OFF_STANCE + 5])


def test_caelum_squad_markers_are_per_squad_not_per_nation():
    if not CAELUM_ATTACHED.exists():
        pytest.skip("Caelum detachment control absent")
    data = CAELUM_ATTACHED.read_bytes()
    block = O.find_order_blocks(data, valid_ids={5})[5]
    slots = O.read_squad_slots(data, block.name_end)
    assert [(row["slot"], row["marker"]) for row in slots] == [(0, 6), (1, 7)]


def test_existing_caelum_squad_reassignment_needs_no_marker_allocation():
    """Both target tokens already exist, so the nation may be multi-marker."""
    if not CAELUM_ATTACHED.exists():
        pytest.skip("Caelum multi-marker control absent")
    editor = OrdersEditor(CAELUM_ATTACHED)
    outcome = editor.assign_troops(24, [(151, 5, 1)])
    assert outcome["moved"] == [{
        "instance_id": 151,
        "type_id": 2566,
        "source_token": (6 << 16) | 64360,
        "target_token": (7 << 16) | 5100,
        "records": 1,
    }]
    data = bytes(editor.data)
    unit = next(unit for unit in O.read_h2_units(data, 24)
                if unit.instance_id == 151 and not unit.is_mount)
    assert unit.warband == (7 << 16) | 5100


def test_caelum_new_squads_reuse_greatest_active_marker_and_match_client(
        tmp_path):
    """Two consecutive client allocations both reuse marker 7, not marker 6."""
    root = SNAP / "example_game_2"
    baseline = root / "t3-auto-2" / "early_caelum.2h"
    third = root / "t3-auto-4" / "early_caelum.2h"
    fourth = root / "t3-auto-5" / "early_caelum.2h"
    if not all(path.exists() for path in (baseline, third, fourth)):
        pytest.skip("Caelum multi-marker new-squad controls absent")

    work = tmp_path / "early_caelum.2h"
    work.write_bytes(baseline.read_bytes())
    editor = OrdersEditor(work)
    assert O.infer_squad_marker(bytes(editor.data), 24) == 7

    first = editor.assign_troops(
        24, [(152, 5, 2)], new_squads={(5, 2): 15332})
    assert first["created_slots"] == [{
        "commander_id": 5, "slot": 2, "squad_id": 15332,
        "token": (7 << 16) | 15332,
    }]
    assert bytes(editor.data)[:-1] == third.read_bytes()[:-1]

    work.write_bytes(bytes(editor.data))
    editor = OrdersEditor(work)
    second = editor.assign_troops(
        24, [(153, 5, 3)], new_squads={(5, 3): 9917})
    assert second["created_slots"] == [{
        "commander_id": 5, "slot": 3, "squad_id": 9917,
        "token": (7 << 16) | 9917,
    }]
    assert bytes(editor.data)[:-1] == fourth.read_bytes()[:-1]


def test_detach_troops_matches_partial_and_empty_client_saves(tmp_path):
    if not all(path.exists() for path in (
            CAELUM_ATTACHED, CAELUM_PARTIAL_GARRISON, CAELUM_EMPTY_SQUAD)):
        pytest.skip("Caelum detachment controls absent")
    work = tmp_path / "early_caelum.2h"
    work.write_bytes(CAELUM_ATTACHED.read_bytes())
    editor = OrdersEditor(work)
    first = editor.detach_troops(24, list(range(151, 161)))
    assert first["cleared_slots"] == []
    assert bytes(editor.data)[:-1] == CAELUM_PARTIAL_GARRISON.read_bytes()[:-1]

    work.write_bytes(bytes(editor.data))
    editor = OrdersEditor(work)
    second = editor.detach_troops(24, list(range(161, 176)))
    assert second["cleared_slots"] == [{
        "commander_id": 5, "slot": 0, "token": (6 << 16) | 64360,
    }]
    assert bytes(editor.data)[:-1] == CAELUM_EMPTY_SQUAD.read_bytes()[:-1]


@pytest.mark.skipif(not TROOP_ASSIGNMENT.exists(),
                    reason="turn 30 troop snapshot absent")
def test_mounted_rider_moves_the_adjacent_mount_atomically(tmp_path):
    work = tmp_path / TROOP_ASSIGNMENT.name
    work.write_bytes(TROOP_ASSIGNMENT.read_bytes())
    before = work.read_bytes()
    units = O.read_h2_units(before, 61)
    rider = next(u for u in units if u.instance_id == 2332 and not u.is_mount)
    mount = next(u for u in units if u.offset == rider.offset + U.RECORD_SIZE)
    assert mount.is_mount and mount.warband == rider.warband == O.NO_SQUAD

    editor = OrdersEditor(work)
    outcome = editor.assign_troops(61, [(2332, 297, 0)])
    after = bytes(editor.data)

    assert outcome["moved"][0]["records"] == 2
    expected = set(range(rider.offset - 4, rider.offset))
    expected.update(range(mount.offset - 4, mount.offset))
    assert {i for i, (old, new) in enumerate(zip(before, after))
            if old != new} == expected


@pytest.mark.skipif(not TROOP_ASSIGNMENT.exists(),
                    reason="turn 30 troop snapshot absent")
def test_troop_assignment_allows_mixed_types_but_refuses_wrong_province():
    editor = OrdersEditor(TROOP_ASSIGNMENT)
    outcome = editor.assign_troops(61, [(800, 297, 0)])
    assert outcome["moved"][0]["instance_id"] == 800
    editor = OrdersEditor(TROOP_ASSIGNMENT)
    # Pikeneer #2202 is in The Obsidian Waste; Bruise is in Marignon.
    with pytest.raises(ValueError, match="province 98"):
        editor.assign_troops(61, [(2202, BRUISE, 1)])


@pytest.mark.skipif(not NEW_SQUAD.exists(),
                    reason="client-confirmed troop snapshot absent")
def test_create_squad_restores_empty_slot_and_resets_stale_combat_bytes(
        tmp_path):
    """Bruise's retired slot-0 id is collision-free and evidence-backed."""
    work = tmp_path / NEW_SQUAD.name
    work.write_bytes(NEW_SQUAD.read_bytes())
    before = work.read_bytes()
    block = O.find_order_blocks(before)[BRUISE]
    crossbow = next(u for u in O.read_h2_units(before, 61)
                    if u.instance_id == 966 and not u.is_mount)

    editor = OrdersEditor(work)
    outcome = editor.assign_troops(
        61, [(966, BRUISE, 0)], new_squads={(BRUISE, 0): 52964})
    after = bytes(editor.data)

    assert outcome["created_slots"] == [{
        "commander_id": BRUISE, "slot": 0, "squad_id": 52964,
        "token": (17 << 16) | 52964,
    }]
    assert O.read_squad_slots(after, block.name_end) == [
        {"slot": 0, "squad_id": 52964, "marker": 17,
         "token": (17 << 16) | 52964},
        {"slot": 1, "squad_id": 57831, "marker": 17,
         "token": (17 << 16) | 57831},
    ]
    squad = next(row for row in O.read_squad_orders(after, block.name_end)
                 if row["slot"] == 0)
    assert (squad["stance"], squad["target"], squad["formation"],
            squad["x"], squad["y"]) == (0, 0, 0, 0, 0)
    changed = {i for i, (old, new) in enumerate(zip(before, after))
               if old != new}
    expected = set(range(block.name_end + O.OFF_SQUAD_SLOTS,
                         block.name_end + O.OFF_SQUAD_SLOTS + 4))
    expected.update(range(crossbow.offset - 4, crossbow.offset))
    expected.update({block.name_end + offset for offset in (
        O.OFF_PLACE_X, O.OFF_STANCE, O.OFF_TARGET, O.OFF_FORMATION)})
    assert changed == expected


@pytest.mark.skipif(not NEW_SQUAD.exists(),
                    reason="client-confirmed troop snapshot absent")
def test_create_squad_requires_location_and_collision_free_id():
    editor = OrdersEditor(NEW_SQUAD)
    with pytest.raises(ValueError, match="already in use"):
        editor.assign_troops(
            61, [(966, BRUISE, 0)], new_squads={(BRUISE, 0): 57831})
    # Guarlan has no remaining occupied squad, so his current province cannot
    # be proven through the save-file linkage used by the writer.
    with pytest.raises(ValueError, match="no single proven province"):
        editor.assign_troops(
            61, [(966, 314, 0)], new_squads={(314, 0): 50000})
