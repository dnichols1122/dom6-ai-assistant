"""Assembling a working tool surface: databases, save files, and the view.

One place that knows where everything lives, so the CLI and the web app cannot
drift into pointing at different saves — which would be hard to notice and
would produce an assistant confidently reasoning about last week's turn.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from dom6_assistant.agent.registry import ToolContext, ToolRegistry
from dom6_assistant.agent.visibility import PlayerView
from dom6_assistant.agent.write_tools import register_all

DEFAULT_GAME_DB = Path("knowledge/game.sqlite3")
DEFAULT_REFERENCE_DB = Path("knowledge/reference/reference.sqlite3")
from dom6_assistant.paths import default_save_root

DEFAULT_SAVE_ROOT = default_save_root()


class SessionError(RuntimeError):
    """Setup failed in a way the user has to fix — a missing save, usually."""


@dataclass
class Session:
    ctx: ToolContext
    registry: ToolRegistry

    @property
    def turn(self) -> int:
        return self.ctx.turn

    def call(self, name: str, args: dict | None = None):
        return self.registry.call(self.ctx, name, args)

    def close(self) -> None:
        self.ctx.game_db.close()
        self.ctx.reference_db.close()


def _connect(path: Path, what: str) -> sqlite3.Connection:
    if not path.exists():
        raise SessionError(f"{what} not found at {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def resolve_save_dir(game_name: str,
                     save_root: Path | None = None,
                     recorded: str | Path | None = None) -> Path:
    """Find the live save directory for a game.

    The folder name and the game's own name are not the same thing. A
    server-hosted game writes its internal name into the `.trn` -- so a folder
    called ``MYGAME`` holds a game named ``srvgame_MYGAME`` -- and deriving the
    path from the name alone misses every one of them.

    Ingest already recorded the directory it read, so *recorded* is preferred,
    but only when it lies inside the live save root. Ingest also walks the
    snapshot corpus, and playing out of an archive would write this turn's
    orders into a frozen copy of an old one.
    """
    root = save_root or DEFAULT_SAVE_ROOT
    if recorded:
        candidate = Path(recorded)
        try:
            inside = candidate.resolve().is_relative_to(root.resolve())
        except (OSError, ValueError):
            inside = False
        if inside and candidate.is_dir():
            return candidate

    path = root / game_name
    if not path.is_dir():
        available = []
        try:
            available = sorted(p.name for p in root.iterdir() if p.is_dir())[:10]
        except OSError:
            pass
        raise SessionError(
            f"no save directory {path}. Available: {available}")
    return path


def open_session(game_name: str | None = None,
                 *,
                 game_db: Path | None = None,
                 reference_db: Path | None = None,
                 save_dir: Path | None = None,
                 nation_id: int | None = None) -> Session:
    """Open a session against the live save.

    `game_name` selects one player-view row in `games`. ``save_name`` supplies
    the actual live save directory: multi-human rows have distinct profile
    names such as ``example_game::mid_ermor`` but share ``example_game``.
    The stored `save_dir` is deliberately not trusted for play because it can
    point at an ingested snapshot rather than the live campaign.
    """
    gdb = _connect(game_db or DEFAULT_GAME_DB, "game database")
    # `CREATE ... IF NOT EXISTS` is also the migration mechanism for additive
    # intent tables.  Sessions often open a long-lived game.sqlite3 directly,
    # without another ingest first, so relying on ingest.connect() to apply a
    # newly-added table leaves the corresponding tool broken until the next
    # turn happens to be imported.
    from dom6_assistant.gamestate.ingest import SCHEMA, ensure_schema_columns
    gdb.executescript(SCHEMA.read_text(encoding="utf-8"))
    ensure_schema_columns(gdb)
    rdb = _connect(reference_db or DEFAULT_REFERENCE_DB, "reference database")

    if game_name:
        row = gdb.execute("SELECT * FROM games WHERE name=?",
                          (game_name,)).fetchone()
    else:
        row = gdb.execute("SELECT * FROM games ORDER BY id LIMIT 1").fetchone()
    if row is None:
        names = [r["name"] for r in gdb.execute("SELECT name FROM games")]
        raise SessionError(
            f"no game {game_name!r} in the database. Known: {names}")

    live_save_name = row["save_name"] or row["name"]
    recorded = row["save_dir"] if "save_dir" in row.keys() else None
    directory = save_dir or resolve_save_dir(live_save_name, recorded=recorded)
    nation = nation_id if nation_id is not None else row["nation_id"]
    if nation is None:
        raise SessionError(
            f"game {row['name']} has no nation_id recorded; pass nation_id "
            "explicitly so the visibility filter has a whitelist to apply")

    trn_name = (
        f"{row['nation_slug']}.trn" if row["nation_slug"] else None
    )
    try:
        view = PlayerView(directory, nation, trn_name=trn_name)
        view.verify_nation()
    except (FileNotFoundError, ValueError) as exc:
        raise SessionError(
            f"could not open player view {row['name']}: {exc}") from exc

    h2 = view.trn_path.with_suffix(".2h")
    ctx = ToolContext(view=view, game_db=gdb, reference_db=rdb,
                      game_id=row["id"], save_dir=directory,
                      h2_path=h2 if h2.exists() else None)
    # Province wards are deliberately absent from every foreign Buildings
    # list. Persist the one kind of evidence the player does receive so it is
    # not forgotten when this turn's Messages screen disappears next turn.
    from dom6_assistant.agent.province_wards import sync_observations
    province_ids_by_name = {
        province.name: province.province_id
        for province in view.provinces()
        if province.name is not None
    }
    sync_observations(
        gdb, int(row["id"]), ctx.turn, view.turn_messages(),
        province_ids_by_name)
    return Session(ctx=ctx, registry=register_all())
