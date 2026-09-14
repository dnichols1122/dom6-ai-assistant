"""Read tools: everything the assistant can ask about the game.

Each of these returns plain dicts and lists, small enough to sit in a local
model's context without crowding out its reasoning. That constraint shapes the
design more than it might appear:

*Summaries are summaries.* `get_turn_summary` returns counts and a handful of
named things, not a roster. A 7B model given 76 unit records will spend its
whole context on them and then have none left to decide anything. The detail is
one call away when a specific question needs it, and that call is cheap.

*Names come with ids and ids come with names.* The commonest failure of a small
model driving a tool surface is calling a tool with a name where an id belongs.
Rather than coercing (see `registry`), every result that mentions a province or
a unit carries both, so the id the next call needs is already in front of it.

*Reference data is queried, never injected.* Dominions has 4,091 units, 529
items and 1,474 spells, and this project exists because unaided knowledge of
them is wrong. Loading them into a prompt is impossible and summarising them
would reintroduce exactly the approximation the tools are here to remove — so
they stay in SQLite and get looked up.

Everything here goes through `PlayerView`. No tool in this module opens a file.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from typing import Any

from dom6_assistant.agent.economics import (
    owned_province_holy_budgets,
    owned_province_income_budgets,
    owned_province_resource_budget,
    owned_province_resource_budgets,
    owned_province_supply_budgets,
)
from dom6_assistant.agent import province_wards as PW
from dom6_assistant.agent.decisions import decision_history
from dom6_assistant.agent.turn_completion import completion_status
from dom6_assistant.agent.registry import Param, ToolContext, ToolError, ToolRegistry
from dom6_assistant.reference import forge_cost as FC
from dom6_assistant.reference import divine_spells as DV
from dom6_assistant.reference import leadership as LD
from dom6_assistant.reference import mercenary_cost as MC
from dom6_assistant.reference import research_rate as RR
from dom6_assistant.reference import ritual_range as RTR
from dom6_assistant.reference import wish as WISH
from dom6_assistant.reference import unit_profile
from dom6_assistant.agent.visibility import VisibilityError
from dom6_assistant.reference.unit_cost import (
    gold_cost_for,
    recruitment_point_cost_for,
    resource_cost_for,
)
from dom6_assistant.reference.province_calc import estimate_recruitment_points
from dom6_assistant.file_reader.formats import commanders as C
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.file_reader.formats import trn as T
from dom6_assistant.file_reader.formats import units as U
from dom6_assistant.file_reader.formats.mapfile import border_names
from dom6_assistant.orders import orders_2h as O
from dom6_assistant.orders.materialize import BASE_SUFFIX


#: Ermor's nation trait: its Pretender can sense corpses in provinces under
#: its Dominion.  Presence of the attribute, rather than nation id 54, keeps
#: this correct for copied/modded nation definitions in the reference data.
CORPSE_SENSING_NATION_ATTRIBUTE = 699


def _site_is_throne(ctx: ToolContext, site_id: int) -> bool:
    """Rarity 11/12/13 are level-one/two/three throne sites."""
    row = ctx.reference_db.execute(
        "SELECT rarity FROM magic_sites WHERE id=?", (site_id,)
    ).fetchone()
    return bool(row and 11 <= row["rarity"] <= 13)


def _nation_senses_corpses(ctx: ToolContext) -> bool:
    row = ctx.reference_db.execute(
        "SELECT 1 FROM attributes_by_nation "
        "WHERE nation_number=? AND attribute=? LIMIT 1",
        (ctx.nation_id, CORPSE_SENSING_NATION_ATTRIBUTE),
    ).fetchone()
    return row is not None


def _visible_corpse_count(
    ctx: ToolContext,
    province: Any,
    nation_senses_corpses: bool | None = None,
) -> int | None:
    """Return the panel-visible corpse count, preserving known zero.

    The two serialized signed components are raw private state: zero is also
    present where the panel says nothing.  The controlled Ermor/Marignon save
    proves the client exposes the sum to a corpse-sensing nation only where
    that nation's Dominion is present.  Unknown therefore stays ``None``.
    """
    senses = (
        _nation_senses_corpses(ctx)
        if nation_senses_corpses is None
        else nation_senses_corpses
    )
    if (
        not senses
        or province.dominion_owner != ctx.nation_id
        or not province.dominion_strength
    ):
        return None
    raw = next(
        (
            candidate
            for candidate in ctx.view.parsed.provinces
            if candidate.province_id == province.province_id
        ),
        None,
    )
    return raw.corpse_count_raw if raw is not None else None


def _visible_local_enchantments(
    ctx: ToolContext, province: Any,
) -> list[dict[str, Any]]:
    """Buildings-list enchantments confirmed visible to this player.

    Complete raw records occur in the other human's turn file too, but the
    controlled UI shows none of them to a foreign player under adjacency,
    local stealth, Spy or friendly dominion. Foreign provinces therefore have
    no *visible* entries, even when hidden raw records exist.
    """

    if not province.is_ours:
        return []
    records = [
        row for row in ctx.view.parsed.local_enchantments
        if row.province_id == province.province_id
        and row.caster_nation_id == ctx.nation_id
        and RTR.spell_is_province_enchantment(
            ctx.reference_db, row.spell_id)
    ]
    if not records:
        return []

    commanders = {row.commander_id: row for row in _commanders(ctx)}
    by_runtime: dict[int, Any] = {}
    if ctx.h2_path is not None and ctx.h2_path.exists():
        h2_data = ctx.h2_path.read_bytes()
        for commander_id, block in O.find_order_blocks(h2_data).items():
            commander = commanders.get(commander_id)
            if commander is None or block.name_end + 4 > len(h2_data):
                continue
            runtime = struct.unpack_from("<I", h2_data, block.name_end)[0]
            by_runtime[runtime] = commander

    out = []
    for record in records:
        spell = ctx.reference_db.execute(
            "SELECT name, gemcost FROM spells WHERE id=?", (record.spell_id,)
        ).fetchone()
        caster = by_runtime.get(record.caster_runtime_index)
        unit = (
            ctx.reference_db.execute(
                "SELECT name FROM units WHERE id=?", (caster.type_id,)
            ).fetchone()
            if caster is not None and caster.type_id is not None
            else None
        )
        lost = ctx.reference_db.execute(
            "SELECT 1 FROM attributes_by_spell WHERE spell_number=? "
            "AND attribute=729 LIMIT 1", (record.spell_id,)
        ).fetchone()
        out.append({
            "spell_id": record.spell_id,
            "name": spell["name"] if spell else None,
            "caster_commander_id": caster.commander_id if caster else None,
            "caster": caster.name if caster else None,
            "caster_unit_type": unit["name"] if unit else None,
            "months_left": record.months_left,
            "duration_extension_months_per_gem": (
                RTR.spell_duration_extension_months_per_gem(
                    ctx.reference_db, record.spell_id)
            ),
            "base_gem_cost": int(spell["gemcost"] or 0) if spell else None,
            "dispelled_if_province_lost": bool(lost),
        })
    return out


def _local_enchantment_visibility(province: Any) -> str:
    if province.is_ours:
        return "owner_buildings_list"
    return (
        "foreign_hidden_even_with_adjacency_local_stealth_spy_or_friendly_dominion"
    )


def register(reg: ToolRegistry) -> ToolRegistry:
    """Add every read tool to `reg`. Returns it for chaining."""

    # -- orientation -----------------------------------------------------

    @reg.tool(
        "get_turn_summary",
        "Overview of the current turn: our nation, gold, gems, research, "
        "province and army counts, and which commanders have no orders yet. "
        "Call this first each turn.",
    )
    def get_turn_summary(ctx: ToolContext) -> dict[str, Any]:
        v = ctx.view
        p = v.parsed
        own = v.own_provinces()
        units = _own_troops(ctx)
        cmds = _commanders(ctx)
        ordered = {r["commander_id"] for r in _intent_rows(ctx)}
        unordered = [
            {"commander_id": c.commander_id, "name": c.name}
            for c in cmds
            if c.commander_id not in ordered
        ]
        global_effects = T.read_global_effects(v.data)
        throne_ids = {
            site_id
            for province in v.provinces()
            for site_id in province.site_ids
            if _site_is_throne(ctx, site_id)
        }
        magic = _magic_economy(ctx)
        out = {
            "game": v.game_name,
            "turn": v.turn,
            "nation_id": v.nation_id,
            "gold": p.player_gold,
            "gold_uncommitted_in_orders_file": _gold_uncommitted(ctx),
            "gems": _gems(p),
            "gems_uncommitted_in_orders_file": magic["uncommitted"],
            "gem_income_from_visible_owned_sites": magic["income"],
            "research_points": p.research_points,
            "provinces_owned": len(own),
            "province_names": [x.name for x in own if x.name],
            "units_total": len(units),
            "commanders": len(cmds),
            "commanders_without_orders": unordered,
            "orders_recorded": len(ordered),
            "active_global_enchantments": len(global_effects),
            "known_thrones": len(throne_ids),
            "claimed_throne_points": p.claimed_throne_points,
        }
        return out

    @reg.tool(
        "list_provinces",
        "Provinces we can see. Ours by default; pass scope='known' to include "
        "foreign provinces whose owner our map shows, or scope='all' to "
        "include unexplored ones. corpse_count is null when the panel hides "
        "it; a visible zero is returned as 0. province_enchantments contains "
        "our Buildings entries and is empty for foreign provinces because "
        "their local enchantments are never displayed.",
        Param(
            "scope",
            "string",
            "ours | known | all",
            required=False,
            default="ours",
            choices=("ours", "known", "all"),
        ),
    )
    def list_provinces(ctx: ToolContext, scope: str = "ours") -> list[dict]:
        out = []
        nation_senses_corpses = _nation_senses_corpses(ctx)
        for p in ctx.view.provinces():
            if scope == "ours" and not p.is_ours:
                continue
            if scope == "known" and not p.owner_is_known:
                continue
            out.append(
                {
                    "province_id": p.province_id,
                    "name": p.name,
                    "status": p.status,
                    "population": p.population,
                    "owner_nation": _nation_name(ctx, p.owner_nation_id),
                    "map_position": {"x": p.map_x, "y": p.map_y},
                    "terrain": _terrain(ctx, p.current_terrain_flags),
                    "fort": bool(p.fort_type),
                    "temple": p.has_temple,
                    "lab": p.has_laboratory,
                    "corpse_count": _visible_corpse_count(
                        ctx, p, nation_senses_corpses
                    ),
                    "province_enchantments": _visible_local_enchantments(ctx, p),
                    "province_enchantment_visibility": (
                        _local_enchantment_visibility(p)
                    ),
                }
            )
        return out

    @reg.tool(
        "get_province",
        "Full detail on one province, including our units stationed there. "
        "corpse_count is null when the province panel hides it; known zero "
        "is returned as 0. province_enchantments includes our Buildings-list "
        "effects and is empty for foreign views. Historical interception "
        "reports remain under province_protection_observations.",
        Param(
            "province_id",
            "integer",
            "numeric province id",
            hint="Call find_province to look up an id by name.",
        ),
    )
    def get_province(ctx: ToolContext, province_id: int) -> dict[str, Any]:
        p = ctx.view.province(province_id)
        if p is None:
            raise ToolError(
                f"no province {province_id} in this save. Call list_provinces to see valid ids."
            )
        here = [u for u in _own_troops(ctx) if u.province_id == province_id]
        sites = []
        for site_id in p.site_ids:
            site = ctx.reference_db.execute(
                "SELECT * FROM magic_sites WHERE id=?", (site_id,)
            ).fetchone()
            detail = _site_brief(ctx, site) if site else {"id": site_id, "name": None}
            detail["site_id"] = site_id
            sites.append(detail)
        return {
            "province_id": p.province_id,
            "name": p.name,
            "status": p.status,
            "owner_nation_id": p.owner_nation_id,
            "owner_nation": _nation_name(ctx, p.owner_nation_id),
            "is_ours": p.is_ours,
            "is_capital": p.is_capital,
            "map_position": {"x": p.map_x, "y": p.map_y},
            "population": p.population,
            "unrest": p.unrest,
            "province_defense": p.province_defense,
            "fort_type": p.fort_type,
            "wall_integrity": p.wall_integrity,
            "fort_under_construction": p.under_construction,
            "has_temple": p.has_temple,
            "has_laboratory": p.has_laboratory,
            "administrative_owner_nation_id": p.administrative_owner,
            "administrative_owner_nation": _nation_name(ctx, p.administrative_owner),
            "dominion_owner_nation_id": p.dominion_owner,
            "dominion_owner_nation": _nation_name(ctx, p.dominion_owner),
            "dominion_strength": p.dominion_strength,
            "corpse_count": _visible_corpse_count(ctx, p),
            "terrain": _terrain(ctx, p.current_terrain_flags),
            "original_map_terrain": _terrain(ctx, p.terrain_flags),
            "scales": (
                {
                    "order": p.order_scale,
                    "productivity": p.productivity_scale,
                    "heat": p.heat_scale,
                    "growth": p.growth_scale,
                    "luck": p.luck_scale,
                    "magic": p.magic_scale,
                }
                if p.is_ours
                else None
            ),
            "visible_magic_sites": sites,
            "province_enchantments": _visible_local_enchantments(ctx, p),
            "province_enchantment_visibility": _local_enchantment_visibility(p),
            "province_protection_observations": PW.remembered_observations(
                ctx.game_db, ctx.game_id, province_id),
            "our_units_here": len(here),
            "our_unit_types": _type_counts(ctx, here),
            # The figure the province panel prints, not the true roster. A
            # province we have never scouted says so rather than reading 0 —
            # "we do not know" and "it is empty" must not look the same.
            **_enemy_estimate(ctx, province_id),
        }

    @reg.tool(
        "scout_report",
        "Everything we know about foreign provinces: the estimated enemy "
        "strength the province panel shows, and whether we have a unit there "
        "giving better information. Provinces we have not scouted are absent, "
        "which means unknown, NOT empty. A Scout supplies intelligence merely "
        "by being there while hidden; there is no separate Scout order.",
    )
    def scout_report(ctx: ToolContext) -> dict[str, Any]:
        names = {p.province_id: p.name for p in ctx.view.provinces()}
        rows = [
            {
                "province_id": pid,
                "name": names.get(pid),
                "enemy_units": i.enemy_units,
                "estimate_uncertainty_percent": i.estimate_uncertainty_percent,
                "intel_from_our_own_unit": i.has_our_unit,
                **_scout_prose(ctx, pid),
            }
            for pid, i in sorted(ctx.view.intel().items())
        ]
        return {
            "scouted": rows,
            "note": "these are the estimates the game shows, not exact counts. "
            "Any province not listed is one we know nothing about. Scouts "
            "report automatically while hidden; do not invent a Scout order.",
        }

    @reg.tool(
        "get_neighbours",
        "Which provinces border this one, who holds them, what we know of "
        "their strength, and what kind of border lies between. This is map "
        "adjacency, not the complete movement highlight; use "
        "get_movement_options for a particular commander or army.",
        Param(
            "province_id",
            "integer",
            "province id",
            hint="Call find_province to look one up by name.",
        ),
    )
    def get_neighbours(ctx: ToolContext, province_id: int) -> dict[str, Any]:
        if ctx.view.province(province_id) is None:
            raise ToolError(f"no province {province_id} in this game.")
        turn = _latest_turn(ctx)
        links = _neighbours(ctx, turn)
        if province_id not in links:
            raise ToolError(
                f"no adjacency recorded for province {province_id}; the turn "
                "may not have been ingested yet."
            )
        borders = {
            (r["province_id"], r["neighbour_id"]): r["border_flags"]
            for r in ctx.game_db.execute(
                "SELECT province_id, neighbour_id, border_flags FROM map_borders WHERE game_id=?",
                (ctx.game_id,),
            )
        }
        out = []
        for nid in sorted(links[province_id]):
            p = ctx.view.province(nid)
            if p is None:
                continue
            flag = borders.get((province_id, nid)) or borders.get((nid, province_id))
            entry: dict[str, Any] = {
                "province_id": nid,
                "name": p.name,
                "status": p.status,
                "owner_nation": _nation_name(ctx, p.owner_nation_id),
                "map_position": {"x": p.map_x, "y": p.map_y},
                "population": p.population,
                "fort": bool(p.fort_type),
                "our_units": len([u for u in _own_troops(ctx) if u.province_id == nid]),
                "connections": len(links.get(nid, ())),
            }
            if flag:
                entry["border"] = border_names(int(flag))
            entry.update(_enemy_estimate(ctx, nid))
            out.append(entry)
        return {
            "province_id": province_id,
            "connections": len(links[province_id]),
            "neighbours": out,
            "note": "connections is how many provinces each one touches — a low "
            "count is a chokepoint, easier to hold and harder to pass.",
        }

    @reg.tool(
        "get_movement_options",
        "Every legal one-turn destination for one commander's current army. "
        "Uses the client's half-province movement costs, slowest unit, current "
        "terrain/ownership/scales, survival traits, roads, rivers, mountain "
        "passes, land/sea rules, Flying, Teleport and Sailing.",
        Param("commander_id", "integer", "commander id from list_commanders"),
    )
    def get_movement_options(ctx: ToolContext, commander_id: int) -> dict[str, Any]:
        from dom6_assistant.agent.movement import movement_analysis

        commander = next(
            (row for row in _commanders(ctx) if row.commander_id == commander_id),
            None,
        )
        if commander is None:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Call "
                "list_commanders for valid ids."
            )
        source = ctx.view.province(commander.province_id) if commander.province_id else None
        if source is None:
            raise ToolError(f"{commander.name}'s current province is unknown.")
        analysis = movement_analysis(ctx, commander)
        analysis["source_province"] = source.name
        analysis["note"] = (
            "Destinations are final provinces the client can accept this turn; "
            "route is the least-cost path and movement_cost is the sum of both "
            "half-province costs for every crossed border."
        )
        return analysis

    @reg.tool(
        "get_item_treasury",
        "Magic items we own that are NOT currently equipped on anyone. These "
        "are the items equip_item or an item-transport ritual can reserve. "
        "Recorded but unmaterialized transport payloads are marked explicitly.",
    )
    def get_item_treasury(ctx: ToolContext) -> dict[str, Any]:
        items = _treasury(ctx)
        reservations: dict[int, list[sqlite3.Row]] = {}
        for reservation in ctx.game_db.execute(
            "SELECT r.target_item_id, o.commander_id, o.commander_name, "
            "r.target_commander_id FROM current_orders o "
            "JOIN ritual_intent r ON r.order_intent_id=o.id "
            "WHERE o.game_id=? AND o.turn=? "
            "AND r.target_item_id IS NOT NULL ORDER BY o.id",
            (ctx.game_id, ctx.turn),
        ):
            reservations.setdefault(
                int(reservation["target_item_id"]), []).append(reservation)
        rows = []
        seen: dict[int, int] = {}
        for item_id in items:
            row = ctx.reference_db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            detail = _item_brief(ctx, row) if row else {"id": item_id, "name": None}
            detail["item_id"] = item_id
            copy_index = seen.get(item_id, 0)
            seen[item_id] = copy_index + 1
            if copy_index < len(reservations.get(item_id, ())):
                reservation = reservations[item_id][copy_index]
                detail["recorded_for_transport"] = {
                    "caster_id": reservation["commander_id"],
                    "caster": reservation["commander_name"],
                    "recipient_id": reservation["target_commander_id"],
                }
            rows.append(detail)
        return {
            "items": rows,
            "recorded_transport_reservations": sorted(reservations),
            "recorded_transport_reservation_details": [
                {
                    "item_id": item_id,
                    "copies": len(bookings),
                    "casters": [int(row["commander_id"]) for row in bookings],
                }
                for item_id, bookings in sorted(reservations.items())
            ],
            "note": "an item already worn by a commander is not listed here; "
            "see get_battle_setup for what each one carries. A recorded "
            "Carrier Eagle or Teleport Item payload remains physically listed until "
            "materialize_orders reserves it.",
        }

    @reg.tool(
        "get_magic_economy",
        "Our gems by path: turn-start stock, the uncommitted amount remaining "
        "in the current orders file, and per-turn income from every visible "
        "magic site we own. Use uncommitted—not turn-start stock—when planning.",
    )
    def get_magic_economy(ctx: ToolContext) -> dict[str, Any]:
        return _magic_economy(ctx)

    @reg.tool(
        "get_mercenaries",
        "Every mercenary company in the auction: asking price, named leader, "
        "troops, contract holder, our standing bid already in the orders "
        "file, and any replacement bid recorded this turn. A contracted "
        "company can still be bid on.",
    )
    def get_mercenaries(ctx: ToolContext) -> dict[str, Any]:
        standing: dict[int, H2.MercenaryBid] = {}
        bid_read_note: str | None = None
        if ctx.h2_path is not None and ctx.h2_path.exists():
            try:
                standing = {
                    bid.slot: bid
                    for bid in H2.read_mercenary_bids(ctx.h2_path.read_bytes())
                }
            except H2.QueueWriteRefused as exc:
                bid_read_note = (
                    "The auction is visible, but standing bids could not be "
                    f"decoded safely: {exc}"
                )
        recorded = {
            int(row["slot"]): row
            for row in ctx.game_db.execute(
                "SELECT * FROM current_mercenary_bid_intent "
                "WHERE game_id=? AND turn=?",
                (ctx.game_id, ctx.turn),
            )
        }
        rows = []
        for index, m in enumerate(T.read_mercenaries(ctx.view.data)):
            minimum = MC.calculate_minimum_bid(
                m, ctx.nation_id, ctx.reference_db)
            unit = ctx.reference_db.execute(
                "SELECT name FROM units WHERE id=?", (m.unit_type_id,)
            ).fetchone()
            leader_ref = ctx.reference_db.execute(
                "SELECT bossname, com FROM mercenary WHERE name=?",
                (m.name,),
            ).fetchone()
            leader_type = (
                ctx.reference_db.execute(
                    "SELECT name FROM units WHERE id=?", (leader_ref["com"],)
                ).fetchone()
                if leader_ref is not None and leader_ref["com"]
                else None
            )
            row: dict[str, Any] = {
                "company": m.name,
                "auction_index": index,
                "asking_price": m.price,
                "minimum_bid": minimum.amount,
                "minimum_bid_percentage": minimum.percentage,
                "minimum_bid_basis": minimum.basis,
                "commander_id": m.commander_id,
                "leader": {
                    "commander_id": m.commander_id,
                    "name": leader_ref["bossname"] if leader_ref else None,
                    "unit_type_id": leader_ref["com"] if leader_ref else None,
                    "unit_type": leader_type["name"] if leader_type else None,
                },
                "troops": m.unit_count,
                "troop_type": unit["name"] if unit else m.unit_type_id,
                "available": m.available,
            }
            if not m.available:
                # Not a fog-of-war leak: the hire screen states this outright.
                # The player read "on contract to Machaka for 1 more month"
                # off it, which is what identified both fields.
                row["contracted_to"] = _nation_name(ctx, m.employer_nation_id)
                row["contract_months_left"] = m.contract_months
            current_bid = standing.get(index)
            if current_bid is not None:
                province = ctx.view.province(current_bid.province)
                row["standing_bid"] = {
                    "amount": current_bid.amount,
                    "arrival_province_id": current_bid.province,
                    "arrival_province": province.name if province else None,
                }
            pending = recorded.get(index)
            if pending is not None:
                province_id = pending["province_id"]
                province = (
                    ctx.view.province(int(province_id))
                    if province_id is not None
                    else None
                )
                row["recorded_bid"] = {
                    "action": "bid" if pending["amount"] is not None else "withdraw",
                    "amount": pending["amount"],
                    "arrival_province_id": province_id,
                    "arrival_province": province.name if province else None,
                    "rationale": pending["rationale"],
                }
            rows.append(row)
        result = {
            "companies": rows,
            "pricing": "minimum_bid is the exact legal floor for our nation; "
            "asking_price is the company's unadjusted stored price",
            "note": "rival bids are not in the turn file, so a winning bid "
            "cannot be computed — bidding above the asking price is a "
            "judgement call. The full bid is reserved now; if it loses, the "
            "game refunds that gold when the next turn arrives",
        }
        if bid_read_note is not None:
            result["standing_bids_note"] = bid_read_note
        return result

    @reg.tool(
        "find_province",
        "Look up provinces by name, or part of a name. Returns ids.",
        Param("name", "string", "full or partial province name"),
    )
    def find_province(ctx: ToolContext, name: str) -> list[dict]:
        q = name.strip().lower()
        if not q:
            raise ToolError("name must not be empty.")
        return [
            {"province_id": p.province_id, "name": p.name, "status": p.status}
            for p in ctx.view.provinces()
            if p.name and q in p.name.lower()
        ]

    # -- our forces ------------------------------------------------------

    @reg.tool(
        "list_commanders",
        "Our commanders: id, name, magic paths, the order currently in the "
        "orders file with typed targets and gem reservations, and any order "
        "we have recorded for them this turn.",
    )
    def list_commanders(ctx: ToolContext) -> dict[str, Any]:
        intent = {r["commander_id"]: r for r in _intent_rows(ctx)}
        cmds = _commanders(ctx)
        h2_data = (
            ctx.h2_path.read_bytes() if ctx.h2_path is not None and ctx.h2_path.exists() else None
        )
        h2_blocks = O.find_order_blocks(h2_data) if h2_data is not None else {}
        commander_by_runtime = (
            {
                struct.unpack_from("<I", h2_data, block.name_end)[0]: commander
                for commander in cmds
                if (block := h2_blocks.get(commander.commander_id)) is not None
            }
            if h2_data is not None
            else {}
        )
        unit_by_runtime = {
            unit.runtime_index: unit for unit in ctx.view.own_units()
            if not unit.is_mount
        }
        global_by_slot = {
            effect.slot: effect for effect in T.read_global_effects(ctx.view.data)
        }
        unit_states = {unit.instance_id: unit for unit in ctx.view.own_units()}
        out = []
        for c in cmds:
            row = intent.get(c.commander_id)
            raw_block = h2_blocks.get(c.commander_id)
            file_order = raw_block.order_name if raw_block is not None else c.order
            unit = (
                ctx.reference_db.execute(
                    "SELECT name, mapmove, leader, undeadleader, magicleader, "
                    "stealthy, spy, assassin, seduce, succubus, corrupt, "
                    "forgebonus, fixforgebonus, mastersmith, shapechange, "
                    "sailingshipsize, sailingmaxunitsize "
                    "FROM units WHERE id=?",
                    (c.type_id,),
                ).fetchone()
                if c.type_id is not None
                else None
            )
            entry: dict[str, Any] = {
                "commander_id": c.commander_id,
                "name": c.name,
                "order_in_file": file_order,
                "heroic_ability": (
                    {
                        "ability_id": c.heroic_ability_id,
                        "name": c.heroic_ability,
                    }
                    if c.heroic_ability_id is not None
                    else None
                ),
            }
            if unit is not None:
                entry["unit_type_id"] = c.type_id
                entry["unit_type"] = unit["name"]
                entry["hp"] = c.hp
                entry["age"] = c.age
                state = unit_states.get(c.unit_instance_id)
                if state is not None:
                    entry["experience"] = state.experience
                    entry["kills"] = state.kills
                    entry["afflictions"] = _afflictions(ctx, state.afflictions)
                    entry["has_fought"] = state.has_fought
                    entry["home_province_id"] = state.home_province_id
                    if state.home_province_id is not None:
                        home = ctx.view.province(state.home_province_id)
                        entry["home_province"] = home.name if home else None
                    if state.is_mercenary:
                        entry["is_mercenary"] = True
                entry["map_move"] = unit["mapmove"]
                # Worn items add to leadership and the term is not optional:
                # a Crown of Bones grants 150 undead where the chassis grants
                # none, which is how a mercenary Necromancer arrives leading
                # 125 Longdead.
                worn_rows = _effective_equipment_rows(ctx, c.commander_id)
                _cap = LD.leadership_for(
                    unit, worn_rows,
                    experience=(state.experience if state is not None else 0),
                )
                entry["leadership"] = {
                    "normal": _cap.normal,
                    "undead": _cap.undead,
                    "magic": _cap.magic,
                    "from_items": _cap.from_items or None,
                    "from_experience": _cap.from_experience,
                    "available_formations": sorted(
                        LD.available_formations(_cap.normal)),
                }
                abilities = {
                    "stealth": unit["stealthy"],
                    "spy": unit["spy"],
                    "assassin": unit["assassin"],
                    "seduction": (unit["seduce"] or unit["succubus"] or unit["corrupt"]),
                    "forge_bonus": unit["forgebonus"],
                    "fixed_forge_bonus": unit["fixforgebonus"],
                    "sailing_ship_size": unit["sailingshipsize"],
                    "sailing_max_unit_size": unit["sailingmaxunitsize"],
                }
                smith = int(unit["mastersmith"] or 0)
                if smith > 0:
                    abilities["master_smith"] = smith
                elif smith < 0:
                    abilities["inept_smith"] = abs(smith)
                abilities = {name: value for name, value in abilities.items() if value}
                if abilities:
                    entry["abilities"] = abilities
                alternate_type_id = int(unit["shapechange"] or 0)
                if alternate_type_id:
                    alternate = ctx.reference_db.execute(
                        "SELECT name FROM units WHERE id=?",
                        (alternate_type_id,),
                    ).fetchone()
                    if alternate is not None:
                        entry["shape_change"] = {
                            "available": True,
                            "instantaneous": True,
                            "consumes_turn": False,
                            "alternate_form": {
                                "unit_type_id": alternate_type_id,
                                "unit_type": alternate["name"],
                            },
                        }
            if file_order in O.RITUAL_ORDER_CODES and raw_block is not None:
                ritual = O.read_ritual_fields(h2_data, raw_block.name_end)
                in_file = (
                    _ritual_fields(
                        ctx, c, ritual, commander_by_runtime, unit_by_runtime,
                        global_by_slot
                    )
                    if ritual is not None
                    else {"parameter": c.order_parameter}
                )
            elif file_order == "forge_magic_item" and raw_block is not None:
                forge = O.read_forge_fields(h2_data, raw_block.name_end)
                in_file = (
                    _forge_fields(ctx, forge)
                    if forge is not None
                    else {"parameter": c.order_parameter}
                )
            else:
                in_file = _parameter_fields(
                    ctx,
                    file_order,
                    raw_block.parameter if raw_block is not None else c.order_parameter,
                )
            if in_file.get("destination") is None:
                in_file.pop("destination", None)
            if in_file:
                entry["order_parameter_in_file"] = in_file
            # Omitted when absent rather than sent as null. On a local model
            # the context is the scarce resource, and ten nulls per commander
            # is a measurable share of it.
            if c.paths:
                entry["paths"] = c.paths
            if c.is_pretender:
                entry["is_pretender"] = True
            if c.province_id:
                entry["province_id"] = c.province_id
                entry["province_name"] = c.province_name
                entry["troops_led"] = c.troops
            else:
                entry["province_id"] = None
            if row:
                entry["recorded_order"] = row["order_name"]
                typed = _intent_parameter(ctx, row)
                if typed.get("destination") is None:
                    typed.pop("destination", None)
                if typed:
                    entry["recorded_parameter"] = typed
            out.append(entry)
        unplaced = [c.name for c in cmds if not c.province_id]
        result: dict[str, Any] = {"commanders": out}
        if unplaced:
            # Said once rather than on every row: the same note per commander
            # cost ~800 characters of a small model's context and told it
            # nothing it had not already read.
            result["note"] = (
                f"{len(unplaced)} commander(s) could not be joined to a unique "
                ".trn stat record; their locations and unit traits are omitted."
            )
        return result

    @reg.tool(
        "list_units",
        "Our units, grouped by province and type. Mounts are excluded — they "
        "are part of their rider, not separate soldiers. Pass "
        "include_instances=true when selecting particular troops for "
        "assign_troops, detach_troops or create_squad; that reads the current "
        "orders file and includes each "
        "unit's assignment, condition and home province (or mercenary origin).",
        Param("province_id", "integer", "restrict to one province", required=False),
        Param(
            "include_instances",
            "boolean",
            "return individual instance ids",
            required=False,
            default=False,
        ),
    )
    def list_units(
        ctx: ToolContext, province_id: int | None = None, include_instances: bool = False
    ) -> list[dict]:
        if include_instances:
            if ctx.h2_path is None or not ctx.h2_path.exists():
                raise ToolError("no .2h file to read current troop assignments from.")
            return _h2_unit_instances(ctx, ctx.h2_path.read_bytes(), province_id)
        units = _own_troops(ctx)
        if province_id is not None:
            units = [u for u in units if u.province_id == province_id]
        groups: dict[int, list] = {}
        for u in units:
            groups.setdefault(u.province_id, []).append(u)
        prov = {p.province_id: p for p in ctx.view.provinces()}
        return [
            {
                "province_id": pid,
                "province_name": prov[pid].name if pid in prov else None,
                "count": len(us),
                "types": _type_counts(ctx, us),
            }
            for pid, us in sorted(groups.items())
        ]

    # -- reference -------------------------------------------------------

    @reg.tool(
        "lookup_unit",
        "Look up a unit type in the reference data by name or id: cost, "
        "stats, magic paths, and special abilities. Use this instead of "
        "recalling stats from memory.",
        Param("name", "string", "unit name or part of one", required=False),
        Param("unit_id", "integer", "exact unit type id", required=False),
        Param(
            "detail",
            "string",
            "summary or full extracted detail",
            required=False,
            default="summary",
            choices=("summary", "full"),
        ),
    )
    def lookup_unit(
        ctx: ToolContext,
        name: str | None = None,
        unit_id: int | None = None,
        detail: str = "summary",
    ) -> list[dict]:
        rows = _ref_lookup(ctx, "units", name, unit_id, limit=10)
        costs = _observed_costs(ctx)
        return [_unit_brief(ctx, r, costs.get(r["id"]), full=detail == "full") for r in rows]

    @reg.tool(
        "lookup_spell",
        "Look up a spell: school, research level, paths required, gem cost.",
        Param("name", "string", "spell name or part of one", required=False),
        Param("spell_id", "integer", "exact spell id", required=False),
        Param(
            "detail",
            "string",
            "summary or full extracted effect detail",
            required=False,
            default="summary",
            choices=("summary", "full"),
        ),
    )
    def lookup_spell(
        ctx: ToolContext,
        name: str | None = None,
        spell_id: int | None = None,
        detail: str = "summary",
    ) -> list[dict]:
        rows = _ref_lookup(ctx, "spells", name, spell_id, limit=10)
        return [_spell_brief(ctx, row, full=detail == "full") for row in rows]

    @reg.tool(
        "lookup_item",
        "Look up a magic item: construction level, paths to forge, effects.",
        Param("name", "string", "item name or part of one", required=False),
        Param("item_id", "integer", "exact item id", required=False),
        Param(
            "detail",
            "string",
            "summary or full weapon/armor detail",
            required=False,
            default="summary",
            choices=("summary", "full"),
        ),
    )
    def lookup_item(
        ctx: ToolContext,
        name: str | None = None,
        item_id: int | None = None,
        detail: str = "summary",
    ) -> list[dict]:
        rows = _ref_lookup(ctx, "items", name, item_id, limit=10)
        return [_item_brief(ctx, row, full=detail == "full") for row in rows]

    @reg.tool(
        "lookup_magic_site",
        "Look up a magic site by name or id: gem income, province effects, "
        "recruitables, ritual-range bonuses and other extracted effects.",
        Param("name", "string", "site name or part of one", required=False),
        Param("magic_site_id", "integer", "exact magic-site id", required=False),
        Param(
            "detail",
            "string",
            "summary or full extracted detail",
            required=False,
            default="summary",
            choices=("summary", "full"),
        ),
    )
    def lookup_magic_site(
        ctx: ToolContext,
        name: str | None = None,
        magic_site_id: int | None = None,
        detail: str = "summary",
    ) -> list[dict]:
        rows = _ref_lookup(ctx, "magic_sites", name, magic_site_id, limit=10)
        return [_site_brief(ctx, row, full=detail == "full") for row in rows]

    @reg.tool(
        "lookup_weapon",
        "Look up a mundane or item weapon and its decoded attack, defence, "
        "length, attacks, ammunition and primary effect.",
        Param("name", "string", "weapon name or part of one", required=False),
        Param("weapon_id", "integer", "exact weapon id", required=False),
    )
    def lookup_weapon(
        ctx: ToolContext, name: str | None = None, weapon_id: int | None = None
    ) -> list[dict]:
        rows = _ref_lookup(ctx, "weapons", name, weapon_id, limit=10)
        return [_weapon_brief(ctx, row) for row in rows]

    @reg.tool(
        "lookup_armor",
        "Look up mundane or item armor: defence, encumbrance, resource cost "
        "and protection by hit-location zone.",
        Param("name", "string", "armor name or part of one", required=False),
        Param("armor_id", "integer", "exact armor id", required=False),
    )
    def lookup_armor(
        ctx: ToolContext, name: str | None = None, armor_id: int | None = None
    ) -> list[dict]:
        rows = _ref_lookup(ctx, "armors", name, armor_id, limit=10)
        return [_armor_brief(ctx, row) for row in rows]

    @reg.tool(
        "lookup_nation",
        "Look up public static nation data by name or reference id. Full "
        "detail includes extracted national attributes and native fort, "
        "non-fort and coastal recruitment rosters; it never reads another "
        "nation's current turn state.",
        Param("name", "string", "nation name or part of one", required=False),
        Param("reference_id", "integer", "exact static nation reference id", required=False),
        Param(
            "detail",
            "string",
            "summary or full extracted detail",
            required=False,
            default="summary",
            choices=("summary", "full"),
        ),
    )
    def lookup_nation(
        ctx: ToolContext,
        name: str | None = None,
        reference_id: int | None = None,
        detail: str = "summary",
    ) -> list[dict]:
        rows = _ref_lookup(ctx, "nations", name, reference_id, limit=10)
        return [_nation_brief(ctx, row, full=detail == "full") for row in rows]

    @reg.tool(
        "list_recruitable",
        "What we can recruit in a province: our nation's own list plus "
        "anything the magic sites there unlock. Costs are the ones seen in "
        "game; a cost of null means we have not observed it yet.",
        Param("province_id", "integer", "a province we own with a fort", required=False),
    )
    def list_recruitable(ctx: ToolContext, province_id: int | None = None) -> dict:
        return _recruitable(ctx, province_id)

    # -- our own notes ---------------------------------------------------

    @reg.tool(
        "read_scratchpad",
        "Our notes for this game: plans, threats, and things to remember across turns.",
        Param("tag", "string", "filter by tag, e.g. 'plan'", required=False),
        Param("limit", "integer", "max notes", required=False, default=20),
    )
    def read_scratchpad(ctx: ToolContext, tag: str | None = None, limit: int = 20) -> list[dict]:
        sql = ("SELECT id,turn,tag,note,status,pinned,created_at,updated_at "
               "FROM scratchpad WHERE game_id=?")
        args: list[Any] = [ctx.game_id]
        if tag:
            sql += " AND tag=?"
            args.append(tag)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 100)))
        return [dict(r) for r in ctx.game_db.execute(sql, args)]

    @reg.tool(
        "read_player_guidance",
        "The player's authored Dominions playbook: gotchas and strategic "
        "guidance with explicit keyword triggers. Relevant entries are "
        "inserted automatically; call this to browse or search the full list.",
        Param("search", "string", "match title, guidance, tags, or triggers",
              required=False),
        Param("limit", "integer", "max entries", required=False, default=50),
    )
    def read_player_guidance(
        ctx: ToolContext, search: str | None = None, limit: int = 50
    ) -> list[dict]:
        sql = (
            "SELECT id,game_id,nation_id,title,guidance,tags_json,triggers_json,"
            "priority,always_include,enabled,created_by,created_at,updated_at "
            "FROM playbook_entry WHERE enabled=1 "
            "AND (game_id IS NULL OR game_id=?) "
            "AND (nation_id IS NULL OR nation_id=?)"
        )
        args: list[Any] = [ctx.game_id, ctx.nation_id]
        if search:
            sql += (
                " AND (title LIKE ? OR guidance LIKE ? OR tags_json LIKE ? "
                "OR triggers_json LIKE ?)"
            )
            match = f"%{search}%"
            args.extend([match] * 4)
        sql += " ORDER BY priority DESC,id LIMIT ?"
        args.append(max(1, min(limit, 200)))
        rows = []
        for row in ctx.game_db.execute(sql, args):
            item = dict(row)
            item["tags"] = json.loads(item.pop("tags_json") or "[]")
            item["triggers"] = json.loads(item.pop("triggers_json") or "[]")
            item["always_include"] = bool(item["always_include"])
            item["enabled"] = bool(item["enabled"])
            rows.append(item)
        return rows

    @reg.tool(
        "read_lessons",
        "Durable lessons about Dominions learned across games — rules, "
        "gotchas, and things confirmed by experiment. Consult this before "
        "relying on your own knowledge of the game.",
        Param("topic", "string", "filter by topic", required=False),
        Param("search", "string", "match text in the lesson", required=False),
        Param("limit", "integer", "max lessons", required=False, default=25),
    )
    def read_lessons(
        ctx: ToolContext, topic: str | None = None, search: str | None = None, limit: int = 25
    ) -> list[dict]:
        sql = "SELECT topic, lesson, evidence FROM lessons WHERE 1=1"
        args: list[Any] = []
        if topic:
            sql += " AND topic=?"
            args.append(topic)
        if search:
            sql += " AND lesson LIKE ?"
            args.append(f"%{search}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 100)))
        rows = [dict(r) for r in ctx.game_db.execute(sql, args)]
        if not rows and (topic or search):
            raise ToolError(
                "no lesson matches that. Call read_lessons with no arguments "
                "to see what topics exist."
            )
        return rows

    @reg.tool(
        "list_battle_options",
        "Verified stances, targets, scripted-round choices, gem paths and "
        "equipment slots accepted by the battle setup tools.",
    )
    def list_battle_options(ctx: ToolContext) -> dict[str, Any]:
        return {
            "stances": sorted(O.STANCE_CODES),
            "commander_stances": sorted(O.COMMANDER_STANCES),
            "squad_stances": sorted(O.SQUAD_STANCES),
            "targets": sorted(O.TARGET_CODES_V2),
            "targetable_stances": sorted(O.TARGETABLE_STANCES),
            "equipment_slots": sorted(O.EQUIPMENT_SLOTS),
            "single_round_choices": sorted(O.SINGLE_ROUND_CODES),
            "carried_gem_paths": list(O.CARRIED_GEM_PATHS),
            "placement": {
                "x": [-O.PLACEMENT_EDGE, O.PLACEMENT_EDGE],
                "y": [-O.PLACEMENT_EDGE, O.PLACEMENT_EDGE],
                "centre": [0, 0],
            },
            "formations": dict(O.FORMATION_CODES),
            "formation_rule": {
                "basic_below_normal_leadership": (
                    LD.ADVANCED_FORMATION_MIN_LEADERSHIP),
                "basic": sorted(LD.BASIC_FORMATIONS),
                "advanced_at_or_above_threshold": sorted(
                    LD.ADVANCED_FORMATIONS),
                "uses_final_leadership": True,
            },
            "note": "a commander has their own battle order plus one per squad "
                "they lead. Box and Skirmish are available below normal "
                "Leadership 80; all five formations unlock at 80 after "
                "experience and worn/pending items. Squad slots "
                "are 0-4 and may be sparse; use "
                "get_battle_setup rather than assuming 0..count-1. 'none' "
                "explicitly clears a stance or target; omitting target preserves it.",
        }

    @reg.tool(
        "get_battle_setup",
        "How a commander is currently set to fight, read from the orders file: "
        "their own stance and target, each squad's, and what they carry.",
        Param("commander_id", "integer", "id from list_commanders"),
    )
    def get_battle_setup(ctx: ToolContext, commander_id: int) -> dict[str, Any]:
        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError("no .2h file to read battle orders from.")
        data = ctx.h2_path.read_bytes()
        blocks = O.find_order_blocks(data)
        if commander_id not in blocks:
            raise ToolError(
                f"no order block for commander {commander_id}. Call list_commanders for ids."
            )
        end = blocks[commander_id].name_end
        commander = next(
            (candidate for candidate in _commanders(ctx)
             if candidate.commander_id == commander_id),
            None,
        )
        leadership = (
            _effective_commander_leadership(ctx, commander)
            if commander is not None else None
        )
        stance, target = O.read_own_battle_order(data, end)
        names = {v: k for k, v in O.STANCE_CODES.items()}
        targets = {v: k for k, v in O.TARGET_CODES_V2.items()}
        equipment = {}
        for slot, item_id in O.read_equipment(data, end).items():
            row = ctx.reference_db.execute(
                "SELECT name FROM items WHERE id=?", (item_id,)
            ).fetchone()
            equipment[slot] = {"item_id": item_id, "name": row["name"] if row else None}
        units = O.read_h2_units(data, ctx.nation_id)
        by_offset = {u.offset: u for u in units}
        type_names = _unit_type_names(ctx, {u.type_id for u in units if not u.is_mount})
        squads = []
        for squad in O.read_squad_orders(data, end):
            token = squad["token"]
            members = []
            for unit in units:
                if unit.is_mount or unit.warband != token:
                    continue
                mount = by_offset.get(unit.offset + U.RECORD_SIZE)
                members.append(
                    {
                        "instance_id": unit.instance_id,
                        "unit_type_id": unit.type_id,
                        "name": type_names.get(unit.type_id),
                        "includes_mount": bool(mount and mount.is_mount),
                    }
                )
            squads.append(
                {
                    "slot": squad["slot"],
                    "squad_id": squad["squad_id"],
                    "stance": names.get(squad["stance"], f"unknown({squad['stance']})"),
                    "target": targets.get(squad["target"], f"unknown({squad['target']})"),
                    "formation": squad["formation"],
                    "formation_name": O.FORMATION_NAMES.get(squad["formation"]),
                    "position": {"x": squad["x"], "y": squad["y"]},
                    "units": members,
                }
            )
        return {
            "commander_id": commander_id,
            "name": blocks[commander_id].commander_name,
            "own_stance": names.get(stance, f"unknown({stance})"),
            "own_target": targets.get(target, f"unknown({target})"),
            "squads": squads,
            "equipment": equipment,
            "carried_gems": {k: v for k, v in O.read_carried_gems(data, end).items() if v},
            "single_round_orders": _spell_queue(ctx, data, end),
            "final_normal_leadership": (
                leadership.normal if leadership is not None else None),
            "available_formations": (
                sorted(LD.available_formations(leadership.normal))
                if leadership is not None else None),
        }

    @reg.tool(
        "get_recruitment_queue",
        "What is queued for recruitment this turn and what it costs, read from the orders file.",
    )
    def get_recruitment_queue(ctx: ToolContext) -> dict[str, Any]:
        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError("no .2h file to read the queue from.")
        from dom6_assistant.file_reader.formats import h2

        data = ctx.h2_path.read_bytes()
        try:
            order_file_provinces = ctx.view.order_file_province_ids(data)
        except VisibilityError as exc:
            raise ToolError(str(exc))
        parsed = h2.parse_bytes(data, owned_provinces=list(order_file_provinces))
        names = {p.province_id: p.name for p in ctx.view.provinces()}
        by_province = []
        for queue in parsed.queues:
            rows = []
            for r in queue.recruits:
                found = ctx.reference_db.execute(
                    "SELECT name FROM units WHERE id=?", (r.unit_type_id,)
                ).fetchone()
                rows.append(
                    {
                        "unit_type_id": r.unit_type_id,
                        "name": found["name"] if found else None,
                        "gold": r.gold,
                    }
                )
            by_province.append(
                {
                    "province_id": queue.province_id,
                    "province_name": names.get(queue.province_id),
                    "queued": rows,
                    "gold": queue.gold,
                }
            )
        return {
            "by_province": by_province,
            "gold_committed": parsed.gold_spent,
            "owned_provinces_without_order_block": sorted(set(names) - set(order_file_provinces)),
            "note": "province-local queue blocks are sparse in the inherited "
            "orders file. A province can be owned without such a block; call queue_recruits to "
            "replace an available queue and materialize_orders to "
            "write it to the .2h",
        }

    @reg.tool(
        "get_province_defence",
        "Province defence in every province we own: turn-start level, the "
        "target currently saved in the orders file, gold committed this turn, "
        "and any newer recorded target awaiting materialization.",
    )
    def get_province_defence(ctx: ToolContext) -> dict[str, Any]:
        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError("no .2h file to read province defence from.")
        from dom6_assistant.file_reader.formats import h2

        provinces = sorted(ctx.view.own_provinces(), key=lambda province: province.province_id)
        data = ctx.h2_path.read_bytes()
        try:
            order_file_provinces = set(ctx.view.order_file_province_ids(data))
        except VisibilityError as exc:
            raise ToolError(str(exc))
        recorded = {
            row["province_id"]: row
            for row in ctx.game_db.execute(
                "SELECT * FROM current_province_defence_intent WHERE game_id=? AND turn=?",
                (ctx.game_id, ctx.turn),
            )
        }
        rows = []
        for province in provinces:
            block_available = province.province_id in order_file_provinces
            target = None
            if block_available:
                try:
                    target = h2.province_defence(
                        data, province.province_id, sorted(order_file_provinces)
                    )
                except h2.QueueWriteRefused as exc:
                    raise ToolError(str(exc))
            turn_start = province.province_defense
            committed = (
                h2.province_defence_cost(target) - h2.province_defence_cost(turn_start)
                if turn_start is not None and target is not None and target >= turn_start
                else None
            )
            intent = recorded.get(province.province_id)
            rows.append(
                {
                    "province_id": province.province_id,
                    "province_name": province.name,
                    "turn_start": turn_start,
                    "orders_block_available": block_available,
                    "target_in_orders_file": target,
                    "points_bought": (
                        target - turn_start
                        if target is not None and turn_start is not None
                        else None
                    ),
                    "gold_committed": committed,
                    "recorded_target": intent["target"] if intent else None,
                    "recorded_rationale": intent["rationale"] if intent else None,
                }
            )
        return {
            "provinces": rows,
            "maximum_purchasable": h2.MAX_PROVINCE_DEFENCE,
            "gold_uncommitted_in_orders_file": h2.gold_remaining(data),
            "cost_rule": "each added point costs its new defence level",
            "note": "call set_province_defence to record a complete target, "
            "then materialize_orders to write it. Province-local blocks are "
            "sparse, so an owned province can lack one in the inherited "
            ".2h; its turn-start defence remains visible but no saved "
            "target can be read or safely written yet",
        }

    @reg.tool(
        "get_province_economics",
        "A province's income, resources, recruitment points, supplies and "
        "holy points. Income, resources, supplies and holy points for "
        "provinces we own are computed from the "
        "current turn and explicitly labelled if conditional; the other "
        "panel-only figures are historical observations.",
        Param("province_id", "integer", "province id", required=False),
    )
    def get_province_economics(ctx: ToolContext, province_id: int | None = None) -> dict[str, Any]:
        sql = (
            "SELECT province_id, turn, income, resources, recruit_points, "
            "commander_points, holy_points, supplies, source "
            "FROM province_economics"
        )
        args: list[Any] = []
        if province_id is not None:
            sql += " WHERE province_id=?"
            args.append(province_id)
        sql += " ORDER BY province_id, turn DESC"
        rows = [dict(r) for r in ctx.game_db.execute(sql, args)]
        recorded: dict[int, dict] = {}
        for row in rows:
            recorded.setdefault(row["province_id"], row)
        computed_resources = owned_province_resource_budgets(ctx)
        computed_income = owned_province_income_budgets(ctx)
        computed_supplies = owned_province_supply_budgets(ctx)
        computed_holy = owned_province_holy_budgets(ctx)
        names = {p.province_id: p.name for p in ctx.view.provinces()}
        out = []
        province_ids = (
            {province_id}
            if province_id is not None
            else set(recorded)
            | set(computed_resources)
            | set(computed_income)
            | set(computed_supplies)
            | set(computed_holy)
        )
        for pid in sorted(province_ids):
            saved = recorded.get(pid)
            resource_budget = computed_resources.get(pid)
            income_budget = computed_income.get(pid)
            supply_budget = computed_supplies.get(pid)
            holy_budget = computed_holy.get(pid)
            if (
                saved is None
                and resource_budget is None
                and income_budget is None
                and supply_budget is None
                and holy_budget is None
            ):
                continue
            row = (
                dict(saved)
                if saved is not None
                else {
                    "province_id": pid,
                    "turn": None,
                    "income": None,
                    "resources": None,
                    "recruit_points": None,
                    "commander_points": None,
                    "holy_points": None,
                    "supplies": None,
                    "source": None,
                }
            )
            row["province_name"] = names.get(row["province_id"])
            row["stale"] = row["turn"] is not None and row["turn"] != ctx.turn
            if income_budget is not None:
                row["income_recorded"] = row["income"]
                row["income"] = income_budget.total
                row["income_source"] = income_budget.source
                row["income_exact"] = income_budget.exact
                if income_budget.note:
                    row["income_note"] = income_budget.note
                if (
                    row["turn"] == ctx.turn
                    and row["income_recorded"] is not None
                    and income_budget.total is not None
                ):
                    row["income_vs_panel"] = income_budget.total - row["income_recorded"]
            if resource_budget is not None:
                row["resources_recorded"] = row["resources"]
                row["resources"] = resource_budget.total
                row["resources_source"] = resource_budget.source
                row["resources_exact"] = resource_budget.exact
                if resource_budget.note:
                    row["resources_note"] = resource_budget.note
                if (
                    row["turn"] == ctx.turn
                    and row["resources_recorded"] is not None
                    and resource_budget.total is not None
                ):
                    row["resources_vs_panel"] = resource_budget.total - row["resources_recorded"]
            if supply_budget is not None:
                row["supplies_recorded"] = row["supplies"]
                row["supplies"] = supply_budget.total
                row["supplies_source"] = supply_budget.source
                row["supplies_exact"] = supply_budget.exact
                if supply_budget.note:
                    row["supplies_note"] = supply_budget.note
                if (
                    row["turn"] == ctx.turn
                    and row["supplies_recorded"] is not None
                    and supply_budget.total is not None
                ):
                    row["supplies_vs_panel"] = supply_budget.total - row["supplies_recorded"]
            if holy_budget is not None:
                row["holy_points_recorded"] = row["holy_points"]
                row["holy_points"] = holy_budget.total
                row["holy_points_source"] = holy_budget.source
                row["holy_points_exact"] = holy_budget.exact
                row["base_dominion"] = holy_budget.base_dominion
                row["maximum_dominion"] = holy_budget.maximum_dominion
                row["own_temples"] = holy_budget.own_temples
                row["shared_temples"] = holy_budget.shared_temples
                row["disciple_nations"] = holy_budget.disciple_nations
                row["temple_holy_point_bonus"] = holy_budget.temple_holy_point_bonus
                if holy_budget.note:
                    row["holy_points_note"] = holy_budget.note
                if (
                    row["turn"] == ctx.turn
                    and row["holy_points_recorded"] is not None
                    and holy_budget.total is not None
                ):
                    row["holy_points_vs_panel"] = holy_budget.total - row["holy_points_recorded"]
            out.append(row)
        if not out:
            raise ToolError(
                "no economics are available for that province. Current income, "
                "resource, supply and holy-point calculation is available only for provinces we "
                "own; no panel observation has been recorded for this one."
            )
        return {
            "provinces": out,
            "current_turn": ctx.turn,
            "note": "income, resources, supplies and holy points for owned "
            "provinces are current computed values. stale=true "
            "applies only to remaining historical panel fields",
        }

    @reg.tool(
        "plan_recruitment",
        "What a province's queued recruits cost, against what it can afford. "
        "Queueing more than a province can build is allowed and useful — the "
        "surplus carries to next turn — so this reports what will likely build "
        "now and what will wait, rather than refusing anything.",
        Param("province_id", "integer", "a province we own with a fort"),
    )
    def plan_recruitment(ctx: ToolContext, province_id: int) -> dict[str, Any]:
        from dom6_assistant.file_reader.formats import h2

        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError("no .2h file to read the queue from.")
        owned = [p.province_id for p in ctx.view.own_provinces()]
        if province_id not in owned:
            raise ToolError(f"province {province_id} is not ours: {owned}")
        parsed = h2.parse(ctx.h2_path, owned_provinces=owned)
        queue = next((q for q in parsed.queues if q.province_id == province_id), None)
        rows, running, running_points = [], 0, 0
        points_complete = True
        for order, r in enumerate(queue.recruits if queue else []):
            cost = resource_cost_for(ctx.reference_db, r.unit_type_id) or 0
            point_cost = recruitment_point_cost_for(ctx.reference_db, r.unit_type_id)
            running += cost
            if point_cost is None:
                points_complete = False
            else:
                running_points += point_cost
            found = ctx.reference_db.execute(
                "SELECT name FROM units WHERE id=?", (r.unit_type_id,)
            ).fetchone()
            entry = {
                "position": order,
                "unit_type_id": r.unit_type_id,
                "name": found["name"] if found else None,
                "gold": r.gold,
                "resources": cost,
                "resources_cumulative": running,
                "recruit_points": point_cost,
                "recruit_points_cumulative": (running_points if points_complete else None),
            }
            rows.append(entry)

        out: dict[str, Any] = {
            "province_id": province_id,
            "queued": rows,
            "gold_total": sum(r.gold for r in queue.recruits) if queue else 0,
            "resources_total": running,
            "recruit_points_total": (running_points if points_complete else None),
        }
        # Recruitment points and resources are both computed from current save
        # inputs for provinces we own; neither relies on a panel observation.
        province = next((p for p in ctx.view.provinces() if p.province_id == province_id), None)
        if province is not None:
            points = estimate_recruitment_points(
                province.population,
                fort_type=province.fort_type,
                is_ours=True,
                order_scale=province.order_scale,
            )
            if points["exact"]:
                out["recruit_points_available"] = points["points"]
                out["commander_points_available"] = points["commander_points"]
                out["recruit_points_source"] = "computed from the .trn"
        budget = owned_province_resource_budget(ctx, province_id)
        out["resources_available"] = budget.total
        out["resources_source"] = budget.source
        out["resources_exact"] = budget.exact
        if budget.note:
            out["resources_note"] = budget.note
        if budget.total is not None:
            for entry in rows:
                entry["fits_this_turn"] = entry["resources_cumulative"] <= budget.total
        holy_budget = owned_province_holy_budgets(ctx)[province_id]
        out["holy_points_available"] = holy_budget.total
        out["holy_points_source"] = holy_budget.source
        out["holy_points_exact"] = holy_budget.exact
        if holy_budget.note:
            out["holy_points_note"] = holy_budget.note

        out.update(_remainders(ctx, province_id, queue, out))
        out["note"] = _budget_note(out)
        return out

    @reg.tool(
        "get_nation_overview",
        "Our own nation at a glance: pretender, prophet, magic item treasury "
        "and who is in the hall of fame.",
    )
    def get_nation_overview(ctx: ToolContext) -> dict[str, Any]:
        from dom6_assistant.agent.call_god import observe as observe_call_god

        data = ctx.view.data
        # Our OWN names come from the .2h, not from the .trn name table: that
        # table holds foreign commanders we have learned about, so our own
        # pretender is simply absent from it and looking him up there returns
        # None. Sugaar is commander 297 in both, named in only one of them.
        own_commanders = {c.commander_id: c for c in ctx.view.own_commanders(ctx.h2_path)}
        ours = {cid: commander.name for cid, commander in own_commanders.items()}
        names = C.read_commander_names(data)
        pretenders = C.pretender_ids(data)
        prophet = None
        for nation_id, _gold, _gems, header in T.read_nation_roster(data):
            if nation_id == ctx.nation_id:
                prophet = T.read_prophet_id(data, header)
                break
        pretender_id = pretenders.get(ctx.nation_id)
        pretender_state = ctx.view.own_pretender_state(ctx.h2_path)
        pretender_name = (
            (ours.get(pretender_id) if pretender_id else None)
            or pretender_state["name"]
        )
        call_god_tracking = observe_call_god(ctx)
        titles = T.read_pretender_titles(data, ours)
        holy_budgets = owned_province_holy_budgets(ctx)
        holy = next(iter(holy_budgets.values()), None)
        nation = ctx.reference_db.execute(
            "SELECT id, name, epithet, abbreviation, era FROM nations WHERE id=?", (ctx.nation_id,)
        ).fetchone()
        nation_attributes = [
            {"attribute": row["attribute"], "name": row["name"], "raw_value": row["raw_value"]}
            for row in ctx.reference_db.execute(
                "SELECT a.attribute, a.raw_value, k.name "
                "FROM attributes_by_nation a "
                "JOIN attribute_keys k ON k.number=a.attribute "
                "WHERE a.nation_number=? AND k.name LIKE '%{Ntn:%' "
                "ORDER BY a.attribute",
                (ctx.nation_id,),
            )
        ]
        return {
            "nation_id": ctx.nation_id,
            "nation": (dict(nation) if nation is not None else None),
            "known_nation_attributes": nation_attributes,
            "pretender": {
                "commander_id": pretender_id,
                "name": pretender_name,
                "title": titles.get(pretender_name),
                "status": pretender_state["status"],
                "dead": pretender_state["dead"],
                "basis": pretender_state["basis"],
                "call_god_recall": (
                    {
                        "ordinary_target_points": 50,
                        "exact_accumulated_points": None,
                        **(call_god_tracking or {}),
                        "note": (
                            "Dominions does not show the exact accumulated "
                            "randomized total to players. These bounds are "
                            "player-equivalent bookkeeping from observed "
                            "orders: an ordinary Holy H priest contributes "
                            "H-1 through H+1 points per resolved turn. A "
                            "complete possible total is reported only when "
                            "every planning turn since the observed death was "
                            "tracked. Disciple status and special Call God "
                            "bonuses can modify the requirement or contribution."
                        ),
                    }
                    if pretender_state["dead"] is True else None
                ),
            },
            "dominion_and_holy_recruitment": (
                {
                    "base_dominion": holy.base_dominion,
                    "maximum_dominion": holy.maximum_dominion,
                    "holy_points_per_administered_province": holy.total,
                    "own_temples": holy.own_temples,
                    "shared_temples": holy.shared_temples,
                    "disciple_nations": holy.disciple_nations,
                    "temple_holy_point_bonus": holy.temple_holy_point_bonus,
                    "exact": holy.exact,
                    "source": holy.source,
                    **({"note": holy.note} if holy.note else {}),
                }
                if holy is not None
                else None
            ),
            "prophet": (
                {"commander_id": prophet, "name": ours[prophet]} if prophet in ours else None
            ),
            "item_treasury": len(_treasury(ctx)),
            "gold": {
                "turn_start": ctx.view.parsed.player_gold,
                "uncommitted_in_orders_file": _gold_uncommitted(ctx),
            },
            "magic_economy": _magic_economy(ctx),
            # The Hall of Fame is a screen the player can open, and it names
            # foreign commanders — so serving all ten is what they already see,
            # not a leak. Rank is the list order. Names we do not hold are left
            # null rather than filled with the id.
            "hall_of_fame": [
                {
                    "rank": i,
                    "commander_id": cid,
                    "name": ours.get(cid) or names.get(cid),
                    "ours": cid in ours,
                    # The exact trait is read only from our own commander record;
                    # foreign Hall entries do not become a back door around the
                    # visibility boundary.
                    "heroic_ability": (
                        {
                            "ability_id": own_commanders[cid].heroic_ability_id,
                            "name": own_commanders[cid].heroic_ability,
                        }
                        if cid in own_commanders
                        and own_commanders[cid].heroic_ability_id is not None
                        else None
                    ),
                }
                for i, cid in enumerate(T.read_hall_of_fame(data), start=1)
            ],
        }

    @reg.tool(
        "get_battle_reports",
        "Battle Summaries delivered with this turn: starting forces by unit "
        "type and conservative outcome totals. Enemy per-instance state is "
        "never exposed; unresolved casualties remain unknown.",
    )
    def get_battle_reports(ctx: ToolContext) -> dict[str, Any]:
        reports = ctx.view.battle_reports()
        type_ids = (
            {
                type_id
                for report in reports
                for force in report.forces
                for type_id, _count in force.unit_types
            }
            | {type_id for report in reports for type_id, _kills in report.kills_by_our_type}
            | {
                type_id
                for report in reports
                for force in report.forces
                for type_id, _count in force.deaths_by_type
            }
            | {
                type_id
                for report in reports
                for force in report.forces
                for type_id, _kills in force.kills_by_type
            }
            | {
                type_id
                for report in reports
                for group in report.exceptional_groups
                for type_id, _count in group.unit_types
            }
        )
        names = _unit_type_names(ctx, type_ids)
        out = []
        for report in reports:
            province = (
                ctx.view.province(report.province_id) if report.province_id is not None else None
            )
            forces = []
            for force in sorted(report.forces, key=lambda found: found.nation_id != ctx.nation_id):
                nation_name = (
                    "Independents" if force.nation_id == 0 else _nation_name(ctx, force.nation_id)
                )
                forces.append(
                    {
                        "nation_id": force.nation_id,
                        "nation": nation_name,
                        "ours": force.nation_id == ctx.nation_id,
                        "role": force.role,
                        "total": force.total_units,
                        "units": [
                            {"unit_type_id": type_id, "name": names.get(type_id), "count": count}
                            for type_id, count in force.unit_types
                        ],
                        "mount_records_not_counted_separately": (force.mounts_not_counted),
                        "survivors_after_combat": force.survivors_total,
                        "deaths_total": force.deaths_total,
                        "deaths_by_type": [
                            {"unit_type_id": type_id, "name": names.get(type_id), "deaths": count}
                            for type_id, count in force.deaths_by_type
                        ],
                        "kills_credited_by_type": [
                            {"unit_type_id": type_id, "name": names.get(type_id), "kills": kills}
                            for type_id, kills in force.kills_by_type
                        ],
                        "routed_survivors": force.routed_survivors_total,
                    }
                )
            exceptional_groups = [
                {
                    "group_id": group.group_id,
                    "total": group.total_units,
                    "role": group.role,
                    "attribution_exact": group.attribution_exact,
                    "survivors_after_combat": group.survivors_total,
                    "deaths_total": group.deaths_total,
                    "units": [
                        {"unit_type_id": type_id, "name": names.get(type_id), "count": count}
                        for type_id, count in group.unit_types
                    ],
                    "deaths_by_type": [
                        {
                            "unit_type_id": type_id,
                            "name": names.get(type_id),
                            "deaths": count,
                        }
                        for type_id, count in group.deaths_by_type
                    ],
                    "note": group.attribution_note,
                }
                for group in report.exceptional_groups
            ]
            winner_nation = (
                "Independents"
                if report.winner_nation_id == 0
                else (
                    _nation_name(ctx, report.winner_nation_id)
                    if report.winner_nation_id is not None
                    else None
                )
            )
            out.append(
                {
                    "province_id": report.province_id,
                    "province": province.name if province is not None else None,
                    "forces": forces,
                    "attacker_defender_roles_decoded": report.roles_decoded,
                    "outcome": {
                        "exact": report.outcome_exact,
                        "winner_nation_id": report.winner_nation_id,
                        "winner_nation": winner_nation,
                        "winner_is_ours": (
                            report.winner_nation_id == ctx.nation_id
                            if report.winner_nation_id is not None
                            else None
                        ),
                        "winner_role": report.winner_role,
                        "our_losses_total": report.our_losses_total,
                        "enemy_losses_total": report.enemy_losses_total,
                        "enemy_losses_by_type": [
                            {"unit_type_id": type_id, "name": names.get(type_id), "deaths": count}
                            for type_id, count in report.enemy_losses_by_type
                        ],
                        "kills_credited_to_our_units": [
                            {"unit_type_id": type_id, "name": names.get(type_id), "kills": kills}
                            for type_id, kills in report.kills_by_our_type
                        ],
                        "note": report.outcome_note,
                    },
                    "exceptional_groups": exceptional_groups,
                }
            )
        return {
            "turn": ctx.turn,
            "reports": out,
            "limitations": (
                "Starting-force totals exclude mount records, matching the "
                "client. Deaths and routed survivors come from each report's "
                "stored summary. Add-on groups 4-5 remain separately visible; "
                "their losses join a side's totals only when the report's exact "
                "kill accounting proves that attribution."
            ),
        }

    @reg.tool(
        "get_turn_messages",
        "Messages and events delivered to our nation this turn, including "
        "their complete text and displayed event effects. Persistent scouting "
        "reports are available through scout_report instead.",
    )
    def get_turn_messages(ctx: ToolContext) -> dict[str, Any]:
        commanders = {commander.commander_id: commander for commander in _commanders(ctx)}
        province_ids_by_name = {
            province.name: province.province_id
            for province in ctx.view.provinces()
            if province.name is not None
        }
        messages = []
        for record in ctx.view.turn_messages():
            body, effects = record.body_and_effects
            interception = PW.interception_from_message(
                record, province_ids_by_name)
            province = (
                ctx.view.province(record.province_id) if record.province_id is not None else None
            )
            commander = (
                commanders.get(record.commander_id) if record.commander_id is not None else None
            )
            messages.append(
                {
                    "message_id": record.message_id,
                    "kind": record.kind,
                    "worldwide": record.is_worldwide,
                    "diplomatic_action": record.diplomatic_action,
                    "type_id": record.type_id,
                    "text": body,
                    "effects": list(effects),
                    "province_id": record.province_id,
                    "province": province.name if province is not None else None,
                    "commander_id": record.commander_id,
                    "commander": commander.name if commander is not None else None,
                    "source_nation_id": (
                        record.source_nation_id if record.source_nation_id >= 0 else None
                    ),
                    "source_nation": (
                        _nation_name(ctx, record.source_nation_id)
                        if record.source_nation_id >= 0
                        else None
                    ),
                    "global_enchantment": record.global_enchantment_name,
                    "global_strength_estimate": record.global_strength_estimate,
                    "province_protection_interception": interception,
                }
            )
        remembered = PW.remembered_observations(ctx.game_db, ctx.game_id)
        return {
            "turn": ctx.turn,
            "count": len(messages),
            "messages": messages,
            "remembered_province_protection_observations": remembered,
            "note": (
                "These records were serialized specifically for our nation. "
                "Type-5 scouting reports are intentionally omitted here and "
                "remain available as structured province intel. Foreign "
                "province enchantments never appear in their Buildings list. "
                "An interception confirms an unidentified protection operated "
                "on that turn; retaliation-based ward candidates are inference, "
                "and current activity or duration remains unknown."
            ),
        }

    @reg.tool(
        "get_diplomatic_relations",
        "Our F4 diplomacy view: current contact, wars, NAP notice state, "
        "incoming NAP proposals, and defeated nations. Hidden relations "
        "between other nations are never returned.",
    )
    def get_diplomatic_relations(ctx: ToolContext) -> dict[str, Any]:
        pending = {}
        pending_note = None
        if ctx.h2_path is not None and ctx.h2_path.exists():
            from dom6_assistant.file_reader.formats import diplomacy as D

            try:
                _start, _end, records = D.find_outgoing_diplomacy(
                    ctx.h2_path.read_bytes()
                )
            except D.DiplomacyDecodeError as exc:
                # The 537-byte turn-1 pre-orders file is a legitimate client
                # state written before the ordinary final sections exist.
                # Relations still come from the .trn; only pending outgoing
                # actions are unavailable in that state.
                pending_note = str(exc)
            else:
                pending = {record.target_nation_id: record.action for record in records}
        intended = {
            int(row["target_nation_id"]): str(row["action"])
            for row in ctx.game_db.execute(
                "SELECT target_nation_id, action FROM current_diplomacy_intent "
                "WHERE game_id=? AND turn=?",
                (ctx.game_id, ctx.turn),
            )
        }
        incoming_diplomacy = {
            record.source_nation_id: record
            for record in ctx.view.turn_messages()
            if (
                record.diplomatic_action is not None
                and record.source_nation_id >= 5
            )
        }
        relations = []
        for relation in ctx.view.diplomatic_relations():
            incoming = incoming_diplomacy.get(relation.nation_id)
            relations.append(
                {
                    "nation_id": relation.nation_id,
                    "nation": _nation_name(ctx, relation.nation_id),
                    "controller": relation.controller,
                    "status": relation.status,
                    "contact": relation.contact,
                    "defeated": relation.defeated,
                    "waiting_for_response": relation.waiting_for_response,
                    "nap_phase": relation.nap_phase,
                    "nap_notice_turns": relation.nap_notice_turns,
                    "pending_action": pending.get(relation.nation_id),
                    "recorded_action": intended.get(relation.nation_id),
                    "incoming_action": (
                        incoming.diplomatic_action if incoming is not None else None
                    ),
                    "incoming_message_id": (
                        incoming.message_id if incoming is not None else None
                    ),
                }
            )
        result = {
            "turn": ctx.turn,
            "relations": relations,
            "note": (
                "This is restricted to our own F4 row. The full serialized "
                "matrix also contains other nations' hidden relations and is "
                "deliberately not exposed."
            ),
        }
        if pending_note is not None:
            result["pending_actions_note"] = pending_note
        return result

    @reg.tool(
        "get_global_enchantments",
        "Active global enchantments visible to our nation. Foreign casting "
        "provinces and opaque internal state fields are deliberately omitted.",
    )
    def get_global_enchantments(ctx: ToolContext) -> dict[str, Any]:
        analyses = {
            record.global_enchantment_name: record.global_strength_estimate
            for record in ctx.view.turn_messages()
            if (
                record.kind == "own_arcane_analysis"
                and record.global_enchantment_name is not None
                and record.global_strength_estimate is not None
            )
        }
        effects = []
        for effect in T.read_global_effects(ctx.view.data):
            spell = ctx.reference_db.execute(
                "SELECT name FROM spells WHERE id=?", (effect.spell_id,)
            ).fetchone()
            nation = ctx.reference_db.execute(
                "SELECT name FROM nations WHERE id=?", (effect.caster_nation_id,)
            ).fetchone()
            row: dict[str, Any] = {
                "effect_id": effect.effect_id,
                "spell_id": effect.spell_id,
                "spell": spell["name"] if spell else None,
                "caster_nation_id": effect.caster_nation_id,
                "caster_nation": nation["name"] if nation else None,
                "turns_remaining": effect.state,
            }
            if effect.caster_nation_id == ctx.nation_id:
                province = ctx.view.province(effect.cast_province_id)
                row["cast_province"] = {
                    "province_id": effect.cast_province_id,
                    "name": province.name if province else None,
                }
                # `value1` is the overcast value: excess gems + 5 per excess
                # path level. It is only true in the caster's own file -- a
                # controlled cast read 17 for us and 1 for the other player,
                # matching the rule that others need Arcane Analysis to even
                # estimate it. So it is returned for our own globals only.
                # This is our own enchantment: we chose the investment, so the
                # overcast is ours to know and withholding it would lose real
                # information a human player would simply remember. Where the
                # file carries more than one figure, the larger is the true
                # overcast and the smaller the masked public one, so report the
                # maximum and say that the file disagreed.
                overcast = max(effect.value1_variants or (effect.value1,))
                row["overcast"] = overcast
                row["dispel_resistance_note"] = (
                    "Others must beat this with their own overcast; both "
                    "sides add an exploding d20, so it is a contest, not "
                    "a threshold."
                )
                if overcast == 1:
                    # The field floors at 1, so a stored 1 is a true 0 or 1.
                    row["overcast_note"] = (
                        "Reported as 1, but the field floors at 1: the real "
                        "overcast is 0 or 1. Compute it from the gems we spent "
                        "and the caster's path level if it matters."
                    )
                if effect.overcast_ambiguous:
                    row["overcast_variants"] = list(effect.value1_variants)
                    row["overcast_note"] = (
                        "Our turn file carries this enchantment more than once "
                        "with differing overcast values; the largest is "
                        "reported. Cross-check it against what we actually "
                        "invested before relying on it."
                    )
            else:
                row["overcast"] = None
                row["overcast_note"] = (
                    "Hidden. A foreign global's overcast is not in our turn "
                    "file; Arcane Analysis is the in-game way to estimate it. "
                    "Dispelling without it is a gamble, not a calculation."
                )
            estimate = analyses.get(row["spell"])
            if estimate is not None:
                row["arcane_analysis"] = {
                    "estimated_strength_astral_pearls": estimate,
                    "approximate": True,
                    "source": "this turn's successful Arcane Analysis report",
                }
            effects.append(row)
        return {"global_enchantments": effects, "count": len(effects)}

    @reg.tool(
        "get_thrones",
        "The public F9 throne view: each throne's level, exact claimant, and "
        "separate current province owner when that owner is known to us.",
    )
    def get_thrones(ctx: ToolContext) -> dict[str, Any]:
        thrones = []
        for province in ctx.view.provinces():
            for site_id in province.site_ids:
                site = ctx.reference_db.execute(
                    "SELECT * FROM magic_sites WHERE id=?", (site_id,)
                ).fetchone()
                if site is None or not 11 <= site["rarity"] <= 13:
                    continue
                detail = _site_brief(ctx, site)
                detail.update(
                    {
                        "province_id": province.province_id,
                        "province_name": province.name,
                        "site_id": site_id,
                        "throne": site["name"],
                        "level": site["rarity"] - 10,
                        "claimed": province.throne_claimant_nation_id is not None,
                        "claimant_nation_id": province.throne_claimant_nation_id,
                        "claimant_nation": _nation_name(
                            ctx, province.throne_claimant_nation_id
                        ),
                        "claimed_by_us": (
                            province.throne_claimant_nation_id == ctx.nation_id
                        ),
                        "known_owner_nation_id": province.owner_nation_id,
                        "owner_status": province.status,
                    }
                )
                thrones.append(detail)
        ours = [row for row in thrones if row["claimed_by_us"]]
        return {
            "thrones": sorted(thrones, key=lambda row: row["province_id"]),
            "count": len(thrones),
            "claimed_by_us_count": len(ours),
            "claimed_by_us_points": ctx.view.parsed.claimed_throne_points,
            "claimed_by_us_points_from_list": sum(row["level"] for row in ours),
            "note": "claimant and province owner are independent fields. An "
            "unknown owner means independent or unexplored, not unclaimed.",
        }

    @reg.tool(
        "get_research",
        "Our research: points spent, the level reached in each of the seven "
        "schools with progress toward the next, plus a modeled production "
        "estimate with its included and omitted modifiers. Determines which "
        "spells we can cast and approximately how soon.",
    )
    def get_research(ctx: ToolContext) -> dict[str, Any]:
        # ctx.nation_id, never a parameter. The .trn carries a record for every
        # nation in the game, so a nation_id argument here would turn one tool
        # into a rival-research oracle — information the player has no screen
        # for. See the module docstring on the visibility boundary.
        parsed = ctx.view.parsed
        total = parsed.research_points
        levels = parsed.research_levels
        progress = parsed.research_progress
        state_words = parsed.research_progress_raw
        learned_spell_ids = parsed.learned_spell_ids
        if (
            total is None
            or levels is None
            or progress is None
            or state_words is None
            or learned_spell_ids is None
        ):
            # Fails loudly. A research report missing its levels is worse than
            # no report, because the levels are what say which spells we can
            # cast, and a caller reading only the total would not notice.
            raise ToolError(
                "our current research state could not be located in this .trn. "
                "Recorded as a gap; do not assume a level."
            )
        schools = []
        for name, level, done in zip(T.RESEARCH_SCHOOLS, levels, progress):
            # Level 9 is terminal for the ordinary school-level table. Dom6's
            # individual Level 9 spell selectors are exposed separately in
            # the queue; do not manufacture a Level 10 price here.
            need = T._LEVEL_COST(level + 1) if level < 9 else None
            schools.append(
                {
                    "school": name,
                    "level": level,
                    "progress": done,
                    "next_level_costs": need,
                    "points_to_next_level": need - done if need is not None else None,
                }
            )
        # Per-turn output, so "points to next level" can be read as a number
        # of turns rather than an abstract figure.
        researchers: list[dict[str, Any]] = []
        rate_total = 0
        inspiring_sources: list[dict[str, Any]] = []
        if ctx.h2_path is not None and ctx.h2_path.exists():
            h2_data = ctx.h2_path.read_bytes()
            blocks = O.find_order_blocks(h2_data)
            own_commanders = ctx.view.own_commanders(ctx.h2_path)
            unit_attributes: dict[int, Any] = {}
            for type_id in {c.type_id for c in own_commanders if c.type_id is not None}:
                unit_attributes[type_id] = ctx.reference_db.execute(
                    "SELECT magicstudy, researchbonus, inspiringres, "
                    "drainimmune FROM units WHERE id=?", (type_id,),
                ).fetchone()
            inspiring_by_province: dict[int, int] = {}
            for source in own_commanders:
                unit = unit_attributes.get(source.type_id)
                value = int(unit["inspiringres"] or 0) if unit else 0
                if not value or source.province_id is None:
                    continue
                inspiring_by_province[source.province_id] = (
                    inspiring_by_province.get(source.province_id, 0) + value
                )
                inspiring_sources.append({
                    "commander_id": source.commander_id,
                    "commander": source.name,
                    "unit_type_id": source.type_id,
                    "province_id": source.province_id,
                    "value": value,
                })

            for commander in own_commanders:
                block = blocks.get(commander.commander_id)
                if block is None or block.order_name != "research":
                    continue
                unit = unit_attributes.get(commander.type_id)
                province = (
                    ctx.view.province(commander.province_id)
                    if commander.province_id is not None
                    else None
                )
                equipped = O.read_equipment(h2_data, block.name_end)
                for pending in ctx.game_db.execute(
                    "SELECT slot, item_id FROM current_equipment_intent "
                    "WHERE game_id=? AND turn=? AND commander_id=?",
                    (ctx.game_id, ctx.turn, commander.commander_id),
                ):
                    if int(pending["item_id"]):
                        equipped[str(pending["slot"])] = int(pending["item_id"])
                    else:
                        equipped.pop(str(pending["slot"]), None)
                item_bonuses: dict[str, int] = {}
                for slot, item_id in equipped.items():
                    item = ctx.reference_db.execute(
                        "SELECT name, researchbonus FROM items WHERE id=?", (item_id,)
                    ).fetchone()
                    if item is not None and int(item["researchbonus"] or 0):
                        item_bonuses[f"{slot}: {item['name']}"] = int(item["researchbonus"])
                chassis_bonus = (
                    int(unit["magicstudy"] or 0) + int(unit["researchbonus"] or 0) if unit else 0
                )
                rate = RR.researcher_rate(
                    commander.commander_id,
                    commander.name,
                    commander.paths,
                    magic_study=chassis_bonus,
                    item_bonuses=item_bonuses,
                    magic_scale=(province.magic_scale if province else 0) or 0,
                    friendly_dominion=bool(
                        province is not None and province.dominion_owner == ctx.nation_id
                    ),
                    drain_immune=bool(unit is not None and unit["drainimmune"]),
                    experience=int(commander.experience or 0),
                    inspiring_researcher=inspiring_by_province.get(
                        commander.province_id, 0),
                )
                rate_total += rate.total
                researchers.append(
                    {
                        "commander_id": rate.commander_id,
                        "commander": rate.name,
                        "magic_path_levels": rate.path_levels,
                        "points": rate.total,
                        "from_base": rate.base,
                        "from_paths": rate.from_paths,
                        "from_chassis": rate.from_unit,
                        "from_items": rate.from_items,
                        "item_bonuses": rate.items or None,
                        "from_magic_scale": rate.from_scale,
                        "drain_immune": bool(unit is not None and unit["drainimmune"]),
                        "experience": rate.experience,
                        "experience_stars": rate.experience_stars,
                        "from_experience": rate.from_experience,
                        "from_inspiring_researcher": (
                            rate.from_inspiring_researcher),
                        "unmodeled": [],
                    }
                )

        queue = None
        queue_note = None
        if ctx.h2_path is not None and ctx.h2_path.exists():
            try:
                targets = H2.read_research_queue(
                    ctx.h2_path.read_bytes(), levels, state_words
                )
            except H2.ResearchQueueNotLocated as exc:
                queue_note = str(exc)
            else:
                seen = [0] * len(T.RESEARCH_SCHOOLS)
                spell_ids = sorted(
                    {target for target in targets if target >= len(T.RESEARCH_SCHOOLS)}
                )
                spells: dict[int, sqlite3.Row] = {}
                if spell_ids:
                    marks = ",".join("?" * len(spell_ids))
                    spells = {
                        row["id"]: row
                        for row in ctx.reference_db.execute(
                            f"SELECT id, name, school, researchlevel FROM spells "
                            f"WHERE id IN ({marks})",
                            spell_ids,
                        )
                    }
                queue = []
                for position, target in enumerate(targets):
                    if target < len(T.RESEARCH_SCHOOLS):
                        seen[target] += 1
                        queue.append(
                            {
                                "position": position,
                                "kind": "school_level",
                                "school": T.RESEARCH_SCHOOLS[target],
                                "target_level": levels[target] + seen[target],
                            }
                        )
                        continue
                    spell = spells.get(target)
                    school_id = spell["school"] if spell is not None else None
                    queue.append(
                        {
                            "position": position,
                            "kind": "level_9_spell",
                            "school": (
                                T.RESEARCH_SCHOOLS[school_id]
                                if isinstance(school_id, int)
                                and 0 <= school_id < len(T.RESEARCH_SCHOOLS)
                                else None
                            ),
                            "target_level": 9,
                            "spell_id": target,
                            "spell": spell["name"] if spell is not None else None,
                            "verified_level_9": bool(
                                spell is not None and spell["researchlevel"] == 9
                            ),
                        }
                    )
        for school in schools:
            # Ceiling division: a level needing 142 points at 145 a turn is one
            # turn away, not zero.
            points_to_next = school["points_to_next_level"]
            school["estimated_turns_to_next_level"] = (
                -(-points_to_next // rate_total)
                if rate_total > 0 and points_to_next is not None else None
            )
        learned_set = set(learned_spell_ids)
        learned_level_nine = [
            {
                "spell_id": int(spell["id"]),
                "spell": spell["name"],
                "school": T.RESEARCH_SCHOOLS[int(spell["school"])],
            }
            for spell in ctx.reference_db.execute(
                "SELECT id, name, school FROM spells "
                "WHERE researchlevel=9 AND school BETWEEN 0 AND 6 "
                "ORDER BY school, name"
            )
            if int(spell["id"]) in learned_set
        ]
        return {
            "nation_id": ctx.nation_id,
            "research_points": total,
            "schools": schools,
            "learned_level_9_spells": learned_level_nine,
            "points_per_turn": rate_total,
            "modeled_points_per_turn": rate_total,
            "points_per_turn_exact": True,
            "researchers": researchers,
            "inspiring_researcher_sources": inspiring_sources,
            "unmodeled_modifiers": [],
            "modeled_points_basis": (
                "2 per non-Holy magic path level plus 5 per researcher, "
                "plus decoded chassis/item researchbonus values and the "
                "province scale — Magic adds only under friendly dominion, "
                "while Drain subtracts unless the chassis is Drain-immune — "
                "and one point per displayed experience star (thresholds "
                "15/50/100/200/400 XP), plus each province-local Inspiring "
                "Researcher value for every research-capable caster there."
            ),
            "queue": queue,
            "queue_capacity": H2.RESEARCH_QUEUE_CAPACITY,
            "queue_note": queue_note,
            "note": "School ids research that school's next level at "
            "that point; repeated schools advance in order. Level "
            "9 entries are individual spell ids. Both forms are "
            "writable. Levels 4 and 5 cost 400 and 700, confirmed by "
            "cross-turn rollover; levels 6-9 use the published table and "
            "remain unverified in this save.",
        }

    @reg.tool(
        "list_research_options",
        "Spells a research choice unlocks. With no arguments, show the next "
        "level in every school; optionally select a school, exact level, or "
        "battle/ritual spells. Use this before replacing the research queue.",
        Param(
            "school",
            "string",
            "school name",
            required=False,
            choices=tuple(name.lower() for name in T.RESEARCH_SCHOOLS),
        ),
        Param("level", "integer", "research level 0-9", required=False),
        Param(
            "kind",
            "string",
            "all, battle, or ritual",
            required=False,
            default="all",
            choices=("all", "battle", "ritual"),
        ),
        Param("max_results", "integer", "maximum spell rows", required=False, default=200),
    )
    def list_research_options(
        ctx: ToolContext,
        school: str | None = None,
        level: int | None = None,
        kind: str = "all",
        max_results: int = 200,
    ) -> dict[str, Any]:
        if level is not None and not 0 <= level <= 9:
            raise ToolError(f"level must be 0-9, got {level}.")
        levels = ctx.view.parsed.research_levels
        if levels is None:
            raise ToolError("current per-school research levels are not decoded")
        school_ids = (
            [_SCHOOL_BY_LOWER[school.lower()]]
            if school is not None
            else list(range(len(_SCHOOL_NAMES)))
        )
        targets = []
        for school_id in school_ids:
            target_level = level if level is not None else min(int(levels[school_id]) + 1, 9)
            targets.append((school_id, target_level))
        spells = []
        for school_id, target_level in targets:
            for row in ctx.reference_db.execute(
                "SELECT * FROM spells WHERE school=? AND researchlevel=? ORDER BY name",
                (school_id, target_level),
            ):
                if (
                    int(row["path1"]) < 0
                    or int(row["pathlevel1"] or 0) <= 0
                    or not _spell_nation_allowed(ctx, int(row["id"]))
                ):
                    continue
                brief = _spell_brief(ctx, row)
                if kind == "battle" and brief["kind"] != "battle_spell":
                    continue
                if kind == "ritual" and brief["kind"] != "ritual":
                    continue
                spells.append(brief)
        limit = max(1, min(max_results, 500))
        return {
            "targets": [{"school": _SCHOOL_NAMES[s], "level": level} for s, level in targets],
            "spells": spells[:limit],
            "returned": min(len(spells), limit),
            "total_matches": len(spells),
            "truncated": len(spells) > limit,
            "note": (
                "Levels 0-8 are ordinary school research. At Level 9, "
                "each returned spell is selected individually in the "
                "research queue."
            ),
        }

    @reg.tool(
        "list_divine_spells",
        "Divine spells this commander can call on. They need no research — "
        "only Holy levels — and which ones our nation has at all depends on "
        "our pretender's magic path. Spells above the commander's Holy level "
        "are listed too, marked not castable, as the game greys them out.",
        Param("commander_id", "integer", "a priest of ours"),
    )
    def list_divine_spells(ctx: ToolContext, commander_id: int) -> dict[str, Any]:
        commander = next((c for c in _commanders(ctx) if c.commander_id == commander_id), None)
        if commander is None:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Call list_commanders for valid ids."
            )
        holy = int(commander.paths.get("H", 0) or 0)
        pretender = next((c for c in _commanders(ctx) if c.is_pretender), None)
        if pretender is None:
            raise ToolError(
                "our pretender could not be identified, and the Divine list "
                "our nation has depends on its magic path."
            )
        # Worn equipment can add Holy levels — five base-game items grant
        # H+1 — and the Divine list has entries at Holy 4 and 5 that no
        # ordinary priest of ours reaches unaided.
        worn = _effective_equipment_rows(ctx, commander_id)
        spells = DV.divine_spells(
            ctx.reference_db,
            ctx.nation_id,
            holy,
            pretender.paths,
            other_paths=commander.paths,
            item_rows=worn,
        )
        rows = [
            {
                "spell_id": s.spell_id,
                "name": s.name,
                "holy_level_required": s.holy_level,
                "granted_by_god_path": s.god_path,
                "castable_now": s.castable,
                "reached_via_equipment": s.needs_equipment,
            }
            for s in spells
        ]
        non_holy = [
            (index, int(pretender.paths.get(letter, 0) or 0))
            for index, letter in enumerate(DV.GOD_PATHS)
            if int(pretender.paths.get(letter, 0) or 0) > 0
        ]
        highest = max((level for _index, level in non_holy), default=0)
        tied_highest = [index for index, level in non_holy if level == highest]
        return {
            "commander_id": commander_id,
            "commander": commander.name,
            "holy_level": holy,
            "pretender": pretender.name,
            "pretender_paths": pretender.paths,
            "spells": rows,
            "castable_now": sum(1 for s in spells if s.castable),
            "basis": (
                "Divine spells are school 7 and need no research. A nation's "
                "list is its unrestricted spells plus one per elemental set, "
                "chosen by the pretender's magic path. Confirmed exactly "
                "against the game's own Divine list for a Holy 3 priest."
            ),
            "selector_confidence": (
                "current design screen-confirmed, general tie rule inferred"
                if len(tied_highest) > 1
                else "god-path attribute mapping confirmed; unique-highest "
                "selection still lacks a second screen control"
            ),
            "holy_from_equipment": DV.holy_from_items(worn),
            "inferred": (
                "Which path wins when the pretender has two at equal level is "
                "inferred, not observed: ours is Fire 6 / Air 6 and the game "
                "grants the Fire spells."
            ),
            "unmodelled_selector": (
                "No second selector has been observed. If Divine tiers depend "
                "on blessing-design points, those points derive from the "
                "pretender's magic paths and levels; they are not the design "
                "Dominion score, the temple-raised holy recruitment allowance, "
                "or province-local dominion candles. The current list remains "
                "verified for this pretender design rather than universal."
            ),
            "communions_not_modelled": (
                "castable_now means 'without a communion'. A Communion Master "
                "borrows levels from its slaves during a battle and can reach "
                "Holy 4 or 5 for spells it could never cast alone; that "
                "happens at resolution and nothing in the save predicts it."
            ),
        }

    @reg.tool(
        "list_castable_spells",
        "Every currently researched spell whose base paths one of our "
        "commanders meets. Separates battle spells from rituals and says "
        "whether the decoded order writer supports the ritual's target family.",
        Param("commander_id", "integer", "caster id from list_commanders"),
        Param(
            "kind",
            "string",
            "all, battle, or ritual",
            required=False,
            default="all",
            choices=("all", "battle", "ritual"),
        ),
        Param("max_results", "integer", "maximum spell rows", required=False, default=200),
    )
    def list_castable_spells(
        ctx: ToolContext, commander_id: int, kind: str = "all", max_results: int = 200
    ) -> dict[str, Any]:
        commander = next((c for c in _commanders(ctx) if c.commander_id == commander_id), None)
        if commander is None:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Call list_commanders for valid ids."
            )
        caster_item_ids: tuple[int, ...] = ()
        if ctx.h2_path is not None and ctx.h2_path.exists():
            h2_data = ctx.h2_path.read_bytes()
            block = O.find_order_blocks(h2_data).get(commander_id)
            if block is not None:
                caster_item_ids = tuple(
                    O.read_equipment(h2_data, block.name_end).values())
        province = (
            ctx.view.province(commander.province_id) if commander.province_id is not None else None
        )
        has_lab = province.has_laboratory if province else None
        levels = ctx.view.parsed.research_levels
        if levels is None:
            raise ToolError("current per-school research levels are not decoded")
        uncommitted = _magic_economy(ctx)["uncommitted"] or {}
        caster_paths = effective_magic_paths(ctx, commander)
        rows = []
        for spell in ctx.reference_db.execute(
            "SELECT * FROM spells WHERE school BETWEEN 0 AND 6 ORDER BY researchlevel, name"
        ):
            if (
                int(spell["path1"]) < 0
                or int(spell["pathlevel1"] or 0) <= 0
                or not _spell_nation_allowed(ctx, int(spell["id"]))
            ):
                continue
            if not _research_met(
                spell, levels, ctx.view.parsed.learned_spell_ids):
                continue
            if not _spell_requirements_met(spell, caster_paths):
                continue
            brief = _spell_brief(ctx, spell)
            is_ritual = brief["kind"] == "ritual"
            if kind == "battle" and is_ritual:
                continue
            if kind == "ritual" and not is_ritual:
                continue
            if is_ritual:
                effect = _effect_brief(ctx, spell["effect_record_id"])
                brief["monthly_behavior"] = (
                    "A monthly order reserves the current cast only. It "
                    "repeats next turn only if that turn can afford it; "
                    "otherwise the caster returns to Defend."
                )
                map_range = RTR.spell_province_range(
                    ctx.reference_db, int(spell["id"]))
                map_targeted = map_range is not None
                friendly_only = (
                    ctx.reference_db.execute(
                        "SELECT 1 FROM attributes_by_spell WHERE spell_number=? "
                        "AND attribute=738 LIMIT 1",
                        (spell["id"],),
                    ).fetchone()
                    is not None
                )
                effect_number = int(effect["effect_number"]) if effect else -1
                no_selector_mode = RTR.spell_no_selector_mode(
                    ctx.reference_db, int(spell["id"]))
                targetless = no_selector_mode is not None
                needs_object = effect_number in RITUAL_EFFECTS_REQUIRING_OBJECT_SELECTION
                unit_target = int(spell["id"]) in UNIT_TARGET_RITUAL_SPELL_IDS
                spell_id = int(spell["id"])
                is_world_global = effect_number == 81
                is_global_selector = (
                    effect_number in RTR.GLOBAL_SELECTOR_RITUAL_EFFECTS)
                accepts_strength_investment = (
                    effect_number
                    in RTR.GLOBAL_STRENGTH_INVESTMENT_RITUAL_EFFECTS)
                province_enchantment = RTR.spell_is_province_enchantment(
                    ctx.reference_db, spell_id)
                implicit_local = RTR.spell_uses_implicit_local_target(
                    ctx.reference_db, spell_id)
                duration_rate = (
                    RTR.spell_duration_extension_months_per_gem(
                        ctx.reference_db, spell_id)
                )
                requires_coast = RTR.spell_requires_coast(
                    ctx.reference_db, spell_id)
                gem_transport = effect_number in GEM_TRANSPORT_RITUAL_EFFECTS
                item_transport = int(spell["id"]) in ITEM_TRANSPORT_RITUAL_SPELL_IDS
                is_wish = int(spell["id"]) in O.WISH_RITUAL_SPELL_IDS
                brief["target"] = (
                    (
                        "own_troop_in_caster_province_including_mindless"
                        if int(spell["id"]) in MINDLESS_ALLOWED_UNIT_TARGET_RITUAL_SPELL_IDS
                        else "own_non_mindless_troop_in_caster_province"
                    )
                    if unit_target
                    else "caster_current_province_implicit"
                    if implicit_local
                    else "province_plus_commander_and_treasury_item"
                    if item_transport
                    else "typed_wish_selector"
                    if is_wish
                    else "province_plus_commander_and_carried_gems"
                    if gem_transport
                    else
                    "province_plus_undecoded_payload"
                    if map_targeted and needs_object
                    else "friendly_province"
                    if map_targeted and friendly_only
                    else "province"
                    if map_targeted
                    else "active_global_enchantment"
                    if is_global_selector
                    else "automatic_eligible_dead_hall_of_fame_hero"
                    if no_selector_mode == "automatic_dead_hall_of_fame_hero"
                    else "none"
                    if is_world_global
                    else "none"
                    if targetless
                    else "undecoded_nonprovince_target"
                )
                if targetless:
                    brief["target_scope"] = no_selector_mode
                brief["order_supported"] = (
                    unit_target or implicit_local or gem_transport or item_transport
                    or is_wish or is_world_global or is_global_selector
                    or (map_targeted and not needs_object) or targetless
                )
                if is_global_selector:
                    brief["target_parameter"] = "target_global"
                    brief["target_lookup"] = "get_global_enchantments"
                    brief["extra_gems_supported"] = accepts_strength_investment
                    brief["monthly_supported"] = False
                elif is_world_global:
                    brief["target_parameter"] = None
                    brief["extra_gems_supported"] = True
                    brief["monthly_supported"] = False
                elif is_wish:
                    brief["target_parameter"] = "typed_wish_selector"
                    brief["target_parameters"] = [
                        "wish_item", "wish_unit", "wish_nation",
                        "wish_result", "wish_random",
                    ]
                    brief["target_lookup"] = [
                        "lookup_item", "lookup_unit",
                        "get_diplomatic_relations",
                    ]
                    brief["payload"] = "exactly_one_selector"
                    brief["wish_result_choices"] = list(WISH.FIXED_RESULTS)
                    brief["wish_random_choices"] = list(WISH.RANDOM_REQUESTS)
                    brief["extra_gems_supported"] = False
                    brief["monthly_supported"] = False
                elif no_selector_mode == "automatic_dead_hall_of_fame_hero":
                    brief["target_parameter"] = None
                    brief["monthly_supported"] = False
                    brief["automatic_target_note"] = (
                        "The game chooses an eligible dead hero who remains in "
                        "the Hall of Fame and revives that hero as a Mummy. "
                        "No target is stored in the order; selection among "
                        "multiple eligible heroes is not yet known."
                    )
                if unit_target:
                    brief["target_parameter"] = "target_unit"
                    brief["target_lookup"] = (
                        "list_units(province_id=<caster province>, "
                        "include_instances=true)"
                    )
                if implicit_local:
                    brief["target_parameter"] = None
                if province_enchantment:
                    brief["extra_gems_supported"] = True
                    brief["extra_gems_effect"] = (
                        f"extends duration by {duration_rate} month"
                        f"{'s' if duration_rate != 1 else ''} per gem")
                    brief["duration_extension_months_per_gem"] = duration_rate
                    brief["monthly_supported"] = True
                if map_targeted:
                    brief["map_range"] = map_range
                if gem_transport:
                    brief["target_parameters"] = [
                        "target_province", "target_commander"
                    ]
                    brief["payload"] = "caster_carried_gems"
                if item_transport:
                    brief["target_parameters"] = [
                        "target_province", "target_commander", "target_item"
                    ]
                    brief["payload_lookup"] = "get_item_treasury"
                path = int(spell["path1"])
                gem_name = _GEM_KEYS[path] if 0 <= path < len(_GEM_KEYS) else None
                available = uncommitted.get(gem_name) if gem_name else None
                brief["uncommitted_gems"] = available
                brief["affordable"] = (
                    available >= int(spell["gemcost"] or 0) if available is not None else None
                )
                blockers = []
                if not brief["order_supported"]:
                    blockers.append("target or payload layout is not decoded")
                if has_lab is not True:
                    blockers.append("caster is not in a confirmed laboratory")
                source_requirements = RTR.spell_source_requirements(
                    ctx.reference_db, spell_id)
                if source_requirements:
                    brief["source_requirements"] = source_requirements
                    try:
                        RTR.validate_ritual_source(
                            ctx.reference_db, spell_id,
                            ctx.view.parsed.provinces,
                            commander.province_id, commander.type_id,
                            caster_item_ids,
                        )
                    except ValueError as exc:
                        brief["source_requirements_met"] = False
                        blockers.append(str(exc))
                    else:
                        brief["source_requirements_met"] = True
                if requires_coast:
                    coastal = (
                        RTR.province_is_coastal(
                            ctx.view.parsed.provinces,
                            commander.province_id,
                        )
                        if commander.province_id is not None
                        else None
                    )
                    brief["coastal_source_confirmed"] = coastal
                if brief["affordable"] is not True:
                    blockers.append("uncommitted gems are insufficient or unknown")
                brief["castable_now"] = not blockers
                if blockers:
                    brief["why_not_castable_now"] = blockers
            else:
                brief["order_supported"] = True
                brief["target"] = "battlefield"
                brief["scriptable_now"] = True
            rows.append(brief)
        limit = max(1, min(max_results, 500))
        return {
            "commander_id": commander.commander_id,
            "commander": commander.name,
            "paths": commander.paths,
            "province_id": commander.province_id,
            "laboratory_here": has_lab,
            "spells": rows[:limit],
            "returned": min(len(rows), limit),
            "total_matches": len(rows),
            "truncated": len(rows) > limit,
            "note": (
                "Path checks use the commander's current serialized "
                "paths. Temporary battle boosts and path boosters are "
                "not assumed. Ritual casting also requires a laboratory."
            ),
        }

    @reg.tool(
        "list_forgeable_items",
        "Magic items whose construction level and effective forging-path "
        "requirements a commander currently meets, filtered for our nation's "
        "item restrictions. Master Smith and Inept Smith adjust eligibility, "
        "not gem cost. Listed and actually reserved costs include the client's "
        "item multipliers, national rebates, chassis bonuses, equipped hammers, "
        "Construction sites and active global enchantments.",
        Param("commander_id", "integer", "forger id from list_commanders"),
        Param(
            "include_unresearched",
            "boolean",
            "include items above current Construction research",
            required=False,
            default=False,
        ),
        Param("max_results", "integer", "maximum item rows", required=False, default=200),
    )
    def list_forgeable_items(
        ctx: ToolContext,
        commander_id: int,
        include_unresearched: bool = False,
        max_results: int = 200,
    ) -> dict[str, Any]:
        commander = next((c for c in _commanders(ctx) if c.commander_id == commander_id), None)
        if commander is None:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Call list_commanders for valid ids."
            )
        levels = ctx.view.parsed.research_levels
        if levels is None or len(levels) <= 3:
            raise ToolError(
                "current Construction research could not be decoded; item "
                "eligibility cannot be listed safely"
            )
        construction = int(levels[3])
        effective_paths, path_adjustment, adjustment_sources = effective_forge_paths(
            ctx, commander)
        candidates = []
        for item in ctx.reference_db.execute("SELECT * FROM items ORDER BY constlevel, name"):
            if not FC.nation_can_forge(item, ctx.view.nation_id):
                continue
            if not include_unresearched and int(item["constlevel"] or 0) > construction:
                continue
            requirements = (
                (item["mainpath"], item["mainlevel"]),
                (item["secondarypath"], item["secondarylevel"]),
            )
            if any(
                path and int(effective_paths.get(str(path).upper(), 0)) < int(required or 0)
                for path, required in requirements
            ):
                continue
            brief = _item_brief(ctx, item, forger=commander)
            brief["researched"] = int(item["constlevel"] or 0) <= construction
            brief["effective_paths_met"] = True
            # Kept for clients written before forging-path adjustments were
            # distinguished from economic forge bonuses.
            brief["base_paths_met"] = True
            candidates.append(brief)
        limit = max(1, min(max_results, 500))
        province = (
            ctx.view.province(commander.province_id) if commander.province_id is not None else None
        )
        return {
            "commander_id": commander.commander_id,
            "commander": commander.name,
            "paths": commander.paths,
            "effective_forge_paths": effective_paths,
            "forge_path_adjustment": path_adjustment,
            "forge_path_adjustment_sources": adjustment_sources or None,
            "construction_research": construction,
            "laboratory_here": (province.has_laboratory if province else None),
            "items": candidates[:limit],
            "returned": min(len(candidates), limit),
            "total_matches": len(candidates),
            "truncated": len(candidates) > limit,
            "writer_status": "exact client-derived forge costs are writable",
        }

    @reg.tool(
        "get_recruitment_options",
        "Everything a province can build this turn: each unit's cost in gold, "
        "resources, recruitment points, commander points and holy points, "
        "against what the province actually has of each, plus the treasury. "
        "Costs we have not established come back null, never guessed.",
        Param("province_id", "integer", "a province we own"),
    )
    def get_recruitment_options(ctx: ToolContext, province_id: int) -> dict[str, Any]:
        owned = [p.province_id for p in ctx.view.own_provinces()]
        if province_id not in owned:
            raise ToolError(f"province {province_id} is not ours: {owned}")
        province = next((p for p in ctx.view.provinces() if p.province_id == province_id), None)

        # All five province budgets now compute from current player-visible
        # files/reference data; none requires a remembered panel observation.
        budgets: dict[str, Any] = {}
        if province is not None:
            points = estimate_recruitment_points(
                province.population,
                fort_type=province.fort_type,
                is_ours=True,
                order_scale=province.order_scale,
            )
            if points["exact"]:
                budgets["recruitment_points"] = {
                    "available": points["points"],
                    "source": "computed from the .trn, current",
                }
                budgets["commander_points"] = {
                    "available": points["commander_points"],
                    "source": "computed from the .trn, current",
                }
        resource_budget = owned_province_resource_budget(ctx, province_id)
        budgets["resources"] = {
            "available": resource_budget.total,
            "source": resource_budget.source,
            "exact": resource_budget.exact,
        }
        if resource_budget.note:
            budgets["resources"]["note"] = resource_budget.note
        holy_budget = owned_province_holy_budgets(ctx)[province_id]
        budgets["holy_points"] = {
            "available": holy_budget.total,
            "source": holy_budget.source,
            "exact": holy_budget.exact,
            "base_dominion": holy_budget.base_dominion,
            "maximum_dominion": holy_budget.maximum_dominion,
            "own_temples": holy_budget.own_temples,
            "shared_temples": holy_budget.shared_temples,
            "disciple_nations": holy_budget.disciple_nations,
            "temple_holy_point_bonus": holy_budget.temple_holy_point_bonus,
        }
        if holy_budget.note:
            budgets["holy_points"]["note"] = holy_budget.note

        units = []
        for entry in _recruitable(ctx, province_id)["recruitable"]:
            unit_id = entry["unit_type_id"]
            sacred, holy_cost = _holy_cost(ctx, unit_id)
            observed = ctx.game_db.execute(
                "SELECT resources, commander_points, recruit_points "
                "FROM recruitable WHERE unit_type_id=? AND nation_id=?",
                (unit_id, ctx.nation_id),
            ).fetchone()
            is_commander = entry["kind"] == "commander"
            units.append(
                {
                    "unit_type_id": unit_id,
                    "name": entry["name"],
                    "is_commander": is_commander,
                    "sacred": sacred,
                    "gold": entry["gold"],
                    "gold_source": entry["gold_source"],
                    "resources": (
                        observed["resources"]
                        if observed and observed["resources"] is not None
                        else resource_cost_for(ctx.reference_db, unit_id)
                    ),
                    "holy_points": holy_cost,
                    "commander_points": (
                        observed["commander_points"]
                        if observed and is_commander
                        else (None if is_commander else 0)
                    ),
                    "recruit_points": (
                        observed["recruit_points"]
                        if observed and observed["recruit_points"] is not None
                        else recruitment_point_cost_for(ctx.reference_db, unit_id)
                    ),
                }
            )
        units.sort(key=lambda u: (not u["is_commander"], u["name"] or ""))

        gaps = []
        unknown_points = [u["name"] for u in units if u["recruit_points"] is None]
        if unknown_points:
            gaps.append(
                "RECRUITMENT POINT cost is unavailable for: "
                + ", ".join(str(n) for n in unknown_points)
            )
        if budgets["resources"]["available"] is None:
            gaps.append(
                "this province's RESOURCES could not be computed: "
                + str(resource_budget.note or "unknown calculator input")
            )
        if budgets["holy_points"]["available"] is None:
            gaps.append(
                "this province's HOLY POINTS could not be computed: "
                + str(holy_budget.note or "unknown pretender Dominion")
            )
        return {
            "province_id": province_id,
            "province_name": province.name if province else None,
            "is_capital": bool(province and province.fort_type == 4),
            "treasury_gold": ctx.view.parsed.player_gold,
            "budgets": budgets,
            "can_build": units,
            "gaps": gaps,
            "note": (
                "resources and recruitment points may go NEGATIVE — gold "
                "can be committed ahead and the deficit worked off over "
                "later turns — so a unit that does not fit this turn is "
                "delayed, not refused."
            ),
        }

    @reg.tool("get_orders", "Orders recorded so far this turn, with the reasoning behind each.")
    def get_orders(ctx: ToolContext) -> list[dict]:
        out = []
        for r in _intent_rows(ctx):
            out.append(
                {
                    "commander_id": r["commander_id"],
                    "commander_name": r["commander_name"],
                    "order": r["order_name"],
                    **_intent_parameter(ctx, r),
                    "rationale": r["rationale"],
                }
            )
        defence_rows = ctx.game_db.execute(
            "SELECT * FROM current_province_defence_intent "
            "WHERE game_id=? AND turn=? ORDER BY province_id",
            (ctx.game_id, ctx.turn),
        )
        province_names = {
            province.province_id: province.name for province in ctx.view.own_provinces()
        }
        for row in defence_rows:
            out.append(
                {
                    "province_id": row["province_id"],
                    "province_name": province_names.get(row["province_id"]),
                    "order": "province_defence",
                    "target": row["target"],
                    "rationale": row["rationale"],
                }
            )
        diplomacy_rows = ctx.game_db.execute(
            "SELECT * FROM current_diplomacy_intent "
            "WHERE game_id=? AND turn=? ORDER BY target_nation_id",
            (ctx.game_id, ctx.turn),
        )
        for row in diplomacy_rows:
            out.append(
                {
                    "nation_id": row["target_nation_id"],
                    "nation": _nation_name(ctx, row["target_nation_id"]),
                    "order": "diplomacy",
                    "action": row["action"],
                    "missive": row["missive"],
                    "rationale": row["rationale"],
                }
            )
        supplemental = (
            ("current_battle_intent", "battle_order"),
            ("current_battle_position_intent", "battle_position"),
            ("current_carried_gem_intent", "carried_gems"),
            ("current_battle_script_intent", "battle_script"),
            ("current_equipment_intent", "equipment"),
            ("current_shape_change_intent", "shape_change"),
            ("current_squad_creation_intent", "squad_creation"),
            ("current_troop_assignment_intent", "troop_assignment"),
        )
        for view, kind in supplemental:
            rows = ctx.game_db.execute(
                f"SELECT * FROM {view} WHERE game_id=? AND turn=? ORDER BY id",
                (ctx.game_id, ctx.turn),
            )
            for row in rows:
                commander_id = (
                    row["target_commander_id"]
                    if kind in {"squad_creation", "troop_assignment"}
                    else row["commander_id"]
                )
                entry = {
                    "commander_id": commander_id,
                    "commander_name": row["commander_name"],
                    "order": kind,
                    "rationale": row["rationale"],
                }
                if kind == "battle_order":
                    entry.update(
                        {
                            "squad": row["squad"],
                            "stance": row["stance"],
                            "target": row["target"],
                            "formation": row["formation"],
                        }
                    )
                elif kind == "battle_position":
                    entry.update({"squad": row["squad"], "x": row["x"], "y": row["y"]})
                elif kind == "carried_gems":
                    entry.update(
                        {"path": O.CARRIED_GEM_PATHS[row["path"]], "amount": row["amount"]}
                    )
                elif kind == "battle_script":
                    raw = json.loads(row["queue_json"])
                    spells = {}
                    for value in raw:
                        if value > 0:
                            spell = ctx.reference_db.execute(
                                "SELECT name FROM spells WHERE id=?", (value,)
                            ).fetchone()
                            if spell:
                                spells[value] = spell["name"]
                    entry["rounds"] = [O.describe_single_round(value, spells) for value in raw]
                elif kind == "equipment":
                    entry.update({"slot": row["slot"], "item_id": row["item_id"]})
                elif kind == "shape_change":
                    entry.update({
                        "source_form": {
                            "unit_type_id": row["source_type_id"],
                            "hp": row["source_hp"],
                        },
                        "target_form": {
                            "unit_type_id": row["target_type_id"],
                            "hp": row["target_hp"],
                        },
                        "instantaneous": True,
                    })
                elif kind == "squad_creation":
                    entry.update({"squad": row["target_squad"], "squad_id": row["squad_id"]})
                elif kind == "troop_assignment":
                    entry.update({
                        "unit_instance_id": row["unit_instance_id"],
                        "unit_type_id": row["unit_type_id"],
                        "unit_name": row["unit_name"],
                        "destination": row["destination"],
                    })
                    if row["destination"] == "garrison":
                        entry["order"] = "troop_detachment"
                        entry.pop("commander_id", None)
                        entry.pop("commander_name", None)
                    else:
                        entry["squad"] = row["target_squad"]
                out.append(entry)
        return out

    @reg.tool(
        "get_decision_history",
        "Append-only decisions across turns, including every explicit "
        "rationale and links to the previous decision for the same subject. "
        "Use this to recover why an earlier order was made or revised.",
        Param("turn", "integer", "restrict to one turn", required=False),
        Param("category", "string", "restrict to a decision category",
              required=False),
        Param("search", "string", "match subject, action, or rationale",
              required=False),
        Param("limit", "integer", "max records", required=False, default=100),
    )
    def get_decision_history(
        ctx: ToolContext,
        turn: int | None = None,
        category: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return decision_history(
            ctx.game_db, ctx.game_id, turn=turn, category=category,
            search=search, limit=limit)

    @reg.tool(
        "get_turn_completion",
        "Whether this turn has a still-valid autonomous completion handshake. "
        "The status becomes incomplete if either recorded decisions or the "
        "live .2h changed afterwards, and separately reports whether the two "
        "local End Turn flags have been submitted.",
    )
    def get_turn_completion(ctx: ToolContext) -> dict[str, Any]:
        return completion_status(
            ctx.game_db, ctx.game_id, ctx.turn, ctx.h2_path).as_dict()

    @reg.tool(
        "list_order_types",
        "Every order we can actually issue, with which have been verified "
        "against the game. Only these can be recorded.",
    )
    def list_order_types(ctx: ToolContext) -> dict[str, Any]:
        rules = {}
        for name, spec in O.ORDER_SPECS.items():
            kind = spec.parameter_kind
            if kind == O.PARAM_NONE:
                rules[name] = {"parameter": "none"}
            elif kind in (O.PARAM_PROVINCE, O.PARAM_PROVINCE_OR_ZERO):
                rules[name] = {
                    "parameter": "destination",
                    "required": True,
                    "lookup": "find_province",
                }
            elif kind == O.PARAM_CURRENT_PROVINCE:
                rules[name] = {
                    "parameter": "current_province",
                    "required": False,
                    "automatic": True,
                }
            elif kind == O.PARAM_ITEM:
                rules[name] = {"parameter": "item_id", "required": True, "lookup": "lookup_item"}
            elif kind == O.PARAM_MAGIC_PATH:
                rules[name] = {
                    "parameter": "magic_path",
                    "required": True,
                    "choices": list(O.MAGIC_PATH_CODES),
                }
            elif kind == O.PARAM_BUILDING:
                rules[name] = {
                    "parameter": "fixed",
                    "building": O.BUILDING_NAMES[spec.fixed_parameter],
                }
        return {
            "orders": sorted(set(O.ORDER_CODES) - O.ECONOMIC_ORDERS_PENDING),
            "encoding_only": {
                name: "gem accounting and full eligibility are not generalized"
                for name in sorted(O.ECONOMIC_ORDERS_PENDING)
            },
            "parameter_rules": rules,
            "note": "orders not in this list have not been verified against "
            "the game or are not yet economically complete; both are "
            "refused",
        }

    from dom6_assistant.agent import wiki_tools
    wiki_tools.register(reg)
    from dom6_assistant.agent import manual_tools
    manual_tools.register(reg)
    from dom6_assistant.agent import video_tools
    video_tools.register(reg)
    return reg


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_GEM_KEYS = ("fire", "air", "water", "earth", "astral", "death", "nature", "glamour", "blood")


def _gems(parsed) -> dict[str, int]:
    g = parsed.player_gems
    if g is None:
        return {}
    return {k: v for k in _GEM_KEYS if (v := getattr(g, k, 0))}


def _all_gems(parsed) -> dict[str, int]:
    """All nine paths, including zeroes when comparing budgets."""
    g = parsed.player_gems
    if g is None:
        return {}
    return {k: int(getattr(g, k, 0)) for k in _GEM_KEYS}


_PATH_LETTERS = "FAWESDNGBH"

_SCHOOL_NAMES = tuple(T.RESEARCH_SCHOOLS)
_SCHOOL_BY_LOWER = {name.lower(): i for i, name in enumerate(_SCHOOL_NAMES)}
# Effect 21 is the ordinary summon/revival family, confirmed by sixteen Ermor
# orders spanning four spells and both one-shot and monthly casting. Effect 164
# is the controlled Distill Gold conversion family. Other rituals without map
# range can still select a unit, enchantment or another object through fields
# whose roles are not yet separated.
VERIFIED_TARGETLESS_RITUAL_EFFECTS = RTR.NO_SELECTOR_RITUAL_EFFECTS
# A province is only one part of these orders: the player also selects a gem
# payload or a magic item through a field whose role is still unnamed.
RITUAL_EFFECTS_REQUIRING_OBJECT_SELECTION = frozenset({161})
GEM_TRANSPORT_RITUAL_EFFECTS = frozenset({160})
ITEM_TRANSPORT_RITUAL_SPELL_IDS = frozenset({1285, 1320})
# Gift of Reason and Divine Name share the controlled effect-39 troop-selector
# layout. Divine Name accepts Mindless targets; Gift of Reason does not.
UNIT_TARGET_RITUAL_SPELL_IDS = frozenset({1327, 1349})
MINDLESS_ALLOWED_UNIT_TARGET_RITUAL_SPELL_IDS = frozenset({1349})
# The field lists live in reference.unit_profile so the pregame chassis
# tools describe a unit exactly as lookup_unit does.
_UNIT_RESISTANCE_FIELDS = unit_profile.RESISTANCE_FIELDS
_UNIT_MOVEMENT_FIELDS = unit_profile.MOVEMENT_FIELDS
_UNIT_ABILITY_FIELDS = unit_profile.ABILITY_FIELDS


_ITEM_META_FIELDS = frozenset(
    {
        "id",
        "name",
        "type",
        "constlevel",
        "mainpath",
        "mainlevel",
        "secondarypath",
        "secondarylevel",
        "weapon",
        "armor",
        "end",
    }
)


def _nonzero(row: sqlite3.Row, fields: tuple[str, ...]) -> dict[str, Any]:
    """Selected non-zero extracted fields, without manufacturing booleans."""
    keys = set(row.keys())
    return {
        field: row[field] for field in fields if field in keys and row[field] not in (None, 0, "")
    }


def _named_bits(ctx: ToolContext, table: str, value: int) -> list[str]:
    rows = ctx.reference_db.execute(f"SELECT bit_value, bit_name FROM {table} ORDER BY bit_value")
    return [
        str(row["bit_name"])
        for row in rows
        if int(row["bit_value"] or 0) and value & int(row["bit_value"])
    ]


def _terrain(ctx: ToolContext, flags: int | None) -> dict[str, Any] | None:
    if flags is None:
        return None
    names = _named_bits(ctx, "map_terrain_types", int(flags))
    if not names:
        names = ["Plains"]
    return {"flags": int(flags), "types": names}


def _afflictions(ctx: ToolContext, mask: int) -> dict[str, Any]:
    return {"mask": int(mask), "names": _named_bits(ctx, "afflictions", int(mask))}


def _gem_yields(row: sqlite3.Row) -> dict[str, int]:
    return {
        name: int(row[letter] or 0)
        for name, letter in zip(_GEM_KEYS, "FAWESDNGB")
        if letter in row.keys() and row[letter]
    }


def _resolve_ids(ctx: ToolContext, table: str, ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    names = {
        int(row["id"]): row["name"]
        for row in ctx.reference_db.execute(
            f"SELECT id, name FROM {table} WHERE id IN ({marks})", ids
        )
    }
    return [{"id": value, "name": names.get(value)} for value in ids]


def _effect_brief(
    ctx: ToolContext, record_id: int | None, *, source: str = "spell"
) -> dict[str, Any] | None:
    if record_id is None:
        return None
    table = "effects_spells" if source == "spell" else "effects_weapons"
    row = ctx.reference_db.execute(
        f"SELECT e.*, i.name AS effect_name FROM {table} e "
        "LEFT JOIN effects_info i ON i.number=e.effect_number "
        "WHERE e.record_id=?",
        (record_id,),
    ).fetchone()
    if row is None:
        return None
    modifiers = _named_bits(ctx, "effect_modifier_bits", int(row["modifiers_mask"] or 0))
    return {
        "effect_number": row["effect_number"],
        "effect": row["effect_name"],
        "ritual": bool(row["ritual"]) if row["ritual"] is not None else None,
        "raw_argument": row["raw_argument"],
        "range": {
            "base": row["range_base"],
            "per_path_level": row["range_per_level"],
            "strength_divisor": row["range_strength_divisor"],
        },
        "area": {
            "base": row["area_base"],
            "per_path_level": row["area_per_level"],
            "battlefield_percent": row["area_battlefield_pct"],
        },
        "duration": row["duration"],
        "modifier_mask": row["modifiers_mask"],
        "modifiers": modifiers,
    }


def _path_str(p1, l1, p2, l2) -> str:
    """Render a path requirement from either encoding the reference data uses.

    The two tables disagree, which is worth stating rather than discovering:
    `spells` stores paths as **integers** indexing FAWESDNGBH, with -1 for "no
    second path"; `items` stores them as the **letters** themselves. Assuming
    the spell encoding for both raised a TypeError on the first item lookup.
    """

    def one(path, level) -> str:
        if path is None or path == "" or not level:
            return ""
        if isinstance(path, str):
            letter = path.strip().upper()
            return f"{letter}{level}" if letter in _PATH_LETTERS else ""
        if 0 <= path < len(_PATH_LETTERS):
            return f"{_PATH_LETTERS[path]}{level}"
        return ""  # -1: no second path

    return " ".join(x for x in (one(p1, l1), one(p2, l2)) if x)


def _artifact_forge_state(ctx: ToolContext, item) -> dict[str, Any] | None:
    """Current world availability for a Construction 7/9 artifact.

    Ordinary items may coexist in any quantity and do not use this as a forge
    gate. Artifacts are unique while an extant copy has a positive state; the
    client's -98 sentinel instead means yearning and halves each path's base
    gem price. If the player-file array is absent, artifact availability and
    price are both unknown and callers fail closed.
    """

    if int(item["constlevel"] or 0) <= 5:
        return None
    item_id = int(item["id"])
    states = getattr(ctx.view.parsed, "item_states", None)
    if states is None or not 0 <= item_id < len(states):
        return {
            "status": "unknown",
            "raw_state": None,
            "available": False,
            "yearning": None,
        }
    raw = int(states[item_id])
    if raw == T.ITEM_STATE_YEARNING:
        status = "yearning"
        available = True
    elif raw > 0:
        status = "already_exists"
        available = False
    else:
        status = "available"
        available = True
    return {
        "status": status,
        "raw_state": raw,
        "available": available,
        "yearning": raw == T.ITEM_STATE_YEARNING,
    }


def _weapon_brief(ctx: ToolContext, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "attack_modifier": row["att"],
        "defence_modifier": row["def"],
        "length": row["len"],
        "attacks": row["nratt"],
        "ammo": row["ammo"],
        "resource_cost": row["rcost"],
        "effect": _effect_brief(ctx, row["effect_record_id"], source="weapon"),
        "secondary_effect_record": row["secondaryeffect"],
        "secondary_effect_always_record": row["secondaryeffectalways"],
    }


def _armor_brief(ctx: ToolContext, row: sqlite3.Row) -> dict[str, Any]:
    protections = [
        {"zone": r["zone_number"], "protection": r["protection"]}
        for r in ctx.reference_db.execute(
            "SELECT zone_number, protection FROM protections_by_armor "
            "WHERE armor_number=? ORDER BY zone_number",
            (row["id"],),
        )
    ]
    attributes = []
    for attr in ctx.reference_db.execute(
        "SELECT a.attribute, a.raw_value, k.name FROM attributes_by_armor a "
        "LEFT JOIN attribute_keys k ON k.number=a.attribute "
        "WHERE a.armor_number=? ORDER BY a.attribute",
        (row["id"],),
    ):
        attributes.append(
            {"attribute": attr["attribute"], "name": attr["name"], "raw_value": attr["raw_value"]}
        )
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "defence_modifier": row["def"],
        "encumbrance": row["enc"],
        "resource_cost": row["rcost"],
        "protection_by_zone": protections,
        "extracted_attributes": attributes,
    }


def forgeable_item_ids(ctx: ToolContext, commander) -> set[int]:
    """Item ids this commander could forge right now.

    Shared with the writer deliberately. If the two grew separate copies of
    "researched and base paths met", the tool would offer a candidate the
    writer then refused, or worse the other way round.
    """
    levels = ctx.view.parsed.research_levels
    if levels is None or len(levels) <= 3:
        raise ToolError(
            "current Construction research could not be decoded; refusing to "
            "offer items whose research eligibility is unknown"
        )
    construction = int(levels[3])
    effective_paths, _adjustment, _sources = effective_forge_paths(ctx, commander)
    out: set[int] = set()
    for item in ctx.reference_db.execute("SELECT * FROM items"):
        if not FC.nation_can_forge(item, ctx.view.nation_id):
            continue
        artifact_state = _artifact_forge_state(ctx, item)
        if artifact_state is not None and not artifact_state["available"]:
            continue
        if int(item["constlevel"] or 0) > construction:
            continue
        requirements = (
            (item["mainpath"], item["mainlevel"]),
            (item["secondarypath"], item["secondarylevel"]),
        )
        if any(
            path and int(effective_paths.get(str(path).upper(), 0)) < int(required or 0)
            for path, required in requirements
        ):
            continue
        out.add(int(item["id"]))
    return out


FORGE_OF_THE_ANCIENTS = 1095
ELEMENTAL_DAMPENING = 1369


def _equipped_forge_items(
    ctx: ToolContext, commander
) -> list[tuple[str, sqlite3.Row]]:
    """Current plus pending equipment that can affect this turn's forge."""
    equipped: dict[str, int] = {}
    if ctx.h2_path is not None and ctx.h2_path.exists():
        data = ctx.h2_path.read_bytes()
        block = O.find_order_blocks(data).get(commander.commander_id)
        if block is not None:
            equipped = O.read_equipment(data, block.name_end)
        for pending in ctx.game_db.execute(
                "SELECT slot, item_id FROM current_equipment_intent WHERE "
                "game_id=? AND turn=? AND commander_id=?",
                (ctx.game_id, ctx.turn, commander.commander_id)):
            if int(pending["item_id"]):
                equipped[str(pending["slot"])] = int(pending["item_id"])
            else:
                equipped.pop(str(pending["slot"]), None)

    out: list[tuple[str, sqlite3.Row]] = []
    for slot, item_id in equipped.items():
        item = ctx.reference_db.execute(
            "SELECT * FROM items WHERE id=?", (item_id,)
        ).fetchone()
        if item is not None:
            out.append((slot, item))
    return out


