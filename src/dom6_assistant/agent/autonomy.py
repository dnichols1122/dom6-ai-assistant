"""Bounded autonomous play of one explicitly bound player view.

The coherent file watcher is the clock.  One settled `.trn`/`.2h`/`ftherlnd`
state starts a bounded series of model invocations; model silence is never a
success condition.  Only a valid completion handshake followed by a submitted
`.2h` ends the run.
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dom6_assistant.agent import profiles
from dom6_assistant.agent.llm import OpenAICompatClient, client_from_config
from dom6_assistant.agent.loop import Event, TurnAgent
from dom6_assistant.agent.session import (
    DEFAULT_GAME_DB,
    SessionError,
    open_session,
    resolve_save_dir,
)
from dom6_assistant.agent.turn_completion import completion_status
from dom6_assistant.file_reader.turn_snapshot import (
    SnapshotNotReady,
    SnapshotResult,
    read_turn_files,
    watch_turns,
)
from dom6_assistant.gamestate.ingest import connect, ingest_file


AUTONOMOUS_PROMPT = """\
Play this entire Dominions turn autonomously. This is execution, not advice.

Work through all new messages, battles, diplomacy, strategic threats, economy,
recruitment, research, magic, every commander, troop organization and battle
setup that materially matters. Use the injected context and tools; verify facts
instead of relying on memory. Record a concise evidence-based rationale for
every decision. Give every current commander an explicit strategic order,
including deliberate Defend or Research choices, using record_orders in
batches where possible.

