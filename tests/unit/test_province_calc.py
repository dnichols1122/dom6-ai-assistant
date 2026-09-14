"""Recruitment points, against the two provinces where nothing else applies.

The base formula is a transcription of the game's own bracket chain, so the
test that matters is whether it reproduces a province with no fort and no
ownership — where every multiplier is 1 and the base IS the answer.
"""
from dataclasses import replace

import pytest

from dom6_assistant.reference.province_calc import (
    BATTLE_LIGHT_PHASE, CAVE, DEEP_SEA, GATEWAY, INFERNAL_WASTE,
    GlobalResourceEffects, LightingEffect, ProvinceIncomeState,
    ProvinceResourceState, ProvinceSupplyState,
    adjacent_fort_draw_percent,
    estimate_recruitment_points, fort_administration_percent,
    global_resource_effects, province_local_resources,
    holy_point_allowance,
    province_income, province_lighting_level, province_resource_total,
    province_supplies, province_supply_line, recruitment_points_base,
    twilight_state_applies, unit_resource_bonuses,
)


def test_holy_allowance_uses_temple_thresholds_and_clamps_dominion():
    assert holy_point_allowance(6, 2) == 6
    assert holy_point_allowance(6, 5) == 7
    assert holy_point_allowance(19, 25) == 20


def test_disciple_temples_use_a_larger_shared_divisor():
    # Three nations sharing one god need fifteen shared temples per increase.
    assert holy_point_allowance(6, 14, disciple_nations=3) == 6
    assert holy_point_allowance(6, 15, disciple_nations=3) == 7


def test_temple_holy_point_nation_bonus_is_added_after_dominion_clamp():
    assert holy_point_allowance(
        20, 10, own_temple_count=4, temple_holy_point_bonus=1) == 24


def _income_state(**changes):
    values = dict(
        province_id=93, population=10_000, unrest=0,
        owner_nation_id=61, administrative_owner=61,
        dominion_owner=61, dominion_strength=6,
        order_scale=0, productivity_scale=0, heat_scale=0,
        growth_scale=0, luck_scale=0,
    )
    values.update(changes)
    return ProvinceIncomeState(**values)


@pytest.mark.parametrize(
    ("province", "expected"),
    [
        (_income_state(province_id=86, population=6_920,
                       order_scale=2, productivity_scale=1,
                       fort_type=0), 75),
        (_income_state(province_id=93, population=39_650,
                       order_scale=2, productivity_scale=1,
                       fort_type=4), 560),
        (_income_state(province_id=98, population=4_630, unrest=2,
                       order_scale=2, productivity_scale=1,
                       fort_type=2), 53),
    ],
)
def test_income_matches_all_direct_live_636_calls(province, expected):
    assert province_income(province, 61) == expected


def test_income_percentages_truncate_in_executable_order():
    # 396 + half of 60% administration = 514; Order then Productivity.
    state = _income_state(
        population=39_650, fort_type=4, order_scale=2,
        productivity_scale=1)
    assert province_income(state, 61) == 560


def test_income_site_and_fort_nation_bonuses_are_additive():
    state = _income_state(
        population=10_000, fort_type=2, terrain_flags=CAVE,
        is_coastal=True, site_province_income=10, site_bring_gold=20,
        nation_trade_coast_percent=10, nation_cave_income_percent=20)
    # Pre-fort 130; +19 half-admin, +13 coast, +26 cave.
    assert province_income(state, 61) == 188

    storms = GlobalResourceEffects(perpetual_storm=True)
    # The storm suppresses Trade Coast, then its land-only 80% penalty is
    # skipped because this is a Cave province.
    assert province_income(state, 61, global_effects=storms) == 175


def test_income_scale_dominion_temperature_luck_and_unrest_paths():
    hostile = _income_state(
        dominion_owner=87, order_scale=2, productivity_scale=-1,
        growth_scale=3, heat_scale=2, preferred_heat_scale=-1,
        luck_scale=4, unrest=50)
    # Beneficial Order/Growth are ignored under hostile dominion. Sloth,
    # temperature distance, extreme Luck and unrest remain sequential.
    assert province_income(hostile, 61) == 38

    reduced = replace(hostile, reduced_temperature_income=True)
    assert province_income(reduced, 61) == 43


