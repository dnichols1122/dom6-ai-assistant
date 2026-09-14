"""Ritual of Rebirth: automatic dead-Hall-of-Fame-hero selection."""

import shutil
import sqlite3
import struct
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.file_reader.formats import trn as T
from dom6_assistant.orders.orders_2h import find_order_blocks, read_ritual_fields


ROOT = Path("knowledge/snapshots/example_game_3")
BASELINE = ROOT / "t26-auto"
ORDERED = ROOT / "t26-auto-2"
RESOLVED = ROOT / "t27-auto"
GAME_DB = Path("knowledge/game.sqlite3")
STEM = "early_sauromatia"
NATION_ID = 9
CASTER = 108  # Naric
ARAXUS = 90
RITUAL_OF_REBIRTH = 1229
MUMMY = 398


def _session(tmp_path: Path, snapshot: Path, profile: str):
    if not (snapshot / f"{STEM}.2h").exists() or not GAME_DB.exists():
        pytest.skip("Ritual of Rebirth control snapshots are absent")
    save = tmp_path / profile
    save.mkdir()
    for name in (f"{STEM}.trn", f"{STEM}.2h"):
        shutil.copy2(snapshot / name, save / name)
    db = tmp_path / f"{profile}.sqlite3"
    shutil.copy2(GAME_DB, db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE '%_intent'"
        ):
            conn.execute(f'DELETE FROM "{table}"')
        conn.execute("DELETE FROM games WHERE name=?", (profile,))
        conn.execute(
            "INSERT INTO games(name, save_name, nation_id, nation_slug, save_dir) "
            "VALUES(?,?,?,?,?)",
            (profile, "example_game_3", NATION_ID, STEM, str(save)),
        )
        conn.commit()
    finally:
        conn.close()
    return open_session(profile, game_db=db, save_dir=save)


def _ok(result):
    assert result["ok"], result.get("error")
    return result["result"]


def test_client_order_has_no_rebirth_target_selector():
    data = (ORDERED / f"{STEM}.2h").read_bytes()
    block = find_order_blocks(data)[CASTER]
    fields = read_ritual_fields(data, block.name_end)
    assert fields is not None
    assert (fields.spell_id, fields.gem_cost, fields.monthly) == (
        RITUAL_OF_REBIRTH, 15, False,
    )
    assert fields.target_province is None
    assert fields.target_commander_runtime_index is None
    assert fields.target_unit_runtime_index is None
    assert fields.target_item_id is None
    assert struct.unpack_from("<Iii", data, block.name_end + 124) == (0, -1, -1)


def test_tool_reproduces_the_client_authored_rebirth_order(tmp_path):
    session = _session(tmp_path, BASELINE, "ritual_rebirth_baseline")
    try:
        spells = _ok(session.call("list_castable_spells", {
            "commander_id": CASTER, "kind": "ritual",
        }))["spells"]
        rebirth = next(row for row in spells if row["id"] == RITUAL_OF_REBIRTH)
        assert rebirth["order_supported"] is True
        assert rebirth["target"] == "automatic_eligible_dead_hall_of_fame_hero"
        assert rebirth["target_parameter"] is None
        assert rebirth["monthly_supported"] is False

        refused = session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": RITUAL_OF_REBIRTH,
            "monthly": True,
            "rationale": "invalid repeating resurrection",
        })
        assert refused["ok"] is False
        assert "one-shot" in refused["error"]

        recorded = _ok(session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": RITUAL_OF_REBIRTH,
            "rationale": "return an eligible fallen hero as a mummy",
        }))
        assert recorded["target_mode"] == "automatic_dead_hall_of_fame_hero"
        assert recorded["target_province"] is None
        assert "cannot promise which one" in recorded["warnings"][0]

        result = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not result["skipped"]
        actual = session.ctx.h2_path.read_bytes()
        order = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == CASTER
        )["order_parameter_in_file"]
        assert order["target_mode"] == "automatic_dead_hall_of_fame_hero"
    finally:
        session.close()

    expected = (ORDERED / f"{STEM}.2h").read_bytes()
    # The client's save action rewrites its ordinary two-byte trailer; the
    # complete order body and gem reservation are otherwise byte-identical.
    assert actual[:-2] == expected[:-2]


def test_resolved_turn_returns_araxus_as_mummy_and_reports_it(tmp_path):
    before = (BASELINE / f"{STEM}.trn").read_bytes()
    after = (RESOLVED / f"{STEM}.trn").read_bytes()
    assert ARAXUS in T.read_hall_of_fame(before)
    assert ARAXUS in T.read_hall_of_fame(after)

    session = _session(tmp_path, RESOLVED, "ritual_rebirth_resolved")
    try:
        araxus = next(
            row for row in _ok(session.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == ARAXUS
        )
        assert araxus["unit_type_id"] == MUMMY
        messages = _ok(session.call("get_turn_messages", {}))["messages"]
        message = next(row for row in messages if "Ritual of Rebirth" in row["text"])
        assert message["kind"] == "own_ritual_cast"
        assert "The famous Araxus has now risen as a mummy." in message["text"]
    finally:
        session.close()
