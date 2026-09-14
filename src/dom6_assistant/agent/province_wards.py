"""Player-visible evidence of hidden foreign province protections.

The owner sees local enchantments in the province Buildings list. Foreign
players do not, even with adjacency, a hidden unit, Spy or their dominion in
the province. A spell interception report is therefore evidence that *some*
protection operated at that moment, not permission to read the complete local
enchantment records serialized in every human player's ``.trn``.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from dom6_assistant.file_reader.formats.messages import MessageRecord

_INTERCEPTED = re.compile(
    r"the spell was destroyed by something that protects the province(?: of ([^.]+))?\.",
    re.IGNORECASE,
)
_CAST = re.compile(r"^[^\n]+ has cast ([^.]+)\.", re.IGNORECASE)

# These are inference cues, not decoded selectors. The report never names the
# ward. Each mapping must remain explicit so a flavour-text resemblance cannot
# silently become a claimed game fact.
_FEEDBACK_CANDIDATES = (
    (
        re.compile(r"powerful frost blast", re.IGNORECASE),
        {
            "spell_id": 1196,
            "spell": "Frost Dome",
            "confidence": "inferred",
            "basis": (
                "the retaliation was a powerful frost blast; Frost Dome is "
                "the matching known province-protection ritual"
            ),
        },
    ),
)


def interception_from_message(
    record: MessageRecord,
    province_ids_by_name: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Return only the information a player can learn from one report."""
    match = _INTERCEPTED.search(record.text)
    if record.type_id != 2 or match is None:
        return None
    cast = _CAST.search(record.text)
    target_name = match.group(1)
    target_id = (
        province_ids_by_name.get(target_name)
        if province_ids_by_name is not None and target_name is not None
        else None
    )
    candidates = [
        dict(candidate)
        for pattern, candidate in _FEEDBACK_CANDIDATES
        if pattern.search(record.text)
    ]
    return {
        "message_id": record.message_id,
        # A type-2 record's ordinary province selector is the caster's
        # location. The remote target exists only in the player-visible prose.
        "caster_province_id": record.province_id,
        "province_id": target_id,
        "province_name_from_report": target_name,
        "attacking_spell": cast.group(1) if cast is not None else None,
        "confirmed": (
            "A hidden province protection intercepted and destroyed our spell "
            "at the time of this report."
        ),
        "retaliation_reported": (
            "powerful frost blast"
            if re.search(r"powerful frost blast", record.text, re.IGNORECASE)
            else None
        ),
        "ward_identity_explicitly_reported": False,
        "candidate_wards": candidates,
        "evidence": record.text.strip(),
    }


def sync_observations(
    conn: sqlite3.Connection,
    game_id: int,
    turn: int,
    records: list[MessageRecord],
    province_ids_by_name: dict[str, int],
) -> None:
    """Persist newly delivered reports idempotently for later turns."""
    rows = []
    for record in records:
        observation = interception_from_message(record, province_ids_by_name)
        if observation is None or observation["province_id"] is None:
            continue
        rows.append(
            (
                game_id,
                turn,
                observation["message_id"],
                observation["province_id"],
                observation["province_name_from_report"],
                observation["attacking_spell"],
                observation["confirmed"],
                observation["retaliation_reported"],
                json.dumps(observation["candidate_wards"], sort_keys=True),
                observation["evidence"],
            )
        )
    if not rows:
        return
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO province_protection_observation("
            "game_id, observed_turn, message_id, province_id, province_name, "
            "attacking_spell, confirmed_effect, retaliation_reported, "
            "candidate_wards_json, evidence) VALUES(?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def remembered_observations(
    conn: sqlite3.Connection,
    game_id: int,
    province_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return durable evidence without claiming the protection is still up."""
    sql = (
        "SELECT observed_turn, message_id, province_id, province_name, "
        "attacking_spell, confirmed_effect, retaliation_reported, "
        "candidate_wards_json, evidence "
        "FROM province_protection_observation WHERE game_id=?"
    )
    params: list[int] = [game_id]
    if province_id is not None:
        sql += " AND province_id=?"
        params.append(province_id)
    sql += " ORDER BY observed_turn DESC, message_id DESC"
    return [
        {
            "observed_turn": row["observed_turn"],
            "message_id": row["message_id"],
            "province_id": row["province_id"],
            "province": row["province_name"],
            "attacking_spell": row["attacking_spell"],
            "confirmed": row["confirmed_effect"],
            "retaliation_reported": row["retaliation_reported"],
            "ward_identity_explicitly_reported": False,
            "candidate_wards": json.loads(row["candidate_wards_json"]),
            "active_now": None,
            "remaining_duration": None,
            "visibility_note": (
                "Foreign province enchantments are hidden. This proves the "
                "protection operated on the observed turn only; its identity, "
                "current activity and remaining duration are not directly visible."
            ),
            "evidence": row["evidence"],
        }
        for row in conn.execute(sql, params)
    ]
