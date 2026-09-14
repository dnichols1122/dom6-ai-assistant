"""Correlation routes — auto-identify memory addresses from journal data.

GET  /api/correlate              — current gem mapping + field correlation state
POST /api/correlate/gems/analyze — (re-)run gem mapping analysis, return result
POST /api/correlate/fields/scan  — scan/narrow memory for global field addresses
GET  /api/correlate/live         — live memory values for all resolved fields
POST /api/correlate/fields/{f}/override — manually set address for a field
DELETE /api/correlate/fields/{f}        — reset candidates + lock for a field
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dom6_assistant.memory_reader import attach
from dom6_assistant.journal.db import get_db

router = APIRouter(prefix="/correlate", tags=["correlate"])

# Global (turn-level) fields we can scan for.
# Maps field_name → (journal table column, struct format string)
_SCANNABLE = {
    "total_income": ("total_income", "<i"),
    "upkeep":       ("upkeep",       "<i"),
}

# ─────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────

class CorrectIn(BaseModel):
    value: int     # the actual in-game value for this field


class FieldState(BaseModel):
    candidates:     Optional[int]   # None = never scanned
    resolved:       bool
    locked_address: Optional[str]   # hex string


class GemMappingResult(BaseModel):
    status:               str   # "resolved" | "ambiguous" | "inconsistent" | "no_data"
    turns_checked:        int
    consistent_mappings:  list[dict]


class CorrelateOut(BaseModel):
    gem_mapping: GemMappingResult
    fields:      dict[str, FieldState]


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _analyze_gem_mapping(con) -> GemMappingResult:
    """Return the fixed 6.35 mapping proved by the all-unique debug game.

    The old three-slot permutation could never succeed: offsets F14 and F20
    are Astral and Glamour, not two members of Earth/Death/Blood.
    """
    turns = con.execute(
        "SELECT COUNT(*) FROM memory_snapshots").fetchone()[0]
    return GemMappingResult(
        status="resolved",
        turns_checked=turns,
        consistent_mappings=[{
            "0xF04": "fire", "0xF08": "air", "0xF0C": "water",
            "0xF10": "earth", "0xF14": "astral", "0xF18": "death",
            "0xF1C": "nature", "0xF20": "glamour", "0xF24": "blood",
        }],
    )


def _get_field_state(con, field_name: str) -> FieldState:
    row = con.execute(
        "SELECT candidates, locked_addr FROM field_correlations WHERE field_name=?",
        (field_name,),
    ).fetchone()
    if row is None:
        return FieldState(candidates=None, resolved=False, locked_address=None)
    candidates = json.loads(row["candidates"])
    return FieldState(
        candidates=len(candidates),
        resolved=row["locked_addr"] is not None,
        locked_address=hex(row["locked_addr"]) if row["locked_addr"] is not None else None,
    )


def _upsert_field(con, field_name: str, candidates: list[int]) -> None:
    locked = candidates[0] if len(candidates) == 1 else None
    with con:
        con.execute("""
            INSERT INTO field_correlations (field_name, candidates, locked_addr)
            VALUES (?, ?, ?)
            ON CONFLICT(field_name) DO UPDATE SET
                candidates  = excluded.candidates,
                locked_addr = excluded.locked_addr,
                updated_at  = datetime('now')
        """, (field_name, json.dumps(candidates), locked))


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

@router.get("", response_model=CorrelateOut)
def get_correlations() -> CorrelateOut:
    con = get_db()
    return CorrelateOut(
        gem_mapping=_analyze_gem_mapping(con),
        fields={name: _get_field_state(con, name) for name in _SCANNABLE},
    )


@router.post("/gems/analyze", response_model=GemMappingResult)
def analyze_gems() -> GemMappingResult:
    """Re-run gem mapping analysis and return the result."""
    return _analyze_gem_mapping(get_db())


@router.post("/fields/scan")
def scan_fields() -> dict:
    """Scan memory for current turn's global field values, or narrow existing candidates.

    Uses the latest recorded turn values from the journal as the expected values.
    May take 10–30 seconds on a large address space — the game's memory is 2+ GiB.
    Does not require the gold address to be locked.
    """
    try:
        reader = attach()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    con = get_db()
    all_turns = con.execute(
        "SELECT * FROM turns ORDER BY turn_number DESC"
    ).fetchall()
    if not all_turns:
        raise HTTPException(status_code=400, detail="No turns recorded in journal")

    results = {}
    for field_name, (col, fmt) in _SCANNABLE.items():
        # Collect all recorded values for this field, newest first.
        values = [t[col] for t in all_turns if t[col] is not None]
        if not values:
            results[field_name] = {"skipped": f"'{col}' not set in any recorded turn"}
            continue

        existing = con.execute(
            "SELECT candidates FROM field_correlations WHERE field_name=?",
            (field_name,),
        ).fetchone()

        if existing:
            # Already have candidates — narrow by the most recent value.
            prev = json.loads(existing["candidates"])
            new_candidates = reader.narrow(prev, values[0], fmt)
            action = f"narrowed {len(prev)} → {len(new_candidates)}"
        else:
            # Fresh scan for the most recent value.
            if fmt.endswith("i"):
                new_candidates = reader.scan_int32(values[0])
            else:
                new_candidates = reader.scan_uint32(values[0])
            action = f"fresh scan → {len(new_candidates)} candidates"

            # Immediately narrow using all older turn values while we have them.
            for older_value in values[1:]:
                if len(new_candidates) <= 1:
                    break
                new_candidates = reader.narrow(new_candidates, older_value, fmt)
            if len(values) > 1:
                action += f", narrowed by {len(values) - 1} prior turn(s) → {len(new_candidates)}"

        _upsert_field(con, field_name, new_candidates)
        state_out = _get_field_state(con, field_name)
        results[field_name] = {
            "action": action,
            **state_out.model_dump(),
        }

    return results


@router.get("/live")
def live_values() -> dict:
    """Return current memory values for all resolved field addresses."""
    try:
        reader = attach()
    except RuntimeError:
        return {}

    con = get_db()
    rows = con.execute(
        "SELECT field_name, locked_addr FROM field_correlations WHERE locked_addr IS NOT NULL"
    ).fetchall()

    out = {}
    for row in rows:
        try:
            out[row["field_name"]] = reader.read_int32(row["locked_addr"])
        except OSError:
            out[row["field_name"]] = None
    return out


@router.post("/fields/{field_name}/correct")
def correct_field(field_name: str, body: CorrectIn) -> FieldState:
    """Narrow candidates for a field using a user-supplied correct value.

    Use this when the auto-filled value was wrong — enter the actual
    in-game number and the candidate list will be re-narrowed against it.
    """
    if field_name not in _SCANNABLE:
        raise HTTPException(status_code=404, detail=f"Unknown field '{field_name}'")

    try:
        reader = attach()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    con = get_db()
    existing = con.execute(
        "SELECT candidates FROM field_correlations WHERE field_name=?",
        (field_name,),
    ).fetchone()
    if existing is None:
        raise HTTPException(status_code=400, detail="No candidates yet — run Scan first")

    _, fmt = _SCANNABLE[field_name]
    prev = json.loads(existing["candidates"])
    new_candidates = reader.narrow(prev, body.value, fmt)
    _upsert_field(con, field_name, new_candidates)
    return _get_field_state(con, field_name)


@router.delete("/fields/{field_name}", status_code=204)
def reset_field(field_name: str) -> None:
    """Clear candidates and lock for a field so it can be re-scanned."""
    if field_name not in _SCANNABLE:
        raise HTTPException(status_code=404, detail=f"Unknown field '{field_name}'")
    con = get_db()
    with con:
        con.execute("DELETE FROM field_correlations WHERE field_name=?", (field_name,))
