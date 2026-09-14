"""Binary differential analysis tools for .trn format reverse-engineering.

Workflow
--------
1. Establish a benchmark save with known game state values::

       dom6-assistant trn-snapshot ~/.dominions6/savedgames/mygame/mid_pangaea.trn \\
           /tmp/trn_session/ \\
           --label benchmark \\
           --fields gold=600,turn=1,fire=0,air=0,nature=0

2. Change something in-game (spend gold, end a turn, cast a ritual …).

3. Snapshot the updated save::

       dom6-assistant trn-snapshot ~/.dominions6/savedgames/mygame/mid_pangaea.trn \\
           /tmp/trn_session/ \\
           --label spent_gold \\
           --fields gold=550

4. Diff any two snapshots to see what bytes changed::

       dom6-assistant trn-diff /tmp/trn_session/benchmark.trn /tmp/trn_session/spent_gold.trn

5. After several snapshots, correlate field changes to byte offsets::

       dom6-assistant trn-analyze /tmp/trn_session/

The analyze step shows, for each labeled field, which byte regions changed
consistently with that field's value — narrowing down candidate offsets quickly.
"""

from __future__ import annotations

import json
import shutil
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Low-level diff primitives
# ---------------------------------------------------------------------------

@dataclass
class ChangedRegion:
    """A contiguous run of bytes that differ between two files."""
    offset: int
    old_bytes: bytes
    new_bytes: bytes

    @property
    def length(self) -> int:
        return len(self.old_bytes)

    def decode_int32(self) -> tuple[Optional[int], Optional[int]]:
        """Try to decode old/new as int32 LE. Returns (None, None) if too short."""
        if len(self.old_bytes) >= 4 and len(self.new_bytes) >= 4:
            return (
                struct.unpack_from("<i", self.old_bytes)[0],
                struct.unpack_from("<i", self.new_bytes)[0],
            )
        return None, None

    def decode_uint32(self) -> tuple[Optional[int], Optional[int]]:
        if len(self.old_bytes) >= 4 and len(self.new_bytes) >= 4:
            return (
                struct.unpack_from("<I", self.old_bytes)[0],
                struct.unpack_from("<I", self.new_bytes)[0],
            )
        return None, None

    def decode_int16(self) -> tuple[Optional[int], Optional[int]]:
        if len(self.old_bytes) >= 2 and len(self.new_bytes) >= 2:
            return (
                struct.unpack_from("<h", self.old_bytes)[0],
                struct.unpack_from("<h", self.new_bytes)[0],
            )
        return None, None


def diff_bytes(baseline: bytes, target: bytes) -> list[ChangedRegion]:
    """Return a list of contiguous changed regions between baseline and target.

    Files of different lengths: the shorter file is treated as zero-padded.
    """
    length = max(len(baseline), len(target))
    regions: list[ChangedRegion] = []

    in_run = False
    run_start = 0
    old_run: list[int] = []
    new_run: list[int] = []

    for i in range(length):
        old_b = baseline[i] if i < len(baseline) else 0
        new_b = target[i] if i < len(target) else 0

        if old_b != new_b:
            if not in_run:
                in_run = True
                run_start = i
                old_run = []
                new_run = []
            old_run.append(old_b)
            new_run.append(new_b)
        else:
            if in_run:
                regions.append(ChangedRegion(
                    offset=run_start,
                    old_bytes=bytes(old_run),
                    new_bytes=bytes(new_run),
                ))
                in_run = False

    if in_run:
        regions.append(ChangedRegion(
            offset=run_start,
            old_bytes=bytes(old_run),
            new_bytes=bytes(new_run),
        ))

    return regions


# ---------------------------------------------------------------------------
# Snapshot management
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    """A labeled copy of a .trn file with known field values."""
    label: str
    source_path: str
    saved_path: str          # path within the session directory
    timestamp: str
    fields: dict[str, float] = field(default_factory=dict)


def snapshot_path(session_dir: Path, label: str) -> Path:
    """Return the .trn file path for a snapshot label."""
    return session_dir / f"{label}.trn"


def meta_path(session_dir: Path) -> Path:
    return session_dir / "session.json"


def load_session(session_dir: Path) -> list[Snapshot]:
    mp = meta_path(session_dir)
    if not mp.exists():
        return []
    data = json.loads(mp.read_text())
    return [Snapshot(**s) for s in data]


def save_session(session_dir: Path, snapshots: list[Snapshot]) -> None:
    meta_path(session_dir).write_text(
        json.dumps([s.__dict__ for s in snapshots], indent=2)
    )


