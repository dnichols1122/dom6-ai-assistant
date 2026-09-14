"""What empowering a commander costs.

Two sources, and they agree where they overlap.

**Measured here:** empowering Bruise from no path to Fire 1 cost 50 fire gems
at turn 15, recovered by accounting for every other gem-reserving order in the
same save (a 5-gem Fire Sword forge) against a pool that fell 60 -> 5.

**The published chart** (illwiki, Empowerment) gives the rest: 50 for the first
level, then 15 per level from the second onward. Its level-1 entry matches the
measurement exactly, which is the only cross-check available and is why the
rest is used rather than treated as folklore.

The chart also states that **every path uses the same prices**, charged in that
path's own gem, and that costs are **not cumulative** — you pay for the level
being reached, not the sum of the levels below it. Only Fire has been observed
here, so the path-independence is the wiki's claim and not ours.
"""
from __future__ import annotations

#: The published price of reaching each path level. Level 1 is an exception at
#: 50; from level 2 the price is a flat 15 per level, continuing past 9 as
#: `15 * level` for modded paths.
FIRST_LEVEL_COST = 50
PER_LEVEL_COST = 15

#: What we have actually seen the game charge, as opposed to what is published.
#: Extend this only from a controlled save, never from the chart.
CONFIRMED_COSTS = {1: 50}


class EmpowermentCostUnknown(RuntimeError):
    """The requested empowerment falls outside what can be priced."""


def empowerment_cost(target_level: int) -> int:
    """Gems to raise a path TO `target_level`, in that path's own gem.

    Not cumulative: empowering a Fire 2 mage to Fire 3 costs the level-3 price
    alone.
    """
    if not isinstance(target_level, int) or isinstance(target_level, bool):
        raise EmpowermentCostUnknown(
            f"target level must be an integer, got {target_level!r}")
    if target_level < 1:
        raise EmpowermentCostUnknown(
            f"empowerment raises a path to at least level 1, got {target_level}")
    if target_level == 1:
        return FIRST_LEVEL_COST
    return PER_LEVEL_COST * target_level


def cost_is_confirmed(target_level: int) -> bool:
    """True when we have watched the game charge this, not merely read it.

    The writer uses this to say which of its reservations rest on our own
    controlled save and which rest on the published chart.
    """
    return target_level in CONFIRMED_COSTS