def forge_cost_for_commander(
    ctx: ToolContext, commander, item: sqlite3.Row
) -> tuple[FC.ForgeCost, dict[str, Any]]:
    """Port the dynamic inputs consumed by client routine ``0x4d6490``."""
    percent = 0
    fixed = 0
    sources: list[str] = []

    if commander.type_id is not None:
        unit = ctx.reference_db.execute(
            "SELECT name, forgebonus, fixforgebonus FROM units WHERE id=?",
            (commander.type_id,),
        ).fetchone()
        if unit is not None:
            unit_percent = int(unit["forgebonus"] or 0)
            unit_fixed = int(unit["fixforgebonus"] or 0)
            percent += unit_percent
            fixed += unit_fixed
            if unit_percent:
                sources.append(
                    f"{unit['name']} Forge Bonus {unit_percent:+d}%")
            if unit_fixed:
                sources.append(
                    f"{unit['name']} fixed Forge Bonus {unit_fixed:+d}")

    for slot, equipped in _equipped_forge_items(ctx, commander):
        item_fixed = int(equipped["fixforge"] or 0)
        if item_fixed:
            fixed += item_fixed
            sources.append(
                f"{slot} item {equipped['name']} fixed Forge Bonus "
                f"{item_fixed:+d}")

    active = ctx.view.parsed.global_effects
    ancient_forge = any(
        effect.spell_id == FORGE_OF_THE_ANCIENTS
        and effect.caster_nation_id == ctx.view.nation_id
        for effect in active
    )
    if ancient_forge:
        percent += 20
        sources.append("our Forge of the Ancients +20%")

    construction_site = 0
    province = (
        ctx.view.province(commander.province_id)
        if commander.province_id is not None else None
    )
    if province is not None:
        for site_id in province.site_ids:
            site = ctx.reference_db.execute(
                'SELECT name, "const" FROM magic_sites WHERE id=?',
                (site_id,),
            ).fetchone()
            if site is None or not site["const"]:
                continue
            try:
                value = int(str(site["const"]).rstrip("%"))
            except ValueError:
                continue
            if value > construction_site:
                construction_site = value
                construction_site_name = str(site["name"])
        if construction_site:
            percent += construction_site
            sources.append(
                f"{construction_site_name} Construction Bonus "
                f"+{construction_site}%")

    dampened = (
        str(item["mainpath"] or "").upper() in {"F", "A", "W", "E"}
        and any(effect.spell_id == ELEMENTAL_DAMPENING for effect in active)
    )
    if dampened:
        percent -= 20
        sources.append("Elemental Dampening -20%")

    artifact_state = _artifact_forge_state(ctx, item)
    if artifact_state is not None and artifact_state["status"] == "unknown":
        raise FC.ForgeCostUnknown(
            f"artifact state for {item['name']} is absent from this player file")
    yearning = bool(artifact_state and artifact_state["yearning"])
    if yearning:
        sources.append("yearning artifact: base cost halved per path")
    cost = FC.forge_cost(
        item,
        nation_id=ctx.view.nation_id,
        forge_bonus_percent=percent,
        fixed_forge_bonus=fixed,
        yearning=yearning,
    )
    details = {
        "percentage_before_item_rules": percent,
        "fixed_before_item_rules": fixed,
        # Positive bonuses are capped separately for each path after the
        # national rebate is folded in.  This value is therefore the combined
        # ordinary percentage before that cap/rebate step, not a final rate.
        "combined_percentage_before_cap": cost.forge_bonus_percent,
        "effective_fixed": cost.fixed_forge_bonus,
        "sources": sources,
        "bonuses_suppressed_by_item": cost.bonuses_suppressed,
        "national_rebate": cost.rebated,
        "artifact_state": artifact_state,
        "yearning": cost.yearning,
    }
    return cost, details


