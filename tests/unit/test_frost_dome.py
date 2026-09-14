"""Frost Dome: a controlled implicit-local effect-82 dome ritual."""

import shutil
import sqlite3
import struct
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.file_reader.formats import trn as T
from dom6_assistant.orders.orders_2h import find_order_blocks, read_ritual_fields


ROOT = Path("knowledge/snapshots/example_game")
BASELINE = ROOT / "t57-auto-2"
ORDERED = ROOT / "t57-auto-3"
MONTHLY = ROOT / "t57-auto-4"
RESOLVED = ROOT / "t58-auto"
GAME_DB = Path("knowledge/game.sqlite3")
MAMBO = 124
FROST_DOME = 1196
ERMOR = 9


def _session(
    tmp_path: Path,
    snapshot: Path = BASELINE,
    stem: str = "mid_ermor",
    nation_id: int = 54,
):
    if not (snapshot / f"{stem}.2h").exists() or not GAME_DB.exists():
        pytest.skip("Frost Dome control snapshots are absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in (f"{stem}.trn", f"{stem}.2h"):
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
        f"example_game::{stem}", game_db=db, save_dir=save,
        nation_id=nation_id,
    )


def _ok(result):
    assert result["ok"], result.get("error")
    return result["result"]


def test_client_frost_dome_writes_its_casters_current_province():
    before = (BASELINE / "mid_ermor.2h").read_bytes()
    data = (ORDERED / "mid_ermor.2h").read_bytes()
    block = find_order_blocks(data)[MAMBO]
    fields = read_ritual_fields(data, block.name_end)
    assert fields is not None
    assert (fields.spell_id, fields.gem_cost, fields.target_province) == (
        FROST_DOME, 15, ERMOR,
    )
    assert fields.target_commander_runtime_index is None
    assert fields.target_unit_runtime_index is None
    assert struct.unpack_from("<ii", data, block.name_end + 128) == (-1, -1)
    assert H2.gem_remaining(before)[2] == 15
    assert H2.gem_remaining(data)[2] == 0


def test_resolved_turn_contains_the_exact_local_enchantment_record():
    before = T.parse(BASELINE / "mid_ermor.trn")
    ours = T.parse(RESOLVED / "mid_ermor.trn")
    foreign = T.parse(RESOLVED / "mid_marignon.trn")
    assert before.local_enchantments == []
    assert len(ours.local_enchantments) == 1
    effect = ours.local_enchantments[0]
    assert (
        effect.caster_runtime_index,
        effect.effect_argument,
        effect.spell_id,
        effect.caster_nation_id,
        effect.province_id,
        effect.months_left,
    ) == (3157, 62, FROST_DOME, 54, ERMOR, 1)
    # Raw presence is not visibility: Marignon's file carries the same state.
    assert foreign.local_enchantments == ours.local_enchantments


def test_tool_reproduces_the_client_authored_frost_dome(tmp_path):
    session = _session(tmp_path)
    try:
        spells = _ok(session.call("list_castable_spells", {
            "commander_id": MAMBO, "kind": "ritual",
        }))["spells"]
        frost_dome = next(row for row in spells if row["id"] == FROST_DOME)
        assert frost_dome["order_supported"] is True
        assert frost_dome["target"] == "caster_current_province_implicit"
        assert frost_dome["extra_gems_effect"] == (
            "extends duration by 1 month per gem")
        assert frost_dome["monthly_supported"] is True

        recorded = _ok(session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": FROST_DOME,
            "rationale": "protect Ermor with a one-month Frost Dome",
        }))
        assert recorded["target_province"] == ERMOR
        assert recorded["target_name"] == "Ermor"
        assert recorded["gem_cost"] == 15
        assert recorded["duration_extension_gems"] == 0
        assert recorded["duration_extension_months"] == 0

        result = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not result["skipped"]
        actual = session.ctx.h2_path.read_bytes()
        order = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == MAMBO
        )["order_parameter_in_file"]
        assert order["target_province"] == ERMOR
        assert order["target_province_name"] == "Ermor"
        assert order["target_mode"] == "caster_current_province_implicit"
        assert order["duration_extension_gems"] == 0
        assert order["duration_extension_months"] == 0
    finally:
        session.close()

    expected = (ORDERED / "mid_ermor.2h").read_bytes()
    assert actual[:-1] == expected[:-1]
    assert H2.gem_remaining(actual)[2] == 0


def test_monthly_frost_dome_matches_the_client_order(tmp_path):
    client = (MONTHLY / "mid_ermor.2h").read_bytes()
    fields = read_ritual_fields(
        client, find_order_blocks(client)[MAMBO].name_end)
    assert fields is not None
    assert (fields.spell_id, fields.gem_cost, fields.target_province) == (
        FROST_DOME, 15, ERMOR,
    )
    assert fields.monthly is True

    session = _session(tmp_path)
    try:
        recorded = _ok(session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": FROST_DOME,
            "monthly": True,
            "rationale": "maintain Frost Dome while Water gems permit",
        }))
        assert recorded["monthly"] is True
        assert "returns the caster to Defend" in recorded["warnings"][0]
        result = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not result["skipped"]
        actual = session.ctx.h2_path.read_bytes()
    finally:
        session.close()

    assert actual[:-1] == client[:-1]


def test_unaffordable_monthly_repeat_returns_to_defend_and_reports_cast(tmp_path):
    session = _session(tmp_path, RESOLVED)
    try:
        mambo = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == MAMBO
        )
        assert mambo["order_in_file"] == "defend"
        messages = _ok(session.call("get_turn_messages", {}))["messages"]
        province = _ok(session.call("get_province", {"province_id": ERMOR}))
    finally:
        session.close()

    cast = next(row for row in messages if row["kind"] == "own_ritual_cast")
    assert cast["text"] == "Mambo has cast Frost Dome."
    assert (cast["province_id"], cast["commander_id"]) == (ERMOR, MAMBO)
    assert province["province_enchantments"] == [{
        "spell_id": FROST_DOME,
        "name": "Frost Dome",
        "caster_commander_id": MAMBO,
        "caster": "Mambo",
        "caster_unit_type": "Serpent King",
        "months_left": 1,
        "duration_extension_months_per_gem": 1,
        "base_gem_cost": 15,
        "dispelled_if_province_lost": True,
    }]


def test_foreign_raw_dome_is_never_exposed_in_the_buildings_list(tmp_path):
    session = _session(
        tmp_path, RESOLVED, stem="mid_marignon", nation_id=61)
    try:
        province = _ok(session.call("get_province", {"province_id": ERMOR}))
    finally:
        session.close()
    assert province["province_enchantments"] == []
    assert province["province_enchantment_visibility"] == (
        "foreign_hidden_even_with_adjacency_local_stealth_spy_or_friendly_dominion"
    )
    assert province["province_protection_observations"] == []
