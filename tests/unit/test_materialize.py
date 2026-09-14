"""Tests for turning stored intent into a .2h.

The property under test is that the file is a pure function of the intent rows.
Everything else follows from it: rebuild-anywhere, discard-and-retry, and an
audit trail that survives the file being overwritten.
"""
import shutil
import sqlite3
import struct
from pathlib import Path

import pytest

from dom6_assistant.orders import materialize as M
from dom6_assistant.orders.orders_2h import (
    OFF_DESTINATION, OFF_ORDER_CODE, find_order_blocks, read_orders,
    read_h2_units, read_ritual_fields, read_squad_orders,
)
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.file_reader.formats import diplomacy as D

_SNAPSHOTS = Path("knowledge/snapshots")
_SCHEMA = Path("src/dom6_assistant/gamestate/schema.sql")
TURGIS = 307


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO games(id, name) VALUES(1, 'test')")
    conn.commit()
    return conn


@pytest.fixture
def h2(tmp_path):
    src = next(_SNAPSHOTS.glob("t9/*.2h"), None)
    if src is None:
        pytest.skip("turn 9 snapshot absent")
    dst = tmp_path / src.name
    shutil.copy2(src, dst)
    return dst


@pytest.fixture
def research_h2(tmp_path):
    snapshot = _SNAPSHOTS / "t25-research-before"
    h2_path = tmp_path / "mid_marignon.2h"
    trn_path = tmp_path / "mid_marignon.trn"
    shutil.copy2(snapshot / h2_path.name, h2_path)
    shutil.copy2(snapshot / trn_path.name, trn_path)
    return h2_path


@pytest.fixture
def ritual_h2(tmp_path):
    snapshot = _SNAPSHOTS / "t30-ritual-before-augury"
    if not (snapshot / "mid_marignon.2h").exists():
        pytest.skip("ritual baseline absent")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    return tmp_path / "mid_marignon.2h"


@pytest.fixture
def battle_h2(tmp_path):
    snapshot = _SNAPSHOTS / "t18-gems"
    if not (snapshot / "mid_marignon.2h").exists():
        pytest.skip("battle setup snapshot absent")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    return tmp_path / "mid_marignon.2h"


@pytest.fixture
def troop_h2(tmp_path):
    snapshot = _SNAPSHOTS / "t30-ritual-augury-copper-canyons"
    if not (snapshot / "mid_marignon.2h").exists():
        pytest.skip("turn 30 troop snapshot absent")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    return tmp_path / "mid_marignon.2h"


@pytest.fixture
def new_squad_h2(tmp_path):
    snapshot = _SNAPSHOTS / "t30-troop-assignment-bruise"
    if not (snapshot / "mid_marignon.2h").exists():
        pytest.skip("turn 30 new-squad baseline absent")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    return tmp_path / "mid_marignon.2h"


@pytest.fixture
def caelum_detach_h2(tmp_path):
    snapshot = _SNAPSHOTS / "example_game_2" / "t3-auto"
    if not (snapshot / "early_caelum.2h").exists():
        pytest.skip("Caelum detachment baseline absent")
    for name in ("early_caelum.2h", "early_caelum.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    return tmp_path / "early_caelum.2h"


@pytest.fixture
def pd_h2(tmp_path):
    snapshot = _SNAPSHOTS / "t30-pd-baseline"
    if not (snapshot / "mid_marignon.2h").exists():
        pytest.skip("turn-30 province-defence baseline absent")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    return tmp_path / "mid_marignon.2h"


@pytest.fixture
def reference_db():
    conn = sqlite3.connect("knowledge/reference/reference.sqlite3")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _fields(path, commander_id=TURGIS):
    data = path.read_bytes()
    block = find_order_blocks(data)[commander_id]
    return (data[block.name_end + OFF_ORDER_CODE],
            struct.unpack_from("<H", data, block.name_end + OFF_DESTINATION)[0])


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (
            "t30-ritual-before-augury/mid_marignon.2h",
            (41, 1, 1, 1, 11, 1, 5, 0, 84),
        ),
        (
            "example_game/t43-auto/mid_ermor.2h",
            (19, 25, 7, 15, 11, 290, 7, 2, 0),
        ),
        (
            "example_game/t45-auto-2/mid_marignon.2h",
            (92, 31, 14, 0, 26, 9, 1, 0, 0),
        ),
        (
            "example_game/t4-auto/mid_ermor.2h",
            (0, 0, 0, 0, 0, 0, 0, 0, 0),
        ),
    ],
)
def test_gem_treasury_structure_spans_single_and_dual_human_layouts(
        relative, expected):
    path = _SNAPSHOTS / relative
    if not path.exists():
        pytest.skip(f"gem-treasury control absent: {relative}")
    data = path.read_bytes()
    assert H2.gem_remaining(data) == expected

    changed = list(expected)
    changed[0] += 1
    rewritten = H2.set_gem_remaining(data, changed)
    assert H2.gem_remaining(rewritten) == tuple(changed)
    start = H2.gem_treasury_offset(data)
    assert rewritten[:start] == data[:start]
    assert rewritten[start + 36:] == data[start + 36:]