def effective_forge_paths(
    ctx: ToolContext, commander
) -> tuple[dict[str, int], int, list[str]]:
    """Paths used only for item-forging eligibility.

    The reference column named ``mastersmith`` is signed: positive values are
    Master Smith and negative values are Inept Smith.  It changes each magic
    path the unit already has for the purpose of meeting an item's path
    requirement; it does not change the item's gem cost.  Worn and pending
    equipment can carry the same adjustment.
    """
    adjustment = 0
    sources: list[str] = []
    if commander.type_id is not None:
        unit = ctx.reference_db.execute(
            "SELECT name, mastersmith FROM units WHERE id=?",
            (commander.type_id,),
        ).fetchone()
        if unit is not None and int(unit["mastersmith"] or 0):
            value = int(unit["mastersmith"])
            adjustment += value
            label = "Master Smith" if value > 0 else "Inept Smith"
            sources.append(f"{label} ({value:+d}) from {unit['name']}")

    forge_items = _equipped_forge_items(ctx, commander)
    for slot, item in forge_items:
        if not int(item["mastersmith"] or 0):
            continue
        value = int(item["mastersmith"])
        adjustment += value
        label = "Master Smith" if value > 0 else "Inept Smith"
        sources.append(f"{label} ({value:+d}) from {slot} item {item['name']}")

    if any(
        effect.spell_id == FORGE_OF_THE_ANCIENTS
        and effect.caster_nation_id == ctx.view.nation_id
        for effect in ctx.view.parsed.global_effects
    ):
        adjustment += 1
        sources.append("Master Smith (+1) from our Forge of the Ancients")

    # Worn boosters are part of the path the client tests before applying the
    # forge-only Master/Inept Smith adjustment. A booster can establish a path
    # the chassis lacks; the smith adjustment then raises/lowers every resulting
    # positive path and clamps Inept Smith at zero.
    base_paths = {
        letter: int(commander.paths.get(letter, 0))
        + sum(int(item[letter] or 0) for _slot, item in forge_items)
        for letter in _PATH_LETTERS
    }
    effective = {
        path: max(0, level + adjustment)
        for path, level in base_paths.items()
        if int(level) > 0
    }
    return effective, adjustment, sources


