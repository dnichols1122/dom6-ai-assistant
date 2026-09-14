"""Selective live-save ingestion watches stable player turn files."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from dom6_assistant.__main__ import main
from dom6_assistant.gamestate.ingest import watch_save_dirs


SNAPSHOT = Path("knowledge/snapshots/example_game/t1-auto")


def test_cli_ingests_every_human_view_in_one_selected_save(tmp_path):
    save = tmp_path / "example_game"
    save.mkdir()
    for name in ("mid_ermor.trn", "mid_marignon.trn"):
        shutil.copy2(SNAPSHOT / name, save / name)
    database = tmp_path / "game.sqlite3"

    result = CliRunner().invoke(main, [
        "ingest-watch", str(save), "--db", str(database),
        "--settle", "0", "--poll", "0.05", "--once",
    ])

    assert result.exit_code == 0, result.output
    assert "example_game::mid_ermor turn 1" in result.output
    assert "example_game::mid_marignon turn 1" in result.output
    conn = sqlite3.connect(database)
    try:
        profiles = {
            row[0] for row in conn.execute("SELECT name FROM games ORDER BY name")
        }
    finally:
        conn.close()
    assert profiles == {
        "example_game::mid_ermor",
        "example_game::mid_marignon",
    }


def test_watcher_ignores_unselected_saves_and_picks_up_new_files(
        tmp_path, monkeypatch):
    watched_dir = tmp_path / "watched"
    ignored_dir = tmp_path / "ignored"
    watched_dir.mkdir()
    ignored_dir.mkdir()
    first = watched_dir / "early_atlantis.trn"
    first.write_bytes(b"first")
    (ignored_dir / "early_yomi.trn").write_bytes(b"ignored")

    calls: list[Path] = []

    def fake_ingest(_conn, path):
        calls.append(path)
        return path.parent.name, 1, True

    monkeypatch.setattr(
        "dom6_assistant.gamestate.ingest.ingest_file", fake_ingest)
    monkeypatch.setattr(
        "dom6_assistant.gamestate.ingest.time.sleep", lambda _seconds: None)
    watched = watch_save_dirs(
        object(), [watched_dir], poll_seconds=0.05, settle_seconds=0)

    first_batch = next(watched)
    assert [event.path.name for event in first_batch] == ["early_atlantis.trn"]
    assert calls == [first.resolve()]

    second = watched_dir / "early_yomi.trn"
    second.write_bytes(b"new player")
    second_batch = next(watched)
    assert [event.path.name for event in second_batch] == ["early_yomi.trn"]
    assert calls == [first.resolve(), second.resolve()]


def test_invalid_stable_file_is_retried_after_it_changes(tmp_path, monkeypatch):
    save = tmp_path / "save"
    save.mkdir()
    turn = save / "early_atlantis.trn"
    turn.write_bytes(b"partial")
    attempts = 0

    def fake_ingest(_conn, path):
        nonlocal attempts
        attempts += 1
        if path.read_bytes() == b"partial":
            raise ValueError("incomplete turn")
        return "game", 2, True

    monkeypatch.setattr(
        "dom6_assistant.gamestate.ingest.ingest_file", fake_ingest)
    monkeypatch.setattr(
        "dom6_assistant.gamestate.ingest.time.sleep", lambda _seconds: None)
    watched = watch_save_dirs(
        object(), [save], poll_seconds=0.05, settle_seconds=0)

    failed = next(watched)
    assert failed[0].error == "incomplete turn"
    turn.write_bytes(b"complete turn")
    recovered = next(watched)
    assert recovered[0].error is None
    assert recovered[0].profile == "game"
    assert attempts == 2