def test_latest_intent_wins_and_earlier_rows_are_kept(db, h2):
    """A change of mind is a new row, not an edit — and the file follows it."""
    M.record_order(db, 1, 9, TURGIS, "move", commander_name="Turgis",
                   destination=93, rationale="reinforce the capital")
    M.record_order(db, 1, 9, TURGIS, "sneak", commander_name="Turgis",
                   destination=91, rationale="scout Kratas instead")
    assert db.execute("SELECT COUNT(*) FROM order_intent").fetchone()[0] == 2
    rows = M.current_intent(db, 1, 9)
    assert len(rows) == 1 and rows[0]["order_name"] == "sneak"

    M.materialize(db, 1, 9, h2)
    assert _fields(h2) == (2, 91)          # sneak, to Kratas

    # The superseded decision and its reasoning survive for later review.
    first = db.execute("SELECT rationale FROM order_intent ORDER BY id").fetchone()
    assert first[0] == "reinforce the capital"


def test_diplomatic_proposal_materializes_and_is_idempotent(db, tmp_path):
    snapshot = _SNAPSHOTS / "t38-diplomacy-baseline"
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    path = tmp_path / "mid_marignon.2h"
    M.record_diplomatic_action(
        db,
        1,
        38,
        85,
        "propose_nap",
        "We propose peace and a non-aggression treaty",
        rationale="secure Kratas",
    )
    result = M.materialize(db, 1, 38, path)
    assert not result.skipped
    rows = D.find_outgoing_diplomacy(path.read_bytes())[2]
    assert [(row.target_nation_id, row.action, row.extra, row.text) for row in rows] == [
        (85, "propose_nap", 0, "We propose peace and a non-aggression treaty")
    ]
    first = path.read_bytes()
    M.materialize(db, 1, 38, path)
    assert path.read_bytes() == first


def test_augury_materializes_spell_target_cost_and_gem_delta(
        db, ritual_h2, reference_db):
    M.record_ritual(
        db, 1, 30, 297, 1283, 0, 2, commander_name="Sugaar",
        spell_name="Augury", target_province=86,
        rationale="search Copper Canyons")
    result = M.materialize(
        db, 1, 30, ritual_h2, reference_conn=reference_db)
    assert not result.skipped
    data = ritual_h2.read_bytes()
    block = find_order_blocks(data)[297]
    ritual = read_ritual_fields(data, block.name_end)
    assert ritual is not None
    assert (ritual.spell_id, ritual.gem_cost, ritual.target_province,
            ritual.monthly) == (1283, 2, 86, False)
    assert H2.gem_remaining(data)[0] == 39

    once = data
    M.materialize(db, 1, 30, ritual_h2, reference_conn=reference_db)
    assert ritual_h2.read_bytes() == once


