"""Ingestion happens because the server is running, not because you remembered.

Forgetting the separate ingest command was silent, and indistinguishable from
the assistant being wrong: it answered confidently from the previous turn.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from dom6_assistant.gamestate.autoingest import (
    SKIP_DIRECTORIES, AutoIngest, discover_save_dirs)

SNAPSHOTS = Path(__file__).resolve().parents[2] / "knowledge" / "snapshots"


def _a_real_turn() -> Path:
    found = next(SNAPSHOTS.rglob("*.trn"), None)
    if found is None:
        pytest.skip("no sample .trn available")
    return found


def test_discovery_finds_games_and_skips_the_game_s_own_folders(tmp_path):
    assert discover_save_dirs(tmp_path) == []
    (tmp_path / "newlords").mkdir()
    (tmp_path / "newlords" / "a.trn").write_bytes(b"x")
    (tmp_path / "mygame").mkdir()
    (tmp_path / "mygame" / "b.trn").write_bytes(b"x")
    (tmp_path / "empty").mkdir()

    found = [p.name for p in discover_save_dirs(tmp_path)]
    assert found == ["mygame"], "newlords is where pretenders are saved, not a game"
    assert "newlords" in SKIP_DIRECTORIES
    assert "empty" not in found


def test_discovery_survives_a_missing_root(tmp_path):
    assert discover_save_dirs(tmp_path / "nope") == []


def test_a_turn_dropped_in_is_ingested_without_being_asked(tmp_path):
    saves = tmp_path / "saves"; (saves / "mygame").mkdir(parents=True)
    service = AutoIngest(database=tmp_path / "game.sqlite3", root=saves,
                         poll_seconds=0.1, settle_seconds=0.1)
    service.start()
    try:
        assert service.status()["running"]
        shutil.copy2(_a_real_turn(), saves / "mygame" / "mid_marignon.trn")
        for _ in range(100):
            if service.status()["ingested"]:
                break
            time.sleep(0.1)
        status = service.status()
        assert status["ingested"] == 1 and status["failed"] == 0
        assert status["recent"][0]["turn"]
    finally:
        service.stop()
    assert not service.status()["running"]


def test_a_game_created_after_startup_is_picked_up(tmp_path):
    """The watcher used to fix its directory list at start, so a game begun
    while the server was up would never have been seen."""
    saves = tmp_path / "saves"; saves.mkdir()
    service = AutoIngest(database=tmp_path / "game.sqlite3", root=saves,
                         poll_seconds=0.1, settle_seconds=0.1)
    service.start()
    try:
        (saves / "later").mkdir()
        shutil.copy2(_a_real_turn(), saves / "later" / "mid_marignon.trn")
        for _ in range(100):
            if service.status()["ingested"]:
                break
            time.sleep(0.1)
        assert [Path(p).name for p in service.status()["watching"]] == ["later"]
    finally:
        service.stop()


def test_a_corrupt_save_is_reported_and_does_not_stop_the_watcher(tmp_path):
    saves = tmp_path / "saves"; (saves / "mygame").mkdir(parents=True)
    service = AutoIngest(database=tmp_path / "game.sqlite3", root=saves,
                         poll_seconds=0.1, settle_seconds=0.1)
    service.start()
    try:
        (saves / "mygame" / "broken.trn").write_bytes(b"not a save at all")
        for _ in range(100):
            if service.status()["failed"]:
                break
            time.sleep(0.1)
        assert service.status()["failed"] == 1
        assert service.status()["running"], "one bad file must not end the watch"
        # A good file afterwards is still ingested.
        shutil.copy2(_a_real_turn(), saves / "mygame" / "mid_marignon.trn")
        for _ in range(100):
            if service.status()["ingested"]:
                break
            time.sleep(0.1)
        assert service.status()["ingested"] == 1
    finally:
        service.stop()


def test_it_can_be_turned_off(monkeypatch, tmp_path):
    """A read-only session, or the tests, must be able to run without the
    server touching save files at all."""
    from dom6_assistant.gamestate import service as svc

    monkeypatch.setenv("DOM6_AUTO_INGEST", "0")
    enabled, _, _ = svc._configured()
    assert enabled is False
    monkeypatch.setenv("DOM6_AUTO_INGEST", "1")
    assert svc._configured()[0] is True


def test_the_status_endpoint_answers_even_when_nothing_is_running():
    from dom6_assistant.gamestate import service as svc

    svc.stop()
    status = svc.ingest_status()
    assert status["running"] is False
    assert "root" in status and status["recent"] == []