def _item_brief(
    ctx: ToolContext,
    row: sqlite3.Row,
    *,
    full: bool = False,
    forger=None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "construction_level": row["constlevel"],
        "path": _path_str(
            row["mainpath"], row["mainlevel"], row["secondarypath"], row["secondarylevel"]
        ),
    }
    artifact_state = _artifact_forge_state(ctx, row)
    if artifact_state is not None:
        out["artifact_state"] = artifact_state
    try:
        if forger is None:
            if artifact_state is not None and artifact_state["status"] == "unknown":
                raise FC.ForgeCostUnknown(
                    "current artifact availability/yearning state is unknown")
            cost = FC.forge_cost(
                row,
                nation_id=ctx.view.nation_id,
                yearning=bool(artifact_state and artifact_state["yearning"]),
            )
            modifier_details = None
        else:
            cost, modifier_details = forge_cost_for_commander(ctx, forger, row)
    except FC.ForgeCostUnknown as exc:
        out["gem_cost"] = None
        out["gem_cost_source"] = f"not costable: {exc}"
    else:
        out["gem_cost"] = cost.listed
        out["gem_cost_source"] = (
            "forge screens and client cost table 1->5, 2->10, 3->15, 4->25, 5->40, "
            "6->60, 7->80, followed by the item's own path-cost fields"
        )
        out["gem_cost_charged"] = cost.charged
        if modifier_details is not None:
            out["forge_cost_modifiers"] = modifier_details
        if cost.rebated:
            # The screen shows the undiscounted figure and the reduction is
            # applied when the item is actually forged, so both numbers have
            # to be reported or the model cannot reconcile either one.
            out["national_rebate"] = (
                "our nation receives the client-defined one-gem reduction "
                "on every required path"
            )
    if row["weapon"]:
        weapon = ctx.reference_db.execute(
            "SELECT * FROM weapons WHERE id=?", (row["weapon"],)
        ).fetchone()
        out["weapon"] = (
            _weapon_brief(ctx, weapon)
            if full and weapon
            else {"id": row["weapon"], "name": weapon["name"] if weapon else None}
        )
    if row["armor"]:
        armor = ctx.reference_db.execute(
            "SELECT * FROM armors WHERE id=?", (row["armor"],)
        ).fetchone()
        out["armor"] = (
            _armor_brief(ctx, armor)
            if full and armor
            else {"id": row["armor"], "name": armor["name"] if armor else None}
        )
    # Item columns are named game effects. Returning only non-zero values keeps
    # a full lookup compact while retaining every extracted effect on the item.
    effects = {
        key: row[key]
        for key in row.keys()
        if key not in _ITEM_META_FIELDS and row[key] not in (None, 0, "")
    }
    if effects:
        out["effects"] = effects
        out["effects_source"] = (
            "named non-zero fields extracted from the game data; numeric "
            "values are engine values unless the field name states otherwise"
        )
    return out