def test_transport_ritual_materializes_selected_recipient(
        db, tmp_path, reference_db):
    snapshot = _SNAPSHOTS / "t35-teleport-five-gems"
    if not (snapshot / "mid_marignon.2h").exists():
        pytest.skip("transport ritual snapshot absent")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    path = tmp_path / "mid_marignon.2h"
    caster, recipient = 126, 87
    before = path.read_bytes()
    before_blocks = find_order_blocks(before)
    expected_runtime = struct.unpack_from(
        "<I", before, before_blocks[recipient].name_end)[0]

    M.record_ritual(
        db, 1, 35, caster, 1288, 0, 5, commander_name="Bretaigne",
        spell_name="Teleport Gems", target_province=98,
        target_commander_id=recipient, rationale="deliver five fire gems")
    result = M.materialize(db, 1, 35, path, reference_conn=reference_db)

    assert not result.skipped
    data = path.read_bytes()
    fields = read_ritual_fields(data, find_order_blocks(data)[caster].name_end)
    assert fields is not None
    assert fields.target_commander_runtime_index == expected_runtime


def test_replacing_game_written_augury_refunds_its_fire_gems(
        db, tmp_path, reference_db):
    snapshot = _SNAPSHOTS / "t30-ritual-augury-copper-canyons"
    if not (snapshot / "mid_marignon.2h").exists():
        pytest.skip("Augury snapshot absent")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    path = tmp_path / "mid_marignon.2h"
    M.record_order(db, 1, 30, 297, "research", commander_name="Sugaar",
                   rationale="return to research")
    result = M.materialize(db, 1, 30, path, reference_conn=reference_db)
    assert not result.skipped
    data = path.read_bytes()
    block = find_order_blocks(data)[297]
    assert block.order_name == "research"
    assert data[block.name_end + 120:block.name_end + 136] == b"\x00" * 16
    assert H2.gem_remaining(data)[0] == 41


def test_complete_battle_setup_materializes_and_moves_gems_atomically(
        db, battle_h2):
    """Position, script and gems compose, including a same-path transfer."""
    from dom6_assistant.orders.orders_2h import (
        SINGLE_ROUND_CODES, read_carried_gems, read_placement,
        read_spell_queue)
    before_gems = H2.gem_remaining(battle_h2.read_bytes())
    M.record_battle_position(
        db, 1, 18, 308, 1, -9, 7, commander_name="Bruise",
        rationale="flank from the west")
    M.record_battle_script(
        db, 1, 18, 308,
        [244, SINGLE_ROUND_CODES["hold_one_turn"]],
        commander_name="Bruise", rationale="open with Fire Flies")
    # Transfer Sugaar's three carried fire gems to Urraca. Budgeting row by
    # row would falsely reject this if the assignment happened before refund.
    M.record_carried_gems(
        db, 1, 18, 297, 0, 0, commander_name="Sugaar", rationale="transfer")
    M.record_carried_gems(
        db, 1, 18, 9, 0, 3, commander_name="Urraca", rationale="receive")

    result = M.materialize(db, 1, 18, battle_h2)
    assert not result.skipped
    data = battle_h2.read_bytes()
    blocks = find_order_blocks(data)
    assert read_placement(data, blocks[308].name_end, 1) == (-9, 7)
    assert read_spell_queue(data, blocks[308].name_end) == [
        244, SINGLE_ROUND_CODES["hold_one_turn"], -1, -1, -1]
    assert read_carried_gems(data, blocks[297].name_end).get("fire", 0) == 0
    assert read_carried_gems(data, blocks[9].name_end)["fire"] == 3
    assert H2.gem_remaining(data) == before_gems
    once = data
    M.materialize(db, 1, 18, battle_h2)
    assert battle_h2.read_bytes() == once


def test_troop_assignment_materializes_and_is_idempotent(db, troop_h2):
    db.execute("UPDATE games SET nation_id=61 WHERE id=1")
    db.commit()
    M.record_troop_assignments(
        db, 1, 30, [(800, 221, "Pikeneer")], 308, 1,
        commander_name="Bruise", rationale="join the capital squad")

    result = M.materialize(db, 1, 30, troop_h2)
    assert not result.skipped
    assert result.written == ["Pikeneer #800 -> Bruise squad 1"]
    first = troop_h2.read_bytes()
    moved = next(u for u in read_h2_units(first, 61)
                 if u.instance_id == 800 and not u.is_mount)
    assert moved.warband == (17 << 16) | 57831

    second = M.materialize(db, 1, 30, troop_h2)
    assert not second.skipped
    assert troop_h2.read_bytes() == first


