"""Which Divine spells a priest of ours can actually cast.

Divine spells are **school 7**: 61 records, every one at `researchlevel 0` and
every one requiring the Holy path. Research never unlocks them; the caster's
Holy level does.

Filtering on Holy level alone **overlists badly** — it offers 25 where the game
offers 10 — which is why this was parked rather than shipped. Two attributes do
the rest of the work:

* **278, `#restricted`** — an ordinary nation restriction, which removes every
  Unholy variant for a nation that is not Ermor or Lemuria.
* **750, `#godpathspell`** — the God Path Restriction, and the piece that was
  missing. Eight Divine spells carry none and are offered to everybody. The
  rest come in **elemental sets of nine**, one per magic path, and the
  pretender's own path selects exactly one from each set.

Confirmed against the game's Divine list for a Holy 3 priest of MA Marignon,
whose pretender is Fire 6 / Air 6: predicted ten, the screen showed the same
ten, including the two greyed-out entries above his Holy level. A controlled
pretender-builder matrix then confirmed the equal-level priority as the game's
path order: Fire, Air, Water, Earth, Astral, Death, Nature, Glamour, Blood.

    always          Blessing, Sermon of Courage, Smite Demon, Holy Word,
                    Holy Avenger, Divine Blessing, Fanaticism, Divine Channeling
    by god path     Ashes to Ashes (H1) and Heavenly Fire (H3) for a Fire god;
                    Sacred Wind and Heavenly Strike for Air, and so on
"""
from __future__ import annotations

from dataclasses import dataclass

#: School id of the Divine list. Beyond the seven research schools, which run
#: 0-6, and the only school whose spells need no research at all.
DIVINE_SCHOOL = 7

#: Holy is index 9 in the spell table's path numbering, after FAWESDNGB. Path
#: 7 is Glamour, which is an easy and expensive thing to get wrong.
HOLY_PATH_INDEX = 9

#: Magic path order used by the God Path Restriction values.
GOD_PATHS = "FAWESDNGB"

ATTR_NATION_RESTRICTION = 278
ATTR_GOD_PATH = 750


@dataclass(frozen=True)
class DivineSpell:
    spell_id: int
    name: str
    holy_level: int
    god_path: str | None
    castable: bool
    #: True when the caster only reaches it with worn equipment counted in.
    needs_equipment: bool = False


def dominant_god_path(pretender_paths: dict[str, int]) -> int | None:
    """Index of the path that selects our god-path Divine spells.

    Highest level wins; equal levels use ``GOD_PATHS`` order. This is directly
    controlled: W4 selects Water, A4/W4 selects Air, F4/A4/W4 selects Fire,
    lowering each leader reveals the next path, and B5 supersedes three paths
    tied at four while B4 does not.
    """
    candidates = [(level, -index)
                  for index, letter in enumerate(GOD_PATHS)
                  if (level := int(pretender_paths.get(letter, 0) or 0)) > 0]
    if not candidates:
        return None
    best_level, negative_index = max(candidates)
    return -negative_index


def holy_from_items(item_rows) -> int:
    """Holy levels granted by worn equipment.

    Five base-game items grant H+1: Crown of the Shah, Immaculate Shield, Ring
    of the False Prophet, Sword of Injustice and Sword of Justice. They matter
    because the Divine list has entries at Holy 4 and 5, which no ordinary
    priest of ours reaches unaided.
    """
    total = 0
    for item in item_rows:
        try:
            total += int(item["H"] or 0)
        except (IndexError, KeyError, TypeError):
            continue
    return total


def divine_spells(reference_conn, nation_id: int, holy_level: int,
                  pretender_paths: dict[str, int],
                  other_paths: dict[str, int] | None = None,
                  item_rows=(),
                  ) -> list[DivineSpell]:
    """Every Divine spell this nation has, flagged by whether it is castable.

    `holy_level` is the caster's BASE level; worn items in `item_rows` are
    added to it, and a spell reachable only with them is marked
    `needs_equipment` so the difference stays visible.

    Spells above the caster's reach are returned with `castable=False` rather
    than dropped: the game lists them greyed out, and a priest one level short
    of Fanaticism is a fact worth planning around.

    **Communions are not modelled and cannot be.** A Communion Master takes
    levels from its slaves during a battle, which can lift a priest to Holy 4
    or 5 for spells they could never cast alone. That happens at resolution,
    depends on how many slaves are present and surviving, and nothing in the
    save predicts it. `castable` here means "without a communion".
    """
    other = {key.upper(): int(value or 0)
             for key, value in (other_paths or {}).items()}
    chosen = dominant_god_path(pretender_paths)
    equipment_holy = holy_from_items(item_rows)
    effective_holy = holy_level + equipment_holy
    out: list[DivineSpell] = []
    for row in reference_conn.execute(
            "SELECT * FROM spells WHERE school=? ORDER BY pathlevel1, id",
            (DIVINE_SCHOOL,)):
        restricted = [int(value[0]) for value in reference_conn.execute(
            "SELECT raw_value FROM attributes_by_spell "
            "WHERE spell_number=? AND attribute=?",
            (row["id"], ATTR_NATION_RESTRICTION))]
        if restricted and nation_id not in restricted:
            continue
        god_paths = [int(value[0]) for value in reference_conn.execute(
            "SELECT raw_value FROM attributes_by_spell "
            "WHERE spell_number=? AND attribute=?",
            (row["id"], ATTR_GOD_PATH))]
        # -1 is a REAL value here, not a placeholder: it appears alone on
        # Smite and beside Blood on Banishment, and it evidently selects a god
        # with no magic path. Discarding it turned "restricted to a pathless
        # god" into "unrestricted" and put Smite on the list when the game
        # does not offer it. Only the ABSENCE of the attribute means no
        # restriction.
        selected_god_path: str | None = None
        if god_paths:
            eligible_path = (-1 if chosen is None else chosen)
            if eligible_path not in god_paths:
                continue
            selected_god_path = (
                "pathless" if eligible_path == -1
                else GOD_PATHS[eligible_path]
            )

        required = int(row["pathlevel1"] or 0)
        secondary_met = True
        path2, level2 = int(row["path2"]), int(row["pathlevel2"] or 0)
        if path2 >= 0 and level2 > 0 and path2 != HOLY_PATH_INDEX:
            letter = GOD_PATHS[path2] if path2 < len(GOD_PATHS) else "?"
            secondary_met = other.get(letter, 0) >= level2
        out.append(DivineSpell(
            spell_id=int(row["id"]),
            name=str(row["name"]),
            holy_level=required,
            god_path=selected_god_path,
            castable=effective_holy >= required and secondary_met,
            needs_equipment=(secondary_met and effective_holy >= required
                             and holy_level < required),
        ))
    return out
