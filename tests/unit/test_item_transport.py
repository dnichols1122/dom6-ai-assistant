"""Carrier Eagle and Teleport Item recipient and treasury-item payloads."""
import shutil
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.orders.orders_2h import (
    find_order_blocks,
    read_equipment,
    read_ritual_fields,
)


ROOT = Path("knowledge/snapshots/example_game")
BASELINE = ROOT / "t48-auto-3"
SHIELD_ORDER = ROOT / "t48-auto-4"
CANCELLED = ROOT / "t48-auto-5"
SLING_ORDER = ROOT / "t48-auto-6"
TELEPORT_BASELINE = ROOT / "t48-auto-7"
TELEPORT_ORDER = ROOT / "t48-auto-8"
RESOLVED = ROOT / "t49-auto-3"
GAME_DB = Path("knowledge/game.sqlite3")
MAMBO = 124
CARNAGE = 7
FALGOTH = 26
CARRIER_EAGLE = 1285
TELEPORT_ITEM = 1320
RAW_HIDE_SHIELD = 163
SLING_OF_ACCURACY = 142


def _session(tmp_path: Path, snapshot: Path):
    if not (snapshot / "mid_ermor.2h").exists() or not GAME_DB.exists():
        pytest.skip("item-transport control snapshots are absent")
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
    ("snapshot", "item_id", "remaining_item"),
    [
        (SHIELD_ORDER, RAW_HIDE_SHIELD, SLING_OF_ACCURACY),
        (SLING_ORDER, SLING_OF_ACCURACY, RAW_HIDE_SHIELD),
    ],
)
def test_client_order_stores_item_at_plus_132_and_removes_it_from_stash(
    snapshot, item_id, remaining_item,
):
    data = (snapshot / "mid_ermor.2h").read_bytes()
    fields = read_ritual_fields(data, find_order_blocks(data)[MAMBO].name_end)
    assert fields is not None
    assert (fields.spell_id, fields.gem_cost, fields.target_province) == (
        CARRIER_EAGLE, 3, FALGOTH,
    )
    assert fields.target_commander_runtime_index == 292
    assert fields.target_item_id == item_id
    assert H2.read_item_stash(data) == [remaining_item]


def test_tool_reproduces_the_shield_transport_exactly(tmp_path):
    session = _session(tmp_path, BASELINE)
    try:
        spells = _ok(session.call("list_castable_spells", {
            "commander_id": MAMBO, "kind": "ritual",
        }))["spells"]
        carrier = next(row for row in spells if row["id"] == CARRIER_EAGLE)
        assert carrier["order_supported"] is True
        assert carrier["map_range"] == 4
        assert carrier["target_parameters"] == [
            "target_province", "target_commander", "target_item",
        ]

        recorded = _ok(session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": CARRIER_EAGLE,
            "target_province": FALGOTH,
            "target_commander": CARNAGE,
            "target_item": RAW_HIDE_SHIELD,
            "rationale": "deliver protection to Carnage",
        }))
        assert recorded["target_item"] == RAW_HIDE_SHIELD
        assert recorded["target_item_name"] == "Raw Hide Shield"
        assert recorded["target_commander"] == CARNAGE
        assert recorded["target_range"] == {"distance": 3, "maximum": 4}
        treasury = _ok(session.call("get_item_treasury", {}))
        assert treasury["recorded_transport_reservations"] == [RAW_HIDE_SHIELD]
        shield = next(
            item for item in treasury["items"]
            if item["item_id"] == RAW_HIDE_SHIELD
        )
        assert shield["recorded_for_transport"]["recipient_id"] == CARNAGE

        intent = next(
            row for row in _ok(session.call("get_orders", {}))
            if row.get("commander_id") == MAMBO
        )
        assert intent["target_item_name"] == "Raw Hide Shield"

        result = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not result["skipped"]
        actual = session.ctx.h2_path.read_bytes()
        file_order = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == MAMBO
        )["order_parameter_in_file"]
        assert file_order["target_item"] == RAW_HIDE_SHIELD
        assert file_order["target_item_name"] == "Raw Hide Shield"
    finally:
        session.close()

    expected = (SHIELD_ORDER / "mid_ermor.2h").read_bytes()
    assert actual[:-1] == expected[:-1]
    assert H2.read_item_stash(actual) == [SLING_OF_ACCURACY]
    assert H2.gem_remaining(actual)[1] == 18


def test_replacing_shield_with_sling_refunds_then_reserves(tmp_path):
    session = _session(tmp_path, SHIELD_ORDER)
    try:
        _ok(session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": CARRIER_EAGLE,
            "target_province": FALGOTH,
            "target_commander": CARNAGE,
            "target_item": SLING_OF_ACCURACY,
            "rationale": "send ranged support instead",
        }))
        _ok(session.call("materialize_orders", {"confirm": True}))
        actual = session.ctx.h2_path.read_bytes()
    finally:
        session.close()
    expected = (SLING_ORDER / "mid_ermor.2h").read_bytes()
    assert actual[:-1] == expected[:-1]
    assert H2.read_item_stash(actual) == [RAW_HIDE_SHIELD]