def test_income_global_enchantments_follow_dominion_and_precedence():
    state = _income_state(
        population=10_000, site_bring_gold=25, is_coastal=True)
    growth = GlobalResourceEffects(
        riches_from_beneath_caster=61,
        gift_of_natures_bounty_caster=61,
        trade_wind_provinces=frozenset({93}),
    )
    # Base 125; Riches adds 25% of income plus the mine's 25; Bounty adds
    # 15% per candle (six candles here); Trade Wind adds one quarter.
    assert province_income(state, 61, global_effects=growth) == 415

    dark = GlobalResourceEffects(
        utterdark=True, theft_of_the_sun=True, second_sun=True)
    # Utterdark takes precedence and Second Sun cannot cancel it.
    assert province_income(state, 61, global_effects=dark) == 12
    theft_cancelled = GlobalResourceEffects(
        theft_of_the_sun=True, second_sun=True)
    assert province_income(state, 61, global_effects=theft_cancelled) == 125


def test_income_ownership_tail_uses_administrative_owner():
    besieged_owner = _income_state(
        fort_type=2, administrative_owner=87)
    # No local fort bonus; owner retains 70% after the 30-admin fort split.
    assert province_income(besieged_owner, 61) == 70

    occupying_admin = replace(
        besieged_owner, owner_nation_id=87, administrative_owner=61)
    # The administrative controller receives the fort's 30% share. Its own
    # fort-income bonus is applied first: 100 + 15 = 115, then 30% = 34.
    assert province_income(occupying_admin, 61) == 34


SUPPLY_PROVINCES = {
    86: ProvinceSupplyState(
        86, 6_920, 61, 61, 0, 0, neighbours=(83, 91, 93)),
    93: ProvinceSupplyState(
        93, 39_650, 61, 61, 0, 0, fort_type=4,
        neighbours=(83, 86, 91, 98)),
    98: ProvinceSupplyState(
        98, 4_630, 61, 61, 0, 0, fort_type=2,
        neighbours=(83, 93)),
}


@pytest.mark.parametrize(
    ("province_id", "expected"), [(86, 420), (93, 1_280), (98, 344)])
def test_supplies_match_all_direct_live_636_calls(province_id, expected):
    assert province_supplies(province_id, 61, SUPPLY_PROVINCES) == expected


def test_supply_line_projects_best_fort_by_distance_not_sum():
    assert province_supply_line(93, 61, SUPPLY_PROVINCES) == 360
    assert province_supply_line(86, 61, SUPPLY_PROVINCES) == 180
    # The local Fortress gives 180 and the adjacent Citadel also gives
    # trunc(360 / 2) = 180; they are not added together.
    assert province_supply_line(98, 61, SUPPLY_PROVINCES) == 180


def test_supply_line_stops_at_wall_links():
    copper = replace(
        SUPPLY_PROVINCES[86], blocked_supply_neighbours=frozenset({93}))
    marignon = replace(
        SUPPLY_PROVINCES[93], blocked_supply_neighbours=frozenset({86}))
    states = {**SUPPLY_PROVINCES, 86: copper, 93: marignon}
    assert province_supply_line(86, 61, states) == 0
    assert province_supplies(86, 61, states) == 240


def test_supply_growth_temperature_population_and_site_order():
    state = ProvinceSupplyState(
        1, 15_000, 61, 61, growth_scale=1, heat_scale=1,
        site_supply_bonus=25)
    # 500 -> Growth 550 -> one climate step 495 -> population 495,
    # then the universal 10 and site 25.
    assert province_supplies(1, 61, {1: state}) == 530

    no_death = replace(state, no_death_supply=True)
    assert province_supplies(1, 61, {1: no_death}) == 485


def test_supply_population_uses_15000_then_half_rate_above_it():
    low = ProvinceSupplyState(1, 7_500, 61, 61, 0, 0)
    high = replace(low, population=45_000)
    assert province_supplies(1, 61, {1: low}) == 260
    # 500 + (4500-1500)*500/3000, then the universal 10.
    assert province_supplies(1, 61, {1: high}) == 1_010


