"""Strategic ritual map range and forbidden target terrain."""

import sqlite3
from types import SimpleNamespace

import pytest

from dom6_assistant.reference.ritual_range import (
    province_is_coastal,
    province_distance,
    ritual_effect_numbers,
    spell_duration_extension_months_per_gem,
    spell_is_province_enchantment,
    spell_no_selector_mode,
    spell_province_range,
    spell_requires_coast,
    spell_source_requirements,
    spell_uses_implicit_local_target,
    validate_ritual_target,
    validate_ritual_source,
)


REFERENCE_DB = "knowledge/reference/reference.sqlite3"
CARRIER_EAGLE = 1285
TELEPORT_ITEM = 1320
TRADE_WIND = 1172
FROST_DOME = 1196
EARTH_SENSE = 1319
IRON_WALLS = 882
MIRAGE = 872
WEAVERS_OF_THE_WOOD = 343
GEOGLYPHS = 472
SUMMON_HOUND_OF_TWILIGHT = 254
TWICEBORN = 1159
DOME_OF_SOLID_AIR = 1193
DISTILL_GOLD = 759
RITUAL_OF_REBIRTH = 1229


def _province(
    province_id, neighbours=(), terrain=0, fort=0, sites=(), owner=1,
):
    return SimpleNamespace(
        province_id=province_id,
        neighbours=tuple(neighbours),
        current_terrain=terrain,
        fort_type=fort,
        sites=tuple(sites),
        owner_nation_id=owner,
    )


def test_extracted_item_transport_ranges_are_four_and_six():
    conn = sqlite3.connect(REFERENCE_DB)
    try:
        assert spell_province_range(conn, CARRIER_EAGLE) == 4
        assert spell_province_range(conn, TELEPORT_ITEM) == 6
    finally:
        conn.close()


def test_province_distance_uses_the_undirected_public_graph():
    provinces = [
        _province(1, (2,)),
        _province(2, (3,)),
        _province(3, (4,)),
        _province(4),
    ]
    assert province_distance(provinces, 1, 4) == 3
    assert province_distance(provinces, 4, 1) == 3


def test_range_and_forbidden_terrain_fail_closed():
    conn = sqlite3.connect(REFERENCE_DB)
    provinces = [
        _province(1, (2,)),
        _province(2, (3,)),
        _province(3, (4,)),
        _province(4, (5,)),
        _province(5, (6,)),
        _province(6, terrain=4),  # Sea; Carrier Eagle has #nogeodst 4.
    ]
    try:
        with pytest.raises(ValueError, match="beyond.*maximum range 4"):
            validate_ritual_target(
                conn, CARRIER_EAGLE, provinces, 1, 6, caster_nation_id=1)
        # Teleport Item reaches the same province and may target underwater.
        checked = validate_ritual_target(
            conn, TELEPORT_ITEM, provinces, 1, 6, caster_nation_id=1)
        assert (checked.distance, checked.maximum) == (5, 6)

        sea_in_range = [
            _province(1, (2,)),
            _province(2, terrain=4),
        ]
        with pytest.raises(ValueError, match="forbidden terrain flags 0x4"):
            validate_ritual_target(
                conn, CARRIER_EAGLE, sea_in_range, 1, 2,
                caster_nation_id=1)
    finally:
        conn.close()


def test_trade_wind_requires_a_publicly_confirmed_coast():
    conn = sqlite3.connect(REFERENCE_DB)
    provinces = [
        _province(1, (2,)),
        _province(2, (1,), terrain=4),
        _province(3, (99,)),
        _province(4),
    ]
    try:
        assert spell_requires_coast(conn, TRADE_WIND) is True
        assert province_is_coastal(provinces, 1) is True
        assert province_is_coastal(provinces, 3) is None
        assert province_is_coastal(provinces, 4) is False
        validate_ritual_source(conn, TRADE_WIND, provinces, 1)
        with pytest.raises(ValueError, match="not a legal coastal source"):
            validate_ritual_source(conn, TRADE_WIND, provinces, 4)
        with pytest.raises(ValueError, match="does not establish it as coastal"):
            validate_ritual_source(conn, TRADE_WIND, provinces, 3)
    finally:
        conn.close()


def test_effect_82_drives_target_shape_and_duration_rate():
    conn = sqlite3.connect(REFERENCE_DB)
    try:
        assert spell_is_province_enchantment(conn, TRADE_WIND)
        assert spell_uses_implicit_local_target(conn, TRADE_WIND)
        assert spell_uses_implicit_local_target(conn, IRON_WALLS)
        assert not spell_uses_implicit_local_target(conn, MIRAGE)
        assert spell_duration_extension_months_per_gem(conn, TRADE_WIND) == 1
        assert spell_duration_extension_months_per_gem(conn, FROST_DOME) == 1
        # Earth Sense carries attribute 709=3. The binary uses that value and
        # otherwise clamps the family to one month per extra gem.
        assert spell_duration_extension_months_per_gem(conn, EARTH_SENSE) == 3
        assert spell_duration_extension_months_per_gem(conn, CARRIER_EAGLE) is None
    finally:
        conn.close()


