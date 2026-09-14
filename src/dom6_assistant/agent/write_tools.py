"""Write tools: recording intent, and turning it into a `.2h`.

Nothing here writes a game file directly. Every decision becomes a row in
`order_intent` first, and the `.2h` is rebuilt from those rows — so the file is
a pure function of recorded intent, and the reasoning behind a turn survives in
a form that can be read back long after the context that produced it is gone.

**Rationale is required, not optional.** `record_order` refuses an order with
no reasoning. That is a deliberate cost imposed on the model: it is the one
field that cannot be reconstructed from the save afterwards, and it is the
whole reason intent is stored rather than the file being edited in place. An
assistant reviewing its own last turn can see *that* it moved Bruise to 86; only
the rationale says whether that was a feint, a reinforcement, or a mistake.

**Orders are verified or refused.** `ORDER_CODES` holds only order names whose
byte encoding was established by experiment against the running game. Anything
else raises, at record time rather than at write time, so a bad decision is
rejected when it is made rather than surfacing later as a skipped row in a
materialisation nobody read closely.

**Materialisation defaults to a dry run.** A wrong order does not fail loudly —
the turn still resolves, it just does something else. So `materialize_orders`
reports what it would write and requires an explicit `confirm=true` to touch
the file.
"""
from __future__ import annotations

from collections import Counter
import json
import struct

from dom6_assistant.agent.economics import owned_province_resource_budget
from dom6_assistant.agent.read_tools import (
    effective_magic_paths,
    effective_forge_paths,
    forge_cost_for_commander,
    forgeable_item_ids,
)
from dom6_assistant.agent.registry import Param, ToolContext, ToolError, ToolRegistry
from dom6_assistant.agent import turn_completion as TC
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.file_reader.formats import trn as T
from dom6_assistant.orders import materialize as M
from dom6_assistant.orders import orders_2h as O
from dom6_assistant.reference import divine_spells as DV
from dom6_assistant.reference import leadership as LD
from dom6_assistant.reference import mercenary_cost as MC
from dom6_assistant.reference import ritual_range as RR
from dom6_assistant.reference import wish as WISH

MAGIC_PATH_CHOICES = tuple(O.MAGIC_PATH_CODES)
NON_HOLY_PATHS = frozenset("FAWESDNGB")
# A missing #provrange does not prove "no target": Gift of Reason, Dispel and
# similar rituals select units or enchantments through other fields.  The
# extracted ritual effect is now the shared source of truth for ordinary local
# summons, caster transformations, local fort/dome effects and conversions.
# Only effects whose UI selects another object remain outside this set.
VERIFIED_TARGETLESS_RITUAL_EFFECTS = RR.NO_SELECTOR_RITUAL_EFFECTS
# World global enchantments. Controlled casts of Foul Air, Burden of Time and
# Eternal Pyre all wrote +124/+128/+132 as 0/-1/-1: a world global selects
# nothing. Ritual effect 81 is the Enchant World family. Effect 82 is a
# different province-enchantment family containing domes, fort enchantments,
# scrying and map-targeted Tapestry of Dreams; it must not be generalized from
# the world-global controls.
WORLD_GLOBAL_RITUAL_EFFECTS = frozenset({81})
# Dispel, Disenchantment and Arcane Analysis select an active global by its
# stable chain slot. Only Dispel and Disenchantment accept extra strength gems.
GLOBAL_SELECTOR_RITUAL_EFFECTS = RR.GLOBAL_SELECTOR_RITUAL_EFFECTS
GLOBAL_STRENGTH_RITUAL_EFFECTS = (
    RR.GLOBAL_STRENGTH_INVESTMENT_RITUAL_EFFECTS
)
#: Gem transport to a named commander. The recipient's runtime handle goes at
#: +128 and the payload is the caster's carried gems, up to the effect argument.
GEM_TRANSPORT_RITUAL_EFFECTS = frozenset({160})
#: Carrier Eagle and Teleport Item are the controlled item transports. Both
#: use province/recipient selectors, store the selected treasury item id at
#: +132, and zero that item in the current `.2h` treasury.
ITEM_TRANSPORT_RITUAL_SPELL_IDS = frozenset({1285, 1320})
ITEM_TRANSPORT_RITUAL_EFFECT = 161
# Wish's item branch. The UI accepts text, but resolves a recognized magic-item
# name to result-family 10001 plus a stable item id before saving.
WISH_RITUAL_SPELL_IDS = O.WISH_RITUAL_SPELL_IDS
WISH_FIXED_RESULTS = {
    name: {
        "outcome": WISH.SPECS[name].outcome,
        "visibility": WISH.SPECS[name].visibility,
    }
    for name in WISH.FIXED_RESULTS
}
# Exact controlled unit-selector family. Gift of Reason and Divine Name
# (effect 39) both write the selected troop's runtime handle into +124/+128.
# Divine Name accepts Mindless targets; Gift of Reason does not.
UNIT_TARGET_RITUAL_SPELL_IDS = frozenset({1327, 1349})
MINDLESS_ALLOWED_UNIT_TARGET_RITUAL_SPELL_IDS = frozenset({1349})

# MA Ermor's Reanimation Priest menu, controlled with three priests on the
# same planning turn.  The number shown by the client is deliberately kept as
# a display estimate: a trailing '+' does not promise an exact resolved yield.
# H4 has not been observed, so these formulae are only asserted for H1-H3.
ERMOR_NATION_ID = 54
REANIMATION_SPECS = {
    "reanimate_ghouls": {
        "minimum_holy": 1, "unit": "Ghoul",
        "displayed_minimum": lambda holy: holy + 5,
        "open_ended": True,
    },
    "reanimate_soulless": {
        "minimum_holy": 1, "unit": "Soulless",
        "displayed_minimum": lambda holy: holy * 8,
        "open_ended": True,
    },
    "reanimate_warriors": {
        "minimum_holy": 1, "unit": "Longdead Warrior",
        "displayed_minimum": lambda holy: holy * 2 + 1,
        "open_ended": True,
    },
    "reanimate_horsemen": {
        "minimum_holy": 2, "unit": "Longdead Horseman",
        "displayed_minimum": lambda holy: holy - 1,
        "open_ended": True,
    },
    "reanimate_lictors": {
        "minimum_holy": 3, "unit": "Lictor",
        "displayed_minimum": lambda holy: 1,
        "open_ended": False,
    },
}


def _reanimation_estimate(order: str, holy: int) -> dict:
    """Player-visible Ermor menu estimate for one verified H1-H3 order."""
    spec = REANIMATION_SPECS[order]
    minimum = spec["displayed_minimum"](holy)
    open_ended = spec["open_ended"]
    return {
        "unit": spec["unit"],
        "displayed_yield": f"{minimum}+" if open_ended else str(minimum),
        "minimum_yield": minimum,
        "exact_yield": not open_ended,
        "gem_cost": 0,
        "persists_each_turn": True,
    }


def _eligibility(ctx: ToolContext, commander, order: str) -> dict:
    """Refuse proven-illegal orders and name checks we cannot yet perform.

    The first post-name u32 in the `.2h` joins exactly to the same handle at
    -32 in the commander's `.trn` stat record.  That supplies location and unit
    type even for troopless commanders, so type traits are enforceable rather
    than advisory.
    """
    checks: list[str] = []
    warnings: list[str] = []
    paths = commander.paths
    is_mage = any(paths.get(path, 0) for path in NON_HOLY_PATHS)
    unit = (ctx.reference_db.execute(
        "SELECT stealthy, spy, assassin, seduce, succubus, corrupt, "
        "reanimpriest FROM units "
        "WHERE id=?", (commander.type_id,)).fetchone()
            if commander.type_id is not None else None)

    if order in {"forge_magic_item", "research", "build_laboratory"}:
        if not is_mage:
            raise ToolError(
                f"{commander.name} has no visible non-Holy magic path and "
                f"cannot {order.replace('_', ' ')}.")
        checks.append("commander has a non-Holy magic path")

    if order == "search_magic_sites_auto":
        if not any(paths.values()):
            raise ToolError(
                f"{commander.name} has no visible magic or Holy path and "
                "cannot search for magic sites.")
        checks.append("commander has at least one magic or Holy path")

    if order == "preach":
        if not paths.get("H", 0):
            raise ToolError(
                f"{commander.name} has no visible Holy path and cannot preach.")
        checks.append("commander has a Holy path")

    if order == "call_god":
        if not paths.get("H", 0):
            raise ToolError(
                f"{commander.name} has no visible Holy path and cannot Call God.")
        pretender = ctx.view.own_pretender_state(ctx.h2_path)
        if pretender["dead"] is None:
            raise ToolError(
                "the own pretender's live/dead state is unknown; refusing to "
                "infer a death from an unavailable commander table.")
        if pretender["dead"] is False:
            raise ToolError(
                "our pretender is present in the writable commander table "
                "(alive or dormant), so Call God is unavailable.")
        checks.extend((
            "commander has a Holy path",
            f"own pretender commander {pretender['commander_id']} is dead",
        ))

    reanimation = None
    if order in REANIMATION_SPECS:
        spec = REANIMATION_SPECS[order]
        holy = paths.get("H", 0)
        minimum_holy = spec["minimum_holy"]
        if ctx.nation_id != ERMOR_NATION_ID:
            raise ToolError(
                f"{order.replace('_', ' ')} is currently verified only for "
                "MA Ermor's Reanimation Priest menu.")
        if holy < minimum_holy:
            raise ToolError(
                f"{order.replace('_', ' ')} needs Holy {minimum_holy}; "
                f"{commander.name} visibly has Holy {holy}.")
        if unit is not None and not unit["reanimpriest"]:
            raise ToolError(
                f"{commander.name}'s {commander.type_id} unit definition has "
                "no Reanimation Priest ability.")
        if unit is None:
            warnings.append(
                "commander unit type is unavailable; Reanimation Priest "
                "eligibility could not be checked")
        else:
            checks.append("commander has the Reanimation Priest ability")
        checks.append(f"commander has at least Holy {minimum_holy}")
        if holy <= 3:
            reanimation = _reanimation_estimate(order, holy)
        else:
            reanimation = {
                "unit": spec["unit"],
                "displayed_yield": None,
                "minimum_yield": None,
                "exact_yield": None,
                "gem_cost": 0,
                "persists_each_turn": True,
            }
            warnings.append(
                "Holy 4+ Reanimation yields and any additional menu choice "
                "are not yet controlled; this lower-tier order's encoding is "
                "known but its displayed yield is not asserted")

    if order == "become_prophet":
        prophet = None
        for nation_id, _gold, _gems, header in T.read_nation_roster(ctx.view.data):
            if nation_id == ctx.nation_id:
                prophet = T.read_prophet_id(ctx.view.data, header)
                break
        if prophet is not None and prophet != commander.commander_id:
            raise ToolError(
                f"our nation already has prophet commander {prophet}; another "
                "commander cannot become prophet while that prophet exists.")
        if prophet == commander.commander_id:
            raise ToolError(f"{commander.name} is already our prophet.")
        checks.append("our nation currently has no prophet")

    province = (ctx.view.province(commander.province_id)
                if commander.province_id is not None else None)
    local_actions = {
        "forge_magic_item", "research", "empowerment", "build_laboratory",
        "build_temple", "build_palisades", "upgrade_fortress",
        "demolish_fort", "demolish_laboratory",
        "claim_throne",
    }
    if order in local_actions and province is None:
        warnings.append(
            "commander did not join to a unique .trn stat record; local "
            "province prerequisites could not be checked")
    elif order in local_actions:
        if not province.is_ours:
            raise ToolError(
                f"{order.replace('_', ' ')} is a local owned-province action, "
                f"but {commander.name} is in {province.name} ({province.status}).")
        checks.append(f"commander is in owned province {province.name}")

    if province is not None:
        # Empowerment needs a laboratory, confirmed by the player against
        # the game: of our three provinces only Marignon has one, and it is
        # the only province whose commanders are offered the order at all.
        # Copper Canyons and The Obsidian Waste both refuse it, and the
        # Waste has a temple but no laboratory, so it is the lab that
        # matters rather than any structure.
        if order in {"forge_magic_item", "research", "empowerment"}:
            if province.has_laboratory is False:
                raise ToolError(
                    f"{province.name} has no laboratory, which "
                    f"{order.replace('_', ' ')} requires.")
            if province.has_laboratory:
                checks.append("province has a laboratory")
        elif order == "build_laboratory":
            if province.has_laboratory:
                raise ToolError(f"{province.name} already has a laboratory.")
            checks.append("province has no laboratory")
        elif order == "build_temple":
            if province.has_temple:
                raise ToolError(f"{province.name} already has a temple.")
            checks.append("province has no temple")
        elif order == "build_palisades":
            if province.fort_type:
                raise ToolError(f"{province.name} already has a fort.")
            if province.under_construction:
                raise ToolError(f"a fort is already being built in {province.name}.")
            checks.append("province has no fort or fort construction")
        elif order == "upgrade_fortress":
            if province.fort_type != O.BUILDING_CODES["palisades"]:
                current = ("no fort" if not province.fort_type
                           else f"fort type {province.fort_type}")
                raise ToolError(
                    f"upgrade fortress is specifically the Palisades → "
                    f"Fortress step, but {province.name} has {current}.")
            if province.under_construction:
                raise ToolError(
                    f"a fort upgrade is already being built in {province.name}.")
            checks.append("province has Palisades and no fort construction")
        elif order == "demolish_fort":
            if not province.fort_type:
                raise ToolError(f"{province.name} has no fort to demolish.")
            checks.append("province has a fort")
        elif order == "demolish_laboratory":
            if not province.has_laboratory:
                raise ToolError(f"{province.name} has no laboratory to demolish.")
            checks.append("province has a laboratory")
        elif order == "claim_throne":
            if paths.get("H", 0) < 3:
                raise ToolError(
                    f"claim throne needs Holy 3; {commander.name} visibly "
                    f"has Holy {paths.get('H', 0)}.")
            throne_ids = []
            for site_id in province.site_ids:
                site = ctx.reference_db.execute(
                    "SELECT rarity FROM magic_sites WHERE id=?", (site_id,)
                ).fetchone()
                if site is not None and 11 <= site["rarity"] <= 13:
                    throne_ids.append(site_id)
            if not throne_ids:
                raise ToolError(f"{province.name} has no visible throne site.")
            if province.throne_claimant_nation_id is not None:
                claimant = (
                    "us" if province.throne_claimant_nation_id == ctx.nation_id
                    else f"nation {province.throne_claimant_nation_id}"
                )
                raise ToolError(
                    f"the throne in {province.name} is already claimed by "
                    f"{claimant}; Claim Throne is only legal while unclaimed.")
            checks.extend(("commander has at least Holy 3",
                           f"province has visible throne site {throne_ids[0]}"))

    if order == "build_temple":
        if paths.get("H", 0):
            checks.append("commander has a Holy path")
        else:
            warnings.append(
                "temple-builder eligibility is not fully decoded; no Holy "
                "path is visible on this commander")

    if order == "attack_current_province":
        if province is None or commander.province_id is None:
            raise ToolError(
                f"{commander.name}'s current province is not decoded, so the "
                "fixed Attack Current Province parameter cannot be written.")
        if province.is_ours:
            raise ToolError(
                f"{commander.name} is in our province {province.name}; there "
                "is no foreign local force to attack.")
        checks.append(
            f"fixed target is current non-owned province {province.name} "
            f"({commander.province_id})")

    construction_cost = M.CONSTRUCTION_GOLD_COSTS.get(order)
    if construction_cost is not None:
        from dom6_assistant.file_reader.formats import h2
        source = ctx.h2_path
        pristine = (source.with_suffix(M.BASE_SUFFIX)
                    if source is not None else None)
        if pristine is not None and pristine.exists():
            source = pristine
        available = (h2.gold_remaining(source.read_bytes())
                     if source is not None and source.exists() else None)
        if available is not None and available < construction_cost:
            raise ToolError(
                f"{order.replace('_', ' ')} reserves {construction_cost} gold, "
                f"but the current .2h has only {available} uncommitted gold.")
        checks.append(f"will reserve {construction_cost} gold")

    required_trait = {
        "hide": ("stealthy", "Stealth"),
        "sneak": ("stealthy", "Stealth"),
        "attack_current_province": ("assassin", "Assassin"),
        "assassinate": ("assassin", "Assassin"),
        "instill_uprising": ("spy", "Spy"),
    }.get(order)
    if order == "seduce":
        has_trait = bool(unit and (unit["seduce"] or unit["succubus"]
                                   or unit["corrupt"]))
        if unit is not None and not has_trait:
            raise ToolError(
                f"{commander.name}'s {commander.type_id} unit definition has "
                "no Seducer, Succubus, or Corrupt ability.")
        if has_trait:
            checks.append("commander has a seduction ability")
        else:
            warnings.append("commander unit type is unavailable; seduction "
                            "eligibility could not be checked")
    elif required_trait is not None:
        field, label = required_trait
        has_trait = bool(unit and unit[field])
        if unit is not None and not has_trait:
            raise ToolError(
                f"{commander.name}'s {commander.type_id} unit definition has "
                f"no {label} ability.")
        if has_trait:
            checks.append(f"commander has the {label} ability")
        else:
            warnings.append(f"commander unit type is unavailable; {label} "
                            "eligibility could not be checked")

    result = {
        "status": "partially_verified" if warnings else "verified",
        "checks": checks,
        "warnings": warnings,
    }
    if reanimation is not None:
        result["reanimation"] = reanimation
    return result


