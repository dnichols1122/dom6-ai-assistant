"""SQLite-backed game journal."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_DB_PATH = Path.home() / ".config" / "dom6-assistant" / "journal.db"

_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS turns (
    turn_number     INTEGER PRIMARY KEY,
    treasury_start  INTEGER,
    treasury_end    INTEGER,
    total_income    INTEGER,
    upkeep          INTEGER,
    recorded_at     TEXT DEFAULT (datetime('now'))
);

-- One row per turn. NULL = not observed that turn (different from 0).
-- Gem type order confirmed by memory scanning + journal cross-reference:
--   idx0=fire, idx1=water, idx2=nature, idx3=?, idx4=?, idx5=astral, idx6=air, idx7=?
-- Remaining unknowns (idx 3,4,7) are Earth, Death, Blood — order TBD via cross-ref.
CREATE TABLE IF NOT EXISTS gem_snapshots (
    turn_number  INTEGER PRIMARY KEY REFERENCES turns(turn_number) ON DELETE CASCADE,
    fire         INTEGER,
    water        INTEGER,
    nature       INTEGER,
    earth        INTEGER,
    death        INTEGER,
    astral       INTEGER,
    air          INTEGER,
    blood        INTEGER
);

CREATE TABLE IF NOT EXISTS provinces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    UNIQUE NOT NULL,
    is_capital      INTEGER NOT NULL DEFAULT 0,
    province_number INTEGER   -- in-game province number (not shown in UI, for future memory mapping)
);

CREATE TABLE IF NOT EXISTS province_snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    province_id          INTEGER NOT NULL REFERENCES provinces(id) ON DELETE CASCADE,
    turn_number          INTEGER NOT NULL,
    terrain              TEXT,
    population           INTEGER,
    income               INTEGER,
    resources            INTEGER,
    recruitment_points   INTEGER,
    recruitment_per_turn INTEGER,
    supplies             INTEGER,
    supply_usage         INTEGER,
    defense              INTEGER,
    unrest               INTEGER,
    dominion_strength    INTEGER,
    scale_order          INTEGER,
    scale_productivity   INTEGER,
    scale_heat           INTEGER,
    scale_growth         INTEGER,
    scale_luck           INTEGER,
    scale_magic          INTEGER,
    UNIQUE(province_id, turn_number)
);

-- Legacy raw memory samples at F10/F14/F20. Those offsets are now proved as
-- Earth/Astral/Glamour; the historical column names are retained for DB
-- compatibility and are no longer used to infer a mapping.
CREATE TABLE IF NOT EXISTS memory_snapshots (
    turn_number  INTEGER PRIMARY KEY REFERENCES turns(turn_number) ON DELETE CASCADE,
    gem_slot_3   INTEGER,
    gem_slot_4   INTEGER,
    gem_slot_7   INTEGER,
    captured_at  TEXT DEFAULT (datetime('now'))
);

-- Candidate memory addresses for global named fields.
-- candidates: JSON array of integer addresses.
-- locked_addr: set when narrowed to exactly 1 (or manually overridden).
-- manual_override: 1 = user set locked_addr manually (skip auto-narrowing).
CREATE TABLE IF NOT EXISTS field_correlations (
    field_name      TEXT PRIMARY KEY,
    candidates      TEXT NOT NULL DEFAULT '[]',
    locked_addr     INTEGER,
    manual_override INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- Sites/buildings are per-province, not per-turn (rarely change).
-- Replaced wholesale when updated.
CREATE TABLE IF NOT EXISTS province_sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    province_id INTEGER NOT NULL REFERENCES provinces(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    site_type   TEXT,   -- 'building' | 'site' | null
    UNIQUE(province_id, name)
);
"""


def get_db() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with row_factory set."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    """Create tables if they don't exist yet, and run any pending migrations."""
    con = get_db()
    with con:
        con.executescript(_SCHEMA)
        _migrate(con)
    con.close()


def _migrate(con: sqlite3.Connection) -> None:
    """Idempotent schema migrations for existing databases."""
    turns_cols = {row[1] for row in con.execute("PRAGMA table_info(turns)")}
    # v2: split treasury into start/end
    if "treasury_start" not in turns_cols:
        con.execute("ALTER TABLE turns ADD COLUMN treasury_start INTEGER")
    if "treasury_end" not in turns_cols:
        con.execute("ALTER TABLE turns ADD COLUMN treasury_end INTEGER")
        if "treasury" in turns_cols:
            con.execute("UPDATE turns SET treasury_end = treasury")

    gems_cols = {row[1] for row in con.execute("PRAGMA table_info(gem_snapshots)")}
    # v3: rename slot_1→water, slot_2→nature now that gem types are confirmed
    if "slot_1" in gems_cols:
        con.execute("ALTER TABLE gem_snapshots RENAME COLUMN slot_1 TO water")
    if "slot_2" in gems_cols:
        con.execute("ALTER TABLE gem_snapshots RENAME COLUMN slot_2 TO nature")
    # v4: rename slot_3/4/7 → earth/death/blood (labels; memory mapping TBD)
    #     also drop NOT NULL constraint by recreating table if old schema still has DEFAULT 0
    if "slot_3" in gems_cols:
        con.execute("ALTER TABLE gem_snapshots RENAME COLUMN slot_3 TO earth")
    if "slot_4" in gems_cols:
        con.execute("ALTER TABLE gem_snapshots RENAME COLUMN slot_4 TO death")
    if "slot_7" in gems_cols:
        con.execute("ALTER TABLE gem_snapshots RENAME COLUMN slot_7 TO blood")

    prov_cols = {row[1] for row in con.execute("PRAGMA table_info(provinces)")}
    # v3: add province_number column
    if "province_number" not in prov_cols:
        con.execute("ALTER TABLE provinces ADD COLUMN province_number INTEGER")

    # v5: field_correlations manual_override column (table may predate this column)
    fc_cols = {row[1] for row in con.execute("PRAGMA table_info(field_correlations)")}
    if fc_cols and "manual_override" not in fc_cols:
        con.execute("ALTER TABLE field_correlations ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0")
