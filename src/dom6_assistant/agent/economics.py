"""Visibility-safe economic calculations for assistant tools.

The game stores the inputs to province resources, not the displayed total.
This module is the adapter between the live player-visible state and the
calculator transcribed in :mod:`dom6_assistant.reference.province_calc`.

Only owned provinces are assembled.  That is sufficient for recruitment and
fort redistribution, because resources cross a border only between provinces
with the same owner.  In particular, this adapter never consults foreign unit
records or ``ftherlnd``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, cast

from dom6_assistant.reference.province_calc import (
    CAVE,
    ProvinceIncomeState,
    ProvinceResourceState,
    ProvinceSupplyState,
    global_resource_effects,
    GlobalEffectRecord,
    holy_point_allowance,
    province_income,
    province_resource_total,
    province_supplies,
    unit_resource_bonuses,
)
from dom6_assistant.file_reader.formats import commanders as C

if TYPE_CHECKING:
    from dom6_assistant.agent.registry import ToolContext


@dataclass(frozen=True)
class ProvinceResourceBudget:
    """One current province resource total and how far it can be trusted."""

    province_id: int
    total: int | None
    exact: bool
    source: str
    note: str | None = None


@dataclass(frozen=True)
class ProvinceIncomeBudget:
    """One current province income total and how far it can be trusted."""

    province_id: int
    total: int | None
    exact: bool
    source: str
    note: str | None = None


@dataclass(frozen=True)
class ProvinceSupplyBudget:
    """One current province supply total and how far it can be trusted."""

    province_id: int
    total: int | None
    exact: bool
    source: str
    note: str | None = None


@dataclass(frozen=True)
class ProvinceHolyBudget:
    """Nation-wide holy recruitment allowance exposed per province."""

    province_id: int
    total: int | None
    exact: bool
    source: str
    base_dominion: int | None = None
    maximum_dominion: int | None = None
    own_temples: int | None = None
    shared_temples: int | None = None
    disciple_nations: int = 1
    temple_holy_point_bonus: int = 0
    note: str | None = None


def owned_province_holy_budgets(
        ctx: ToolContext) -> dict[int, ProvinceHolyBudget]:
    """Compute the holy allowance shared by all our administered provinces.

    The current base Dominion comes from our pretender's serialized commander
    record. Nations that point at the same pretender are treated as a disciple
    group, matching the executable's shared-temple divisor.
    """
    nation_id = ctx.nation_id
    data = ctx.view.data
    parsed = ctx.view.parsed
    owned = [p for p in parsed.provinces
             if p.owner_nation_id == nation_id]
    source = ("computed from the current .trn and base-game reference data "
              "with the decoded Dominions 6.36 holy-point calculator at "
              "0x46a9a0 and panel continuation at 0x15cfa0")

    base = C.read_pretender_dominion(data, nation_id)
    pretenders = C.pretender_ids(data)
    our_pretender = pretenders.get(nation_id)
    group = ({nid for nid, pretender in pretenders.items()
              if pretender == our_pretender}
             if our_pretender is not None else {nation_id})
    group.add(nation_id)
    own_temples = sum(
        bool(p.has_temple) and p.administrative_owner == nation_id
        for p in parsed.provinces)
    shared_temples = sum(
        bool(p.has_temple) and p.administrative_owner in group
        for p in parsed.provinces)
    attr = ctx.reference_db.execute(
        "SELECT raw_value FROM attributes_by_nation "
        "WHERE nation_number=? AND attribute=427", (nation_id,)).fetchone()
    temple_bonus = int(attr["raw_value"] or 0) if attr is not None else 0

    maximum = (holy_point_allowance(
        base, shared_temples, disciple_nations=len(group))
               if base is not None else None)
    total = (maximum + temple_bonus * own_temples
             if maximum is not None else None)
    exact = base is not None
    note = None
    if base is None:
        note = ("the current pretender Dominion byte could not be resolved "
                "unambiguously from the .trn")
    elif len(group) > 1:
        # The formula and shared-pretender grouping are decoded, but this
        # project has no disciple fixture proving that every teammate temple
        # flag survives the player visibility filter.
        exact = False
        note = ("disciple formula decoded, but teammate temple visibility in "
                "a player .trn has not yet been verified in a disciple game")

    out: dict[int, ProvinceHolyBudget] = {}
    for p in owned:
        if p.administrative_owner != nation_id:
            out[p.province_id] = ProvinceHolyBudget(
                province_id=p.province_id, total=None, exact=False,
                source=source,
                note=("the panel uses administrative owner "
                      f"{p.administrative_owner}, not nominal owner "
                      f"{nation_id}; foreign current base Dominion is not "
                      "exposed as our recruitment budget"))
            continue
        out[p.province_id] = ProvinceHolyBudget(
            province_id=p.province_id, total=total, exact=exact, source=source,
            base_dominion=base, maximum_dominion=maximum,
            own_temples=own_temples, shared_temples=shared_temples,
            disciple_nations=len(group),
            temple_holy_point_bonus=temple_bonus, note=note)
    return out


def owned_province_supply_budgets(
        ctx: ToolContext) -> dict[int, ProvinceSupplyBudget]:
    """Compute current supplies for every province the player owns."""
    parsed = ctx.view.parsed
    nation_id = ctx.nation_id
    owned = [p for p in parsed.provinces
             if p.owner_nation_id == nation_id]

    site_ids = sorted({site_id for province in owned
                       for site_id in province.sites})
    sites: dict[int, sqlite3.Row] = {}
    if site_ids:
        marks = ",".join("?" * len(site_ids))
        sites = {row["id"]: row for row in ctx.reference_db.execute(
            f"SELECT id, sup FROM magic_sites WHERE id IN ({marks})",
            site_ids)}

    attributes = {row["attribute"]: int(row["raw_value"] or 0)
                  for row in ctx.reference_db.execute(
        "SELECT attribute, raw_value FROM attributes_by_nation "
        "WHERE nation_number=? AND attribute IN (170, 705)",
        (nation_id,))}

    blocked_links: set[tuple[int, int]] = set()
    for row in ctx.game_db.execute(
            "SELECT province_id, neighbour_id, border_flags FROM map_borders "
            "WHERE game_id=?", (ctx.game_id,)):
        if row["border_flags"] & 0x04:
            blocked_links.add(tuple(sorted(
                (row["province_id"], row["neighbour_id"]))))

    states: dict[int, ProvinceSupplyState] = {}
    for province in owned:
        visible_sites = [sites[site_id] for site_id in province.sites
                         if site_id in sites]
        blocked = frozenset(
            neighbour for neighbour in province.neighbours
            if tuple(sorted((province.province_id, neighbour)))
            in blocked_links)
        states[province.province_id] = ProvinceSupplyState(
            province_id=province.province_id,
            population=province.population,
            owner_nation_id=province.owner_nation_id,
            administrative_owner=province.administrative_owner,
            # Unlike the other scale fields, the parser retains raw Growth.
            growth_scale=-province.growth_scale,
            heat_scale=province.heat_scale,
            fort_type=province.fort_type,
            neighbours=tuple(province.neighbours),
            site_supply_bonus=sum(int(row["sup"] or 0)
                                  for row in visible_sites),
            preferred_heat_scale=-attributes.get(705, 0),
            no_death_supply=bool(attributes.get(170, 0)),
            blocked_supply_neighbours=blocked,
        )

    source = ("computed from the current .trn and public map with the decoded "
              "Dominions 6.36 supply calculators at 0x152470/0x1521b0")
    out: dict[int, ProvinceSupplyBudget] = {}
    for province_id in states:
        try:
            total = province_supplies(province_id, nation_id, states)
        except ValueError as exc:
            out[province_id] = ProvinceSupplyBudget(
                province_id, None, False, source, str(exc))
        else:
            out[province_id] = ProvinceSupplyBudget(
                province_id, total, True, source)
    return out


def owned_province_income_budgets(
        ctx: ToolContext) -> dict[int, ProvinceIncomeBudget]:
    """Compute the current income of every province the player owns.

    The ordinary vanilla path is complete. The two runtime-only exceptions
    that cannot be serialized (secondary flag bit 38 and mod-only aggregate
    province attribute 59) are absent from the base-game definitions and all
    observed saves; a mod integrating either must provide those inputs before
    claiming exactness.
    """
    parsed = ctx.view.parsed
    nation_id = ctx.nation_id
    owned = [p for p in parsed.provinces
             if p.owner_nation_id == nation_id]

    unit_types: dict[int, list[int]] = {}
    for unit in ctx.view.own_units():
        unit_types.setdefault(unit.province_id, []).append(unit.type_id)

    site_ids = sorted({site_id for province in owned
                       for site_id in province.sites})
    sites: dict[int, sqlite3.Row] = {}
    if site_ids:
        marks = ",".join("?" * len(site_ids))
        sites = {row["id"]: row for row in ctx.reference_db.execute(
            f"SELECT id, provinc, bringgold FROM magic_sites "
            f"WHERE id IN ({marks})", site_ids)}

    attributes = {row["attribute"]: int(row["raw_value"] or 0)
                  for row in ctx.reference_db.execute(
        "SELECT attribute, raw_value FROM attributes_by_nation "
        "WHERE nation_number=? AND attribute IN "
        "(267, 309, 311, 358, 359, 705, 706)", (nation_id,))}

    by_id = {p.province_id: p for p in parsed.provinces}
    effects = global_resource_effects(cast(
        Iterable[GlobalEffectRecord], parsed.global_effects))
    source = ("computed from the current .trn and base-game reference data "
              "with the decoded Dominions 6.36 income calculator at 0x46c970")
    out: dict[int, ProvinceIncomeBudget] = {}
    for province in owned:
        visible_sites = [sites[site_id] for site_id in province.sites
                         if site_id in sites]
        bonuses = unit_resource_bonuses(
            unit_types.get(province.province_id, ()))
        is_coastal = (not bool(province.current_terrain & 0x04)
                      and any(
                          bool(by_id[neighbour].current_terrain & 0x04)
                          for neighbour in province.neighbours
                          if neighbour in by_id))
        preferred_raw = attributes.get(705, 0)
        capital_raw = attributes.get(706, 0)
        if province.is_capital and capital_raw:
            preferred_raw = capital_raw
        state = ProvinceIncomeState(
            province_id=province.province_id,
            population=province.population,
            unrest=province.unrest,
            owner_nation_id=province.owner_nation_id,
            administrative_owner=province.administrative_owner,
            dominion_owner=province.dominion_owner,
            dominion_strength=province.dominion_strength,
            order_scale=province.order_scale,
            productivity_scale=province.productivity_scale,
            heat_scale=province.heat_scale,
            # The parser retains the raw signed byte for compatibility; the
            # calculator, like the other scale fields, uses the UI convention.
            growth_scale=-province.growth_scale,
            luck_scale=province.luck_scale,
            fort_type=province.fort_type,
            terrain_flags=province.current_terrain,
            is_capital=province.is_capital,
            is_coastal=is_coastal,
            land_gold=province.land_gold,
            site_province_income=sum(int(row["provinc"] or 0)
                                     for row in visible_sites),
            site_bring_gold=sum(int(row["bringgold"] or 0)
                                for row in visible_sites),
            preferred_heat_scale=-preferred_raw,
            nation_trade_coast_percent=attributes.get(267, 0),
            nation_cave_income_percent=attributes.get(311, 0),
            nation_income_percent=attributes.get(358, 0),
            half_death_income=bool(attributes.get(359, 0)),
            reduced_temperature_income=bool(attributes.get(309, 0)),
            counts_as_sun=bonuses.counts_as_sun,
        )
        try:
            total = province_income(
                state, nation_id, global_effects=effects)
        except ValueError as exc:
            out[province.province_id] = ProvinceIncomeBudget(
                province.province_id, None, False, source, str(exc))
            continue

        exact = True
        note = None
        darkness_active = (
            effects.utterdark or effects.theft_of_the_sun
            or effects.world_darkness
            or effects.eternal_twilight_caster is not None)
        if darkness_active and not bonuses.counts_as_sun:
            # The executable checks units of every nation. Our own units are
            # exact, but visibility rules deliberately withhold foreign unit
            # types even when their records happen to exist in our .trn.
            exact = False
            note = ("conditional during darkness: an unseen foreign Counts "
                    "as Sun unit in this province would suppress the penalty")
        out[province.province_id] = ProvinceIncomeBudget(
            province.province_id, total, exact, source, note)
    return out


def owned_province_resource_budgets(
        ctx: ToolContext) -> dict[int, ProvinceResourceBudget]:
    """Compute current resources for every province the player owns.

    Inputs come from the live ``.trn``, the public map-border table, visible
    magic sites, our own units, and the offline reference database.  Unknown
    future/modded fort types fail closed for that province rather than falling
    back to a historical range.
    """
    parsed = ctx.view.parsed
    nation_id = ctx.nation_id
    owned = [p for p in parsed.provinces
             if p.owner_nation_id == nation_id]

    # Resource-producing abilities are instance effects in the executable.
    # The base-game table is keyed by type id; include mounts because they are
    # live unit instances too, even though roster tools hide them as separate
    # soldiers.
    unit_types: dict[int, list[int]] = {}
    for unit in ctx.view.own_units():
        unit_types.setdefault(unit.province_id, []).append(unit.type_id)

    site_ids = sorted({site_id for province in owned
                       for site_id in province.sites})
    sites: dict[int, sqlite3.Row] = {}
    if site_ids:
        marks = ",".join("?" * len(site_ids))
        sites = {row["id"]: row for row in ctx.reference_db.execute(
            f"SELECT id, res, bringgold, bringres FROM magic_sites "
            f"WHERE id IN ({marks})", site_ids)}

    nation_attributes = {row["attribute"]: int(row["raw_value"] or 0)
                         for row in ctx.reference_db.execute(
        "SELECT attribute, raw_value FROM attributes_by_nation "
        "WHERE nation_number=? AND attribute IN (136, 312)",
        (nation_id,))}

    # #neighbourspec flag bit 0x04 is a Wall and blocks resource draw.  The
    # table stores its pair canonically, but accept either orientation so an
    # older database cannot make a wall disappear.
    blocked_links: set[tuple[int, int]] = set()
    for row in ctx.game_db.execute(
            "SELECT province_id, neighbour_id, border_flags FROM map_borders "
            "WHERE game_id=?", (ctx.game_id,)):
        if row["border_flags"] & 0x04:
            blocked_links.add(tuple(sorted(
                (row["province_id"], row["neighbour_id"]))))

    states: dict[int, ProvinceResourceState] = {}
    for province in owned:
        visible_sites = [sites[site_id] for site_id in province.sites
                         if site_id in sites]
        bonuses = unit_resource_bonuses(
            unit_types.get(province.province_id, ()))
        blocked = frozenset(
            neighbour for neighbour in province.neighbours
            if tuple(sorted((province.province_id, neighbour)))
            in blocked_links)
        states[province.province_id] = ProvinceResourceState(
            province_id=province.province_id,
            raw_resources=province.raw_b32,
            population=province.population,
            unrest=province.unrest,
            owner_nation_id=province.owner_nation_id,
            dominion_owner=province.dominion_owner,
            dominion_strength=province.dominion_strength,
            order_scale=province.order_scale,
            productivity_scale=province.productivity_scale,
            heat_scale=province.heat_scale,
            terrain_flags=province.current_terrain,
            fort_type=province.fort_type,
            neighbours=tuple(province.neighbours),
            administrative_owner=province.administrative_owner,
            site_resource_bonus=sum(int(row["res"] or 0)
                                    for row in visible_sites),
            site_bring_gold_bonus=sum(int(row["bringgold"] or 0)
                                      for row in visible_sites),
            site_bring_resource_bonus=sum(int(row["bringres"] or 0)
                                          for row in visible_sites),
            unit_resource_bonus=bonuses.resources,
            unit_mining_resource_bonus=bonuses.mining_resources,
            unit_ice_forging_bonus=bonuses.ice_forging,
            counts_as_sun=bonuses.counts_as_sun,
            nation_resource_percent_bonus=nation_attributes.get(136, 0),
            nation_cave_resource_percent_bonus=nation_attributes.get(312, 0),
            is_cave=bool(province.current_terrain & CAVE),
            luck_scale=province.luck_scale,
            blocked_resource_neighbours=blocked,
        )

    effects = global_resource_effects(cast(
        Iterable[GlobalEffectRecord], parsed.global_effects))
    source = ("computed from the current .trn and map with the decoded "
              "Dominions 6.35 resource calculator, revalidated against "
              "the shifted 6.36 calculator")
    out: dict[int, ProvinceResourceBudget] = {}
    for province_id in states:
        try:
            total = province_resource_total(
                province_id, nation_id, states, global_effects=effects)
        except ValueError as exc:
            out[province_id] = ProvinceResourceBudget(
                province_id, None, False, source, str(exc))
        else:
            out[province_id] = ProvinceResourceBudget(
                province_id, total, True, source)
    return out


def owned_province_resource_budget(
        ctx: ToolContext, province_id: int) -> ProvinceResourceBudget:
    """Return one owned province's budget, refusing absent/non-owned ids."""
    budgets = owned_province_resource_budgets(ctx)
    try:
        return budgets[province_id]
    except KeyError as exc:
        raise ValueError(
            f"province {province_id} is not one of our resource provinces") from exc
