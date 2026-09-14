"""Divine Name's ordinary-unit selector and Mindless eligibility."""

import shutil
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.orders.orders_2h import (
    find_order_blocks,
    read_h2_units,
    read_ritual_fields,
)


ROOT = Path("knowledge/snapshots/example_game")
BASELINE = ROOT / "t45-auto-8"
KNIGHT_ORDER = ROOT / "t45-auto-9"
LONGDEAD_ORDER = ROOT / "t45-auto-10"
GAME_DB = Path("knowledge/game.sqlite3")
MAMBO = 124
DIVINE_NAME = 1349
KNIGHT_INSTANCE = 61
KNIGHT_RUNTIME = 60
LONGDEAD_INSTANCE = 56
LONGDEAD_RUNTIME = 55


def _session(tmp_path: Path, snapshot: Path):
    if not (snapshot / "mid_ermor.2h").exists() or not GAME_DB.exists():
        pytest.skip("Divine Name control snapshots are absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_ermor.trn", "mid_ermor.2h"):
        shutil.copy2(snapshot / name, save / name)
    db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, db)
    conn = sqlite3.connect(db)
    try:
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE '%_intent'"
        ):
            conn.execute(f'DELETE FROM "{table}"')
        conn.commit()
    finally:
        conn.close()
    return open_session(
        "example_game::mid_ermor", game_db=db, save_dir=save,
        nation_id=54,
    )


def _ok(result):
    assert result["ok"], result.get("error")
    return result["result"]


@pytest.mark.parametrize(
    ("snapshot", "instance_id", "runtime_index"),
    [
        (KNIGHT_ORDER, KNIGHT_INSTANCE, KNIGHT_RUNTIME),
        (LONGDEAD_ORDER, LONGDEAD_INSTANCE, LONGDEAD_RUNTIME),
    ],
)
def test_client_divine_name_duplicates_the_selected_troop_handle(
    snapshot, instance_id, runtime_index,
):
    data = (snapshot / "mid_ermor.2h").read_bytes()
    fields = read_ritual_fields(data, find_order_blocks(data)[MAMBO].name_end)
    assert fields is not None
    assert (fields.spell_id, fields.gem_cost, fields.monthly) == (
        DIVINE_NAME, 25, False,
    )
    assert fields.target_unit_runtime_index == runtime_index
    assert fields.target_province is None
    assert fields.target_commander_runtime_index is None
    selected = next(
        unit for unit in read_h2_units(data, 54)
        if unit.instance_id == instance_id
    )
    assert selected.runtime_index == runtime_index


@pytest.mark.parametrize(
    ("instance_id", "expected_snapshot", "expected_name"),
    [
        (KNIGHT_INSTANCE, KNIGHT_ORDER, "Knight of the Unholy Sepulchre"),
        (LONGDEAD_INSTANCE, LONGDEAD_ORDER, "Longdead"),
    ],
)
def test_tool_reproduces_both_divine_name_targets_exactly(
    tmp_path, instance_id, expected_snapshot, expected_name,
):
    session = _session(tmp_path, BASELINE)
    try:
        spells = _ok(session.call("list_castable_spells", {
            "commander_id": MAMBO, "kind": "ritual",
        }))["spells"]
        divine_name = next(row for row in spells if row["id"] == DIVINE_NAME)
        assert divine_name["order_supported"] is True
        assert divine_name["target_parameter"] == "target_unit"
        assert divine_name["target"] == (
            "own_troop_in_caster_province_including_mindless"
        )

        recorded = _ok(session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": DIVINE_NAME,
            "target_unit": instance_id,
            "rationale": "grant this troop a divine name",
        }))
        assert recorded["target_unit"] == instance_id
        assert recorded["target_unit_name"] == expected_name
        assert recorded["gem_cost"] == 25

        result = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not result["skipped"]
        actual = session.ctx.h2_path.read_bytes()
        file_order = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == MAMBO
        )["order_parameter_in_file"]
        assert file_order["target_unit"] == instance_id
        assert file_order["target_unit_name"] == expected_name
    finally:
        session.close()

    expected = (expected_snapshot / "mid_ermor.2h").read_bytes()
    # This snapshot pair changes both client-owned trailer bytes. The ritual,
    # gem reservation and order-code bytes must otherwise match exactly.
    assert actual[:-2] == expected[:-2]
    assert H2.gem_remaining(actual)[4] == 1


def test_divine_name_requires_one_shot_local_ordinary_troop(tmp_path):
    session = _session(tmp_path, BASELINE)
    try:
        monthly = session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": DIVINE_NAME,
            "target_unit": LONGDEAD_INSTANCE,
            "monthly": True,
            "rationale": "a troop can be promoted only once",
        })
        assert not monthly["ok"]
        assert "Monthly Ritual" in monthly["error"]
    finally:
        session.close()
