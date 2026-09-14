"""Keep the database current while the assistant is running.

Ingesting a turn used to be a separate command you had to remember between
finishing a turn in Dominions and asking the assistant about it. Forgetting it
is silent and looks exactly like the assistant being wrong: it answers
confidently from the turn before.

So the server does it. A background thread watches the save root, ingests every
`.trn` once its bytes stop changing, and notices games created after start-up.
Nothing about the parsing changes -- this is the same `watch_save_dirs` the CLI
uses, on a thread, with its own connection because SQLite objects belong to the
thread that made them.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from dom6_assistant.gamestate.ingest import connect, watch_save_dirs
from dom6_assistant.paths import default_save_root

DEFAULT_SAVE_ROOT = default_save_root()
#: Directories the game writes for its own purposes, never a player's game.
SKIP_DIRECTORIES = frozenset({"newlords", "maps", "mods"})

log = logging.getLogger(__name__)


def discover_save_dirs(root: Path) -> list[Path]:
    """Every directory under ``root`` that currently holds a ``.trn``.

    Re-evaluated on every watch cycle, so starting a new game is enough to have
    it ingested; nothing needs restarting.
    """
    if not root.is_dir():
        return []
    found = []
    if any(root.glob("*.trn")):
        found.append(root)
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in SKIP_DIRECTORIES:
            continue
        if any(child.glob("*.trn")):
            found.append(child)
    return found


@dataclass
class AutoIngest:
    """A watcher bound to the lifetime of the running server."""

    database: Path
    root: Path = DEFAULT_SAVE_ROOT
    poll_seconds: float = 1.0
    settle_seconds: float = 1.5

    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _ingested: int = field(default=0, init=False)
    _failed: int = field(default=0, init=False)
    _started_at: float | None = field(default=None, init=False)
    _recent: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=20), init=False)
    _watching: tuple[Path, ...] = field(default=(), init=False)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._run, name="dom6-autoingest", daemon=True)
        self._thread.start()
        log.info("auto-ingest watching %s", self.root)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "root": str(self.root),
                "root_exists": self.root.is_dir(),
                "watching": [str(path) for path in self._watching],
                "ingested": self._ingested,
                "failed": self._failed,
                "started_at": self._started_at,
                "recent": list(self._recent),
            }

    # -- the thread ------------------------------------------------------

    def _run(self) -> None:
        # This connection is created here on purpose: a sqlite3.Connection made
        # on the request thread cannot legally be used from this one.
        try:
            conn = connect(self.database)
        except Exception:                                   # noqa: BLE001
            log.exception("auto-ingest could not open %s", self.database)
            return
        try:
            for batch in watch_save_dirs(
                conn,
                discover_save_dirs(self.root) or [self.root],
                poll_seconds=self.poll_seconds,
                settle_seconds=self.settle_seconds,
                discover=lambda: discover_save_dirs(self.root),
                should_stop=self._stop.is_set,
            ):
                self._record(batch)
        except Exception:                                   # noqa: BLE001
            # A crashed watcher must not take the server with it, and must not
            # fail silently either: the status endpoint keeps reporting
            # running=False, which is visible in the UI.
            log.exception("auto-ingest stopped unexpectedly")
        finally:
            conn.close()

    def _record(self, batch: Iterable[Any]) -> None:
        with self._lock:
            self._watching = tuple(discover_save_dirs(self.root))
            for event in batch:
                if event.error:
                    self._failed += 1
                    entry = {"path": str(event.path), "error": event.error}
                else:
                    self._ingested += 1
                    entry = {"path": str(event.path), "profile": event.profile,
                             "turn": event.turn, "changed": event.changed}
                entry["at"] = time.time()
                self._recent.appendleft(entry)
                if event.error:
                    log.warning("auto-ingest failed on %s: %s", event.path, event.error)
                elif event.changed:
                    log.info("ingested %s turn %s", event.profile, event.turn)