When the plan is complete, call materialize_orders once as a preview. Resolve
every refusal or skipped entry, then call materialize_orders with confirm=true.
Call complete_turn with a concise strategic summary and outstanding risks.
Then call submit_turn with confirm=true as the last action for this revision.
Do not stop at a recommendation, progress report, or prose conclusion: the run
is incomplete until submit_turn succeeds.
"""


RETRY_PROMPT = """\
The previous autonomous invocation stopped before the turn was both completed
and submitted. Continue the same turn from the recorded decisions and
scratchpad. Call get_turn_completion and get_decision_history as needed,
finish every missing decision, preview and confirm materialization, call
complete_turn, then call submit_turn with confirm=true. Do not merely explain
what remains.
"""


@dataclass(frozen=True)
class BoundPlayer:
    profile: str
    game_id: int
    save_name: str
    nation_id: int
    nation_slug: str
    save_dir: Path


@dataclass(frozen=True)
class AutonomousResult:
    profile: str
    turn: int
    complete: bool
    attempts: int
    reason: str


def list_bound_players(game_db: Path = DEFAULT_GAME_DB) -> list[BoundPlayer]:
    db = sqlite3.connect(game_db)
    db.row_factory = sqlite3.Row
    try:
        out = []
        for row in db.execute(
            "SELECT id,name,save_name,nation_id,nation_slug FROM games ORDER BY id"
        ):
            if row["nation_id"] is None or not row["nation_slug"]:
                continue
            save_name = row["save_name"] or row["name"].split("::", 1)[0]
            try:
                save_dir = resolve_save_dir(save_name)
            except SessionError:
                continue
            out.append(BoundPlayer(
                profile=row["name"], game_id=int(row["id"]),
                save_name=save_name, nation_id=int(row["nation_id"]),
                nation_slug=row["nation_slug"], save_dir=save_dir,
            ))
        return out
    finally:
        db.close()


def resolve_bound_player(
    profile: str, game_db: Path = DEFAULT_GAME_DB
) -> BoundPlayer:
    matches = {row.profile: row for row in list_bound_players(game_db)}
    if profile not in matches:
        raise ValueError(
            f"no live player profile {profile!r}. Available: {sorted(matches)}"
        )
    return matches[profile]


@contextmanager
def player_lock(bound: BoundPlayer) -> Iterator[None]:
    path = bound.save_dir / f".dom6-assistant-{bound.nation_slug}.autonomy.lock"
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another autonomous supervisor already holds {path}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _start_attempt(
    db: sqlite3.Connection, bound: BoundPlayer, turn: int,
    attempt: int, prompt: str,
) -> int:
    cur = db.execute(
        "INSERT INTO autonomous_turn_run(game_id,turn,attempt,status,prompt) "
        "VALUES(?,?,?,'running',?) ON CONFLICT(game_id,turn,attempt) DO UPDATE "
        "SET status='running',prompt=excluded.prompt,final_text=NULL,error=NULL,"
        "started_at=datetime('now'),finished_at=NULL",
        (bound.game_id, turn, attempt, prompt),
    )
    db.commit()
    if cur.lastrowid:
        return int(cur.lastrowid)
    row = db.execute(
        "SELECT id FROM autonomous_turn_run WHERE game_id=? AND turn=? AND attempt=?",
        (bound.game_id, turn, attempt),
    ).fetchone()
    return int(row["id"])


def _finish_attempt(
    db: sqlite3.Connection, run_id: int, status: str,
    final_text: str = "", error: str = "",
) -> None:
    db.execute(
        "UPDATE autonomous_turn_run SET status=?,final_text=?,error=?,"
        "finished_at=datetime('now') WHERE id=?",
        (status, final_text or None, error or None, run_id),
    )
    db.commit()


def run_autonomous_turn(
    bound: BoundPlayer,
    *,
    game_db: Path = DEFAULT_GAME_DB,
    client: OpenAICompatClient | None = None,
    max_attempts: int = 3,
    max_steps: int = 96,
    on_event: Callable[[int, Event], None] | None = None,
) -> AutonomousResult:
    if max_attempts < 1 or max_steps < 1:
        raise ValueError("max_attempts and max_steps must be positive")
    trn_path = bound.save_dir / f"{bound.nation_slug}.trn"
    ingest_db = connect(game_db)
    try:
        ingest_file(ingest_db, trn_path)
    finally:
        ingest_db.close()

    model = client or client_from_config()
    last_turn = 0
    last_reason = "no autonomous attempt ran"
    for attempt in range(1, max_attempts + 1):
        session = open_session(
            bound.profile, game_db=game_db, save_dir=bound.save_dir,
            nation_id=bound.nation_id,
        )
        try:
            last_turn = session.turn
            status = completion_status(
                session.ctx.game_db, session.ctx.game_id, session.turn,
                session.ctx.h2_path,
            )
            if status.complete and status.submitted:
                return AutonomousResult(
                    bound.profile, session.turn, True, attempt - 1, status.reason)
            prompt = AUTONOMOUS_PROMPT if attempt == 1 else RETRY_PROMPT
            run_id = _start_attempt(
                session.ctx.game_db, bound, session.turn, attempt, prompt)
            agent = TurnAgent(
                session, model, max_steps=max_steps, allow_writes=True,
                profile=profiles.active_profile(session.ctx.game_db),
            )
            final: list[str] = []
            errors: list[str] = []
            try:
                for event in agent.run(prompt):
                    if event.kind == "done" and event.text:
                        final.append(event.text)
                    if event.kind == "error" and event.text:
                        errors.append(event.text)
                    if on_event:
                        on_event(attempt, event)
            except Exception as exc:
                _finish_attempt(
                    session.ctx.game_db, run_id, "error",
                    final_text="\n".join(final), error=str(exc))
                if attempt == max_attempts:
                    return AutonomousResult(
                        bound.profile, session.turn, False, attempt, str(exc))
                continue
            status = completion_status(
                session.ctx.game_db, session.ctx.game_id, session.turn,
                session.ctx.h2_path,
            )
            _finish_attempt(
                session.ctx.game_db, run_id,
                ("completed" if status.complete and status.submitted
                 else "incomplete"),
                final_text="\n".join(final), error="\n".join(errors),
            )
            if status.complete and status.submitted:
                return AutonomousResult(
                    bound.profile, session.turn, True, attempt, status.reason)
            last_reason = (
                status.reason if not status.complete
                else "the turn is ready but submit_turn has not succeeded"
            )
        finally:
            session.close()
    return AutonomousResult(
        bound.profile, last_turn, False, max_attempts, last_reason)


def watch_autonomous_turns(
    bound: BoundPlayer,
    *,
    snapshot_root: Path,
    game_db: Path = DEFAULT_GAME_DB,
    poll_seconds: float = 0.5,
    settle_seconds: float = 1.0,
    skip_current: bool = False,
    once: bool = False,
    max_attempts: int = 3,
    max_steps: int = 96,
    on_snapshot: Callable[[SnapshotResult], None] | None = None,
    on_event: Callable[[int, Event], None] | None = None,
) -> Iterator[AutonomousResult]:
    skipped = None
    if skip_current:
        skipped = read_turn_files(bound.save_dir, (bound.nation_slug,))
    handled_turns: set[int] = set()
    with player_lock(bound):
        for snapshot in watch_turns(
            bound.save_dir, snapshot_root,
            nation_stem=(bound.nation_slug,),
            skip_fingerprint=skipped.fingerprint if skipped else None,
            poll_seconds=poll_seconds, settle_seconds=settle_seconds,
        ):
            if on_snapshot:
                on_snapshot(snapshot)
            if snapshot.turn in handled_turns:
                continue
            result = run_autonomous_turn(
                bound, game_db=game_db, max_attempts=max_attempts,
                max_steps=max_steps, on_event=on_event)
            yield result
            # Avoid an unbounded same-turn loop caused by our own .2h write.
            # A same-turn player intervention requires an explicit restart,
            # which is safer than silently replacing it.
            handled_turns.add(snapshot.turn)
            if once:
                return