def test_squad_creation_materializes_and_is_idempotent(db, new_squad_h2):
    db.execute("UPDATE games SET nation_id=61 WHERE id=1")
    db.commit()
    M.record_squad_creation(
        db, 1, 30, [(966, 218, "Crossbowman")], 308, 0, 52964,
        commander_name="Bruise", rationale="form a ranged squad")

    result = M.materialize(db, 1, 30, new_squad_h2)
    assert not result.skipped
    assert result.written == [
        "commander 308 squad 0: created with id 52964",
        "Crossbowman #966 -> Bruise squad 0",
    ]
    first = new_squad_h2.read_bytes()
    block = find_order_blocks(first)[308]
    created = next(row for row in read_squad_orders(first, block.name_end)
                   if row["slot"] == 0)
    assert (created["squad_id"], created["x"], created["y"],
            created["stance"], created["target"],
            created["formation"]) == (52964, 0, 0, 0, 0, 0)
    moved = next(u for u in read_h2_units(first, 61)
                 if u.instance_id == 966 and not u.is_mount)
    assert moved.warband == (17 << 16) | 52964

    second = M.materialize(db, 1, 30, new_squad_h2)
    assert not second.skipped
    assert new_squad_h2.read_bytes() == first


def test_troop_detachment_materializes_exact_client_bytes_and_is_idempotent(
        db, caelum_detach_h2):
    expected = (
        _SNAPSHOTS / "example_game_2" / "t3-auto-2" /
        "early_caelum.2h")
    db.execute("UPDATE games SET nation_id=24 WHERE id=1")
    db.commit()
    M.record_troop_detachments(
        db, 1, 3,
        [(instance_id, 2566, "Spire Horn Warrior")
         for instance_id in range(151, 161)],
        rationale="leave ten warriors in the capital garrison")

    result = M.materialize(db, 1, 3, caelum_detach_h2)
    assert not result.skipped
    assert len(result.written) == 10
    assert all("-> province garrison" in row for row in result.written)
    first = caelum_detach_h2.read_bytes()
    assert first[:-1] == expected.read_bytes()[:-1]

    second = M.materialize(db, 1, 3, caelum_detach_h2)
    assert not second.skipped
    assert caelum_detach_h2.read_bytes() == first


def test_carried_gem_overspend_is_refused_without_changing_the_file(
        db, battle_h2):
    from dom6_assistant.orders.orders_2h import read_carried_gems
    before = battle_h2.read_bytes()
    M.record_carried_gems(
        db, 1, 18, 9, 1, 1, commander_name="Urraca",
        rationale="no air gems exist")
    result = M.materialize(db, 1, 18, battle_h2)
    assert any("only 0 remain" in message for message in result.skipped)
    data = battle_h2.read_bytes()
    block = find_order_blocks(data)[9]
    assert read_carried_gems(data, block.name_end).get("air", 0) == 0
    assert data == before


def test_materialisation_is_idempotent(db, h2):
    """Same intent, same bytes — no matter how many times, or what came before.

    This is the property that makes the file safe to rebuild without inspecting
    it: whatever state it was left in, applying the current intent to a pristine
    base produces one answer.
    """
    M.record_order(db, 1, 9, TURGIS, "move", commander_name="Turgis",
                   destination=93)
    M.materialize(db, 1, 9, h2)
    once = h2.read_bytes()
    M.materialize(db, 1, 9, h2)
    M.materialize(db, 1, 9, h2)
    assert h2.read_bytes() == once


def test_reverting_to_a_targetless_order_clears_the_destination(db, h2):
    """Rebuilding from pristine leaves nothing behind from a dropped decision.

    NOTE: in-place patching also passes this today, because set_order zeroes the
    destination for targetless orders. The test is kept because it pins the
    behaviour the whole design exists to guarantee, for the fields the editor
    does not own yet — formation, stance, target and placement share this block
    and have no clearing path.
    """
    M.record_order(db, 1, 9, TURGIS, "move", commander_name="Turgis",
                   destination=93)
    M.materialize(db, 1, 9, h2)
    assert _fields(h2) == (1, 93)
    M.record_order(db, 1, 9, TURGIS, "defend", commander_name="Turgis")
    M.materialize(db, 1, 9, h2)
    assert _fields(h2) == (0, 0)


