"""The client's one-turn strategic movement calculation.

The ``.2h`` stores only an order and final destination.  Highlighted provinces
are derived from the current army, terrain, ownership, scales and map borders.
Dominions computes ordinary movement as a shortest path over *half-province*
costs; Flying and Sailing use the same map graph with their own legality and
cost rules.  Keep all three here so read and write tools cannot disagree.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import heapq
from typing import Any, TYPE_CHECKING

from dom6_assistant.file_reader.formats import units as U
from dom6_assistant.file_reader.formats import mapfile as MF
from dom6_assistant.orders import orders_2h as O

if TYPE_CHECKING:
    from dom6_assistant.agent.registry import ToolContext


SEA = 0x04
HIGHLANDS = 0x10
SWAMP = 0x20
WASTE = 0x40
FOREST = 0x80
CAVE = 0x1000

MOUNTAIN_PASS = 0x01
RIVER = 0x02
IMPASSABLE = 0x04
ROAD = 0x08
BRIDGE = 0x10
DECORATIVE_BORDER_MOUNTAINS = 0x20

PERPETUAL_STORM = 708
SEA_OF_ICE = 880

# Official 6.36 modding manual: ordinary Sailing crosses up to two sea
# provinces; each point of the Windcatcher Sail's `farsail` adds one.
BASE_SAILING_SEA_PROVINCES = 2


@dataclass(frozen=True)
class MovementProfile:
    map_move: int
    flying: bool
    teleport: bool
    stealthy: bool
    forest_survival: bool
    mountain_survival: bool
    waste_survival: bool
    swamp_survival: bool
    winter_move: bool
    river_crossing: bool
    no_river_pass: bool
    can_enter_water: bool
    can_enter_land: bool
    follower_count: int
    flying_transport_capacity: int = 0
    flying_transport_used: int = 0
    flying_map_move: int | None = None


def _map_metadata(ctx: ToolContext) -> tuple[dict[tuple[int, int], int], int]:
    """Return global-id borders and the map's configured Sailing distance.

    Province numbers restart at one in each plane's ``.map``.  The ``.trn``
    graph numbers planes consecutively, so applying a cumulative offset is
    essential: storing both maps under their local ids lets the cave plane
    silently overwrite surface borders.
    """
    borders: dict[tuple[int, int], int] = {}
    offset = 0
    sail_distance = BASE_SAILING_SEA_PROVINCES
    paths = MF.find_for_save(ctx.save_dir)
    for index, path in enumerate(paths):
        parsed = MF.parse(path)
        if index == 0:
            sail_distance = parsed.sail_distance
        for (a, b), flags in parsed.borders.items():
            borders[(min(a + offset, b + offset), max(a + offset, b + offset))] = flags
        offset += parsed.province_count
    if not paths:
        # Snapshot fixtures made before map capture still have the static map
        # table in their copied game database.
        borders = {
            (min(int(row["province_id"]), int(row["neighbour_id"])),
             max(int(row["province_id"]), int(row["neighbour_id"]))):
            int(row["border_flags"])
            for row in ctx.game_db.execute(
                "SELECT province_id, neighbour_id, border_flags FROM map_borders "
                "WHERE game_id=?", (ctx.game_id,))
        }
    return borders, sail_distance


def _ground_half_cost(flags: int, profile: MovementProfile) -> int:
    """Official 6.36 half-province cost before owner/snow/road modifiers."""
    if flags & SEA:
        return 5
    if flags & CAVE:
        costs = [4]
        if flags & FOREST:
            costs.append(6 - (2 if profile.forest_survival else 0))
        if flags & HIGHLANDS:
            costs.append(6 - (2 if profile.mountain_survival else 0))
        if flags & SWAMP:
            costs.append(7 - (2 if profile.swamp_survival else 0))
        if flags & WASTE:
            costs.append(5 - (2 if profile.waste_survival else 0))
        return max(costs)
    costs = [3]
    if flags & FOREST:
        costs.append(5 - (2 if profile.forest_survival else 0))
    if flags & WASTE:
        costs.append(5 - (2 if profile.waste_survival else 0))
    if flags & HIGHLANDS:
        costs.append(6 - (2 if profile.mountain_survival else 0))
    if flags & SWAMP:
        costs.append(7 - (2 if profile.swamp_survival else 0))
    return max(costs)


def _half_cost(
    flags: int,
    heat_scale: int,
    owner: int,
    nation_id: int,
    profile: MovementProfile,
    *,
    road: bool,
) -> int:
    if profile.flying and not flags & SEA:
        cost = 5 if flags & CAVE else 3
        if owner != nation_id:
            cost += 1
        return cost
    cost = _ground_half_cost(flags, profile)
    if owner != nation_id:
        cost += 3 if profile.stealthy else 4
    # Province scales in the parser use the panel convention: negative is Cold.
    if heat_scale < 0 and not profile.winter_move:
        cost += 1
    if road:
        cost = max(2, cost - 2)
    return cost


def _effective_equipment(ctx: ToolContext, commander_id: int) -> dict[str, int]:
    if ctx.h2_path is None or not ctx.h2_path.exists():
        return {}
    data = ctx.h2_path.read_bytes()
    block = O.find_order_blocks(data).get(commander_id)
    if block is None:
        return {}
    equipped = O.read_equipment(data, block.name_end)
    for row in ctx.game_db.execute(
        "SELECT slot, item_id FROM current_equipment_intent "
        "WHERE game_id=? AND turn=? AND commander_id=?",
        (ctx.game_id, ctx.turn, commander_id),
    ):
        if int(row["item_id"]):
            equipped[str(row["slot"])] = int(row["item_id"])
        else:
            equipped.pop(str(row["slot"]), None)
    return equipped


def _final_followers(ctx: ToolContext, commander_id: int) -> list[Any]:
    """Follower records after current troop-assignment intent is applied."""
    if ctx.h2_path is None or not ctx.h2_path.exists():
        return []
    data = ctx.h2_path.read_bytes()
    commanders = ctx.view.own_commanders(ctx.h2_path)
    commander_instances = {
        commander.unit_instance_id for commander in commanders
        if commander.unit_instance_id is not None
    }
    token_owner: dict[int, int] = {}
    blocks = O.find_order_blocks(data)
    for commander in commanders:
        block = blocks.get(commander.commander_id)
        if block is None:
            continue
        try:
            squads = O.read_squad_slots(data, block.name_end)
        except ValueError:
            # A commander with no real squad can contain bytes that resemble a
            # slot header.  Troop records below remain authoritative; do not
            # make an unrelated commander's malformed lookalike suppress all
            # movement options.
            squads = []
        for squad in squads:
            token_owner[int(squad["token"])] = commander.commander_id

    pending = {
        int(row["unit_instance_id"]): (
            None if row["destination"] == "garrison"
            else int(row["target_commander_id"]))
        for row in ctx.game_db.execute(
            "SELECT unit_instance_id, target_commander_id, destination FROM "
            "current_troop_assignment_intent WHERE game_id=? AND turn=?",
            (ctx.game_id, ctx.turn),
        )
    }
    followers = []
    for unit in O.read_h2_units(data, ctx.nation_id):
        if unit.is_mount or unit.instance_id in commander_instances:
            continue
        owner = pending.get(unit.instance_id, token_owner.get(unit.warband))
        if owner == commander_id:
            followers.append(unit)
    return followers


_UNIT_FIELDS = (
    "id,name,mapmove,size,flying,teleport,stealthy,forestsurvival,"
    "mountainsurvival,wastesurvival,swampsurvival,aquatic,amphibian,"
    "pooramphibian,float,waterbreathing,swimming,snowmove,noriverpass,"
    "stormimmune"
)


def _rows_for_types(ctx: ToolContext, type_ids: set[int]) -> dict[int, Any]:
    if not type_ids:
        return {}
    marks = ",".join("?" * len(type_ids))
    return {
        int(row["id"]): row
        for row in ctx.reference_db.execute(
            f"SELECT {_UNIT_FIELDS} FROM units WHERE id IN ({marks})",
            sorted(type_ids),
        )
    }


def _movement_items(ctx: ToolContext, commander_id: int) -> list[Any]:
    ids = list(_effective_equipment(ctx, commander_id).values())
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return list(ctx.reference_db.execute(
        "SELECT id,name,mapmovebonus,forest,mount,waste,swamp,fly,float,"
        "waterbreathing,giftofwater,airbr,wintermove,unhindered,floating,"
        "flyingmaxtotalsize,flyingmapmove,swimming,stormimmune,farsail "
        f"FROM items WHERE id IN ({marks})",
        ids,
    ))


def army_movement_profile(ctx: ToolContext, commander: Any) -> MovementProfile:
    """Aggregate the commander, current followers and equipped movement items."""
    if commander.type_id is None:
        raise ValueError("commander type is unknown")
    followers = _final_followers(ctx, commander.commander_id)
    all_h2 = (
        O.read_h2_units(ctx.h2_path.read_bytes(), ctx.nation_id)
        if ctx.h2_path is not None and ctx.h2_path.exists() else []
    )
    by_offset = {unit.offset: unit for unit in all_h2}
    # Mounts are serialized immediately after their riders.  They constrain
    # movement abilities and speed, but do not count as a second passenger.
    mounts = [
        mount
        for follower in followers
        if (mount := by_offset.get(follower.offset + U.RECORD_SIZE)) is not None
        and mount.is_mount
    ]
    type_ids = {
        int(unit.type_id) for unit in [*followers, *mounts]
    } | {int(commander.type_id)}
    rows = _rows_for_types(ctx, type_ids)
    if int(commander.type_id) not in rows:
        raise ValueError(f"missing unit definition for type {commander.type_id}")
    commander_row = rows[int(commander.type_id)]
    follower_rows = [rows[int(unit.type_id)] for unit in followers if int(unit.type_id) in rows]
    mount_rows = [rows[int(unit.type_id)] for unit in mounts if int(unit.type_id) in rows]
    if len(follower_rows) != len(followers) or len(mount_rows) != len(mounts):
        raise ValueError("one or more army unit definitions are missing")

    items = _movement_items(ctx, commander.commander_id)
    commander_move = int(commander_row["mapmove"] or 0) + sum(
        int(item["mapmovebonus"] or 0) for item in items)
    component_rows = [commander_row, *follower_rows, *mount_rows]
    component_moves = [commander_move, *(
        int(row["mapmove"] or 0) for row in [*follower_rows, *mount_rows]
    )]

    def item_flag(name: str) -> bool:
        return any(int(item[name] or 0) for item in items)

    def all_have(name: str, *, commander_item: str | None = None) -> bool:
        commander_has = bool(int(commander_row[name] or 0))
        if commander_item is not None:
            commander_has = commander_has or item_flag(commander_item)
        return commander_has and all(bool(int(row[name] or 0)) for row in component_rows[1:])

    # Each passenger's mounted footprint is the larger of rider and mount.
    mount_by_rider = {
        follower.offset: by_offset.get(follower.offset + U.RECORD_SIZE)
        for follower in followers
    }
    follower_size = 0
    for follower in followers:
        rider = rows[int(follower.type_id)]
        size = int(rider["size"] or 0)
        mount = mount_by_rider[follower.offset]
        if mount is not None and mount.is_mount and int(mount.type_id) in rows:
            size = max(size, int(rows[int(mount.type_id)]["size"] or 0))
        follower_size += size

    natural_flying = all_have("flying", commander_item="fly")
    transport_capacity = max(
        (int(item["flyingmaxtotalsize"] or 0) for item in items),
        default=0,
    )
    transported_flying = bool(transport_capacity and follower_size <= transport_capacity)
    flying = natural_flying or transported_flying
    flying_move = max(
        (int(item["flyingmapmove"] or 0) for item in items),
        default=0,
    ) or None
    if transported_flying and flying_move:
        map_move = flying_move
    elif natural_flying:
        adjusted = list(component_moves)
        if item_flag("fly") and flying_move:
            adjusted[0] = flying_move
        map_move = min(adjusted)
    else:
        map_move = min(component_moves)

    active_globals = {effect.spell_id for effect in ctx.view.parsed.global_effects}
    if PERPETUAL_STORM in active_globals:
        storm_safe = all_have("stormimmune", commander_item="stormimmune")
        if not storm_safe:
            flying = False
            map_move = min(component_moves)

    water_fields = ("aquatic", "amphibian", "pooramphibian", "waterbreathing")
    commander_water = any(int(commander_row[name] or 0) for name in water_fields)
    commander_water = commander_water or item_flag("waterbreathing")
    gift_capacity = sum(int(item["giftofwater"] or 0) for item in items)
    follower_water = all(
        any(int(row[name] or 0) for name in water_fields)
        for row in [*follower_rows, *mount_rows]
    )
    if gift_capacity >= follower_size:
        follower_water = True
    can_enter_water = commander_water and follower_water
    can_enter_land = all(
        not int(row["aquatic"] or 0)
        or int(row["amphibian"] or 0)
        or int(row["pooramphibian"] or 0)
        for row in component_rows
    )

    river_crossing = all(
        any(int(row[name] or 0) for name in (
            "flying", "float", "swimming", "aquatic", "amphibian",
            "pooramphibian", "waterbreathing",
        ))
        for row in component_rows
    )
    if item_flag("fly") or item_flag("float") or item_flag("floating") \
            or item_flag("swimming") or item_flag("waterbreathing"):
        commander_crosses = True
    else:
        commander_crosses = any(int(commander_row[name] or 0) for name in (
            "flying", "float", "swimming", "aquatic", "amphibian",
            "pooramphibian", "waterbreathing",
        ))
    river_crossing = commander_crosses and all(
        any(int(row[name] or 0) for name in (
            "flying", "float", "swimming", "aquatic", "amphibian",
            "pooramphibian", "waterbreathing",
        )) for row in component_rows[1:]
    )

    return MovementProfile(
        map_move=map_move,
        flying=flying,
        teleport=not followers and bool(int(commander_row["teleport"] or 0)),
        stealthy=all(bool(int(row["stealthy"] or 0)) for row in component_rows),
        forest_survival=all_have("forestsurvival", commander_item="forest"),
        mountain_survival=all_have("mountainsurvival", commander_item="mount"),
        waste_survival=all_have("wastesurvival", commander_item="waste"),
        swamp_survival=all_have("swampsurvival", commander_item="swamp"),
        winter_move=all_have("snowmove", commander_item="wintermove"),
        river_crossing=river_crossing,
        no_river_pass=any(bool(int(row["noriverpass"] or 0)) for row in component_rows),
        can_enter_water=can_enter_water,
        can_enter_land=can_enter_land,
        follower_count=len(followers),
        flying_transport_capacity=transport_capacity,
        flying_transport_used=follower_size,
        flying_map_move=flying_move,
    )


def province_graph(ctx: ToolContext) -> dict[int, set[int]]:
    """Return the parsed map links as an undirected graph.

    A small number of map links are serialized in only one direction, so the
    movement calculation must not mistake those records for one-way borders.
    """
    graph: dict[int, set[int]] = {}
    for province in ctx.view.parsed.provinces:
        graph.setdefault(province.province_id, set())
        for neighbour_id in province.neighbours:
            graph[province.province_id].add(neighbour_id)
            graph.setdefault(neighbour_id, set()).add(province.province_id)
    return graph


def _province_records(ctx: ToolContext) -> dict[int, Any]:
    return {province.province_id: province for province in ctx.view.parsed.provinces}


def _edge_cost(
    ctx: ToolContext,
    source: Any,
    destination: Any,
    profile: MovementProfile,
    border: int,
) -> tuple[int | None, str | None, bool]:
    """Return ``(cost, blocker, stop_after_edge)`` for one graph edge."""
    source_flags = int(source.current_terrain or source.terrain_flags or 0)
    destination_flags = int(
        destination.current_terrain or destination.terrain_flags or 0)
    source_sea = bool(source_flags & SEA)
    destination_sea = bool(destination_flags & SEA)
    active_globals = {effect.spell_id for effect in ctx.view.parsed.global_effects}

    if profile.teleport:
        # The Teleport capability bypasses the terrain/owner/sea and special-
        # border branches in 0x1e21a0.  0x1dbd10 then returns the plain flying
        # half-cost (3) in both directions.
        return 6, None, False
    if border & IMPASSABLE:
        return None, "impassable border", True
    if border & MOUNTAIN_PASS:
        if PERPETUAL_STORM in active_globals:
            return None, "mountain pass closed by Perpetual Storm", True
        warm = int(source.heat_scale or 0) > 0 and int(destination.heat_scale or 0) > 0
        if not (warm or profile.flying or profile.mountain_survival):
            return None, "mountain pass is closed in the current cold", True
    if border & RIVER and not border & BRIDGE:
        if profile.no_river_pass:
            return None, "army contains a unit that cannot cross rivers", True
        frozen = int(source.heat_scale or 0) < 0 and int(destination.heat_scale or 0) < 0
        if not (frozen or profile.river_crossing or profile.flying):
            return None, "river is not frozen and the whole army cannot cross it", True

    if source_sea != destination_sea and SEA_OF_ICE in active_globals:
        return None, "Sea of Ice prevents movement between land and sea", True
    if destination_sea and not profile.can_enter_water:
        return None, "the whole army cannot enter an underwater province", True
    if not destination_sea and source_sea and not profile.can_enter_land:
        return None, "the whole army cannot enter a land province", True
    if profile.flying and (source_sea or destination_sea):
        # Fliers that are also amphibious can enter/leave the sea, but that
        # transition consumes the move rather than permitting an overflight.
        if not (profile.can_enter_water and profile.can_enter_land):
            return None, "flying cannot cross a sea province", True
        stop_after = True
    else:
        stop_after = False

    road = bool(border & ROAD)
    cost = _half_cost(
        source_flags, int(source.heat_scale or 0), int(source.owner_nation_id or 0),
        ctx.nation_id, profile, road=road,
    ) + _half_cost(
        destination_flags, int(destination.heat_scale or 0),
        int(destination.owner_nation_id or 0), ctx.nation_id, profile, road=road,
    )
    # Without Mountain Survival a highland is a legal destination, but not an
    # intermediate province in the same month's movement.
    if destination_flags & HIGHLANDS and not profile.mountain_survival \
            and not profile.flying:
        stop_after = True
    return cost, None, stop_after


def ordinary_movement_analysis(ctx: ToolContext, commander: Any) -> dict[str, Any]:
    """Calculate all legal ordinary/Flying/Teleport destinations this turn."""
    result: dict[str, Any] = {
        "available": False,
        "commander_id": commander.commander_id,
        "commander": commander.name,
        "destinations": [],
        "blocked_edges": [],
    }
    if commander.province_id is None:
        result["blockers"] = ["commander province is unknown"]
        return result
    try:
        profile = army_movement_profile(ctx, commander)
    except ValueError as exc:
        result["blockers"] = [str(exc)]
        return result
    result["profile"] = {
        "map_move": profile.map_move,
        "mode": "teleport" if profile.teleport else (
            "flying" if profile.flying else "ground"),
        "all_stealthy": profile.stealthy,
        "survival": {
            "forest": profile.forest_survival,
            "mountain": profile.mountain_survival,
            "waste": profile.waste_survival,
            "swamp": profile.swamp_survival,
            "winter": profile.winter_move,
        },
        "can_enter_water": profile.can_enter_water,
        "can_enter_land": profile.can_enter_land,
        "can_cross_unfrozen_rivers": profile.river_crossing,
        "follower_count": profile.follower_count,
    }
    if profile.flying_transport_capacity:
        result["profile"]["flying_transport"] = {
            "capacity": profile.flying_transport_capacity,
            "used": profile.flying_transport_used,
            "map_move": profile.flying_map_move,
        }
    if profile.map_move <= 0:
        result["blockers"] = ["the army has no strategic Map Move"]
        return result

    provinces = _province_records(ctx)
    graph = province_graph(ctx)
    borders, _sail_distance = _map_metadata(ctx)
    start = int(commander.province_id)
    if start not in provinces:
        result["blockers"] = [f"source province {start} is absent from the turn"]
        return result

    distances: dict[int, int] = {start: 0}
    routes: dict[int, list[int]] = {start: [start]}
    queue: list[tuple[int, int]] = [(0, start)]
    destinations: dict[int, dict[str, Any]] = {}
    seen_blockers: set[tuple[int, int, str]] = set()
    while queue:
        spent, current_id = heapq.heappop(queue)
        if spent != distances.get(current_id):
            continue
        current = provinces[current_id]
        for neighbour_id in sorted(graph.get(current_id, ())):
            if neighbour_id == start:
                continue
            neighbour = provinces.get(neighbour_id)
            if neighbour is None:
                continue
            key = (min(current_id, neighbour_id), max(current_id, neighbour_id))
            edge_cost, blocker, stop_after = _edge_cost(
                ctx, current, neighbour, profile, borders.get(key, 0))
            if edge_cost is None:
                marker = (current_id, neighbour_id, blocker or "blocked")
                if marker not in seen_blockers:
                    seen_blockers.add(marker)
                    result["blocked_edges"].append({
                        "from": current_id, "to": neighbour_id, "reason": blocker,
                    })
                continue
            total = spent + edge_cost
            adjacent = current_id == start
            # The client guarantees one legal province even when its cost is
            # greater than the slowest unit's allowance.
            if total > profile.map_move and not adjacent:
                continue
            route = [*routes[current_id], neighbour_id]
            mode = "teleport" if profile.teleport else (
                "flying" if profile.flying else "ground")
            candidate = {
                "province_id": neighbour_id,
                "province": neighbour.name or None,
                "mode": mode,
                "route": route,
                "movement_cost": total,
                "map_move": profile.map_move,
                "cost_exceeds_allowance_but_adjacent": total > profile.map_move,
            }
            previous = destinations.get(neighbour_id)
            if previous is None or total < int(previous["movement_cost"]):
                destinations[neighbour_id] = candidate
            if stop_after or total > profile.map_move:
                continue
            if total < distances.get(neighbour_id, 10**9):
                distances[neighbour_id] = total
                routes[neighbour_id] = route
                heapq.heappush(queue, (total, neighbour_id))

    result["destinations"] = [destinations[key] for key in sorted(destinations)]
    result["available"] = bool(destinations)
    return result


def sailing_analysis(ctx: ToolContext, commander: Any) -> dict[str, Any]:
    """Return every non-adjacent destination this army can prove it can sail."""
    result: dict[str, Any] = {
        "available": False,
        "commander_id": commander.commander_id,
        "commander": commander.name,
        "destinations": [],
        "blockers": [],
    }
    if commander.type_id is None or commander.province_id is None:
        result["blockers"].append("commander type or current province is unknown")
        return result
    unit = ctx.reference_db.execute(
        "SELECT id, name, mapmove, size, sailingshipsize, "
        "sailingmaxunitsize FROM units WHERE id=?", (commander.type_id,),
    ).fetchone()
    if unit is None or not int(unit["sailingshipsize"] or 0):
        result["blockers"].append("commander has no Sailing ability")
        return result

    followers = _final_followers(ctx, commander.commander_id)
    type_ids = {int(follower.type_id) for follower in followers}
    rows = {
        int(row["id"]): row
        for row in ctx.reference_db.execute(
            f"SELECT id, name, mapmove, size FROM units WHERE id IN "
            f"({','.join('?' * len(type_ids))})", sorted(type_ids),
        )
    } if type_ids else {}

    record_by_offset = {
        follower.offset: follower
        for follower in O.read_h2_units(ctx.h2_path.read_bytes(), ctx.nation_id)
    }
    mounted = [
        follower.instance_id for follower in followers
        if (mount := record_by_offset.get(follower.offset + U.RECORD_SIZE)) is not None
        and mount.is_mount
    ]
    if mounted:
        result["blockers"].append(
            "mounted follower Sailing transport size still needs a client "
            "cross-check: "
            f"instances {mounted}")

    missing = sorted(type_ids - rows.keys())
    if missing:
        result["blockers"].append(f"missing unit definitions for types {missing}")
    follower_sizes = [int(rows[f.type_id]["size"] or 0) for f in followers if f.type_id in rows]
    follower_moves = [int(rows[f.type_id]["mapmove"] or 0) for f in followers if f.type_id in rows]
    if any(size <= 0 for size in follower_sizes):
        result["blockers"].append("a follower has no decoded Size")
    if any(move <= 0 for move in follower_moves):
        result["blockers"].append("a follower has no decoded Map Move")

    equipment = _effective_equipment(ctx, commander.commander_id)
    farsail = 0
    mapmove_bonus = 0
    if equipment:
        ids = list(equipment.values())
        for item in ctx.reference_db.execute(
            f"SELECT id, farsail, mapmovebonus FROM items WHERE id IN "
            f"({','.join('?' * len(ids))})", ids,
        ):
            farsail += int(item["farsail"] or 0)
            mapmove_bonus += int(item["mapmovebonus"] or 0)

    capacity = int(unit["sailingshipsize"] or 0)
    maximum_unit_size = int(unit["sailingmaxunitsize"] or 0)
    used = sum(follower_sizes)
    largest = max(follower_sizes, default=0)
    commander_move = int(unit["mapmove"] or 0) + mapmove_bonus
    slowest = min([commander_move, *follower_moves])
    _border_flags, configured_sail_distance = _map_metadata(ctx)
    result.update({
        "ship_size_capacity": capacity,
        "ship_size_used": used,
        "maximum_transportable_unit_size": maximum_unit_size,
        "largest_follower_size": largest,
        "followers": len(followers),
        "slowest_map_move": slowest,
        "base_sea_province_range": configured_sail_distance,
        "far_sailing_bonus": farsail,
        "maximum_sea_provinces_crossed": configured_sail_distance + farsail,
    })
    if used > capacity:
        result["blockers"].append(
            f"followers use {used} size points but ship capacity is {capacity}")
    if largest > maximum_unit_size:
        result["blockers"].append(
            f"largest follower is size {largest}, above the ship limit "
            f"of {maximum_unit_size}")
    if result["blockers"]:
        return result

    provinces = {p.province_id: p for p in ctx.view.provinces()}
    graph = province_graph(ctx)
    source = provinces.get(commander.province_id)
    if source is None or int(source.current_terrain_flags or 0) & SEA:
        result["blockers"].append("Sailing must start in a visible land province")
        return result
    profile = army_movement_profile(ctx, commander)
    embark_cost = _ground_half_cost(int(source.current_terrain_flags or 0), profile)
    border_flags, configured_sail_distance = _map_metadata(ctx)
    maximum_seas = configured_sail_distance + farsail
    active_globals = {effect.spell_id for effect in ctx.view.parsed.global_effects}
    if PERPETUAL_STORM in active_globals:
        result["blockers"].append("Perpetual Storm prevents Sailing")
        return result
    if SEA_OF_ICE in active_globals:
        result["blockers"].append("Sea of Ice prevents Sailing")
        return result

    queue = deque([(source.province_id, [source.province_id], 0)])
    best_seas: dict[int, int] = {source.province_id: 0}
    candidates: dict[int, dict[str, Any]] = {}
    while queue:
        current_id, route, seas = queue.popleft()
        for neighbour_id in sorted(graph.get(current_id, ())):
            neighbour = provinces.get(neighbour_id)
            if neighbour is None or neighbour_id in route:
                continue
            border = border_flags.get(tuple(sorted((current_id, neighbour_id))), 0)
            if border & IMPASSABLE:
                continue
            flags = int(neighbour.current_terrain_flags or 0)
            if flags & SEA:
                next_seas = seas + 1
                if next_seas > maximum_seas:
                    continue
                if next_seas < best_seas.get(neighbour_id, 10**9):
                    best_seas[neighbour_id] = next_seas
                    queue.append((neighbour_id, [*route, neighbour_id], next_seas))
                continue
            if seas < 1 or neighbour_id in graph.get(source.province_id, ()):
                continue
            movement_cost = embark_cost + _ground_half_cost(flags, profile)
            if movement_cost > slowest:
                continue
            candidate = {
                "province_id": neighbour_id,
                "province": neighbour.name,
                "mode": "sailing",
                "route": [*route, neighbour_id],
                "sea_provinces_crossed": seas,
                "embark_disembark_movement_cost": movement_cost,
            }
            previous = candidates.get(neighbour_id)
            if previous is None or len(candidate["route"]) < len(previous["route"]):
                candidates[neighbour_id] = candidate

    result["destinations"] = [candidates[key] for key in sorted(candidates)]
    result["available"] = bool(candidates)
    if not candidates:
        result["blockers"].append(
            "no non-adjacent land destination has an open route through the "
            "allowed number of sea provinces and the army's Map Move")
    return result


def sailing_destination(ctx: ToolContext, commander: Any, destination: int) -> dict[str, Any] | None:
    """Return the proved sailing route to ``destination``, if one exists."""
    analysis = sailing_analysis(ctx, commander)
    return next(
        (row for row in analysis["destinations"]
         if row["province_id"] == destination),
        None,
    )


def movement_analysis(ctx: ToolContext, commander: Any) -> dict[str, Any]:
    """Unified ordinary/Flying/Teleport/Sailing movement result."""
    ordinary = ordinary_movement_analysis(ctx, commander)
    sailing = sailing_analysis(ctx, commander)
    by_destination: dict[int, dict[str, Any]] = {
        int(row["province_id"]): row for row in ordinary["destinations"]
    }
    alternatives: dict[int, list[dict[str, Any]]] = {}
    for row in sailing["destinations"]:
        province_id = int(row["province_id"])
        if province_id in by_destination:
            alternatives.setdefault(province_id, []).append(row)
        else:
            by_destination[province_id] = row
    for province_id, rows in alternatives.items():
        by_destination[province_id] = {
            **by_destination[province_id],
            "alternative_routes": rows,
        }
    return {
        "commander_id": commander.commander_id,
        "commander": commander.name,
        "source_province_id": commander.province_id,
        "ordinary": ordinary,
        "sailing": sailing,
        "destinations": [by_destination[key] for key in sorted(by_destination)],
    }


def movement_destination(
    ctx: ToolContext,
    commander: Any,
    destination: int,
    *,
    allow_sailing: bool = True,
) -> dict[str, Any] | None:
    analysis = movement_analysis(ctx, commander)
    for row in analysis["destinations"]:
        if int(row["province_id"]) != destination:
            continue
        if allow_sailing or row["mode"] != "sailing":
            return row
        alternatives = row.get("alternative_routes", [])
        return next((route for route in alternatives if route["mode"] != "sailing"), None)
    return None
