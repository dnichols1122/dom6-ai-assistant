"""Forging as a public write path.

Everything here rests on two controlled saves: a Fire Sword at turn 30 and a
rebated Holy Scourge plus a national Mercybrand at turn 33. The writer's job is
to reproduce those bytes from stored intent and to reserve the same gems.
"""
import shutil
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.orders import materialize as M
from dom6_assistant.orders.orders_2h import (
    OrdersEditor, find_order_blocks, read_forge_fields,
)

SNAPSHOTS = Path("knowledge/snapshots")
CLEAN = SNAPSHOTS / "t33-construction3-clean"
FORGED = SNAPSHOTS / "t33-forge-rebate" / "mid_marignon.2h"
BRETAIGNE, GUARLAN, TURGIS, SUGAAR, BRUISE = 126, 314, 307, 297, 308
HOLY_SCOURGE, MERCYBRAND, SWORD_OF_JUSTICE = 11, 135, 100
CROWN_OF_THE_SHAH, FLAMBEAU, GOLDEN_BARDING = 225, 14, 493
MULTI_CLEAN = SNAPSHOTS / "t44-auto-6"
MULTI_FORGED = SNAPSHOTS / "t44-auto-7" / "mid_marignon.2h"


def _require(*paths):
    for path in paths:
        if not path.exists():
            pytest.skip(f"{path} absent")


@pytest.fixture
def save(tmp_path):
    _require(CLEAN / "mid_marignon.2h", FORGED)
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(CLEAN / name, tmp_path / name)
    return tmp_path / "mid_marignon.2h"


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        Path("src/dom6_assistant/gamestate/schema.sql").read_text())
    conn.execute("INSERT INTO games(id, name) VALUES(1, 'test')")
    conn.commit()
    return conn


