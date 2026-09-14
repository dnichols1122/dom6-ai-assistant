"""What a fresh install gets, which is not what a long-lived one has.

Three defects shipped because every check ran against a database built up over
months of development. Each produced a working-looking install whose Live turn
workspace could not open a single game:

* `nation_id` was resolved from `knowledge/reference/nations.json`, a file no
  documented build step creates. The miss was swallowed, so ingest wrote NULL
  for every game, and `open_session` refuses a game with no nation id.
* The live save directory was rebuilt from the game's *internal* name. A
  server-hosted game writes `srvgame_MYGAME` into its `.trn` while the folder is
  `MYGAME`, so every such game reported "live files unavailable".
* `decode_status` was created by hand during decoding and never added to the
  schema, so `/api/agent/gaps` returned 500 rather than an empty list.

These tests build their fixtures from nothing, which is the point: anything
that reads the developer's own knowledge/ directory cannot catch this class of
bug.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.agent.session import SessionError, resolve_save_dir
from dom6_assistant.gamestate import ingest as ingest_mod


@pytest.fixture()
def reference_db(tmp_path: Path) -> Path:
    """The nations table as `build_db --refresh` produces it."""
    path = tmp_path / "reference.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nations (id INTEGER, name TEXT, "
                 "file_name_base TEXT, era INTEGER)")
    conn.executemany("INSERT INTO nations VALUES (?,?,?,?)", [
        (33, "Niefelheim", "early_niefelheim", 1),
        (44, "R'lyeh", "early_rlyeh", 1),
        (61, "Marignon", "mid_marignon", 2),
        (71, "Caelum", None, 2),          # an older scrape, missing the stem
    ])
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# nation_id
# ---------------------------------------------------------------------------

def test_nation_id_comes_from_the_database_the_build_step_creates(reference_db):
    ingest_mod._NATION_ID_CACHE.clear()
    assert ingest_mod._nation_id_for_slug("early_niefelheim", reference_db) == 33
    assert ingest_mod._nation_id_for_slug("early_rlyeh", reference_db) == 44


def test_nation_id_falls_back_to_era_and_name(reference_db):
    """Older scrapes have no file_name_base for every nation."""
    ingest_mod._NATION_ID_CACHE.clear()
    assert ingest_mod._nation_id_for_slug("mid_caelum", reference_db) == 71


def test_an_unknown_nation_is_none_rather_than_a_guess(reference_db):
    ingest_mod._NATION_ID_CACHE.clear()
    assert ingest_mod._nation_id_for_slug("mid_notanation", reference_db) is None


def test_a_missing_reference_db_gives_none_rather_than_raising(tmp_path):
    """Ingesting before the reference build is a normal order of operations.

    Both sources are pointed at nothing. The developer tree really does have a
    nations.json, so leaving the legacy path at its default would make this
    pass here and fail on the fresh install it is written to protect.
    """
    ingest_mod._NATION_ID_CACHE.clear()
    result = ingest_mod._nation_id_for_slug(
        "early_rlyeh",
        reference_db=tmp_path / "never-built.sqlite3",
        legacy_json=tmp_path / "no-such.json")
    assert result is None


def test_the_legacy_json_still_resolves_for_an_older_tree(tmp_path):
    """Someone mid-upgrade has the JSON and not the table; do not regress them."""
    import json
    legacy = tmp_path / "nations.json"
    legacy.write_text(json.dumps([{"id": 44, "name": "R'lyeh",
                                   "file_name_base": "early_rlyeh"}]))
    ingest_mod._NATION_ID_CACHE.clear()

    result = ingest_mod._nation_id_for_slug(
        "early_rlyeh",
        reference_db=tmp_path / "never-built.sqlite3",
        legacy_json=legacy)
    assert result == 44


# ---------------------------------------------------------------------------
# the live save directory
# ---------------------------------------------------------------------------

def test_the_recorded_directory_wins_when_the_folder_name_differs(tmp_path):
    """A server game is named srvgame_MYGAME and lives in a folder called MYGAME."""
    root = tmp_path / "savedgames"
    (root / "MYGAME").mkdir(parents=True)

    found = resolve_save_dir("srvgame_MYGAME", save_root=root,
                             recorded=root / "MYGAME")

    assert found == root / "MYGAME"


def test_a_recorded_snapshot_directory_is_refused(tmp_path):
    """Ingest also walks the snapshot corpus.

    Playing out of an archive would write this turn's orders into a frozen
    copy of an old one, so a recorded path outside the live root is ignored
    even though it exists.
    """
    root = tmp_path / "savedgames"
    (root / "mygame").mkdir(parents=True)
    snapshot = tmp_path / "knowledge" / "snapshots" / "t17"
    snapshot.mkdir(parents=True)

    found = resolve_save_dir("mygame", save_root=root, recorded=snapshot)

    assert found == root / "mygame", "an archived snapshot is not the live save"


def test_a_stale_recorded_directory_falls_back_to_the_name(tmp_path):
    root = tmp_path / "savedgames"
    (root / "mygame").mkdir(parents=True)

    found = resolve_save_dir("mygame", save_root=root,
                             recorded=root / "deleted-since")

    assert found == root / "mygame"


def test_a_genuinely_missing_game_still_raises(tmp_path):
    root = tmp_path / "savedgames"
    root.mkdir()

    with pytest.raises(SessionError, match="no save directory"):
        resolve_save_dir("absent", save_root=root, recorded=None)


# ---------------------------------------------------------------------------
# the schema
# ---------------------------------------------------------------------------

def test_a_new_database_has_the_tables_the_ui_queries(tmp_path):
    """decode_status backs /api/agent/gaps and was missing from the schema."""
    conn = ingest_mod.connect(tmp_path / "game.sqlite3")
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "decode_status" in names
        # The endpoint's own query must work against an empty install.
        rows = conn.execute(
            "SELECT field, outcome, finding, unblock_test FROM decode_status "
            "WHERE outcome IN ('blocked','characterised')").fetchall()
        assert rows == []
    finally:
        conn.close()
