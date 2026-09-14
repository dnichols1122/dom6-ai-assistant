"""Persistent named assistant conversations.

Browser localStorage remains a responsive cache, but long tool transcripts are
too valuable (and too large) to entrust to a small per-origin quota.  The
SQLite row stores the same transparent history/transcript object the UI uses.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from dom6_assistant.agent.character_cards import validate_scope


MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_TITLE_CHARS = 160

SCHEMA = """
CREATE TABLE IF NOT EXISTS assistant_conversation (
    id         TEXT PRIMARY KEY,
    scope_key  TEXT NOT NULL,
    title      TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{"history":[],"transcript":[]}',
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_assistant_conversation_scope
    ON assistant_conversation(scope_key, archived, updated_at);
"""


class ConversationError(ValueError):
    pass


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def normalize_state(state: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(state, dict):
        raise ConversationError("conversation state must be an object")
    history = state.get("history")
    transcript = state.get("transcript")
    if not isinstance(history, list) or not isinstance(transcript, list):
        raise ConversationError(
            "conversation state requires history and transcript arrays")
    normalized = {"history": history, "transcript": transcript}
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
        raise ConversationError("conversation exceeds the 8 MiB storage limit")
    return normalized, encoded


def _title(value: str) -> str:
    title = " ".join((value or "").split()).strip()[:MAX_TITLE_CHARS]
    return title or "New chat"


def create(db: sqlite3.Connection, scope_key: str, title: str,
           state: dict[str, Any], *, conversation_id: str | None = None) -> dict[str, Any]:
    ensure_schema(db)
    scope = validate_scope(scope_key)
    normalized, encoded = normalize_state(state)
    cid = conversation_id or uuid.uuid4().hex
    if not cid or len(cid) > 120 or not all(
            char.isalnum() or char in "_.:-" for char in cid):
        raise ConversationError("invalid conversation id")
    try:
        db.execute(
            """INSERT INTO assistant_conversation(id,scope_key,title,state_json)
               VALUES(?,?,?,?)""", (cid, scope, _title(title), encoded))
    except sqlite3.IntegrityError as exc:
        raise ConversationError(f"conversation {cid!r} already exists") from exc
    db.commit()
    return get(db, scope, cid) | {"state": normalized}


def list_all(db: sqlite3.Connection, scope_key: str) -> list[dict[str, Any]]:
    ensure_schema(db)
    scope = validate_scope(scope_key)
    rows = db.execute(
        """SELECT id,scope_key,title,created_at,updated_at,
                  length(CAST(state_json AS BLOB)) AS bytes
             FROM assistant_conversation
            WHERE scope_key=? AND archived=0
            ORDER BY updated_at DESC,id DESC""", (scope,),
    ).fetchall()
    return [dict(row) for row in rows]


def get(db: sqlite3.Connection, scope_key: str,
        conversation_id: str) -> dict[str, Any]:
    ensure_schema(db)
    scope = validate_scope(scope_key)
    row = db.execute(
        """SELECT id,scope_key,title,state_json,created_at,updated_at
             FROM assistant_conversation
            WHERE id=? AND scope_key=? AND archived=0""",
        (conversation_id, scope),
    ).fetchone()
    if row is None:
        raise ConversationError(f"no conversation {conversation_id!r} in {scope}")
    result = dict(row)
    result["state"] = json.loads(result.pop("state_json"))
    return result


def save(db: sqlite3.Connection, scope_key: str, conversation_id: str,
         state: dict[str, Any], *, title: str | None = None) -> dict[str, Any]:
    ensure_schema(db)
    scope = validate_scope(scope_key)
    normalized, encoded = normalize_state(state)
    if title is None:
        changed = db.execute(
            """UPDATE assistant_conversation
                  SET state_json=?,updated_at=datetime('now')
                WHERE id=? AND scope_key=? AND archived=0""",
            (encoded, conversation_id, scope),
        ).rowcount
    else:
        changed = db.execute(
            """UPDATE assistant_conversation
                  SET state_json=?,title=?,updated_at=datetime('now')
                WHERE id=? AND scope_key=? AND archived=0""",
            (encoded, _title(title), conversation_id, scope),
        ).rowcount
    if not changed:
        raise ConversationError(
            f"no conversation {conversation_id!r} in {scope}")
    db.commit()
    return get(db, scope, conversation_id) | {"state": normalized}


def archive(db: sqlite3.Connection, scope_key: str,
            conversation_id: str) -> None:
    ensure_schema(db)
    scope = validate_scope(scope_key)
    changed = db.execute(
        """UPDATE assistant_conversation
              SET archived=1,updated_at=datetime('now')
            WHERE id=? AND scope_key=? AND archived=0""",
        (conversation_id, scope),
    ).rowcount
    if not changed:
        raise ConversationError(
            f"no conversation {conversation_id!r} in {scope}")
    db.commit()