@pytest.fixture
def reference():
    conn = sqlite3.connect("knowledge/reference/reference.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def session(tmp_path):
    _require(CLEAN / "mid_marignon.2h")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(CLEAN / name, tmp_path / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2("knowledge/game.sqlite3", game_db)
    s = open_session(save_dir=tmp_path, game_db=game_db)
    yield s
    s.close()


@pytest.fixture
def multi_session(tmp_path):
    _require(MULTI_CLEAN / "mid_marignon.2h", MULTI_FORGED)
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(MULTI_CLEAN / name, tmp_path / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2("knowledge/game.sqlite3", game_db)
    s = open_session(save_dir=tmp_path, game_db=game_db)
    yield s
    s.close()


def test_the_editor_reproduces_the_game_authored_forges(save):
    """Two mages, two items, byte-for-byte apart from the trailer."""
    editor = OrdersEditor(save)
    editor.set_forge(BRETAIGNE, HOLY_SCOURGE, 4)
    editor.set_forge(GUARLAN, MERCYBRAND, 10)
    data = bytes(editor.data)
    gems = list(H2.gem_remaining(data))
    gems[0] -= 14                       # fire: 4 + 10
    data = H2.set_gem_remaining(data, gems)

    game = FORGED.read_bytes()
    changed = [i for i in range(len(data)) if data[i] != game[i]]
    assert changed == [len(game) - 2, len(game) - 1]


def test_multi_path_forge_fields_and_editor_match_the_client(tmp_path):
    _require(MULTI_CLEAN / "mid_marignon.2h", MULTI_FORGED)
    clean = MULTI_CLEAN / "mid_marignon.2h"
    game = MULTI_FORGED.read_bytes()
    fields = read_forge_fields(
        game, find_order_blocks(game)[BRETAIGNE].name_end)
    assert fields.item_id == GOLDEN_BARDING
    assert fields.gem_cost == 4
    assert fields.secondary_gem_cost == 4

    work = tmp_path / "mid_marignon.2h"
    shutil.copy2(clean, work)
    editor = OrdersEditor(work)
    editor.set_forge(BRETAIGNE, GOLDEN_BARDING, 4, 4)
    data = bytes(editor.data)
    gems = list(H2.gem_remaining(data))
    gems[0] -= 4
    gems[4] -= 4
    data = H2.set_gem_remaining(data, gems)
    assert data[:-1] == game[:-1]


def test_multi_path_materialization_matches_the_client(db, reference, tmp_path):
    _require(MULTI_CLEAN / "mid_marignon.2h", MULTI_FORGED)
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(MULTI_CLEAN / name, tmp_path / name)
    path = tmp_path / "mid_marignon.2h"
    M.record_forge(
        db, 1, 44, BRETAIGNE, GOLDEN_BARDING, 0, 4,
        secondary_gem_path=4, secondary_gem_cost=4,
        commander_name="Bretaigne", item_name="Golden Barding",
        rationale="controlled multi-path forge")
    result = M.materialize(db, 1, 44, path, reference_conn=reference)

    assert not result.skipped, result.skipped
    assert path.read_bytes()[:-1] == MULTI_FORGED.read_bytes()[:-1]


def test_materialising_forge_intent_reproduces_them_too(save, db, reference):
    M.record_forge(db, 1, 33, BRETAIGNE, HOLY_SCOURGE, 0, 4,
                   commander_name="Bretaigne", item_name="Holy Scourge",
                   rationale="rebated national item")
    M.record_forge(db, 1, 33, GUARLAN, MERCYBRAND, 0, 10,
                   commander_name="Guarlan", item_name="Mercybrand",
                   rationale="national weapon")
    result = M.materialize(db, 1, 33, save, reference_conn=reference)

    assert not result.skipped, result.skipped
    game = FORGED.read_bytes()
    data = save.read_bytes()
    assert [i for i in range(len(data)) if data[i] != game[i]] == [
        len(game) - 2, len(game) - 1]


def test_replacing_a_forge_refunds_before_charging(save, db, reference):
    """Two forges recorded for one commander must not both reserve gems.

    The old reservation exists only in the file, so the refund has to read it
    back — the same problem rituals have, and the same solution.
    """
    before = H2.gem_remaining(save.read_bytes())[0]
    for item, cost in ((MERCYBRAND, 10), (HOLY_SCOURGE, 4)):
        M.record_forge(db, 1, 33, BRETAIGNE, item, 0, cost,
                       commander_name="Bretaigne", rationale="reconsidered")
    M.materialize(db, 1, 33, save, reference_conn=reference)

    data = save.read_bytes()
    block = find_order_blocks(data)[BRETAIGNE]
    assert read_forge_fields(data, block.name_end).item_id == HOLY_SCOURGE
    assert H2.gem_remaining(data)[0] == before - 4


def test_cross_family_gem_overspend_aborts_every_order(save, db, reference):
    """Forges and empowerment share one national treasury transaction."""
    before = save.read_bytes()
    M.record_forge(
        db, 1, 33, BRETAIGNE, MERCYBRAND, 0, 10,
        commander_name="Bretaigne", item_name="Mercybrand", rationale="forge")
    M.record_forge(
        db, 1, 33, GUARLAN, MERCYBRAND, 0, 10,
        commander_name="Guarlan", item_name="Mercybrand", rationale="forge")
    M.record_empowerment(
        db, 1, 33, BRUISE, 0, 2, 30,
        commander_name="Bruise", rationale="empower")

    result = M.materialize(db, 1, 33, save, reference_conn=reference)

    assert result.aborted
    assert result.path is None
    assert not result.written
    assert any("only 6" in row for row in result.skipped)
    assert save.read_bytes() == before


def test_a_forge_already_in_the_file_is_refunded_when_replaced(db, reference,
                                                              tmp_path):
    """Starting from the game's own two-forge save, replace one of them."""
    _require(FORGED)
    h2_path = tmp_path / "mid_marignon.2h"
    shutil.copy2(FORGED, h2_path)
    shutil.copy2(CLEAN / "mid_marignon.trn", tmp_path / "mid_marignon.trn")
    before = H2.gem_remaining(h2_path.read_bytes())[0]

    # Guarlan drops Mercybrand (10 reserved in the file) for Holy Scourge (4)
    M.record_forge(db, 1, 33, GUARLAN, HOLY_SCOURGE, 0, 4,
                   commander_name="Guarlan", rationale="cheaper")
    M.materialize(db, 1, 33, h2_path, reference_conn=reference)

    assert H2.gem_remaining(h2_path.read_bytes())[0] == before + 10 - 4


def test_the_tool_reserves_the_discounted_cost(session):
    result = session.call("forge_item", {
        "commander_id": BRETAIGNE, "item_id": HOLY_SCOURGE,
        "rationale": "rebated, cheapest way to arm a mage"})
    assert result["ok"], result.get("error")
    out = result["result"]
    assert out["gems_reserved"] == {"fire": 4}
    assert "one-gem" in out["national_rebate"]


def test_tool_writes_both_golden_barding_reservations(multi_session):
    result = multi_session.call("forge_item", {
        "commander_id": BRETAIGNE, "item_id": GOLDEN_BARDING,
        "rationale": "arm a proud steed"})
    assert result["ok"], result.get("error")
    assert result["result"]["gems_reserved"] == {"fire": 4, "astral": 4}
    recorded = multi_session.call("get_orders", {})
    assert recorded["ok"], recorded.get("error")
    forge_intent = next(
        row for row in recorded["result"] if row["order"] == "forge_magic_item"
    )
    assert forge_intent["gem_reservations"] == {"fire": 4, "astral": 4}
    written = multi_session.call("materialize_orders", {"confirm": True})
    assert written["ok"], written.get("error")
    assert multi_session.ctx.h2_path.read_bytes()[:-1] == (
        MULTI_FORGED.read_bytes()[:-1])
    commanders = multi_session.call("list_commanders", {})
    assert commanders["ok"], commanders.get("error")
    bretaigne = next(
        row for row in commanders["result"]["commanders"]
        if row["commander_id"] == BRETAIGNE
    )
    assert bretaigne["order_parameter_in_file"] == {
        "item_id": GOLDEN_BARDING,
        "item_name": "Golden Barding",
        "gem_reservations": {"fire": 4, "astral": 4},
        "primary_gem_path": 0,
        "primary_gem_cost": 4,
        "secondary_gem_path": 4,
        "secondary_gem_cost": 4,
    }


def test_a_nation_restricted_item_is_refused(session):
    result = session.call("forge_item", {
        "commander_id": SUGAAR, "item_id": CROWN_OF_THE_SHAH,
        "rationale": "belongs to another nation"})
    assert not result["ok"]
    assert "restricted to nation id(s) [105]" in result["error"]


def test_restricted_items_are_absent_from_the_candidate_list(session):
    listing = session.call("list_forgeable_items", {
        "commander_id": SUGAAR, "include_unresearched": True})
    assert listing["ok"], listing.get("error")
    ids = {item["id"] for item in listing["result"]["items"]}
    assert CROWN_OF_THE_SHAH not in ids


def test_inept_smith_changes_paths_not_cost_and_does_not_block_sugaar(session):
    listing = session.call("list_forgeable_items", {
        "commander_id": SUGAAR})
    assert listing["ok"], listing.get("error")
    shown = listing["result"]
    assert shown["paths"] == {"F": 6, "A": 6}
    assert shown["forge_path_adjustment"] == -1
    assert shown["effective_forge_paths"] == {"F": 5, "A": 5}
    assert "Inept Smith (-1)" in shown["forge_path_adjustment_sources"][0]
    assert shown["writer_status"] == "exact client-derived forge costs are writable"

    result = session.call("forge_item", {
        "commander_id": SUGAAR, "item_id": 1,
        "rationale": "inept smith changes eligibility, not the Fire Sword price"})
    assert result["ok"], result.get("error")
    assert result["result"]["gems_reserved"] == {"fire": 5}


def test_a_worn_dwarven_hammer_uses_the_client_fixed_reduction(session):
    editor = OrdersEditor(session.ctx.h2_path)
    editor.set_equipment(BRETAIGNE, "weapon", 29)  # Dwarven Hammer
    editor.save(backup=False)

    result = session.call("forge_item", {
        "commander_id": BRETAIGNE, "item_id": HOLY_SCOURGE,
        "rationale": "hammer would reduce the price"})

    assert result["ok"], result.get("error")
    out = result["result"]
    # Base 5, hammer -2, then the national one-gem rebate: 5 -> 3 -> 2.
    assert out["gems_reserved"] == {"fire": 2}
    modifiers = out["forge_cost_modifiers"]
    assert modifiers["effective_fixed"] == 2
    assert any("Dwarven Hammer" in source for source in modifiers["sources"])

    before = H2.gem_remaining(session.ctx.h2_path.read_bytes())[0]
    written = session.call("materialize_orders", {"confirm": True})
    assert written["ok"], written.get("error")
    data = session.ctx.h2_path.read_bytes()
    forge = read_forge_fields(
        data, find_order_blocks(data)[BRETAIGNE].name_end)
    assert forge.gem_cost == 2
    assert H2.gem_remaining(data)[0] == before - 2


def test_replacing_an_existing_multi_path_forge_refunds_both_paths(
        db, reference, tmp_path):
    _require(MULTI_CLEAN / "mid_marignon.trn", MULTI_FORGED)
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        source = MULTI_FORGED if name.endswith(".2h") else MULTI_CLEAN / name
        shutil.copy2(source, tmp_path / name)
    path = tmp_path / "mid_marignon.2h"
    M.record_order(db, 1, 44, BRETAIGNE, "research", commander_name="Bretaigne",
                   rationale="cancel the forge and resume research")

    result = M.materialize(db, 1, 44, path, reference_conn=reference)

    assert not result.skipped, result.skipped
    assert H2.gem_remaining(path.read_bytes())[0] == 51
    assert H2.gem_remaining(path.read_bytes())[4] == 11
    assert path.read_bytes()[:-1] == (
        MULTI_CLEAN / "mid_marignon.2h").read_bytes()[:-1]


def test_an_unreachable_item_is_refused_with_its_requirement(session):
    result = session.call("forge_item", {
        "commander_id": BRETAIGNE, "item_id": FLAMBEAU,
        "rationale": "needs Construction 5"})
    assert not result["ok"]
    assert "Construction 5" in result["error"]


def test_a_commander_with_no_path_cannot_forge(session):
    result = session.call("forge_item", {
        "commander_id": TURGIS, "item_id": 1, "rationale": "paladin"})
    assert not result["ok"]
    assert "no visible non-Holy magic path" in result["error"]


def test_record_order_redirects_to_the_forge_tool(session):
    """Recording a forge here would write the order with no reservation."""
    result = session.call("record_order", {
        "commander_id": BRETAIGNE, "order": "forge_magic_item",
        "item_id": 1, "rationale": "wrong path"})
    assert not result["ok"]
    assert "use forge_item" in result["error"]


def test_forging_is_no_longer_withheld():
    from dom6_assistant.orders import orders_2h as O
    assert "forge_magic_item" not in O.ECONOMIC_ORDERS_PENDING
    assert "empowerment" in O.ECONOMIC_ORDERS_PENDING
