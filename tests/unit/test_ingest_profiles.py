"""A multi-human save retains one independent database profile per player."""
import sqlite3
from pathlib import Path

from dom6_assistant.gamestate.ingest import (
    SCHEMA,
    ensure_schema_columns,
    player_profile_name,
)


def test_single_human_save_keeps_raw_game_name(tmp_path: Path):
    trn = tmp_path / "mid_marignon.trn"
    trn.write_bytes(b"")

    assert player_profile_name(trn, "ordinary_game") == "ordinary_game"


def test_dual_human_save_keys_each_private_view_by_nation(tmp_path: Path):
    ermor = tmp_path / "mid_ermor.trn"
    marignon = tmp_path / "mid_marignon.trn"
    ermor.write_bytes(b"")
    marignon.write_bytes(b"")

    assert player_profile_name(ermor, "example_game") == (
        "example_game::mid_ermor")
    assert player_profile_name(marignon, "example_game") == (
        "example_game::mid_marignon")


def test_existing_recruit_intent_table_gains_kind_column():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    connection.execute("DROP TABLE recruit_intent_v2")
    connection.execute(
        "CREATE TABLE recruit_intent_v2 (id INTEGER PRIMARY KEY, "
        "unit_type_id INTEGER NOT NULL)"
    )

    ensure_schema_columns(connection)

    columns = {
        row[1] for row in connection.execute(
            "PRAGMA table_info(recruit_intent_v2)")
    }
    assert "kind" in columns
    connection.close()


def test_existing_squad_creation_view_gains_final_assignment_filter():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    connection.execute("DROP VIEW current_squad_creation_intent")
    connection.execute(
        "CREATE VIEW current_squad_creation_intent AS "
        "SELECT * FROM squad_creation_intent"
    )

    ensure_schema_columns(connection)

    definition = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' "
        "AND name='current_squad_creation_intent'"
    ).fetchone()[0]
    assert "current_troop_assignment_intent" in definition
    connection.close()
