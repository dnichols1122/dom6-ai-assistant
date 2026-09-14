from __future__ import annotations

import sqlite3

import pytest

from dom6_assistant.agent import conversations


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    conversations.ensure_schema(db)
    return db


def test_named_conversations_are_scoped_saved_and_archived() -> None:
    db = _db()
    initial = {"history": [{"role": "user", "content": "Yomi"}],
               "transcript": []}
    created = conversations.create(
        db, "pretender:23", "EA Yomi build", initial,
        conversation_id="yomi-build")
    assert created["id"] == "yomi-build"
    assert created["state"] == initial
    assert [row["title"] for row in conversations.list_all(
        db, "pretender:23")] == ["EA Yomi build"]
    assert conversations.list_all(db, "pretender:61") == []

    changed = {"history": initial["history"] + [
        {"role": "assistant", "content": "Oni Kunshu"}],
        "transcript": [{"kind": "bot", "text": "Oni Kunshu"}]}
    saved = conversations.save(
        db, "pretender:23", "yomi-build", changed,
        title="Oni Kunshu experiment")
    assert saved["title"] == "Oni Kunshu experiment"
    assert saved["state"] == changed

    conversations.archive(db, "pretender:23", "yomi-build")
    assert conversations.list_all(db, "pretender:23") == []
    with pytest.raises(conversations.ConversationError):
        conversations.get(db, "pretender:23", "yomi-build")


def test_conversation_state_requires_both_transparent_arrays() -> None:
    db = _db()
    with pytest.raises(conversations.ConversationError, match="history and transcript"):
        conversations.create(db, "turn:1", "bad", {"history": []})


def test_duplicate_ids_and_cross_scope_updates_are_refused() -> None:
    db = _db()
    empty = {"history": [], "transcript": []}
    conversations.create(
        db, "turn:1", "one", empty, conversation_id="same-id")
    with pytest.raises(conversations.ConversationError, match="already exists"):
        conversations.create(
            db, "turn:1", "two", empty, conversation_id="same-id")
    with pytest.raises(conversations.ConversationError, match="no conversation"):
        conversations.save(db, "turn:2", "same-id", empty)

