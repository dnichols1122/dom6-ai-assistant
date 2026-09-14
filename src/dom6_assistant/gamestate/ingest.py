"""Ingest a `.trn` into `game.sqlite3`.

One snapshot per (game, turn), never overwritten, so history accumulates and a
turn-over-turn diff is just a query. Re-ingesting identical bytes is a no-op —
the file's SHA-256 is stored and compared, which matters because the natural
usage is a filesystem watcher firing on every write.

Usage:
    python -m dom6_assistant.gamestate.ingest <path-to.trn> [--db PATH]
    python -m dom6_assistant.gamestate.ingest --scan       # every savedgame
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from ..file_reader.formats import h2 as h2_fmt
from ..file_reader.formats import mapfile as map_fmt
from ..file_reader.formats import trn as trn_fmt
from ..file_reader.formats import units as units_fmt

DEFAULT_DB = Path(__file__).resolve().parents[3] / "knowledge" / "game.sqlite3"
SCHEMA = Path(__file__).parent / "schema.sql"
from dom6_assistant.paths import default_save_root

SAVEDGAMES = default_save_root()


@dataclass(frozen=True)
class IngestWatchEvent:
    path: Path
    profile: str | None
    turn: int | None
    changed: bool
    error: str | None = None


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    ensure_schema_columns(conn)
    return conn


def ensure_schema_columns(conn: sqlite3.Connection) -> None:
    """Apply additive columns that ``CREATE TABLE IF NOT EXISTS`` cannot."""
    game_columns = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
    if "save_name" not in game_columns:
        conn.execute("ALTER TABLE games ADD COLUMN save_name TEXT")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(provinces)")}
    if "land_gold" not in columns:
        conn.execute("ALTER TABLE provinces ADD COLUMN land_gold INTEGER")
    if "administrative_owner" not in columns:
        conn.execute(
            "ALTER TABLE provinces ADD COLUMN administrative_owner INTEGER")
    ritual_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(ritual_intent)")}
    if ritual_columns and "target_commander_id" not in ritual_columns:
        conn.execute(
            "ALTER TABLE ritual_intent ADD COLUMN target_commander_id INTEGER")
    if ritual_columns and "target_unit_instance_id" not in ritual_columns:
        conn.execute(
            "ALTER TABLE ritual_intent ADD COLUMN target_unit_instance_id INTEGER")
    if ritual_columns and "target_item_id" not in ritual_columns:
        conn.execute(
            "ALTER TABLE ritual_intent ADD COLUMN target_item_id INTEGER")
    if ritual_columns and "wish_item_id" not in ritual_columns:
        conn.execute(
            "ALTER TABLE ritual_intent ADD COLUMN wish_item_id INTEGER")
    if ritual_columns and "wish_unit_id" not in ritual_columns:
        conn.execute(
            "ALTER TABLE ritual_intent ADD COLUMN wish_unit_id INTEGER")
    if ritual_columns and "wish_nation_id" not in ritual_columns:
        conn.execute(
            "ALTER TABLE ritual_intent ADD COLUMN wish_nation_id INTEGER")
    if ritual_columns and "wish_result" not in ritual_columns:
        conn.execute(
            "ALTER TABLE ritual_intent ADD COLUMN wish_result TEXT")
    if ritual_columns and "target_global_effect_id" not in ritual_columns:
        conn.execute(
            "ALTER TABLE ritual_intent ADD COLUMN target_global_effect_id INTEGER")
    bid_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(mercenary_bid_intent)")}
    if bid_columns and "quoted_minimum" not in bid_columns:
        conn.execute(
            "ALTER TABLE mercenary_bid_intent ADD COLUMN quoted_minimum INTEGER")
    forge_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(forge_intent)")}
    if forge_columns and "secondary_gem_path" not in forge_columns:
        conn.execute(
            "ALTER TABLE forge_intent ADD COLUMN secondary_gem_path INTEGER")
    if forge_columns and "secondary_gem_cost" not in forge_columns:
        conn.execute(
            "ALTER TABLE forge_intent ADD COLUMN secondary_gem_cost "
            "INTEGER NOT NULL DEFAULT 0")
    troop_columns = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(troop_assignment_intent)")}
    if troop_columns and "destination" not in troop_columns:
        conn.execute(
            "ALTER TABLE troop_assignment_intent ADD COLUMN destination "
            "TEXT NOT NULL DEFAULT 'squad'")
    scratch_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(scratchpad)")}
    if scratch_columns and "status" not in scratch_columns:
        conn.execute(
            "ALTER TABLE scratchpad ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
    if scratch_columns and "pinned" not in scratch_columns:
        conn.execute(
            "ALTER TABLE scratchpad ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    if scratch_columns and "updated_at" not in scratch_columns:
        conn.execute("ALTER TABLE scratchpad ADD COLUMN updated_at TEXT")
        conn.execute(
            "UPDATE scratchpad SET updated_at=created_at WHERE updated_at IS NULL")
    completion_columns = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(agent_turn_completion)")}
    if completion_columns and "submitted_at" not in completion_columns:
        conn.execute(
            "ALTER TABLE agent_turn_completion ADD COLUMN submitted_at TEXT")
    recruit_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(recruit_intent_v2)")}
    if recruit_columns and "kind" not in recruit_columns:
        conn.execute("ALTER TABLE recruit_intent_v2 ADD COLUMN kind TEXT")
    squad_creation_view = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' "
        "AND name='current_squad_creation_intent'"
    ).fetchone()
    if (squad_creation_view is not None
            and "current_troop_assignment_intent" not in
            str(squad_creation_view[0])):
        # A squad creation is a pending final-state operation, not an eternal
        # command. If every troop's latest assignment moves elsewhere, the
        # squad has been implicitly cancelled and must not be recreated empty.
        conn.execute("DROP VIEW current_squad_creation_intent")
        conn.execute(
            "CREATE VIEW current_squad_creation_intent AS "
            "SELECT s.* FROM squad_creation_intent s "
            "JOIN (SELECT game_id, turn, target_commander_id, target_squad, "
            "MAX(id) AS id FROM squad_creation_intent "
            "GROUP BY game_id, turn, target_commander_id, target_squad) l "
            "ON l.id = s.id WHERE EXISTS ("
            "SELECT 1 FROM current_troop_assignment_intent a "
            "WHERE a.game_id=s.game_id AND a.turn=s.turn "
            "AND a.destination='squad' "
            "AND a.target_commander_id=s.target_commander_id "
            "AND a.target_squad=s.target_squad)"
        )


def sync_map_borders(
    conn: sqlite3.Connection,
    game_id: int,
    save_dir: Path,
) -> None:
    """Refresh static borders, translating plane-local to global province ids."""
    conn.execute("DELETE FROM map_borders WHERE game_id=?", (game_id,))
    plane_offset = 0
    for mp in map_fmt.find_for_save(save_dir):
        try:
            parsed = map_fmt.parse(mp)
        except OSError:
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO map_borders(game_id, province_id, "
            "neighbour_id, border_flags) VALUES(?,?,?,?)",
            [(game_id, a + plane_offset, b + plane_offset, flags)
             for (a, b), flags in parsed.borders.items()],
        )
        plane_offset += parsed.province_count


def ingest_file(conn: sqlite3.Connection, path: Path) -> tuple[str, int, bool]:
    """Ingest one player view. Returns (profile_name, turn, changed)."""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    parsed = trn_fmt.parse_bytes(data, nation_name=path.stem)

    # The player's nation: the .trn is written for one nation, named by the file.
    nation_slug = path.stem
    profile_name = player_profile_name(path, parsed.game_name)
    nation_id = _nation_id_for_slug(nation_slug)

    with conn:
        conn.execute(
            "INSERT INTO games(name, save_name, nation_id, nation_slug, save_dir) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET nation_id=COALESCE(excluded.nation_id, nation_id), "
            "save_name=excluded.save_name, nation_slug=excluded.nation_slug, "
            "save_dir=excluded.save_dir",
            (profile_name, parsed.game_name, nation_id, nation_slug,
             str(path.parent)),
        )
        game_id = conn.execute("SELECT id FROM games WHERE name=?",
                               (profile_name,)).fetchone()["id"]
        # Do this before the identical-turn early return. Map files can be
        # corrected independently, and older builds stored underworld-local
        # ids over surface ids.
        sync_map_borders(conn, game_id, path.parent)

        existing = conn.execute(
            "SELECT id, trn_sha256 FROM turns WHERE game_id=? AND turn=?",
            (game_id, parsed.turn)).fetchone()
        if existing and existing["trn_sha256"] == digest:
            return profile_name, parsed.turn, False

        if existing:
            # Same turn, different bytes: the player edited orders. Replace it.
            conn.execute("DELETE FROM turns WHERE id=?", (existing["id"],))
        cur = conn.execute(
            "INSERT INTO turns(game_id, turn, trn_sha256, trn_bytes) VALUES(?,?,?,?)",
            (game_id, parsed.turn, digest, len(data)))
        turn_id = cur.lastrowid

        g = parsed.player_gems
        conn.execute(
            "INSERT INTO nation_state(turn_id, gems_fire, gems_air, gems_water, "
            "gems_earth, gems_astral, gems_death, gems_nature, gems_glamour, "
            "gems_blood, gold) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (turn_id, *( (g.fire, g.air, g.water, g.earth, g.astral, g.death,
                          g.nature, g.glamour, g.blood) if g else (None,)*9 ),
             parsed.player_gold))

        conn.executemany(
            "INSERT OR REPLACE INTO provinces(turn_id, province_id, name, name2, "
            "owner_nation_id, is_capital, population, unrest, land_gold, "
            "administrative_owner, "
            "province_defense, dominion_owner, dominion_strength, "
            "terrain_flags, commanders_queued, troops_queued, wall_integrity, "
            "raw_b32, raw_b51, fort_type, has_laboratory, has_temple, "
            "order_scale, productivity_scale, "
            "heat_scale, growth_scale, luck_scale, magic_scale) "
            "VALUES(" + ",".join("?" * 28) + ")",
            [(turn_id, p.province_id, p.name, p.name2, p.owner_nation_id,
              int(p.is_capital), p.population, p.unrest,
              p.land_gold, p.administrative_owner,
              p.province_defense, p.dominion_owner,
              p.dominion_strength, p.terrain_flags,
              p.commanders_queued, p.troops_queued,
              getattr(p, "wall_integrity", None), p.raw_b32, p.raw_b51,
              p.fort_type, p.has_laboratory, p.has_temple,
              p.order_scale, p.productivity_scale, p.heat_scale,
              p.growth_scale, p.luck_scale, p.magic_scale)
             for p in parsed.provinces])

        conn.executemany(
            "INSERT OR REPLACE INTO province_sites(turn_id, province_id, slot, "
            "site_id) VALUES(?,?,?,?)",
            [(turn_id, p.province_id, i, sid)
             for p in parsed.provinces for i, sid in enumerate(p.sites)])

        conn.executemany(
            "INSERT OR REPLACE INTO nation_roster(turn_id, nation_id, gold) "
            "VALUES(?,?,?)",
            [(turn_id, nid, gold) for nid, gold, _g, _h in parsed.roster])

        conn.executemany(
            "INSERT OR REPLACE INTO province_links(turn_id, province_id, "
            "neighbour_id) VALUES(?,?,?)",
            [(turn_id, p.province_id, n)
             for p in parsed.provinces for n in p.neighbours])

        # Unit costs from the matching .2h, if the player has saved orders.
        # These are the only authoritative costs available — see the table's
        # comment in schema.sql for why the reference data cannot supply them.
        h2_path = path.with_suffix(".2h")
        if h2_path.exists():
            try:
                orders = h2_fmt.parse(h2_path)
            except (OSError, ValueError):
                orders = None
            if orders:
                src = f"2h:{profile_name}:t{parsed.turn}"
                conn.executemany(
                    "INSERT INTO observed_unit_costs(unit_type_id, gold, source) "
                    "VALUES(?,?,?) ON CONFLICT(unit_type_id) DO UPDATE SET "
                    "gold=excluded.gold, source=excluded.source",
                    [(r.unit_type_id, r.gold, src) for r in orders.recruits])

        # Units are filtered to the player's nation: an unfiltered scan returns
        # hundreds of false positives across phantom nation ids, while the
        # filtered result matched the fixture army exactly.
        found = units_fmt.find_units(data, nation_id=nation_id)
        conn.executemany(
            "INSERT OR REPLACE INTO units(turn_id, file_offset, instance_id, "
            "type_id, nation_id, hp, age, experience, kills, afflictions, "
            "squad_id, province_id, home_province, is_mount, has_fought, "
            "is_pretender) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(turn_id, u.offset, u.instance_id, u.type_id, u.nation_id, u.hp,
              u.age, u.experience, u.kills, u.afflictions, u.squad_id,
              struct.unpack_from("<H", data, u.offset + 4)[0],
              struct.unpack_from("<H", data, u.offset + 6)[0],
              int(u.is_mount), int(u.has_fought),
              int(u.is_pretender))
             for u in found])

    return profile_name, parsed.turn, True


#: Nations resolved this run. The lookup is per .trn and a scan reads hundreds.
_NATION_ID_CACHE: dict[str, int | None] = {}


def _nation_id_for_slug(nation_slug: str,
                        reference_db: Path | None = None,
                        legacy_json: Path | None = None) -> int | None:
    """Map a `.trn` filename stem such as ``early_niefelheim`` to a nation id.

    This used to read ``knowledge/reference/nations.json``, which nothing in
    the documented setup ever creates -- ``build_db --refresh`` writes
    ``reference.sqlite3``. Every lookup therefore raised OSError, and the
    surrounding ``except: pass`` turned that into a NULL nation_id on every
    row. The result was a database that looks fully ingested and that
    `open_session` refuses to open, because without a nation id the visibility
    filter has no whitelist to apply.

    Returns None when the reference data is genuinely not built yet, which is
    the one case the caller can do nothing about.
    """
    if nation_slug in _NATION_ID_CACHE:
        return _NATION_ID_CACHE[nation_slug]

    found: int | None = None
    path = reference_db or Path("knowledge/reference/reference.sqlite3")
    if path.exists():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT id FROM nations WHERE file_name_base=?",
                    (nation_slug,)).fetchone()
                if row is None:
                    # Older scrapes lack file_name_base for some nations, so
                    # fall back to era + squashed name: "early_niefelheim"
                    # against era 1 and "niefelheim".
                    era = {"early": 1, "mid": 2, "late": 3}.get(
                        nation_slug.split("_")[0])
                    key = nation_slug.split("_", 1)[-1].replace("_", "").lower()
                    for candidate in conn.execute(
                            "SELECT id,name FROM nations WHERE era=?", (era,)):
                        name = candidate[1].lower().replace("'", "").replace(" ", "")
                        if name == key:
                            found = candidate[0]
                            break
                else:
                    found = row[0]
            finally:
                conn.close()
        except sqlite3.Error:
            found = None

    if found is None:
        # Kept as a fallback so an older tree that does have the JSON still
        # resolves, rather than regressing anyone mid-upgrade.
        try:
            import json
            legacy = legacy_json or (
                Path(__file__).resolve().parents[3] / "knowledge"
                / "reference" / "nations.json")
            for row in json.loads(legacy.read_text(encoding="utf-8")):
                if row.get("file_name_base") == nation_slug:
                    found = row["id"]
                    break
        except (OSError, ValueError):
            pass

    _NATION_ID_CACHE[nation_slug] = found
    return found


def player_profile_name(path: Path, raw_game_name: str) -> str:
    """Stable database key for one player's private view of a save.

    A single-human campaign keeps its historical key. If the directory has
    multiple human ``.trn`` files, each gets an independent profile while both
    retain ``raw_game_name`` as the live save-directory name.
    """
    human_views = list(path.parent.glob("*.trn"))
    if len(human_views) <= 1:
        return raw_game_name
    return f"{raw_game_name}::{path.stem}"


def watch_save_dirs(
    conn: sqlite3.Connection,
    save_dirs: Sequence[Path],
    *,
    poll_seconds: float = 0.5,
    settle_seconds: float = 1.0,
    discover: Callable[[], Sequence[Path]] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[tuple[IngestWatchEvent, ...]]:
    """Ingest every stable new or changed ``.trn`` in selected save folders.

    Existing files are included in the first batch. A file signature must stay
    unchanged for ``settle_seconds`` before it is read, avoiding the partial
    writes produced while Dominions is advancing a turn. Failed signatures are
    reported once and retried when the file changes again.

    ``discover`` is re-consulted every cycle when supplied, so a game created
    after the watcher started is picked up without a restart. ``should_stop``
    lets a caller end the loop between cycles; without it the generator runs
    until it is closed, which is what the CLI wants and a service does not.
    """
    if poll_seconds <= 0 or settle_seconds < 0:
        raise ValueError("poll must be positive and settle must not be negative")
    directories = tuple(dict.fromkeys(Path(path).resolve() for path in save_dirs))
    if not directories:
        raise ValueError("at least one save directory is required")

    # path -> ((size, mtime_ns), first time that signature was observed)
    observed: dict[Path, tuple[tuple[int, int], float]] = {}
    attempted: dict[Path, tuple[int, int]] = {}
    while True:
        if should_stop is not None and should_stop():
            return
        if discover is not None:
            found = tuple(dict.fromkeys(Path(p).resolve() for p in discover()))
            if found != directories:
                # A game directory appeared or went away. Forget signatures for
                # paths no longer watched; keep the rest so an existing game is
                # not re-ingested just because a new one was created.
                directories = found
                keep = set(directories)
                for path in list(observed):
                    if path.parent not in keep:
                        observed.pop(path, None)
                        attempted.pop(path, None)
        candidates = {
            path.resolve()
            for directory in directories
            for path in directory.glob("*.trn")
            if path.is_file()
        }
        for missing in set(observed) - candidates:
            observed.pop(missing, None)
            attempted.pop(missing, None)

        now = time.monotonic()
        ready: list[tuple[Path, tuple[int, int]]] = []
        for path in sorted(candidates):
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            previous = observed.get(path)
            if previous is None or previous[0] != signature:
                observed[path] = (signature, now)
                continue
            if attempted.get(path) == signature:
                continue
            if now - previous[1] >= settle_seconds:
                ready.append((path, signature))

        if ready:
            events: list[IngestWatchEvent] = []
            for path, signature in ready:
                # Record the attempted signature before parsing. If the file is
                # incomplete, a later write changes its signature and retries;
                # a permanently invalid file does not flood the terminal.
                attempted[path] = signature
                try:
                    profile, turn, changed = ingest_file(conn, path)
                except Exception as exc:  # noqa: BLE001 - watcher must survive one bad save
                    events.append(IngestWatchEvent(
                        path=path, profile=None, turn=None, changed=False,
                        error=str(exc),
                    ))
                else:
                    events.append(IngestWatchEvent(
                        path=path, profile=profile, turn=turn, changed=changed,
                    ))
            yield tuple(events)
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("trn", nargs="?", type=Path)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--scan", action="store_true",
                    help="ingest every .trn under ~/.dominions6/savedgames")
    args = ap.parse_args(argv)

    paths: list[Path]
    if args.scan:
        paths = sorted(SAVEDGAMES.glob("*/*.trn"))
    elif args.trn:
        paths = [args.trn]
    else:
        ap.error("give a .trn path or --scan")

    conn = connect(args.db)
    new = same = failed = 0
    try:
        for p in paths:
            try:
                name, turn, changed = ingest_file(conn, p)
            except Exception as exc:                      # noqa: BLE001
                print(f"  ! {p.name}: {exc}", file=sys.stderr)
                failed += 1
                continue
            if changed:
                new += 1
                print(f"  + {name} turn {turn}")
            else:
                same += 1
    finally:
        conn.close()
    print(f"  {new} ingested, {same} unchanged, {failed} failed -> {args.db}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
