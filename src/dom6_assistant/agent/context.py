"""Transparent working context for each model invocation.

This is deliberately a deterministic retriever, not an embedding black box.
The player gives a playbook entry explicit trigger words or phrases; a match is
logged with the exact trigger that caused it.  That makes a bad insertion
correctable from the UI instead of leaving the player to wonder which semantic
similarity happened to fire.

Four kinds of memory remain separate:

* the live turn bootstrap is exact, player-visible state;
* the scratchpad is campaign-local working memory;
* the playbook is player-authored strategic guidance;
* action rationales live in the append-only intent tables and are not inferred
  from model prose.

Already-visible reasoning/prose and tool names are retained only as retrieval
signals for the next invocation.  They are not treated as a decision record.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from dom6_assistant.agent.decisions import decision_history


_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9][a-z0-9'_-]*")


class ContextSession(Protocol):
    ctx: Any
    turn: int

    def call(self, name: str, args: dict | None = None) -> dict: ...


@dataclass(frozen=True)
class RetrievedEntry:
    id: int
    title: str
    guidance: str
    tags: tuple[str, ...]
    score: int
    matched_triggers: tuple[str, ...]
    priority: int


@dataclass
class ContextBundle:
    rendered: str
    retrieval_text: str
    turn_summary: dict[str, Any] | None = None
    current_decisions: list[dict[str, Any]] = field(default_factory=list)
    scratchpad: list[dict[str, Any]] = field(default_factory=list)
    playbook: list[RetrievedEntry] = field(default_factory=list)
    run_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "turn_summary": self.turn_summary,
            "current_decisions": self.current_decisions,
            "scratchpad": self.scratchpad,
            "playbook": [
                {
                    "id": row.id,
                    "title": row.title,
                    "guidance": row.guidance,
                    "tags": list(row.tags),
                    "priority": row.priority,
                    "score": row.score,
                    "matched_triggers": list(row.matched_triggers),
                }
                for row in self.playbook
            ],
            "rendered": self.rendered,
        }


def _normalise(value: str) -> str:
    return _SPACE.sub(" ", value.casefold()).strip()


def _json_list(value: Any) -> list[str]:
    try:
        found = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(found, list):
        return []
    return [str(item).strip() for item in found if str(item).strip()]


def _history_text(history: Iterable[dict[str, Any]]) -> str:
    # Recent visible conversation is sufficient and bounded. Tool payloads are
    # intentionally absent from browser history and need not be re-injected.
    parts = []
    for message in list(history)[-6:]:
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
    return "\n".join(parts)


def _previous_signals(db: sqlite3.Connection, game_id: int) -> str:
    try:
        row = db.execute(
            "SELECT emitted_signals, visible_response, tool_names_json "
            "FROM agent_context_run WHERE game_id=? AND completed_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (game_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    if row is None:
        return ""
    tools = " ".join(_json_list(row["tool_names_json"]))
    return "\n".join(
        part for part in (row["emitted_signals"], row["visible_response"], tools)
        if part
    )


def _retrieve(
    db: sqlite3.Connection,
    *,
    game_id: int,
    nation_id: int,
    query: str,
    limit: int = 8,
) -> list[RetrievedEntry]:
    normal = _normalise(query)
    words = set(_WORD.findall(normal))
    rows = db.execute(
        "SELECT * FROM playbook_entry WHERE enabled=1 "
        "AND (game_id IS NULL OR game_id=?) "
        "AND (nation_id IS NULL OR nation_id=?) "
        "ORDER BY priority DESC, id",
        (game_id, nation_id),
    )
    found: list[RetrievedEntry] = []
    for row in rows:
        triggers = _json_list(row["triggers_json"])
        matched: list[str] = []
        for raw in triggers:
            trigger = _normalise(raw.lstrip("#"))
            if not trigger:
                continue
            if " " in trigger:
                if trigger in normal:
                    matched.append(raw)
            elif trigger in words:
                matched.append(raw)
        if not row["always_include"] and not matched:
            continue
        # Priority controls ordering, while explicit matches dominate entries
        # that the player chose to include on every invocation.
        score = int(row["priority"]) + 100 * len(matched)
        if row["always_include"]:
            score += 25
        found.append(RetrievedEntry(
            id=int(row["id"]),
            title=row["title"],
            guidance=row["guidance"],
            tags=tuple(_json_list(row["tags_json"])),
            score=score,
            matched_triggers=tuple(matched),
            priority=int(row["priority"]),
        ))
    found.sort(key=lambda item: (-item.score, item.id))
    return found[:max(1, min(limit, 20))]


def _result(session: ContextSession, tool: str) -> Any:
    try:
        response = session.call(tool)
    except Exception:  # context must never prevent the assistant from opening
        return None
    return response.get("result") if response.get("ok") else None


def _scratchpad(db: sqlite3.Connection, game_id: int) -> list[dict[str, Any]]:
    try:
        rows = db.execute(
            "SELECT id,turn,tag,note,status,pinned,created_at,updated_at "
            "FROM scratchpad WHERE game_id=? AND status='open' "
            "ORDER BY pinned DESC, COALESCE(turn,-1) DESC, id DESC LIMIT 20",
            (game_id,),
        )
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def _render(
    turn_summary: dict[str, Any] | None,
    current_decisions: list[dict[str, Any]],
    scratchpad: list[dict[str, Any]],
    playbook: list[RetrievedEntry],
) -> str:
    sections = [
        "AUTOMATIC CONTEXT (assembled locally; exact sources are visible in "
        "the Context panel). Current user instructions override older notes "
        "or playbook guidance when they conflict.",
    ]
    if turn_summary is not None:
        sections.append(
            "LIVE TURN BOOTSTRAP — exact player-visible summary:\n" +
            json.dumps(turn_summary, ensure_ascii=False, separators=(",", ":"))
        )
    if current_decisions:
        compact = [
            {
                "decision_ref": row["decision_ref"],
                "category": row["category"],
                "subject": row["subject"],
                "action": row["action"],
                "rationale": row["rationale"],
                "supersedes": row["supersedes"],
            }
            for row in current_decisions
        ]
        included: list[dict[str, Any]] = []
        for row in compact:
            candidate = [*included, row]
            if len(json.dumps(candidate, ensure_ascii=False)) > 12000:
                break
            included.append(row)
        omitted = len(compact) - len(included)
        if omitted:
            included.append({
                "omitted_current_decisions": omitted,
                "instruction": (
                    "Call get_decision_history for this turn to inspect them."
                ),
            })
        sections.append(
            "CURRENT-TURN DECISIONS — recorded intent and explicit rationale:\n" +
            json.dumps(included, ensure_ascii=False,
                       separators=(",", ":"))
        )
    if scratchpad:
        lines = []
        for note in scratchpad:
            marker = "PINNED " if note["pinned"] else ""
            lines.append(
                f"- [{marker}{note['tag'] or 'note'}; turn "
                f"{note['turn'] if note['turn'] is not None else '?'}; "
                f"id {note['id']}] {note['note']}"
            )
        sections.append("OPEN CAMPAIGN SCRATCHPAD:\n" + "\n".join(lines))
    if playbook:
        lines = []
        for entry in playbook:
            reason = (
                "always included" if not entry.matched_triggers else
                "matched " + ", ".join(entry.matched_triggers)
            )
            lines.append(
                f"- [player guidance #{entry.id}; {reason}] "
                f"{entry.title}: {entry.guidance}"
            )
        sections.append("RETRIEVED PLAYER PLAYBOOK:\n" + "\n".join(lines))
    return "\n\n".join(sections)


def assemble_context(
    session: ContextSession,
    message: str,
    history: list[dict[str, Any]] | None = None,
    *,
    record: bool = True,
) -> ContextBundle:
    # Small scripted-test or pregame sessions can intentionally expose only a
    # registry and a database connection. They have no campaign identity and
    # therefore no campaign memory to assemble.
    if not hasattr(session.ctx, "game_id"):
        return ContextBundle(rendered="", retrieval_text=message)
    db = session.ctx.game_db
    game_id = int(session.ctx.game_id)
    nation_id = int(session.ctx.nation_id)
    prior = _previous_signals(db, game_id)
    retrieval_text = "\n".join(
        part for part in (message, _history_text(history or []), prior) if part
    )
    playbook = _retrieve(
        db, game_id=game_id, nation_id=nation_id, query=retrieval_text)
    turn_summary = _result(session, "get_turn_summary")
    try:
        current_decisions = [
            row for row in decision_history(
                db, game_id, turn=int(session.turn), limit=300)
            if row["is_current_revision"]
        ]
    except sqlite3.OperationalError:
        current_decisions = _result(session, "get_orders") or []
    scratch = _scratchpad(db, game_id)
    rendered = _render(turn_summary, current_decisions, scratch, playbook)
    bundle = ContextBundle(
        rendered=rendered,
        retrieval_text=retrieval_text,
        turn_summary=turn_summary,
        current_decisions=current_decisions,
        scratchpad=scratch,
        playbook=playbook,
    )
    if not record:
        return bundle
    cur = db.execute(
        "INSERT INTO agent_context_run(game_id,turn,user_message,retrieval_text,"
        "injected_context,injected_entry_ids) VALUES(?,?,?,?,?,?)",
        (game_id, int(session.turn), message, retrieval_text, rendered,
         json.dumps([row.id for row in playbook])),
    )
    bundle.run_id = int(cur.lastrowid)
    db.executemany(
        "INSERT INTO context_injection_log(context_run_id,playbook_entry_id,"
        "score,matched_triggers) VALUES(?,?,?,?)",
        [
            (bundle.run_id, row.id, row.score,
             json.dumps(list(row.matched_triggers)))
            for row in playbook
        ],
    )
    db.commit()
    return bundle


def complete_context_run(
    db: sqlite3.Connection,
    run_id: int | None,
    *,
    visible_response: str,
    emitted_signals: str,
    tool_names: Iterable[str],
) -> None:
    if run_id is None:
        return
    db.execute(
        "UPDATE agent_context_run SET visible_response=?, emitted_signals=?, "
        "tool_names_json=?, completed_at=datetime('now') WHERE id=?",
        (visible_response, emitted_signals, json.dumps(list(tool_names)), run_id),
    )
    db.commit()
