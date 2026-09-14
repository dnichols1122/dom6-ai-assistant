from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.file_reader.formats.pretender import Awakening, parse_bytes
from dom6_assistant.reference.pretender_design import (
    BLESSINGS,
    PretenderDesignError,
    available_chassis,
    blessing_points,
    chassis_cost,
    scale_limits,
    cost_tables,
    design_cost,
    dominion_cost,
    get_chassis,
    paths_cost,
    scales_cost,
    validate_blessings,
)
from ..pretender_fixtures import SUGAAR, WEEPING_ONE

REFERENCE_DB = Path(__file__).resolve().parents[2] / "knowledge/reference/reference.sqlite3"


@pytest.fixture
def reference() -> sqlite3.Connection:
    connection = sqlite3.connect(REFERENCE_DB)
    yield connection
    connection.close()


def test_sugaar_spends_exactly_all_design_and_bless_points(reference):
    saved = parse_bytes(SUGAAR)

    cost = design_cost(
        reference,
        saved.nation_id,
        saved.chassis_id,
        saved.awakening,
        saved.dominion,
        saved.paths,
        saved.scales,
    )
    assert (cost.point_pool, cost.chassis, cost.dominion, cost.paths, cost.scales) == (
        600, 170, 70, 240, 120,
    )
    assert cost.spent == 600
    assert cost.remaining == 0

    selection = validate_blessings(
        reference, saved.nation_id, saved.paths, saved.scales, saved.blessing_ids
    )
    assert [blessing.name for blessing in selection.selected] == [
        "Death Explosion", "Righteous Wrath", "Fire Resistance", "Fire Resistance",
    ]
    assert selection.points_spent == selection.points_available == 13


def test_client_bless_ids_are_not_stale_database_sequence_numbers(reference):
    saved = parse_bytes(WEEPING_ONE)
    selection = validate_blessings(
        reference, saved.nation_id, saved.paths, saved.scales, saved.blessing_ids
    )

    assert [blessing.name for blessing in selection.selected] == [
        "Enchanted Blood",
        "Blood Surge",
        "Strong Vitae",
        "Strong Vitae",
        "Strong Vitae",
        "Strong Vitae",
        "Strong Vitae",
        "Resilient",
    ]
    assert selection.points_spent == selection.points_available == 14


def test_national_bless_bonus_is_included(reference):
    # MA Marignon's client attribute 329 adds three points.
    assert blessing_points(reference, 61, {"fire": 6, "air": 6}) == 13


def test_path_cost_uses_chassis_base_levels_and_new_path_price(reference):
    jade_emperor = get_chassis(reference, 22, 905)

    assert jade_emperor.base_paths == {"air": 1, "water": 1, "astral": 1}
    assert paths_cost(
        jade_emperor, {"air": 5, "water": 3, "astral": 8}
    ) == 328
    # A new path to three costs 60 to open, then 16 and 24.
    assert paths_cost(jade_emperor, {**jade_emperor.base_paths, "fire": 3}) == 100


def test_temperature_cost_uses_national_preference_and_neutral_cap(reference):
    neutral = {name: 0 for name in ("order", "productivity", "heat", "growth", "luck", "magic")}

    assert scales_cost(reference, 61, {**neutral, "heat": 3}) == -120
    assert scales_cost(reference, 61, {**neutral, "heat": -3}) == -120
    # MA Caelum's Cold 3 preference is free; moving to neutral grants 120.
    assert scales_cost(reference, 71, {**neutral, "heat": -3}) == 0
    assert scales_cost(reference, 71, neutral) == -120
    # Crossing to Heat is allowed but gives no refund beyond reaching neutral.
    assert scales_cost(reference, 71, {**neutral, "heat": 3}) == -120


