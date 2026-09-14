"""How many research points a commander produces in a turn.

Confirmed against our own save rather than assumed. The rule is

    2 * (total non-Holy magic path levels) + 5

per commander ordered to research, plus the per-unit `magicstudy` bonus, plus
the research bonus of any items worn, plus the Magic scale of the province
being researched in (Drain subtracts), plus one point per experience star,
plus province-wide Inspiring Researcher.

The ordinary base term reproduces the controlled early turns. Inspiring
Researcher is isolated by the Caelum dual-human control: value 1 adds one to a
single researcher on turn 1 and two to two researchers on turn 2.

Across the base controls, turns 3 through 24 of the
Marignon game, across rosters of one to four researchers and path totals from
1 to 6: a lone three-path mage gives 11, two mages of one and three paths give
18, and a six-path Grand Master alongside a one-path priest gives 24.

Holy levels do not count. The wiki states it as "adding a Magic level, as long
as it's not a Priest level, increases their Research ability by 2", and our
Witch Hunters carry H1 that never contributes.

Chassis and worn-item `researchbonus` values are additive inputs when the
caller can identify them. Experience stars appear at 15, 50, 100, 200 and 400
XP, and each displayed star adds one research point.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Every commander ordered to research contributes this regardless of paths.
BASE_PER_RESEARCHER = 5

#: And this much per level of every non-Holy magic path.
PER_PATH_LEVEL = 2

#: Holy is excluded: a priest level is not a magic level for research.
NON_HOLY_PATHS = "FAWESDNGB"

#: Raw-XP thresholds for the five displayed experience stars.
EXPERIENCE_STAR_THRESHOLDS = (15, 50, 100, 200, 400)


@dataclass
class ResearcherRate:
    """One commander's contribution, itemised so it can be checked."""
    commander_id: int
    name: str
    path_levels: int
    base: int
    from_paths: int
    from_unit: int = 0
    from_items: int = 0
    from_scale: int = 0
    experience: int = 0
    experience_stars: int = 0
    from_experience: int = 0
    from_inspiring_researcher: int = 0
    items: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        # A commander cannot produce negative research; heavy Drain zeroes them
        # rather than draining the national total.
        return max(0, self.base + self.from_paths + self.from_unit
                   + self.from_items + self.from_scale
                   + self.from_experience + self.from_inspiring_researcher)


def path_levels(paths: dict[str, int]) -> int:
    """Total non-Holy magic levels."""
    return sum(int(level) for key, level in paths.items()
               if key in NON_HOLY_PATHS and level)


def experience_stars(experience: int) -> int:
    """Displayed stars, and therefore the research bonus, for raw XP."""
    xp = max(0, int(experience or 0))
    return sum(xp >= threshold for threshold in EXPERIENCE_STAR_THRESHOLDS)


def researcher_rate(commander_id: int, name: str, paths: dict[str, int], *,
                    magic_study: int = 0, item_bonuses: dict[str, int] | None = None,
                    magic_scale: int = 0,
                    friendly_dominion: bool = True,
                    drain_immune: bool = False,
                    experience: int = 0,
                    inspiring_researcher: int = 0) -> ResearcherRate:
    """One commander's research output for a turn.

    `magic_scale` is the province's scale, positive for Magic and negative for
    Drain. The two are NOT symmetric: Magic adds a point per level only where
    the province also has friendly dominion, while Drain subtracts one per
    level regardless of dominion. Both are per province, not national.
    """
    levels = path_levels(paths)
    scale = int(magic_scale or 0)
    if scale > 0 and not friendly_dominion:
        scale = 0
    if scale < 0 and drain_immune:
        scale = 0
    items = dict(item_bonuses or {})
    xp = max(0, int(experience or 0))
    stars = experience_stars(xp)
    return ResearcherRate(
        commander_id=commander_id,
        name=name,
        path_levels=levels,
        base=BASE_PER_RESEARCHER,
        from_paths=PER_PATH_LEVEL * levels,
        from_unit=int(magic_study or 0),
        from_items=sum(items.values()),
        from_scale=scale if levels else 0,
        experience=xp,
        experience_stars=stars,
        from_experience=stars,
        from_inspiring_researcher=int(inspiring_researcher or 0),
        items=items,
    )