def _spell_brief(ctx: ToolContext, row: sqlite3.Row, *, full: bool = False) -> dict[str, Any]:
    school = row["school"]
    school_name = (
        "Divine"
        if school == 7
        else _SCHOOL_NAMES[school]
        if isinstance(school, int) and 0 <= school < len(_SCHOOL_NAMES)
        else None
    )
    effect = _effect_brief(ctx, row["effect_record_id"])
    out: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "school_id": school,
        "school": school_name,
        "research_level": row["researchlevel"],
        "path": _path_str(row["path1"], row["pathlevel1"], row["path2"], row["pathlevel2"]),
        "fatigue": row["fatiguecost"],
        "gem_cost": row["gemcost"],
        "kind": (
            "ritual" if effect and effect["ritual"] else "battle_spell" if effect else "unknown"
        ),
        "effect": effect["effect"] if effect else None,
        "damage_or_effect_argument": row["damage"],
    }
    attributes = []
    for attr in ctx.reference_db.execute(
        "SELECT a.attribute, a.raw_value, k.name FROM attributes_by_spell a "
        "LEFT JOIN attribute_keys k ON k.number=a.attribute "
        "WHERE a.spell_number=? ORDER BY a.attribute",
        (row["id"],),
    ):
        attributes.append(
            {"attribute": attr["attribute"], "name": attr["name"], "raw_value": attr["raw_value"]}
        )
    if attributes:
        out["attributes"] = attributes
    if full:
        out["effect_details"] = effect
        out["precision_modifier"] = row["precision"]
        out["number_of_effects_raw"] = row["effects_count"]
        out["next_spell_record"] = row["next_spell"] or None
    return out