def test_dry_run_writes_nothing(db, h2):
    before = h2.read_bytes()
    M.record_order(db, 1, 9, TURGIS, "move", commander_name="Turgis",
                   destination=93)
    result = M.materialize(db, 1, 9, h2, dry_run=True)
    assert result.path is None
    assert result.written and h2.read_bytes() == before


def test_unverified_orders_are_refused_at_decision_time(db):
    """A bad order is rejected when decided, not silently dropped at write time.

    Refusing late would leave the decision recorded and unapplied, which reads as
    "ordered" to anything querying intent while the turn resolves otherwise.
    """
    with pytest.raises(ValueError, match="not verified"):
        M.record_order(db, 1, 9, TURGIS, "teleport", commander_name="Turgis")
    assert db.execute("SELECT COUNT(*) FROM order_intent").fetchone()[0] == 0


def test_unknown_commander_is_skipped_not_approximated(db, h2):
    """An order we cannot place must not be written to whoever is nearby."""
    M.record_order(db, 1, 9, 99999, "defend", commander_name="Nobody")
    result = M.materialize(db, 1, 9, h2, dry_run=True)
    assert not result.ok and result.skipped and not result.written


def test_other_commanders_orders_are_untouched(db, h2):
    """Writing one order must not disturb the rest of the file."""
    before = {o.commander_name: str(o) for o in read_orders(h2)}
    M.record_order(db, 1, 9, TURGIS, "move", commander_name="Turgis",
                   destination=93)
    M.materialize(db, 1, 9, h2)
    after = {o.commander_name: str(o) for o in read_orders(h2)}
    assert set(before) == set(after)
    changed = [n for n in before if before[n] != after[n]]
    assert changed == ["Turgis"], f"unexpected changes: {changed}"


def test_pristine_base_survives_writes(db, h2):
    """The base is captured before the first write and never re-captured.

    If it were refreshed from an already-modified file, the "pristine" copy would
    carry earlier edits and every guarantee here would quietly stop holding.
    """
    original = h2.read_bytes()
    M.record_order(db, 1, 9, TURGIS, "move", commander_name="Turgis",
                   destination=93)
    M.materialize(db, 1, 9, h2)
    M.materialize(db, 1, 9, h2)
    assert M.pristine_base(h2).read_bytes() == original


def test_pristine_base_refreshes_when_the_game_advances_a_turn(tmp_path):
    old = _SNAPSHOTS / "t25-research-before" / "mid_marignon.2h"
    new = _SNAPSHOTS / "t30-ritual-before-augury" / "mid_marignon.2h"
    if not old.exists() or not new.exists():
        pytest.skip("cross-turn snapshots absent")
    live = tmp_path / "mid_marignon.2h"
    live.write_bytes(new.read_bytes())
    base = live.with_suffix(M.BASE_SUFFIX)
    base.write_bytes(old.read_bytes())
    assert M.pristine_base(live).read_bytes() == new.read_bytes()


def test_research_intent_materializes_and_is_idempotent(db, research_h2):
    queue = [1, 1, 2, 0, 3, 4, 5, 6, 0]
    M.record_research_queue(db, 1, 25, queue, rationale="controlled queue")
    result = M.materialize(db, 1, 25, research_h2)

    assert result.written == [
        "research: Alteration 1 -> Alteration 2 -> Evocation 1 -> "
        "Conjuration 4 -> Construction 2 -> Enchantment 1 -> "
        "Thaumaturgy 2 -> Blood Magic 1 -> Conjuration 5"]
    written = research_h2.read_bytes()
    assert H2.read_research_queue(
        written, [3, 0, 0, 1, 0, 1, 0], [9, 0, 0, 0, 0, 0, 0]) == queue
    assert result.changed_bytes > 0

    M.materialize(db, 1, 25, research_h2)
    assert research_h2.read_bytes() == written


