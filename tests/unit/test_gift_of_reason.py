"""Gift of Reason's ordinary-unit selector, from order through promotion."""
import shutil
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.orders.orders_2h import find_order_blocks, read_ritual_fields


ROOT = Path("knowledge/snapshots/example_game")
BASELINE = ROOT / "t48-auto"
ORDERED = ROOT / "t48-auto-2"
RESOLVED = ROOT / "t49-auto"
GAME_DB = Path("knowledge/game.sqlite3")
MAMBO = 124
GIFT_OF_REASON = 1327
LICTOR_INSTANCE = 54
LICTOR_RUNTIME = 53
MINDLESS_LONGDEAD_INSTANCE = 625


def _session(tmp_path: Path, snapshot: Path):
    if not (snapshot / "mid_ermor.2h").exists() or not GAME_DB.exists():
        pytest.skip("Gift of Reason control snapshots are absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_ermor.trn", "mid_ermor.2h"):
        shutil.copy2(snapshot / name, save / name)
    db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, db)
    conn = sqlite3.connect(db)
    try:
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE '%_intent'"
            )
        ]
        for table in tables:
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


def test_client_order_names_the_troop_runtime_handle_twice():
    data = (ORDERED / "mid_ermor.2h").read_bytes()
    fields = read_ritual_fields(data, find_order_blocks(data)[MAMBO].name_end)
    assert fields is not None
    assert (fields.spell_id, fields.gem_cost, fields.monthly) == (
        GIFT_OF_REASON, 20, False,
    )
    assert fields.target_unit_runtime_index == LICTOR_RUNTIME
    assert fields.target_province is None
    assert fields.target_commander_runtime_index is None


def test_resolution_promotes_the_same_stable_instance(tmp_path):
    session = _session(tmp_path, RESOLVED)
    try:
        promoted = next(
            commander for commander in session.ctx.view.own_commanders(
                session.ctx.h2_path
            )
            if commander.unit_instance_id == LICTOR_INSTANCE
        )
    finally:
        session.close()
    assert promoted.commander_id == 149
    assert promoted.name == "Meikru Zoth"
    assert promoted.type_id == 259


def test_tool_materializes_the_exact_client_semantics(tmp_path):
    session = _session(tmp_path, BASELINE)
    try:
        spells = _ok(session.call(
            "list_castable_spells",
            {"commander_id": MAMBO, "kind": "ritual"},
        ))["spells"]
        gift = next(row for row in spells if row["id"] == GIFT_OF_REASON)
        assert gift["order_supported"] is True
        assert gift["target_parameter"] == "target_unit"

        units = _ok(session.call(
            "list_units", {"province_id": 45, "include_instances": True}
        ))
        selected = next(row for row in units if row["instance_id"] == LICTOR_INSTANCE)
        assert (selected["unit_type_id"], selected["name"]) == (259, "Lictor")

        recorded = _ok(session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": GIFT_OF_REASON,
            "target_unit": LICTOR_INSTANCE,
            "rationale": "give this Lictor independent command",
        }))
        assert recorded["target_unit"] == LICTOR_INSTANCE
        assert recorded["target_unit_name"] == "Lictor"
        assert recorded["gem_cost"] == 20

        intent = next(
            row for row in _ok(session.call("get_orders", {}))
            if row.get("commander_id") == MAMBO
        )
        assert intent["target_unit"] == LICTOR_INSTANCE
        assert intent["target_unit_name"] == "Lictor"

        materialized = _ok(session.call(
            "materialize_orders", {"confirm": True}
        ))
        assert not materialized["skipped"]
        mambo = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == MAMBO
        )
        assert mambo["order_parameter_in_file"]["target_unit"] == LICTOR_INSTANCE
        assert mambo["order_parameter_in_file"]["target_unit_name"] == "Lictor"
        actual = session.ctx.h2_path.read_bytes()
    finally:
        session.close()

    expected = (ORDERED / "mid_ermor.2h").read_bytes()
    # The client owns the final checksum byte; materialisation intentionally
    # preserves the pristine trailer. Every semantic byte must match.
    assert actual[:-1] == expected[:-1]
    fields = read_ritual_fields(
        actual, find_order_blocks(actual)[MAMBO].name_end
    )
    assert fields is not None
    assert fields.target_unit_runtime_index == LICTOR_RUNTIME


def test_gift_of_reason_requires_a_local_ordinary_troop(tmp_path):
    session = _session(tmp_path, BASELINE)
    try:
        missing = session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": GIFT_OF_REASON,
            "rationale": "missing selector",
        })
        assert not missing["ok"] and "target_unit" in missing["error"]

        wrong_kind = session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": GIFT_OF_REASON,
            "target_unit": MAMBO,
            "rationale": "commanders are not troop targets",
        })
        assert not wrong_kind["ok"] and "ordinary troop" in wrong_kind["error"]

        monthly = session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": GIFT_OF_REASON,
            "target_unit": LICTOR_INSTANCE,
            "monthly": True,
            "rationale": "invalid repeated promotion",
        })
        assert not monthly["ok"] and "Monthly Ritual" in monthly["error"]

        mindless = session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": GIFT_OF_REASON,
            "target_unit": MINDLESS_LONGDEAD_INSTANCE,
            "rationale": "Gift of Reason cannot affect Mindless troops",
        })
        assert not mindless["ok"]
        assert "non-Mindless" in mindless["error"]
    finally:
        session.close()
