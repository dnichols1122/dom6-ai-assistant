"""Auditable readiness handshake for autonomous turns."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dom6_assistant.agent.decisions import decision_history
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.orders import materialize as M


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decision_fingerprint(
    db: sqlite3.Connection, game_id: int, turn: int
) -> str:
    current = [
        {
            "decision_ref": row["decision_ref"],
            "category": row["category"],
            "subject_key": row["subject_key"],
            "action": row["action"],
            "rationale": row["rationale"],
        }
        for row in decision_history(db, game_id, turn=turn, limit=5000)
        if row["is_current_revision"]
    ]
    current.sort(key=lambda row: (row["category"], row["decision_ref"]))
    payload = json.dumps(
        current, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CompletionStatus:
    complete: bool
    reason: str
    summary: str | None = None
    outstanding_risks: str | None = None
    completed_at: str | None = None
    submitted: bool = False
    submitted_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "reason": self.reason,
            "summary": self.summary,
            "outstanding_risks": self.outstanding_risks,
            "completed_at": self.completed_at,
            "submitted": self.submitted,
            "submitted_at": self.submitted_at,
        }


def file_is_submitted(h2_path: Path | None) -> bool:
    """Best-effort state for status reporting; mutation uses strict decoding."""
    if h2_path is None or not h2_path.exists():
        return False
    try:
        return H2.turn_is_submitted(h2_path.read_bytes())
    except (OSError, H2.SubmissionStateError):
        return False


def completion_status(
    db: sqlite3.Connection,
    game_id: int,
    turn: int,
    h2_path: Path | None,
) -> CompletionStatus:
    submitted = file_is_submitted(h2_path)
    row = db.execute(
        "SELECT * FROM agent_turn_completion WHERE game_id=? AND turn=?",
        (game_id, turn),
    ).fetchone()
    if row is None:
        return CompletionStatus(
            False, "no completion handshake was recorded", submitted=submitted)
    if h2_path is None or not h2_path.exists():
        return CompletionStatus(False, "the live .2h is unavailable")
    if row["h2_sha256"] != file_sha256(h2_path):
        return CompletionStatus(
            False, "the live .2h changed after the completion handshake",
            submitted=submitted, submitted_at=row["submitted_at"])
    if row["decision_fingerprint"] != decision_fingerprint(db, game_id, turn):
        return CompletionStatus(
            False, "recorded decisions changed after the completion handshake",
            submitted=submitted, submitted_at=row["submitted_at"])
    if not M.matches_last_written(h2_path):
        return CompletionStatus(
            False, "the live .2h is not the assistant's last materialisation",
            submitted=submitted, submitted_at=row["submitted_at"])
    return CompletionStatus(
        True,
        ("submitted .2h, materialized file, and decision fingerprint still match"
         if submitted else
         "materialized file and decision fingerprint still match"),
        summary=row["summary"],
        outstanding_risks=row["outstanding_risks"],
        completed_at=row["completed_at"],
        submitted=submitted,
        submitted_at=row["submitted_at"],
    )
