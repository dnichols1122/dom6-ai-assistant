"""Wish's client-resolved named-item selector and artifact resolution."""

import shutil
import sqlite3
import struct
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.agent.visibility import PlayerView
from dom6_assistant.file_reader.formats import trn as T
from dom6_assistant.orders import orders_2h as O


ROOT = Path("knowledge/snapshots/example_game_3")
GAME_DB = Path("knowledge/game.sqlite3")
STEM = "early_tienchi"
NATION_ID = 22
CASTER = 109  # Ren An
WISH = 915
ATLAS_OF_CREATION = 441


def _session(tmp_path: Path, snapshot: str, profile: str):
    source = ROOT / snapshot
    if not (source / f"{STEM}.2h").exists() or not GAME_DB.exists():
        pytest.skip("Wish control snapshots are absent")
    save = tmp_path / profile
    save.mkdir()
    for name in (f"{STEM}.trn", f"{STEM}.2h"):
        shutil.copy2(source / name, save / name)
    db = tmp_path / f"{profile}.sqlite3"
    shutil.copy2(GAME_DB, db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO games(name, save_name, nation_id, nation_slug, save_dir) "
            "VALUES(?,?,?,?,?)",
            (profile, "example_game_3", NATION_ID, STEM, str(save)),
        )
        conn.commit()
    finally:
        conn.close()
    return open_session(profile, game_db=db, save_dir=save)


def _ok(envelope):
    assert envelope["ok"], envelope.get("error")
    return envelope["result"]


def test_client_resolves_artifact_name_to_typed_wish_payload():
    data = (ROOT / "t70-auto-3" / f"{STEM}.2h").read_bytes()
    block = O.find_order_blocks(data)[CASTER]
    assert block.order_code == O.RITUAL_ORDER_CODES["cast_ritual"]
    assert struct.unpack_from("<HxxIIii", data, block.name_end + 116) == (
        WISH,
        100,
        O.WISH_ITEM_RESULT_CODE,
        ATLAS_OF_CREATION,
        -1,
    )
    ritual = O.read_ritual_fields(data, block.name_end)
    assert ritual is not None
    assert ritual.target_province is None
    assert ritual.target_commander_runtime_index is None
    assert ritual.wish_result_code == O.WISH_ITEM_RESULT_CODE
    assert ritual.wish_result == "magic_item"
    assert ritual.wish_item_id == ATLAS_OF_CREATION


def test_tool_reproduces_client_wish_order(tmp_path):
    session = _session(tmp_path, "t70-auto", "wish_artifact")
    try:
        spells = _ok(session.call("list_castable_spells", {
            "commander_id": CASTER,
            "kind": "ritual",
        }))["spells"]
        brief = next(row for row in spells if row["id"] == WISH)
        assert brief["order_supported"] is True
        assert brief["target"] == "typed_wish_selector"
        assert brief["target_parameter"] == "typed_wish_selector"
        assert brief["target_parameters"] == [
            "wish_item", "wish_unit", "wish_nation", "wish_result",
            "wish_random",
        ]
        assert "magic_power" in brief["wish_result_choices"]
        assert "commanders" in brief["wish_result_choices"]
        assert brief["wish_random_choices"] == [
            "item", "artifact", "something", "horror",
        ]
        assert brief["monthly_supported"] is False

        recorded = _ok(session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": WISH,
            "wish_item": ATLAS_OF_CREATION,
            "rationale": "create the controlled artifact",
        }))
        assert recorded["wish_item"] == ATLAS_OF_CREATION
        assert recorded["wish_item_name"] == "Atlas of Creation"

        intent = _ok(session.call("get_orders", {}))
        ren_an = next(row for row in intent if row["commander_id"] == CASTER)
        assert ren_an["wish_item"] == ATLAS_OF_CREATION
        assert ren_an["wish_item_name"] == "Atlas of Creation"

        materialized = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not materialized["skipped"]
        actual = session.ctx.h2_path.read_bytes()
    finally:
        session.close()

    expected = (ROOT / "t70-auto-3" / f"{STEM}.2h").read_bytes()
    assert actual[:-1] == expected[:-1]


def test_wish_rejects_monthly_and_missing_payload_but_allows_artifact_theft(tmp_path):
    session = _session(tmp_path, "t70-auto", "wish_refusals")
    try:
        monthly = session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": WISH,
            "wish_item": ATLAS_OF_CREATION,
            "monthly": True,
            "rationale": "invalid repetition",
        })
        missing = session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": WISH,
            "rationale": "invalid missing item",
        })
        ordinary = session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": WISH,
            "wish_item": 1,
            "rationale": "create an ordinary non-unique magic item",
        })
    finally:
        session.close()
    assert not monthly["ok"]
    assert "cannot be set to Monthly Ritual" in monthly["error"]
    assert not missing["ok"]
    assert "exactly one of wish_item" in missing["error"]
    assert ordinary["ok"]
    assert ordinary["result"]["wish_item"] == 1

    resolved = _session(tmp_path, "t71-auto-3", "wish_existing")
    try:
        existing = resolved.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": WISH,
            "wish_item": ATLAS_OF_CREATION,
            "rationale": "teleport the existing artifact as the client allows",
        })
    finally:
        resolved.close()
    assert existing["ok"]
    assert existing["result"]["wish_item"] == ATLAS_OF_CREATION