def test_split_fort_ownership_uses_storage_or_owner_share():
    admin = ProvinceSupplyState(
        1, 10_000, 87, 61, 0, 0, fort_type=2)
    assert province_supplies(1, 61, {1: admin}) == 750
    assert province_supplies(
        1, 61, {1: replace(admin, fort_supply_divisor=3)}) == 250

    owner = replace(admin, owner_nation_id=61, administrative_owner=87)
    # Population path 333 + 10, less half the Fortress's 30 administration.
    assert province_supplies(1, 61, {1: owner}) == 292


# Live 6.35 calibration set. Each raw value is `.trn` body+32 and every other
# input also comes from the turn file. Neighbour lists are reduced to the six
# measured provinces; omitted neighbours are foreign and cannot share resources.
RESOURCE_PROVINCES = {
    83: ProvinceResourceState(83, 38, 7_320, 0, 0, 61, 6, 2, 1,
                              neighbours=(86, 93, 98)),
    86: ProvinceResourceState(86, 88, 6_880, 0, 61, 61, 6, 2, 1,
                              neighbours=(83, 91, 93)),
    91: ProvinceResourceState(91, 182, 16_400, 97, 0, 61, 6, 2, 1,
                              neighbours=(86, 93, 96)),
    93: ProvinceResourceState(93, 119, 39_650, 0, 61, 61, 6, 2, 1,
                              fort_type=4, neighbours=(83, 86, 91, 98)),
    96: ProvinceResourceState(96, 73, 33_490, 0, 0, 61, 3, -1, 1,
                              neighbours=(91,)),
    98: ProvinceResourceState(98, 96, 4_680, 2, 61, 61, 6, 2, 1,
                              fort_type=1, neighbours=(83, 93)),
}


@pytest.mark.parametrize(
    ("province_id", "nation_id", "expected"),
    [
        (83, 0, 38), (86, 0, 88), (91, 0, 92),
        (93, 0, 119), (96, 0, 70), (98, 0, 94),
        (83, 61, 46), (86, 61, 106), (91, 61, 111),
        (93, 61, 144), (96, 61, 80), (98, 61, 113),
    ],
)
def test_local_resources_match_direct_live_function_calls(
        province_id, nation_id, expected):
    assert province_local_resources(
        RESOURCE_PROVINCES[province_id], nation_id) == expected


@pytest.mark.parametrize(
    ("province_id", "nation_id", "expected"),
    [
        (83, 0, 19),       # ordinary fortless province: half local
        (91, 0, 46),       # 182 raw / 197% unrest divisor, then half
        (96, 0, 35),       # hostile dominion: Sloth applies, Prod does not
        (86, 61, 42),      # Citadel draws 60%, leaving 40%
        (93, 61, 207),     # 144 local + 60% of Copper's 106
        (98, 61, 113),     # own Palisades; fortified neighbours contribute 0
    ],
)
def test_total_resources_match_every_observed_panel(
        province_id, nation_id, expected):
    assert province_resource_total(
        province_id, nation_id, RESOURCE_PROVINCES) == expected


def test_order_and_productivity_truncate_sequentially():
    # The binary produces 119 -> floor(126.14) -> floor(144.9) = 144.
    # Combining the percentages into 121% would incorrectly produce 143.
    assert province_local_resources(RESOURCE_PROVINCES[93], 61) == 144


def test_adjacent_fort_draw_is_the_sum_of_friendly_forts():
    assert adjacent_fort_draw_percent(
        RESOURCE_PROVINCES[86], RESOURCE_PROVINCES) == 60
    assert adjacent_fort_draw_percent(
        RESOURCE_PROVINCES[93], RESOURCE_PROVINCES) == 15


def test_flat_resource_attributes_are_added_after_unrest():
    kratas = replace(RESOURCE_PROVINCES[91], site_resource_bonus=10)
    # Base reaches 111 after the unrest divisor for nation 61, then +10.
    assert province_local_resources(kratas, 61) == 121