def add_snapshot(
    trn_path: Path,
    session_dir: Path,
    label: str,
    fields: dict[str, float],
) -> Snapshot:
    """Copy trn_path into session_dir, record metadata, return Snapshot."""
    session_dir.mkdir(parents=True, exist_ok=True)
    dest = snapshot_path(session_dir, label)
    shutil.copy2(trn_path, dest)

    snap = Snapshot(
        label=label,
        source_path=str(trn_path),
        saved_path=str(dest),
        timestamp=datetime.now(timezone.utc).isoformat(),
        fields=fields,
    )

    snapshots = load_session(session_dir)
    # Replace existing snapshot with same label if present
    snapshots = [s for s in snapshots if s.label != label]
    snapshots.append(snap)
    save_session(session_dir, snapshots)
    return snap


# ---------------------------------------------------------------------------
# Correlation analysis
# ---------------------------------------------------------------------------

@dataclass
class FieldCandidate:
    """A byte offset that is a candidate for encoding a specific field."""
    offset: int
    length: int          # bytes in the changed region
    # For each snapshot pair where this field changed, the decoded int32 delta
    observed: list[tuple[float, float, int, int]]  # (field_old, field_new, bytes_old_i32, bytes_new_i32)


def _read_i32_at(data: bytes, offset: int) -> Optional[int]:
    """Read an int32 LE from data at offset, returning None if out of range."""
    if offset + 4 <= len(data):
        return struct.unpack_from("<i", data, offset)[0]
    return None


def analyze_session(session_dir: Path) -> dict[str, list[FieldCandidate]]:
    """Correlate field value changes to byte offset changes across all snapshots.

    For each changed byte position, we read a full int32 from the surrounding
    bytes in both files (not just the differing bytes), so partial changes to
    multi-byte integers are correctly decoded.

    Returns a dict mapping field_name → list of FieldCandidate sorted by
    how consistently the offset tracks the field value.
    """
    snapshots = load_session(session_dir)
    if len(snapshots) < 2:
        return {}

    # Load all snapshot bytes
    snap_bytes: dict[str, bytes] = {}
    for s in snapshots:
        p = Path(s.saved_path)
        if p.exists():
            snap_bytes[s.label] = p.read_bytes()

    # Collect all fields that appear in any snapshot
    all_fields: set[str] = set()
    for s in snapshots:
        all_fields.update(s.fields.keys())

    results: dict[str, list[FieldCandidate]] = {}

    for field_name in sorted(all_fields):
        # Find all snapshot pairs where this field has different values
        field_snaps = [(s, s.fields[field_name]) for s in snapshots
                       if field_name in s.fields and s.label in snap_bytes]
        if len(field_snaps) < 2:
            continue

        # For each consecutive pair, find changed regions
        candidate_map: dict[int, FieldCandidate] = {}  # offset → candidate

        for i in range(len(field_snaps) - 1):
            snap_a, val_a = field_snaps[i]
            snap_b, val_b = field_snaps[i + 1]
            if val_a == val_b:
                continue  # field didn't change between these two

            data_a = snap_bytes[snap_a.label]
            data_b = snap_bytes[snap_b.label]
            regions = diff_bytes(data_a, data_b)
            field_delta = val_b - val_a

            for reg in regions:
                # Try reading int32 from up to 3 alignments around this changed region.
                # This handles the case where only part of a 4-byte int changed.
                offsets_to_try: set[int] = set()
                for byte_pos in range(reg.offset, reg.offset + reg.length):
                    for align in range(4):
                        candidate_off = byte_pos - align
                        if candidate_off >= 0:
                            offsets_to_try.add(candidate_off)

                for off in offsets_to_try:
                    old_i32 = _read_i32_at(data_a, off)
                    new_i32 = _read_i32_at(data_b, off)
                    if old_i32 is None or new_i32 is None:
                        continue
                    bytes_delta = new_i32 - old_i32
                    # Accept if delta matches exactly OR same direction (loose mode)
                    if abs(bytes_delta - field_delta) < 1e-6 or (
                        field_delta != 0 and bytes_delta != 0 and
                        (field_delta > 0) == (bytes_delta > 0)
                    ):
                        if off not in candidate_map:
                            candidate_map[off] = FieldCandidate(
                                offset=off,
                                length=reg.length,
                                observed=[],
                            )
                        candidate_map[off].observed.append(
                            (val_a, val_b, old_i32, new_i32)
                        )

        # Sort candidates by number of consistent observations (descending)
        candidates = sorted(candidate_map.values(), key=lambda c: -len(c.observed))
        if candidates:
            results[field_name] = candidates

    return results
