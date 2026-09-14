"""Province income, resources and recruitment points transcribed from game code.

The panel figures are computed at render time and stored as no final total, so
the only way to model them is to read the executable. Income transcribes the
6.36 path through `0x46c970`. Resources transcribe the paths through
`0x46caf0`, `0x465500`, and `0x46d110`; site and unit producers, global
enchantments, extreme Luck, and Eternal Twilight's lighting predicate are
decoded. The results reproduce every observed owned income/resource panel and
all direct local-function probes.

The recruitment section transcribes `0x46d8f0`, the function the panel calls
with a province index to get its recruitment points. See
`knowledge/reverse/province_panel.md` for how all four functions were located.

The rest of this module-level discussion concerns recruitment points.

**The base is exact.** The bracket structure below is a direct reading of the
assembly, and it reproduces the two provinces where nothing else applies:
Kratas 116 and Citala 81, both to the unit. Those are independent provinces
with no fort, so the multipliers that follow are all 1.

**The multipliers are solved for provinces we own**, which is the only case
that can be acted on — you cannot recruit in a province you do not hold:

    points = floor(base * fort_multiplier * (1 + 0.10 * order))

Exact on all three provinces we own, at two different turns and three fort
tiers: Marignon 477, the Obsidian Waste 118, Copper Canyons 94, at turns 24
and 25 alike. Neither multiplier is fitted to that data. The citadel's 2.25 is
the fort screen's own stated "125% recruitment point value", the palisade's
1.5 is its stated +50%, and 0.10 per step of Order is the game's published
figure. Six observations, no free parameters.

This supersedes an earlier note that population was not monotonic — Copper
Canyons showing 94 against Citala's 81 on a smaller population — which was
read as a terrain effect. It is not terrain. Copper Canyons is ours and
carries the Order multiplier; Citala is not and does not.

**Foreign provinces are the unsolved case, and they do not matter much.** The
bare base is exact on Citala (81) and Kratas (116), so no multiplier applies
even though both read order_scale 2 in our file. Jinport reads 145 against a
base of 162, a ratio of 0.895, and two candidates fit it equally well: its
order scale of -1 giving 0.9, or its dominion strength of 3 where every other
province reads 6. One province cannot separate them, so nothing is claimed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Protocol


# ---------------------------------------------------------------------------
# Resources -- 0x46caf0 (local) and 0x46d110 (fort redistribution)
# ---------------------------------------------------------------------------

#: Percentage points per scale step. These are globals read by 0x46caf0, not
#: values fitted to province observations.
ORDER_RESOURCE_PERCENT = 3
PRODUCTIVITY_RESOURCE_PERCENT = 15

#: Income settings read as globals by the 6.36 function at ``0x46c970``.
#: These are the base-game/default values exposed by the map mod commands
#: ``#poppergold``, ``#unresthalfinc``, ``#turmoilincome``, ``#slothincome``
#: and ``#coldincome``. Callers can override them for a modified game.
POPULATION_PER_GOLD = 100
UNREST_HALF_INCOME = 50
ORDER_INCOME_PERCENT = 3
PRODUCTIVITY_INCOME_PERCENT = 3
GROWTH_INCOME_PERCENT = 1
TEMPERATURE_INCOME_PERCENT = 5

#: Supply settings read by the 6.36 function at ``0x152470``. These are the
#: live/default base percentage, ``#deathsupply``, ``#coldsupply`` and the
#: world supply multiplier.
BASE_SUPPLY_PERCENT = 100
DEATH_SUPPLY_PERCENT = 10
COLD_SUPPLY_PERCENT = 10

#: Fort definition +0x14, from the 40-byte table at 0x82d840 in Dom 6.35.
#: Entries 32--43 are identical placeholder "xxx" forts and are omitted.
FORT_ADMINISTRATION_PERCENT = {
    0: 0,
    1: 15, 2: 30, 3: 45, 4: 60,
    5: 15, 6: 30, 7: 45, 8: 60,
    9: 45, 10: 25, 11: 30, 12: 40, 13: 50, 14: 60,
    15: 15, 16: 30, 17: 45, 18: 60, 19: 70,
    20: 15, 21: 30, 22: 45, 23: 60,
    24: 40, 25: 60, 26: 5, 27: 15, 28: 20, 29: 30,
    30: 25, 31: 50,
}

#: Fort definition +0x18. The ordinary owner/admin-owner path uses the fort's
#: administration for supply-line projection; this storage value is returned
#: only in the split-ownership fort branch (for example an occupied fort).
FORT_SUPPLY_STORAGE = {
    0: 0,
    1: 150, 2: 750, 3: 2_500, 4: 7_500,
    5: 150, 6: 750, 7: 2_500, 8: 7_500,
    9: 3_000, 10: 300, 11: 750, 12: 2_500, 13: 5_000, 14: 10_000,
    15: 150, 16: 750, 17: 2_500, 18: 7_500, 19: 10_000,
    20: 150, 21: 750, 22: 2_500, 23: 7_500,
    24: 3_000, 25: 3_000, 26: 100, 27: 250, 28: 500, 29: 750,
    30: 300, 31: 5_000,
}

#: Fort definition +0x22 has bit 0x08 on these entries. Their administration
#: falls by 10% of the base for every step from Cold 3 toward Heat 3.
TEMPERATURE_SENSITIVE_FORTS = frozenset({20, 21, 22, 23, 26})

#: Unit attributes read by ``0x233d40`` in the final resource calculator.
#: Values were extracted from the 6.35 unit-definition table through the
#: game's own ``0x1ebfa0`` accessor.  The tuple is
#: ``(Resource Bonus, Mining Resource Bonus, Ice Forging)``.  In particular,
#: dom6inspector currently omits the three Mining Resource Bonus values and
#: mislabels attribute 408 as Bodyguard.
UNIT_RESOURCE_BONUSES: dict[int, tuple[int, int, int]] = {
    325: (10, 0, 0), 1159: (5, 0, 0), 1281: (10, 0, 0),
    1283: (0, 0, 4), 1702: (10, 0, 0), 1982: (10, 0, 0),
    2456: (10, 0, 0), 2714: (5, 0, 0), 2749: (5, 0, 0),
    2763: (15, 0, 0), 2835: (5, 0, 0), 3118: (25, 0, 0),
    3121: (30, 0, 0), 3138: (25, 0, 0), 3225: (20, 0, 0),
    3231: (25, 0, 0), 3602: (25, 0, 0), 3890: (0, 5, 0),
    3891: (0, 25, 0), 3892: (0, 25, 0), 4088: (10, 0, 0),
}


@dataclass(frozen=True)
class UnitResourceBonuses:
    """Sums of the three unit abilities used by ``0x46d110``."""

    resources: int = 0
    mining_resources: int = 0
    ice_forging: int = 0
    counts_as_sun: bool = False


#: A single such unit makes ``0x233d40(province, -1, 0x2ad, 1)`` positive and
#: suppresses all three darkness-enchantment resource penalties.
COUNTS_AS_SUN_UNITS = frozenset({1384, 1405, 2052, 2053, 2054, 2055})


@dataclass(frozen=True)
class GlobalResourceEffects:
    """Active global enchantments that change province resources or income.

    Caster nation ids come from global-effect record ``+0x04``. Eternal
    Twilight's narrow caster-dominion exception is part of its resource path;
    diplomatic agreements do not protect foreign nations. ``lighting_effects``
    retains the normalized fields consumed by ``0x47d270`` and ``0x47d960``
    so the exceptional Eternal Twilight predicate can be computed instead of
    supplied by a caller.
    """

    riches_from_beneath_caster: int | None = None
    gift_of_natures_bounty_caster: int | None = None
    perpetual_storm: bool = False
    sea_of_ice: bool = False
    world_storm: bool = False
    trade_wind_provinces: frozenset[int] = frozenset()
    utterdark: bool = False
    theft_of_the_sun: bool = False
    second_sun: bool = False
    world_darkness: bool = False
    eternal_twilight_caster: int | None = None
    lighting_effects: tuple[LightingEffect, ...] = ()


@dataclass(frozen=True)
class LightingEffect:
    """The four runtime-effect fields consumed by the lighting functions."""

    effect_id: int
    spell_id: int
    cast_province_id: int
    value1: int


class GlobalEffectRecord(Protocol):
    """Serialized global-effect fields consumed by the calculators."""

    effect_id: int
    spell_id: int
    caster_nation_id: int
    cast_province_id: int
    value1: int


# Secondary-province ``+0x68`` bits consumed by 0x47d270. The low terrain
# bits are also present in `.trn` current terrain. Infernal Waste is identified
# by the game's terrain-name formatter; Gateway is set alongside ``+0x66``'s
# map-editor gate number. Bit 24 is reused as transient special-battle state.
DEEP_SEA = 0x800
CAVE = 0x1000
BATTLE_LIGHT_PHASE = 0x1000000
INFERNAL_WASTE = 1 << 34
GATEWAY = 1 << 35

# In the 6.35 spell-definition table these are the only two spells whose
# 15 effect-opcode slots contain 0x300 (light). Both corresponding values are
# one; 0x47d270 nevertheless collapses any positive maximum to exactly -1.
LOCAL_LIGHT_SPELL_VALUES = {1188: 1, 1224: 1}  # Eternal Pyre, Solar Brilliance


def province_lighting_level(
        province_id: int,
        secondary_flags: int,
        effects: Iterable[LightingEffect], *,
        battle_province_id: int = -1,
        battle_time: int = 0,
        battle_counts_as_sun: int | bool = 0,
        ) -> int:
    """Return Dom 6.35's signed lighting value from ``0x47d270``.

    Positive values are darkness, zero is ordinary light, and negative values
    are illumination. ``effects`` is the active runtime/global-effect list;
    parsed `.trn` records can be normalized with :func:`global_resource_effects`.
    The battle arguments expose the three globals in the function's tail and
    default to strategic-map state.
    """
    if province_id < 0 or secondary_flags & GATEWAY:
        return 0

    records = tuple(effects)

    # Runtime records local to the province point at spell definitions. The
    # executable scans 15 opcode/value pairs, keeps their maximum Light value,
    # then returns -1 immediately if that maximum is nonzero.
    if any(
        record.effect_id > 0
        and record.cast_province_id == province_id
        and LOCAL_LIGHT_SPELL_VALUES.get(record.spell_id, 0) != 0
        for record in records
    ):
        return -1

    utterdark = any(
        record.effect_id == 0x38 and record.value1 > 0
        for record in records)
    base_darkness = 2 if utterdark else 1
    level = 2 if utterdark else 0

    # Event `worlddarkness` is represented here by 0x62 at pseudo-province -2.
    if any(record.effect_id == 0x62 and record.cast_province_id == -2
           for record in records):
        level = base_darkness

    theft = any(record.effect_id == 0x65 and record.value1 > 0
                for record in records)
    second_sun = any(record.effect_id == 0x29 and record.value1 > 0
                     for record in records)
    if theft and not second_sun:
        level = base_darkness

    # Battlefield Darkness is an unconditional local override, not a floor.
    if any(record.effect_id == 0x4D
           and record.cast_province_id == province_id
           for record in records):
        level = 2

    if secondary_flags & (DEEP_SEA | CAVE | INFERNAL_WASTE):
        level = max(level, 1)

    if any(record.effect_id == 0x61  # Solar Eclipse
           and record.cast_province_id == province_id
           for record in records):
        level = max(level, 1)

    phase_flag = bool(secondary_flags & BATTLE_LIGHT_PHASE)
    if province_id == battle_province_id:
        # The flag means darkness through time 149 and flips at time 150.
        if ((battle_time <= 149 and phase_flag)
                or (battle_time > 149 and not phase_flag)):
            level = max(level, 1)
        return level - int(battle_counts_as_sun > 0)

    if phase_flag:
        level = max(level, 1)

    # While any battle is active the game's battle-wide Counts as Sun cache is
    # used and the strategic Fire Storm branch is skipped.
    if battle_province_id >= 0:
        return level - int(battle_counts_as_sun > 0)

    if any(record.effect_id == 0x19  # Fire Storm
           and record.cast_province_id == province_id
           for record in records):
        level -= 1
    return level


def twilight_state_applies(
        province_id: int,
        secondary_flags: int,
        effects: Iterable[LightingEffect], *,
        is_astral_plane: bool = False,
        battle_province_id: int = -1,
        battle_time: int = 0,
        battle_counts_as_sun: int | bool = 0,
        ) -> bool:
    """Return the full Twilight predicate at ``0x47d960``.

    This includes local Twilight (effect ``0x73``), active Eternal Twilight
    (``0x76``), and the battle-time Twilight window. Resource calculation uses
    the active Eternal Twilight case, but exposing the whole predicate makes
    the transcription independently testable.
    """
    if province_id < 0 or is_astral_plane:
        return False
    records = tuple(effects)
    if province_lighting_level(
            province_id, secondary_flags, records,
            battle_province_id=battle_province_id,
            battle_time=battle_time,
            battle_counts_as_sun=battle_counts_as_sun) != 0:
        return False
    if secondary_flags & (DEEP_SEA | CAVE):
        return False
    if any(record.effect_id == 0x73
           and record.cast_province_id == province_id
           for record in records):
        return True
    if any(record.effect_id == 0x76 and record.value1 > 0
           for record in records):
        return True
    return (province_id == battle_province_id
            and 100 <= battle_time <= 149)


def global_resource_effects(
        records: Iterable[GlobalEffectRecord]) -> GlobalResourceEffects:
    """Select resource-changing globals from parsed `.trn` effect records."""
    source = tuple(records)
    normalized = tuple(
        LightingEffect(
            effect_id=record.effect_id,
            spell_id=record.spell_id,
            cast_province_id=record.cast_province_id,
            value1=record.value1,
        )
        for record in source
    )
    riches_caster = bounty_caster = twilight_caster = None
    perpetual_storm = any(
        record.effect_id == 0x10 and record.value1 > 0
        for record in normalized)
    sea_of_ice = any(record.effect_id == 0x1C and record.value1 > 0
                     for record in normalized)
    world_storm = any(
        record.effect_id == 0x6E and record.cast_province_id == -2
        for record in normalized)
    trade_winds = frozenset(
        record.cast_province_id for record in normalized
        if record.effect_id == 0x5F and record.value1 > 0)
    utterdark = any(record.effect_id == 0x38 and record.value1 > 0
                    for record in normalized)
    theft = any(record.effect_id == 0x65 and record.value1 > 0
                for record in normalized)
    second_sun = any(record.effect_id == 0x29 and record.value1 > 0
                     for record in normalized)
    world_darkness = any(
        record.effect_id == 0x62 and record.cast_province_id == -2
        for record in normalized)
    # Caster ids are resource-calculator inputs, but not lighting inputs.
    # Iterate the source again by materializing it alongside the normalized
    # records rather than adding unrelated fields to LightingEffect.
    for source_record in source:
        if source_record.effect_id == 0x23 and source_record.value1 > 0:
            riches_caster = source_record.caster_nation_id
        elif source_record.effect_id == 0x1B and source_record.value1 > 0:
            bounty_caster = source_record.caster_nation_id
        elif source_record.effect_id == 0x76 and source_record.value1 > 0:
            twilight_caster = source_record.caster_nation_id
    return GlobalResourceEffects(
        riches_from_beneath_caster=riches_caster,
        gift_of_natures_bounty_caster=bounty_caster,
        perpetual_storm=perpetual_storm,
        sea_of_ice=sea_of_ice,
        world_storm=world_storm,
        trade_wind_provinces=trade_winds,
        utterdark=utterdark,
        theft_of_the_sun=theft,
        second_sun=second_sun,
        world_darkness=world_darkness,
        eternal_twilight_caster=twilight_caster,
        lighting_effects=normalized,
    )


def unit_resource_bonuses(unit_type_ids: Iterable[int]) -> UnitResourceBonuses:
    """Sum base-game resource abilities for units present in one province.

    ``0x233d40`` evaluates live instances, so an instance modified by a mod or
    another game mechanic can differ from this base-type lookup.  Ordinary
    unmodified units in the player's `.trn` are represented exactly.
    """
    resources = mining = ice = 0
    counts_as_sun = False
    for unit_type_id in unit_type_ids:
        unit_resources, unit_mining, unit_ice = UNIT_RESOURCE_BONUSES.get(
            unit_type_id, (0, 0, 0))
        resources += unit_resources
        mining += unit_mining
        ice += unit_ice
        counts_as_sun = counts_as_sun or unit_type_id in COUNTS_AS_SUN_UNITS
    return UnitResourceBonuses(resources, mining, ice, counts_as_sun)


def _trunc_div(numerator: int, denominator: int) -> int:
    """Integer division toward zero, matching x86 ``idiv`` and C.

    Python's ``//`` rounds negative values down. Most resource intermediates
    are positive, but event and site modifiers can be negative, so preserving
    the executable's rule is cheap insurance.
    """
    if denominator == 0:
        raise ZeroDivisionError("resource calculation divisor is zero")
    quotient = abs(numerator) // abs(denominator)
    return -quotient if (numerator < 0) != (denominator < 0) else quotient


@dataclass(frozen=True)
class ProvinceResourceState:
    """The file-resident inputs used by the ordinary resource path.

    ``raw_resources`` is `.trn` ``raw_b32``. Site ids resolve through
    ``magic_sites``: ``res`` is attribute 0x0e, ``bringgold`` is 0x17f, and
    ``bringres`` is 0x180. Unit bonus sums can be obtained with
    :func:`unit_resource_bonuses` from the player's own units in the province.

    ``terrain_flags`` should be the `.trn` current terrain; its Sea bit blocks
    land/sea draw. ``blocked_resource_neighbours`` represents `.map` links
    whose `#neighbourspec` flags contain Wall bit 0x04. It is empty for
    ordinary symmetric links.
    """
    province_id: int
    raw_resources: int
    population: int
    unrest: int
    owner_nation_id: int
    dominion_owner: int
    dominion_strength: int
    order_scale: int
    productivity_scale: int
    heat_scale: int = 0
    terrain_flags: int = 0
    fort_type: int = 0
    neighbours: tuple[int, ...] = ()
    administrative_owner: int | None = None
    site_resource_bonus: int = 0
    site_bring_gold_bonus: int = 0
    site_bring_resource_bonus: int = 0
    unit_resource_bonus: int = 0
    unit_mining_resource_bonus: int = 0
    unit_ice_forging_bonus: int = 0
    counts_as_sun: bool = False
    # ``None`` computes 0x47d960 from the effect list and lighting flags.
    # A bool remains available for observations or callers lacking those
    # inputs. ``lighting_flags`` carries secondary high bits absent from the
    # ordinary 32-bit `.trn` current-terrain field.
    twilight_applies: bool | None = None
    lighting_flags: int = 0
    is_astral_plane: bool = False
    nation_resource_percent_bonus: int = 0
    nation_cave_resource_percent_bonus: int = 0
    is_cave: bool = False
    luck_scale: int = 0
    fort_administration_override: int | None = None
    blocked_resource_neighbours: frozenset[int] = field(
        default_factory=frozenset)

    @property
    def admin_owner(self) -> int:
        """The second owner field; ordinary `.trn` state equals the owner."""
        if self.administrative_owner is None:
            return self.owner_nation_id
        return self.administrative_owner


def province_local_resources(province: ProvinceResourceState,
                             nation_id: int, *,
                             raw_resource_percent: int = 100,
                             world_resource_percent: int = 100,
                             global_effects: GlobalResourceEffects | None = None,
                             ) -> int:
    """Return `0x46caf0(province, nation)` for the decoded ordinary path.

    Positive scales benefit a nation only under its own dominion. Harmful
    scales apply everywhere. Magic-site ``res`` and ``bringres`` values are
    added after unrest. ``bringgold`` affects income instead, but its presence
    also enables Mining Resource Bonus in the final fortified-province path.

    Global enchantments are applied in their exact executable order. Extreme
    positive Luck is also represented: Luck 4 applies 95%, while Luck 5 or
    greater applies 85%, before unrest. When ``twilight_applies`` is ``None``,
    the complete executable predicate is evaluated from ``lighting_effects``.
    """
    if province.population < 0:
        raise ValueError("population cannot be negative")
    if province.unrest <= -100:
        raise ValueError("unrest must be greater than -100")

    value = _trunc_div(province.raw_resources * raw_resource_percent, 100)
    value = _trunc_div(value * world_resource_percent, 100)

    effects = global_effects or GlobalResourceEffects()
    riches_caster = effects.riches_from_beneath_caster
    if (riches_caster is not None
            and province.dominion_owner == riches_caster
            and province.dominion_strength > 0):
        strength = min(province.dominion_strength, 5)
        value += _trunc_div(value * strength, 10)

    if not province.counts_as_sun:
        # Deep Sea and Cave provinces bypass Utterdark and Theft of the Sun.
        if not (province.terrain_flags & 0x1800):
            if effects.utterdark:
                value = _trunc_div(value, 10)
            if effects.theft_of_the_sun:
                value = _trunc_div(value * 70, 100)

        twilight_caster = effects.eternal_twilight_caster
        twilight_applies = province.twilight_applies
        if twilight_applies is None:
            twilight_applies = twilight_state_applies(
                province.province_id,
                province.terrain_flags | province.lighting_flags,
                effects.lighting_effects,
                is_astral_plane=province.is_astral_plane,
            )
        if twilight_caster is not None and twilight_applies:
            protected = (
                province.owner_nation_id == twilight_caster
                and province.dominion_owner == twilight_caster
                and province.dominion_strength > 0
            )
            if not protected:
                # The assembly subtracts trunc(value / 5), rather than doing
                # one combined 80% multiplication; that differs at remainders.
                value -= _trunc_div(value, 5)

    friendly_dominion = (
        province.dominion_strength > 0
        and province.dominion_owner == nation_id
    )
    order = (province.order_scale if friendly_dominion
             else min(province.order_scale, 0))
    productivity = (province.productivity_scale if friendly_dominion
                    else min(province.productivity_scale, 0))
    value = _trunc_div(
        value * (100 + ORDER_RESOURCE_PERCENT * order), 100)
    value = _trunc_div(
        value * (100 + PRODUCTIVITY_RESOURCE_PERCENT * productivity), 100)
    if province.luck_scale == 4:
        value = _trunc_div(value * 95, 100)
    elif province.luck_scale >= 5:
        value = _trunc_div(value * 85, 100)
    value = _trunc_div(value * 100, 100 + province.unrest)
    value += (province.site_resource_bonus
              + province.site_bring_resource_bonus)

    # The in-memory field is population / 10 and the assembly applies
    # pop10 / 100 when pop10 <= 99: equivalently population / 1000.
    if province.population < 1_000:
        value = _trunc_div(value * province.population, 1_000)
    return value


def _fort_administration_percent(fort_type: int, heat_scale: int,
                                 override: int | None = None) -> int:
    """Shared fort-table lookup used by both province calculators."""
    if override is not None:
        return override
    try:
        administration = FORT_ADMINISTRATION_PERCENT[fort_type]
    except KeyError as exc:
        raise ValueError(
            f"unknown fort type {fort_type}; supply an administration override"
        ) from exc
    if fort_type in TEMPERATURE_SENSITIVE_FORTS:
        administration -= _trunc_div(
            administration * (3 + heat_scale), 10)
    return administration


def fort_administration_percent(province: ProvinceResourceState) -> int:
    """Return the fort's resource-draw percentage.

    An override is required for a future/modded fort absent from the decoded
    6.35 table. The game's `+0x22 & 0x08` temperature adjustment is applied to
    the ice forts and Half Melted Fort using the `.trn` Heat/Cold scale.
    """
    return _fort_administration_percent(
        province.fort_type, province.heat_scale,
        province.fort_administration_override)


def _resource_link_is_open(a: ProvinceResourceState,
                           b: ProvinceResourceState) -> bool:
    return ((a.terrain_flags ^ b.terrain_flags) & 0x04 == 0
            and b.province_id not in a.blocked_resource_neighbours
            and a.province_id not in b.blocked_resource_neighbours)


def adjacent_fort_draw_percent(
        province: ProvinceResourceState,
        provinces: Mapping[int, ProvinceResourceState]) -> int:
    """Transcribe `0x465500`: administration drawn by adjacent forts."""
    total = 0
    for neighbour_id in province.neighbours:
        neighbour = provinces.get(neighbour_id)
        if neighbour is None:
            continue
        if neighbour.owner_nation_id != province.owner_nation_id:
            continue
        if neighbour.admin_owner != province.owner_nation_id:
            continue
        if neighbour.fort_type <= 0 or not _resource_link_is_open(
                province, neighbour):
            continue
        total += fort_administration_percent(neighbour)
    return total


def province_resource_total(
        province_id: int,
        nation_id: int,
        provinces: Mapping[int, ProvinceResourceState], *,
        global_effects: GlobalResourceEffects | None = None) -> int:
    """Return `0x46d110(province_id, nation_id)` on the decoded path.

    This includes the no-fort halving rule and redistribution into a fort from
    adjacent friendly unfortified provinces. Callers must provide every
    neighbour that might share resources. Missing neighbour records are
    conservatively skipped rather than invented.
    """
    try:
        province = provinces[province_id]
    except KeyError as exc:
        raise ValueError(f"province {province_id} is absent") from exc

    local = province_local_resources(
        province, nation_id, global_effects=global_effects)
    if province.fort_type <= 0:
        if province.owner_nation_id != nation_id:
            return 0
        draw = adjacent_fort_draw_percent(province, provinces)
        if draw > 99:
            return 0
        if draw > 49:
            return _trunc_div(local * (100 - draw), 100)
        return max(2, _trunc_div(local, 2))

    total = _trunc_div(
        local * (100 + province.nation_resource_percent_bonus), 100)
    if province.is_cave:
        total = _trunc_div(
            total * (100 + province.nation_cave_resource_percent_bonus),
            100)

    # Province +0x45bc is the negated Heat scale.  Ice Forging contributes its
    # value once per Cold step in any fort, not only in an ice fort.
    cold_level = max(0, -province.heat_scale)
    total += cold_level * province.unit_ice_forging_bonus
    total += province.unit_resource_bonus

    # Mining Resource Bonus works in Cave terrain and in any province
    # containing a positive bringgold or bringres site (the two 0x4695a0
    # eligibility checks at 0x46d303 and 0x46d408).
    mining_eligible = (
        province.is_cave
        or province.site_bring_gold_bonus > 0
        or province.site_bring_resource_bonus > 0
    )
    if mining_eligible:
        total += province.unit_mining_resource_bonus

    owner_matches = province.owner_nation_id == nation_id
    admin_matches = province.admin_owner == nation_id
    if owner_matches != admin_matches:
        return _trunc_div(total, 2)
    if not owner_matches:
        return 0

    administration = fort_administration_percent(province)
    if administration > 0:
        for neighbour_id in province.neighbours:
            neighbour = provinces.get(neighbour_id)
            if neighbour is None:
                continue
            if neighbour.owner_nation_id != nation_id:
                continue
            if neighbour.admin_owner != nation_id:
                continue
            if neighbour.fort_type > 0 or not _resource_link_is_open(
                    province, neighbour):
                continue
            neighbour_local = province_local_resources(
                neighbour, nation_id, global_effects=global_effects)
            competing_draw = adjacent_fort_draw_percent(neighbour, provinces)
            if competing_draw > 100:
                neighbour_local = _trunc_div(
                    neighbour_local * 100, competing_draw)
            total += _trunc_div(
                administration * neighbour_local, 100)
    return max(2, total)


# ---------------------------------------------------------------------------
# Income -- 0x46c970 (Dom 6.36)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProvinceIncomeState:
    """File/reference inputs consumed by the province-income calculator.

    Scale fields use the user-facing convention (positive Order,
    Productivity, Heat, Growth and Luck). ``preferred_heat_scale`` uses the
    same convention. The nation table stores the latter with the opposite
    sign, so the adapter negates attribute 705/706 once when building this
    state.

    Two exceptionally narrow runtime branches are represented explicitly:
    ``transient_thirty_percent_penalty`` is secondary-province flag bit 38,
    and ``local_dominion_income_modifier`` is aggregate province attribute 59.
    Neither occurs in the base-game site table or the observed saves, but
    keeping them as inputs prevents a modded caller from receiving a silently
    incomplete result.
    """

    province_id: int
    population: int
    unrest: int
    owner_nation_id: int
    administrative_owner: int
    dominion_owner: int
    dominion_strength: int
    order_scale: int
    productivity_scale: int
    heat_scale: int
    growth_scale: int
    luck_scale: int
    fort_type: int = 0
    terrain_flags: int = 0
    is_capital: bool = False
    is_coastal: bool = False
    land_gold: int = 0
    site_province_income: int = 0
    site_bring_gold: int = 0
    preferred_heat_scale: int = 0
    nation_trade_coast_percent: int = 0
    nation_cave_income_percent: int = 0
    nation_income_percent: int = 0
    half_death_income: bool = False
    reduced_temperature_income: bool = False
    counts_as_sun: bool = False
    twilight_applies: bool | None = None
    lighting_flags: int = 0
    is_astral_plane: bool = False
    local_dominion_income_modifier: int = 0
    transient_thirty_percent_penalty: bool = False
    fort_administration_override: int | None = None

    @property
    def is_sea(self) -> bool:
        return bool(self.terrain_flags & 0x04)

    @property
    def is_cave(self) -> bool:
        return bool(self.terrain_flags & CAVE)


def province_income(
        province: ProvinceIncomeState,
        nation_id: int, *,
        global_effects: GlobalResourceEffects | None = None,
        population_per_gold: int = POPULATION_PER_GOLD,
        world_income_percent: int = 100,
        unrest_half_income: int = UNREST_HALF_INCOME,
        order_income_percent: int = ORDER_INCOME_PERCENT,
        productivity_income_percent: int = PRODUCTIVITY_INCOME_PERCENT,
        growth_income_percent: int = GROWTH_INCOME_PERCENT,
        temperature_income_percent: int = TEMPERATURE_INCOME_PERCENT,
        ) -> int:
    """Return Dom 6.36 ``0x46c970(province, nation)``.

    Integer operations deliberately remain separate and in executable order;
    collapsing the percentages changes real panel values at truncation
    boundaries. The defaults are the live 6.36 globals and map defaults.
    """
    if province.population < 0:
        raise ValueError("population cannot be negative")
    if population_per_gold <= 0:
        raise ValueError("population_per_gold must be positive")
    if unrest_half_income <= 0:
        raise ValueError("unrest_half_income must be positive")

    effects = global_effects or GlobalResourceEffects()

    # Population and event income are scaled by the world setting. The two
    # site aggregates are added afterwards and therefore escape that setting.
    value = _trunc_div(province.population, population_per_gold)
    value += province.land_gold
    value = _trunc_div(value * world_income_percent, 100)
    value += province.site_province_income + province.site_bring_gold
    value = max(value, 0)

    # A fort contributes half its administration percentage to its local
    # province's income. Trade Coast and Cave Income are additive percentages
    # of the same pre-fort value, not sequential multipliers.
    if (province.fort_type > 0
            and province.administrative_owner == nation_id):
        pre_fort = value
        administration = _fort_administration_percent(
            province.fort_type, province.heat_scale,
            province.fort_administration_override)
        value += _trunc_div(pre_fort * administration, 200)
        coast_blocked = (effects.perpetual_storm or effects.sea_of_ice
                         or effects.world_storm)
        if (province.is_coastal and not coast_blocked
                and province.nation_trade_coast_percent):
            value += _trunc_div(
                pre_fort * province.nation_trade_coast_percent, 100)
        if province.is_cave and province.nation_cave_income_percent:
            value += _trunc_div(
                pre_fort * province.nation_cave_income_percent, 100)

    # Beneficial scales are ignored under hostile dominion; harmful scales
    # still apply. No dominion is the non-hostile path in the executable.
    hostile_dominion = (
        province.dominion_strength > 0
        and province.dominion_owner >= 0
        and province.dominion_owner != nation_id
    )

    def effective_scale(scale: int) -> int:
        return min(scale, 0) if hostile_dominion else scale

    order = effective_scale(province.order_scale)
    value = _trunc_div(
        value * (100 + order_income_percent * order), 100)

    growth = effective_scale(province.growth_scale)
    growth_denominator = 200 if province.half_death_income else 100
    value = _trunc_div(
        value * (growth_denominator
                 + growth_income_percent * growth),
        growth_denominator)

    productivity = effective_scale(province.productivity_scale)
    value = _trunc_div(
        value * (100 + productivity_income_percent * productivity), 100)

    temperature_step = temperature_income_percent
    if province.reduced_temperature_income:
        # The assembly's signed divide by two rounds toward zero. With the
        # default +5 this becomes two percentage points per scale step.
        temperature_step = _trunc_div(temperature_step, 2)
    temperature_distance = abs(
        province.heat_scale - province.preferred_heat_scale)
    value = _trunc_div(
        value * (100 - temperature_step * temperature_distance), 100)

    if province.luck_scale == 4:
        value = _trunc_div(value * 95, 100)
    elif province.luck_scale >= 5:
        value = _trunc_div(value * 85, 100)

    if province.transient_thirty_percent_penalty:
        value = _trunc_div(value * 70, 100)

    if (not province.is_sea and not province.is_cave
            and (effects.perpetual_storm or effects.world_storm)):
        value = _trunc_div(value * 80, 100)

    riches_caster = effects.riches_from_beneath_caster
    if (riches_caster is not None
            and province.dominion_owner == riches_caster
            and province.dominion_strength > 0):
        strength = min(province.dominion_strength, 5)
        value += _trunc_div(value * strength, 25)
        value += _trunc_div(province.site_bring_gold * strength, 5)

    bounty_caster = effects.gift_of_natures_bounty_caster
    if (bounty_caster is not None
            and province.dominion_owner == bounty_caster
            and province.dominion_strength > 0):
        strength = min(province.dominion_strength, 10)
        value += _trunc_div(value * strength * 15, 100)

    if (province.is_coastal
            and province.province_id in effects.trade_wind_provinces):
        value += _trunc_div(value, 4)

    if not province.counts_as_sun:
        twilight_caster = effects.eternal_twilight_caster
        twilight_applies = province.twilight_applies
        if twilight_applies is None:
            twilight_applies = twilight_state_applies(
                province.province_id,
                province.terrain_flags | province.lighting_flags,
                effects.lighting_effects,
                is_astral_plane=province.is_astral_plane,
            )
        if twilight_caster is not None and twilight_applies:
            protected = (
                province.owner_nation_id == twilight_caster
                and province.dominion_owner == twilight_caster
                and province.dominion_strength > 0
            )
            if not protected:
                value -= _trunc_div(value, 5)

        # Deep Sea and Cave bypass these income penalties. Utterdark takes
        # precedence; Second Sun cancels Theft/worlddarkness but not Utterdark.
        if not (province.terrain_flags & (DEEP_SEA | CAVE)):
            if effects.utterdark:
                value = _trunc_div(value * 10, 100)
            elif (not effects.second_sun
                  and (effects.theft_of_the_sun
                       or effects.world_darkness)):
                value = _trunc_div(value * 70, 100)

    if province.local_dominion_income_modifier > 0:
        modifier = max(
            province.local_dominion_income_modifier,
            province.dominion_strength)
        if province.owner_nation_id != province.dominion_owner:
            modifier *= -5
        value = _trunc_div(value * (100 + modifier), 100)

    value = _trunc_div(
        value * (100 + province.nation_income_percent), 100)
    value = max(_trunc_div(
        value * unrest_half_income,
        unrest_half_income + max(province.unrest, 0)), 0)

    owner_matches = province.owner_nation_id == nation_id
    admin_matches = province.administrative_owner == nation_id
    if owner_matches:
        if province.fort_type > 0 and not admin_matches:
            administration = _fort_administration_percent(
                province.fort_type, province.heat_scale,
                province.fort_administration_override)
            value = _trunc_div(value * (100 - administration), 100)
        return max(value, 0)
    if admin_matches and province.fort_type > 0:
        administration = _fort_administration_percent(
            province.fort_type, province.heat_scale,
            province.fort_administration_override)
        return max(_trunc_div(value * administration, 100), 0)
    return 0


# ---------------------------------------------------------------------------
# Supplies -- 0x152470 and recursive supply line 0x1521b0 (Dom 6.36)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProvinceSupplyState:
    """Serialized/reference inputs consumed by the supply calculator.

    ``growth_scale`` and ``heat_scale`` use the user-facing convention.
    ``preferred_heat_scale`` is the requesting nation's preferred climate in
    the same convention. ``fort_supply_divisor`` is runtime province +0x71;
    it matters only when the requesting nation is administrative owner but not
    ordinary owner of a fort, and defaults to the ordinary value one.
    """

    province_id: int
    population: int
    owner_nation_id: int
    administrative_owner: int
    growth_scale: int
    heat_scale: int
    fort_type: int = 0
    neighbours: tuple[int, ...] = ()
    site_supply_bonus: int = 0
    preferred_heat_scale: int = 0
    no_death_supply: bool = False
    fort_supply_divisor: int = 1
    fort_administration_override: int | None = None
    fort_supply_storage_override: int | None = None
    blocked_supply_neighbours: frozenset[int] = field(
        default_factory=frozenset)


def _fort_supply_storage(province: ProvinceSupplyState) -> int:
    if province.fort_supply_storage_override is not None:
        storage = province.fort_supply_storage_override
    else:
        try:
            storage = FORT_SUPPLY_STORAGE[province.fort_type]
        except KeyError as exc:
            raise ValueError(
                f"unknown fort type {province.fort_type}; supply a storage override"
            ) from exc
    if province.fort_type in TEMPERATURE_SENSITIVE_FORTS:
        storage -= _trunc_div(
            storage * (3 + province.heat_scale), 10)
    return storage


def _supply_link_is_open(a: ProvinceSupplyState,
                         b: ProvinceSupplyState) -> bool:
    return (b.province_id not in a.blocked_supply_neighbours
            and a.province_id not in b.blocked_supply_neighbours)


def province_supply_line(
        province_id: int,
        nation_id: int,
        provinces: Mapping[int, ProvinceSupplyState]) -> int:
    """Return recursive 6.36 ``0x1521b0`` fort supply projection.

    A friendly fort originates six times its administration percentage. The
    best (not summed) fort is projected through same-owner, non-Wall links,
    divided by one plus graph distance, out to four links. The executable
    carries the preceding two provinces rather than a visited set; reproducing
    that detail keeps cyclic maps and truncation identical.
    """
    if province_id not in provinces:
        raise ValueError(f"province {province_id} is absent")

    def walk(current_id: int, previous: int, previous2: int,
             depth: int) -> int:
        current = provinces.get(current_id)
        if current is None:
            return 0
        best = 0
        if (current.administrative_owner == nation_id
                and current.fort_type > 0):
            administration = _fort_administration_percent(
                current.fort_type, current.heat_scale,
                current.fort_administration_override)
            best = administration * 6
        if depth > 1:
            best = _trunc_div(best, depth)
        if depth > 4:
            return best

        for neighbour_id in current.neighbours:
            if neighbour_id <= 0 or neighbour_id in (previous, previous2):
                continue
            neighbour = provinces.get(neighbour_id)
            if neighbour is None or neighbour.owner_nation_id != nation_id:
                continue
            if not _supply_link_is_open(current, neighbour):
                continue
            candidate = walk(
                neighbour_id, current_id, previous, depth + 1)
            best = max(best, candidate)
        return best

    return walk(province_id, 0, 0, 1)


def province_supplies(
        province_id: int,
        nation_id: int,
        provinces: Mapping[int, ProvinceSupplyState], *,
        base_supply_percent: int = BASE_SUPPLY_PERCENT,
        world_supply_percent: int = 100,
        death_supply_percent: int = DEATH_SUPPLY_PERCENT,
        cold_supply_percent: int = COLD_SUPPLY_PERCENT,
        ) -> int:
    """Return 6.36 ``0x152470(province, nation)`` on the decoded path."""
    try:
        province = provinces[province_id]
    except KeyError as exc:
        raise ValueError(f"province {province_id} is absent") from exc
    if province.population < 0:
        raise ValueError("population cannot be negative")

    value = _trunc_div(base_supply_percent * 500, 100)
    value = _trunc_div(value * world_supply_percent, 100)

    if not province.no_death_supply:
        value = _trunc_div(
            value * (100 + death_supply_percent * province.growth_scale),
            100)

    temperature_distance = abs(
        province.heat_scale - province.preferred_heat_scale)
    value = _trunc_div(
        value * (100 - cold_supply_percent * temperature_distance), 100)

    population_tenths = _trunc_div(province.population, 10)
    if population_tenths <= 1_499:
        value = _trunc_div(value * population_tenths, 1_500)
    else:
        value += _trunc_div(
            value * (population_tenths - 1_500), 3_000)

    admin_matches = province.administrative_owner == nation_id
    owner_matches = province.owner_nation_id == nation_id
    if admin_matches:
        value += province.site_supply_bonus + 10
        if not owner_matches:
            if province.fort_type <= 0:
                return 0
            divisor = max(province.fort_supply_divisor, 1)
            return _trunc_div(_fort_supply_storage(province), divisor)
    else:
        value += 10
        if owner_matches and province.fort_type > 0:
            administration = _fort_administration_percent(
                province.fort_type, province.heat_scale,
                province.fort_administration_override)
            value -= _trunc_div(value * administration, 200)

    return value + province_supply_line(province_id, nation_id, provinces)


# ---------------------------------------------------------------------------
# Recruitment points -- 0x46d8f0
# ---------------------------------------------------------------------------

#: Population brackets and the divisor applied within each, straight from the
#: cmp/cmovle chain at 0x46d941 onwards. Below 5,000 people count in full;
#: each band above that contributes progressively less.
BRACKETS = ((5_000, 1), (10_000, 2), (20_000, 3), (40_000, 4), (140_000, 5))

#: Added unconditionally after the first bracket — `add $0x7d0,%edx`.
BASE_OFFSET = 2_000

#: The whole total is divided by this at the end, via the reciprocal
#: multiply at 0x46d9f1. Confirmed by Kratas and Citala coming out exact.
FINAL_DIVISOR = 100


def holy_point_allowance(base_dominion: int, shared_temple_count: int, *,
                         disciple_nations: int = 1,
                         own_temple_count: int | None = None,
                         temple_holy_point_bonus: int = 0) -> int:
    """Holy recruitment points available in each administered province.

    This transcribes 6.36 ``0x46a9a0`` and the province-panel continuation at
    ``0x15cfa0``. Every five temples raise maximum Dominion by one. In a
    disciple game the shared temple pool is divided by five times the number
    of nations sharing the god. The calculator clamps that Dominion component
    to 0..20. Nations with ``#templeholypoints`` then add its value once for
    each of *their own* temples; that panel addition is not clamped to 20.
    """
    if shared_temple_count < 0:
        raise ValueError("temple count cannot be negative")
    if disciple_nations < 1:
        raise ValueError("disciple_nations must be at least one")
    if own_temple_count is None:
        own_temple_count = shared_temple_count
    if own_temple_count < 0:
        raise ValueError("own temple count cannot be negative")
    dominion_component = base_dominion + (
        shared_temple_count // (5 * disciple_nations))
    dominion_component = min(max(dominion_component, 0), 20)
    return (dominion_component
            + temple_holy_point_bonus * own_temple_count)


def recruitment_points_base(population: int) -> int:
    """Recruitment points before fort, ownership and nation modifiers.

    Exact for a foreign province with no fort. Everything else needs a
    multiplier this does not yet know.
    """
    if population < 0:
        raise ValueError("population cannot be negative")
    total = min(population, BRACKETS[0][0]) + BASE_OFFSET
    previous = BRACKETS[0][0]
    for ceiling, divisor in BRACKETS[1:]:
        if population <= previous:
            break
        total += (min(population, ceiling) - previous) // divisor
        previous = ceiling
    return total // FINAL_DIVISOR


#: Recruitment points a fort multiplies its province by, and the commander
#: points it adds. These are the fort screen's own stated figures, not values
#: fitted to the observations: a citadel states a "125% recruitment point
#: value" and gives 2 commander points, a palisade states +50% and gives none.
#: Tiers we have never held are absent rather than interpolated.
FORT_MULTIPLIER = {0: 1.0, 1: 1.5, 4: 2.25}

#: Commander points before the fort's contribution.
BASE_COMMANDER_POINTS = 1
FORT_COMMANDER_POINTS = {0: 0, 1: 0, 4: 2}

#: Gain per step of the Order scale, positive or negative. Applies only in a
#: province we own; see the module docstring.
ORDER_PER_STEP = 0.10


def estimate_recruitment_points(population: int, *, fort_type: int = 0,
                                is_ours: bool = False,
                                order_scale: int = 0) -> dict[
                                    str, int | bool | str | None]:
    """Recruitment and commander points, with `exact` saying whether to trust it.

    Deliberately a dict rather than a number, so that a caller cannot mistake
    an estimate for the figure the game will show. `exact` is true for a
    province we own whose fort tier is one we have measured, and for a foreign
    province with no fort; it is false otherwise, and then `points` is a base
    that a fort can multiply by nearly three.
    """
    base = recruitment_points_base(population)
    if is_ours:
        multiplier = FORT_MULTIPLIER.get(fort_type)
        if multiplier is None:
            return {
                "points": base, "commander_points": None, "exact": False,
                "note": (f"fort type {fort_type} has never been held, so its "
                         "multiplier is unmeasured. This is the unmultiplied "
                         "base. Read the panel and record it."),
            }
        points = int(base * multiplier * (1 + ORDER_PER_STEP * order_scale))
        return {
            "points": points,
            "commander_points": (BASE_COMMANDER_POINTS
                                 + FORT_COMMANDER_POINTS[fort_type]),
            "exact": True,
            "note": "base x fort x order; exact on every province we own",
        }
    # Never exact for a foreign province. The base is right for two of the
    # three we can see — Citala 81 and Kratas 116 — and wrong for the third:
    # Jinport shows 145 against a base of 162. Its order scale is -1 where the
    # others are 2, and its dominion strength is 3 where the others are 6, and
    # a single province cannot say which of those is responsible. Reporting
    # the two that fit as exact would be fitting the sample, not the game.
    #
    # This costs nothing to play with: recruitment happens in provinces we
    # hold, and those are exact.
    return {
        "points": base, "commander_points": None, "exact": False,
        "note": ("foreign province — this is the unmultiplied base. It is "
                 "exact on two of the three we have measured and 12% high on "
                 "the third, cause not established. Do not plan against it."),
    }
