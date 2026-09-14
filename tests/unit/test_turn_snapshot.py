"""Automatic player-view turn snapshots stay coherent and non-destructive."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from dom6_assistant.__main__ import main
from dom6_assistant.file_reader.turn_snapshot import (
    MANIFEST_NAME,
    SnapshotNotReady,
    read_turn_files,
    save_snapshot,
    watch_turns,
)


def _turn_file(turn: int, payload: bytes = b"") -> bytes:
    data = bytearray(32)
    data[3:6] = b"DOM"
    struct.pack_into("<I", data, 0x0E, turn)
    return bytes(data) + payload


def _save(save_dir: Path, turn: int, *, suffix: bytes = b"") -> None:
    save_dir.mkdir(parents=True, exist_ok=True)
    for name in ("mid_marignon.trn", "mid_marignon.2h", "ftherlnd"):
        (save_dir / name).write_bytes(_turn_file(turn, name.encode() + suffix))


def _dual_save(save_dir: Path, turn: int, *, suffix: bytes = b"") -> None:
    save_dir.mkdir(parents=True, exist_ok=True)
    names = (
        "mid_ermor.trn", "mid_ermor.2h",
        "mid_marignon.trn", "mid_marignon.2h", "ftherlnd",
    )
    for name in names:
        (save_dir / name).write_bytes(_turn_file(turn, name.encode() + suffix))


def test_reads_only_a_coherent_three_file_turn(tmp_path):
    save = tmp_path / "save"
    _save(save, 39)
    (save / "mid_marignon.2h").write_bytes(_turn_file(38))

    with pytest.raises(SnapshotNotReady, match="coherent turn"):
        read_turn_files(save)


def test_snapshot_contains_all_files_manifest_and_hashes(tmp_path):
    save, out = tmp_path / "save", tmp_path / "snapshots"
    _save(save, 39)

    result = save_snapshot(read_turn_files(save), out)

    assert result.created
    assert result.path == out / "t39-auto"
    assert {path.name for path in result.path.iterdir()} == {
        "mid_marignon.trn", "mid_marignon.2h", "ftherlnd", MANIFEST_NAME}
    manifest = json.loads((result.path / MANIFEST_NAME).read_text())
    assert manifest["turn"] == 39
    assert set(manifest["files"]) == {
        "mid_marignon.trn", "mid_marignon.2h", "ftherlnd"}
    assert all(len(row["sha256"]) == 64 for row in manifest["files"].values())


def test_dual_human_snapshot_contains_both_views_and_shared_world(tmp_path):
    save, out = tmp_path / "save", tmp_path / "snapshots"
    _dual_save(save, 1)

    result = save_snapshot(read_turn_files(save), out)

    expected = {
        "mid_ermor.trn", "mid_ermor.2h",
        "mid_marignon.trn", "mid_marignon.2h", "ftherlnd",
    }
    assert {path.name for path in result.path.iterdir()} == expected | {MANIFEST_NAME}
    manifest = json.loads((result.path / MANIFEST_NAME).read_text())
    assert manifest["nation_stems"] == ["mid_ermor", "mid_marignon"]
    assert set(manifest["files"]) == expected


def test_dual_human_snapshot_waits_for_both_order_files(tmp_path):
    save = tmp_path / "save"
    _dual_save(save, 1)
    (save / "mid_ermor.2h").write_bytes(_turn_file(0))

    with pytest.raises(SnapshotNotReady, match="implausible turn 0"):
        read_turn_files(save)


def test_identical_capture_deduplicates_and_changed_same_turn_never_overwrites(
        tmp_path):
    save, out = tmp_path / "save", tmp_path / "snapshots"
    _save(save, 39)
    first = save_snapshot(read_turn_files(save), out)
    duplicate = save_snapshot(read_turn_files(save), out)

    assert not duplicate.created
    assert duplicate.path == first.path

    _save(save, 39, suffix=b"changed")
    changed = save_snapshot(read_turn_files(save), out)
    assert changed.created
    assert changed.path == out / "t39-auto-2"
    assert (first.path / "mid_marignon.trn").read_bytes() != (
        changed.path / "mid_marignon.trn").read_bytes()


def test_cli_once_waits_for_settlement_then_exits(tmp_path):
    save, out = tmp_path / "save", tmp_path / "snapshots"
    _save(save, 40)

    result = CliRunner().invoke(main, [
        "snapshot-watch", str(save), "--out", str(out),
        "--settle", "0", "--poll", "0.05", "--once",
    ])

    assert result.exit_code == 0, result.output
    assert (out / "t40-auto" / "mid_marignon.trn").is_file()
    assert (out / "t40-auto" / "mid_marignon.2h").is_file()
    assert (out / "t40-auto" / "ftherlnd").is_file()


def test_cli_auto_detects_and_captures_two_human_players(tmp_path):
    save, out = tmp_path / "save", tmp_path / "snapshots"
    _dual_save(save, 1)

    result = CliRunner().invoke(main, [
        "snapshot-watch", str(save), "--out", str(out),
        "--settle", "0", "--poll", "0.05", "--once",
    ])

    assert result.exit_code == 0, result.output
    assert "mid_ermor.trn/.2h + mid_marignon.trn/.2h" in result.output
    assert (out / "t1-auto" / "mid_ermor.trn").is_file()
    assert (out / "t1-auto" / "mid_marignon.2h").is_file()


def test_watcher_captures_multiple_changed_saves_in_the_same_turn(
        tmp_path, monkeypatch):
    save, out = tmp_path / "save", tmp_path / "snapshots"
    _save(save, 44)
    monkeypatch.setattr(
        "dom6_assistant.file_reader.turn_snapshot.time.sleep", lambda _seconds: None
    )
    watched = watch_turns(save, out, poll_seconds=0.01, settle_seconds=0)

    first = next(watched)
    (save / "mid_marignon.2h").write_bytes(
        _turn_file(44, b"mid_marignon.2h-equipped")
    )
    second = next(watched)

    assert first.path == out / "t44-auto"
    assert second.path == out / "t44-auto-2"
    assert first.path.joinpath("mid_marignon.2h").read_bytes() != (
        second.path / "mid_marignon.2h").read_bytes()


def test_skip_current_ignores_only_the_exact_starting_state(
        tmp_path, monkeypatch):
    save, out = tmp_path / "save", tmp_path / "snapshots"
    _save(save, 44)
    starting = read_turn_files(save)
    calls = 0

    def advance_same_turn(_seconds):
        nonlocal calls
        calls += 1
        if calls == 1:
            (save / "mid_marignon.2h").write_bytes(
                _turn_file(44, b"mid_marignon.2h-boots-equipped")
            )

    monkeypatch.setattr(
        "dom6_assistant.file_reader.turn_snapshot.time.sleep", advance_same_turn
    )
    watched = watch_turns(
        save, out, skip_fingerprint=starting.fingerprint,
        poll_seconds=0.01, settle_seconds=0,
    )

    result = next(watched)

    assert result.turn == 44
    assert b"boots-equipped" in (result.path / "mid_marignon.2h").read_bytes()
