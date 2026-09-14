"""A tool session for talking about Dominions 6 without playing it.

Separate from both the turn registry and the pretender registry, and for the
same reason they are separate from each other: what a session can reach should
be decided when it opens, not argued about per call.

This one reaches **static reference data only** — no ``.trn``, no ``.2h``, no
``ftherlnd``, no game database. There is no visibility boundary to police here
because there is no game in scope: nothing this session can read depends on
whose turn it is or what anyone has scouted. That is what makes it safe to
answer open questions with, and it is enforced structurally by the context
carrying no save path and no game connection rather than by care at each tool.

It exists so the player can probe what the model knows, watch how it moves
through the tools, and settle on a nation before opening a pretender session
for it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dom6_assistant.agent.registry import Param, ToolError, ToolRegistry
from dom6_assistant.reference import unit_profile
from dom6_assistant.reference.pretender_design import (
    ATTR_SCALE_LIMIT_BASE,
    ATTR_TEMPERATURE_PREFERENCE,
)
from dom6_assistant.file_reader.formats.pretender import SCALE_NAMES
from dom6_assistant.wiki.index import DEFAULT_INDEX_DB

DEFAULT_REFERENCE_DB = Path("knowledge/reference/reference.sqlite3")

_ERAS = {1: "Early Age", 2: "Middle Age", 3: "Late Age"}

#: Each scale's two named ends, good side first. The game's own screens use
#: these words -- EA Yomi's nation screen reads "Turmoil limit +1", never
#: "order -1" -- and a named mechanic must reach the model as its real name.
_SCALE_ENDS = {
    "order": ("Order", "Turmoil"),
    "productivity": ("Productivity", "Sloth"),
    "heat": ("Heat", "Cold"),
    "growth": ("Growth", "Death"),
    "luck": ("Luck", "Misfortune"),
    "magic": ("Magic", "Drain"),
}


@dataclass
class OpenChatContext:
    #: The only data source. No game database and no save directory exist on
    #: this context, so no tool registered here can reach one.
    reference_db: sqlite3.Connection
    wiki_db: Path = DEFAULT_INDEX_DB


@dataclass
class OpenChatSession:
    ctx: OpenChatContext
    registry: ToolRegistry

    @property
    def turn(self) -> int:
        return 0

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.registry.call(self.ctx, name, args)

    def close(self) -> None:
        self.ctx.reference_db.close()


def _rows(ctx: OpenChatContext, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in ctx.reference_db.execute(sql, params)]


def _lookup(ctx: OpenChatContext, table: str, name: str | None,
            row_id: int | None, limit: int = 10) -> list[sqlite3.Row]:
    if row_id is not None:
        found = ctx.reference_db.execute(
            f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchall()
        if not found:
            raise ToolError(f"no {table[:-1]} with id {row_id}")
        return found
    if not (name or "").strip():
        raise ToolError(f"give either a name or an exact {table[:-1]} id")
    needle = name.strip()
    found = ctx.reference_db.execute(
        f"SELECT * FROM {table} WHERE name LIKE ? ORDER BY length(name) LIMIT ?",
        (f"%{needle}%", limit)).fetchall()
    if not found:
        raise ToolError(f"no {table[:-1]} matching {needle!r}")
    return found


def register(registry: ToolRegistry | None = None,
             wiki_db: Path | None = None) -> ToolRegistry:
    reg = registry or ToolRegistry()

    @reg.tool(
        "list_nations",
        "Every playable nation, optionally filtered by era. Use this to choose "
        "a nation to talk about or to design a pretender for.",
        Param("era", "string", "early, middle, late, or all",
              required=False, default="all",
              choices=("early", "middle", "late", "all")),
        Param("search", "string", "match part of a nation name",
              required=False, default=""),
    )
    def list_nations(ctx: OpenChatContext, era: str = "all",
                     search: str = "") -> dict[str, Any]:
        wanted = {"early": 1, "middle": 2, "late": 3}.get(era)
        sql = "SELECT id,name,epithet,era FROM nations"
        clauses, params = [], []
        if wanted:
            clauses.append("era=?"); params.append(wanted)
        if search.strip():
            clauses.append("name LIKE ?"); params.append(f"%{search.strip()}%")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = _rows(ctx, sql + " ORDER BY era,name", tuple(params))
        for row in rows:
            row["era_name"] = _ERAS.get(row["era"], "?")
        return {"count": len(rows), "nations": rows}

    @reg.tool(
        "describe_nation",
        "A nation's identity and the national features that shape a pretender "
        "choice: its scale limits, temperature preference and cheap-god forms.",
        Param("nation_id", "integer", "id from list_nations"),
    )
    def describe_nation(ctx: OpenChatContext, nation_id: int) -> dict[str, Any]:
        row = ctx.reference_db.execute(
            "SELECT id,name,epithet,era FROM nations WHERE id=?",
            (int(nation_id),)).fetchone()
        if row is None:
            raise ToolError(f"no nation with id {nation_id}")

        def attr(number: int) -> list[int]:
            return [int(r[0]) for r in ctx.reference_db.execute(
                "SELECT raw_value FROM attributes_by_nation "
                "WHERE nation_number=? AND attribute=?", (int(nation_id), number))]

        # Scale limits use the negated convention. A negative value shifts the
        # five-step window toward the bad side: EA Yomi's -1 on order is the
        # "Turmoil limit +1" its own nation screen shows.
        limits = {}
        for index, scale in enumerate(SCALE_NAMES):
            values = attr(ATTR_SCALE_LIMIT_BASE + index)
            if values and values[0]:
                value = values[0]
                good, bad = _SCALE_ENDS[scale]
                limits[scale] = {
                    "raw": value,
                    "reads_as": (f"{good} limit +{value}" if value > 0
                                 else f"{bad} limit +{-value}"),
                }
        preference = attr(ATTR_TEMPERATURE_PREFERENCE)
        return {
            "nation": dict(row) | {"era_name": _ERAS.get(row["era"], "?")},
            "scale_limit_modifiers": limits or None,
            "temperature_preference_raw": preference[0] if preference else 0,
            "note": (
                "An unmodified scale ranges from -2 to +2. A limit modifier "
                "shifts that whole window, so bad-side +1 means -3..+1. "
                "Open a Pretender design session for a chassis-resolved range "
                "and exact prices."
            ),
        }

    @reg.tool(
        "lookup_unit",
        "A unit's stats, traits, weapons, armour, resistances and movement. "
        "Reference data only: no recruitment cost, which depends on a game.",
        Param("name", "string", "unit name or part of one", required=False),
        Param("unit_id", "integer", "exact unit id", required=False),
        Param("detail", "string", "summary or full", required=False,
              default="summary", choices=("summary", "full")),
    )
    def lookup_unit(ctx: OpenChatContext, name: str | None = None,
                    unit_id: int | None = None,
                    detail: str = "summary") -> list[dict[str, Any]]:
        out = []
        for row in _lookup(ctx, "units", name, unit_id):
            entry: dict[str, Any] = {
                "id": row["id"], "name": row["name"],
                "stats": unit_profile.core_stats(row),
                "traits": unit_profile.summary_traits(row) or None,
                "description": unit_profile.description(
                    ctx.reference_db, int(row["id"])),
            }
            if detail == "full":
                entry.update(unit_profile.full_profile(ctx.reference_db, row))
            out.append(entry)
        return out

    def _simple(table: str, label: str, description: str, columns: tuple[str, ...]):
        @reg.tool(
            f"lookup_{label}", description,
            Param("name", "string", f"{label} name or part of one", required=False),
            Param(f"{label}_id", "integer", f"exact {label} id", required=False),
        )
        def handler(ctx: OpenChatContext, **values: Any) -> list[dict[str, Any]]:
            rows = _lookup(ctx, table, values.get("name"),
                           values.get(f"{label}_id"))
            keys = set(rows[0].keys())
            return [{c: r[c] for c in columns if c in keys} for r in rows]
        return handler

    _simple("spells", "spell",
            "A spell: school, research level, paths, gem cost and effect.",
            ("id", "name", "school", "researchlevel", "path1", "pathlevel1",
             "path2", "pathlevel2", "fatiguecost", "effect", "descr"))
    _simple("items", "item",
            "A magic item: construction level, paths and effect.",
            ("id", "name", "type", "constlevel", "mainpath", "mainlevel",
             "secondarypath", "secondarylevel"))
    _simple("magic_sites", "magic_site",
            "A magic site: paths, gems and what it enables.",
            ("id", "name", "path", "level", "rarity", "gems1", "gems2"))

    from dom6_assistant.agent import wiki_tools
    wiki_tools.register(reg, wiki_db or DEFAULT_INDEX_DB)
    return reg


def open_chat_session(reference_db: Path | None = None,
                      wiki_db: Path | None = None) -> OpenChatSession:
    path = reference_db or DEFAULT_REFERENCE_DB
    if not path.exists():
        raise FileNotFoundError(f"reference database not found at {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    ctx = OpenChatContext(reference_db=connection,
                          wiki_db=wiki_db or DEFAULT_INDEX_DB)
    return OpenChatSession(ctx=ctx, registry=register(wiki_db=ctx.wiki_db))