@pytest.mark.parametrize(
    ("ordered", "result", "code"),
    [
        ("t70-auto-4", "magic_power", 10006),
        ("t70-auto-5", "physical_power", 10007),
        ("t70-auto-6", "divine_power", 10009),
        ("t70-auto-7", "provinces", 10015),
    ],
)
def test_fixed_wish_result_codes_and_tool_writer(
    tmp_path, ordered, result, code,
):
    expected = (ROOT / ordered / f"{STEM}.2h").read_bytes()
    block = O.find_order_blocks(expected)[CASTER]
    assert struct.unpack_from("<HxxIIii", expected, block.name_end + 116) == (
        WISH, 100, code, -1, -1,
    )
    decoded = O.read_ritual_fields(expected, block.name_end)
    assert decoded is not None
    assert decoded.wish_result_code == code
    assert decoded.wish_result == result
    assert decoded.wish_item_id is None

    session = _session(tmp_path, "t70-auto", f"wish_{result}")
    try:
        recorded = _ok(session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": WISH,
            "wish_result": result,
            "rationale": f"reproduce controlled {result} wish",
        }))
        assert recorded["wish_result"] == result
        assert recorded["wish_outcome"]["outcome"]
        intent = _ok(session.call("get_orders", {}))
        ren_an = next(row for row in intent if row["commander_id"] == CASTER)
        assert ren_an["wish_result"] == result
        materialized = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not materialized["skipped"]
        actual = session.ctx.h2_path.read_bytes()
    finally:
        session.close()
    assert actual[:-1] == expected[:-1]


def test_controlled_wish_resolutions_and_worldwide_messages():
    before = PlayerView(ROOT / "t70-auto-4", NATION_ID, f"{STEM}.trn")
    after_magic = PlayerView(ROOT / "t71-auto-4", NATION_ID, f"{STEM}.trn")
    old_caster = next(c for c in before.own_commanders() if c.commander_id == CASTER)
    magic_caster = next(
        c for c in after_magic.own_commanders() if c.commander_id == CASTER)
    assert old_caster.paths == {"A": 5, "W": 3, "S": 8}
    assert magic_caster.paths == {
        "F": 1, "A": 6, "W": 4, "E": 1, "S": 9,
        "D": 1, "N": 1, "G": 1, "B": 1,
    }

    before_power = PlayerView(ROOT / "t70-auto-5", NATION_ID, f"{STEM}.trn")
    after_power = PlayerView(ROOT / "t71-auto-5", NATION_ID, f"{STEM}.trn")
    old_commander = next(c for c in before_power.own_commanders() if c.commander_id == CASTER)
    new_commander = next(c for c in after_power.own_commanders() if c.commander_id == CASTER)
    old_unit = next(u for u in before_power.own_units() if u.instance_id == old_commander.unit_instance_id)
    new_unit = next(u for u in after_power.own_units() if u.instance_id == new_commander.unit_instance_id)
    assert (old_unit.hp, new_unit.hp) == (187, 237)

    for branch, kind in [
        (6, "wish_divine_power_worldwide"),
        (7, "wish_provinces_worldwide"),
    ]:
        for stem, nation in [(STEM, NATION_ID), ("early_sauromatia", 9)]:
            view = PlayerView(ROOT / f"t71-auto-{branch}", nation, f"{stem}.trn")
            message = next(row for row in view.turn_messages() if row.kind == kind)
            assert message.is_worldwide
        own = PlayerView(ROOT / f"t71-auto-{branch}", NATION_ID, f"{STEM}.trn")
        assert any(row.kind == "own_wish" for row in own.turn_messages())

    before_provinces = PlayerView(
        ROOT / "t70-auto-7", NATION_ID, f"{STEM}.trn")
    after_provinces = PlayerView(
        ROOT / "t71-auto-7", NATION_ID, f"{STEM}.trn")
    assert len(before_provinces.own_provinces()) == 10
    assert len(after_provinces.own_provinces()) == 10