def test_level_nine_spell_ids_materialize_in_the_same_queue(db, research_h2):
    queue = [0, 0, 0, 0, 0, 1080, 1078]
    M.record_research_queue(db, 1, 25, queue,
                            rationale="Conjuration eight then two spells")
    result = M.materialize(db, 1, 25, research_h2)

    assert result.written == [
        "research: Conjuration 4 -> Conjuration 5 -> Conjuration 6 -> "
        "Conjuration 7 -> Conjuration 8 -> Level 9 spell 1080 -> "
        "Level 9 spell 1078"]
    assert H2.read_research_queue(
        research_h2.read_bytes(), [3, 0, 0, 1, 0, 1, 0],
        [9, 0, 0, 0, 0, 0, 0]) == queue


def test_province_defence_matches_client_authored_bytes(db, pd_h2):
    """The writer reproduces two independent province blocks and gold costs."""
    baseline = pd_h2.read_bytes()
    authored = (_SNAPSHOTS / "t30-pd-copper2-marignon26" /
                "mid_marignon.2h").read_bytes()
    M.record_province_defence(
        db, 1, 30, 86, 2, rationale="guard the exposed canyon")
    M.record_province_defence(
        db, 1, 30, 93, 26, rationale="strengthen the capital")

    result = M.materialize(db, 1, 30, pd_h2)
    assert not result.skipped
    generated = pd_h2.read_bytes()
    # Client saves change their ordinary two-byte trailer. It is non-semantic
    # and every existing writer deliberately preserves the pristine value.
    assert generated[:-2] == authored[:-2]
    assert generated[-2:] == baseline[-2:]
    assert H2.province_defence(generated, 86, [86, 93, 98]) == 2
    assert H2.province_defence(generated, 93, [86, 93, 98]) == 26
    assert H2.gold_remaining(generated) == 7140
    assert result.written == [
        "province 86: defence 1 -> 2 (+1 point(s), 2 gold committed)",
        "province 93: defence 25 -> 26 (+1 point(s), 26 gold committed)",
    ]

    once = generated
    M.materialize(db, 1, 30, pd_h2)
    assert pd_h2.read_bytes() == once


def test_replacing_saved_defence_purchase_refunds_gold(db, tmp_path):
    snapshot = _SNAPSHOTS / "t30-pd-copper2"
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    path = tmp_path / "mid_marignon.2h"
    assert H2.gold_remaining(path.read_bytes()) == 7166

    M.record_province_defence(
        db, 1, 30, 86, 1, rationale="refund this turn's purchase")
    result = M.materialize(db, 1, 30, path)

    assert not result.skipped
    data = path.read_bytes()
    assert H2.province_defence(data, 86, [86, 93, 98]) == 1
    assert H2.gold_remaining(data) == 7168


def test_remaining_gold_anchor_uses_the_file_nation(pd_h2):
    """The anchor embeds nation id; it is not Marignon-specific."""
    original = pd_h2.read_bytes()
    before = H2.gold_remaining(original)
    assert before is not None
    marignon_pattern = (
        H2._GOLD_PREFIX + struct.pack("<I", 61) + H2._GOLD_SUFFIX
    )
    at = original.find(marignon_pattern)
    assert at >= 0

    atlantis = bytearray(original)
    struct.pack_into("<H", atlantis, H2.OFF_NATION, 43)
    struct.pack_into("<I", atlantis, at + len(H2._GOLD_PREFIX), 43)

    assert H2.gold_remaining(bytes(atlantis)) == before
    changed = H2.set_gold_remaining_value(bytes(atlantis), before - 195)
    assert H2.gold_remaining(changed) == before - 195


def test_province_defence_cannot_refund_below_turn_start(db, pd_h2):
    M.record_province_defence(
        db, 1, 30, 93, 24, rationale="illegal refund")
    result = M.materialize(db, 1, 30, pd_h2, dry_run=True)

    assert any("below the turn-start level 25" in row
               for row in result.skipped)
    assert not result.written


def test_gold_overspend_aborts_province_defence_and_mercenary_bid(
        db, pd_h2):
    """Individually valid commitments may not produce a partial turn file."""
    before = pd_h2.read_bytes()
    M.record_province_defence(
        db, 1, 30, 86, 100, rationale="maximum local defence")
    M.record_mercenary_bid(
        db, 1, 30, 0, "Nergash's Damned Legion", 3000, 86,
        quoted_minimum=700,
        rationale="also hire the legion")

    result = M.materialize(db, 1, 30, pd_h2)

    assert result.aborted
    assert result.path is None
    assert not result.written
    assert any("implausible remaining gold" in row for row in result.skipped)
    assert pd_h2.read_bytes() == before