def _site_brief(ctx: ToolContext, row: sqlite3.Row, *, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "rarity": row["rarity"],
        "is_throne": 11 <= row["rarity"] <= 13,
        "path": row["path"],
        "level": row["level"],
        "gem_income": _gem_yields(row),
    }
    economy_fields = (
        "gold",
        "res",
        "sup",
        "unr",
        "exp",
        "recpoints",
        "recpointpercent",
        "recpointpercentcmd",
        "popgrowth",
        "bringgold",
        "bringres",
        "provinc",
        "agingpercent",
        "unaging",
    )
    economy = _nonzero(row, economy_fields)
    if economy:
        out["province_effects"] = economy
    unit_fields = tuple(
        f"{prefix}{i}" for prefix in ("hmon", "mon", "hcom", "com") for i in range(1, 6)
    )
    unit_ids = [int(row[field]) for field in unit_fields if field in row.keys() and row[field]]
    if unit_ids:
        out["recruitable_or_granted_units"] = _resolve_ids(ctx, "units", unit_ids)
    if row["rit"] or row["ritrng"]:
        out["ritual_range_bonus"] = {"path": row["rit"], "bonus": row["ritrng"]}
    if full:
        covered = {
            "id",
            "name",
            "rarity",
            "path",
            "level",
            "end",
            *"FAWESDNGB",
            *economy_fields,
            *unit_fields,
            "rit",
            "ritrng",
        }
        extra = {
            key: row[key]
            for key in row.keys()
            if key not in covered and row[key] not in (None, 0, "")
        }
        if extra:
            out["other_extracted_effects"] = extra
    return out


def _spell_requirements_met(spell: sqlite3.Row, paths: dict[str, int]) -> bool:
    for path_col, level_col in (("path1", "pathlevel1"), ("path2", "pathlevel2")):
        path = int(spell[path_col])
        required = int(spell[level_col] or 0)
        if path < 0 or required <= 0:
            continue
        if path >= len(_PATH_LETTERS) or int(paths.get(_PATH_LETTERS[path], 0)) < required:
            return False
    return True


