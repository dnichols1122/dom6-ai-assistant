"""Province-range and target-terrain rules for strategic rituals."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable


PROVINCE_RANGE_ATTRIBUTE = 700
REQUIRED_TARGET_TERRAIN_ATTRIBUTE = 701
REQUIRED_SOURCE_TERRAIN_ATTRIBUTE = 702
FORBIDDEN_SOURCE_TERRAIN_ATTRIBUTE = 703
FORBIDDEN_TARGET_TERRAIN_ATTRIBUTE = 704
PERSISTENCE_MONTHS_PER_GEM_ATTRIBUTE = 709
REQUIRED_SITE_ATTRIBUTE = 711
ONLY_COAST_SOURCE_ATTRIBUTE = 713
REQUIRES_FLYING_CASTER_ATTRIBUTE = 715
REQUIRED_CASTER_TYPE_ATTRIBUTE = 731
ONLY_FORT_SOURCE_ATTRIBUTE = 730
FRIENDLY_TARGET_ATTRIBUTE = 738
SEA_TERRAIN_FLAG = 4
PROVINCE_ENCHANTMENT_EFFECT = 82

# Rituals without ``#provrange`` are not all mysterious object selectors.  The
# effect table already separates the large, ordinary no-selector families:
# troop/commander summons, caster transformations and local fort/dome effects.
# Their order names the caster, so the shared selector words remain
# ``0/-1/-1``.  Keep the semantic split here so discovery and writing cannot
# drift back into maintaining different per-spell allowlists.
#
# Wish (34) remains deliberately absent because its client UI chooses an
# object through a still-unresolved payload. Dispel, Disenchantment and Arcane
# Analysis instead select an active global enchantment by its stable chain slot
# in the ordinary +124 word; they are classified separately below. Ritual of
# Rebirth (26) is different again: the controlled turn-26 order has ordinary
# empty selector words and the game automatically chose a dead Hall-of-Fame
# hero when the turn resolved.
LOCAL_NO_SELECTOR_RITUAL_EFFECTS = frozenset({
    1,    # ordinary troop summons
    21,   # commander summons/revivals
    35,   # cross breeding
    63,   # fort creation/growth in the caster's province
    68,   # animal summons
    76,   # Tartarian Gate
    84,   # persistent local domes/walls
    85,   # Epopteia (summons unit 94)
    89,   # unique commander summons
    93,   # unique monster summons
    100,  # Hidden in Snow/Sand/Underneath
    116,  # Unleash Imprisoned Ones
    127,  # Infernal Breeding
    130,  # Hannya pacts
    137,  # Call Ladon
    141,  # Call the Birds of Splendor
    153,  # elemental opposition summons
    163,  # Nexus Gate in the caster's province
    501,  # Lore of Legends (summons unit 1086)
})

CASTER_NO_SELECTOR_RITUAL_EFFECTS = frozenset({
    10,   # Simulacrum
    23,   # Twiceborn and other caster enchantments
    44,   # Transformation
    101,  # rejuvenation
    111,  # Internal Alchemy
    118,  # Blood Feast
    131,  # Cure Disease
    132,  # Pyre of Catharsis
    167,  # Lichcraft
    511,  # Taurobolium / Blessing of the God-slayer
})

PLAIN_NO_SELECTOR_RITUAL_EFFECTS = frozenset({
    22,   # Fate of Oedipus
    157,  # Astral Disruption
    164,  # alchemical conversion
})

AUTOMATIC_NO_SELECTOR_RITUAL_EFFECTS = frozenset({
    26,   # Ritual of Rebirth: client chooses an eligible dead HoF hero
})

NO_SELECTOR_RITUAL_EFFECTS = frozenset(
    LOCAL_NO_SELECTOR_RITUAL_EFFECTS
    | CASTER_NO_SELECTOR_RITUAL_EFFECTS
    | PLAIN_NO_SELECTOR_RITUAL_EFFECTS
    | AUTOMATIC_NO_SELECTOR_RITUAL_EFFECTS
)

# Active-global selectors. Controlled client orders put the selected global's
# stable chain slot in +124. Well of Misery occupied slot zero in both new
# controls, which is a real selector value rather than an absent target.
GLOBAL_SELECTOR_RITUAL_EFFECTS = frozenset({
    30,   # Dispel
    152,  # Disenchantment
    156,  # Arcane Analysis
})

# The 6.36 client order UI at 0x228c8d explicitly combines effects 30 and 152
# for its Boost Spell path. Arcane Analysis is a fixed-cost probe.
GLOBAL_STRENGTH_INVESTMENT_RITUAL_EFFECTS = frozenset({30, 152})


def ritual_effect_numbers(reference_conn, spell_id: int) -> frozenset[int]:
    """Return the strategic effect ids belonging to one ritual spell."""

    return frozenset(
        int(row[0])
        for row in reference_conn.execute(
            "SELECT e.effect_number FROM spells s JOIN effects_spells e "
            "ON e.record_id=s.effect_record_id WHERE s.id=? "
            "AND CAST(e.ritual AS INTEGER)=1",
            (spell_id,),
        )
    )


def spell_no_selector_mode(reference_conn, spell_id: int) -> str | None:
    """Classify a verified ritual whose order carries no selected object.

    The returned value describes gameplay semantics, not a different wire
    layout.  Every returned mode writes ``0/-1/-1`` in the selector words.
    ``None`` means the spell belongs to another target family.
    """

    effects = ritual_effect_numbers(reference_conn, spell_id)
    if not effects or not effects <= NO_SELECTOR_RITUAL_EFFECTS:
        return None
    if effects & CASTER_NO_SELECTOR_RITUAL_EFFECTS:
        return "caster"
    if effects & LOCAL_NO_SELECTOR_RITUAL_EFFECTS:
        return "caster_current_province_implicit"
    if effects & AUTOMATIC_NO_SELECTOR_RITUAL_EFFECTS:
        return "automatic_dead_hall_of_fame_hero"
    return "none"


@dataclass(frozen=True)
class RitualTargetCheck:
    """The verified map reach of one province-targeted ritual."""

    distance: int
    maximum: int
    forbidden_terrain_flags: int = 0


def _spell_attribute(reference_conn, spell_id: int, attribute: int) -> int | None:
    row = reference_conn.execute(
        "SELECT raw_value FROM attributes_by_spell "
        "WHERE spell_number=? AND attribute=? LIMIT 1",
        (spell_id, attribute),
    ).fetchone()
    return int(row[0]) if row is not None else None


def spell_province_range(reference_conn, spell_id: int) -> int | None:
    """Return extracted ``#provrange``, or ``None`` for another target shape."""

    value = _spell_attribute(
        reference_conn, spell_id, PROVINCE_RANGE_ATTRIBUTE)
    return value if value is not None and value >= 0 else None