def test_unit_resource_bonus_is_added_only_in_a_fort():
    capital = replace(RESOURCE_PROVINCES[93], unit_resource_bonus=25)
    provinces = {**RESOURCE_PROVINCES, 93: capital}
    assert province_resource_total(93, 61, provinces) == 232

    copper = replace(RESOURCE_PROVINCES[86], unit_resource_bonus=25)
    provinces = {**RESOURCE_PROVINCES, 86: copper}
    assert province_resource_total(86, 61, provinces) == 42


def test_mining_bonus_requires_cave_terrain_or_a_mine_site():
    capital = replace(
        RESOURCE_PROVINCES[93], unit_mining_resource_bonus=25)
    provinces = {**RESOURCE_PROVINCES, 93: capital}
    assert province_resource_total(93, 61, provinces) == 207

    cave = replace(capital, is_cave=True)
    provinces = {**RESOURCE_PROVINCES, 93: cave}
    assert province_resource_total(93, 61, provinces) == 232

    gold_mine = replace(capital, site_bring_gold_bonus=30)
    provinces = {**RESOURCE_PROVINCES, 93: gold_mine}
    assert province_resource_total(93, 61, provinces) == 232

    iron_mine = replace(capital, site_bring_resource_bonus=60)
    provinces = {**RESOURCE_PROVINCES, 93: iron_mine}
    # bringres adds 60 to local before the mining ability adds another 25.
    assert province_resource_total(93, 61, provinces) == 292


def test_ice_forging_is_bonus_times_cold_level_in_any_fort():
    capital = replace(
        RESOURCE_PROVINCES[93], heat_scale=-3, unit_ice_forging_bonus=4)
    provinces = {**RESOURCE_PROVINCES, 93: capital}
    assert province_resource_total(93, 61, provinces) == 219

    hot = replace(capital, heat_scale=2)
    provinces = {**RESOURCE_PROVINCES, 93: hot}
    assert province_resource_total(93, 61, provinces) == 207


def test_binary_unit_resource_ability_lookup():
    bonuses = unit_resource_bonuses(
        [325, 325, 1283, 1384, 3890, 3891, 99999])
    assert bonuses.resources == 20
    assert bonuses.mining_resources == 30
    assert bonuses.ice_forging == 4
    assert bonuses.counts_as_sun is True


def test_parsed_effect_records_select_only_resource_globals():
    from dom6_assistant.file_reader.formats.trn import TrnGlobalEffect

    def record(effect_id, caster):
        return TrnGlobalEffect(effect_id, 1, caster, 93, 1, 1, 0)

    effects = global_resource_effects(
        [record(0x23, 61), record(0x38, 76), record(0x65, 81),
         record(0x76, 87), record(0x8a, 87)])
    assert effects == GlobalResourceEffects(
        riches_from_beneath_caster=61,
        utterdark=True,
        theft_of_the_sun=True,
        eternal_twilight_caster=87,
        lighting_effects=(
            LightingEffect(0x23, 1, 93, 1),
            LightingEffect(0x38, 1, 93, 1),
            LightingEffect(0x65, 1, 93, 1),
            LightingEffect(0x76, 1, 93, 1),
            LightingEffect(0x8a, 1, 93, 1),
        ),
    )


def _lighting_effect(effect_id, *, spell_id=1, province=93, value1=1):
    return LightingEffect(effect_id, spell_id, province, value1)


def test_lighting_invalid_and_gateway_provinces_return_normal_light():
    utterdark = [_lighting_effect(0x38)]
    assert province_lighting_level(-1, 0, utterdark) == 0
    assert province_lighting_level(93, GATEWAY, utterdark) == 0


@pytest.mark.parametrize("spell_id", [1188, 1224])
def test_local_light_spell_has_absolute_precedence(spell_id):
    effects = [
        _lighting_effect(0x38),
        _lighting_effect(0x4D),
        _lighting_effect(999, spell_id=spell_id),
    ]
    assert province_lighting_level(
        93, DEEP_SEA | CAVE | INFERNAL_WASTE, effects) == -1