def test_availability_canonicalizes_shape_pairs_and_honours_exclusions(reference):
    marignon = {chassis.id: chassis for chassis in available_chassis(reference, 61)}

    assert marignon[3894].name == "Serpent of Heavenly Fires"
    assert marignon[3710].name == "Dragon"
    assert 3711 not in marignon
    assert 812 not in marignon  # explicit unpretender entry


def test_client_only_eligibility_rejections_are_not_offered(reference):
    marignon = {chassis.id for chassis in available_chassis(reference, 61)}
    pelagia = {chassis.id for chassis in available_chassis(reference, 125)}

    assert 179 not in marignon  # Master Lich is in the realm candidate superset.
    assert 2848 not in marignon  # Father of the Sea is restricted to MA Ys.
    assert 248 not in pelagia  # The client rejects terrestrial Arch Mage here.


def test_illegal_design_and_blessing_are_rejected(reference):
    saved = parse_bytes(SUGAAR)

    with pytest.raises(PretenderDesignError, match="overspends"):
        design_cost(
            reference, 61, 3894, Awakening.AWAKE, 10,
            {"fire": 10, "air": 10}, saved.scales,
        )
    with pytest.raises(PretenderDesignError, match="not stackable"):
        validate_blessings(reference, 61, saved.paths, saved.scales, [7, 7])
    with pytest.raises(PretenderDesignError, match="requires glamour 8"):
        validate_blessings(reference, 61, saved.paths, saved.scales, [80])


def test_all_client_blessing_ids_are_dense_and_named():
    assert set(BLESSINGS) == set(range(1, 93))
    assert BLESSINGS[91].name == "Awareness"
    assert BLESSINGS[92].name == "Heroism"


def test_published_cost_tables_cannot_drift_from_what_is_charged(reference):
    """The tables exist so the designer need not guess; they must be exact.

    They are generated from the same functions design_cost calls, and this
    pins that: a design priced from the published tables has to come to the
    same total the evaluator charges, for nations whose national adjustments
    differ -- a temperature preference, a Growth baseline, none at all.
    """
    import random

    random.seed(11)
    checked = 0
    for nation_id in (23, 61, 71, 13, 54, 16, 22):
        for chassis in random.sample(
            (options := available_chassis(reference, nation_id)),
            min(4, len(options)),
        ):
            for awakening in Awakening:
                if awakening < chassis.minimum_awakening:
                    continue
                tables = cost_tables(reference, nation_id, chassis, awakening)
                for _ in range(6):
                    dominion = random.randint(1, 10)
                    paths = dict(chassis.base_paths)
                    for path in random.sample(list(tables["paths"]), 3):
                        floor = max(chassis.base_paths.get(path, 0), 1)
                        paths[path] = random.randint(floor, 6)
                    # Only legal values are priced now, so sample from the
                    # range this nation and chassis actually allow.
                    scales = {
                        name: random.choice(sorted(prices))
                        for name, prices in tables["scales"]["delta_by_value"].items()
                    }

                    predicted = (
                        tables["chassis"][awakening.name.lower()]
                        + tables["dominion"]["by_value"][dominion]
                        + sum(
                            tables["paths"][path]["by_level"][level]["total"]
                            for path, level in paths.items()
                        )
                        + tables["scales"]["all_neutral_cost"]
                        + sum(
                            tables["scales"]["delta_by_value"][name][value]
                            for name, value in scales.items()
                        )
                    )
                    charged = (
                        chassis_cost(reference, nation_id, chassis, awakening)
                        + dominion_cost(chassis.start_dominion, dominion)
                        + paths_cost(chassis, paths)
                        + scales_cost(reference, nation_id, scales)
                    )
                    assert predicted == charged, (
                        f"nation {nation_id} chassis {chassis.id} {awakening.name}: "
                        f"tables say {predicted}, evaluator charges {charged}"
                    )

                    published = tables["bless_points"]
                    assert blessing_points(reference, nation_id, paths) == max(
                        0,
                        published["national_flat_bonus"]
                        + sum(
                            published["points_from_path_level"][path][level]
                            for path, level in paths.items()
                            if level >= 1
                        ),
                    )
                    checked += 1
    assert checked > 300


