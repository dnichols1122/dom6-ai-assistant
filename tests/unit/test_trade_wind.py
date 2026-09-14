"""Trade Wind: a controlled coast-restricted implicit-local ritual."""

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
INLAND = ROOT / "t58-auto"
BASELINE = ROOT / "t60-auto"
ORDERED = ROOT / "t60-auto-2"
BOOSTED = ROOT / "t60-auto-3"
RESOLVED = ROOT / "t61-auto"
GLOBAL_ONLY = ROOT / "t47-auto"
GAME_DB = Path("knowledge/game.sqlite3")
SUGAAR = 125
TRADE_WIND = 1172
MARIGNON = 8
CLIFF_COAST = 11


def _session(tmp_path: Path, snapshot: Path = BASELINE):
    if not (snapshot / "mid_marignon.2h").exists() or not GAME_DB.exists():
        pytest.skip("Trade Wind control snapshots are absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_marignon.trn", "mid_marignon.2h"):
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
        "example_game::mid_marignon", game_db=db, save_dir=save,
        nation_id=61,
    )


def _ok(result):
    assert result["ok"], result.get("error")
    return result["result"]


def test_client_trade_wind_implicitly_targets_cliff_coast():
    before = (BASELINE / "mid_marignon.2h").read_bytes()
    data = (ORDERED / "mid_marignon.2h").read_bytes()
    block = find_order_blocks(data)[SUGAAR]
    fields = read_ritual_fields(data, block.name_end)
    assert fields is not None
    assert (fields.spell_id, fields.gem_cost, fields.target_province) == (
        TRADE_WIND, 10, CLIFF_COAST,
    )
    assert fields.monthly is False
    assert struct.unpack_from("<ii", data, block.name_end + 128) == (-1, -1)
    assert H2.gem_remaining(before)[1] == 55
    assert H2.gem_remaining(data)[1] == 45


def test_tool_reproduces_the_coastal_trade_wind_order(tmp_path):
    session = _session(tmp_path)
    try:
        spells = _ok(session.call("list_castable_spells", {
            "commander_id": SUGAAR, "kind": "ritual",
        }))["spells"]
        trade_wind = next(row for row in spells if row["id"] == TRADE_WIND)
        assert trade_wind["order_supported"] is True
        assert trade_wind["target"] == "caster_current_province_implicit"
        assert trade_wind["source_requirements"] == ["coastal province"]
        assert trade_wind["coastal_source_confirmed"] is True
        assert trade_wind["extra_gems_supported"] is True
        assert trade_wind["extra_gems_effect"] == (
            "extends duration by 1 month per gem")
        assert trade_wind["duration_extension_months_per_gem"] == 1
        assert trade_wind["monthly_supported"] is True
        assert trade_wind["castable_now"] is True

        monthly = _ok(session.call("cast_ritual", {
            "commander_id": SUGAAR,
            "spell_id": TRADE_WIND,
            "monthly": True,
            "rationale": "repeat the province enchantment while affordable",
        }))
        assert monthly["monthly"] is True

        recorded = _ok(session.call("cast_ritual", {
            "commander_id": SUGAAR,
            "spell_id": TRADE_WIND,
            "rationale": "bring favorable winds to Cliff Coast",
        }))
        assert recorded["target_province"] == CLIFF_COAST
        assert recorded["target_name"] == "Cliff Coast"
        assert recorded["gem_cost"] == 10
        assert recorded["duration_extension_gems"] == 0
        assert recorded["duration_extension_months"] == 0

        intent = next(
            row for row in _ok(session.call("get_orders", {}))
            if row["commander_id"] == SUGAAR
        )
        assert intent["target_mode"] == (
            "caster_current_province_implicit")
        assert intent["duration_extension_gems"] == 0
        assert intent["duration_extension_months"] == 0

        result = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not result["skipped"]
        actual = session.ctx.h2_path.read_bytes()
        file_order = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == SUGAAR
        )["order_parameter_in_file"]
        assert file_order["target_mode"] == "caster_current_province_implicit"
        assert file_order["duration_extension_gems"] == 0
        assert file_order["duration_extension_months"] == 0
    finally:
        session.close()

    expected = (ORDERED / "mid_marignon.2h").read_bytes()
    assert actual[:-1] == expected[:-1]
    assert H2.gem_remaining(actual)[1] == 45