@pytest.mark.parametrize(
    ("effects", "expected"),
    [
        ([], 0),
        ([_lighting_effect(0x38)], 2),                 # Utterdark
        ([_lighting_effect(0x65)], 1),                 # Theft of the Sun
        ([_lighting_effect(0x65), _lighting_effect(0x29)], 0),
        ([_lighting_effect(0x38), _lighting_effect(0x29)], 2),
        ([_lighting_effect(0x62, province=-2)], 1),    # worlddarkness
        ([_lighting_effect(0x38),
          _lighting_effect(0x62, province=-2)], 2),
        ([_lighting_effect(0x4D)], 2),                 # Darkness
        ([_lighting_effect(0x61)], 1),                 # Solar Eclipse
    ],
)
def test_lighting_effect_precedence(effects, expected):
    assert province_lighting_level(93, 0, effects) == expected


def test_nonpositive_runtime_values_do_not_activate_global_darkness():
    effects = [
        _lighting_effect(0x38, value1=0),
        _lighting_effect(0x65, value1=-1),
    ]
    assert province_lighting_level(93, 0, effects) == 0


@pytest.mark.parametrize("flag", [DEEP_SEA, CAVE, INFERNAL_WASTE])
def test_dark_terrain_floors_lighting_at_one(flag):
    assert province_lighting_level(93, flag, []) == 1


def test_fire_storm_subtracts_one_only_outside_battle():
    fire_storm = [_lighting_effect(0x19)]
    assert province_lighting_level(93, 0, fire_storm) == -1
    assert province_lighting_level(93, CAVE, fire_storm) == 0
    assert province_lighting_level(
        93, 0, fire_storm, battle_province_id=12) == 0


def test_battle_phase_flag_reverses_after_time_149():
    assert province_lighting_level(
        93, BATTLE_LIGHT_PHASE, [],
        battle_province_id=93, battle_time=149) == 1
    assert province_lighting_level(
        93, BATTLE_LIGHT_PHASE, [],
        battle_province_id=93, battle_time=150) == 0
    assert province_lighting_level(
        93, 0, [], battle_province_id=93, battle_time=149) == 0
    assert province_lighting_level(
        93, 0, [], battle_province_id=93, battle_time=150) == 1


def test_battle_counts_as_sun_subtracts_one_from_final_level():
    assert province_lighting_level(
        93, CAVE, [], battle_province_id=93,
        battle_counts_as_sun=1) == 0
    assert province_lighting_level(
        12, 0, [], battle_province_id=93,
        battle_counts_as_sun=1) == -1


def test_full_twilight_predicate_rejects_wrong_light_terrain_and_plane():
    eternal_twilight = [_lighting_effect(0x76, value1=80)]
    assert twilight_state_applies(93, 0, eternal_twilight)
    assert not twilight_state_applies(
        93, 0, eternal_twilight + [_lighting_effect(0x65)])
    assert not twilight_state_applies(93, CAVE, eternal_twilight)
    assert not twilight_state_applies(
        93, 0, eternal_twilight, is_astral_plane=True)


def test_full_twilight_predicate_includes_local_spell_and_battle_window():
    assert twilight_state_applies(93, 0, [_lighting_effect(0x73)])
    assert twilight_state_applies(
        93, 0, [], battle_province_id=93, battle_time=100)
    assert twilight_state_applies(
        93, 0, [], battle_province_id=93, battle_time=149)
    assert not twilight_state_applies(
        93, 0, [], battle_province_id=93, battle_time=150)


def test_resource_path_computes_twilight_predicate_from_trn_effects():
    from dom6_assistant.file_reader.formats.trn import TrnGlobalEffect

    province = replace(
        RESOURCE_PROVINCES[93], raw_resources=119, order_scale=0,
        productivity_scale=0)
    record = TrnGlobalEffect(0x76, 1, 87, 93, 1, 80, 0)
    effects = global_resource_effects([record])
    assert province.twilight_applies is None
    assert province_local_resources(
        province, 61, global_effects=effects) == 96

    dark = global_resource_effects([
        record, TrnGlobalEffect(0x65, 517, 81, 93, 1, 70, 0)])
    # Theft applies 70%; its nonzero lighting state prevents Twilight stacking.
    assert province_local_resources(province, 61, global_effects=dark) == 83


