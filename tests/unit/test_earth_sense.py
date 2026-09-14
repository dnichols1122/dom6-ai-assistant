"""Earth Sense: an implicit-current-province effect-82 ritual."""
import shutil
import sqlite3
import struct
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.orders.orders_2h import find_order_blocks, read_ritual_fields


ROOT = Path("knowledge/snapshots/example_game")
BASELINE = ROOT / "t49-auto"
ORDERED = ROOT / "t49-auto-2"
GAME_DB = Path("knowledge/game.sqlite3")
MAMBO = 124
EARTH_SENSE = 1319
MUDWATER_CAVES = 45


def _session(tmp_path: Path):
    if not (BASELINE / "mid_ermor.2h").exists() or not GAME_DB.exists():
        pytest.skip("Earth Sense control snapshots are absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_ermor.trn", "mid_ermor.2h"):
        shutil.copy2(BASELINE / name, save / name)
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


def test_client_order_writes_the_casters_current_province():
    data = (ORDERED / "mid_ermor.2h").read_bytes()
    block = find_order_blocks(data)[MAMBO]
    fields = read_ritual_fields(data, block.name_end)
    assert fields is not None
    assert (fields.spell_id, fields.gem_cost, fields.target_province) == (
        EARTH_SENSE, 6, MUDWATER_CAVES,
    )
    assert fields.target_commander_runtime_index is None
    assert fields.target_unit_runtime_index is None
    assert struct.unpack_from("<ii", data, block.name_end + 128) == (-1, -1)


def test_tool_reproduces_the_client_authored_base_cast(tmp_path):
    session = _session(tmp_path)
    try:
        spells = _ok(session.call("list_castable_spells", {
            "commander_id": MAMBO, "kind": "ritual",
        }))["spells"]
        earth_sense = next(row for row in spells if row["id"] == EARTH_SENSE)
        assert earth_sense["order_supported"] is True
        assert earth_sense["target"] == "caster_current_province_implicit"
        assert earth_sense["extra_gems_effect"] == (
            "extends duration by 3 months per gem")
        assert earth_sense["duration_extension_months_per_gem"] == 3
        assert earth_sense["monthly_supported"] is True

        recorded = _ok(session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": EARTH_SENSE,
            "rationale": "sense movement through Mudwater Caves",
        }))
        assert recorded["target_province"] == MUDWATER_CAVES
        assert recorded["target_name"] == "Mudwater Caves"
        assert recorded["gem_cost"] == 6
        assert recorded["duration_extension_gems"] == 0
        assert recorded["duration_extension_months"] == 0
        intent = next(
            row for row in _ok(session.call("get_orders", {}))
            if row.get("commander_id") == MAMBO
        )
        assert intent["target_mode"] == "caster_current_province_implicit"
        assert intent["duration_extension_gems"] == 0
        assert intent["duration_extension_months"] == 0

        result = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not result["skipped"]
        actual = session.ctx.h2_path.read_bytes()
        order = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == MAMBO
        )["order_parameter_in_file"]
        assert order["target_province"] == MUDWATER_CAVES
        assert order["target_province_name"] == "Mudwater Caves"
        assert order["target_mode"] == "caster_current_province_implicit"
        assert order["duration_extension_gems"] == 0
        assert order["duration_extension_months"] == 0
    finally:
        session.close()

    expected = (ORDERED / "mid_ermor.2h").read_bytes()
    # This client save changed two trailer/check bytes; the writer leaves the
    # pristine trailer alone and must match everything semantic before it.
    assert actual[:-2] == expected[:-2]
    assert H2.gem_remaining(actual)[3] == 14


def test_extra_gems_extend_duration_and_reserve_the_total(tmp_path):
    session = _session(tmp_path)
    try:
        recorded = _ok(session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": EARTH_SENSE,
            "extra_gems": 3,
            "rationale": "keep Earth Sense active longer",
        }))
        assert recorded["gem_cost"] == 9
        assert recorded["base_gem_cost"] == 6
        assert recorded["duration_extension_gems"] == 3
        assert recorded["duration_extension_months"] == 9
        _ok(session.call("materialize_orders", {"confirm": True}))
        data = session.ctx.h2_path.read_bytes()
    finally:
        session.close()
    fields = read_ritual_fields(data, find_order_blocks(data)[MAMBO].name_end)
    assert fields is not None and fields.gem_cost == 9
    assert fields.target_province == MUDWATER_CAVES
    assert H2.gem_remaining(data)[3] == 11


def test_implicit_local_ritual_refuses_explicit_but_supports_monthly(tmp_path):
    session = _session(tmp_path)
    try:
        explicit = session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": EARTH_SENSE,
            "target_province": MUDWATER_CAVES,
            "rationale": "the client chooses this implicitly",
        })
        assert not explicit["ok"] and "omit target_province" in explicit["error"]

        monthly = _ok(session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": EARTH_SENSE,
            "monthly": True,
            "rationale": "repeat the province enchantment while affordable",
        }))
        assert monthly["monthly"] is True
    finally:
        session.close()