def spell_forbidden_target_terrain(reference_conn, spell_id: int) -> int:
    """Return the extracted ``#nogeodst`` terrain mask."""

    return _spell_attribute(
        reference_conn, spell_id, FORBIDDEN_TARGET_TERRAIN_ATTRIBUTE) or 0


def spell_required_target_terrain(reference_conn, spell_id: int) -> int:
    """Return the extracted ``#onlygeodst`` terrain mask."""

    return _spell_attribute(
        reference_conn, spell_id, REQUIRED_TARGET_TERRAIN_ATTRIBUTE) or 0


def spell_is_province_enchantment(reference_conn, spell_id: int) -> bool:
    """Whether a ritual has the client's persistent-province effect 82."""

    row = reference_conn.execute(
        "SELECT 1 FROM spells s JOIN effects_spells e "
        "ON e.record_id=s.effect_record_id "
        "WHERE s.id=? AND CAST(e.ritual AS INTEGER)=1 "
        "AND e.effect_number=? LIMIT 1",
        (spell_id, PROVINCE_ENCHANTMENT_EFFECT),
    ).fetchone()
    return row is not None


def spell_uses_implicit_local_target(reference_conn, spell_id: int) -> bool:
    """Effect 82 without ``#provrange`` targets the caster's province."""

    return (
        spell_is_province_enchantment(reference_conn, spell_id)
        and spell_province_range(reference_conn, spell_id) is None
    )


def spell_duration_extension_months_per_gem(
    reference_conn, spell_id: int,
) -> int | None:
    """Return the Boost Spell duration rate for province enchantments.

    The client binary selects the duration message when the ritual effect is
    82, looks up attribute 709, and clamps its value to a minimum of one.
    Thus a missing attribute means one month per gem, while e.g. value 3 means
    three months per gem.
    """

    if not spell_is_province_enchantment(reference_conn, spell_id):
        return None
    value = _spell_attribute(
        reference_conn, spell_id, PERSISTENCE_MONTHS_PER_GEM_ATTRIBUTE)
    return max(int(value or 0), 1)


def spell_requires_coast(reference_conn, spell_id: int) -> bool:
    """Whether the extracted spell has ``#onlycoastsrc``."""

    return _spell_attribute(
        reference_conn, spell_id, ONLY_COAST_SOURCE_ATTRIBUTE) is not None