def test_riches_from_beneath_adds_ten_percent_per_candle_capped_at_five():
    province = replace(
        RESOURCE_PROVINCES[93], raw_resources=100, dominion_owner=61,
        dominion_strength=9, order_scale=0, productivity_scale=0)
    effects = GlobalResourceEffects(riches_from_beneath_caster=61)
    assert province_local_resources(
        province, 61, global_effects=effects) == 150

    hostile = replace(province, dominion_owner=76)
    assert province_local_resources(
        hostile, 61, global_effects=effects) == 100


def test_utterdark_and_theft_of_the_sun_stack_in_sequence():
    province = replace(
        RESOURCE_PROVINCES[93], raw_resources=119, order_scale=0,
        productivity_scale=0)
    effects = GlobalResourceEffects(utterdark=True, theft_of_the_sun=True)
    # 119 / 10 = 11, then 11 * 70 / 100 = 7.
    assert province_local_resources(
        province, 61, global_effects=effects) == 7


@pytest.mark.parametrize("terrain_flags", [0x800, 0x1000, 0x1800])
def test_deep_sea_and_cave_ignore_utterdark_and_theft(terrain_flags):
    province = replace(
        RESOURCE_PROVINCES[93], raw_resources=100, order_scale=0,
        productivity_scale=0, terrain_flags=terrain_flags)
    effects = GlobalResourceEffects(utterdark=True, theft_of_the_sun=True)
    assert province_local_resources(
        province, 61, global_effects=effects) == 100


def test_counts_as_sun_suppresses_all_darkness_resource_penalties():
    province = replace(
        RESOURCE_PROVINCES[93], raw_resources=100, order_scale=0,
        productivity_scale=0, counts_as_sun=True,
        twilight_applies=True)
    effects = GlobalResourceEffects(
        utterdark=True, theft_of_the_sun=True,
        eternal_twilight_caster=87)
    assert province_local_resources(
        province, 61, global_effects=effects) == 100


def test_eternal_twilight_ignores_diplomacy_but_keeps_caster_exception():
    province = replace(
        RESOURCE_PROVINCES[93], raw_resources=119, order_scale=0,
        productivity_scale=0, twilight_applies=True)
    effects = GlobalResourceEffects(eternal_twilight_caster=87)
    # Exact assembly is 119 - trunc(119 / 5), which is 96 rather than 95.
    assert province_local_resources(
        province, 61, global_effects=effects) == 96

    foreign_in_caster_dominion = replace(
        province, owner_nation_id=61, dominion_owner=87,
        dominion_strength=4)
    # A NAP or other diplomatic relation is not an input to global effects.
    # Foreign provinces remain affected even under the caster's dominion.
    assert province_local_resources(
        foreign_in_caster_dominion, 61, global_effects=effects) == 96

    caster_owned = replace(foreign_in_caster_dominion, owner_nation_id=87)
    assert province_local_resources(
        caster_owned, 61, global_effects=effects) == 119


def test_full_darkness_prevents_eternal_twilight_from_stacking():
    province = replace(
        RESOURCE_PROVINCES[93], raw_resources=119, order_scale=0,
        productivity_scale=0, twilight_applies=False)
    effects = GlobalResourceEffects(
        utterdark=True, theft_of_the_sun=True,
        eternal_twilight_caster=87)
    # 119 / 10 -> 11; 11 * 70% -> 7. The extra twilight fifth is not taken.
    assert province_local_resources(
        province, 61, global_effects=effects) == 7


@pytest.mark.parametrize(("luck", "expected"), [(3, 100), (4, 95), (5, 85)])
def test_extreme_luck_resource_penalty(luck, expected):
    province = replace(
        RESOURCE_PROVINCES[93], raw_resources=100, order_scale=0,
        productivity_scale=0, luck_scale=luck)
    assert province_local_resources(province, 61) == expected


def test_unknown_fort_type_refuses_to_guess():
    province = replace(RESOURCE_PROVINCES[93], fort_type=99)
    with pytest.raises(ValueError, match="unknown fort type"):
        province_resource_total(93, 61, {**RESOURCE_PROVINCES, 93: province})