def test_cancelling_transport_restores_the_item_and_gems(tmp_path):
    session = _session(tmp_path, SHIELD_ORDER)
    try:
        _ok(session.call("record_order", {
            "commander_id": MAMBO,
            "order": "research",
            "rationale": "cancel delivery",
        }))
        _ok(session.call("materialize_orders", {"confirm": True}))
        actual = session.ctx.h2_path.read_bytes()
    finally:
        session.close()
    expected = (CANCELLED / "mid_ermor.2h").read_bytes()
    assert actual[:-1] == expected[:-1]
    assert H2.read_item_stash(actual) == [RAW_HIDE_SHIELD, SLING_OF_ACCURACY]
    assert H2.gem_remaining(actual)[1] == 21


def test_resolved_sling_is_equipped_on_the_selected_carnage():
    data = (RESOLVED / "mid_ermor.2h").read_bytes()
    blocks = find_order_blocks(data)
    assert read_equipment(data, blocks[CARNAGE].name_end)["ranged"] == SLING_OF_ACCURACY
    assert H2.read_item_stash(data) == [RAW_HIDE_SHIELD]


def test_client_teleport_item_uses_the_carrier_eagle_layout():
    data = (TELEPORT_ORDER / "mid_ermor.2h").read_bytes()
    fields = read_ritual_fields(data, find_order_blocks(data)[MAMBO].name_end)
    assert fields is not None
    assert (fields.spell_id, fields.gem_cost, fields.target_province) == (
        TELEPORT_ITEM, 3, FALGOTH,
    )
    assert fields.target_commander_runtime_index == 292
    assert fields.target_item_id == SLING_OF_ACCURACY
    assert H2.read_item_stash(data) == [RAW_HIDE_SHIELD]
    assert H2.gem_remaining(data)[4] == 7


def test_tool_reproduces_teleport_item_exactly(tmp_path):
    session = _session(tmp_path, TELEPORT_BASELINE)
    try:
        spells = _ok(session.call("list_castable_spells", {
            "commander_id": MAMBO, "kind": "ritual",
        }))["spells"]
        teleport = next(row for row in spells if row["id"] == TELEPORT_ITEM)
        assert teleport["order_supported"] is True
        assert teleport["map_range"] == 6
        assert teleport["target_parameters"] == [
            "target_province", "target_commander", "target_item",
        ]

        recorded = _ok(session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": TELEPORT_ITEM,
            "target_province": FALGOTH,
            "target_commander": CARNAGE,
            "target_item": SLING_OF_ACCURACY,
            "rationale": "teleport ranged support to Carnage",
        }))
        assert recorded["target_item"] == SLING_OF_ACCURACY
        assert recorded["target_item_name"] == "Sling of Accuracy"
        assert recorded["target_range"] == {"distance": 3, "maximum": 6}
        result = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not result["skipped"]
        actual = session.ctx.h2_path.read_bytes()
        file_order = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == MAMBO
        )["order_parameter_in_file"]
        assert file_order["target_item"] == SLING_OF_ACCURACY
        assert file_order["target_item_name"] == "Sling of Accuracy"
    finally:
        session.close()

    expected = (TELEPORT_ORDER / "mid_ermor.2h").read_bytes()
    assert actual[:-1] == expected[:-1]
    assert H2.read_item_stash(actual) == [RAW_HIDE_SHIELD]
    assert H2.gem_remaining(actual)[4] == 7


def test_cancelling_teleport_item_restores_sling_and_astral_gems(tmp_path):
    session = _session(tmp_path, TELEPORT_ORDER)
    try:
        _ok(session.call("record_order", {
            "commander_id": MAMBO,
            "order": "research",
            "rationale": "cancel the teleport",
        }))
        _ok(session.call("materialize_orders", {"confirm": True}))
        actual = session.ctx.h2_path.read_bytes()
    finally:
        session.close()

    expected = (TELEPORT_BASELINE / "mid_ermor.2h").read_bytes()
    assert actual[:-1] == expected[:-1]
    assert H2.read_item_stash(actual) == [RAW_HIDE_SHIELD, SLING_OF_ACCURACY]
    assert H2.gem_remaining(actual)[4] == 10


def test_item_transport_requires_an_available_item(tmp_path):
    session = _session(tmp_path, BASELINE)
    try:
        missing = session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": CARRIER_EAGLE,
            "target_province": FALGOTH,
            "target_commander": CARNAGE,
            "rationale": "missing payload",
        })
        assert not missing["ok"] and "target_item is required" in missing["error"]

        unavailable = session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": CARRIER_EAGLE,
            "target_province": FALGOTH,
            "target_commander": CARNAGE,
            "target_item": 999,
            "rationale": "not in treasury",
        })
        assert not unavailable["ok"] and "unequipped treasury" in unavailable["error"]

        missing_province = session.call("cast_ritual", {
            "commander_id": MAMBO,
            "spell_id": TELEPORT_ITEM,
            "target_commander": CARNAGE,
            "target_item": RAW_HIDE_SHIELD,
            "rationale": "missing destination province",
        })
        assert not missing_province["ok"]
        assert "target_province" in missing_province["error"]

        for spell_id in (CARRIER_EAGLE, TELEPORT_ITEM):
            monthly = session.call("cast_ritual", {
                "commander_id": MAMBO,
                "spell_id": spell_id,
                "target_province": FALGOTH,
                "target_commander": CARNAGE,
                "target_item": RAW_HIDE_SHIELD,
                "monthly": True,
                "rationale": "an item cannot be resent every month",
            })
            assert not monthly["ok"]
            assert "one-shot ritual" in monthly["error"]
    finally:
        session.close()