def spell_source_requirements(reference_conn, spell_id: int) -> list[str]:
    """Describe extracted source/caster restrictions for assistant discovery."""

    requirements: list[str] = []
    required = _spell_attribute(
        reference_conn, spell_id, REQUIRED_SOURCE_TERRAIN_ATTRIBUTE) or 0
    forbidden = _spell_attribute(
        reference_conn, spell_id, FORBIDDEN_SOURCE_TERRAIN_ATTRIBUTE) or 0
    site = _spell_attribute(reference_conn, spell_id, REQUIRED_SITE_ATTRIBUTE)
    caster_type = _spell_attribute(
        reference_conn, spell_id, REQUIRED_CASTER_TYPE_ATTRIBUTE)
    if required:
        requirements.append(f"source terrain mask {int(required):#x}")
    if forbidden:
        requirements.append(f"not source terrain mask {int(forbidden):#x}")
    if spell_requires_coast(reference_conn, spell_id):
        requirements.append("coastal province")
    if _spell_attribute(
            reference_conn, spell_id, ONLY_FORT_SOURCE_ATTRIBUTE) is not None:
        requirements.append("completed fort")
    if site is not None:
        site_row = reference_conn.execute(
            "SELECT name FROM magic_sites WHERE id=?", (int(site),)
        ).fetchone()
        name = site_row[0] if site_row is not None else f"site {int(site)}"
        requirements.append(f"visible site: {name}")
    if _spell_attribute(
            reference_conn, spell_id,
            REQUIRES_FLYING_CASTER_ATTRIBUTE) is not None:
        requirements.append("flying caster")
    if caster_type is not None:
        unit_row = reference_conn.execute(
            "SELECT name FROM units WHERE id=?", (int(caster_type),)
        ).fetchone()
        name = unit_row[0] if unit_row is not None else f"unit {int(caster_type)}"
        requirements.append(f"caster type: {name}")
    return requirements


def province_is_coastal(
    provinces: Iterable[Any], source_province: int,
) -> bool | None:
    """Return whether a land province has a publicly known Sea neighbour.

    ``None`` means the province or part of its adjacency is absent from the
    player-visible graph. A known Sea neighbour proves coast immediately.
    """

    rows = {int(province.province_id): province for province in provinces}
    source = rows.get(source_province)
    if source is None:
        return None
    if int(getattr(source, "current_terrain", 0) or 0) & SEA_TERRAIN_FLAG:
        return False
    unknown_neighbour = False
    for neighbour_id in source.neighbours:
        neighbour = rows.get(int(neighbour_id))
        if neighbour is None:
            unknown_neighbour = True
            continue
        terrain = int(getattr(neighbour, "current_terrain", 0) or 0)
        if terrain & SEA_TERRAIN_FLAG:
            return True
    return None if unknown_neighbour else False


def validate_ritual_source(
    reference_conn,
    spell_id: int,
    provinces: Iterable[Any],
    source_province: int | None,
    caster_type_id: int | None = None,
    caster_item_ids: Iterable[int] = (),
) -> None:
    """Validate extracted restrictions on the caster and source province."""

    restrictions = {
        "required_terrain": _spell_attribute(
            reference_conn, spell_id, REQUIRED_SOURCE_TERRAIN_ATTRIBUTE) or 0,
        "forbidden_terrain": _spell_attribute(
            reference_conn, spell_id, FORBIDDEN_SOURCE_TERRAIN_ATTRIBUTE) or 0,
        "site": _spell_attribute(
            reference_conn, spell_id, REQUIRED_SITE_ATTRIBUTE),
        "coast": spell_requires_coast(reference_conn, spell_id),
        "flying": _spell_attribute(
            reference_conn, spell_id,
            REQUIRES_FLYING_CASTER_ATTRIBUTE) is not None,
        "fort": _spell_attribute(
            reference_conn, spell_id, ONLY_FORT_SOURCE_ATTRIBUTE) is not None,
        "caster_type": _spell_attribute(
            reference_conn, spell_id, REQUIRED_CASTER_TYPE_ATTRIBUTE),
    }
    if not any(value for value in restrictions.values()):
        return
    if source_province is None:
        raise ValueError(
            "caster location is unknown, so ritual source restrictions cannot "
            "be checked")
    rows = {int(province.province_id): province for province in provinces}
    source = rows.get(source_province)
    if source is None:
        raise ValueError(
            f"caster province {source_province} is absent from the public map")
    terrain = int(getattr(source, "current_terrain", 0) or 0)
    required = int(restrictions["required_terrain"])
    if required and not terrain & required:
        raise ValueError(
            f"caster province {source_province} lacks required terrain flags "
            f"{required:#x}")
    forbidden = int(restrictions["forbidden_terrain"])
    if forbidden and terrain & forbidden:
        raise ValueError(
            f"caster province {source_province} has forbidden terrain flags "
            f"{terrain & forbidden:#x}")
    if restrictions["coast"]:
        coastal = province_is_coastal(rows.values(), source_province)
        if coastal is not True:
            detail = (
                "the public map does not establish it as coastal"
                if coastal is None
                else "it has no Sea neighbour"
            )
            raise ValueError(
                f"caster province {source_province} is not a legal coastal "
                f"source: {detail}")
    if restrictions["fort"] and int(getattr(source, "fort_type", 0) or 0) <= 0:
        raise ValueError(
            f"caster province {source_province} does not have a completed fort")
    site = restrictions["site"]
    if site is not None and int(site) not in set(getattr(source, "sites", ())):
        raise ValueError(
            f"caster province {source_province} does not contain required "
            f"visible site {int(site)}")
    required_type = restrictions["caster_type"]
    if required_type is not None and caster_type_id != int(required_type):
        raise ValueError(
            f"spell requires caster unit type {int(required_type)}, not "
            f"{caster_type_id if caster_type_id is not None else 'an unknown type'}")
    if restrictions["flying"]:
        if caster_type_id is None:
            raise ValueError(
                "caster type is unknown, so the flying requirement cannot be "
                "checked")
        row = reference_conn.execute(
            "SELECT flying FROM units WHERE id=?", (caster_type_id,)
        ).fetchone()
        item_ids = tuple(int(item_id) for item_id in caster_item_ids)
        item_grants_flight = False
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            item_grants_flight = reference_conn.execute(
                f"SELECT 1 FROM items WHERE id IN ({placeholders}) "
                "AND fly!=0 LIMIT 1", item_ids,
            ).fetchone() is not None
        if (row is None or not int(row[0] or 0)) and not item_grants_flight:
            raise ValueError(
                f"caster unit type {caster_type_id} and its worn items do not "
                "provide flight")


