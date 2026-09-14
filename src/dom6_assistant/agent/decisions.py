"""A single audit timeline over the existing append-only intent tables.

The writers already do the important part: every action carries a required
rationale and changing one's mind inserts a new row.  This module does not
duplicate that journal.  It projects the separate, correctly-keyed tables into
one readable timeline and links each decision to the previous decision for the
same subject.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DecisionSource:
    table: str
    category: str
    key: tuple[str, ...]


SOURCES = (
    DecisionSource("order_intent", "strategic_order", ("commander_id", "commander_name")),
    DecisionSource("battle_intent", "battle_order", ("commander_id", "commander_name", "squad")),
    DecisionSource("equipment_intent", "equipment", ("commander_id", "commander_name", "slot")),
    DecisionSource("shape_change_intent", "shape_change", ("commander_id", "commander_name")),
    DecisionSource("battle_position_intent", "battle_position", ("commander_id", "commander_name", "squad")),
    DecisionSource("carried_gem_intent", "carried_gems", ("commander_id", "commander_name", "path")),
    DecisionSource("battle_script_intent", "battle_script", ("commander_id", "commander_name")),
    DecisionSource("troop_assignment_intent", "troop_assignment", ("unit_instance_id", "unit_type_id", "unit_name")),
    DecisionSource("squad_creation_intent", "squad_creation", ("target_commander_id", "commander_name", "target_squad")),
    DecisionSource("recruit_intent_v2", "recruitment", ("province_id", "position")),
    DecisionSource("research_intent", "research", ()),
    DecisionSource("province_defence_intent", "province_defence", ("province_id",)),
    DecisionSource("mercenary_bid_intent", "mercenary_bid", ("slot",)),
    DecisionSource("diplomacy_intent", "diplomacy", ("target_nation_id",)),
)


_OMIT = {"id", "game_id", "turn", "rationale", "created_at"}


def _tables(db: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _value(row: sqlite3.Row, name: str) -> Any:
    return row[name] if name in row.keys() else None


def _subject(source: DecisionSource, row: sqlite3.Row) -> str:
    name = _value(row, "commander_name")
    if name:
        return str(name)
    for column, label in (
        ("company", "mercenary"),
        ("province_id", "province"),
        ("target_nation_id", "nation"),
        ("unit_name", "unit"),
        ("unit_instance_id", "unit"),
    ):
        value = _value(row, column)
        if value is not None:
            return f"{label} {value}"
    return source.category


def _action(db: sqlite3.Connection, source: DecisionSource,
            row: sqlite3.Row) -> dict[str, Any]:
    action = {key: row[key] for key in row.keys() if key not in _OMIT}
    if source.table == "order_intent":
        child_specs = (
            ("ritual_intent", "order_intent_id"),
            ("forge_intent", "order_intent_id"),
            ("empowerment_intent", "order_intent_id"),
        )
        available = _tables(db)
        for table, foreign_key in child_specs:
            if table not in available:
                continue
            child = db.execute(
                f"SELECT * FROM {table} WHERE {foreign_key}=?", (row["id"],)
            ).fetchone()
            if child:
                action[table.removesuffix("_intent")] = {
                    key: child[key] for key in child.keys()
                    if key != foreign_key
                }
    for key, value in list(action.items()):
        if key.endswith("_json") and isinstance(value, str):
            try:
                action[key.removesuffix("_json")] = json.loads(value)
                del action[key]
            except ValueError:
                pass
    return action


def decision_history(
    db: sqlite3.Connection,
    game_id: int,
    *,
    turn: int | None = None,
    category: str | None = None,
    search: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    available = _tables(db)
    collected: list[tuple[DecisionSource, sqlite3.Row]] = []
    for source in SOURCES:
        if source.table not in available or (category and category != source.category):
            continue
        sql = f"SELECT * FROM {source.table} WHERE game_id=?"
        args: list[Any] = [game_id]
        if turn is not None:
            sql += " AND turn=?"
            args.append(turn)
        sql += " ORDER BY turn,id"
        collected.extend((source, row) for row in db.execute(sql, args))

    # Link the entire campaign, while distinguishing a same-turn replacement
    # from the corresponding subject's previous-turn decision.
    previous: dict[tuple[str, tuple[Any, ...]], str] = {}
    latest_same_turn: dict[tuple[str, int, tuple[Any, ...]], str] = {}
    records: list[dict[str, Any]] = []
    for source, row in sorted(
        collected, key=lambda pair: (
            int(pair[1]["turn"]), str(pair[1]["created_at"]),
            pair[0].table, int(pair[1]["id"]),
        )
    ):
        key_values = tuple(_value(row, key) for key in source.key)
        subject_key = (source.category, key_values)
        turn_key = (source.category, int(row["turn"]), key_values)
        ref = f"{source.table}:{row['id']}"
        record = {
            "decision_ref": ref,
            "game_id": int(row["game_id"]),
            "turn": int(row["turn"]),
            "category": source.category,
            "subject": _subject(source, row),
            "subject_key": list(key_values),
            "action": _action(db, source, row),
            "rationale": row["rationale"],
            "previous_decision": previous.get(subject_key),
            "supersedes": latest_same_turn.get(turn_key),
            "created_at": row["created_at"],
            "is_current_revision": True,
        }
        prior_revision = latest_same_turn.get(turn_key)
        if prior_revision:
            for old in reversed(records):
                if old["decision_ref"] == prior_revision:
                    old["is_current_revision"] = False
                    break
        previous[subject_key] = ref
        latest_same_turn[turn_key] = ref
        records.append(record)

    if search:
        needle = search.casefold()
        records = [
            row for row in records
            if needle in json.dumps(row, default=str).casefold()
        ]
    records.reverse()
    return records[:max(1, min(limit, 5000))]