def _strategic_parameter(ctx: ToolContext, order: str, *,
                         current_province_id: int | None,
                         destination: int | None, item_id: int | None,
                         magic_path: str | None) -> tuple[int, dict]:
    """Turn typed tool arguments into the order block's shared u16 field."""
    spec = O.ORDER_SPECS[order]
    kind = spec.parameter_kind
    # Older/smaller models commonly supply destination=0 on local orders. In
    # the file that means exactly "no parameter", so preserve that harmless
    # spelling for no-parameter actions. It is still invalid for movement.
    if kind == O.PARAM_NONE and destination == 0:
        destination = None
    supplied = {
        "destination": destination,
        "item_id": item_id,
        "magic_path": magic_path,
    }

    if kind == O.PARAM_CURRENT_PROVINCE:
        wrong = [name for name, value in supplied.items() if value is not None]
        if wrong:
            raise ToolError(
                f"{order} always targets the commander's current province; "
                f"remove {', '.join(wrong)}.")
        if current_province_id is None:
            raise ToolError(
                f"{order} needs the commander's decoded current province.")
        return O.normalize_order_parameter(order, current_province_id), {
            "current_province_id": current_province_id}

    if kind == O.PARAM_NATION:
        wrong = [name for name, value in supplied.items() if value is not None]
        if wrong:
            raise ToolError(
                f"{order} always targets our own dead pretender; remove "
                f"{', '.join(wrong)}.")
        return O.normalize_order_parameter(order, ctx.nation_id), {
            "pretender_nation_id": ctx.nation_id}

    if kind in (O.PARAM_PROVINCE, O.PARAM_PROVINCE_OR_ZERO):
        wrong = [k for k in ("item_id", "magic_path") if supplied[k] is not None]
        if wrong:
            raise ToolError(
                f"{order} takes destination, not {', '.join(wrong)}.")
        # The public `sneak` action means travel. Code 2 with an explicit zero
        # is Hide, but Hide is not exposed as a made-up second order code.
        if destination in (None, 0):
            raise ToolError(
                f"{order} needs a destination province id. Without one the "
                "order does not travel — call list_provinces or "
                "find_province for an id.")
        if ctx.view.province(destination) is None:
            raise ToolError(
                f"no province {destination} in this game. Call find_province "
                "to look one up by name.")
        return O.normalize_order_parameter(order, destination), {
            "destination": destination}

    if kind == O.PARAM_ITEM:
        wrong = [k for k in ("destination", "magic_path")
                 if supplied[k] is not None]
        if wrong:
            raise ToolError(
                f"{order} takes item_id, not {', '.join(wrong)}.")
        if item_id is None:
            raise ToolError(
                "forge_magic_item needs item_id. Call lookup_item to find the "
                "verified reference id and its path/research requirements.")
        item = ctx.reference_db.execute(
            "SELECT name FROM items WHERE id=?", (item_id,)).fetchone()
        if item is None:
            raise ToolError(
                f"no magic item {item_id} in the reference data. Call "
                "lookup_item to find an id.")
        return O.normalize_order_parameter(order, item_id), {
            "item_id": item_id, "item_name": item["name"]}

    if kind == O.PARAM_MAGIC_PATH:
        wrong = [k for k in ("destination", "item_id")
                 if supplied[k] is not None]
        if wrong:
            raise ToolError(
                f"{order} takes magic_path, not {', '.join(wrong)}.")
        if magic_path is None:
            raise ToolError(
                "empowerment needs magic_path: fire, air, water, earth, "
                "astral, death, nature, glamour, or blood.")
        path = magic_path.strip().lower()
        if path not in O.MAGIC_PATH_CODES:
            raise ToolError(
                f"magic_path must be one of {list(O.MAGIC_PATH_CODES)}, got "
                f"{magic_path!r}.")
        code = O.MAGIC_PATH_CODES[path]
        return O.normalize_order_parameter(order, code), {"magic_path": path}

    if any(value is not None for value in supplied.values()):
        wrong = [name for name, value in supplied.items() if value is not None]
        raise ToolError(
            f"{order} takes no caller-supplied parameter; remove "
            f"{', '.join(wrong)}.")
    parameter = O.normalize_order_parameter(order, None)
    if kind == O.PARAM_BUILDING:
        return parameter, {
            "building": O.BUILDING_NAMES.get(parameter, parameter)}
    return parameter, {}


def _leadership_check(ctx: ToolContext, commander, assignments) -> dict:
    """Refuse the final army if the commander could not actually lead it.

    Capacity is the chassis value plus experience and every worn/pending item.
    The item term is
    load-bearing: Nergash's chassis leads ten and no undead at all, and the
    Crown of Bones his company equips grants 150 undead, which is how he came
    with 125 Longdead.
    """
    capacity = _effective_commander_leadership(ctx, commander)
    if capacity is None:
        return {}
    data = ctx.h2_path.read_bytes()

    # Resolve the final commander of every troop after all recorded moves plus
    # this prospective call. Moving between two squads under the same leader
    # must count once, not once as an existing troop and again as an addition.
    token_owner: dict[int, int] = {}
    order_blocks = O.find_order_blocks(data)
    own_commander_ids = {
        candidate.commander_id
        for candidate in ctx.view.own_commanders(ctx.h2_path)
    }
    for commander_id in own_commander_ids:
        order_block = order_blocks.get(commander_id)
        if order_block is None:
            continue
        for squad_slot in O.read_squad_slots(data, order_block.name_end):
            token_owner[int(squad_slot["token"])] = int(commander_id)
    final_destinations = {
        int(row["unit_instance_id"]): (
            None if row["destination"] == "garrison"
            else int(row["target_commander_id"]))
        for row in ctx.game_db.execute(
            "SELECT unit_instance_id, target_commander_id, destination FROM "
            "current_troop_assignment_intent WHERE game_id=? AND turn=?",
            (ctx.game_id, ctx.turn))
    }
    selected_types = {
        int(instance_id): int(type_id)
        for instance_id, type_id, _name in assignments
    }
    final_destinations.update({
        instance_id: commander.commander_id for instance_id in selected_types
    })
    counts: dict[str, int] = {}
    for existing in O.read_h2_units(data, ctx.nation_id):
        if existing.is_mount:
            continue
        final_commander = final_destinations.get(
            existing.instance_id, token_owner.get(existing.warband))
        if final_commander != commander.commander_id:
            continue
        row = ctx.reference_db.execute(
            "SELECT * FROM units WHERE id=?", (existing.type_id,)).fetchone()
        if row is not None:
            pool = LD.troop_pool(row)
            counts[pool] = counts.get(pool, 0) + 1

    for pool, needed in counts.items():
        allowed = capacity.capacity(pool)
        if needed > allowed:
            raise ToolError(
                f"{commander.name} would lead {needed} {pool} troops but can "
                f"lead {allowed}"
                + (f" ({capacity.from_items})" if capacity.from_items else "")
                + ". Split them between commanders, or equip one who can.")
    return {"capacity": {"normal": capacity.normal, "undead": capacity.undead,
                         "magic": capacity.magic},
            "leading_after": counts,
            "from_items": capacity.from_items}


def _effective_commander_leadership(ctx: ToolContext, commander):
    """Final current-turn leadership, including pending kit and XP."""
    if commander.type_id is None:
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
    experience = int(state.experience or 0) if state is not None else 0
    return LD.leadership_for(
        unit,
        _effective_equipment_rows(ctx, commander.commander_id),
        experience=experience,
    )


def _is_leader_type(ctx: ToolContext, type_id: int) -> bool:
    """True when this unit type is recruitable as a commander by ANY nation.

    Deliberately not restricted to our own nation. A type that leads for
    somebody is a commander wherever it turns up, and the cost of being wrong
    is asymmetric: refusing a troop is an inconvenience, reassigning a
    commander rewrites a warband token that belongs to a squad.
    """
    return ctx.reference_db.execute(
        "SELECT 1 FROM ("
        "SELECT monster_number FROM fort_leader_types_by_nation "
        "UNION SELECT monster_number FROM nonfort_leader_types_by_nation "
        "UNION SELECT monster_number FROM coast_leader_types_by_nation"
        ") WHERE monster_number=? LIMIT 1", (type_id,)).fetchone() is not None


def _commander_runtime_index(ctx: ToolContext, commander_id: int) -> int:
    """Runtime handle joining a commander block to its embedded unit."""
    if ctx.h2_path is None or not ctx.h2_path.exists():
        raise ToolError("no .2h file contains the current commander state.")
    data = ctx.h2_path.read_bytes()
    block = O.find_order_blocks(data, valid_ids={commander_id}).get(commander_id)
    if block is None:
        raise ToolError(
            f"no order block for commander {commander_id} in the current .2h.")
    return struct.unpack_from("<I", data, block.name_end)[0]


def _troop_selection(ctx: ToolContext, unit_instance_ids: str) -> tuple[list[int], list, list]:
    """Parse, locate and conservatively classify troop instances."""
    try:
        requested = json.loads(unit_instance_ids)
    except json.JSONDecodeError as exc:
        raise ToolError(f"unit_instance_ids must be a JSON array: {exc}")
    if (not isinstance(requested, list) or not requested
            or any(not isinstance(value, int) or isinstance(value, bool)
                   for value in requested)):
        raise ToolError(
            "unit_instance_ids must be a non-empty JSON array of integers.")
    if len(set(requested)) != len(requested):
        raise ToolError("unit_instance_ids must not contain duplicates.")
    # 65535 is the "no instance" sentinel. Records carrying it are scanner
    # over-matches — one in the live save reads instance 65535, warband 0 and
    # a runtime index of 556 million — and looking a unit up by it resolves to
    # whichever phantom happens to survive the mount filter.
    if any(value == 0xFFFF for value in requested):
        raise ToolError(
            "65535 is the no-instance sentinel, not a unit. Call list_units "
            "with include_instances=true for real instance ids.")
    if ctx.h2_path is None or not ctx.h2_path.exists():
        raise ToolError("no .2h file contains the current unit instances.")

    data = ctx.h2_path.read_bytes()
    # A commander is any unit the game can give orders to. Two signals, and
    # the UNION of them is used because each misses cases the other catches:
    # a mercenary or summoned commander has an order block but appears in no
    # nation's roster, while a freshly recruited one can be in the roster
    # before its block is found. Refusing a troop is an inconvenience;
    # reassigning a commander corrupts the file.
    commander_handles = {
        struct.unpack_from("<I", data, block.name_end)[0]
        for block in O.find_order_blocks(data).values()
    }
    records: dict[int, list] = {}
    for unit in O.read_h2_units(data, ctx.nation_id):
        if not unit.is_mount:
            records.setdefault(unit.instance_id, []).append(unit)
    chosen = []
    selected_records = []
    for instance_id in requested:
        matches = records.get(instance_id, [])
        if len(matches) != 1:
            detail = "not found" if not matches else "not unique"
            raise ToolError(
                f"unit instance {instance_id} is {detail} in our .2h.")
        unit = matches[0]
        if unit.runtime_index in commander_handles or _is_leader_type(
                ctx, unit.type_id):
            raise ToolError(
                f"instance {instance_id} type {unit.type_id} is a commander, "
                "not a troop. Commanders lead squads rather than belonging to "
                "one, and rewriting their warband token would corrupt both.")
        name = ctx.reference_db.execute(
            "SELECT name FROM units WHERE id=?", (unit.type_id,)).fetchone()
        chosen.append((instance_id, unit.type_id,
                       name["name"] if name else None))
        selected_records.append(unit)
    return requested, chosen, selected_records


