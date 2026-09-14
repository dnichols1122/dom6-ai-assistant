"""A pre-game tool session for designing and saving a pretender god.

This is intentionally separate from the turn registry.  There is no player
``.trn`` before nation selection, and accepting a nation id in ordinary turn
tools would weaken the visibility boundary.  A pretender session fixes one
nation at open time and exposes only static reference data plus ``newlords``.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dom6_assistant.agent.registry import Param, ToolError, ToolRegistry
from dom6_assistant.file_reader.formats.pretender import Awakening, Pretender
from dom6_assistant.reference.pretender_design import (
    BLESSINGS,
    scale_limits,
    PretenderDesignError,
    available_chassis,
    blessing_points,
    chassis_cost,
    cost_tables,
    design_cost,
    get_chassis,
    validate_blessings,
)
from dom6_assistant.paths import default_newlords_dir
from dom6_assistant.reference import unit_profile
from dom6_assistant.wiki.index import DEFAULT_INDEX_DB, resolve_page_focus

DEFAULT_REFERENCE_DB = Path("knowledge/reference/reference.sqlite3")
DEFAULT_NEWLORDS_DIR = default_newlords_dir()

_AWAKENING_BY_NAME = {
    "awake": Awakening.AWAKE,
    "dormant": Awakening.DORMANT,
    "imprisoned": Awakening.IMPRISONED,
}


@dataclass
class PretenderContext:
    reference_db: sqlite3.Connection
    nation_id: int
    output_dir: Path
    wiki_db: Path = DEFAULT_INDEX_DB


@dataclass
class PretenderSession:
    ctx: PretenderContext
    registry: ToolRegistry

    @property
    def turn(self) -> int:
        return 0

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.registry.call(self.ctx, name, args)

    def close(self) -> None:
        self.ctx.reference_db.close()


def _json_object(value: Any, field: str) -> dict[str, int]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ToolError(f"{field} must be a JSON object: {exc}") from exc
    else:
        decoded = value
    if not isinstance(decoded, dict) or any(
        not isinstance(key, str) or isinstance(number, bool) or not isinstance(number, int)
        for key, number in decoded.items()
    ):
        raise ToolError(f"{field} must be a JSON object of integer values")
    return decoded


def _json_ids(value: Any, field: str) -> tuple[int, ...]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ToolError(f"{field} must be a JSON array: {exc}") from exc
    else:
        decoded = value
    if not isinstance(decoded, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in decoded
    ):
        raise ToolError(f"{field} must be a JSON array of integer blessing ids")
    return tuple(decoded)


def _awakening(value: str) -> Awakening:
    try:
        return _AWAKENING_BY_NAME[value]
    except KeyError as exc:
        raise ToolError(f"unknown awakening {value!r}") from exc


def _unit_row(ctx: PretenderContext, chassis_id: int) -> sqlite3.Row:
    row = ctx.reference_db.execute(
        "SELECT * FROM units WHERE id=?", (int(chassis_id),)
    ).fetchone()
    if row is None:
        raise ToolError(f"no unit record for chassis {chassis_id}")
    return row


def _nation(ctx: PretenderContext) -> sqlite3.Row:
    row = ctx.reference_db.execute(
        "SELECT id,name,epithet,file_name_base,era FROM nations WHERE id=?",
        (ctx.nation_id,),
    ).fetchone()
    if row is None:
        raise ToolError(f"nation {ctx.nation_id} is absent from the reference database")
    return row


def _blessing_effect(ctx: PretenderContext, name: str) -> str | None:
    """Read the compact generated reference effect by stable blessing name."""
    try:
        row = ctx.reference_db.execute(
            "SELECT effect FROM blesses WHERE name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row is not None and row[0] else None


def _blessing_details(ctx: PretenderContext, blessing: Any) -> dict[str, Any]:
    """Combine exact client eligibility with the wiki's linked mechanic graph.

    Blessing names/requirements come from the client-derived records.  Effects
    and definitions remain attributed secondary material, but resolving the
    links here prevents an agent from treating canonical terms such as
    Recuperation and Regeneration as ordinary synonyms.
    """
    detail = {
        "id": blessing.id,
        "name": blessing.name,
        "cost": blessing.cost,
        "effect": _blessing_effect(ctx, blessing.name),
        "matched_passage": None,
        "linked_mechanics": [],
    }
    try:
        wiki = resolve_page_focus(
            "dom6:bless", blessing.name, database_path=ctx.wiki_db
        )
    except (FileNotFoundError, KeyError, ValueError, sqlite3.Error):
        return detail
    detail.update({
        "wiki_url": wiki["url"],
        "matched_passage": wiki["matched_passage"],
        "linked_mechanics": wiki["linked_mechanics"],
    })
    return detail


def _evaluate(
    ctx: PretenderContext,
    *,
    chassis_id: int,
    awakening: str,
    dominion: int,
    paths: Any,
    scales: Any,
    blessing_ids: Any,
) -> tuple[dict[str, Any], dict[str, int], dict[str, int], tuple[int, ...]]:
    path_values = _json_object(paths, "paths")
    scale_values = _json_object(scales, "scales")
    selected_blessings = _json_ids(blessing_ids, "blessing_ids")
    try:
        costs = design_cost(
            ctx.reference_db,
            ctx.nation_id,
            chassis_id,
            _awakening(awakening),
            dominion,
            path_values,
            scale_values,
        )
        blessings = validate_blessings(
            ctx.reference_db,
            ctx.nation_id,
            path_values,
            scale_values,
            selected_blessings,
        )
    except (PretenderDesignError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    result = {
        "nation_id": ctx.nation_id,
        "chassis_id": chassis_id,
        "awakening": awakening,
        "dominion": dominion,
        "paths": path_values,
        "scales": {name: scale_values.get(name, 0) for name in (
            "order", "productivity", "heat", "growth", "luck", "magic"
        )},
        "design_points": {
            **asdict(costs),
            "spent": costs.spent,
            "remaining": costs.remaining,
        },
        "bless_points": {
            "available": blessings.points_available,
            "spent": blessings.points_spent,
            "remaining": blessings.points_remaining,
        },
        "blessings": [
            _blessing_details(ctx, blessing) for blessing in blessings.selected
        ],
        "mechanics_policy": (
            "Blessing names and linked mechanic names are exact game terms, not synonyms. "
            "Base strategic claims on matched_passage and linked_mechanics; never substitute "
            "one named effect for another."
        ),
        "valid": True,
    }
    return result, path_values, scale_values, selected_blessings


_SCALE_LIMIT_RULE = (
    "An unmodified scale ranges from -2 to +2. A scale-limit modifier shifts "
    "that whole five-step window: a bad-side +1 gives -3..+1, while a "
    "good-side +1 gives -1..+3. Nation and chassis modifiers in the same "
    "direction do NOT add; the stronger shift wins. Opposing modifiers supply "
    "their respective endpoints. legal_range gives the resolved minimum and "
    "maximum for this nation and chassis; evaluate_pretender enforces it."
)


def register(registry: ToolRegistry | None = None) -> ToolRegistry:
    reg = registry or ToolRegistry()

    @reg.tool(
        "get_pretender_rules",
        "Show the selected nation and exact design/blessing conventions before designing.",
    )
    def get_pretender_rules(ctx: PretenderContext) -> dict[str, Any]:
        nation = _nation(ctx)
        return {
            "nation": dict(nation),
            "design_point_pools": {"awake": 450, "dormant": 600, "imprisoned": 800},
            "paths": [
                "fire", "air", "water", "earth", "astral",
                "death", "nature", "glamour", "blood",
            ],
            "scales": ["order", "productivity", "heat", "growth", "luck", "magic"],
            "unmodified_scale_range": [-2, 2],
            "dominion_range": [1, 10],
            "cost_model": {
                "summary": (
                    "Paths and Dominion escalate; ordinary scales are flat at 40 "
                    "a step. Do not budget from these shapes -- call "
                    "get_pretender_costs for the exact price of every choice on "
                    "a chassis, because several adjustments are national."
                ),
                "paths": (
                    "Opening a path from 0 costs the chassis new_path_cost. Each "
                    "level above max(starting level, 1) then costs 8 * (level - "
                    "starting level), so successive levels get steadily dearer. "
                    "Costs depend on that individual path's chassis starting "
                    "level: on Dagon, Earth 5->6 costs 32 because Earth starts "
                    "at 2, while Water 5->6 costs 40 because Water starts at 1."
                ),
                "dominion": (
                    "Moving n points from the chassis starting Dominion costs the "
                    "triangular sum 7 * n * (n + 1) / 2 -- 7, 21, 42, 70, 105 for "
                    "one through five steps. Going below the starting value "
                    "refunds the same amount."
                ),
                "scales": (
                    "Order, Productivity, Growth, Luck and Magic cost 40 a step "
                    "toward the good side and refund 40 a step toward the bad, "
                    "capped three steps from the national baseline. Ermor and "
                    "Lemuria start at Growth -3, so their first steps differ."
                ),
                "temperature": (
                    "Heat is not a good/bad axis. It is priced by distance from "
                    "the nation's preferred temperature, and deviation refunds in "
                    "either direction, capped at reaching neutral. For the 52 "
                    "nations with an effective preference an all-neutral scale "
                    "set therefore already refunds points before you choose "
                    "anything; read scales.all_neutral_cost, do not assume zero."
                ),
                "blessings": (
                    "Bless points are separate from design points and cannot be "
                    "exchanged. Each path yields max(level + national modifier - "
                    "1, 0), plus any national flat bonus."
                ),
            },
            "workflow": [
                "list_pretender_chassis",
                "get_pretender_costs",
                "list_pretender_blessings",
                "evaluate_pretender",
                "create_pretender",
            ],
            "note": (
                "Evaluate freely before creating. create_pretender writes a new, "
                "non-overwriting saved-god file the Dominions client can select."
            ),
            "tool_argument_shapes": {
                "paths": {"fire": 6, "air": 4},
                "scales": {"order": 1, "magic": 2},
                "blessing_ids": [7, 6, 3, 3],
                "instruction": (
                    "Pass these as native JSON objects/arrays, never as quoted "
                    "JSON strings."
                ),
            },
        }

    @reg.tool(
        "list_pretender_chassis",
        "List client-eligible chassis and exact costs for one awakening choice.",
        Param(
            "awakening", "string", "awake, dormant, or imprisoned",
            required=False, default="awake", choices=tuple(_AWAKENING_BY_NAME),
        ),
        Param(
            "detail", "string",
            "full includes each chassis's in-game description; costs omits it",
            required=False, default="full", choices=("full", "costs"),
        ),
    )
    def list_pretender_chassis(
        ctx: PretenderContext, awakening: str = "awake", detail: str = "full",
    ) -> dict[str, Any]:
        selected = _awakening(awakening)
        with_text = detail != "costs"
        rows = []
        for chassis in available_chassis(ctx.reference_db, ctx.nation_id):
            if selected < chassis.minimum_awakening:
                continue
            unit = _unit_row(ctx, chassis.id)
            rows.append({
                "chassis_id": chassis.id,
                "name": chassis.name,
                "minimum_awakening": chassis.minimum_awakening.name.lower(),
                "selected_awakening_cost": chassis_cost(
                    ctx.reference_db, ctx.nation_id, chassis, selected
                ),
                "costs": {
                    name: chassis_cost(ctx.reference_db, ctx.nation_id, chassis, value)
                    for name, value in _AWAKENING_BY_NAME.items()
                    if value >= chassis.minimum_awakening
                },
                "starting_dominion": chassis.start_dominion,
                "new_path_cost": chassis.new_path_cost,
                "starting_paths": chassis.base_paths,
                # Enough of the body to tell a battle god from a research god
                # from a hiding god without opening all of them.
                "stats": unit_profile.core_stats(unit),
                "traits": unit_profile.summary_traits(unit) or None,
                "scale_limit_modifiers": unit_profile.scale_limits(unit) or None,
                "scale_legal_range": {
                    name: [bound["minimum"], bound["maximum"]]
                    for name, bound in scale_limits(
                        ctx.reference_db, ctx.nation_id, chassis).items()
                },
                "description": unit_profile.description(
                    ctx.reference_db, chassis.id) if with_text else None,
            })
        return {
            "nation_id": ctx.nation_id,
            "awakening": awakening,
            "count": len(rows),
            "chassis": rows,
            "detail": detail,
            "note": (
                ("description is the card text the game shows for the chassis. "
                 if with_text else
                 "descriptions omitted at detail=costs; call this again with "
                 "detail=full, or describe_pretender_chassis for one. ") +
                "stats and traits are a triage summary; call "
                "describe_pretender_chassis for a candidate's weapons, armour, "
                "resistances, movement and its complete trait list."
            ),
            "scale_limit_rule": _SCALE_LIMIT_RULE,
        }

    @reg.tool(
        "describe_pretender_chassis",
        "Everything the game data records about one chassis: stats, weapons, armour, "
        "resistances, movement and survival, its complete trait list, equipment slots and "
        "leadership. Use this to judge what a chassis is for before pricing a design.",
        Param("chassis_id", "integer", "id from list_pretender_chassis"),
    )
    def describe_pretender_chassis(
        ctx: PretenderContext, chassis_id: int,
    ) -> dict[str, Any]:
        try:
            chassis = get_chassis(ctx.reference_db, ctx.nation_id, int(chassis_id))
        except (PretenderDesignError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        unit = _unit_row(ctx, chassis.id)
        return {
            "nation_id": ctx.nation_id,
            "chassis_id": chassis.id,
            "name": chassis.name,
            "minimum_awakening": chassis.minimum_awakening.name.lower(),
            "starting_dominion": chassis.start_dominion,
            "starting_paths": chassis.base_paths,
            "new_path_cost": chassis.new_path_cost,
            "description": unit_profile.description(ctx.reference_db, chassis.id),
            **unit_profile.full_profile(ctx.reference_db, unit),
            "scale_legal_range": scale_limits(
                ctx.reference_db, ctx.nation_id, chassis),
            "scale_limit_rule": _SCALE_LIMIT_RULE,
            "reference_value_note": (
                "Stats, traits, weapons, resistances and the description are "
                "the game's own, the description taken from the 6.36 binary. "
                "Leadership values are reported as reference values because "
                "not every display normalisation is decoded."
            ),
        }

    @reg.tool(
        "list_pretender_blessings",
        "List blessings currently eligible for exact path and scale choices, including their "
        "compact reference effects. evaluate_pretender resolves the full linked definitions for "
        "the blessings selected in a candidate. Pass paths/scales as native objects, not strings.",
        Param("paths", "object", 'path levels, e.g. {"fire": 6, "air": 4}'),
        Param(
            "scales", "object", 'scale choices, e.g. {"order": 1, "magic": 2}',
            required=False, default={},
        ),
    )
    def list_pretender_blessings(
        ctx: PretenderContext, paths: dict[str, int],
        scales: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        path_values = _json_object(paths, "paths")
        scale_values = _json_object(scales or {}, "scales")
        eligible = []
        for blessing in BLESSINGS.values():
            try:
                validate_blessings(
                    ctx.reference_db,
                    ctx.nation_id,
                    path_values,
                    scale_values,
                    [blessing.id],
                )
            except PretenderDesignError:
                continue
            eligible.append({
                "id": blessing.id,
                "name": blessing.name,
                "cost": blessing.cost,
                "path": blessing.path,
                "required_level": blessing.required_level,
                "secondary_path": blessing.secondary_path,
                "secondary_level": blessing.secondary_level,
                "scale_requirement": blessing.scale_requirement,
                "stackable": blessing.stackable,
                "effect": _blessing_effect(ctx, blessing.name),
            })
        return {
            "points_available": blessing_points(
                ctx.reference_db, ctx.nation_id, path_values
            ),
            "eligible": eligible,
            "mechanics_policy": (
                "Named mechanics in effect are exact terms. Do not infer what they do from the "
                "English word; evaluate a candidate to receive their linked definitions."
            ),
        }

    @reg.tool(
        "get_pretender_costs",
        "Exact price of every individual design choice on one chassis: each Dominion value, "
        "each level of each path with explicit previous/next-level costs, each scale step, "
        "and the bless points each path level yields. Call this before budgeting a design "
        "rather than inferring costs from evaluate_pretender totals.",
        Param("chassis_id", "integer", "id from list_pretender_chassis"),
        Param(
            "awakening", "string", "awake, dormant, or imprisoned",
            required=False, default="awake", choices=tuple(_AWAKENING_BY_NAME),
        ),
    )
    def get_pretender_costs(
        ctx: PretenderContext, chassis_id: int, awakening: str = "awake",
    ) -> dict[str, Any]:
        selected = _awakening(awakening)
        try:
            chassis = get_chassis(ctx.reference_db, ctx.nation_id, int(chassis_id))
            tables = cost_tables(
                ctx.reference_db, ctx.nation_id, chassis, selected
            )
        except (PretenderDesignError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {
            "nation_id": ctx.nation_id,
            "chassis_id": chassis.id,
            "chassis_name": chassis.name,
            "awakening": awakening,
            **tables,
            "how_to_read": (
                "Design total = chassis + dominion.by_value[chosen] + the "
                "paths.<path>.by_level[chosen].total of every path you raise + "
                "scales.all_neutral_cost + every scales.delta_by_value[scale]"
                "[chosen]. Negative numbers are refunds that enlarge the budget. "
                "At a currently selected path level, cost_to_next_level is the "
                "price to raise it once; cost_from_previous_level is what the "
                "last increase cost. These differ by 8, so do not substitute "
                "one for the other. Spend against point_pool."
            ),
        }

    common = (
        Param("chassis_id", "integer", "id from list_pretender_chassis"),
        Param(
            "awakening", "string", "awake, dormant, or imprisoned",
            choices=tuple(_AWAKENING_BY_NAME),
        ),
        Param("dominion", "integer", "selected Dominion score from 1 through 10"),
        Param("paths", "object", 'final path levels, e.g. {"fire": 6}'),
        Param("scales", "object", "the six signed scale choices"),
        Param(
            "blessing_ids", "array",
            "ids from list_pretender_blessings; duplicates mean stacks",
        ),
    )

    @reg.tool(
        "evaluate_pretender",
        "Validate a complete design and return exact point and blessing accounting.",
        *common,
    )
    def evaluate_pretender(ctx: PretenderContext, **values: Any) -> dict[str, Any]:
        result, _, _, _ = _evaluate(ctx, **values)
        return result

    @reg.tool(
        "create_pretender",
        "Validate and write a new non-overwriting saved-god file for this nation.",
        Param("name", "string", "pretender god name"),
        *common,
        writes=True,
    )
    def create_pretender(
        ctx: PretenderContext, name: str, **values: Any,
    ) -> dict[str, Any]:
        result, paths, scales, blessing_ids = _evaluate(ctx, **values)
        nation = _nation(ctx)
        design = Pretender(
            name=name.strip(),
            nation_slug=str(nation["file_name_base"]),
            nation_id=ctx.nation_id,
            chassis_id=int(values["chassis_id"]),
            awakening=_awakening(str(values["awakening"])),
            dominion=int(values["dominion"]),
            paths=paths,
            scales=scales,
            blessing_ids=blessing_ids,
        )
        try:
            payload = design.to_bytes()
        except (ValueError, KeyError) as exc:
            raise ToolError(str(exc)) from exc

        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        stem = str(nation["file_name_base"])
        destination = next(
            ctx.output_dir / f"{stem}_{index}.2h"
            for index in range(10000)
            if not (ctx.output_dir / f"{stem}_{index}.2h").exists()
        )
        destination.write_bytes(payload)
        return {
            **result,
            "name": design.name,
            "saved_file": str(destination),
            "bytes": len(payload),
            "checksum": int.from_bytes(payload[-2:], "little"),
        }

    from dom6_assistant.agent import wiki_tools
    wiki_tools.register(reg)
    return reg


def open_pretender_session(
    nation_id: int,
    *,
    reference_db: Path | None = None,
    output_dir: Path | None = None,
    wiki_db: Path | None = None,
) -> PretenderSession:
    path = reference_db or DEFAULT_REFERENCE_DB
    if not path.exists():
        raise FileNotFoundError(f"reference database not found at {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    ctx = PretenderContext(
        reference_db=connection,
        nation_id=int(nation_id),
        output_dir=output_dir or DEFAULT_NEWLORDS_DIR,
        wiki_db=wiki_db or DEFAULT_INDEX_DB,
    )
    _nation(ctx)  # fail at session open, not halfway through a design
    return PretenderSession(ctx=ctx, registry=register())
