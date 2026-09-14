"""Player-equivalent bookkeeping for the hidden Call God point total."""
from __future__ import annotations

import json
from typing import Any

from dom6_assistant.agent.registry import ToolContext


def observe(ctx: ToolContext) -> dict[str, Any] | None:
    """Store and summarize the Call God bounds visible from current orders.

    The game never shows the random accumulated total.  This records only what
    a human can know: which Holy levels were praying in each planning turn and
    therefore the H-1..H+1 interval that will resolve when the turn is hosted.
    """
    state = ctx.view.own_pretender_state(ctx.h2_path)
    if state["dead"] is None:
        return None

    priests: list[dict[str, Any]] = []
    if state["dead"]:
        for commander in ctx.view.own_commanders(ctx.h2_path):
            holy = commander.paths.get("H", 0)
            if commander.order != "call_god" or holy < 1:
                continue
            priests.append({
                "commander_id": commander.commander_id,
                "name": commander.name,
                "holy_level": holy,
                "ordinary_contribution_range": [max(0, holy - 1), holy + 1],
            })

    minimum = sum(row["ordinary_contribution_range"][0] for row in priests)
    maximum = sum(row["ordinary_contribution_range"][1] for row in priests)
    ctx.game_db.execute(
        "INSERT INTO call_god_observation("
        "game_id, turn, pretender_id, pretender_dead, minimum_points, "
        "maximum_points, active_priests, observed_at) VALUES(?,?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(game_id, turn) DO UPDATE SET "
        "pretender_id=excluded.pretender_id, "
        "pretender_dead=excluded.pretender_dead, "
        "minimum_points=excluded.minimum_points, "
        "maximum_points=excluded.maximum_points, "
        "active_priests=excluded.active_priests, "
        "observed_at=excluded.observed_at",
        (ctx.game_id, ctx.turn, state["commander_id"], int(state["dead"]),
         minimum, maximum, json.dumps(priests, separators=(",", ":"))),
    )
    ctx.game_db.commit()

    if not state["dead"]:
        return {
            "active_priests": [],
            "current_turn_contribution_range": [0, 0],
            "tracked_resolved_contribution_range": [0, 0],
            "possible_accumulated_points_range": None,
            "tracking_complete_since_death": False,
        }

    rows = list(ctx.game_db.execute(
        "SELECT turn, pretender_dead, minimum_points, maximum_points "
        "FROM call_god_observation WHERE game_id=? AND turn<=? ORDER BY turn",
        (ctx.game_id, ctx.turn),
    ))
    last_alive = max(
        (row["turn"] for row in rows if not row["pretender_dead"]),
        default=None,
    )
    death_rows = [
        row for row in rows
        if row["pretender_dead"] and (last_alive is None or row["turn"] > last_alive)
    ]
    resolved = [row for row in death_rows if row["turn"] < ctx.turn]
    tracked_min = sum(row["minimum_points"] for row in resolved)
    tracked_max = sum(row["maximum_points"] for row in resolved)

    first_dead = death_rows[0]["turn"] if death_rows else ctx.turn
    observed_turns = {row["turn"] for row in death_rows}
    expected_turns = set(range(first_dead, ctx.turn + 1))
    complete = (
        last_alive == first_dead - 1
        and expected_turns.issubset(observed_turns)
    )
    possible = [tracked_min, min(49, tracked_max)] if complete else None

    return {
        "active_priests": priests,
        "current_turn_contribution_range": [minimum, maximum],
        "tracked_resolved_contribution_range": [tracked_min, tracked_max],
        "possible_accumulated_points_range": possible,
        "tracking_started_turn": first_dead,
        "tracking_through_resolved_turn": ctx.turn - 1,
        "tracking_complete_since_death": complete,
    }