def register(reg: ToolRegistry) -> ToolRegistry:
    """Add every write tool to `reg`. Returns it for chaining."""

    @reg.tool(
        "get_commander_action_options",
        "Strategic orders whose decoded prerequisites a commander currently "
        "passes, plus known reasons other orders are unavailable. Movement "
        "still needs a destination from get_movement_options.",
        Param("commander_id", "integer", "id from list_commanders"))
    def get_commander_action_options(ctx: ToolContext,
                                     commander_id: int) -> dict:
        commanders = {c.commander_id: c
                      for c in ctx.view.own_commanders(ctx.h2_path)}
        commander = commanders.get(commander_id)
        if commander is None:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Call "
                "list_commanders for valid ids.")
        available = []
        unavailable = []
        for order in sorted(set(O.ORDER_CODES) - O.ECONOMIC_ORDERS_PENDING):
            try:
                eligibility = _eligibility(ctx, commander, order)
            except ToolError as exc:
                unavailable.append({"order": order, "reason": str(exc)})
                continue
            spec = O.ORDER_SPECS[order]
            entry = {"order": order, "eligibility": eligibility}
            if spec.parameter_kind in (O.PARAM_PROVINCE, O.PARAM_PROVINCE_OR_ZERO):
                entry["needs_destination"] = True
                entry["next_tool"] = "get_movement_options"
            available.append(entry)
        return {
            "commander_id": commander.commander_id,
            "commander": commander.name,
            "province_id": commander.province_id,
            "current_order": commander.order,
            "available": available,
            "known_unavailable": unavailable,
            "encoding_only": {
                name: "gem cost/accounting or complete eligibility is unresolved"
                for name in sorted(O.ECONOMIC_ORDERS_PENDING)},
            "note": ("Available means every currently decoded prerequisite "
                     "passed. Orders with unresolved game-specific constraints "
                     "retain warnings and are not asserted universally legal."),
        }

    @reg.tool(
        "change_shape",
        "Instantly change one of our commanders to the alternate form shown "
        "by list_commanders. This is not a turn order and preserves the "
        "commander's strategic order. Records the desired form first; call "
        "materialize_orders to preview or write it.",
        Param("commander_id", "integer", "id from list_commanders"),
        Param("rationale", "string", "why this form is wanted now"),
        writes=True)
    def change_shape(ctx: ToolContext, commander_id: int,
                     rationale: str) -> dict:
        if not rationale.strip():
            raise ToolError(
                "rationale must not be empty. It is the reason this immediate "
                "state change is stored as auditable intent.")
        commanders = {
            commander.commander_id: commander
            for commander in ctx.view.own_commanders(ctx.h2_path)
        }
        commander = commanders.get(commander_id)
        if commander is None:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Call "
                "list_commanders for valid ids.")
        if commander.type_id is None or commander.hp is None:
            raise ToolError(
                f"{commander.name}'s live unit record did not join uniquely; "
                "Change Shape cannot be written safely.")
        source = ctx.reference_db.execute(
            "SELECT name, shapechange FROM units WHERE id=?",
            (commander.type_id,),
        ).fetchone()
        target_type_id = int(source["shapechange"] or 0) if source else 0
        if not target_type_id:
            raise ToolError(
                f"{commander.name}'s current unit type {commander.type_id} "
                "does not have a decoded Change Shape target.")
        target = ctx.reference_db.execute(
            "SELECT name FROM units WHERE id=?", (target_type_id,)).fetchone()
        if target is None:
            raise ToolError(
                f"alternate unit type {target_type_id} is absent from the "
                "reference database.")

        # Effective HP cannot be reconstructed from raw chassis HP for a
        # pretender: this control is 32/18 while reference data says 25/12.
        # Build a per-commander catalogue only from states actually present in
        # this turn's files or prior audited shape intent, and refuse a first
        # transition whose alternate effective HP has never been observed.
        observed: dict[int, int] = {
            int(commander.type_id): int(commander.hp)}
        runtime_index = _commander_runtime_index(ctx, commander_id)
        runtime_match = next(
            (unit for unit in ctx.view.own_units()
             if unit.runtime_index == runtime_index), None)
        if runtime_match is not None:
            observed[int(runtime_match.type_id)] = int(runtime_match.hp)
        for row in ctx.game_db.execute(
                "SELECT source_type_id, source_hp, target_type_id, target_hp "
                "FROM shape_change_intent WHERE game_id=? AND turn=? AND "
                "commander_id=? ORDER BY id",
                (ctx.game_id, ctx.turn, commander_id)):
            observed[int(row["source_type_id"])] = int(row["source_hp"])
            observed[int(row["target_type_id"])] = int(row["target_hp"])
        target_hp = observed.get(target_type_id)
        if target_hp is None:
            raise ToolError(
                f"{commander.name}'s alternate form type {target_type_id} has "
                "not been observed for this commander this turn. Raw chassis "
                "HP is unsafe because pretender/game modifiers change it. "
                "Manually change shape and save once to seed both effective "
                "form values; wounded-form conversion remains undecoded.")

        row_id = M.record_shape_change(
            ctx.game_db, ctx.game_id, ctx.turn, commander_id,
            int(commander.type_id), int(commander.hp),
            target_type_id, target_hp,
            commander_name=commander.name, rationale=rationale.strip())
        return {
            "recorded": row_id,
            "commander_id": commander_id,
            "commander": commander.name,
            "source_form": {
                "unit_type_id": commander.type_id,
                "unit_type": source["name"],
                "hp": commander.hp,
            },
            "target_form": {
                "unit_type_id": target_type_id,
                "unit_type": target["name"],
                "hp": target_hp,
            },
            "instantaneous": True,
            "strategic_order_preserved": True,
            "note": "call materialize_orders to preview or write the change",
        }

    @reg.tool(
        "record_order",
        "Record an order for one commander, with the reasoning behind it. "
        "This does not write the game file — call materialize_orders for that. "
        "Recording again for the same commander replaces the previous order.",
        Param("commander_id", "integer", "id from list_commanders",
              hint="Call list_commanders to get valid ids."),
        Param("order", "string", "an order name from list_order_types",
              hint="Call list_order_types to see what can be issued."),
        Param("rationale", "string",
              "why this order, in one or two sentences",
              hint="This is required: it is the only part of a decision that "
                   "cannot be recovered from the save file afterwards."),
        Param("destination", "integer", "target province id, for orders that "
              "move to another province", required=False),
        Param("item_id", "integer", "reference item id for forge_magic_item; "
              "call lookup_item", required=False),
        Param("magic_path", "string", "path for empowerment",
              required=False, choices=MAGIC_PATH_CHOICES),
        writes=True)
    def record_order(ctx: ToolContext, commander_id: int, order: str,
                     rationale: str, destination: int | None = None,
                     item_id: int | None = None,
                     magic_path: str | None = None) -> dict:
        cmds = {c.commander_id: c for c in ctx.view.own_commanders(ctx.h2_path)}
        if commander_id not in cmds:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Ours are: "
                f"{sorted(cmds)}. Call list_commanders for names.")
        if order not in O.ORDER_CODES:
            raise ToolError(
                f"order {order!r} has not been verified against the game and "
                f"will not be written. Verified orders: {sorted(O.ORDER_CODES)}.")
        if order in O.ECONOMIC_ORDERS_PENDING:
            raise ToolError(
                f"{order} has a decoded command field but is temporarily "
                "read-only: its gem reservation/refund and full eligibility "
                "rules are not yet generalized. Refusing a partial order.")
        if order == "forge_magic_item":
            # Writable, but not from here: a forge reserves gems, and this
            # path has nowhere to record the cost. Recording the order alone
            # would leave the national pool disagreeing with the file.
            raise ToolError(
                "use forge_item to forge, not record_order. Forging reserves "
                "gems from the national pool and the reservation has to be "
                "recorded with the order; call list_forgeable_items for "
                "candidates and their costs.")
        if not rationale.strip():
            raise ToolError(
                "rationale must not be empty. It is the reason this order is "
                "stored rather than written straight to the file.")
        commander = cmds[commander_id]
        parameter, typed = _strategic_parameter(
            ctx, order, current_province_id=commander.province_id,
            destination=destination, item_id=item_id,
            magic_path=magic_path)
        if order in {"move", "sneak"} and commander.province_id is not None:
            from dom6_assistant.agent.movement import (
                army_movement_profile,
                movement_destination,
            )

            if order == "sneak" and not army_movement_profile(ctx, commander).stealthy:
                raise ToolError(
                    f"{commander.name}'s complete moving formation is not "
                    "stealthy, so it cannot Sneak. Reassign the non-stealthy "
                    "troops or use Move.")

            route = movement_destination(
                ctx, commander, parameter, allow_sailing=order == "move")
            if route is None:
                raise ToolError(
                    f"{commander.name} cannot reach province {destination} "
                    f"with this army in one turn. Call get_movement_options "
                    "for the current legal destinations and routes.")
        eligibility = _eligibility(ctx, commander, order)

        row_id = M.record_order(
            ctx.game_db, ctx.game_id, ctx.turn, commander_id, order,
            commander_name=commander.name,
            parameter=parameter, rationale=rationale.strip())
        result = {"recorded": row_id, "commander": commander.name,
                  "order": order, **typed, "eligibility": eligibility}
        if order == "move" and commander.province_id is not None:
            result["movement"] = route
        return result

    @reg.tool(
        "record_orders",
        "Record orders for several commanders at once. Pass a JSON array of "
        '{"commander_id": N, "order": "name", "rationale": "why", plus '
        'the order-specific "destination", "item_id", or "magic_path"}. '
        'Each is validated separately: the valid ones are '
        "recorded and the rest are reported back, so one bad entry does not "
        "lose the others. Prefer this over calling record_order repeatedly.",
        Param("orders", "string",
              "JSON array of order objects",
              hint='Example: [{"commander_id": 308, "order": "move", '
                   '"destination": 86, "rationale": "reinforce the border"}]'),
        writes=True)
    def record_orders(ctx: ToolContext, orders: str) -> dict:
        """Batch recording, because a slow model cannot afford ten round trips.

        On a local model each step costs tens of seconds of prompt processing,
        so ordering ten commanders one at a time is most of an hour and will
        exhaust any sensible step budget before the turn is finished. This is
        the same validation applied ten times in one call.

        Partial success is deliberate. Rejecting the whole batch for one bad
        entry would make the model re-send nine good orders it has already
        reasoned about, and on a small model the re-derivation is where new
        mistakes come from.
        """
        try:
            parsed = json.loads(orders) if isinstance(orders, str) else orders
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"orders must be a JSON array; could not parse it: {exc}")
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list) or not parsed:
            raise ToolError(
                "orders must be a non-empty JSON array of order objects.")

        known_keys = {"commander_id", "order", "rationale", "destination",
                      "item_id", "magic_path"}
        recorded, rejected = [], []
        for i, entry in enumerate(parsed):
            if not isinstance(entry, dict):
                rejected.append({"index": i, "error": "not an object"})
                continue
            # Diagnose a wrong key by name. The model has been observed sending
            # "action" instead of "order", and the resulting error — «order ''
            # has not been verified» — describes the symptom rather than the
            # mistake, which is exactly the kind of message a small model
            # cannot act on.
            unexpected = set(entry) - known_keys
            if unexpected:
                rejected.append({
                    "index": i, "commander_id": entry.get("commander_id"),
                    "error": f"unexpected key(s) {sorted(unexpected)}; each "
                             f"order takes exactly {sorted(known_keys)}. "
                             "The order name goes in 'order'."})
                continue
            try:
                result = record_order(
                    ctx,
                    commander_id=Param("commander_id", "integer", "").validate(
                        entry.get("commander_id")),
                    order=str(entry.get("order", "")),
                    rationale=str(entry.get("rationale", "")),
                    destination=(Param("destination", "integer", "").validate(
                        entry["destination"])
                        if entry.get("destination") is not None else None),
                    item_id=(Param("item_id", "integer", "").validate(
                        entry["item_id"])
                        if entry.get("item_id") is not None else None),
                    magic_path=(Param("magic_path", "string", "").validate(
                        entry["magic_path"])
                        if entry.get("magic_path") is not None else None))
                recorded.append(result)
            except ToolError as exc:
                rejected.append({"index": i,
                                 "commander_id": entry.get("commander_id"),
                                 "error": str(exc)})
        return {"recorded": len(recorded), "orders": recorded,
                "rejected": rejected or None}

    @reg.tool(
        "clear_order",
        "Remove the order recorded for a commander this turn.",
        Param("commander_id", "integer", "id from list_commanders"),
        writes=True)
    def clear_order(ctx: ToolContext, commander_id: int) -> dict:
        cur = ctx.game_db.execute(
            "DELETE FROM order_intent WHERE game_id=? AND turn=? AND "
            "commander_id=?", (ctx.game_id, ctx.turn, commander_id))
        ctx.game_db.commit()
        if not cur.rowcount:
            raise ToolError(
                f"no order recorded for commander {commander_id} this turn.")
        return {"cleared": cur.rowcount, "commander_id": commander_id}

    @reg.tool(
        "set_battle_order",
        "How a commander fights: their own battle order, or one of the squads "
        "they lead. Omit squad for the commander themselves; give 0-4 for a "
        "squad slot. Call list_battle_options to see valid stances and targets.",
        Param("commander_id", "integer", "id from list_commanders"),
        Param("stance", "string", "e.g. attack, hold_and_fire, retreat",
              hint="Call list_battle_options for the verified list."),
        Param("rationale", "string", "why this stance"),
        Param("target", "string", "who to prefer attacking, e.g. archers",
              required=False),
        Param("squad", "integer", "squad slot 0-4; omit for the commander",
              required=False),
        writes=True)
    def set_battle_order(ctx: ToolContext, commander_id: int, stance: str,
                         rationale: str, target: str | None = None,
                         squad: int | None = None) -> dict:
        cmds = {c.commander_id: c for c in ctx.view.own_commanders(ctx.h2_path)}
        if commander_id not in cmds:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Ours are: "
                f"{sorted(cmds)}.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        _check_squad(ctx, commander_id, squad)
        allowed = O.COMMANDER_STANCES if squad is None else O.SQUAD_STANCES
        if stance not in allowed:
            subject = "commander" if squad is None else "squad"
            raise ToolError(
                f"{stance!r} is not verified as a valid {subject} stance. Valid choices: "
                f"{sorted(allowed)}.")
        if target is not None and target not in O.TARGET_CODES_V2:
            raise ToolError(
                f"unknown target {target!r}. Valid choices: "
                f"{sorted(O.TARGET_CODES_V2)}.")
        if target not in (None, "none") and stance not in O.TARGETABLE_STANCES:
            raise ToolError(
                f"{stance!r} does not accept a preferred target. Pass "
                "target='none' to clear an old target or omit target to preserve it.")
        try:
            row_id = M.record_battle_order(
                ctx.game_db, ctx.game_id, ctx.turn, commander_id,
                squad=squad, stance=stance, target=target,
                commander_name=cmds[commander_id].name,
                rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        return {"recorded": row_id, "commander": cmds[commander_id].name,
                "squad": squad, "stance": stance, "target": target}

    @reg.tool(
        "set_formation",
        "Set one decoded squad formation by name: box, line, sparse_line, "
        "skirmish, or double_line. Box and Skirmish require no threshold; "
        "Line, Sparse Line and Double Line require final normal Leadership "
        "80 after experience and worn/pending equipment.",
        Param("commander_id", "integer", "id from list_commanders"),
        Param("squad", "integer", "squad slot 0-4"),
        Param("formation", "string", "decoded formation name",
              choices=tuple(O.FORMATION_CODES)),
        Param("rationale", "string", "why this formation"),
        writes=True)
    def set_formation(ctx: ToolContext, commander_id: int, squad: int,
                      formation: str, rationale: str) -> dict:
        cmds = {c.commander_id: c for c in ctx.view.own_commanders(ctx.h2_path)}
        if commander_id not in cmds:
            raise ToolError(f"commander {commander_id} is not one of ours.")
        _check_squad(ctx, commander_id, squad)
        leadership = _effective_commander_leadership(
            ctx, cmds[commander_id])
        if leadership is None:
            raise ToolError(
                f"{cmds[commander_id].name}'s final leadership is unknown.")
        available = LD.available_formations(leadership.normal)
        if formation not in available:
            raise ToolError(
                f"{formation} requires final normal Leadership "
                f"{LD.ADVANCED_FORMATION_MIN_LEADERSHIP}; "
                f"{cmds[commander_id].name} has {leadership.normal}. "
                f"Available formations: {sorted(available)}.")
        code = O.FORMATION_CODES[formation]
        try:
            row_id = M.record_battle_order(
                ctx.game_db, ctx.game_id, ctx.turn, commander_id, squad=squad,
                formation=code, commander_name=cmds[commander_id].name,
                rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        return {"recorded": row_id, "commander": cmds[commander_id].name,
                "squad": squad, "formation": formation,
                "formation_code": code}

    @reg.tool(
        "set_battle_position",
        "Place one squad on the battlefield grid. x runs -12 west to +12 "
        "east; y runs -12 north to +12 south; (0,0) is the centre.",
        Param("commander_id", "integer", "id from list_commanders"),
        Param("squad", "integer", "real squad slot 0-4"),
        Param("x", "integer", "horizontal coordinate -12..12"),
        Param("y", "integer", "vertical coordinate -12..12"),
        Param("rationale", "string", "why this battlefield position"),
        writes=True)
    def set_battle_position(ctx: ToolContext, commander_id: int, squad: int,
                            x: int, y: int, rationale: str) -> dict:
        commanders = {c.commander_id: c
                      for c in ctx.view.own_commanders(ctx.h2_path)}
        commander = commanders.get(commander_id)
        if commander is None:
            raise ToolError(f"commander {commander_id} is not one of ours.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        _check_squad(ctx, commander_id, squad)
        try:
            row_id = M.record_battle_position(
                ctx.game_db, ctx.game_id, ctx.turn, commander_id, squad, x, y,
                commander_name=commander.name, rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        return {"recorded": row_id, "commander": commander.name,
                "squad": squad, "x": x, "y": y}

    @reg.tool(
        "set_carried_gems",
        "Set how many gems of one path a commander carries into battle. "
        "Amount 0 removes that path. Materialisation adjusts the national "
        "remaining-gem pool by the change from the pristine save.",
        Param("commander_id", "integer", "id from list_commanders"),
        Param("path", "string", "gem path",
              choices=tuple(O.CARRIED_GEM_PATHS)),
        Param("amount", "integer", "desired carried total, 0-255"),
        Param("rationale", "string", "why this commander needs the gems"),
        writes=True)
    def set_carried_gems(ctx: ToolContext, commander_id: int, path: str,
                         amount: int, rationale: str) -> dict:
        commanders = {c.commander_id: c
                      for c in ctx.view.own_commanders(ctx.h2_path)}
        commander = commanders.get(commander_id)
        if commander is None:
            raise ToolError(f"commander {commander_id} is not one of ours.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        canonical = path.strip().lower()
        if canonical not in O.CARRIED_GEM_PATHS:
            raise ToolError(
                f"unknown gem path {path!r}. Known: {list(O.CARRIED_GEM_PATHS)}")
        try:
            row_id = M.record_carried_gems(
                ctx.game_db, ctx.game_id, ctx.turn, commander_id,
                O.CARRIED_GEM_PATHS.index(canonical), amount,
                commander_name=commander.name, rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        return {"recorded": row_id, "commander": commander.name,
                "path": canonical, "amount": amount,
                "note": "call materialize_orders to verify the shared gem budget"}

    @reg.tool(
        "set_battle_script",
        "Replace a commander's five-round battle script. Pass a JSON array "
        "of up to five fixed choices or researched combat spell ids/exact "
        "names. An empty array clears the script.",
        Param("commander_id", "integer", "id from list_commanders"),
        Param("rounds", "string", "JSON array, for example "
              '["hold_one_turn", "Fire Flies", "attack_one_turn"]'),
        Param("rationale", "string", "why this battle script"),
        writes=True)
    def set_battle_script(ctx: ToolContext, commander_id: int, rounds: str,
                          rationale: str) -> dict:
        commanders = {c.commander_id: c
                      for c in ctx.view.own_commanders(ctx.h2_path)}
        commander = commanders.get(commander_id)
        if commander is None:
            raise ToolError(f"commander {commander_id} is not one of ours.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        try:
            requested = json.loads(rounds)
        except json.JSONDecodeError as exc:
            raise ToolError(f"rounds must be a JSON array: {exc}")
        if not isinstance(requested, list):
            raise ToolError("rounds must be a JSON array.")
        if len(requested) > 5:
            raise ToolError(
                f"battle script holds at most 5 rounds, got {len(requested)}.")

        encoded: list[int] = []
        rendered: list[dict] = []
        fixed = {name.casefold(): (name, code)
                 for name, code in O.SINGLE_ROUND_CODES.items()}
        for position, value in enumerate(requested):
            choice = (fixed.get(value.strip().casefold())
                      if isinstance(value, str) else None)
            if choice is not None:
                name, code = choice
                encoded.append(code)
                rendered.append({"round": position + 1, "choice": name})
                continue
            spell = _script_spell(ctx, commander, value, position)
            spell_id = int(spell["id"])
            encoded.append(spell_id)
            rendered.append({"round": position + 1, "spell_id": spell_id,
                             "spell": spell["name"]})
        try:
            row_id = M.record_battle_script(
                ctx.game_db, ctx.game_id, ctx.turn, commander_id, encoded,
                commander_name=commander.name, rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        return {"recorded": row_id, "commander": commander.name,
                "rounds": rendered}

    @reg.tool(
        "assign_troops",
        "Move named troop instances into an existing squad led by one of our "
        "commanders. Squads may mix troop types; leadership is checked against "
        "the commander's complete final army. This tool cannot create a new "
        "squad. Mounted riders automatically bring their adjacent mount record.",
        Param("commander_id", "integer", "target id from list_commanders"),
        Param("squad", "integer", "occupied target squad slot 0-4"),
        Param("unit_instance_ids", "string", "JSON array of instance ids from "
              "list_units(include_instances=true)"),
        Param("rationale", "string", "why these troops join this squad"),
        writes=True)
    def assign_troops(ctx: ToolContext, commander_id: int, squad: int,
                      unit_instance_ids: str, rationale: str) -> dict:
        commanders = {c.commander_id: c
                      for c in ctx.view.own_commanders(ctx.h2_path)}
        commander = commanders.get(commander_id)
        if commander is None:
            raise ToolError(f"commander {commander_id} is not one of ours.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        _check_squad(ctx, commander_id, squad)
        requested, chosen, _selected = _troop_selection(
            ctx, unit_instance_ids)
        leadership = _leadership_check(ctx, commander, chosen)

        # Run the exact byte-level validator now, in memory, so an invalid
        # decision is returned to the model while it can still correct it.
        try:
            preview = O.OrdersEditor(ctx.h2_path).assign_troops(
                ctx.nation_id,
                [(instance_id, commander_id, squad)
                 for instance_id in requested])
            row_ids = M.record_troop_assignments(
                ctx.game_db, ctx.game_id, ctx.turn, chosen, commander_id,
                squad, commander_name=commander.name,
                rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        return {
            "recorded": row_ids,
            "commander": commander.name,
            "leadership": leadership,
            "squad": squad,
            "units": [
                {"instance_id": instance_id, "unit_type_id": type_id,
                 "name": name,
                 "includes_mount": moved["records"] == 2}
                for (instance_id, type_id, name), moved
                in zip(chosen, preview["moved"])],
            "note": "call materialize_orders to preview the complete assignment",
        }

    @reg.tool(
        "detach_troops",
        "Move selected troops from their current commander squad into the "
        "garrison of the province where they already stand. Their province "
        "does not change. Mounted riders bring their mount, and detaching a "
        "squad's final troop removes that now-empty squad slot.",
        Param("unit_instance_ids", "string", "JSON array of attached troop "
              "instance ids from list_units(include_instances=true)"),
        Param("rationale", "string", "why these troops return to the garrison"),
        writes=True)
    def detach_troops(ctx: ToolContext, unit_instance_ids: str,
                      rationale: str) -> dict:
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        requested, chosen, _selected = _troop_selection(
            ctx, unit_instance_ids)
        assert ctx.h2_path is not None
        try:
            preview = O.OrdersEditor(ctx.h2_path).detach_troops(
                ctx.nation_id, requested)
            row_ids = M.record_troop_detachments(
                ctx.game_db, ctx.game_id, ctx.turn, chosen,
                rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        by_instance = {
            int(row["instance_id"]): row for row in preview["detached"]}
        return {
            "recorded": row_ids,
            "destination": "province_garrison",
            "units": [
                {
                    "instance_id": instance_id,
                    "unit_type_id": type_id,
                    "name": name,
                    "includes_mount": by_instance[instance_id]["records"] == 2,
                    "source_commander_id": by_instance[instance_id][
                        "source_commander_id"],
                    "source_squad": by_instance[instance_id]["source_slot"],
                }
                for instance_id, type_id, name in chosen
            ],
            "squads_emptied": preview["cleared_slots"],
            "note": "call materialize_orders to write the detachment",
        }

    @reg.tool(
        "create_squad",
        "Create a new squad in the first empty slot under one of our "
        "commanders, populated with unattached garrison troops. Mixed troop "
        "types are legal, as they share the same squad warband token. "
        "The commander's exact current province comes from the .2h/.trn "
        "runtime-index join, so troopless commanders are valid targets. "
        "Mounted riders bring their mount.",
        Param("commander_id", "integer", "target id from list_commanders"),
        Param("unit_instance_ids", "string", "JSON array of unattached "
              "instance ids from list_units(include_instances=true)"),
        Param("rationale", "string", "why these troops form a new squad"),
        writes=True)
    def create_squad(ctx: ToolContext, commander_id: int,
                     unit_instance_ids: str, rationale: str) -> dict:
        commanders = {c.commander_id: c
                      for c in ctx.view.own_commanders(ctx.h2_path)}
        commander = commanders.get(commander_id)
        if commander is None:
            raise ToolError(f"commander {commander_id} is not one of ours.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        requested, chosen, selected = _troop_selection(
            ctx, unit_instance_ids)
        if any(unit.warband != O.NO_SQUAD for unit in selected):
            attached = [unit.instance_id for unit in selected
                        if unit.warband != O.NO_SQUAD]
            raise ToolError(
                "new squads currently accept only unattached garrison troops; "
                f"already attached: {attached}.")
        leadership = _leadership_check(ctx, commander, chosen)
        assert ctx.h2_path is not None
        data = ctx.h2_path.read_bytes()
        block = O.find_order_blocks(data).get(commander_id)
        if block is None:
            raise ToolError(f"no order block for commander {commander_id}.")
        occupied = {row["slot"] for row in O.read_squad_slots(
            data, block.name_end)}
        if commander.province_id is None:
            raise ToolError(
                f"{commander.name}'s exact province could not be joined to "
                "their .trn stat record, so a new squad cannot be placed.")
        selected_provinces = {
            struct.unpack_from("<H", data, unit.offset + 4)[0]
            for unit in selected
        }
        if selected_provinces != {commander.province_id}:
            raise ToolError(
                f"{commander.name} is in province {commander.province_id}, "
                f"but the selected troops are in {sorted(selected_provinces)}.")
        pending = list(ctx.game_db.execute(
            "SELECT target_commander_id, target_squad, squad_id FROM "
            "current_squad_creation_intent WHERE game_id=? AND turn=?",
            (ctx.game_id, ctx.turn)))
        pending_slots = {
            int(row["target_squad"]) for row in pending
            if int(row["target_commander_id"])
            == commander_id
        }
        available = sorted(set(range(5)) - occupied - pending_slots)
        if not available:
            raise ToolError(f"{commander.name} has no empty squad slot.")
        squad = available[0]
        reserved = {int(row["squad_id"]) for row in pending}
        squad_id = O.allocate_squad_id(
            data, ctx.nation_id, commander_id, squad, requested,
            reserved_ids=reserved)
        try:
            preview = O.OrdersEditor(ctx.h2_path).assign_troops(
                ctx.nation_id,
                [(instance_id, commander_id, squad)
                 for instance_id in requested],
                new_squads={(commander_id, squad): squad_id},
                commander_provinces={commander_id: commander.province_id})
            recorded = M.record_squad_creation(
                ctx.game_db, ctx.game_id, ctx.turn, chosen, commander_id,
                squad, squad_id, commander_name=commander.name,
                rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        return {
            "recorded": recorded,
            "commander": commander.name,
            "squad": squad,
            "squad_id": squad_id,
            "position": {"x": 0, "y": 0},
            "battle_orders": "default",
            "leadership": leadership,
            "units": [
                {"instance_id": instance_id, "unit_type_id": type_id,
                 "name": name,
                 "includes_mount": moved["records"] == 2}
                for (instance_id, type_id, name), moved
                in zip(chosen, preview["moved"])],
            "note": "call materialize_orders to preview the complete assignment",
        }

    @reg.tool(
        "equip_item",
        "Put a magic item in one of a commander's equipment slots. item_id 0 "
        "empties the slot. Only an unequipped item in our treasury, or the "
        "item already in this exact slot, is accepted. Use transfer_item to "
        "move a worn item directly between slots.",
        Param("commander_id", "integer", "id from list_commanders"),
        Param("slot", "string", "equipment slot",
              choices=tuple(O.EQUIPMENT_SLOTS)),
        Param("item_id", "integer", "reference item id, or 0 to empty"),
        Param("rationale", "string", "why this item on this commander"),
        writes=True)
    def equip_item(ctx: ToolContext, commander_id: int, slot: str,
                   item_id: int, rationale: str) -> dict:
        cmds = {c.commander_id: c for c in ctx.view.own_commanders(ctx.h2_path)}
        if commander_id not in cmds:
            raise ToolError(f"commander {commander_id} is not one of ours.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        if item_id:
            found = ctx.reference_db.execute(
                "SELECT name, type FROM items WHERE id=?", (item_id,)).fetchone()
            if not found:
                raise ToolError(
                    f"no item with id {item_id}. Call lookup_item by name.")
            allowed_types = O.ITEM_TYPES_BY_SLOT[slot]
            if found["type"] not in allowed_types:
                raise ToolError(
                    f"{found['name']} is type {found['type']!r}, which does not "
                    f"fit slot {slot!r}; accepted type(s): {sorted(allowed_types)}.")
            _validate_item_allocation(
                ctx, equipment_overrides={(commander_id, slot): item_id})
        try:
            row_id = M.record_equipment(
                ctx.game_db, ctx.game_id, ctx.turn, commander_id, slot,
                item_id, commander_name=cmds[commander_id].name,
                rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        return {"recorded": row_id, "commander": cmds[commander_id].name,
                "slot": slot, "item_id": item_id}

    @reg.tool(
        "transfer_item",
        "Move the exact item worn in one commander slot into another legal "
        "commander slot in the same turn. The source clear and destination "
        "equip are recorded transactionally; an item displaced at the "
        "destination returns to the treasury.",
        Param("source_commander_id", "integer", "source id from list_commanders"),
        Param("source_slot", "string", "occupied source equipment slot",
              choices=tuple(O.EQUIPMENT_SLOTS)),
        Param("destination_commander_id", "integer", "destination commander id"),
        Param("destination_slot", "string", "compatible destination slot",
              choices=tuple(O.EQUIPMENT_SLOTS)),
        Param("rationale", "string", "why this item is being transferred"),
        writes=True)
    def transfer_item(
        ctx: ToolContext,
        source_commander_id: int,
        source_slot: str,
        destination_commander_id: int,
        destination_slot: str,
        rationale: str,
    ) -> dict:
        commanders = {
            commander.commander_id: commander
            for commander in ctx.view.own_commanders(ctx.h2_path)
        }
        source = commanders.get(source_commander_id)
        destination = commanders.get(destination_commander_id)
        if source is None:
            raise ToolError(f"source commander {source_commander_id} is not one of ours.")
        if destination is None:
            raise ToolError(
                f"destination commander {destination_commander_id} is not one of ours.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        if (source_commander_id, source_slot) == (
                destination_commander_id, destination_slot):
            raise ToolError("source and destination equipment slots are identical.")

        item_id = _effective_equipment(ctx, source_commander_id).get(source_slot)
        if not item_id:
            raise ToolError(
                f"{source.name}'s {source_slot} slot is empty after pending intents.")
        item = ctx.reference_db.execute(
            "SELECT name, type FROM items WHERE id=?", (item_id,)).fetchone()
        if item is None:
            raise ToolError(f"item {item_id} is absent from the reference data.")
        accepted = O.ITEM_TYPES_BY_SLOT[destination_slot]
        if item["type"] not in accepted:
            raise ToolError(
                f"{item['name']} is type {item['type']!r}, which does not fit "
                f"slot {destination_slot!r}; accepted type(s): {sorted(accepted)}.")
        _validate_item_allocation(
            ctx,
            equipment_overrides={
                (source_commander_id, source_slot): 0,
                (destination_commander_id, destination_slot): item_id,
            },
        )
        try:
            source_row, destination_row = M.record_equipment_transfer(
                ctx.game_db,
                ctx.game_id,
                ctx.turn,
                source_commander_id,
                source_slot,
                destination_commander_id,
                destination_slot,
                item_id,
                source_commander_name=source.name,
                destination_commander_name=destination.name,
                rationale=rationale.strip(),
            )
        except ValueError as exc:
            raise ToolError(str(exc))
        return {
            "recorded": [source_row, destination_row],
            "item_id": item_id,
            "item": item["name"],
            "source": {
                "commander_id": source_commander_id,
                "commander": source.name,
                "slot": source_slot,
            },
            "destination": {
                "commander_id": destination_commander_id,
                "commander": destination.name,
                "slot": destination_slot,
            },
        }

    @reg.tool(
        "set_diplomatic_action",
        "Propose a non-aggression pact, accept or decline a current incoming "
        "proposal, declare war, or clear our pending outgoing action toward one nation. "
        "NAP responses remain replaceable until the turn is submitted. This "
        "records intent; call materialize_orders to write it.",
        Param("nation_id", "integer", "target nation id from get_diplomatic_relations"),
        Param(
            "action",
            "string",
            "propose_nap | accept_nap | decline_nap | declare_war | clear",
            choices=(
                "propose_nap",
                "accept_nap",
                "decline_nap",
                "declare_war",
                "clear",
            ),
        ),
        Param("rationale", "string", "why this diplomatic action is appropriate"),
        Param(
            "missive",
            "string",
            "the declaration text sent to the target, required for declare_war "
            "only. It is delivered to them verbatim, so write what we mean to "
            "say. The other actions use the game's own fixed wording.",
            required=False,
            default="",
        ),
        writes=True,
    )
    def set_diplomatic_action(
        ctx: ToolContext, nation_id: int, action: str, rationale: str,
        missive: str = "",
    ) -> dict:
        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError("no .2h file to update. The game writes it on save.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        relation = next(
            (row for row in ctx.view.diplomatic_relations() if row.nation_id == nation_id),
            None,
        )
        if relation is None:
            valid = [row.nation_id for row in ctx.view.diplomatic_relations()]
            raise ToolError(
                f"nation {nation_id} is not a participant in this game. Valid: {valid}."
            )

        supplied_missive = missive.strip()
        outgoing: str | None = None
        if action != "declare_war" and supplied_missive:
            raise ToolError(
                f"{action} uses the game's own fixed wording; a missive can "
                "only be supplied for declare_war."
            )
        if action != "clear":
            if relation.defeated:
                raise ToolError(f"nation {nation_id} is defeated.")
            if relation.status == "same_god":
                raise ToolError(f"nation {nation_id} serves the same god and is an ally.")
            if not relation.contact:
                raise ToolError(
                    f"we have no contact with nation {nation_id}; the F4 panel "
                    "does not permit sending a diplomatic action."
                )
        if action == "propose_nap":
            if relation.waiting_for_response:
                raise ToolError(
                    f"already waiting for nation {nation_id} to answer our NAP proposal."
                )
            if relation.nap_phase == "ending":
                raise ToolError(
                    f"nation {nation_id} is in a {relation.status} countdown to war. "
                    "A pact cannot be proposed while one is expiring. Peace can be "
                    "sued for once the war has actually begun."
                )
            if relation.nap_phase is not None:
                raise ToolError(
                    f"nation {nation_id} already has NAP state {relation.status}."
                )
            outgoing = "We propose peace and a non-aggression treaty"
        elif action in {"accept_nap", "decline_nap"}:
            from dom6_assistant.file_reader.formats import messages as Msg

            incoming_sources = {
                message.source_nation_id
                for message in ctx.view.turn_messages()
                if message.type_id == Msg.NAP_PROPOSAL_TYPE
            }
            if nation_id not in incoming_sources:
                raise ToolError(
                    f"nation {nation_id} has not sent us a current NAP proposal."
                )
            outgoing = {
                "accept_nap": "We accept the non-aggression treaty",
                "decline_nap": "We decline the non-aggression treaty",
            }[action]
        elif action == "declare_war":
            # Legal from `apprehensive`, and from an active NAP, where it starts
            # the pact's notice counting down instead of beginning the war at
            # once. Both are observed. The remaining states are closed by rule:
            # a countdown is already a declaration, and a war is already a war.
            if relation.nap_phase == "ending":
                raise ToolError(
                    f"nation {nation_id} is already in a {relation.status} countdown "
                    "to war; war cannot be declared against them twice."
                )
            if relation.status != "apprehensive" and relation.nap_phase != "active":
                raise ToolError(
                    f"war declaration is not available in relation state "
                    f"{relation.status}."
                )
            if not supplied_missive:
                raise ToolError(
                    "declare_war requires a missive. Unlike the NAP actions, "
                    "the declaration text is delivered to the target verbatim, "
                    "and the client's own default wording is written per nation "
                    "in the game binary, so it cannot be reproduced here. "
                    "Supply the text we mean to send."
                )
            outgoing = supplied_missive
        try:
            row_id = M.record_diplomatic_action(
                ctx.game_db,
                ctx.game_id,
                ctx.turn,
                nation_id,
                action,
                outgoing,
                rationale=rationale.strip(),
            )
        except ValueError as exc:
            raise ToolError(str(exc))
        return {
            "recorded": row_id,
            "nation_id": nation_id,
            "action": action,
            "missive": outgoing,
            "note": "call materialize_orders to preview or write it",
        }

    @reg.tool(
        "set_province_defence",
        "Set the complete province-defence target for one province we own. "
        "The target may be raised through 100 or lowered only as far as the "
        "turn-start level to refund purchases made this turn. Each added point "
        "costs its new level in gold. This records intent; call "
        "materialize_orders to write it.",
        Param("province_id", "integer", "a province we own",
              hint="Call get_province_defence for valid ids and current levels."),
        Param("target", "integer", "complete desired defence level, 0-100"),
        Param("rationale", "string", "why this defence spending is worthwhile"),
        writes=True)
    def set_province_defence(ctx: ToolContext, province_id: int, target: int,
                             rationale: str) -> dict:
        from dom6_assistant.file_reader.formats import h2

        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError(
                "no .2h file to update. The game writes it on save.")
        provinces = {
            province.province_id: province
            for province in ctx.view.own_provinces()
        }
        if province_id not in provinces:
            raise ToolError(
                f"province {province_id} is not ours: {sorted(provinces)}. "
                "Call get_province_defence for valid ids.")
        if not 0 <= target <= h2.MAX_PROVINCE_DEFENCE:
            raise ToolError(
                f"target must be 0-{h2.MAX_PROVINCE_DEFENCE}, got {target}.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        province = provinces[province_id]
        turn_start = province.province_defense
        if turn_start is None:
            raise ToolError(
                f"turn-start defence in {province.name} is not visible, so a "
                "legal refund floor cannot be established.")
        if target < turn_start:
            raise ToolError(
                f"target {target} is below {province.name}'s turn-start level "
                f"{turn_start}. Only points bought this turn can be refunded.")

        source = ctx.h2_path
        pristine = source.with_suffix(M.BASE_SUFFIX)
        if pristine.exists():
            source = pristine
        data = source.read_bytes()
        from dom6_assistant.agent.visibility import VisibilityError
        try:
            order_file_provinces = list(
                ctx.view.order_file_province_ids(data))
        except VisibilityError as exc:
            raise ToolError(str(exc))
        if province_id not in order_file_provinces:
            raise ToolError(
                f"{province.name} has no province-local block in the inherited "
                ".2h. Its turn-start defence is "
                f"{turn_start}, but a saved target cannot be written safely "
                "until the game creates that block.")
        try:
            file_targets = {
                pid: h2.province_defence(data, pid, order_file_provinces)
                for pid in order_file_provinces
            }
        except h2.QueueWriteRefused as exc:
            raise ToolError(str(exc))
        intended = {
            row["province_id"]: row["target"]
            for row in ctx.game_db.execute(
                "SELECT province_id, target FROM "
                "current_province_defence_intent WHERE game_id=? AND turn=?",
                (ctx.game_id, ctx.turn))
        }
        inaccessible = sorted(set(intended) - set(order_file_provinces))
        if inaccessible:
            raise ToolError(
                "recorded province-defence intent targets province(s) with no "
                f"writable .2h block: {inaccessible}")
        intended[province_id] = target
        projected_delta = sum(
            h2.province_defence_cost(intended.get(pid, file_targets[pid]))
            - h2.province_defence_cost(file_targets[pid])
            for pid in order_file_provinces)
        available = h2.gold_remaining(data)
        if available is None:
            raise ToolError(
                "the .2h remaining-gold field could not be located; refusing "
                "to record spending whose affordability cannot be checked.")
        if projected_delta > available:
            raise ToolError(
                f"the recorded defence targets would reserve {projected_delta} "
                f"gold, but the orders file has only {available} uncommitted.")

        try:
            row_id = M.record_province_defence(
                ctx.game_db, ctx.game_id, ctx.turn, province_id, target,
                rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        committed_here = (h2.province_defence_cost(target)
                          - h2.province_defence_cost(turn_start))
        return {
            "recorded": row_id,
            "province_id": province_id,
            "province_name": province.name,
            "turn_start": turn_start,
            "target": target,
            "points_bought": target - turn_start,
            "gold_committed_here": committed_here,
            "projected_gold_delta_from_orders_file": projected_delta,
            "projected_gold_remaining": available - projected_delta,
            "note": "call materialize_orders to preview the complete gold budget",
        }

    @reg.tool(
        "place_mercenary_bid",
        "Bid gold for a mercenary company, or withdraw a bid by passing 0. "
        "The whole bid is reserved immediately whether or not it wins. Name "
        "the province the company should arrive in. Rival bids are not in the "
        "turn file, so no bid can be known to win; a losing bid is refunded "
        "when the next turn arrives.",
        Param("company", "string", "exact company name",
              hint="Call get_mercenaries for the companies and their prices."),
        Param("amount", "integer", "gold to bid, or 0 to withdraw"),
        Param("province_id", "integer",
              "a province we own, where the company would arrive",
              required=False),
        Param("rationale", "string", "why this company at this price"),
        writes=True)
    def place_mercenary_bid(ctx: ToolContext, company: str, amount: int,
                            rationale: str,
                            province_id: int | None = None) -> dict:
        from dom6_assistant.file_reader.formats import h2

        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError(
                "no .2h file to update. The game writes it on save.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")

        companies = T.read_mercenaries(ctx.view.data)
        if not companies:
            raise ToolError("no mercenary companies are on offer this turn.")
        names = [c.name for c in companies]
        matches = [i for i, c in enumerate(companies)
                   if c.name.lower() == company.strip().lower()]
        if not matches:
            raise ToolError(
                f"no company named {company!r} is on offer. On offer: {names}. "
                "Call get_mercenaries for prices and contract state.")
        slot = matches[0]
        chosen = companies[slot]
        minimum = MC.calculate_minimum_bid(
            chosen, ctx.nation_id, ctx.reference_db)

        # The slot IS the auction-list position, confirmed by a controlled
        # save: with a bid already standing on the first company, bidding the
        # second wrote amount and province into slot 1 and left slot 0
        # untouched. Both arrays are indexed the same way.
        source = ctx.h2_path
        pristine = source.with_suffix(M.BASE_SUFFIX)
        if pristine.exists():
            source = pristine
        data = source.read_bytes()
        try:
            standing = {b.slot: b for b in h2.read_mercenary_bids(data)}
        except h2.QueueWriteRefused as exc:
            raise ToolError(str(exc))
        was = standing.get(slot)

        if amount == 0:
            if province_id is not None:
                raise ToolError(
                    "withdrawing a bid takes no province_id.")
            if was is None:
                raise ToolError(
                    f"there is no standing bid on {chosen.name} to withdraw.")
            row_id = M.record_mercenary_bid(
                ctx.game_db, ctx.game_id, ctx.turn, slot, chosen.name,
                None, None, rationale=rationale.strip())
            return {"recorded": row_id, "company": chosen.name,
                    "withdrawn": True, "gold_refunded": was.amount,
                    "note": "call materialize_orders to write it"}

        if amount < 0:
            raise ToolError(f"amount must not be negative, got {amount}.")
        if amount < minimum.amount:
            raise ToolError(
                f"bid {amount} is below our nation-adjusted minimum of "
                f"{minimum.amount} for {chosen.name} "
                f"({minimum.percentage}% of the {chosen.price}-gold asking "
                "price).")
        if province_id is None:
            raise ToolError(
                "province_id is required: the hire screen always has a land "
                "selected, and it is where the company arrives if the bid "
                "wins.")
        owned = {p.province_id: p for p in ctx.view.own_provinces()}
        if province_id not in owned:
            raise ToolError(
                f"province {province_id} is not ours: {sorted(owned)}. A "
                "mercenary company arrives in a province we hold.")
        # Every pending bid competes for the same uncommitted gold, so budget
        # them together rather than one at a time.
        intended = {
            int(r["slot"]): r["amount"]
            for r in ctx.game_db.execute(
                "SELECT slot, amount FROM current_mercenary_bid_intent "
                "WHERE game_id=? AND turn=?", (ctx.game_id, ctx.turn))
        }
        intended[slot] = amount
        projected = sum(
            (intended.get(s, standing[s].amount if s in standing else 0) or 0)
            - (standing[s].amount if s in standing else 0)
            for s in set(intended) | set(standing))
        available = h2.gold_remaining(data)
        if available is None:
            raise ToolError(
                "the .2h remaining-gold field could not be located; refusing "
                "to record a bid whose affordability cannot be checked.")
        if projected > available:
            raise ToolError(
                f"the recorded bids would reserve {projected} gold, but the "
                f"orders file has only {available} uncommitted.")

        try:
            row_id = M.record_mercenary_bid(
                ctx.game_db, ctx.game_id, ctx.turn, slot, chosen.name,
                amount, province_id, rationale=rationale.strip(),
                quoted_minimum=minimum.amount)
        except ValueError as exc:
            raise ToolError(str(exc))
        return {
            "recorded": row_id,
            "company": chosen.name,
            "amount": amount,
            "asking_price": chosen.price,
            "minimum_bid": minimum.amount,
            "minimum_bid_percentage": minimum.percentage,
            "minimum_bid_basis": minimum.basis,
            "arrives_in": owned[province_id].name,
            "province_id": province_id,
            "previous_bid": was.amount if was is not None else None,
            "projected_gold_delta_from_orders_file": projected,
            "projected_gold_remaining": available - projected,
            "warning": "the whole amount is reserved now and the auction may "
                       "still be lost to a higher bid we cannot see; if it "
                       "loses, the game refunds the gold next turn",
            "note": "call materialize_orders to write it",
        }

    @reg.tool(
        "queue_recruits",
        "Queue units for recruitment in one province, replacing whatever is "
        'queued there. Pass a JSON array of {"unit_type_id": N, "count": N}. '
        "Queueing more than the province can afford is ALLOWED and sometimes "
        "right: it builds what it can and carries the rest to next turn, which "
        "is how you commit to an expensive unit early. Call plan_recruitment "
        "to see what is likely to fit.",
        Param("province_id", "integer", "a province we own with a fort"),
        Param("units", "string",
              'JSON array, e.g. [{"unit_type_id": 218, "count": 3}]',
              hint="Call list_recruitable for ids and costs."),
        Param("rationale", "string", "why these units"),
        writes=True)
    def queue_recruits(ctx: ToolContext, province_id: int, units: str,
                       rationale: str) -> dict:
        from dom6_assistant.reference.unit_cost import (
            gold_cost_for,
            recruitment_point_cost_for,
            resource_cost_for,
        )
        owned = [p.province_id for p in ctx.view.own_provinces()]
        if province_id not in owned:
            raise ToolError(f"province {province_id} is not ours: {owned}.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        try:
            parsed = json.loads(units) if isinstance(units, str) else units
        except json.JSONDecodeError as exc:
            raise ToolError(f"units must be a JSON array: {exc}")
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            raise ToolError("units must be a JSON array of objects.")

        recruitable = {e["unit_type_id"]: e
                       for e in _recruitable_here(ctx, province_id)}
        expanded: list[tuple[int, int, str]] = []
        for entry in parsed:
            if not isinstance(entry, dict):
                raise ToolError(f"{entry!r} is not an object.")
            unit_id = Param("unit_type_id", "integer", "").validate(
                entry.get("unit_type_id"))
            count = Param("count", "integer", "").validate(entry.get("count", 1))
            if unit_id not in recruitable:
                raise ToolError(
                    f"unit {unit_id} is not recruitable in province "
                    f"{province_id}. Call list_recruitable for what is.")
            if not 1 <= count <= 50:
                raise ToolError(f"count must be 1-50, got {count}")
            kind = recruitable[unit_id]["kind"]
            gold = (recruitable[unit_id].get("gold")
                    or gold_cost_for(ctx.reference_db, unit_id,
                                     is_commander=kind == "commander"))
            if gold is None:
                raise ToolError(
                    f"no price known for unit {unit_id}, and the .2h stores "
                    "the cost beside the id — writing a wrong one may desync "
                    "the treasury. Observe it in game first.")
            expanded.extend([(unit_id, gold, kind)] * count)

        conn = ctx.game_db
        conn.execute("DELETE FROM recruit_intent_v2 WHERE game_id=? AND turn=? "
                     "AND province_id=?", (ctx.game_id, ctx.turn, province_id))
        for position, (unit_id, gold, kind) in enumerate(expanded):
            conn.execute(
                "INSERT INTO recruit_intent_v2(game_id, turn, province_id, "
                "position, unit_type_id, kind, gold, rationale) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (ctx.game_id, ctx.turn, province_id, position, unit_id, kind,
                 gold, rationale.strip()))
        conn.commit()

        resources = sum(resource_cost_for(ctx.reference_db, u) or 0
                        for u, _g, _kind in expanded)
        recruit_points = [recruitment_point_cost_for(ctx.reference_db, u)
                          for u, _g, _kind in expanded]
        budget = owned_province_resource_budget(ctx, province_id)
        out = {"province_id": province_id, "queued": len(expanded),
               "commanders": sum(kind == "commander"
                                 for _u, _g, kind in expanded),
               "troops": sum(kind == "troop"
                             for _u, _g, kind in expanded),
               "gold": sum(g for _u, g, _kind in expanded),
               "resources": resources,
               "resources_available": budget.total,
               "resources_source": budget.source,
               "resources_exact": budget.exact,
               "recruitment_points": (sum(recruit_points)
                                      if all(p is not None
                                             for p in recruit_points)
                                      else None)}
        if budget.note:
            out["resources_note"] = budget.note
        if budget.total is not None and resources > budget.total:
            # Reported, not refused. The surplus carries over, and committing
            # to it deliberately is a real tactic.
            out["note"] = (
                f"this costs {resources} resources against "
                f"{budget.total} available, so some will build next turn "
                "rather than this one. That is allowed.")
        return out

    @reg.tool(
        "queue_research",
        "Replace the nation's research queue. Pass a JSON array of up to nine "
        "targets in execution order. Use a school name for its next ordinary "
        "level, repeating it to continue that school. Use a Level 9 spell id "
        "or exact spell name after its school reaches Level 8. An empty array "
        "clears the queue. Call get_research first and lookup_spell for ids.",
        Param("targets", "string", "JSON array of school names and Level 9 "
              "spell ids, for example "
              '["Conjuration", "Conjuration", 1080]'),
        Param("rationale", "string", "why this research order"),
        writes=True)
    def queue_research(ctx: ToolContext, targets: str,
                       rationale: str) -> dict:
        try:
            requested = json.loads(targets)
        except json.JSONDecodeError as exc:
            raise ToolError(f"targets must be a JSON array: {exc}")
        if not isinstance(requested, list):
            raise ToolError(
                "targets must be a JSON array of school names and/or Level 9 spell ids.")
        if len(requested) > 9:
            raise ToolError(f"research queue holds at most 9 entries, got {len(requested)}.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")

        canonical = {name.casefold(): (index, name)
                     for index, name in enumerate(T.RESEARCH_SCHOOLS)}
        levels = ctx.view.parsed.research_levels
        if levels is None:
            raise ToolError(
                "current research levels could not be decoded; refusing to "
                "guess the levels reached by queued entries.")
        effective_levels = list(levels)
        encoded: list[int] = []
        queue: list[dict[str, int | str]] = []
        for position, value in enumerate(requested):
            school = (canonical.get(value.strip().casefold())
                      if isinstance(value, str) else None)
            if school is not None:
                school_id, school_name = school
                target_level = effective_levels[school_id] + 1
                if target_level > 8:
                    raise ToolError(
                        f"entry {position} asks for {school_name}'s next "
                        f"ordinary level, but that would be Level {target_level}. "
                        "At Level 9, give an individual spell id or exact name.")
                effective_levels[school_id] = target_level
                encoded.append(school_id)
                queue.append({"position": position, "kind": "school_level",
                              "school": school_name,
                              "target_level": target_level})
                continue

            spell = _level_nine_spell(ctx, value, position)
            spell_school = int(spell["school"])
            school_name = T.RESEARCH_SCHOOLS[spell_school]
            if effective_levels[spell_school] < 8:
                raise ToolError(
                    f"entry {position} is {spell['name']} ({school_name} "
                    f"Level 9), but {school_name} reaches only Level "
                    f"{effective_levels[spell_school]} before that queue "
                    "position. Queue its missing ordinary levels first.")
            spell_id = int(spell["id"])
            encoded.append(spell_id)
            queue.append({"position": position, "kind": "level_9_spell",
                          "school": school_name, "target_level": 9,
                          "spell_id": spell_id, "spell": str(spell["name"])})

        try:
            row_id = M.record_research_queue(
                ctx.game_db, ctx.game_id, ctx.turn, encoded,
                rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        return {"recorded": row_id, "queue": queue,
                "entries": len(queue),
                "note": "call materialize_orders to write this queue to the .2h"}

    @reg.tool(
        "forge_item",
        "Forge a magic item. The gem cost is reserved immediately from the "
        "national pool using the client's exact item, nation, chassis, hammer, "
        "site and global-enchantment modifiers. This records intent; call "
        "materialize_orders to write it.",
        Param("commander_id", "integer", "a mage in a province with a lab",
              hint="Call list_forgeable_items for who can forge what."),
        Param("item_id", "integer", "reference item id",
              hint="Call list_forgeable_items or lookup_item for ids."),
        Param("rationale", "string", "why this item, for this commander, now"),
        writes=True)
    def forge_item(ctx: ToolContext, commander_id: int, item_id: int,
                   rationale: str) -> dict:
        from dom6_assistant.file_reader.formats import h2
        from dom6_assistant.reference import forge_cost as FC

        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError(
                "no .2h file to update. The game writes it on save.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        cmds = {c.commander_id: c for c in ctx.view.own_commanders(ctx.h2_path)}
        if commander_id not in cmds:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Ours are: "
                f"{sorted(cmds)}.")
        commander = cmds[commander_id]

        # Same prerequisites the read side enforces: a visible non-Holy path,
        # an owned province, and a laboratory in it. Raises on failure with a
        # message naming the specific check, so it is not re-wrapped here.
        _eligibility(ctx, commander, "forge_magic_item")

        row = ctx.reference_db.execute(
            "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ToolError(
                f"no item with id {item_id}. Call lookup_item by name.")

        restrictions = FC.restricted_nations(row)
        if restrictions and ctx.view.nation_id not in restrictions:
            raise ToolError(
                f"{row['name']} is restricted to nation id(s) "
                f"{sorted(restrictions)}, not our nation "
                f"{ctx.view.nation_id}.")

        candidates = forgeable_item_ids(ctx, commander)
        if item_id not in candidates:
            effective_paths, adjustment, _sources = effective_forge_paths(
                ctx, commander)
            raise ToolError(
                f"{commander.name} cannot forge {row['name']}: it needs "
                f"Construction {row['constlevel']} and base paths "
                f"{row['mainpath']}{row['mainlevel']}"
                f"{(' ' + str(row['secondarypath']) + str(row['secondarylevel'])) if row['secondarypath'] else ''}. "
                f"Effective forge paths are {effective_paths} after a signed "
                f"{adjustment:+d} Master/Inept Smith adjustment. "
                "Call list_forgeable_items for what this commander can make.")

        try:
            cost, modifier_details = forge_cost_for_commander(
                ctx, commander, row)
        except FC.ForgeCostUnknown as exc:
            raise ToolError(str(exc))
        if not 1 <= len(cost.charged) <= 2:
            raise ToolError(
                f"{row['name']} requires {len(cost.charged)} magic paths "
                f"({', '.join(cost.listed)}); only the base game's one- and "
                "two-path forge layouts are decoded.")
        reservations = [
            (path_name, h2.GEM_PATHS.index(path_name), gems)
            for path_name, gems in cost.charged.items()
        ]

        source = ctx.h2_path
        pristine = source.with_suffix(M.BASE_SUFFIX)
        if pristine.exists():
            source = pristine
        data = source.read_bytes()
        try:
            available = list(h2.gem_remaining(data))
        except h2.QueueWriteRefused as exc:
            raise ToolError(str(exc))

        # Every pending forge competes for the same pool, so budget together.
        pending_by_path = [0] * len(h2.GEM_PATHS)
        for other in ctx.game_db.execute(
                "SELECT f.gem_path, f.gem_cost, f.secondary_gem_path, "
                "f.secondary_gem_cost FROM forge_intent f "
                "JOIN current_orders o ON o.id = f.order_intent_id "
                "WHERE o.game_id=? AND o.turn=? AND o.commander_id != ?",
                (ctx.game_id, ctx.turn, commander_id)):
            pending_by_path[int(other["gem_path"])] += int(other["gem_cost"])
            if other["secondary_gem_path"] is not None:
                pending_by_path[int(other["secondary_gem_path"])] += int(
                    other["secondary_gem_cost"])
        for path_name, path_index, gems in reservations:
            pending = pending_by_path[path_index]
            if available[path_index] - pending < gems:
                raise ToolError(
                    f"forging {row['name']} needs {gems} {path_name} gems; "
                    f"{available[path_index]} remain uncommitted and {pending} "
                    "are already reserved by other recorded forges.")

        primary = reservations[0]
        secondary = reservations[1] if len(reservations) == 2 else None
        try:
            row_id = M.record_forge(
                ctx.game_db, ctx.game_id, ctx.turn, commander_id, item_id,
                primary[1], primary[2], commander_name=commander.name,
                item_name=row["name"],
                secondary_gem_path=(secondary[1] if secondary else None),
                secondary_gem_cost=(secondary[2] if secondary else 0),
                rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        out = {
            "recorded": row_id,
            "commander": commander.name,
            "item": row["name"],
            "item_id": item_id,
            "gems_reserved": {
                path_name: gems for path_name, _path_index, gems in reservations
            },
            "gems_remaining_after": {
                path_name: available[path_index]
                - pending_by_path[path_index] - gems
                for path_name, path_index, gems in reservations
            },
            "forge_cost_modifiers": modifier_details,
            "note": "call materialize_orders to write it",
        }
        if cost.rebated:
            out["national_rebate"] = (
                "our nation receives the client-defined one-gem reduction on "
                "each required path; all other modifiers are then rounded in "
                "the client's order")
        return out

    @reg.tool(
        "empower_commander",
        "Permanently raise one commander's magic path by one level, using "
        "gems. Very expensive, and it works on any commander including one "
        "with no paths at all. This records intent; call materialize_orders "
        "to write it.",
        Param("commander_id", "integer", "any commander of ours"),
        Param("path", "string",
              "magic path: fire, air, water, earth, astral, death, nature, "
              "glamour or blood"),
        Param("rationale", "string", "why this commander and this path"),
        writes=True)
    def empower_commander(ctx: ToolContext, commander_id: int, path: str,
                          rationale: str) -> dict:
        from dom6_assistant.file_reader.formats import h2
        from dom6_assistant.reference import empowerment_cost as EC

        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError(
                "no .2h file to update. The game writes it on save.")
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        cmds = {c.commander_id: c for c in ctx.view.own_commanders(ctx.h2_path)}
        if commander_id not in cmds:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Ours are: "
                f"{sorted(cmds)}.")
        commander = cmds[commander_id]
        # Requires an owned province with a laboratory. Raises with the
        # specific failing check.
        _eligibility(ctx, commander, "empowerment")

        key = path.strip().lower()
        if key not in O.MAGIC_PATH_CODES:
            raise ToolError(
                f"unknown magic path {path!r}. Empowerable paths: "
                f"{sorted(O.MAGIC_PATH_CODES)}. Holy cannot be empowered.")
        path_index = O.MAGIC_PATH_CODES[key]
        # Astral is "S", so the initial letter will not do.
        letter = {"fire": "F", "air": "A", "water": "W", "earth": "E",
                  "astral": "S", "death": "D", "nature": "N",
                  "glamour": "G", "blood": "B"}[key]
        current = int(commander.paths.get(letter, 0))
        target = current + 1
        try:
            cost = EC.empowerment_cost(target)
        except EC.EmpowermentCostUnknown as exc:
            raise ToolError(str(exc))

        source = ctx.h2_path
        pristine = source.with_suffix(M.BASE_SUFFIX)
        if pristine.exists():
            source = pristine
        data = source.read_bytes()
        try:
            available = list(h2.gem_remaining(data))
        except h2.QueueWriteRefused as exc:
            raise ToolError(str(exc))
        if available[path_index] < cost:
            raise ToolError(
                f"empowering {commander.name} to {key} {target} costs {cost} "
                f"{key} gems; only {available[path_index]} remain uncommitted.")

        try:
            row_id = M.record_empowerment(
                ctx.game_db, ctx.game_id, ctx.turn, commander_id, path_index,
                target, cost, commander_name=commander.name,
                rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))

        out = {
            "recorded": row_id,
            "commander": commander.name,
            "path": key,
            "from_level": current,
            "to_level": target,
            "gems_reserved": {key: cost},
            "gems_remaining_after": available[path_index] - cost,
            "cost_source": (
                "measured in a controlled save" if EC.cost_is_confirmed(target)
                else "the published price chart; only the level-1 price has "
                     "been watched being charged"),
            "note": "permanent, and not cumulative — this pays for the level "
                    "being reached, not the levels below it",
        }
        return out

    @reg.tool(
        "cast_ritual",
        "Record a ritual spell for one commander. The spell must be researched, "
        "the commander must have its paths, and each supported target family "
        "must receive its typed selector. Set monthly=true for Monthly Ritual. This "
        "reserves the ritual's gems when materialize_orders writes the .2h. "
        "A monthly order repeats only while the next cast is affordable; if "
        "not, the client returns the caster to Defend. "
        "World global enchantments, Dispel/Disenchantment, and effect-82 "
        "persistent province enchantments may invest extra_gems. Globals, "
        "active-global selectors, unit-promoting rituals and item transports "
        "and Wish may not be monthly.",
        Param("commander_id", "integer", "caster id from list_commanders"),
        Param("spell_id", "integer", "ritual id from lookup_spell",
              hint="Call lookup_spell to find the exact id and requirements."),
        Param("rationale", "string", "why this ritual is being cast"),
        Param("target_province", "integer",
              "province id for a map-targeted ritual", required=False,
              hint="Call find_province to look up an id by name; "
                   "list_castable_spells reports the spell's map_range."),
        Param("target_commander", "integer",
              "recipient id, for a ritual that delivers to a commander",
              required=False,
              hint="Required by gem/item transport rituals; the recipient "
                   "must be in target_province."),
        Param("target_unit", "integer",
              "ordinary troop instance_id for a unit-targeted ritual",
              required=False,
              hint="For Gift of Reason or Divine Name, call list_units with "
                   "include_instances=true in the caster's province."),
        Param("target_item", "integer",
              "unequipped treasury item id for an item-transport ritual",
              required=False,
              hint="Call get_item_treasury; the selected item is reserved "
                   "when orders are materialized."),
        Param("wish_item", "integer",
              "magic item id to create with Wish", required=False,
              hint="Call lookup_item for an item id. This is not an owned "
                   "treasury item: Wish creates the selected magic item and the "
                   "game equips it on the caster if the cast succeeds."),
        Param("wish_unit", "integer",
              "unit type id to summon or transfer with Wish", required=False,
              hint="Call lookup_unit for the stable unit type id. No-Wish "
                   "units are rejected; named Horrors are classified "
                   "automatically."),
        Param("wish_nation", "integer",
              "nation id whose pretender Wish should try to kill", required=False,
              hint="Call get_nations for visible nation ids."),
        Param("wish_result", "string",
              "typed fixed outcome for Wish", required=False,
              choices=tuple(WISH_FIXED_RESULTS),
              hint="Canonical result, independent of the UI's many prose aliases."),
        Param("wish_random", "string",
              "client-equivalent random Wish request", required=False,
              choices=WISH.RANDOM_REQUESTS,
              hint="item chooses a Construction 1/3/5 tier then an item; "
                   "artifact chooses an available Construction 9 artifact; "
                   "something resolves to a special item, 7500 gold, or death; "
                   "horror leaves the host to choose the responding Horror."),
        Param("target_global", "integer",
              "effect_id selected by Dispel, Disenchantment, or Arcane "
              "Analysis", required=False,
              hint="Call get_global_enchantments for the effect_id of each "
                   "one currently active."),
        Param("extra_gems", "integer",
              "gems invested above base to strengthen a global, Dispel, or "
              "Disenchantment, or "
              "extend a persistent province enchantment", required=False,
              default=0,
              hint="For globals/Dispel/Disenchantment, strength also gains "
                   "five per excess "
                   "primary-path level. For effect-82 enchantments, the "
                   "database supplies the added months per gem."),
        Param("monthly", "boolean", "repeat the ritual every month",
              required=False, default=False),
        writes=True)
    def cast_ritual(ctx: ToolContext, commander_id: int, spell_id: int,
                    rationale: str, target_province: int | None = None,
                    target_commander: int | None = None,
                    target_unit: int | None = None,
                    target_item: int | None = None,
                    wish_item: int | None = None,
                    wish_unit: int | None = None,
                    wish_nation: int | None = None,
                    wish_result: str | None = None,
                    wish_random: str | None = None,
                    target_global: int | None = None,
                    extra_gems: int = 0,
                    monthly: bool = False) -> dict:
        if not rationale.strip():
            raise ToolError("rationale must not be empty.")
        commanders = {c.commander_id: c
                      for c in ctx.view.own_commanders(ctx.h2_path)}
        commander = commanders.get(commander_id)
        if commander is None:
            raise ToolError(
                f"commander {commander_id} is not one of ours. Call "
                "list_commanders for valid ids.")
        caster_item_ids = tuple(
            _effective_equipment(ctx, commander_id).values())

        spell = ctx.reference_db.execute(
            "SELECT * FROM spells WHERE id=?", (spell_id,)).fetchone()
        if spell is None:
            raise ToolError(
                f"no spell {spell_id}. Call lookup_spell to find its id.")
        if int(spell["path1"]) < 0 or int(spell["pathlevel1"] or 0) <= 0:
            raise ToolError(
                f"{spell['name']} has no caster path requirement and is an "
                "internal/event spell record, not a castable ritual.")
        restrictions = [int(row["raw_value"])
                        for row in ctx.reference_db.execute(
                            "SELECT raw_value FROM attributes_by_spell "
                            "WHERE spell_number=? AND attribute=278", (spell_id,))]
        if restrictions and ctx.nation_id not in restrictions:
            raise ToolError(
                f"{spell['name']} is restricted to nation id(s) "
                f"{restrictions}, not our nation {ctx.nation_id}.")
        effect_rows = list(ctx.reference_db.execute(
            "SELECT effect_number, CAST(ritual AS INTEGER) AS ritual, "
            "raw_argument FROM effects_spells WHERE record_id=?",
            (spell["effect_record_id"],)))
        if not effect_rows or not any(row["ritual"] == 1 for row in effect_rows):
            raise ToolError(
                f"{spell['name']} ({spell_id}) is not marked as a ritual in "
                "the game data; use set_battle_order for combat behavior.")

        school, level = int(spell["school"]), int(spell["researchlevel"])
        levels = ctx.view.parsed.research_levels
        if levels is None or not 0 <= school < len(levels):
            raise ToolError("current research levels could not be decoded.")
        if not T.spell_is_researched(
            levels,
            getattr(ctx.view.parsed, "learned_spell_ids", None),
            spell_id,
            school,
            level,
        ):
            detail = (
                "the individual Level-9 spell has not been learned"
                if level == 9 else f"only level {levels[school]} is researched"
            )
            raise ToolError(
                f"{spell['name']} requires {T.RESEARCH_SCHOOLS[school]} "
                f"{level}, but {detail}.")

        path_letters = "FAWESDNGBH"
        caster_paths = effective_magic_paths(ctx, commander)
        requirements: list[str] = []
        for path_col, level_col in (("path1", "pathlevel1"),
                                    ("path2", "pathlevel2")):
            path, required = int(spell[path_col]), int(spell[level_col])
            if path < 0 or required <= 0:
                continue
            if path >= len(path_letters):
                raise ToolError(
                    f"{spell['name']} has unknown path index {path} in the "
                    "reference data.")
            letter = path_letters[path]
            actual = int(caster_paths.get(letter, 0))
            if actual < required:
                raise ToolError(
                    f"{commander.name} has {letter}{actual}, but "
                    f"{spell['name']} requires {letter}{required}.")
            requirements.append(f"{letter}{required}")

        maximum_map_range = RR.spell_province_range(
            ctx.reference_db, spell_id)
        map_targeted = maximum_map_range is not None
        friendly_only = ctx.reference_db.execute(
            "SELECT 1 FROM attributes_by_spell WHERE spell_number=? "
            "AND attribute=738 LIMIT 1", (spell_id,)).fetchone() is not None
        target = None
        resolved_slot: int | None = None
        ritual_effects = {int(row["effect_number"]) for row in effect_rows
                          if row["ritual"] == 1}
        is_world_global = bool(ritual_effects & WORLD_GLOBAL_RITUAL_EFFECTS)
        is_global_selector = bool(
            ritual_effects & GLOBAL_SELECTOR_RITUAL_EFFECTS)
        accepts_strength_investment = bool(
            ritual_effects & GLOBAL_STRENGTH_RITUAL_EFFECTS)
        is_unit_target = spell_id in UNIT_TARGET_RITUAL_SPELL_IDS
        is_wish = spell_id in WISH_RITUAL_SPELL_IDS
        is_implicit_local = RR.spell_uses_implicit_local_target(
            ctx.reference_db, spell_id)
        duration_rate = RR.spell_duration_extension_months_per_gem(
            ctx.reference_db, spell_id)
        is_duration_extendable = duration_rate is not None
        no_selector_mode = RR.spell_no_selector_mode(
            ctx.reference_db, spell_id)
        gem_transport = bool(ritual_effects & GEM_TRANSPORT_RITUAL_EFFECTS)
        item_transport = spell_id in ITEM_TRANSPORT_RITUAL_SPELL_IDS
        unverified_item_transport = (
            ITEM_TRANSPORT_RITUAL_EFFECT in ritual_effects and not item_transport)
        if (not isinstance(extra_gems, int) or isinstance(extra_gems, bool)
                or extra_gems < 0):
            raise ToolError("extra_gems must be a non-negative integer.")
        if extra_gems and not (
                is_world_global or accepts_strength_investment
                or is_duration_extendable):
            raise ToolError(
                "extra_gems is supported only for world global enchantments "
                "and Dispel/Disenchantment, plus effect-82 persistent province "
                "enchantments; "
                "ordinary rituals use their fixed listed cost.")
        if monthly and (is_world_global or is_global_selector):
            kind = (
                "world global enchantments" if is_world_global
                else "active-global selector rituals"
            )
            raise ToolError(
                f"{kind} cannot be set to Monthly Ritual; cast it once and "
                "make a fresh strategic decision when another cast is needed.")
        if monthly and is_unit_target:
            raise ToolError(
                f"{spell['name']} changes its selected troop into a commander and "
                "cannot be set to Monthly Ritual.")
        if monthly and item_transport:
            raise ToolError(
                f"{spell['name']} consumes its selected treasury item and is "
                "controlled only as a one-shot ritual, not Monthly Ritual.")
        if monthly and is_wish:
            raise ToolError(
                "Wish is a one-shot free-text result ritual and cannot be set "
                "to Monthly Ritual; choose a fresh result for each cast.")
        if monthly and no_selector_mode == "caster":
            raise ToolError(
                f"{spell['name']} changes or enchants the caster and cannot be "
                "set to Monthly Ritual; cast it once and reassess next turn.")
        if monthly and no_selector_mode == "automatic_dead_hall_of_fame_hero":
            raise ToolError(
                f"{spell['name']} automatically chooses an eligible dead "
                "Hall-of-Fame hero and is controlled only as a one-shot "
                "ritual, not Monthly Ritual.")
        if target_global is not None and not is_global_selector:
            raise ToolError(
                f"{spell['name']} does not select an active global; omit "
                "target_global.")
        if is_unit_target and target_unit is None:
            raise ToolError(
                f"{spell['name']} targets an ordinary troop and requires "
                "target_unit. Call list_units with include_instances=true in "
                "the caster's province for instance ids.")
        if (target_unit is not None
                and (not isinstance(target_unit, int)
                     or isinstance(target_unit, bool)
                     or not 1 <= target_unit < 0xFFFF)):
            raise ToolError(
                "target_unit must be a real troop instance id from list_units "
                "with include_instances=true.")
        if target_unit is not None and not is_unit_target:
            raise ToolError(
                f"{spell['name']} does not use an ordinary troop selector; "
                "omit target_unit.")
        transport = gem_transport or item_transport
        if unverified_item_transport:
            raise ToolError(
                f"{spell['name']} shares the item-transport effect with Carrier "
                "Eagle, but its order has not been controlled; refusing to "
                "assume the same selector layout.")
        if transport and target_commander is None:
            raise ToolError(
                f"{spell['name']} delivers to a commander, so "
                "target_commander is required as well as target_province. "
                "Call list_commanders for ids.")
        if target_commander is not None and not transport:
            raise ToolError(
                f"{spell['name']} does not deliver to a commander; pass only "
                "target_province.")
        if item_transport and target_item is None:
            raise ToolError(
                f"{spell['name']} carries one unequipped treasury item, so "
                "target_item is required. Call get_item_treasury for ids.")
        if target_item is not None and not item_transport:
            raise ToolError(
                f"{spell['name']} does not use an item payload; omit "
                "target_item.")
        wish_selector_count = sum(value is not None for value in (
            wish_item, wish_unit, wish_nation, wish_result, wish_random))
        if is_wish and wish_selector_count != 1:
            raise ToolError(
                "Wish requires exactly one of wish_item, wish_unit, wish_nation, "
                "wish_result, or wish_random; arbitrary prose is not stored in "
                "the .2h.")
        if wish_selector_count and not is_wish:
            raise ToolError(
                f"{spell['name']} does not use Wish's result selector; omit "
                "all wish_* parameters.")
        if wish_result is not None and wish_result not in WISH_FIXED_RESULTS:
            raise ToolError(
                f"unsupported wish_result {wish_result!r}; choose one of "
                f"{sorted(WISH_FIXED_RESULTS)}.")
        selected_unit = None
        selected_unit_name = None
        recorded_target_province = target_province
        if is_unit_target:
            if target_province is not None or target_global is not None:
                raise ToolError(
                    f"{spell['name']} targets a troop in the caster's current "
                    "province; omit target_province and target_global.")
            assert target_unit is not None
            commander_instances = {
                candidate.unit_instance_id for candidate in commanders.values()
                if candidate.unit_instance_id is not None
            }
            candidates = [
                unit for unit in ctx.view.own_units()
                if unit.instance_id == target_unit and not unit.is_mount
                and unit.instance_id not in commander_instances
            ]
            if len(candidates) != 1:
                detail = "not found" if not candidates else "not unique"
                raise ToolError(
                    f"ordinary troop instance {target_unit} is {detail} among "
                    "our units. Call list_units with include_instances=true.")
            selected_unit = candidates[0]
            if (commander.province_id is None
                    or selected_unit.province_id != commander.province_id):
                raise ToolError(
                    f"troop instance {target_unit} is in province "
                    f"{selected_unit.province_id}, but {commander.name} is in "
                    f"{commander.province_id}; {spell['name']} selects a troop "
                    "in the caster's province.")
            unit_row = ctx.reference_db.execute(
                "SELECT name, inanimate FROM units WHERE id=?",
                (selected_unit.type_id,)).fetchone()
            selected_unit_name = unit_row["name"] if unit_row else None
            if (spell_id not in MINDLESS_ALLOWED_UNIT_TARGET_RITUAL_SPELL_IDS
                    and unit_row is not None
                    and int(unit_row["inanimate"] or 0)):
                raise ToolError(
                    f"{selected_unit_name or 'that unit'} is Inanimate and "
                    f"cannot receive {spell['name']}; select a non-Mindless "
                    "troop.")
        elif is_wish:
            if (target_province is not None or target_commander is not None
                    or target_unit is not None or target_item is not None
                    or target_global is not None):
                raise ToolError(
                    "Wish takes only its typed wish_* selector; "
                    "omit every province, commander, troop, transport-item, "
                    "and global selector.")
        elif is_implicit_local:
            if target_province is not None:
                raise ToolError(
                    f"{spell['name']} implicitly targets the caster's current "
                    "province; omit target_province.")
            if commander.province_id is None:
                raise ToolError(
                    f"{commander.name}'s current province is not decoded, so "
                    f"{spell['name']}'s implicit local target cannot be written.")
            recorded_target_province = commander.province_id
            target = ctx.view.province(commander.province_id)
            if target is None or not target.is_ours:
                raise ToolError(
                    f"{spell['name']} requires the caster's current friendly "
                    "province, but that province is not confirmed as ours.")
        elif transport:
            if target_province is None:
                raise ToolError(
                    f"{spell['name']} delivers to a commander and requires "
                    "target_province. Call find_province for an id.")
            target = ctx.view.province(target_province)
            if target is None:
                raise ToolError(
                    f"no province {target_province} in this game. Call "
                    "find_province to look one up.")
            if friendly_only and not target.is_ours:
                raise ToolError(
                    f"{spell['name']} can target only a friendly province, "
                    f"but {target.name} is {target.status}.")
        elif map_targeted:
            if target_province is None:
                raise ToolError(
                    f"{spell['name']} is province-targeted and requires "
                    "target_province. Call find_province for an id.")
            target = ctx.view.province(target_province)
            if target is None:
                raise ToolError(
                    f"no province {target_province} in this game. Call "
                    "find_province to look one up.")
            if friendly_only and not target.is_ours:
                raise ToolError(
                    f"{spell['name']} can target only a friendly province, "
                    f"but {target.name} is {target.status}.")
        elif is_global_selector:
            # All three name an active global by its chain slot. Slots are
            # stable identifiers, but a vacated one can be reused by the next
            # global cast, so resolve from identity here and at materialization.
            if target_global is None:
                raise ToolError(
                    f"{spell['name']} selects an active global enchantment and "
                    "requires target_global. Call get_global_enchantments for "
                    "the effect_id of each one currently up.")
            if target_province is not None:
                raise ToolError(
                    f"{spell['name']} targets a global enchantment, not a "
                    "province; pass target_global instead.")
            active = T.read_global_effects(ctx.view.data)
            chosen = [row for row in active if row.effect_id == target_global]
            if not chosen:
                available = sorted({row.effect_id for row in active})
                raise ToolError(
                    f"no active global enchantment with effect_id "
                    f"{target_global}. Currently active: {available or 'none'}.")
            resolved_slot = chosen[0].slot
        else:
            if not ritual_effects <= (
                VERIFIED_TARGETLESS_RITUAL_EFFECTS
                | WORLD_GLOBAL_RITUAL_EFFECTS
            ):
                raise ToolError(
                    f"{spell['name']} has no province-range attribute, but its "
                    f"ritual effect {sorted(ritual_effects)} has no controlled-save "
                    "target layout. Only the Distill Gold conversion family and "
                    "world global enchantments are verified targetless; "
                    "refusing to guess whether this spell selects a unit or "
                    "another object.")
            if target_province is not None:
                raise ToolError(
                    f"{spell['name']} takes no target_province: it is either a "
                    "global enchantment or in the verified conversion family.")

        target_range = None
        if map_targeted:
            try:
                checked = RR.validate_ritual_target(
                    ctx.reference_db, spell_id, ctx.view.parsed.provinces,
                    commander.province_id, recorded_target_province,
                    ctx.nation_id)
            except ValueError as exc:
                raise ToolError(f"{spell['name']}: {exc}")
            assert checked is not None
            target_range = {
                "distance": checked.distance,
                "maximum": checked.maximum,
            }
        try:
            RR.validate_ritual_source(
                ctx.reference_db, spell_id, ctx.view.parsed.provinces,
                commander.province_id, commander.type_id, caster_item_ids)
        except ValueError as exc:
            raise ToolError(f"{spell['name']}: {exc}")

        warnings: list[str] = []
        if no_selector_mode == "automatic_dead_hall_of_fame_hero":
            warnings.append(
                "Ritual of Rebirth has no target selector. Dominions "
                "automatically chooses an eligible dead hero who remains in "
                "the Hall of Fame and returns that hero as a Mummy. The choice "
                "rule when several eligible heroes are dead has not been "
                "observed, so this order cannot promise which one will return."
            )
        if monthly:
            warnings.append(
                "Monthly Ritual reserves this cast only. It repeats next turn "
                "only if the new turn can afford it; otherwise the client "
                "returns the caster to Defend."
            )
        province = (ctx.view.province(commander.province_id)
                    if commander.province_id is not None else None)
        if province is None:
            warnings.append(
                "caster location is not decoded, so the required laboratory "
                "could not be checked")
        elif not province.has_laboratory:
            raise ToolError(
                f"{commander.name} is in {province.name}, which has no laboratory.")

        gem_path = int(spell["path1"])
        base_gem_cost = int(spell["gemcost"] or 0)
        gem_cost = base_gem_cost + extra_gems
        if gem_cost > 1_000_000:
            raise ToolError(f"implausible total ritual gem cost {gem_cost}.")
        if not 0 <= gem_path <= 8:
            raise ToolError(
                f"{spell['name']} has no valid primary gem path to reserve.")
        recipient = None
        if transport:
            recipient = commanders.get(target_commander)
            if recipient is None:
                raise ToolError(
                    f"commander {target_commander} is not one of ours. Ours "
                    f"are: {sorted(commanders)}.")
            if (recipient.province_id is not None
                    and target_province is not None
                    and recipient.province_id != target_province):
                raise ToolError(
                    f"{recipient.name} is in province {recipient.province_id}, "
                    f"not {target_province}. The client picks a province and "
                    "then a commander in it, so the two must agree.")
            # Effect 160's payload is whatever the caster happens to be
            # carrying. Effect 161 instead names one treasury item at +132.
            # nothing in the order says WHICH gems travel. Every observed cast
            # had a single type; a mixed stock leaves the choice undetermined,
            # and over the capacity the remainder stays behind unpredictably.
            data = ctx.h2_path.read_bytes()
            if gem_transport:
                caster_block = O.find_order_blocks(data).get(commander_id)
                if caster_block is None:
                    raise ToolError(
                        f"{commander.name} has no order block in the .2h, so the "
                        "gems they carry cannot be read.")
                carried = {path: count for path, count
                           in O.read_carried_gems(
                               data, caster_block.name_end).items() if count}
                capacity = max((int(row["raw_argument"] or 0)
                                for row in effect_rows
                                if int(row["effect_number"])
                                in GEM_TRANSPORT_RITUAL_EFFECTS),
                               default=0)
                if not carried:
                    raise ToolError(
                        f"{commander.name} carries no gems, so {spell['name']} "
                        "would send nothing. Use set_carried_gems to load "
                        "exactly what should travel.")
            # A mixed stock is fine as long as it ALL fits: nothing has to be
            # chosen, so nothing is undetermined. Only an overfull caster is
            # refused, because then the spell takes a subset and the order
            # records no rule for which. 5 fire + 3 water + 2 air is ten gems
            # and travels whole.
                total_carried = sum(carried.values())
                if capacity and total_carried > capacity:
                    raise ToolError(
                        f"{commander.name} carries {total_carried} gems "
                        f"({carried}) but {spell['name']} sends at most "
                        f"{capacity}. Which of them would travel is not recorded "
                        "anywhere in the order, so load no more than the capacity.")

        selected_item = None
        selected_item_name = None
        if item_transport:
            if (not isinstance(target_item, int) or isinstance(target_item, bool)
                    or not 1 <= target_item <= 1500):
                raise ToolError(
                    "target_item must be an item id from get_item_treasury.")
            _validate_item_allocation(
                ctx,
                ritual_override=(commander_id, int(target_item)),
            )
            selected_item = int(target_item)
            item_row = ctx.reference_db.execute(
                "SELECT name FROM items WHERE id=?", (selected_item,)
            ).fetchone()
            if item_row is None:
                raise ToolError(
                    f"item {selected_item} is absent from the reference data.")
            selected_item_name = item_row["name"]

        selected_wish_item = None
        selected_wish_item_name = None
        selected_wish_unit = None
        selected_wish_unit_name = None
        selected_wish_nation = None
        selected_wish_nation_name = None
        selected_wish_result = None
        selected_wish = None
        if is_wish:
            for value, label in (
                (wish_item, "wish_item"), (wish_unit, "wish_unit"),
                (wish_nation, "wish_nation"),
            ):
                if (value is not None and
                        (not isinstance(value, int) or isinstance(value, bool))):
                    raise ToolError(f"{label} must be an integer id.")
            caster_unit = next(
                (unit for unit in ctx.view.own_units()
                 if unit.instance_id == commander.unit_instance_id), None)
            worn_cursed_item = any(
                ctx.reference_db.execute(
                    "SELECT coalesce(cursed, 0) FROM items WHERE id=?", (item_id,)
                ).fetchone()[0]
                for item_id in caster_item_ids
                if item_id is not None
            )
            caster_is_cursed = bool(
                (caster_unit is not None and caster_unit.afflictions & 2)
                or worn_cursed_item)
            try:
                selected_wish = WISH.build_typed(
                    ctx.reference_db,
                    result=wish_result,
                    item_id=wish_item,
                    unit_id=wish_unit,
                    nation_id=wish_nation,
                    random_request=wish_random,
                    item_states=getattr(ctx.view.parsed, "item_states", None),
                    caster_is_cursed=caster_is_cursed,
                )
            except ValueError as exc:
                raise ToolError(str(exc))
            selected_wish_result = selected_wish.result
            kind = selected_wish.spec.payload_kind
            if kind == "item":
                selected_wish_item = selected_wish.payload
                row = ctx.reference_db.execute(
                    "SELECT name FROM items WHERE id=?", (selected_wish_item,)
                ).fetchone()
                selected_wish_item_name = row["name"] if row else None
            elif kind in {"unit", "optional_unit"} and selected_wish.payload >= 0:
                selected_wish_unit = selected_wish.payload
                row = ctx.reference_db.execute(
                    "SELECT name FROM units WHERE id=?", (selected_wish_unit,)
                ).fetchone()
                selected_wish_unit_name = row["name"] if row else None
            elif kind == "nation":
                selected_wish_nation = selected_wish.payload
                active_nations = {ctx.view.nation_id} | {
                    relation.nation_id
                    for relation in ctx.view.diplomatic_relations()
                    if not relation.defeated
                }
                if selected_wish_nation not in active_nations:
                    raise ToolError(
                        f"nation {selected_wish_nation} is not an active nation "
                        "in this game. Call get_diplomatic_relations for ids.")
                row = ctx.reference_db.execute(
                    "SELECT name FROM nations WHERE id=?", (selected_wish_nation,)
                ).fetchone()
                selected_wish_nation_name = row["name"] if row else None

        try:
            row_id = M.record_ritual(
                ctx.game_db, ctx.game_id, ctx.turn, commander_id, spell_id,
                gem_path, gem_cost, commander_name=commander.name,
                spell_name=spell["name"],
                target_province=recorded_target_province,
                target_commander_id=(recipient.commander_id
                                     if recipient is not None else None),
                target_unit_instance_id=(
                    selected_unit.instance_id
                    if selected_unit is not None else None),
                target_item_id=selected_item,
                wish_item_id=selected_wish_item,
                wish_unit_id=selected_wish_unit,
                wish_nation_id=selected_wish_nation,
                wish_result=selected_wish_result,
                target_global_effect_id=target_global,
                monthly=monthly, rationale=rationale.strip())
        except ValueError as exc:
            raise ToolError(str(exc))
        return {
            "recorded": row_id,
            "commander": commander.name,
            "spell_id": spell_id,
            "spell": spell["name"],
            "requirements": requirements,
            "gem_path": O.MAGIC_PATH_NAMES[gem_path],
            "gem_cost": gem_cost,
            "base_gem_cost": base_gem_cost,
            "extra_gems": extra_gems,
            "target_province": recorded_target_province,
            "target_name": target.name if target is not None else None,
            "target_commander": (recipient.commander_id
                                 if recipient is not None else None),
            "target_commander_name": (recipient.name
                                      if recipient is not None else None),
            "target_unit": (selected_unit.instance_id
                            if selected_unit is not None else None),
            "target_unit_type_id": (selected_unit.type_id
                                    if selected_unit is not None else None),
            "target_unit_name": selected_unit_name,
            "target_item": selected_item,
            "target_item_name": selected_item_name,
            "wish_item": selected_wish_item,
            "wish_item_name": selected_wish_item_name,
            "wish_unit": selected_wish_unit,
            "wish_unit_name": selected_wish_unit_name,
            "wish_nation": selected_wish_nation,
            "wish_nation_name": selected_wish_nation_name,
            "wish_result": selected_wish_result,
            "wish_outcome": selected_wish.as_dict() if selected_wish else None,
            "target_global": target_global,
            "target_global_slot": resolved_slot,
            "target_mode": no_selector_mode,
            "target_range": target_range,
            "monthly": monthly,
            "overcast": (
                extra_gems
                + 5 * (
                    int(caster_paths.get(path_letters[gem_path], 0))
                    - int(spell["pathlevel1"] or 0)
                )
                if is_world_global or accepts_strength_investment else None
            ),
            "duration_extension_gems": (
                extra_gems if is_duration_extendable else None
            ),
            "duration_extension_months": (
                extra_gems * duration_rate
                if is_duration_extendable else None
            ),
            "warnings": warnings or None,
            "note": "call materialize_orders to write this ritual to the .2h",
        }

    @reg.tool(
        "materialize_orders",
        "Write the recorded orders into the .2h file the game reads. Runs as "
        "a dry run and reports what it would do unless confirm is true.",
        Param("confirm", "boolean",
              "true to actually write the file; omit to preview",
              required=False, default=False),
        writes=True)
    def materialize_orders(ctx: ToolContext, confirm: bool = False) -> dict:
        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError(
                "no .2h file to write. The game writes it on save; there is "
                "nothing to materialise into.")
        was_submitted = TC.file_is_submitted(ctx.h2_path)
        result = M.materialize(ctx.game_db, ctx.game_id, ctx.turn,
                               ctx.h2_path, dry_run=not confirm,
                               reference_conn=ctx.reference_db)
        reopened = bool(
            confirm and result.path is not None and was_submitted
            and not TC.file_is_submitted(ctx.h2_path)
        )
        if reopened:
            ctx.game_db.execute(
                "UPDATE agent_turn_completion SET submitted_at=NULL "
                "WHERE game_id=? AND turn=?",
                (ctx.game_id, ctx.turn),
            )
            ctx.game_db.commit()
        return {
            "dry_run": not confirm,
            "written": result.written,
            # Surfaced rather than summarised: a skipped order means the turn
            # will resolve without it, and that must not be a quiet detail.
            "skipped": result.skipped,
            "aborted": result.aborted,
            "changed_bytes": result.changed_bytes,
            "reopened_submitted_turn": reopened,
            "path": str(result.path) if result.path else None,
            "note": (
                "transaction aborted — no orders were written; resolve every "
                "skipped item and retry" if result.aborted else
                "preview only — nothing was written. Call again with "
                "confirm=true to write." if not confirm else
                "written; the earlier submission was reopened as unfinished. "
                "Call complete_turn and submit_turn again after review."
                if reopened else
                "written; reload the turn in game to verify"),
        }

    @reg.tool(
        "complete_turn",
        "Mark this turn ready for autonomous handoff. This succeeds only "
        "after every current commander has an explicit reasoned strategic "
        "order and materialize_orders(confirm=true) wrote the live .2h. "
        "Changing a decision or the file makes the handoff stale. Call "
        "submit_turn afterwards to set the game's local End Turn flags.",
        Param("summary", "string",
              "concise strategic summary of what this turn is intended to accomplish"),
        Param("outstanding_risks", "string",
              "known uncertainty, contingency, or 'none identified'"),
        writes=True,
    )
    def complete_turn(
        ctx: ToolContext, summary: str, outstanding_risks: str
    ) -> dict:
        if not summary.strip() or not outstanding_risks.strip():
            raise ToolError("summary and outstanding_risks must not be empty.")
        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError("the current .2h is unavailable.")
        commanders = {
            int(commander.commander_id): commander.name
            for commander in ctx.view.own_commanders(ctx.h2_path)
        }
        ordered = {
            int(row["commander_id"])
            for row in ctx.game_db.execute(
                "SELECT commander_id FROM current_orders "
                "WHERE game_id=? AND turn=?", (ctx.game_id, ctx.turn)
            )
        }
        missing = [
            {"commander_id": commander_id, "name": commanders[commander_id]}
            for commander_id in sorted(set(commanders) - ordered)
        ]
        if missing:
            raise ToolError(
                f"{len(missing)} commander(s) have no explicit strategic "
                f"decision this turn: {missing}. Use record_orders to assign "
                "even intentional Defend/Research orders with a rationale."
            )
        if not M.matches_last_written(ctx.h2_path):
            raise ToolError(
                "the live .2h is not the assistant's last materialized output. "
                "Call materialize_orders to preview, resolve every skipped "
                "entry, then call it with confirm=true."
            )
        fingerprint = TC.decision_fingerprint(
            ctx.game_db, ctx.game_id, ctx.turn)
        digest = TC.file_sha256(ctx.h2_path)
        ctx.game_db.execute(
            "INSERT INTO agent_turn_completion(game_id,turn,summary,"
            "outstanding_risks,h2_sha256,decision_fingerprint) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(game_id,turn) DO UPDATE SET "
            "summary=excluded.summary,outstanding_risks=excluded.outstanding_risks,"
            "h2_sha256=excluded.h2_sha256,"
            "decision_fingerprint=excluded.decision_fingerprint,"
            "completed_at=datetime('now')",
            (ctx.game_id, ctx.turn, summary.strip(), outstanding_risks.strip(),
             digest, fingerprint),
        )
        if not TC.file_is_submitted(ctx.h2_path):
            ctx.game_db.execute(
                "UPDATE agent_turn_completion SET submitted_at=NULL "
                "WHERE game_id=? AND turn=?",
                (ctx.game_id, ctx.turn),
            )
        ctx.game_db.commit()
        return {
            "complete": True,
            "turn": ctx.turn,
            "commanders_explicitly_decided": len(commanders),
            "summary": summary.strip(),
            "outstanding_risks": outstanding_risks.strip(),
            "h2_sha256": digest,
            "note": (
                "the turn is ready but not yet submitted. Call submit_turn "
                "with confirm=true to mark this revision ready; any later "
                "decision or file change invalidates this completion"
            ),
        }

    @reg.tool(
        "submit_turn",
        "Perform the local equivalent of End Turn by marking the completed "
        ".2h as submitted. Requires a still-valid complete_turn handshake and "
        "confirm=true. This changes only the two decoded submission flags; it "
        "does not host the game or transmit the file to a network server. A "
        "submitted turn can still be revised and resubmitted until hosting.",
        Param("confirm", "boolean",
              "must be true because this marks the current revision ready"),
        writes=True,
    )
    def submit_turn(ctx: ToolContext, confirm: bool) -> dict:
        if not confirm:
            raise ToolError(
                "submit_turn marks the current revision ready and requires "
                "confirm=true. Review the completion summary and materialized "
                "orders before retrying.")
        if ctx.h2_path is None or not ctx.h2_path.exists():
            raise ToolError("the current .2h is unavailable.")
        status = TC.completion_status(
            ctx.game_db, ctx.game_id, ctx.turn, ctx.h2_path)
        if not status.complete:
            raise ToolError(
                f"the turn is not ready for submission: {status.reason}. "
                "Finish every decision, confirm materialization, then call "
                "complete_turn before retrying submit_turn.")
        if status.submitted:
            ctx.game_db.execute(
                "UPDATE agent_turn_completion SET submitted_at="
                "COALESCE(submitted_at,datetime('now')) "
                "WHERE game_id=? AND turn=?",
                (ctx.game_id, ctx.turn),
            )
            ctx.game_db.commit()
            return {
                "submitted": True,
                "already_submitted": True,
                "turn": ctx.turn,
                "changed_offsets": [],
                "h2_sha256": TC.file_sha256(ctx.h2_path),
                "note": "the local .2h was already marked submitted",
            }
        try:
            result = M.submit_turn_file(ctx.h2_path)
        except H2.SubmissionStateError as exc:
            raise ToolError(f"cannot submit this .2h safely: {exc}") from exc
        ctx.game_db.execute(
            "UPDATE agent_turn_completion SET h2_sha256=?,"
            "submitted_at=datetime('now') WHERE game_id=? AND turn=?",
            (result.h2_sha256, ctx.game_id, ctx.turn),
        )
        ctx.game_db.commit()
        return {
            "submitted": True,
            "already_submitted": result.already_submitted,
            "turn": ctx.turn,
            "changed_offsets": list(result.changed_offsets),
            "h2_sha256": result.h2_sha256,
            "trailer_preserved": True,
            "path": str(result.path),
            "note": (
                "the local .2h is marked finished. A hotseat host can read it "
                "directly; a network game still needs the file transmitted by "
                "the game client or another upload mechanism. Until hosting, "
                "new decisions can be materialized and submitted again"
            ),
        }

    # -- memory ----------------------------------------------------------

    @reg.tool(
        "write_note",
        "Save a note for this game — a plan, a threat, something to remember "
        "next turn. Notes persist across turns and are visible via "
        "read_scratchpad.",
        Param("note", "string", "the note"),
        Param("tag", "string", "a short category, e.g. 'plan' or 'threat'",
              required=False),
        writes=True)
    def write_note(ctx: ToolContext, note: str, tag: str | None = None) -> dict:
        if not note.strip():
            raise ToolError("note must not be empty.")
        cur = ctx.game_db.execute(
            "INSERT INTO scratchpad(game_id, turn, tag, note) VALUES(?,?,?,?)",
            (ctx.game_id, ctx.turn, tag, note.strip()))
        ctx.game_db.commit()
        return {"saved": cur.lastrowid, "turn": ctx.turn, "tag": tag}

    @reg.tool(
        "write_lesson",
        "Record something learned about Dominions itself — a rule, a cost, a "
        "mechanic. These persist across games. Say where it came from.",
        Param("topic", "string", "short topic, e.g. 'siege' or 'ma_marignon'"),
        Param("lesson", "string", "what was learned"),
        Param("evidence", "string",
              "where this came from: 'experiment', 'game-screen', 'player', "
              "or a specific observation",
              hint="A lesson with no evidence is a guess that will later be "
                   "mistaken for knowledge."),
        writes=True)
    def write_lesson(ctx: ToolContext, topic: str, lesson: str,
                     evidence: str) -> dict:
        if not lesson.strip() or not evidence.strip():
            raise ToolError("lesson and evidence must both be non-empty.")
        cur = ctx.game_db.execute(
            "INSERT INTO lessons(topic, lesson, evidence) VALUES(?,?,?)",
            (topic.strip(), lesson.strip(), evidence.strip()))
        ctx.game_db.commit()
        return {"saved": cur.lastrowid, "topic": topic}

    @reg.tool(
        "record_gap",
        "Record something we needed and could not determine, and what would "
        "settle it. Use this instead of guessing.",
        Param("subject", "string", "what was needed"),
        Param("reason", "string", "why it could not be determined, and the "
                                  "observation that would settle it"),
        writes=True)
    def record_gap(ctx: ToolContext, subject: str, reason: str) -> dict:
        ctx.game_db.execute(
            "INSERT OR REPLACE INTO capability_gaps(subject, reason) "
            "VALUES(?,?)", (subject.strip(), reason.strip()))
        ctx.game_db.commit()
        return {"recorded": subject}

    return reg


def _effective_equipment(ctx: ToolContext, commander_id: int) -> dict[str, int]:
    """Equipment after applying this turn's pending slot intents.

    Spell eligibility and forge checks must describe the turn we are building,
    not merely the untouched source `.2h`.  In particular, Holy-granting items
    can make an otherwise grey Divine spell scriptable this turn.
    """
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
    return equipped


def _effective_equipment_rows(ctx: ToolContext, commander_id: int) -> list:
    rows = []
    for item_id in _effective_equipment(ctx, commander_id).values():
        item = ctx.reference_db.execute(
            "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if item is not None:
            rows.append(item)
    return rows


_NO_RITUAL_OVERRIDE = object()


def _validate_item_allocation(
    ctx: ToolContext,
    *,
    equipment_overrides: dict[tuple[int, str], int] | None = None,
    ritual_override: tuple[int, int | None] | object = _NO_RITUAL_OVERRIDE,
) -> Counter:
    """Validate the final item plan as a multiset, not a set of item ids.

    Item ids describe fungible item types, so duplicate copies are legal. The
    invariant is simply that final worn slots plus item-transport payloads do
    not consume more copies than the pristine turn owns. This catches two
    pending equips racing for one copy while still allowing two real copies or
    an atomic clear-and-re-equip transfer.
    """
    if ctx.h2_path is None or not ctx.h2_path.exists():
        raise ToolError("the current .2h is unavailable; item ownership is unknown.")
    source = ctx.h2_path.with_suffix(M.BASE_SUFFIX)
    if not source.exists():
        source = ctx.h2_path
    data = source.read_bytes()
    try:
        base_blocks = O.find_order_blocks(data)
        stash = H2.read_item_stash(data)
    except (ValueError, O.OrderTableNotLocated, H2.QueueWriteRefused) as exc:
        raise ToolError(f"the current item inventory could not be decoded: {exc}")

    slots: dict[tuple[int, str], int] = {}
    total = Counter(stash)
    own_ids = {
        commander.commander_id
        for commander in ctx.view.own_commanders(ctx.h2_path)
    }
    for commander_id in own_ids:
        block = base_blocks.get(commander_id)
        if block is None:
            continue
        for slot, item_id in O.read_equipment(data, block.name_end).items():
            slots[(commander_id, slot)] = item_id
            total[item_id] += 1
    for pending in ctx.game_db.execute(
            "SELECT commander_id, slot, item_id FROM "
            "current_equipment_intent WHERE game_id=? AND turn=?",
            (ctx.game_id, ctx.turn)):
        key = (int(pending["commander_id"]), str(pending["slot"]))
        item_id = int(pending["item_id"])
        if item_id:
            slots[key] = item_id
        else:
            slots.pop(key, None)
    for key, item_id in (equipment_overrides or {}).items():
        if item_id:
            slots[key] = item_id
        else:
            slots.pop(key, None)

    consumed = Counter(slots.values())
    overridden_caster = None
    override_item = None
    if ritual_override is not _NO_RITUAL_OVERRIDE:
        overridden_caster, override_item = ritual_override
    for pending in ctx.game_db.execute(
            "SELECT o.commander_id, r.target_item_id FROM current_orders o "
            "JOIN ritual_intent r ON r.order_intent_id=o.id "
            "WHERE o.game_id=? AND o.turn=? "
            "AND r.target_item_id IS NOT NULL",
            (ctx.game_id, ctx.turn)):
        if (overridden_caster is not None
                and int(pending["commander_id"]) == overridden_caster):
            continue
        consumed[int(pending["target_item_id"])] += 1
    if override_item is not None:
        consumed[int(override_item)] += 1

    shortages = {
        item_id: needed - total[item_id]
        for item_id, needed in consumed.items()
        if needed > total[item_id]
    }
    if shortages:
        rendered = []
        for item_id, short in sorted(shortages.items()):
            item = ctx.reference_db.execute(
                "SELECT name FROM items WHERE id=?", (item_id,)).fetchone()
            name = item["name"] if item else f"item {item_id}"
            rendered.append(
                f"{name} ({item_id}) needs {consumed[item_id]} copy/copies, "
                f"owns {total[item_id]} (short {short})")
        hint = (
            " An existing copy is already worn or reserved; use "
            "transfer_item for a worn-item move."
            if any(total[item_id] for item_id in shortages)
            else ""
        )
        raise ToolError(
            "item allocation exceeds owned/unequipped treasury availability; "
            "we do not own enough copies: "
            + "; ".join(rendered) + hint)
    return total - consumed


def _level_nine_spell(ctx: ToolContext, value: object,
                      position: int) -> dict[str, object]:
    """Resolve an exact Level 9 spell target without guessing from a fragment."""
    if isinstance(value, bool):
        rows = []
    elif isinstance(value, int):
        rows = list(ctx.reference_db.execute(
            "SELECT id, name, school, researchlevel FROM spells WHERE id=?",
            (value,)))
    elif isinstance(value, str):
        rows = list(ctx.reference_db.execute(
            "SELECT id, name, school, researchlevel FROM spells "
            "WHERE lower(name)=lower(?)", (value.strip(),)))
    else:
        rows = []
    if not rows:
        raise ToolError(
            f"research entry {position} is neither a school name nor an exact "
            f"spell id/name: {value!r}. Call lookup_spell for a verified id.")
    if len(rows) > 1:
        raise ToolError(
            f"research entry {position} names multiple spells; use one of the "
            f"exact ids {[row['id'] for row in rows]}.")
    row = rows[0]
    path_row = ctx.reference_db.execute(
        "SELECT path1, pathlevel1 FROM spells WHERE id=?", (row["id"],)).fetchone()
    if (path_row is None or int(path_row["path1"]) < 0
            or int(path_row["pathlevel1"] or 0) <= 0):
        raise ToolError(
            f"research entry {position} is {row['name']}, an internal/event "
            "spell record with no caster path requirement.")
    restrictions = [int(found["raw_value"])
                    for found in ctx.reference_db.execute(
                        "SELECT raw_value FROM attributes_by_spell "
                        "WHERE spell_number=? AND attribute=278", (row["id"],))]
    if restrictions and ctx.nation_id not in restrictions:
        raise ToolError(
            f"research entry {position} is {row['name']}, restricted to "
            f"nation id(s) {restrictions}, not our nation {ctx.nation_id}.")
    school = row["school"]
    if (row["researchlevel"] != 9 or not isinstance(school, int)
            or not 0 <= school < len(T.RESEARCH_SCHOOLS)):
        raise ToolError(
            f"research entry {position} is {row['name']} (id {row['id']}), "
            f"which is not a Level 9 research spell. Use school names for "
            "ordinary Levels 1-8.")
    return dict(row)


def _script_spell(ctx: ToolContext, commander, value: object,
                  position: int) -> dict[str, object]:
    """Resolve and conservatively validate one scripted combat spell."""
    if isinstance(value, bool):
        rows = []
    elif isinstance(value, int):
        rows = list(ctx.reference_db.execute(
            "SELECT * FROM spells WHERE id=?", (value,)))
    elif isinstance(value, str):
        rows = list(ctx.reference_db.execute(
            "SELECT * FROM spells WHERE lower(name)=lower(?)",
            (value.strip(),)))
    else:
        rows = []
    if not rows:
        raise ToolError(
            f"battle-script round {position + 1} is not a verified fixed "
            f"choice or exact spell id/name: {value!r}. Call lookup_spell.")
    if len(rows) > 1:
        raise ToolError(
            f"battle-script round {position + 1} names multiple spells; use "
            f"one of the exact ids {[row['id'] for row in rows]}.")
    spell = rows[0]
    if int(spell["path1"]) < 0 or int(spell["pathlevel1"] or 0) <= 0:
        raise ToolError(
            f"{spell['name']} has no caster path requirement and is an "
            "internal/event spell record, not a scriptable choice.")
    restrictions = [int(row["raw_value"])
                    for row in ctx.reference_db.execute(
                        "SELECT raw_value FROM attributes_by_spell "
                        "WHERE spell_number=? AND attribute=278", (spell["id"],))]
    if restrictions and ctx.nation_id not in restrictions:
        raise ToolError(
            f"{spell['name']} is restricted to nation id(s) {restrictions}, "
            f"not our nation {ctx.nation_id}.")
    effects = list(ctx.reference_db.execute(
        "SELECT CAST(ritual AS INTEGER) AS ritual FROM effects_spells "
        "WHERE record_id=?", (spell["effect_record_id"],)))
    if not effects or any(row["ritual"] == 1 for row in effects):
        raise ToolError(
            f"{spell['name']} ({spell['id']}) is a ritual, not a combat spell.")

    school, level = int(spell["school"]), int(spell["researchlevel"])
    if school == DV.DIVINE_SCHOOL:
        pretender = next(
            (candidate for candidate in ctx.view.own_commanders(ctx.h2_path)
             if candidate.is_pretender),
            None,
        )
        if pretender is None:
            raise ToolError(
                "our pretender could not be identified, and the nation's "
                "Divine spell list depends on its magic path.")
        divine = {
            candidate.spell_id: candidate
            for candidate in DV.divine_spells(
                ctx.reference_db,
                ctx.nation_id,
                int(commander.paths.get("H", 0) or 0),
                pretender.paths,
                other_paths=commander.paths,
                item_rows=_effective_equipment_rows(
                    ctx, commander.commander_id),
            )
        }
        eligible = divine.get(int(spell["id"]))
        if eligible is None:
            raise ToolError(
                f"{spell['name']} is not in our nation's Divine spell list; "
                "that list is selected by the pretender's magic path.")
        if not eligible.castable:
            raise ToolError(
                f"{commander.name} cannot currently cast {spell['name']}; "
                f"it requires Holy {eligible.holy_level}, including any "
                "pending worn-item Holy bonus.")
        return dict(spell)

    levels = ctx.view.parsed.research_levels
    if levels is None or not 0 <= school < len(levels):
        raise ToolError(
            f"{spell['name']} has no ordinary researched combat school.")
    if not T.spell_is_researched(
        levels,
        getattr(ctx.view.parsed, "learned_spell_ids", None),
        int(spell["id"]),
        school,
        level,
    ):
        detail = (
            "the individual Level-9 spell has not been learned"
            if level == 9 else f"only level {levels[school]} is researched"
        )
        raise ToolError(
            f"{spell['name']} requires {T.RESEARCH_SCHOOLS[school]} {level}, "
            f"but {detail}.")

    path_letters = "FAWESDNGBH"
    caster_paths = effective_magic_paths(ctx, commander)
    for path_col, level_col in (("path1", "pathlevel1"),
                                ("path2", "pathlevel2")):
        path, required = int(spell[path_col]), int(spell[level_col])
        if path < 0 or required <= 0:
            continue
        if path >= len(path_letters):
            raise ToolError(
                f"{spell['name']} has unknown path index {path}.")
        letter = path_letters[path]
        actual = int(caster_paths.get(letter, 0))
        if actual < required:
            raise ToolError(
                f"{commander.name} has {letter}{actual}, but "
                f"{spell['name']} requires {letter}{required}.")
    return dict(spell)


def _recruitable_here(ctx: ToolContext, province_id: int) -> list[dict]:
    from dom6_assistant.agent import read_tools
    return read_tools._recruitable(ctx, province_id)["recruitable"]


def _check_squad(ctx: ToolContext, commander_id: int,
                 squad: int | None) -> None:
    """Refuse a squad slot the commander does not actually lead.

    Checked here, at record time, rather than only when the file is written.
    A decision rejected when it is made is one the model can correct while it
    still remembers why it made it; the same rejection surfacing later as a
    skipped row in a materialisation is one nobody reads.

    Writing to an unused slot is not harmless — the five slots are contiguous
    arrays, so a stance for a squad that does not exist lands in the space the
    next field occupies.
    """
    if squad is None:
        return
    if not 0 <= squad < 5:
        raise ToolError(f"squad must be 0-4, got {squad}.")
    if ctx.h2_path is None or not ctx.h2_path.exists():
        return
    data = ctx.h2_path.read_bytes()
    slots = O.read_squad_orders(
        data, O.find_order_blocks(data)[commander_id].name_end)
    occupied = [row["slot"] for row in slots]
    pending = ctx.game_db.execute(
        "SELECT 1 FROM current_squad_creation_intent WHERE game_id=? "
        "AND turn=? AND target_commander_id=? AND target_squad=? LIMIT 1",
        (ctx.game_id, ctx.turn, commander_id, squad)).fetchone()
    if pending is not None:
        return
    if squad not in occupied:
        raise ToolError(
            f"that commander leads {len(slots)} squad(s), but slot {squad} is "
            f"empty. Occupied slots: {occupied}. Omit squad "
            "entirely to set the commander's own battle order.")


def register_all(reg: ToolRegistry | None = None) -> ToolRegistry:
    """The complete tool surface: read tools then write tools."""
    from dom6_assistant.agent import read_tools
    reg = reg or ToolRegistry()
    read_tools.register(reg)
    register(reg)
    return reg
