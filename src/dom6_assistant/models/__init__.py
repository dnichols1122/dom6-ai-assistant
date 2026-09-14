"""Core game state data models — pure dataclasses, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


class Era(IntEnum):
    EARLY = 1
    MIDDLE = 2
    LATE = 3


@dataclass
class Nation:
    nation_id: int
    name: str
    epithet: str
    era: Era
    # Expanded as we reverse-engineer more fields


@dataclass
class Unit:
    unit_id: int
    name: str
    nation_id: int
    hp: int
    max_hp: int
    strength: int
    attack: int
    defense: int
    morale: int
    # Positions etc. added as parsing matures


@dataclass
class Province:
    province_id: int
    name: str
    owner_nation_id: Optional[int]
    capital: bool = False
    units: list[Unit] = field(default_factory=list)


@dataclass
class GemStockpile:
    """Gem stockpile for one nation.

    The live 6.35/6.36 array has nine i32 slots in display order. A debug game
    giving every path a distinct stockpile proved all nine in one capture:
    Fire, Air, Water, Earth, Astral, Death, Nature, Glamour, Blood slaves.
    """
    fire: int = 0
    air: int = 0
    water: int = 0
    earth: int = 0
    astral: int = 0
    death: int = 0
    nature: int = 0
    glamour: int = 0
    blood: int = 0


@dataclass
class NationEconomicState:
    """Live economic snapshot of the player's nation, read from process memory.

    Offsets are relative to the gold field address (confirmed across 5 turns):
      +0x000 gold      int32
      +0x00C nation_id int16
      +0xF04 gem array start (FAWESDNGB, nine consecutive i32 values)
    """
    gold: int
    nation_id: int
    gems: GemStockpile


@dataclass
class GameState:
    """Top-level container for a snapshot of the game world."""
    turn: int
    era: Era
    nations: dict[int, Nation] = field(default_factory=dict)
    provinces: dict[int, Province] = field(default_factory=dict)