@pytest.mark.parametrize(
    ("heat_scale", "expected"),
    [(-3, 15), (0, 11), (3, 6)],
)
def test_ice_fort_administration_tracks_temperature(heat_scale, expected):
    ice_walls = replace(
        RESOURCE_PROVINCES[98], fort_type=20, heat_scale=heat_scale)
    assert fort_administration_percent(ice_walls) == expected


@pytest.mark.parametrize("sea_flags", [4, 4 | 2 | 2048])
def test_forts_do_not_draw_across_land_sea_boundary(sea_flags):
    copper = replace(RESOURCE_PROVINCES[86], terrain_flags=sea_flags)
    provinces = {**RESOURCE_PROVINCES, 86: copper}
    assert province_resource_total(93, 61, provinces) == 144


def test_map_border_bit_four_blocks_resource_draw():
    capital = replace(
        RESOURCE_PROVINCES[93], blocked_resource_neighbours=frozenset({86}))
    provinces = {**RESOURCE_PROVINCES, 93: capital}
    assert province_resource_total(93, 61, provinces) == 144

#: Foreign, fortless, and therefore unmodified. Read off the panel at turn 25.
UNMODIFIED = {"Kratas": (16400, 116), "Citala": (7320, 81)}

#: Provinces we own, with the fort tier and Order scale that apply. The model
#: is exact on all of these, at turn 24 and turn 25 alike.
OURS = {
    "Copper Canyons": (6880, 0, 2, 94, 1),
    "Obsidian Waste": (4680, 1, 2, 118, 1),
    "Marignon": (39650, 4, 2, 477, 3),
}


@pytest.mark.parametrize("name", sorted(UNMODIFIED))
def test_base_is_exact_where_nothing_modifies_it(name):
    population, expected = UNMODIFIED[name]
    assert recruitment_points_base(population) == expected


@pytest.mark.parametrize("name", sorted(OURS))
def test_a_province_we_own_is_exact(name):
    """base x fort x order, with no parameter fitted to these numbers.

    The citadel's 2.25 is the fort screen's stated 125% recruitment value and
    the palisade's 1.5 its stated +50%; 0.10 per step of Order is the game's
    published figure. Three provinces, three fort tiers, two turns.
    """
    population, fort, order, expected, commanders = OURS[name]
    out = estimate_recruitment_points(population, fort_type=fort,
                                      is_ours=True, order_scale=order)
    assert out["points"] == expected
    assert out["commander_points"] == commanders
    assert out["exact"] is True


def test_a_foreign_province_is_never_claimed_exact():
    """Jinport is why. It reads 145 against a base of 162, while Citala and
    Kratas match their base to the unit — and its Order scale and its dominion
    strength both differ from theirs, so one province cannot say which is
    responsible. Reporting the two that fit as exact would fit the sample.
    """
    for population in (7320, 16400, 33490):
        out = estimate_recruitment_points(population, fort_type=0,
                                          is_ours=False, order_scale=2)
        assert out["exact"] is False
        assert "Do not plan against it" in out["note"]


def test_an_unmeasured_fort_tier_refuses_to_guess():
    out = estimate_recruitment_points(10_000, fort_type=7, is_ours=True,
                                      order_scale=2)
    assert out["exact"] is False
    assert "never been held" in out["note"]


def test_the_order_scale_cuts_as_well_as_adds():
    low = estimate_recruitment_points(20_000, fort_type=0, is_ours=True,
                                      order_scale=-2)["points"]
    high = estimate_recruitment_points(20_000, fort_type=0, is_ours=True,
                                       order_scale=3)["points"]
    assert low < recruitment_points_base(20_000) < high


def test_brackets_are_monotonic():
    """More people never means fewer points."""
    values = [recruitment_points_base(p) for p in range(0, 200_000, 997)]
    assert all(b >= a for a, b in zip(values, values[1:]))


def test_the_top_bracket_saturates():
    """Above 140,000 the game stops counting, so the base must too."""
    assert recruitment_points_base(140_000) == recruitment_points_base(500_000)


def test_negative_population_is_refused():
    with pytest.raises(ValueError):
        recruitment_points_base(-1)
