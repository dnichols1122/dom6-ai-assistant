"""The Dominions 6.36 magic-item cost routine.

The path-level table was first established from the client's forge screens.
The complete modifier pipeline was subsequently transcribed from the client at
``0x4d6490`` and its base-cost helper at ``0x5334f0``.  Keep the order and
rounding here literal: fixed reductions, percentage reductions, national
rebates and item-specific multipliers do not commute.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The exact i32 lookup table copied by ``0x5334f0``.  Base-game items stop at
#: level seven, but the last three entries are real client behaviour and make
#: modded level-eight through level-ten requirements costable too.
GEMS_BY_PATH_LEVEL = {
    0: 0,
    1: 5,
    2: 10,
    3: 15,
    4: 25,
    5: 40,
    6: 60,
    7: 80,
    8: 100,
    9: 120,
    10: 140,
}

#: The client caps a positive combined forge bonus here.  A negative bonus
#: (for example Elemental Dampening without an offsetting reduction) is not
#: subject to a corresponding lower cap.
MAX_FORGE_BONUS_PERCENT = 80

#: Reference `items` columns naming nations that receive the rebate. They hold
#: nation ids: Sword of Justice carries 61, and the player saw exactly that
#: item flagged as reduced-cost for MA Marignon, which is nation 61.
REBATE_COLUMNS = ("nationrebate1", "nationrebate2")
RESTRICTION_COLUMNS = tuple(f"restricted{i}" for i in range(1, 7))

PATH_NAMES = {"F": "fire", "A": "air", "W": "water", "E": "earth",
              "S": "astral", "D": "death", "N": "nature", "G": "glamour",
              "B": "blood"}


class ForgeCostUnknown(RuntimeError):
    """The item's requirements fall outside what the screens established."""


@dataclass(frozen=True)
class ForgeCost:
    """Gems per path before and after commander/world modifiers.

    `listed` is what the forge cost page displays. `charged` is what the game
    actually takes, and **it is `charged` that a forge order reserves in the
    `.2h`**.

    ``listed`` includes the item's own ``itemcost1``/``itemcost2`` multiplier
    and a yearning-artifact reduction, but not the forger's bonuses. ``charged``
    is what the order reserves. A national rebate is one gem per required path,
    not 20%; the controlled level-one orders happened to be compatible with
    both interpretations until the client routine settled it.
    """
    listed: dict[str, int]
    charged: dict[str, int]
    rebated: bool
    forge_bonus_percent: int = 0
    fixed_forge_bonus: int = 0
    bonuses_suppressed: bool = False
    yearning: bool = False

    @property
    def total_listed(self) -> int:
        return sum(self.listed.values())

    @property
    def total_charged(self) -> int:
        return sum(self.charged.values())


def _requirements(row) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for path_key, level_key in (("mainpath", "mainlevel"),
                                ("secondarypath", "secondarylevel")):
        path, level = row[path_key], row[level_key]
        if not path or not level:
            continue
        if path not in PATH_NAMES:
            raise ForgeCostUnknown(
                f"item {row['id']} requires unknown magic path {path!r}")
        out.append((path, int(level)))
    return out


def rebate_nations(row) -> set[int]:
    """Nation ids that forge this item at reduced cost."""
    out: set[int] = set()
    for column in REBATE_COLUMNS:
        try:
            value = row[column]
        except (IndexError, KeyError):
            continue
        if value:
            out.add(int(value))
    return out


def restricted_nations(row) -> set[int]:
    """Nation ids allowed to forge an otherwise nation-exclusive item."""
    out: set[int] = set()
    for column in RESTRICTION_COLUMNS:
        try:
            value = row[column]
        except (IndexError, KeyError):
            continue
        if value:
            out.add(int(value))
    return out


def nation_can_forge(row, nation_id: int | None) -> bool:
    restrictions = restricted_nations(row)
    return not restrictions or nation_id in restrictions