def test_bare_item_is_resolved_to_a_concrete_item_before_turn_processing():
    data = (ROOT / "t70-auto-8" / f"{STEM}.2h").read_bytes()
    block = O.find_order_blocks(data)[CASTER]
    ritual = O.read_ritual_fields(data, block.name_end)
    assert ritual is not None
    assert ritual.wish_result == "magic_item"
    assert ritual.wish_item_id == 292  # Ranger's Boots, Construction 3


@pytest.mark.parametrize(
    ("selector", "result", "code", "payload"),
    [
        ({"wish_result": "nothing"}, "nothing", 10000, -1),
        ({"wish_result": "blood_slaves"}, "blood_slaves", 10003, -1),
        ({"wish_result": "gold"}, "gold", 10004, 5000),
        ({"wish_result": "lesser_gold"}, "lesser_gold", 10004, 3000),
        ({"wish_result": "gems"}, "gems", 10005, -1),
        ({"wish_result": "armageddon"}, "armageddon", 10008, -1),
        ({"wish_result": "troops"}, "troops", 10010, 500),
        ({"wish_result": "food"}, "food", 10011, 1),
        ({"wish_result": "population"}, "population", 10012, 1),
        ({"wish_result": "death"}, "death", 10013, -1),
        ({"wish_result": "experience"}, "experience", 10017, 500),
        ({"wish_result": "commanders"}, "commanders", 10020, 1),
        ({"wish_unit": 30}, "unit", 10002, 30),
        ({"wish_nation": 9}, "kill_pretender", 10014, 9),
        ({"wish_random": "horror"}, "horror", 10018, -1),
    ],
)
def test_complete_typed_wish_families_serialize(
    tmp_path, selector, result, code, payload,
):
    session = _session(tmp_path, "t70-auto", f"wish_family_{code}_{payload}")
    try:
        recorded = _ok(session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": WISH,
            **selector,
            "rationale": f"exercise typed {result} result",
        }))
        assert recorded["wish_result"] == result
        materialized = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not materialized["skipped"]
        data = session.ctx.h2_path.read_bytes()
    finally:
        session.close()
    block = O.find_order_blocks(data)[CASTER]
    ritual = O.read_ritual_fields(data, block.name_end)
    assert ritual is not None
    assert (ritual.wish_result_code, ritual.wish_result, ritual.wish_payload) == (
        code, result, payload,
    )


def test_random_item_request_is_concretized_and_persisted(tmp_path):
    session = _session(tmp_path, "t70-auto", "wish_random_item")
    try:
        recorded = _ok(session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": WISH,
            "wish_random": "item",
            "rationale": "accept a client-equivalent random item",
        }))
        assert recorded["wish_result"] == "magic_item"
        assert recorded["wish_item"] is not None
        assert recorded["wish_outcome"]["requested"] == "random_item"
        intent = _ok(session.call("get_orders", {}))
        ren_an = next(row for row in intent if row["commander_id"] == CASTER)
        assert ren_an["wish_item"] == recorded["wish_item"]
        _ok(session.call("materialize_orders", {"confirm": True}))
        data = session.ctx.h2_path.read_bytes()
    finally:
        session.close()
    block = O.find_order_blocks(data)[CASTER]
    ritual = O.read_ritual_fields(data, block.name_end)
    assert ritual is not None
    assert ritual.wish_item_id == recorded["wish_item"]


def test_wish_resolution_is_private_and_equips_the_artifact(tmp_path):
    before = T.parse(ROOT / "t70-auto-3" / f"{STEM}.trn")
    after = T.parse(ROOT / "t71-auto-3" / f"{STEM}.trn")
    assert before.item_states[ATLAS_OF_CREATION] == T.ITEM_STATE_UNMADE
    assert after.item_states[ATLAS_OF_CREATION] > 0

    tienchi = PlayerView(ROOT / "t71-auto-3", NATION_ID, f"{STEM}.trn")
    own = [row for row in tienchi.turn_messages() if row.kind == "own_wish"]
    assert len(own) == 1
    assert own[0].commander_id == CASTER
    assert own[0].text == "Ren An has cast Wish.\n\n"

    sauromatia = PlayerView(
        ROOT / "t71-auto-3", 9, "early_sauromatia.trn")
    assert not any("Wish" in row.text for row in sauromatia.turn_messages())

    session = _session(tmp_path, "t71-auto-3", "wish_resolution")
    try:
        setup = _ok(session.call("get_battle_setup", {
            "commander_id": CASTER,
        }))
        assert setup["equipment"]["misc2"] == {
            "item_id": ATLAS_OF_CREATION,
            "name": "Atlas of Creation",
        }
        messages = _ok(session.call("get_turn_messages", {}))["messages"]
        exposed = next(row for row in messages if row["kind"] == "own_wish")
        assert exposed["commander_id"] == CASTER
        assert exposed["commander"] == "Ren An"
    finally:
        session.close()