def test_ten_extra_gems_match_the_client_and_add_ten_months(tmp_path):
    session = _session(tmp_path)
    try:
        recorded = _ok(session.call("cast_ritual", {
            "commander_id": SUGAAR,
            "spell_id": TRADE_WIND,
            "extra_gems": 10,
            "rationale": "extend Trade Wind by ten months",
        }))
        assert recorded["base_gem_cost"] == 10
        assert recorded["gem_cost"] == 20
        assert recorded["duration_extension_gems"] == 10
        assert recorded["duration_extension_months"] == 10
        result = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not result["skipped"]
        actual = session.ctx.h2_path.read_bytes()
        file_order = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == SUGAAR
        )["order_parameter_in_file"]
        assert file_order["gem_cost"] == 20
        assert file_order["duration_extension_gems"] == 10
        assert file_order["duration_extension_months"] == 10
    finally:
        session.close()

    expected = (BOOSTED / "mid_marignon.2h").read_bytes()
    assert actual[:-1] == expected[:-1]
    assert H2.gem_remaining(actual)[1] == 35


def test_resolved_trade_wind_exposes_eleven_months(tmp_path):
    ours = T.parse(RESOLVED / "mid_marignon.trn")
    foreign = T.parse(RESOLVED / "mid_ermor.trn")
    trade_wind = next(
        record for record in ours.local_enchantments
        if record.spell_id == TRADE_WIND
    )
    assert (
        trade_wind.caster_runtime_index,
        trade_wind.effect_argument,
        trade_wind.caster_nation_id,
        trade_wind.province_id,
        trade_wind.months_left,
    ) == (3158, 95, 61, CLIFF_COAST, 11)
    assert foreign.local_enchantments == ours.local_enchantments

    session = _session(tmp_path, RESOLVED)
    try:
        province = _ok(session.call(
            "get_province", {"province_id": CLIFF_COAST}))
        messages = _ok(session.call("get_turn_messages", {}))["messages"]
    finally:
        session.close()
    assert province["province_enchantments"] == [{
        "spell_id": TRADE_WIND,
        "name": "Trade Wind",
        "caster_commander_id": SUGAAR,
        "caster": "Sugaar",
        "caster_unit_type": "Serpent of Heavenly Fires",
        "months_left": 11,
        "duration_extension_months_per_gem": 1,
        "base_gem_cost": 10,
        "dispelled_if_province_lost": True,
    }]
    cast = next(row for row in messages if row["kind"] == "own_ritual_cast")
    assert cast["text"] == "Sugaar has cast Trade Wind."
    assert (cast["province_id"], cast["commander_id"]) == (
        CLIFF_COAST, SUGAAR)


def test_effect_81_structural_record_is_not_a_province_enchantment(tmp_path):
    raw = T.parse(GLOBAL_ONLY / "mid_marignon.trn")
    assert any(record.spell_id == 1188 for record in raw.local_enchantments)
    session = _session(tmp_path, GLOBAL_ONLY)
    try:
        province = _ok(session.call(
            "get_province", {"province_id": MARIGNON}))
    finally:
        session.close()
    assert province["province_enchantments"] == []


def test_trade_wind_is_refused_from_inland_marignon(tmp_path):
    session = _session(tmp_path, INLAND)
    try:
        spells = _ok(session.call("list_castable_spells", {
            "commander_id": SUGAAR, "kind": "ritual",
        }))["spells"]
        trade_wind = next(row for row in spells if row["id"] == TRADE_WIND)
        assert trade_wind["coastal_source_confirmed"] is not True
        assert trade_wind["castable_now"] is False
        assert any(
            "not a legal coastal source" in blocker
            for blocker in trade_wind["why_not_castable_now"]
        )

        refused = session.call("cast_ritual", {
            "commander_id": SUGAAR,
            "spell_id": TRADE_WIND,
            "rationale": "the client refuses this inland",
        })
        assert not refused["ok"]
        assert "not a legal coastal source" in refused["error"]
    finally:
        session.close()


def test_materializer_revalidates_the_coastal_source(tmp_path, monkeypatch):
    from dom6_assistant.reference import ritual_range

    session = _session(tmp_path)
    try:
        _ok(session.call("cast_ritual", {
            "commander_id": SUGAAR,
            "spell_id": TRADE_WIND,
            "rationale": "record a currently legal coastal cast",
        }))
        monkeypatch.setattr(
            ritual_range, "province_is_coastal",
            lambda provinces, source_province: False,
        )
        result = _ok(session.call(
            "materialize_orders", {"confirm": True}))
        assert result["aborted"] is True
        assert any(
            "not a legal coastal source" in row
            for row in result["skipped"]
        )
    finally:
        session.close()