def _trunc_div(numerator: int, denominator: int) -> int:
    """C signed integer division (truncate toward zero) without floats."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator >= 0:
        return numerator // denominator
    return -((-numerator) // denominator)


def _item_multiplier(row, column: str) -> int:
    try:
        return int(row[column] or 0)
    except (IndexError, KeyError):
        return 0


def _item_suppresses_forge_bonus(row) -> bool:
    """Whether the client discards ordinary smithing bonuses for this item."""
    try:
        explicit = bool(int(row["noforgebonus"] or 0))
    except (IndexError, KeyError):
        explicit = False
    # 0x4d6632 takes the same branch for Construction 7/9 artifacts and for
    # an explicit Item No Forge Bonus attribute. Nation rebates still apply.
    return int(row["constlevel"] or 0) > 5 or explicit


def _rounded_percent_delta(cost: int, percent: int) -> int:
    """Nearest-integer percentage magnitude used by ``0x4d69c9``."""
    return (abs(percent) * cost + 50) // 100


def forge_cost(
    row,
    nation_id: int | None = None,
    *,
    forge_bonus_percent: int = 0,
    fixed_forge_bonus: int = 0,
    yearning: bool = False,
) -> ForgeCost:
    """Gems needed to forge one item, keyed by path name.

    ``forge_bonus_percent`` is the already-combined chassis, world-enchantment,
    province-site and other percentage adjustment. Positive values are a
    reduction; negative values increase price. ``fixed_forge_bonus`` is the
    summed hammer-style reduction. For a two-path item the client assigns the
    odd gem to the primary path: fixed 3 becomes 2 primary plus 1 secondary.
    """
    requirements = _requirements(row)
    if not requirements:
        raise ForgeCostUnknown(
            f"item {row['id']} states no magic path requirement")
    listed: dict[str, int] = {}
    ordered: list[tuple[str, int]] = []
    for index, (path, level) in enumerate(requirements):
        if level not in GEMS_BY_PATH_LEVEL:
            raise ForgeCostUnknown(
                f"item {row['id']} requires {path}{level}, and only levels "
                f"{sorted(GEMS_BY_PATH_LEVEL)} exist in the client table")
        base = GEMS_BY_PATH_LEVEL[level]
        modifier = _item_multiplier(
            row, "itemcost1" if index == 0 else "itemcost2")
        base = _trunc_div(base * (100 + modifier), 100)
        if yearning:
            base = (base + 1) // 2
        if base <= 0:
            raise ForgeCostUnknown(
                f"item {row['id']} produced non-positive {path} cost {base}")
        name = PATH_NAMES[path]
        listed[name] = listed.get(name, 0) + base
        ordered.append((name, base))

    rebated = nation_id is not None and nation_id in rebate_nations(row)
    suppressed = _item_suppresses_forge_bonus(row)
    percent = 0 if suppressed else int(forge_bonus_percent)
    fixed = 0 if suppressed else max(0, int(fixed_forge_bonus))

    if len(ordered) == 1:
        fixed_shares = (fixed,)
    else:
        fixed_shares = ((fixed + 1) // 2, fixed // 2)

    charged: dict[str, int] = {}
    for (path, base), fixed_share in zip(ordered, fixed_shares, strict=True):
        path_percent = percent
        if rebated:
            # 0x4d695b expresses the one-gem national rebate as just enough
            # extra percentage to round to one gem at this path's base cost.
            path_percent += 20 if base <= 4 else 100 // base
        path_percent = min(path_percent, MAX_FORGE_BONUS_PERCENT)

        cost = max(1, base - fixed_share)
        delta = _rounded_percent_delta(cost, path_percent)
        cost = cost - delta if path_percent > 0 else cost + delta
        cost = max(1, cost)
        charged[path] = charged.get(path, 0) + cost

    return ForgeCost(
        listed=listed,
        charged=charged,
        rebated=rebated,
        forge_bonus_percent=(0 if suppressed else int(forge_bonus_percent)),
        fixed_forge_bonus=(0 if suppressed else fixed),
        bonuses_suppressed=suppressed,
        yearning=yearning,
    )
