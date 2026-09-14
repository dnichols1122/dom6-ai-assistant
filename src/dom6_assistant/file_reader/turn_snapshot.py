"""Coherent, automatic snapshots of every human player's Dominions files.

Dominions does not rewrite the player ``.trn``/``.2h`` pairs and shared
``ftherlnd`` atomically. A watcher that copies as soon as the first mtime moves
can therefore pair one player's new turn with another player's previous orders.
This module waits until every requested file reports the same turn and their
complete contents have remained unchanged for a settling interval before
publishing one directory atomically.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


TURN_OFFSET = 0x0E
HEADER_SIZE = TURN_OFFSET + 4
MANIFEST_NAME = "SNAPSHOT.json"


class SnapshotNotReady(RuntimeError):
    """The required files are missing, moving, invalid, or from different turns."""


@dataclass(frozen=True)
class TurnFiles:
    turn: int
    source_dir: Path
    nation_stems: tuple[str, ...]
    contents: dict[str, bytes]
    mtimes_ns: dict[str, int]

    @property
    def nation_stem(self) -> str | None:
        """Backward-compatible singular stem for old three-file callers."""
        return self.nation_stems[0] if len(self.nation_stems) == 1 else None

    @property
    def hashes(self) -> dict[str, str]:
        return {
            name: hashlib.sha256(data).hexdigest()
            for name, data in self.contents.items()
        }

    @property
    def fingerprint(self) -> tuple[int, tuple[tuple[str, str], ...]]:
        return self.turn, tuple(sorted(self.hashes.items()))


@dataclass(frozen=True)
class SnapshotResult:
    turn: int
    path: Path
    created: bool


def file_turn(data: bytes, name: str = "turn file") -> int:
    """Read the common turn number in a ``.trn``, ``.2h`` or ``ftherlnd``."""
    if len(data) < HEADER_SIZE or data[3:6] != b"DOM":
        raise SnapshotNotReady(f"{name} has no valid Dominions header")
    turn = struct.unpack_from("<I", data, TURN_OFFSET)[0]
    if not 0 < turn < 100000:
        raise SnapshotNotReady(f"{name} has implausible turn {turn}")
    return turn


def resolve_nation_stem(save_dir: Path, nation_stem: str | None = None) -> str:
    """Return the requested stem, or auto-detect the sole player ``.trn``."""
    stems = resolve_nation_stems(
        save_dir, (nation_stem,) if nation_stem is not None else None)
    if len(stems) != 1:
        raise SnapshotNotReady(
            f"expected exactly one .trn in {save_dir}, found {list(stems)}"
        )
    return stems[0]


def resolve_nation_stems(
    save_dir: Path, nation_stems: tuple[str, ...] | list[str] | None = None
) -> tuple[str, ...]:
    """Return explicit stems, or auto-detect every human player ``.trn``."""
    save_dir = Path(save_dir)
    requested = nation_stems or ()
    if requested:
        cleaned: list[str] = []
        for requested_stem in requested:
            stem = Path(requested_stem).name
            if stem.endswith(".trn"):
                stem = stem[:-4]
            if not stem:
                raise SnapshotNotReady("nation stem must not be empty")
            if stem not in cleaned:
                cleaned.append(stem)
        return tuple(cleaned)
    candidates = tuple(sorted(path.stem for path in save_dir.glob("*.trn")))
    if not candidates:
        raise SnapshotNotReady(f"no .trn player files found in {save_dir}")
    return candidates


def source_paths(save_dir: Path, nation_stems: tuple[str, ...]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for stem in nation_stems:
        paths[f"{stem}.trn"] = save_dir / f"{stem}.trn"
        paths[f"{stem}.2h"] = save_dir / f"{stem}.2h"
    paths["ftherlnd"] = save_dir / "ftherlnd"
    return paths


def read_turn_files(
    save_dir: Path,
    nation_stem: str | tuple[str, ...] | list[str] | None = None,
) -> TurnFiles:
    """Read one coherent same-turn set for one or more human players."""
    save_dir = Path(save_dir)
    requested = (nation_stem,) if isinstance(nation_stem, str) else nation_stem
    stems = resolve_nation_stems(save_dir, requested)
    contents: dict[str, bytes] = {}
    mtimes: dict[str, int] = {}
    turns: dict[str, int] = {}
    for name, path in source_paths(save_dir, stems).items():
        try:
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
        except FileNotFoundError as exc:
            raise SnapshotNotReady(f"waiting for {path}") from exc
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SnapshotNotReady(f"{path.name} changed while it was being read")
        contents[name] = data
        mtimes[name] = after.st_mtime_ns
        turns[name] = file_turn(data, path.name)
    unique_turns = set(turns.values())
    if len(unique_turns) != 1:
        detail = ", ".join(f"{name}={turn}" for name, turn in turns.items())
        raise SnapshotNotReady(f"waiting for one coherent turn: {detail}")
    return TurnFiles(
        turn=unique_turns.pop(),
        source_dir=save_dir.resolve(),
        nation_stems=stems,
        contents=contents,
        mtimes_ns=mtimes,
    )


def _manifest(files: TurnFiles) -> dict:
    hashes = files.hashes
    return {
        "turn": files.turn,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(files.source_dir),
        "nation_stems": list(files.nation_stems),
        "files": {
            name: {
                "size": len(data),
                "sha256": hashes[name],
                "source_mtime_ns": files.mtimes_ns[name],
            }
            for name, data in files.contents.items()
        },
    }


def _manifest_hashes(path: Path) -> dict[str, str] | None:
    try:
        data = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
        return {name: value["sha256"] for name, value in data["files"].items()}
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def save_snapshot(files: TurnFiles, output_root: Path) -> SnapshotResult:
    """Publish ``files`` as ``tTURN-auto``, deduplicating identical captures."""
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    base_name = f"t{files.turn}-auto"
    hashes = files.hashes
    existing = sorted(output_root.glob(f"{base_name}*"))
    for path in existing:
        if path.is_dir() and _manifest_hashes(path) == hashes:
            return SnapshotResult(turn=files.turn, path=path, created=False)

    destination = output_root / base_name
    suffix = 2
    while destination.exists():
        destination = output_root / f"{base_name}-{suffix}"
        suffix += 1

    temp = Path(tempfile.mkdtemp(prefix=f".{base_name}-", dir=output_root))
    try:
        for name, data in files.contents.items():
            (temp / name).write_bytes(data)
        (temp / MANIFEST_NAME).write_text(
            json.dumps(_manifest(files), indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temp, destination)
    except BaseException:
        for child in temp.iterdir():
            child.unlink()
        temp.rmdir()
        raise
    return SnapshotResult(turn=files.turn, path=destination, created=True)


def watch_turns(
    save_dir: Path,
    output_root: Path,
    *,
    nation_stem: str | tuple[str, ...] | list[str] | None = None,
    after_turn: int | None = None,
    skip_fingerprint: tuple[int, tuple[tuple[str, str], ...]] | None = None,
    poll_seconds: float = 0.5,
    settle_seconds: float = 1.0,
) -> Iterator[SnapshotResult]:
    """Yield each new settled multi-player file state.

    ``after_turn`` is a fixed lower bound, not the last emitted turn: later
    saves of the same turn are valuable controlled diffs and must be retained.
    ``skip_fingerprint`` ignores one exact starting state while still allowing
    a changed ``.2h`` from that turn to be captured.
    """
    if poll_seconds <= 0 or settle_seconds < 0:
        raise ValueError("poll must be positive and settle must not be negative")
    stable_fingerprint: tuple[int, tuple[tuple[str, str], ...]] | None = None
    stable_since = 0.0
    emitted: set[tuple[int, tuple[tuple[str, str], ...]]] = set()
    if skip_fingerprint is not None:
        emitted.add(skip_fingerprint)
    while True:
        try:
            files = read_turn_files(save_dir, nation_stem)
        except SnapshotNotReady:
            stable_fingerprint = None
            time.sleep(poll_seconds)
            continue
        if after_turn is not None and files.turn <= after_turn:
            stable_fingerprint = None
            time.sleep(poll_seconds)
            continue
        if files.fingerprint in emitted:
            stable_fingerprint = None
            time.sleep(poll_seconds)
            continue

        now = time.monotonic()
        if files.fingerprint != stable_fingerprint:
            stable_fingerprint = files.fingerprint
            stable_since = now
        elif now - stable_since >= settle_seconds:
            result = save_snapshot(files, output_root)
            emitted.add(files.fingerprint)
            stable_fingerprint = None
            yield result
        time.sleep(poll_seconds)