def _research_met(
    spell: sqlite3.Row,
    levels: list[int] | None,
    learned_spell_ids: tuple[int, ...] | None = None,
) -> bool:
    school = spell["school"]
    return bool(
        isinstance(school, int)
        and T.spell_is_researched(
            levels,
            learned_spell_ids,
            int(spell["id"]),
            school,
            int(spell["researchlevel"] or 0),
        )
    )


def _spell_nation_allowed(ctx: ToolContext, spell_id: int) -> bool:
    restrictions = [
        int(row["raw_value"])
        for row in ctx.reference_db.execute(
            "SELECT raw_value FROM attributes_by_spell WHERE spell_number=? AND attribute=278",
            (spell_id,),
        )
    ]
    return not restrictions or ctx.nation_id in restrictions


def _nation_name(ctx: ToolContext, nation_id: int | None) -> str | None:
    if nation_id is None:
        return None
    row = ctx.reference_db.execute("SELECT name FROM nations WHERE id=?", (nation_id,)).fetchone()
    return str(row["name"]) if row is not None else None


def _nation_brief(ctx: ToolContext, row: sqlite3.Row, *, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "epithet": row["epithet"],
        "abbreviation": row["abbreviation"],
        "era": row["era"],
    }
    if not full:
        return out
    out["extracted_attributes"] = [
        {"attribute": attr["attribute"], "name": attr["name"], "raw_value": attr["raw_value"]}
        for attr in ctx.reference_db.execute(
            "SELECT a.attribute, a.raw_value, k.name "
            "FROM attributes_by_nation a "
            "JOIN attribute_keys k ON k.number=a.attribute "
            "WHERE a.nation_number=? AND k.name LIKE '%{Ntn:%' "
            "ORDER BY a.attribute",
            (row["id"],),
        )
    ]
    roster_tables = {
        "fort_commanders": "fort_leader_types_by_nation",
        "fort_troops": "fort_troop_types_by_nation",
        "nonfort_commanders": "nonfort_leader_types_by_nation",
        "nonfort_troops": "nonfort_troop_types_by_nation",
        "coastal_commanders": "coast_leader_types_by_nation",
        "coastal_troops": "coast_troop_types_by_nation",
        "pretender_chassis": "pretender_types_by_nation",
    }
    rosters: dict[str, list[dict[str, Any]]] = {}
    for label, table in roster_tables.items():
        ids = [
            int(found["monster_number"])
            for found in ctx.reference_db.execute(
                f"SELECT monster_number FROM {table} WHERE nation_number=?", (row["id"],)
            )
        ]
        if ids:
            rosters[label] = _resolve_ids(ctx, "units", ids)
    out["native_rosters"] = rosters
    out["source"] = "static extracted game data, not current rival state"
    return out


def _ref_lookup(
    ctx: ToolContext, table: str, name: str | None, row_id: int | None, limit: int
) -> list[sqlite3.Row]:
    if row_id is not None:
        rows = ctx.reference_db.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchall()
        if not rows:
            raise ToolError(f"no {table[:-1]} with id {row_id}.")
        return rows
    if not name or not name.strip():
        raise ToolError(
            f"lookup_{table[:-1]} needs either a name or its exact reference id parameter."
        )
    q = name.strip()
    rows = ctx.reference_db.execute(
        f"SELECT * FROM {table} WHERE name = ? COLLATE NOCASE", (q,)
    ).fetchall()
    if not rows:
        rows = ctx.reference_db.execute(
            f"SELECT * FROM {table} WHERE name LIKE ? ORDER BY LENGTH(name) LIMIT ?",
            (f"%{q}%", limit),
        ).fetchall()
    if not rows:
        raise ToolError(f"nothing in {table} matches {name!r}.")
    return rows


def _unit_brief(
    ctx: ToolContext, r: sqlite3.Row, observed_gold: int | None = None, *, full: bool = False
) -> dict[str, Any]:
    """Decision-safe unit summary, with an opt-in extracted detail layer."""
    paths = {p: r[p] for p in "FAWESDNGBH" if r[p]}
    flags = [
        k
        for k in (
            "holy",
            "stealthy",
            "assassin",
            "flying",
            "aquatic",
            "amphibian",
            "undead",
            "demon",
            "immobile",
            "spy",
            "forestsurvival",
            "mountainsurvival",
            "wastesurvival",
            "swampsurvival",
            "cavesurvival",
            "immortal",
            "sacred",
        )
        if k in r.keys() and r[k]
    ]
    out: dict[str, Any] = {
        "id": r["id"],
        "name": r["name"],
        "hp": r["hp"],
        "protection": r["prot"],
        "morale": r["mor"],
        "magic_resistance": r["mr"],
        "strength": r["str"],
        "attack": r["att"],
        "defence": r["def"],
        "precision": r["prec"],
        "encumbrance": r["enc"],
        "size": r["size"],
        "paths": paths or None,
        "flags": flags or None,
        "resource_cost": resource_cost_for(ctx.reference_db, int(r["id"])),
        "recruitment_point_cost": recruitment_point_cost_for(ctx.reference_db, int(r["id"])),
    }
    if observed_gold is not None:
        out["gold"] = observed_gold
        out["gold_source"] = "observed from the game's recruitment screen"
    else:
        out["gold"] = None
        out["gold_source"] = (
            "unknown — the reference cost field is unreliable for sacred and "
            "magical units; check the recruitment screen in game"
        )
    if full:
        weapon_ids = [int(r[f"wpn{i}"]) for i in range(1, 8) if r[f"wpn{i}"]]
        armor_ids = [int(r[f"armor{i}"]) for i in range(1, 5) if r[f"armor{i}"]]
        out["weapons"] = _resolve_ids(ctx, "weapons", weapon_ids)
        out["armor"] = _resolve_ids(ctx, "armors", armor_ids)
        out["resistances"] = _nonzero(r, _UNIT_RESISTANCE_FIELDS)
        out["movement_and_survival"] = _nonzero(r, _UNIT_MOVEMENT_FIELDS)
        out["abilities"] = _nonzero(r, _UNIT_ABILITY_FIELDS)
        out["leadership_reference_values"] = _nonzero(r, ("leader", "undeadleader", "magicleader"))
        random_paths = []
        for index in range(1, 7):
            chance = r[f"rand{index}"]
            if chance:
                random_paths.append(
                    {
                        "chance": chance,
                        "number": r[f"nbr{index}"],
                        "levels": r[f"link{index}"],
                        "path_mask": r[f"mask{index}"],
                    }
                )
        if random_paths:
            out["random_magic_raw"] = random_paths
        out["description"] = unit_profile.description(ctx.reference_db, int(r["id"]))
        limits = unit_profile.scale_limits(r)
        if limits:
            out["scale_limit_modifiers"] = limits
        out["reference_value_note"] = (
            "weapons, armor, traits and resistance fields are extracted from "
            "the game data. Leadership and random-path values are retained as "
            "reference values because every display normalization is not decoded."
        )
    return out


def _spell_queue(ctx: ToolContext, data: bytes, name_end: int) -> list[str]:
    """The five single-round order slots, rendered readably.

    Positive values are spell ids; the negatives are the fixed choices the
    game offers alongside them.
    """
    spells = {}
    raw = O.read_spell_queue(data, name_end)
    for value in raw:
        if value > 0 and value not in spells:
            row = ctx.reference_db.execute(
                "SELECT name FROM spells WHERE id=?", (value,)
            ).fetchone()
            if row:
                spells[value] = row["name"]
    return [O.describe_single_round(v, spells) for v in raw]


def _holy_cost(ctx: ToolContext, unit_type_id: int) -> tuple[bool, int]:
    """Whether a unit is sacred and its actual holy recruitment cost."""
    row = ctx.reference_db.execute(
        "SELECT holy, holycost FROM units WHERE id=?", (unit_type_id,)
    ).fetchone()
    sacred = bool(row and row["holy"])
    if not sacred:
        return False, 0
    # #holycost is absent for ordinary sacreds, whose default is one. A small
    # set of giants/elite sacreds explicitly costs two or three.
    explicit = int(row["holycost"] or 0)
    return True, explicit if explicit > 0 else 1


def _holy_plan(ctx: ToolContext, queue: Any, available: int | None) -> dict[str, Any] | None:
    """Holy points the queue will spend, against what the province has.

    Ordinary sacreds cost one. Units with ``#holycost`` can cost two or three;
    the executable-backed reference table supplies that exception.
    """
    sacred = []
    for position, recruit in enumerate(queue.recruits if queue else []):
        row = ctx.reference_db.execute(
            "SELECT name FROM units WHERE id=?", (recruit.unit_type_id,)
        ).fetchone()
        sacred_unit, cost = _holy_cost(ctx, recruit.unit_type_id)
        if row is not None and sacred_unit:
            sacred.append({"position": position, "name": row["name"], "holy_points": cost})
    if not sacred and available is None:
        return None
    out: dict[str, Any] = {
        "available": available,
        "spent_by_queue": sum(unit["holy_points"] for unit in sacred),
        "sacred_queued": sacred,
    }
    distinct_costs = {unit["holy_points"] for unit in sacred}
    if len(distinct_costs) == 1:
        out["cost_per_sacred_unit"] = next(iter(distinct_costs))
    if available is not None:
        out["remaining"] = available - out["spent_by_queue"]
    else:
        out["source"] = (
            "not computed because the current pretender Dominion "
            "could not be resolved from the .trn"
        )
    return out


def _remainders(
    ctx: ToolContext, province_id: int, queue: Any, out: dict[str, Any]
) -> dict[str, Any]:
    """What is left after the queue, per budget, each labelled with its basis.

    Gold is national and exact. Recruitment and commander points are exact for
    a province we own. Resources are only as good as the figure above them,
    which is why every remainder carries the source of the total it came from.

    Units already in the pristine base were paid for on the turn they were
    queued — the game leaves the remaining-gold figure alone for them — so
    charging them again would double-count. Only what we have added since is
    newly committed.
    """
    result: dict[str, Any] = {}
    committed_gold = sum(r.gold for r in queue.recruits) if queue else 0

    already_paid = 0
    base = ctx.h2_path.with_suffix(BASE_SUFFIX) if ctx.h2_path else None
    if base is not None and base.exists():
        from dom6_assistant.file_reader.formats import h2

        owned = [p.province_id for p in ctx.view.own_provinces()]
        try:
            prior = h2.parse(base, owned_provinces=owned)
            for q in prior.queues:
                if q.province_id == province_id:
                    already_paid = sum(r.gold for r in q.recruits)
        except Exception:
            already_paid = 0

    treasury = ctx.view.parsed.player_gold
    if treasury is not None:
        newly = max(committed_gold - already_paid, 0)
        result["gold"] = {
            "treasury": treasury,
            "committed_here": committed_gold,
            "already_paid_last_turn": already_paid,
            "newly_committed": newly,
            "remaining": treasury - newly,
            "source": "exact, from the .trn treasury and the .2h queue",
        }

    points = out.get("recruit_points_available")
    if points is not None:
        spent_points = out.get("recruit_points_total")
        result["recruitment_points"] = {
            "available": points,
            "spent_by_queue": spent_points,
            "remaining": (points - spent_points if spent_points is not None else None),
            "source": out.get("recruit_points_source"),
        }
        if spent_points is None:
            result["recruitment_points"]["why_unknown"] = (
                "at least one queued unit is absent from the executable cost "
                "extraction, so its cost is not guessed"
            )

    resources = out.get("resources_available")
    spent = out.get("resources_total", 0)
    if resources is not None:
        result["resources"] = {
            "available": resources,
            "spent_by_queue": spent,
            "remaining": resources - spent,
            "source": out.get("resources_source"),
            "exact": out.get("resources_exact", False),
        }

    commander_points = out.get("commander_points_available")
    if commander_points is not None:
        used = 0
        unknown = []
        for recruit in queue.recruits if queue else []:
            row = ctx.game_db.execute(
                "SELECT commander_points, is_commander FROM recruitable "
                "WHERE unit_type_id=? AND nation_id=?",
                (recruit.unit_type_id, ctx.nation_id),
            ).fetchone()
            if row is None:
                continue
            if row["is_commander"]:
                if row["commander_points"] is None:
                    unknown.append(recruit.unit_type_id)
                else:
                    used += row["commander_points"]
        result["commander_points"] = {
            "available": commander_points,
            "spent_by_queue": used,
            "remaining": commander_points - used,
            "unknown_cost_for": unknown,
            "source": "exact for commanders whose point cost we have observed",
        }

    holy = _holy_plan(ctx, queue, out.get("holy_points_available"))
    if holy:
        result["holy_points"] = holy
    return result


def _budget_note(out: dict[str, Any]) -> str:
    """One sentence per budget, saying how far to trust it."""
    parts = [
        "Anything that does not build stays queued and builds next turn, "
        "which is a legitimate way to commit to an expensive unit early."
    ]
    if out.get("resources_available") is None:
        parts.append(
            "Resources could not be computed; do not plan a tight queue against an unknown budget."
        )
    elif not out.get("resources_exact", False):
        parts.append(
            "Resources are computed from current save inputs, but a "
            "named exceptional input remains conditional; read the "
            "resource note before planning tightly."
        )
    else:
        parts.append(
            "Resources are current and computed with the decoded "
            "Dominions 6.35 calculator, revalidated on 6.36."
        )
    holy = out.get("holy_points")
    if holy and holy.get("remaining") is not None and holy["remaining"] < 0:
        parts.append(
            f"The queue wants {holy['spent_by_queue']} holy points "
            f"against {holy['available']} available."
        )
    return " ".join(parts)


def _latest_turn(ctx: ToolContext) -> int:
    row = ctx.game_db.execute(
        "SELECT id FROM turns WHERE game_id=? ORDER BY turn DESC LIMIT 1", (ctx.game_id,)
    ).fetchone()
    if row is None:
        raise ToolError("no turns ingested for this game.")
    return row["id"]


def _neighbours(ctx: ToolContext, turn_id: int) -> dict[int, set[int]]:
    """Adjacency, as an undirected graph.

    Stored one row per direction, but 50 of 870 links are recorded only one
    way. A border is a border, so both directions are taken — a missing reverse
    is a gap in the record rather than a one-way road, and treating it as one
    would have the assistant believe an army could enter a province it could
    not leave.
    """
    out: dict[int, set[int]] = {}
    for row in ctx.game_db.execute(
        "SELECT province_id, neighbour_id FROM province_links WHERE turn_id=?", (turn_id,)
    ):
        out.setdefault(row["province_id"], set()).add(row["neighbour_id"])
        out.setdefault(row["neighbour_id"], set()).add(row["province_id"])
    return out


def _treasury(ctx: ToolContext) -> list[int]:
    """Unequipped magic items belonging to our nation.

    The `.trn` is the turn-start state and does not change when an item is
    equipped during planning. The expanded `.2h` carries the authoritative
    current treasury; only the tiny unsaved turn-1 stub needs the fallback.
    """
    if ctx.h2_path is not None and ctx.h2_path.exists():
        try:
            return H2.read_item_stash(ctx.h2_path.read_bytes())
        except H2.QueueWriteRefused:
            pass
    for nation_id, _gold, _gems, header in T.read_nation_roster(ctx.view.data):
        if nation_id == ctx.nation_id:
            return T.read_item_stash(ctx.view.data, header)
    return []


def _magic_economy(ctx: ToolContext) -> dict[str, Any]:
    """Current stock, uncommitted pool, and visible owned-site production."""
    income = {name: 0 for name in _GEM_KEYS}
    provinces = []
    for province in ctx.view.own_provinces():
        produced = {name: 0 for name in _GEM_KEYS}
        sites = []
        for site_id in province.site_ids:
            row = ctx.reference_db.execute(
                "SELECT * FROM magic_sites WHERE id=?", (site_id,)
            ).fetchone()
            if row is None:
                continue
            yields = _gem_yields(row)
            for path, value in yields.items():
                produced[path] += value
                income[path] += value
            sites.append({"site_id": site_id, "name": row["name"], "gem_income": yields})
        if sites:
            provinces.append(
                {
                    "province_id": province.province_id,
                    "province_name": province.name,
                    "income": {k: v for k, v in produced.items() if v},
                    "sites": sites,
                }
            )
    uncommitted = None
    if ctx.h2_path is not None and ctx.h2_path.exists():
        try:
            values = H2.gem_remaining(ctx.h2_path.read_bytes())
        except (ValueError, H2.QueueWriteRefused):
            values = None
        if values is not None:
            uncommitted = {name: int(value) for name, value in zip(_GEM_KEYS, values)}
    return {
        "stock_at_turn_start": _all_gems(ctx.view.parsed),
        "uncommitted": uncommitted,
        "income": {k: v for k, v in income.items() if v},
        "by_province": provinces,
        "source": (
            "stock is from our .trn; uncommitted gems are from our .2h; "
            "income is summed from visible sites in provinces we own"
        ),
    }


def _gold_uncommitted(ctx: ToolContext) -> int | None:
    if ctx.h2_path is None or not ctx.h2_path.exists():
        return None
    try:
        return H2.gold_remaining(ctx.h2_path.read_bytes())
    except (ValueError, H2.QueueWriteRefused):
        return None


def _enemy_estimate(ctx: ToolContext, province_id: int) -> dict[str, Any]:
    """The panel's "about N enemy units", or an honest statement of ignorance."""
    try:
        intel = ctx.view.enemy_strength(province_id)
    except VisibilityError:
        return {
            "enemy_units": "unknown — we have not scouted this province. "
            "This does NOT mean it is empty. Sneak a stealthy Scout there; "
            "once present it reports automatically, with no separate Scout order."
        }
    return {
        "enemy_units": intel.enemy_units,
        "estimate_uncertainty_percent": intel.estimate_uncertainty_percent,
        "enemy_units_note": "the estimate the province panel shows, not an exact count",
        "intel_from_our_own_unit": intel.has_our_unit,
        **_scout_prose(ctx, province_id),
    }


def _scout_prose(ctx: ToolContext, province_id: int) -> dict[str, Any]:
    """Exact panel prose and conservative structure from its fixed sentences."""
    record = ctx.view.scout_reports().get(province_id)
    if record is None:
        return {
            "army_composition_report": None,
            "reported_commander": None,
            "scout_report_text": None,
        }
    text = record.text.strip()
    composition = None
    marker = "The army consists of "
    if marker in text:
        composition = text.partition(marker)[2].partition(".")[0].strip() or None
    commander = None
    marker = "The army appears to be commanded by "
    if marker in text:
        commander = text.partition(marker)[2].partition(".")[0].strip() or None
    return {
        "army_composition_report": composition,
        "reported_commander": commander,
        "scout_report_text": text,
    }


#: The nation lists in the reference data. Each maps a nation to unit ids it
#: may recruit; `fort_*` needs a fort, the others do not.
_NATION_LISTS = (
    ("fort_troop_types_by_nation", "troop", "needs a fort"),
    ("fort_leader_types_by_nation", "commander", "needs a fort"),
    ("nonfort_troop_types_by_nation", "troop", "anywhere"),
    ("nonfort_leader_types_by_nation", "commander", "anywhere"),
    ("coast_troop_types_by_nation", "troop", "coastal"),
    ("coast_leader_types_by_nation", "commander", "coastal"),
)

#: A site grants recruits through these columns. `hcom*` is the one that
#: matters here — Marignon's capital holds The House of Fiery Justice, whose
#: hcom1 and hcom2 are the High Inquisitor and Grand Master, and The Royal
#: Academy, whose hcom1 is the Architect. None of those three appear in any
#: nation list, which is why a nation-only view of recruitment is wrong.
_SITE_RECRUIT_COLUMNS = (
    "hcom1",
    "hcom2",
    "hcom3",
    "hcom4",
    "hcom5",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "natcom",
    "natmon",
    "mon1",
    "mon2",
    "mon3",
)


