"""Attach the auto-ingest watcher to a running FastAPI application.

One process-wide watcher, shared by both web entry points. They mount the same
routers against the same database, so two watchers would mean two connections
racing to ingest the same file.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI

from dom6_assistant.gamestate.autoingest import DEFAULT_SAVE_ROOT, AutoIngest

log = logging.getLogger(__name__)

DEFAULT_DATABASE = Path("knowledge/game.sqlite3")

_service: AutoIngest | None = None


def _configured() -> tuple[bool, Path, Path]:
    """Whether to watch, and where. Config first, environment as an override."""
    enabled, root, database = True, DEFAULT_SAVE_ROOT, DEFAULT_DATABASE
    try:
        from dom6_assistant import config as cfg
        section = (cfg.load() or {}).get("ingest") or {}
        enabled = bool(section.get("auto", True))
        if section.get("save_root"):
            root = Path(str(section["save_root"])).expanduser()
        if section.get("database"):
            database = Path(str(section["database"])).expanduser()
    except Exception:                                       # noqa: BLE001
        # A malformed config must not stop the server from starting; the
        # defaults are the ones almost everyone wants anyway.
        log.warning("could not read [ingest] config; using defaults", exc_info=True)

    # Set DOM6_AUTO_INGEST=0 to run the server without touching saves, which is
    # what the tests and the read-only verifier want.
    override = os.environ.get("DOM6_AUTO_INGEST")
    if override is not None:
        enabled = override.strip().lower() not in {"0", "false", "no", "off"}
    return enabled, root, database


def service() -> AutoIngest | None:
    return _service


def start() -> AutoIngest | None:
    global _service
    if _service is not None:
        return _service
    enabled, root, database = _configured()
    if not enabled:
        log.info("auto-ingest disabled")
        return None
    _service = AutoIngest(database=database, root=root)
    _service.start()
    return _service


def stop() -> None:
    global _service
    if _service is not None:
        _service.stop()
        _service = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    start()
    try:
        yield
    finally:
        stop()


router = APIRouter()


@router.get("/ingest/status")
def ingest_status() -> dict[str, Any]:
    """What the watcher has done, for the page header."""
    current = service()
    if current is None:
        enabled, root, _ = _configured()
        return {"running": False, "enabled": enabled, "root": str(root),
                "watching": [], "ingested": 0, "failed": 0, "recent": []}
    return {"enabled": True, **current.status()}
