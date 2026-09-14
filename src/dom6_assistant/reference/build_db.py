"""Build `reference.sqlite3` — the offline game-data database.

Why this exists: the .trn stores units as type ids (a Pikeneer is 221), so every
parsed file is a pile of numbers until something can turn 221 into "Pikeneer,
20 gold, 10 hp, pike". This is that something, and it is offline so the
assistant never has to recall a stat or consult a website mid-game.

Sources, in order of authority:

  BinaryUnitCosts.csv
                     exact resource and recruitment-point results extracted
                     from the live 6.35 executable's own calculators
  BinaryUnitResourceBonuses.csv
                     exact Resource Bonus, Mining Resource Bonus and Ice
                     Forging values plus Counts as Sun, extracted from the
                     6.35 definition table
  gamedata/*.csv     dom6inspector (larzm42/dom6inspector), tab-separated data
                     extracted from the game itself — units, items, spells,
                     weapons, armours, sites, nations, mercenaries, afflictions
  blesses.json       transcribed from illwiki; the ONE hand-copied source, and
                     the only one covering blesses at all
  unit_descriptions.json
                     the in-game description shown on a unit's card. The text
                     is taken from the 6.36 binary; dom6inspector's
                     gamedata/unitdescr/NNNN.txt supplies only the id->unit
                     mapping, because four of its files carry stale wording
                     the game has since corrected
  spells/events/
  nations.json       from the game binary's undocumented --list* switches

SQLite rather than Postgres on purpose: single user, single machine, a few
thousand rows. The file is the database, so it is trivially backed up, copied
to the VPS, and diffed. Postgres would add a daemon, credentials and a backup
story in exchange for concurrency nobody needs here.

Usage:
    python -m dom6_assistant.reference.build_db [--refresh] [--out PATH]

    --refresh   re-download the CSVs from GitHub before building
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

# .../src/dom6_assistant/reference/build_db.py -> parents[3] is the project root
REF_DIR = Path(__file__).resolve().parents[3] / "knowledge" / "reference"

GAMEDATA_DIR = REF_DIR / "gamedata"
DB_PATH = REF_DIR / "reference.sqlite3"

GITHUB_API = "https://api.github.com/repos/larzm42/dom6inspector/contents/gamedata"
RAW_BASE = "https://raw.githubusercontent.com/larzm42/dom6inspector/main/gamedata"

# Tables worth an index on these columns, where present.
INDEXED_COLUMNS = ("id", "name", "nation", "unit_id", "spell_id", "item_id")


def refresh_csvs(dest: Path) -> int:
    """Re-download the dom6inspector CSVs. Returns the number fetched."""
    dest.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(GITHUB_API, timeout=60) as resp:
        listing = json.load(resp)
    names = [r["name"] for r in listing if r["name"].endswith(".csv")]
    # gamedata/unitdescr/ is a directory of per-id .txt files, not a CSV, and
    # was silently skipped by this filter for as long as it has existed. That
    # is why unit descriptions were absent from the reference database.
    got = 0
    for name in names:
        try:
            with urllib.request.urlopen(f"{RAW_BASE}/{name}", timeout=120) as resp:
                (dest / name).write_bytes(resp.read())
            got += 1
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"  ! {name}: {exc}", file=sys.stderr)
    return got


def _coerce(value: str):
    """Empty -> NULL, integral -> int, decimal -> float, else the string.

    Keeping numbers as numbers matters: these columns get compared and summed
    ("units under 15 gold", "total upkeep"), and a text column silently sorts
    "100" before "20".
    """
    v = value.strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _table_name(csv_path: Path) -> str:
    """BaseU.csv -> units, BaseI.csv -> items; otherwise the stem, lowercased."""
    special = {
        "baseu": "units",
        "basei": "items",
        "magicsites": "magic_sites",
        "binaryunitcosts": "binary_unit_costs",
        "binaryunitresourcebonuses": "binary_unit_resource_bonuses",
    }
    stem = csv_path.stem.lower()
    return special.get(stem, stem)


def load_csv(conn: sqlite3.Connection, path: Path) -> tuple[str, int]:
    """Load one tab-separated file into its own table. Returns (table, rows)."""
    table = _table_name(path)
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            return table, 0
        # Duplicate and blank column names occur; make them unique and safe.
        cols, seen = [], {}
        for i, raw in enumerate(header):
            name = "".join(c if c.isalnum() or c == "_" else "_"
                           for c in raw.strip()) or f"col{i}"
            if name[0].isdigit():
                name = f"c_{name}"
            seen[name] = seen.get(name, 0) + 1
            cols.append(name if seen[name] == 1 else f"{name}_{seen[name]}")

        quoted = ", ".join(f'"{c}"' for c in cols)
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute(f'CREATE TABLE "{table}" ({quoted})')
        placeholders = ",".join("?" * len(cols))
        rows = 0
        batch = []
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            row = (row + [""] * len(cols))[:len(cols)]
            batch.append([_coerce(c) for c in row])
            if len(batch) >= 1000:
                conn.executemany(
                    f'INSERT INTO "{table}" VALUES ({placeholders})', batch)
                rows += len(batch)
                batch = []
        if batch:
            conn.executemany(
                f'INSERT INTO "{table}" VALUES ({placeholders})', batch)
            rows += len(batch)

    for col in INDEXED_COLUMNS:
        if col in cols:
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS "idx_{table}_{col}" '
                f'ON "{table}"("{col}")')
    return table, rows


def load_json_table(conn: sqlite3.Connection, path: Path, table: str) -> int:
    """Load one of our own extracted JSON files (list of flat dicts)."""
    if not path.is_file():
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not rows:
        return 0
    cols = list(rows[0].keys())
    quoted = ", ".join(f'"{c}"' for c in cols)
    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    conn.execute(f'CREATE TABLE "{table}" ({quoted})')
    conn.executemany(
        f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols))})',
        [[r.get(c) for c in cols] for r in rows])
    for col in INDEXED_COLUMNS:
        if col in cols:
            conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_{col}" '
                         f'ON "{table}"("{col}")')
    return len(rows)


def build(db_path: Path = DB_PATH, gamedata: Path = GAMEDATA_DIR) -> dict[str, int]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    counts: dict[str, int] = {}
    try:
        for path in sorted(gamedata.glob("*.csv")):
            table, n = load_csv(conn, path)
            counts[table] = n

        # Our own extractions. blesses is the important one — no other source
        # covers blesses, including dom6inspector.
        for fname, table in (("blesses.json", "blesses"),
                             ("events.json", "binary_events"),
                             ("spells.json", "binary_spells"),
                             ("nations.json", "binary_nations"),
                             ("binary_unit_attributes.json",
                              "binary_unit_attributes"),
                             ("binary_unit_cost_build.json",
                              "binary_unit_cost_build"),
                             ("unit_descriptions.json",
                              "unit_descriptions")):
            n = load_json_table(conn, REF_DIR / fname, table)
            if n:
                counts[table] = n

        # dom6inspector 6.36 exports an empty `inspiringres` column for all
        # units. The game's own unit-definition table stores this as effect
        # 454; the version-pinned extraction above supplies every base-game
        # source. Fold it into the ordinary rows so all callers see it.
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='binary_unit_attributes'"
        ).fetchone():
            conn.execute(
                "UPDATE units SET inspiringres=("
                "SELECT inspiringres FROM binary_unit_attributes b "
                "WHERE b.id=units.id) WHERE id IN ("
                "SELECT id FROM binary_unit_attributes)"
            )

        # Full-text search over unit names, for "what was that unit called"
        # style lookups. Names are NOT unique — there are 9 distinct
        # "Crossbowman" and 5 "Pikeneer" across nations — so search returns ids
        # and the id is always the key.
        conn.execute("DROP TABLE IF EXISTS unit_search")
        conn.execute("CREATE VIRTUAL TABLE unit_search USING fts5(id, name)")
        conn.execute("INSERT INTO unit_search SELECT id, name FROM units")

        # `basecost` is not gold. Recruitable units store 10000 + gold:
        # Militia 10007 = 7g, Crossbowman 10010 = 10g, Knight of the Chalice
        # 10020 = 20g — all matching the game. 1309 units store 0 (not
        # recruitable), and 43 store 1200..9996, which fits no offset that
        # keeps them positive. Those are left NULL rather than guessed at, so a
        # wrong cost can never be quietly reported as fact.
        conn.execute("DROP VIEW IF EXISTS unit_costs")
        conn.execute("""
            CREATE VIEW unit_costs AS
            SELECT id, name,
                   basecost AS basecost_raw,
                   CASE WHEN basecost >= 10000 THEN basecost - 10000 END AS gold,
                   rcost AS resource_multiplier,
                   hp, prot, mr, mor, str, att, def, prec, enc, ap, size
            FROM units
        """)
        conn.commit()
    finally:
        conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true",
                    help="re-download the CSVs from GitHub first")
    ap.add_argument("--out", type=Path, default=DB_PATH)
    args = ap.parse_args(argv)

    if args.refresh:
        n = refresh_csvs(GAMEDATA_DIR)
        print(f"  downloaded {n} CSVs")

    if not any(GAMEDATA_DIR.glob("*.csv")):
        print(f"No CSVs in {GAMEDATA_DIR}; run with --refresh", file=sys.stderr)
        return 1

    counts = build(args.out, GAMEDATA_DIR)
    total = sum(counts.values())
    print(f"  {len(counts)} tables, {total:,} rows -> {args.out}")
    for table in sorted(counts, key=lambda t: -counts[t])[:8]:
        print(f"    {table:<28}{counts[table]:>8,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