def _recruitable(ctx: ToolContext, province_id: int | None) -> dict[str, Any]:
    """Nation list plus site grants, with observed costs where we have them.

    Neither half alone is right. The nation lists miss anything a site unlocks;
    the harvested recruitment screen only ever covered commanders. Together
    they give the whole roster, and the costs stay honest — `null` where the
    price has not been seen, because the reference `basecost` is wrong for
    exactly the units worth thinking about.
    """
    costs = _observed_costs(ctx)
    screen = {
        r["name"]: r
        for r in ctx.game_db.execute(
            "SELECT name, gold, resources, commander_points, is_commander "
            "FROM recruitable WHERE nation_id=?",
            (ctx.nation_id,),
        )
    }
    out: dict[int, dict[str, Any]] = {}

    def add(unit_id: int, kind: str, source: str) -> None:
        row = ctx.reference_db.execute(
            "SELECT id, name FROM units WHERE id=?", (unit_id,)
        ).fetchone()
        if row is None:
            return
        seen = screen.get(row["name"])
        # Observed first, computed second, and the entry says which. A price
        # read off the game's own screen is authoritative; the ported formula
        # is close but reproduces dom6inspector rather than the game, and is
        # known to miss on mounted units.
        gold = costs.get(unit_id) or (seen["gold"] if seen else None)
        origin = "observed in game" if gold is not None else None
        if gold is None:
            gold = gold_cost_for(ctx.reference_db, unit_id, is_commander=(kind == "commander"))
            origin = "computed, approximate" if gold is not None else None
        entry = out.setdefault(
            unit_id,
            {
                "unit_type_id": unit_id,
                "name": row["name"],
                "kind": kind,
                "gold": gold,
                "gold_source": origin,
                "sources": [],
            },
        )
        if source not in entry["sources"]:
            entry["sources"].append(source)

    for table, kind, note in _NATION_LISTS:
        try:
            rows = ctx.reference_db.execute(
                f"SELECT monster_number FROM {table} WHERE nation_number=?", (ctx.nation_id,)
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            add(row["monster_number"], kind, note)

    sites = _sites_in(ctx, province_id)
    for site_id, site_name in sites:
        row = ctx.reference_db.execute(
            "SELECT * FROM magic_sites WHERE id=?", (site_id,)
        ).fetchone()
        if row is None:
            continue
        for column in _SITE_RECRUIT_COLUMNS:
            if column in row.keys() and row[column]:
                kind = "commander" if "com" in column else "troop"
                add(row[column], kind, f"site: {site_name}")

    unknown = [e["name"] for e in out.values() if e["gold"] is None]
    computed = [e["name"] for e in out.values() if e["gold_source"] == "computed, approximate"]
    result: dict[str, Any] = {
        "province_id": province_id,
        "recruitable": sorted(
            out.values(),
            key=lambda e: (e["kind"] != "commander", e["gold"] is None, -(e["gold"] or 0)),
        ),
    }
    if unknown:
        result["costs_unknown"] = unknown
    if computed:
        result["costs_computed"] = computed
        result["note"] = (
            "prices marked 'computed, approximate' come from a port of "
            "dom6inspector's formula, not from the game. It matches 13 of the "
            "15 prices we have observed; the two it misses are mounted units, "
            "where it reads 10 low. Treat them as close, not exact."
        )
    return result


def _sites_in(ctx: ToolContext, province_id: int | None) -> list[tuple[int, str]]:
    """Magic sites in a province, from the latest ingested turn.

    Restricted to provinces we own: a site grants recruits only where we can
    build, and site knowledge elsewhere is not a recruitment question.
    """
    ours = {p.province_id for p in ctx.view.own_provinces()}
    wanted = {province_id} & ours if province_id is not None else ours
    if not wanted:
        return []
    turn = ctx.game_db.execute(
        "SELECT id FROM turns WHERE game_id=? ORDER BY turn DESC LIMIT 1", (ctx.game_id,)
    ).fetchone()
    if turn is None:
        return []
    marks = ",".join("?" * len(wanted))
    rows = ctx.game_db.execute(
        f"SELECT DISTINCT site_id FROM province_sites WHERE turn_id=? AND province_id IN ({marks})",
        (turn["id"], *wanted),
    )
    out = []
    for row in rows:
        name = ctx.reference_db.execute(
            "SELECT name FROM magic_sites WHERE id=?", (row["site_id"],)
        ).fetchone()
        out.append((row["site_id"], name["name"] if name else str(row["site_id"])))
    return out


def _observed_costs(ctx: ToolContext) -> dict[int, int]:
    """Recruitment prices read off the game's own screen, by unit type id."""
    return {
        r["unit_type_id"]: r["gold"]
        for r in ctx.game_db.execute("SELECT unit_type_id, gold FROM observed_unit_costs")
    }


def _commanders(ctx: ToolContext):
    """Our commanders, via the boundary — never the raw cross-nation table."""
    return ctx.view.own_commanders(ctx.h2_path)


def _effective_equipment_rows(ctx: ToolContext, commander_id: int) -> list:
    """Reference rows for equipment after pending slot intents are applied."""
    equipped: dict[str, int] = {}
    if ctx.h2_path is not None and ctx.h2_path.exists():
        data = ctx.h2_path.read_bytes()
        block = O.find_order_blocks(data).get(commander_id)
        if block is not None:
            equipped.update(O.read_equipment(data, block.name_end))
    for pending in ctx.game_db.execute(
            "SELECT slot, item_id FROM current_equipment_intent WHERE "
            "game_id=? AND turn=? AND commander_id=?",
            (ctx.game_id, ctx.turn, commander_id)):
        slot = str(pending["slot"])
        item_id = int(pending["item_id"])
        if item_id:
            equipped[slot] = item_id
        else:
            equipped.pop(slot, None)
    rows = []
    for item_id in equipped.values():
        item = ctx.reference_db.execute(
            "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if item is not None:
            rows.append(item)
    return rows


def effective_magic_paths(ctx: ToolContext, commander) -> dict[str, int]:
    """Caster paths after worn and pending path-boosting equipment.

    Commander records retain intrinsic paths. Dominions nevertheless counts
    worn boosters when it offers and accepts spells: the controlled Wish caster
    Ren An is S8 intrinsically and reaches its S9 requirement through a Coin of
    Meteoritic Iron. Keep this separate from Master/Inept Smith, which applies
    only to forging eligibility.
    """

    paths = {
        letter: int(commander.paths.get(letter, 0))
        for letter in _PATH_LETTERS
    }
    for item in _effective_equipment_rows(ctx, commander.commander_id):
        for letter in _PATH_LETTERS:
            paths[letter] += int(item[letter] or 0)
    return {letter: level for letter, level in paths.items() if level > 0}


def _effective_commander_leadership(ctx: ToolContext, commander):
    """Final normal/undead/magic leadership for the staged turn."""
    if commander is None or commander.type_id is None:
        return None
    unit = ctx.reference_db.execute(
        "SELECT * FROM units WHERE id=?", (commander.type_id,)).fetchone()
    if unit is None:
        return None
    state = next(
        (candidate for candidate in ctx.view.own_units()
         if candidate.instance_id == commander.unit_instance_id),
        None,
    )
    return LD.leadership_for(
        unit,
        _effective_equipment_rows(ctx, commander.commander_id),
        experience=(state.experience if state is not None else 0),
    )


def _intent_rows(ctx: ToolContext) -> list[sqlite3.Row]:
    return list(
        ctx.game_db.execute(
            "SELECT * FROM current_orders WHERE game_id=? AND turn=? ORDER BY commander_id",
            (ctx.game_id, ctx.turn),
        )
    )


def _intent_parameter(ctx: ToolContext, row: sqlite3.Row) -> dict[str, Any]:
    """Render the legacy `destination` column according to its order type."""
    if row["order_name"] in O.RITUAL_ORDER_CODES:
        detail = ctx.game_db.execute(
            "SELECT * FROM ritual_intent WHERE order_intent_id=?", (row["id"],)
        ).fetchone()
        if detail is None:
            return {"spell_id": row["destination"], "ritual_details": "missing"}
        spell = ctx.reference_db.execute(
            "SELECT gemcost FROM spells WHERE id=?", (detail["spell_id"],)
        ).fetchone()
        base_gem_cost = int(spell["gemcost"] or 0) if spell is not None else None
        target_province = detail["target_province"]
        province = (
            ctx.view.province(int(target_province))
            if target_province is not None
            else None
        )
        target_commander = detail["target_commander_id"]
        commander = (
            next(
                (
                    candidate
                    for candidate in ctx.view.own_commanders(ctx.h2_path)
                    if candidate.commander_id == target_commander
                ),
                None,
            )
            if target_commander is not None
            else None
        )
        target_global = detail["target_global_effect_id"]
        target_unit = detail["target_unit_instance_id"]
        target_item = detail["target_item_id"]
        wish_item = detail["wish_item_id"]
        wish_unit = detail["wish_unit_id"]
        wish_nation = detail["wish_nation_id"]
        wish_result = detail["wish_result"]
        unit = (
            next(
                (
                    candidate for candidate in ctx.view.own_units()
                    if candidate.instance_id == target_unit
                    and not candidate.is_mount
                ),
                None,
            )
            if target_unit is not None
            else None
        )
        unit_type = (
            ctx.reference_db.execute(
                "SELECT name FROM units WHERE id=?", (unit.type_id,)
            ).fetchone()
            if unit is not None
            else None
        )
        item = (
            ctx.reference_db.execute(
                "SELECT name FROM items WHERE id=?", (target_item,)
            ).fetchone()
            if target_item is not None else None
        )
        wished_item = (
            ctx.reference_db.execute(
                "SELECT name FROM items WHERE id=?", (wish_item,)
            ).fetchone()
            if wish_item is not None else None
        )
        wished_unit = (
            ctx.reference_db.execute(
                "SELECT name FROM units WHERE id=?", (wish_unit,)
            ).fetchone()
            if wish_unit is not None else None
        )
        wished_nation = (
            ctx.reference_db.execute(
                "SELECT name FROM nations WHERE id=?", (wish_nation,)
            ).fetchone()
            if wish_nation is not None else None
        )
        active_global = (
            next(
                (
                    effect
                    for effect in T.read_global_effects(ctx.view.data)
                    if effect.effect_id == target_global
                ),
                None,
            )
            if target_global is not None
            else None
        )
        global_spell = (
            ctx.reference_db.execute(
                "SELECT name FROM spells WHERE id=?", (active_global.spell_id,)
            ).fetchone()
            if active_global is not None
            else None
        )
        out = {
            "spell_id": detail["spell_id"],
            "spell": detail["spell_name"],
            "gem_path": O.MAGIC_PATH_NAMES.get(
                detail["gem_path"], f"unknown({detail['gem_path']})"
            ),
            "gem_cost": detail["gem_cost"],
            "base_gem_cost": base_gem_cost,
            "extra_gems": (
                detail["gem_cost"] - base_gem_cost
                if base_gem_cost is not None else None
            ),
            "target_province": target_province,
            "target_province_name": province.name if province else None,
            "target_commander": target_commander,
            "target_commander_name": commander.name if commander else None,
            "target_unit": target_unit,
            "target_unit_type_id": unit.type_id if unit else None,
            "target_unit_name": unit_type["name"] if unit_type else None,
            "target_item": target_item,
            "target_item_name": item["name"] if item else None,
            "wish_item": wish_item,
            "wish_item_name": wished_item["name"] if wished_item else None,
            "wish_unit": wish_unit,
            "wish_unit_name": wished_unit["name"] if wished_unit else None,
            "wish_nation": wish_nation,
            "wish_nation_name": (
                wished_nation["name"] if wished_nation else None),
            "wish_result": wish_result,
            "wish_outcome": (
                {
                    "result": wish_result,
                    "result_code": WISH.SPECS[wish_result].code,
                    "outcome": WISH.SPECS[wish_result].outcome,
                    "visibility": WISH.SPECS[wish_result].visibility,
                }
                if wish_result in WISH.SPECS else None
            ),
            "target_global": target_global,
            "target_global_spell": global_spell["name"] if global_spell else None,
            "monthly": bool(detail["monthly"]),
        }
        spell_id = int(detail["spell_id"])
        no_selector_mode = RTR.spell_no_selector_mode(
            ctx.reference_db, spell_id)
        if RTR.spell_uses_implicit_local_target(ctx.reference_db, spell_id):
            out["target_mode"] = "caster_current_province_implicit"
        elif no_selector_mode is not None:
            out["target_mode"] = no_selector_mode
        duration_rate = RTR.spell_duration_extension_months_per_gem(
            ctx.reference_db, spell_id)
        if duration_rate is not None:
            extension_gems = (
                int(detail["gem_cost"]) - base_gem_cost
                if base_gem_cost is not None else None
            )
            out["duration_extension_gems"] = extension_gems
            out["duration_extension_months"] = (
                extension_gems * duration_rate
                if extension_gems is not None else None
            )
        return out
    if row["order_name"] == "forge_magic_item":
        detail = ctx.game_db.execute(
            "SELECT * FROM forge_intent WHERE order_intent_id=?", (row["id"],)
        ).fetchone()
        if detail is None:
            return {"item_id": row["destination"], "forge_details": "missing"}
        return {
            "item_id": detail["item_id"],
            "item_name": detail["item_name"],
            "gem_reservations": _gem_reservations(
                detail["gem_path"],
                detail["gem_cost"],
                detail["secondary_gem_path"],
                detail["secondary_gem_cost"],
            ),
        }
    return _parameter_fields(ctx, row["order_name"], row["destination"])


def _ritual_fields(
    ctx: ToolContext,
    caster: Any,
    ritual: O.RitualFields,
    commander_by_runtime: dict[int, Any],
    unit_by_runtime: dict[int, Any],
    global_by_slot: dict[int, T.TrnGlobalEffect],
) -> dict[str, Any]:
    """Describe the overloaded ritual fields by the selected spell's effect."""
    spell = ctx.reference_db.execute(
        "SELECT name, path1, pathlevel1, gemcost, effect_record_id "
        "FROM spells WHERE id=?",
        (ritual.spell_id,),
    ).fetchone()
    base_cost = int(spell["gemcost"] or 0) if spell is not None else None
    effect_numbers = (
        {
            int(row["effect_number"])
            for row in ctx.reference_db.execute(
                "SELECT effect_number FROM effects_spells "
                "WHERE record_id=? AND CAST(ritual AS INTEGER)=1",
                (spell["effect_record_id"],),
            )
        }
        if spell is not None
        else set()
    )
    out: dict[str, Any] = {
        "spell_id": ritual.spell_id,
        "spell": spell["name"] if spell else None,
        "gem_cost": ritual.gem_cost,
        "base_gem_cost": base_cost,
        "extra_gems": ritual.gem_cost - base_cost if base_cost is not None else None,
        "monthly": ritual.monthly,
    }
    no_selector_mode = RTR.spell_no_selector_mode(
        ctx.reference_db, ritual.spell_id)
    if RTR.spell_uses_implicit_local_target(
            ctx.reference_db, ritual.spell_id):
        out["target_mode"] = "caster_current_province_implicit"
    elif no_selector_mode is not None:
        out["target_mode"] = no_selector_mode
    duration_rate = RTR.spell_duration_extension_months_per_gem(
        ctx.reference_db, ritual.spell_id)
    if duration_rate is not None:
        extension_gems = (
            ritual.gem_cost - base_cost
            if base_cost is not None else None
        )
        out["duration_extension_gems"] = extension_gems
        out["duration_extension_months"] = (
            extension_gems * duration_rate
            if extension_gems is not None else None
        )

    if ritual.target_unit_runtime_index is not None:
        runtime = ritual.target_unit_runtime_index
        unit = unit_by_runtime.get(runtime)
        out["target_unit_runtime_index"] = runtime
        out["target_unit"] = unit.instance_id if unit is not None else None
        out["target_unit_type_id"] = unit.type_id if unit is not None else None
        if unit is not None:
            type_row = ctx.reference_db.execute(
                "SELECT name FROM units WHERE id=?", (unit.type_id,)
            ).fetchone()
            out["target_unit_name"] = type_row["name"] if type_row else None
        else:
            out["target_unit_status"] = "runtime handle did not resolve"
    elif ritual.wish_result_code is not None:
        out["target_mode"] = "wish_result"
        out["wish_result_code"] = ritual.wish_result_code
        out["wish_result"] = ritual.wish_result
        out["wish_payload"] = ritual.wish_payload
        out["wish_item"] = ritual.wish_item_id
        if ritual.wish_item_id is not None:
            item = ctx.reference_db.execute(
                "SELECT name FROM items WHERE id=?", (ritual.wish_item_id,)
            ).fetchone()
            out["wish_item_name"] = item["name"] if item else None
        out["wish_unit"] = ritual.wish_unit_id
        if ritual.wish_unit_id is not None:
            unit = ctx.reference_db.execute(
                "SELECT name FROM units WHERE id=?", (ritual.wish_unit_id,)
            ).fetchone()
            out["wish_unit_name"] = unit["name"] if unit else None
        out["wish_nation"] = ritual.wish_nation_id
        if ritual.wish_nation_id is not None:
            nation = ctx.reference_db.execute(
                "SELECT name FROM nations WHERE id=?", (ritual.wish_nation_id,)
            ).fetchone()
            out["wish_nation_name"] = nation["name"] if nation else None
        out["wish_amount"] = ritual.wish_amount
        if ritual.wish_result in WISH.SPECS:
            out["wish_outcome"] = {
                "outcome": WISH.SPECS[ritual.wish_result].outcome,
                "visibility": WISH.SPECS[ritual.wish_result].visibility,
            }
        else:
            out["wish_result_status"] = "unrecognized Wish result family"
    elif effect_numbers & RTR.GLOBAL_SELECTOR_RITUAL_EFFECTS:
        # Dispel, Disenchantment and Arcane Analysis: +124 is the stable
        # global-chain slot. Slot zero is a real target, not an absent field.
        slot = ritual.target_province if ritual.target_province is not None else 0
        target = global_by_slot.get(slot)
        out["target_global_slot"] = slot
        out["target_global"] = target.effect_id if target is not None else None
        if target is not None:
            target_spell = ctx.reference_db.execute(
                "SELECT name FROM spells WHERE id=?", (target.spell_id,)
            ).fetchone()
            out["target_global_spell_id"] = target.spell_id
            out["target_global_spell"] = target_spell["name"] if target_spell else None
        else:
            out["target_global_status"] = "no longer active in this slot"
    elif effect_numbers & {160, 161}:
        out["target_province"] = ritual.target_province
        province = (
            ctx.view.province(ritual.target_province)
            if ritual.target_province is not None
            else None
        )
        out["target_province_name"] = province.name if province else None
        runtime = ritual.target_commander_runtime_index
        recipient = commander_by_runtime.get(runtime) if runtime is not None else None
        out["target_commander"] = recipient.commander_id if recipient else None
        out["target_commander_name"] = recipient.name if recipient else None
        if runtime is not None and recipient is None:
            out["target_commander_runtime_index"] = runtime
            out["target_commander_status"] = "runtime handle did not resolve"
        if ritual.target_item_id is not None:
            item = ctx.reference_db.execute(
                "SELECT name FROM items WHERE id=?", (ritual.target_item_id,)
            ).fetchone()
            out["target_item"] = ritual.target_item_id
            out["target_item_name"] = item["name"] if item else None
    elif 81 not in effect_numbers:  # World globals have no selected object.
        out["target_province"] = ritual.target_province
        province = (
            ctx.view.province(ritual.target_province)
            if ritual.target_province is not None
            else None
        )
        out["target_province_name"] = province.name if province else None

    if effect_numbers & {30, 81} and spell is not None:
        path = "FAWESDNGBH"[int(spell["path1"])]
        caster_paths = effective_magic_paths(ctx, caster)
        excess_levels = max(
            0, int(caster_paths.get(path, 0)) - int(spell["pathlevel1"] or 0)
        )
        out["overcast"] = (out["extra_gems"] or 0) + 5 * excess_levels
    return out


def _gem_reservations(
    primary_path: int,
    primary_cost: int,
    secondary_path: int | None = None,
    secondary_cost: int = 0,
) -> dict[str, int]:
    reservations = {
        O.MAGIC_PATH_NAMES.get(int(primary_path), f"unknown({primary_path})"):
        int(primary_cost)
    }
    if secondary_path is not None and int(secondary_cost):
        reservations[
            O.MAGIC_PATH_NAMES.get(
                int(secondary_path), f"unknown({secondary_path})"
            )
        ] = int(secondary_cost)
    return reservations


def _forge_fields(ctx: ToolContext, forge: O.ForgeFields) -> dict[str, Any]:
    """Name an inherited forge and both path-specific gem reservations."""
    item = ctx.reference_db.execute(
        "SELECT name, mainpath, mainlevel, secondarypath, secondarylevel "
        "FROM items WHERE id=?",
        (forge.item_id,),
    ).fetchone()
    if item is None:
        return {
            "item_id": forge.item_id,
            "item_name": None,
            "primary_gem_cost": forge.gem_cost,
            "secondary_gem_cost": forge.secondary_gem_cost,
        }
    primary_name = FC.PATH_NAMES.get(item["mainpath"])
    secondary_name = FC.PATH_NAMES.get(item["secondarypath"])
    primary_path = O.MAGIC_PATH_CODES.get(primary_name)
    secondary_path = O.MAGIC_PATH_CODES.get(secondary_name)
    reservations: dict[str, int] = {}
    if primary_name is not None:
        reservations[primary_name] = forge.gem_cost
    if secondary_name is not None and forge.secondary_gem_cost:
        reservations[secondary_name] = forge.secondary_gem_cost
    return {
        "item_id": forge.item_id,
        "item_name": item["name"],
        "gem_reservations": reservations,
        "primary_gem_path": primary_path,
        "primary_gem_cost": forge.gem_cost,
        "secondary_gem_path": secondary_path,
        "secondary_gem_cost": forge.secondary_gem_cost,
    }


def _parameter_fields(ctx: ToolContext, name: str | None, raw: int | None) -> dict[str, Any]:
    """Render one raw +116 value according to its strategic order type."""
    spec = O.ORDER_SPECS.get(name)
    if spec is None:
        return {"parameter": raw}
    kind = spec.parameter_kind
    if kind == O.PARAM_NONE:
        # Keep `destination: null` for compatibility with clients that used it
        # to distinguish local orders from movement.
        return {"destination": None}
    if kind in (O.PARAM_PROVINCE, O.PARAM_PROVINCE_OR_ZERO):
        return {"destination": raw or None}
    if kind == O.PARAM_CURRENT_PROVINCE:
        province = ctx.view.province(raw) if raw else None
        return {
            "current_province_id": raw or None,
            "current_province": province.name if province is not None else None,
        }
    if kind == O.PARAM_MAGIC_PATH:
        return {"magic_path": O.MAGIC_PATH_NAMES.get(raw, f"unknown({raw})")}
    if kind == O.PARAM_BUILDING:
        return {"building": O.BUILDING_NAMES.get(raw, f"unknown({raw})")}
    if kind == O.PARAM_ITEM:
        item = ctx.reference_db.execute("SELECT name FROM items WHERE id=?", (raw,)).fetchone()
        return {"item_id": raw, "item_name": item["name"] if item else None}
    return {"parameter": raw}


def _own_troops(ctx: ToolContext) -> list:
    """Our ordinary units, excluding commander and mount records.

    Commander records share the 173-byte unit shape and used to inflate army
    and garrison counts.  The runtime-index join now identifies their exact
    instance ids, so this filter is structural rather than a unit-type guess.
    """
    commander_instances = {
        commander.unit_instance_id
        for commander in ctx.view.own_commanders(ctx.h2_path)
        if commander.unit_instance_id is not None
    }
    return [
        unit
        for unit in ctx.view.own_units()
        if not unit.is_mount and unit.instance_id not in commander_instances
    ]


def _type_counts(ctx: ToolContext, units) -> dict[str, int]:
    """Unit type ids resolved to names, so a count means something."""
    counts: dict[int, int] = {}
    for u in units:
        counts[u.type_id] = counts.get(u.type_id, 0) + 1
    if not counts:
        return {}
    q = ",".join("?" * len(counts))
    names = {
        r["id"]: r["name"]
        for r in ctx.reference_db.execute(
            f"SELECT id, name FROM units WHERE id IN ({q})", list(counts)
        )
    }
    return {
        names.get(tid, f"type {tid}"): n for tid, n in sorted(counts.items(), key=lambda kv: -kv[1])
    }


def _unit_type_names(ctx: ToolContext, type_ids: set[int]) -> dict[int, str]:
    """Resolve a set of unit type ids without one query per soldier."""
    if not type_ids:
        return {}
    marks = ",".join("?" * len(type_ids))
    return {
        row["id"]: row["name"]
        for row in ctx.reference_db.execute(
            f"SELECT id, name FROM units WHERE id IN ({marks})", sorted(type_ids)
        )
    }


def _h2_unit_instances(
    ctx: ToolContext, data: bytes, province_id: int | None
) -> list[dict[str, Any]]:
    """Current own troop records with their authoritative warband owner."""
    units = O.read_h2_units(data, ctx.nation_id)
    by_offset = {unit.offset: unit for unit in units}
    names = _unit_type_names(ctx, {unit.type_id for unit in units})
    conditions = {unit.instance_id: unit for unit in ctx.view.own_units()}

    own_commanders = {
        commander.commander_id: commander for commander in ctx.view.own_commanders(ctx.h2_path)
    }
    commander_instances = {
        commander.unit_instance_id
        for commander in own_commanders.values()
        if commander.unit_instance_id is not None
    }
    blocks = O.find_order_blocks(data)
    assignments: dict[int, dict[str, Any]] = {}
    for commander_id, commander in own_commanders.items():
        block = blocks.get(commander_id)
        if block is None:
            continue
        for squad in O.read_squad_slots(data, block.name_end):
            token = squad["token"]
            assignments[token] = {
                "commander_id": commander_id,
                "commander_name": commander.name,
                "squad": squad["slot"],
            }
    pending = {
        int(row["unit_instance_id"]): row
        for row in ctx.game_db.execute(
            "SELECT unit_instance_id, destination, target_commander_id, "
            "commander_name, target_squad FROM "
            "current_troop_assignment_intent WHERE game_id=? AND turn=?",
            (ctx.game_id, ctx.turn),
        )
    }

    out = []
    for unit in units:
        if unit.is_mount or unit.instance_id in commander_instances:
            continue
        current_province = struct.unpack_from("<H", data, unit.offset + 4)[0]
        if province_id is not None and current_province != province_id:
            continue
        mount = by_offset.get(unit.offset + U.RECORD_SIZE)
        entry: dict[str, Any] = {
            "instance_id": unit.instance_id,
            "unit_type_id": unit.type_id,
            "name": names.get(unit.type_id),
            "province_id": current_province,
            "includes_mount": bool(mount and mount.is_mount),
            "assignment": assignments.get(unit.warband),
        }
        intent = pending.get(unit.instance_id)
        if intent is not None:
            if intent["destination"] == "garrison":
                entry["recorded_assignment"] = None
                entry["recorded_destination"] = "province_garrison"
            else:
                entry["recorded_assignment"] = {
                    "commander_id": intent["target_commander_id"],
                    "commander_name": intent["commander_name"],
                    "squad": intent["target_squad"],
                }
        condition = conditions.get(unit.instance_id)
        if condition is not None:
            entry.update(
                {
                    "hp": condition.hp,
                    "age": condition.age,
                    "experience": condition.experience,
                    "kills": condition.kills,
                    "afflictions": _afflictions(ctx, condition.afflictions),
                    "has_fought": condition.has_fought,
                    "home_province_id": condition.home_province_id,
                    "is_mercenary": condition.is_mercenary,
                }
            )
            if condition.home_province_id is not None:
                home = ctx.view.province(condition.home_province_id)
                entry["home_province"] = home.name if home else None
        if unit.warband != O.NO_SQUAD and entry["assignment"] is None:
            entry["assignment"] = {
                "status": "unknown squad token",
                "token": unit.warband,
            }
        out.append(entry)
    return out