def province_distance(provinces: Iterable[Any], source: int,
                      target: int) -> int | None:
    """Shortest undirected distance through the public province graph."""

    graph: dict[int, set[int]] = {}
    for province in provinces:
        province_id = int(province.province_id)
        graph.setdefault(province_id, set())
        for neighbour in province.neighbours:
            neighbour_id = int(neighbour)
            graph[province_id].add(neighbour_id)
            graph.setdefault(neighbour_id, set()).add(province_id)
    if source not in graph or target not in graph:
        return None
    pending = deque([(source, 0)])
    seen = {source}
    while pending:
        province_id, distance = pending.popleft()
        if province_id == target:
            return distance
        for neighbour in graph[province_id]:
            if neighbour not in seen:
                seen.add(neighbour)
                pending.append((neighbour, distance + 1))
    return None


def validate_ritual_target(
    reference_conn,
    spell_id: int,
    provinces: Iterable[Any],
    source_province: int | None,
    target_province: int | None,
    caster_nation_id: int | None = None,
) -> RitualTargetCheck | None:
    """Validate an extracted ranged target, returning its distance evidence.

    Spells without ``#provrange`` use another target family and return ``None``.
    A ranged spell fails closed when its source, target, graph path, or target
    terrain cannot be established.
    """

    maximum = spell_province_range(reference_conn, spell_id)
    if maximum is None:
        return None
    if source_province is None:
        raise ValueError(
            "caster location is unknown, so ritual map range cannot be checked")
    if target_province is None:
        raise ValueError(
            f"ranged ritual requires a target province (maximum {maximum})")
    rows = {int(province.province_id): province for province in provinces}
    target = rows.get(target_province)
    if target is None:
        raise ValueError(
            f"target province {target_province} is absent from the public map")
    friendly_only = _spell_attribute(
        reference_conn, spell_id, FRIENDLY_TARGET_ATTRIBUTE) is not None
    if friendly_only:
        if caster_nation_id is None:
            raise ValueError(
                "caster nation is unknown, so the friendly target restriction "
                "cannot be checked")
        if int(getattr(target, "owner_nation_id", -1)) != caster_nation_id:
            raise ValueError(
                f"target province {target_province} is not owned by caster "
                f"nation {caster_nation_id}")
    distance = province_distance(rows.values(), source_province, target_province)
    if distance is None:
        raise ValueError(
            f"no public map path connects province {source_province} to "
            f"{target_province}; ritual range cannot be established")
    if distance > maximum:
        unit = "province" if distance == 1 else "provinces"
        raise ValueError(
            f"target province {target_province} is {distance} {unit} from "
            f"the caster, beyond this ritual's maximum range {maximum}")

    forbidden = spell_forbidden_target_terrain(reference_conn, spell_id)
    terrain = int(getattr(target, "current_terrain", 0) or 0)
    required = spell_required_target_terrain(reference_conn, spell_id)
    if required and not terrain & required:
        raise ValueError(
            f"target province {target_province} lacks required terrain flags "
            f"{required:#x} for this ritual")
    if forbidden and terrain & forbidden:
        overlap = terrain & forbidden
        raise ValueError(
            f"target province {target_province} has forbidden terrain flags "
            f"{overlap:#x} for this ritual")
    return RitualTargetCheck(distance, maximum, forbidden)
