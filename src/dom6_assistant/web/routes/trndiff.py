"""API routes for .trn binary differential analysis (format reverse-engineering)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dom6_assistant.file_reader.trn_diff import (
    add_snapshot,
    analyze_session,
    diff_bytes,
    load_session,
    snapshot_path,
)

router = APIRouter(prefix="/trndiff", tags=["trndiff"])

_SESSIONS_ROOT = Path.home() / ".config" / "dom6-assistant" / "trn_sessions"
from dom6_assistant.paths import default_save_root
_DOM6_SAVES    = default_save_root()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_dir(session_name: str) -> Path:
    return _SESSIONS_ROOT / session_name


# ---------------------------------------------------------------------------
# Save file discovery
# ---------------------------------------------------------------------------

@router.get("/saves")
def list_saves() -> dict[str, Any]:
    """Scan ~/.dominions6/savedgames for available .trn files."""
    saves: list[dict[str, str]] = []
    if _DOM6_SAVES.exists():
        for game_dir in sorted(_DOM6_SAVES.iterdir()):
            if not game_dir.is_dir():
                continue
            for trn in sorted(game_dir.glob("*.trn")):
                saves.append({
                    "game": game_dir.name,
                    "nation": trn.stem,
                    "path": str(trn),
                    "label": f"{game_dir.name} / {trn.stem}",
                })
    return {"saves": saves}


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

@router.get("/sessions")
def list_sessions() -> dict[str, Any]:
    """List all existing diff sessions."""
    sessions: list[dict[str, Any]] = []
    if _SESSIONS_ROOT.exists():
        for sd in sorted(_SESSIONS_ROOT.iterdir()):
            if not sd.is_dir():
                continue
            snaps = load_session(sd)
            sessions.append({
                "name": sd.name,
                "snapshot_count": len(snaps),
                "snapshots": [s.label for s in snaps],
            })
    return {"sessions": sessions}


class CreateSessionBody(BaseModel):
    name: str


@router.post("/sessions")
def create_session(body: CreateSessionBody) -> dict[str, Any]:
    """Create a new (empty) diff session directory."""
    if not body.name or "/" in body.name or ".." in body.name:
        raise HTTPException(400, "Invalid session name")
    sd = _session_dir(body.name)
    sd.mkdir(parents=True, exist_ok=True)
    return {"name": body.name, "path": str(sd)}


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_name}/snapshots")
def get_snapshots(session_name: str) -> dict[str, Any]:
    sd = _session_dir(session_name)
    if not sd.exists():
        raise HTTPException(404, f"Session '{session_name}' not found")
    snaps = load_session(sd)
    return {
        "session": session_name,
        "snapshots": [
            {
                "label":     s.label,
                "timestamp": s.timestamp,
                "fields":    s.fields,
                "source":    s.source_path,
            }
            for s in snaps
        ],
    }


class SnapshotBody(BaseModel):
    trn_path: str
    label: str
    fields: dict[str, float] = {}


@router.post("/sessions/{session_name}/snapshot")
def take_snapshot(session_name: str, body: SnapshotBody) -> dict[str, Any]:
    """Copy a .trn file into the session with a label and known field values."""
    trn = Path(body.trn_path)
    if not trn.exists():
        raise HTTPException(404, f"File not found: {body.trn_path}")
    if not body.label:
        raise HTTPException(400, "label is required")
    sd = _session_dir(session_name)
    snap = add_snapshot(trn, sd, body.label, body.fields)
    return {
        "ok": True,
        "label": snap.label,
        "timestamp": snap.timestamp,
        "fields": snap.fields,
    }


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

class DiffBody(BaseModel):
    label_a: str
    label_b: str
    min_len: int = 1
    max_regions: int = 300


@router.post("/sessions/{session_name}/diff")
def diff_snapshots(session_name: str, body: DiffBody) -> dict[str, Any]:
    """Return byte-level diff between two snapshots."""
    sd = _session_dir(session_name)
    if not sd.exists():
        raise HTTPException(404, f"Session '{session_name}' not found")

    path_a = snapshot_path(sd, body.label_a)
    path_b = snapshot_path(sd, body.label_b)

    if not path_a.exists():
        raise HTTPException(404, f"Snapshot '{body.label_a}' not found")
    if not path_b.exists():
        raise HTTPException(404, f"Snapshot '{body.label_b}' not found")

    data_a = path_a.read_bytes()
    data_b = path_b.read_bytes()
    regions = diff_bytes(data_a, data_b)
    regions = [r for r in regions if r.length >= body.min_len]

    out = []
    for r in regions[: body.max_regions]:
        old_i32, new_i32 = r.decode_int32()
        old_i16, new_i16 = r.decode_int16()
        out.append({
            "offset":     r.offset,
            "offset_hex": f"0x{r.offset:06x}",
            "length":     r.length,
            "old_hex":    r.old_bytes[:16].hex(),
            "new_hex":    r.new_bytes[:16].hex(),
            "old_i32":    old_i32,
            "new_i32":    new_i32,
            "delta_i32":  (new_i32 - old_i32) if old_i32 is not None else None,
            "old_i16":    old_i16,
            "new_i16":    new_i16,
        })

    return {
        "label_a":       body.label_a,
        "label_b":       body.label_b,
        "size_a":        len(data_a),
        "size_b":        len(data_b),
        "total_regions": len(regions),
        "shown":         len(out),
        "regions":       out,
    }


# ---------------------------------------------------------------------------
# Correlation analysis
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_name}/analyze")
def analyze(session_name: str, top: int = 5) -> dict[str, Any]:
    """Correlate labeled field changes to byte offsets across all snapshots."""
    sd = _session_dir(session_name)
    if not sd.exists():
        raise HTTPException(404, f"Session '{session_name}' not found")

    results = analyze_session(sd)
    out: dict[str, Any] = {}
    for field_name, candidates in results.items():
        # Re-sort: exact-delta matches first, then by observation count
        def sort_key(c):
            exact = sum(1 for fo, fn, bo, bn in c.observed if abs((bn - bo) - (fn - fo)) < 1e-6)
            return (-exact, -len(c.observed))
        candidates = sorted(candidates, key=sort_key)

        out[field_name] = [
            {
                "offset":       c.offset,
                "offset_hex":   f"0x{c.offset:06x}",
                "length":       c.length,
                "observations": len(c.observed),
                # True if every observed delta matches the field delta exactly
                "exact_match":  all(abs((bn - bo) - (fn - fo)) < 1e-6
                                    for fo, fn, bo, bn in c.observed),
                "samples": [
                    {
                        "field_old": fo,
                        "field_new": fn,
                        "bytes_old": bo,
                        "bytes_new": bn,
                        "delta":     bn - bo,
                        "exact":     abs((bn - bo) - (fn - fo)) < 1e-6,
                    }
                    for fo, fn, bo, bn in c.observed[:4]
                ],
            }
            for c in candidates[:top]
        ]
    return {"session": session_name, "fields": out}