def test_every_extracted_effect_82_spell_is_classified_from_metadata():
    conn = sqlite3.connect(REFERENCE_DB)
    try:
        spell_ids = [
            int(row[0]) for row in conn.execute(
                "SELECT DISTINCT s.id FROM spells s JOIN effects_spells e "
                "ON e.record_id=s.effect_record_id "
                "WHERE CAST(e.ritual AS INTEGER)=1 AND e.effect_number=82"
            )
        ]
        assert len(spell_ids) == 34
        assert sum(
            spell_uses_implicit_local_target(conn, spell_id)
            for spell_id in spell_ids
        ) == 23
        assert {
            spell_duration_extension_months_per_gem(conn, spell_id)
            for spell_id in spell_ids
        } == {1, 3}
    finally:
        conn.close()


def test_no_selector_ritual_families_are_classified_by_effect_semantics():
    conn = sqlite3.connect(REFERENCE_DB)
    conn.row_factory = sqlite3.Row
    try:
        assert spell_no_selector_mode(
            conn, SUMMON_HOUND_OF_TWILIGHT) == (
                "caster_current_province_implicit")
        assert spell_no_selector_mode(conn, TWICEBORN) == "caster"
        assert spell_no_selector_mode(
            conn, DOME_OF_SOLID_AIR) == "caster_current_province_implicit"
        assert spell_no_selector_mode(conn, DISTILL_GOLD) == "none"
        assert spell_no_selector_mode(
            conn, RITUAL_OF_REBIRTH
        ) == "automatic_dead_hall_of_fame_hero"

        unsupported = []
        supported = 0
        for spell in conn.execute(
            "SELECT * FROM spells WHERE CAST(path1 AS INTEGER)>=0 "
            "AND CAST(pathlevel1 AS INTEGER)>0 AND school BETWEEN 0 AND 6"
        ):
            spell_id = int(spell["id"])
            effects = ritual_effect_numbers(conn, spell_id)
            if not effects:
                continue
            has_range = spell_province_range(conn, spell_id) is not None
            known_special = bool(
                spell_id in {1285, 1320, 1327, 1349}
                or effects & {30, 39, 81, 82, 160}
            )
            if (
                known_special
                or (has_range and 161 not in effects)
                or spell_no_selector_mode(conn, spell_id) is not None
            ):
                supported += 1
            else:
                unsupported.append((spell_id, spell["name"], effects))
        assert supported == 647
        assert unsupported == [
            (915, "Wish", frozenset({34})),
            (1226, "Disenchantment", frozenset({152})),
            (1370, "Arcane Analysis", frozenset({156})),
        ]
    finally:
        conn.close()


def test_effect_82_source_restrictions_come_from_spell_attributes():
    conn = sqlite3.connect(REFERENCE_DB)
    try:
        assert spell_source_requirements(conn, IRON_WALLS) == ["completed fort"]
        with pytest.raises(ValueError, match="does not have a completed fort"):
            validate_ritual_source(
                conn, IRON_WALLS, [_province(1)], 1, caster_type_id=1)
        validate_ritual_source(
            conn, IRON_WALLS, [_province(1, fort=1)], 1, caster_type_id=1)

        assert spell_source_requirements(conn, WEAVERS_OF_THE_WOOD) == [
            "source terrain mask 0x80"]
        with pytest.raises(ValueError, match="lacks required terrain flags 0x80"):
            validate_ritual_source(
                conn, WEAVERS_OF_THE_WOOD, [_province(1)], 1,
                caster_type_id=1)
        validate_ritual_source(
            conn, WEAVERS_OF_THE_WOOD, [_province(1, terrain=0x80)], 1,
            caster_type_id=1)

        assert spell_source_requirements(conn, GEOGLYPHS) == [
            "source terrain mask 0x40", "flying caster"]
        with pytest.raises(ValueError, match="worn items do not provide flight"):
            validate_ritual_source(
                conn, GEOGLYPHS, [_province(1, terrain=0x40)], 1,
                caster_type_id=1)
        # Winged Shoes (294) carry items.fly=1 and satisfy the same effective
        # flying check the discovery and writer use.
        validate_ritual_source(
            conn, GEOGLYPHS, [_province(1, terrain=0x40)], 1,
            caster_type_id=1, caster_item_ids=(294,))
    finally:
        conn.close()