# -- the pristine base, and telling our own output from the player's ------

@pytest.fixture
def live_save(tmp_path):
    """A turn-30 .2h and its .trn, standing in for the live save."""
    snapshot = _SNAPSHOTS / "t30-pd-baseline"
    if not (snapshot / "mid_marignon.2h").exists():
        pytest.skip("turn-30 baseline absent")
    h2_path = tmp_path / "mid_marignon.2h"
    shutil.copy2(snapshot / "mid_marignon.2h", h2_path)
    shutil.copy2(_SNAPSHOTS / "t30-merc-bid-747" / "mid_marignon.trn",
                 tmp_path / "mid_marignon.trn")
    return h2_path


def test_our_own_output_does_not_re_base(db, live_save):
    """Rebuilding from our own last write would bake in its staleness."""
    M.record_province_defence(db, 1, 30, 86, 2, rationale="hold the border")
    M.materialize(db, 1, 30, live_save)
    assert M.base_is_stale(live_save) is None

    # a second materialisation must still see the ORIGINAL file as its base
    base = live_save.with_suffix(M.BASE_SUFFIX).read_bytes()
    M.materialize(db, 1, 30, live_save)
    assert live_save.with_suffix(M.BASE_SUFFIX).read_bytes() == base


def test_a_player_save_is_detected_and_re_based(db, live_save):
    """The failure this exists to stop: the player saves inside our turn.

    Before this, the base only refreshed when the turn number changed, so
    orders the player issued mid-turn were rebuilt away — and the result was a
    perfectly consistent file, which is why nothing downstream could notice.
    """
    M.record_province_defence(db, 1, 30, 86, 2, rationale="hold the border")
    M.materialize(db, 1, 30, live_save)

    # the player now saves in game: two mercenary bids appear
    played = H2.set_mercenary_bid(live_save.read_bytes(), 0, 900, 86)
    played = H2.set_mercenary_bid(played, 1, 611, 98)
    played = H2.set_gold_remaining_value(played,
                                         H2.gold_remaining(played) - 1511)
    live_save.write_bytes(played)

    assert "player saving in game" in (M.base_is_stale(live_save) or "")
    M.materialize(db, 1, 30, live_save)

    # their bids survive our rebuild, and so does the gold they reserved
    kept = live_save.read_bytes()
    assert [(b.slot, b.amount) for b in H2.read_mercenary_bids(kept)] == [
        (0, 900), (1, 611)]
    assert H2.gold_remaining(kept) == H2.gold_remaining(played)


def test_a_turn_advance_still_re_bases(db, live_save):
    """The original rule, which the digest check must not have replaced."""
    M.materialize(db, 1, 30, live_save, dry_run=True)
    M.pristine_base(live_save)
    data = bytearray(live_save.read_bytes())
    struct.pack_into("<I", data, 14, 31)
    live_save.write_bytes(bytes(data))
    assert "turn advanced from 30 to 31" in (M.base_is_stale(live_save) or "")


def test_reset_base_clears_both_files(db, live_save):
    """The explicit control, for cases detection cannot see."""
    M.record_province_defence(db, 1, 30, 86, 2, rationale="hold")
    M.materialize(db, 1, 30, live_save)
    assert live_save.with_suffix(M.BASE_SUFFIX).exists()
    assert live_save.with_suffix(M.WRITTEN_SUFFIX).exists()

    M.reset_base(live_save)
    assert not live_save.with_suffix(M.BASE_SUFFIX).exists()
    assert not live_save.with_suffix(M.WRITTEN_SUFFIX).exists()
    assert M.base_is_stale(live_save) == "no base has been captured for this file yet"


def test_a_sandbox_write_does_not_claim_the_live_base(db, live_save, tmp_path):
    """out_path is a throwaway copy; its bytes are not the live file's state."""
    M.record_province_defence(db, 1, 30, 86, 2, rationale="hold")
    M.materialize(db, 1, 30, live_save, out_path=tmp_path / "copy.2h")
    assert not live_save.with_suffix(M.WRITTEN_SUFFIX).exists()