def test_scale_limits_take_the_higher_modifier_and_do_not_add(reference):
    """A modifier shifts the whole window and equal modifiers do not add.

    Nation attributes 640..645 hold the national half, in SCALE_NAMES order,
    with the sign choosing the direction: EA Yomi's 640 = -1 is the "Turmoil
    limit +1" its nation screen shows, which caps Order at 1.
    """
    oni = get_chassis(reference, 23, 2203)          # chassis moreorder -1 too
    limits = scale_limits(reference, 23, oni)
    order = limits["order"]
    assert order["nation_modifier"] == -1 and order["chassis_modifier"] == -1
    # Both shift in the same direction but do not stack into -4..0.
    assert (order["minimum"], order["maximum"]) == (-3, 1)
    # A scale neither side touches stays at the default.
    assert (limits["magic"]["minimum"], limits["magic"]["maximum"]) == (-2, 2)

    # MA Marignon raises Order; the Serpent raises Turmoil. Different
    # directions, so both apply.
    serpent = get_chassis(reference, 61, 3894)
    marignon = scale_limits(reference, 61, serpent)["order"]
    assert (marignon["minimum"], marignon["maximum"]) == (-3, 3)


def test_design_cost_refuses_a_scale_beyond_the_limit(reference):
    oni = get_chassis(reference, 23, 2203)
    paths = dict(oni.base_paths)
    legal = design_cost(reference, 23, 2203, Awakening.AWAKE, 3, paths,
                        {"order": -3})
    assert legal.spent  # Turmoil 3 is legal for Yomi
    with pytest.raises(PretenderDesignError, match="outside the legal range"):
        design_cost(reference, 23, 2203, Awakening.AWAKE, 3, paths,
                    {"order": 3})
    # The hole this closes: magic 3 has no modifier anywhere and used to pass.
    with pytest.raises(PretenderDesignError, match="outside the legal range"):
        design_cost(reference, 23, 2203, Awakening.AWAKE, 3, paths,
                    {"magic": 3})


def test_chassis_scale_limit_shifts_both_ends_of_the_window(reference):
    # These are the three configurations verified directly in the designer.
    dagon = scale_limits(reference, 43, get_chassis(reference, 43, 109))
    titan = scale_limits(reference, 43, get_chassis(reference, 43, 961))
    crystal = scale_limits(reference, 43, get_chassis(reference, 43, 3640))

    assert (dagon["productivity"]["minimum"],
            dagon["productivity"]["maximum"]) == (-3, 1)
    assert (titan["order"]["minimum"], titan["order"]["maximum"]) == (-3, 1)
    assert (crystal["magic"]["minimum"], crystal["magic"]["maximum"]) == (-1, 3)


def test_dagon_atlantis_design_prices_the_effective_productivity_limit(reference):
    paths = {"earth": 5, "water": 5}
    scales = {
        "order": 0, "productivity": 1, "heat": 0,
        "growth": 0, "luck": 0, "magic": 2,
    }
    cost = design_cost(
        reference, 43, 109, Awakening.DORMANT, 4, paths, scales,
    )
    assert (cost.point_pool, cost.chassis, cost.dominion, cost.paths, cost.scales) == (
        600, 300, 7, 128, 120,
    )
    assert cost.spent == 555
    assert cost.remaining == 45

    with pytest.raises(PretenderDesignError, match=r"legal range -3\.\.1"):
        design_cost(
            reference, 43, 109, Awakening.DORMANT, 4, paths,
            {**scales, "productivity": 2},
        )

    prices = cost_tables(reference, 43, get_chassis(reference, 43, 109),
                         Awakening.DORMANT)
    assert prices["scales"]["legal_range"]["productivity"]["maximum"] == 1
    assert 2 not in prices["scales"]["delta_by_value"]["productivity"]
