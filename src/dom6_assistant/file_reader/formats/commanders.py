"""Commanders in `.trn` files.

Commanders are stored in two places, and keeping them straight is most of the
work:

1. A **name table**: `u32 commander id` followed by the XOR-encoded name, one
   entry after another in ascending id order, padded with zeros. This is the
   only place a commander's name appears.

2. A **stat record**, which is the same shape as the troop record in units.py
   — type id at +0, hp at +2, age at +30, nation at +32 — plus the province at
   +6, which troops do not carry because they are located by the commander they
   follow.

Verified against the game (example_game, MA Marignon, turn 1):

    id   name        type  what the player sees
    116  Dapamort     148  Friar, leading 20 Pikeneer + 15 Crossbowman
    182  Estorgant   2107  Troubadour, acting as a scout
    297  Sugaar      3894  Serpent of Heavenly Fires — the pretender, dormant

The name table lists every commander the player can see, which is a wider set
than "commanders someone owns":

  * our own commanders
  * the five rival pretenders (Inberke, Soggoth, Tukulti'ninurta, Frasrutar,
    Ahluic), which the player can see in-game
  * mercenaries currently **up for auction** — Sanne, leading 30 Amazons, is on
    offer and hired by nobody. If a nation hires her the player sees which one,
    so a mercenary's allegiance is visible and presumably changes in the file.

So membership in this table says nothing about ownership. Anything that needs
"whose commander is this" must read the nation from the stat record, not infer
it from the name table.

Each nation record points at its own pretender by id — see `pretender_ids`.

**The exact link.** The first u32 after a commander's name in our `.2h` is a
runtime-unit handle. The identical value sits 32 bytes before exactly one own
unit record's type id in `.trn`. It is not the commander id or the unit instance
id, which is why searches for either missed it. This joins the named order block
to type, current province, HP and age for all 18 turn-30 commanders, including
the 13 who lead no troops.

Examples are Dapamort handle 779 -> Friar 148 in province 93, Turgis 6312 ->
Paladin 440 in province 98, and Sugaar 6935 -> Serpent 3894 in province 93.
The earlier positional join remains retracted: pretenders happened to be sorted
by construction, but own commanders disprove it. Callers use the runtime handle
from `units.py`, never file order.

The `.2h` itself establishes ownership: it contains only commanders for whom
our nation can issue orders. Rival name-table entries are therefore never
admitted merely because they occur in the same cross-nation `.trn` run.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .trn import _decode_xor_string, read_nation_roster

# Within a stat record, relative to the unit type id at +0.
OFF_TYPE     = 0
OFF_HP       = 2
# CONFIRMED by two independent cases: +4 is where the commander IS, +6 is where
# they came from.
#
#   turn 2  Estorgant sneaks to Kratas.        +4 = 91,  +6 = 93
#   turn 3  a Paladin takes The Obsidian Waste. +4 = 98,  +6 = 93
#
# Every commander recruited in the capital keeps +6 = 93 forever, which is what
# made +6 look like a location while nobody had moved, and what made it look
# like location was hidden for sneakers once somebody had. It was neither: the
# field was simply the wrong one.
OFF_LOCATION = 4      # current province
OFF_HOME     = 6      # home/origin province
OFF_AGE      = 30
OFF_NATION   = 32

_OFF_PRETENDER = 58   # from a nation record's header offset

_SLOT_MIN, _SLOT_MAX = 100, 400   # observed name-table stride ~217-225
_MIN_RUN = 4                       # consecutive entries before we believe it


@dataclass
class CommanderName:
    """One entry of the id -> name table."""
    commander_id: int
    name: str
    offset: int


@dataclass
class Commander:
    """A commander's stat record."""
    type_id: int
    nation_id: int
    province_id: int | None   # current province (+4); None = not on the map
    home_province: int        # where recruited (+6)
    hp: int
    age: int
    offset: int
    name: str | None = None      # only when the id link is known


@dataclass(frozen=True)
class HeroicAbility:
    ability_id: int
    name: str


# Magic paths, ten consecutive bytes in the game's own order, inside the
# commander record that the name table is the front of.
#
# The "name table" turned out not to be a name table. Entries are ~218 bytes
# with a ~10-byte name, and the rest is the commander's record: the queued
# spells sit at name_end+136 as u16 spell ids (Sugaar's read 927, 928 and 245 —
# Summon Hawk, Summon Storm Power and Air Shield) and the paths at name_end+154.
#
# Confirmed by watching two commanders change:
#
#   Bruise    t15 {H:1}  ->  t16 {F:1, H:1}   after empowering himself in Fire
#   Floredee  t15 {}     ->  t16 {H:3}        after becoming prophet
#
# and by agreeing with the reference data for unmodified commanders: Michael the
# Inquisitor reads F1 H2 and the reference says F1 H2.
#
# This is where per-commander paths live. They are NOT in the 173-byte unit
# record — Bruise's changed only at +8, experience, across the empowerment turn —
# and not in the .2h, whose commander block did not move at all.
OFF_PATHS = 154
# Heroic ability id in the same commander record.  Clodius changes from 0 to 7
# exactly when the Hall of Fame awards Heroic Toughness, then retains 7 on all
# later controlled turns.  The complete id/name switch was recovered from the
# 6.36 client routine that renders this field.
OFF_HEROIC_ABILITY = 146
# The byte immediately before the path array is the pretender's current base
# Dominion strength. It must come from the current turn rather than a saved
# newlord design: a pretender death can reduce Dominion.
OFF_DOMINION = OFF_PATHS - 1
PATH_ORDER = ("F", "A", "W", "E", "S", "D", "N", "G", "B", "H")

HEROIC_ABILITY_NAMES = {
    1: "Enormous Strength",
    2: "Heroic Battle Prowess",
    3: "Lightning Reflexes",
    4: "Iron Will",
    5: "Valor",
    6: "Unbreakable Skin",
    7: "Heroic Toughness",
    8: "Heroic Quickness",
    9: "Heroic Precision",
    10: "Heroic Endurance",
    11: "Unequaled Obesity",
    12: "Extraordinary Agility",
    100: "Awesome Presence",
    101: "Battle Bellow",
    102: "Command of the Undead",
    103: "Adept Research Ability",
    104: "Third Eye",
    105: "Unsurpassed Daftness",
    106: "Unsurpassed Cruelty",
    107: "Soul Butchering Ability",
    108: "Fast Casting Ability",
    109: "Troll Blood",
    110: "Legendary Berserker",
    111: "Unsurpassed Luck",
    112: "Giant Blood",
    113: "Jinn Blood",
    114: "Caveman Blood",
    115: "Ichtyid Blood",
}


def read_paths(data: bytes, name_end: int) -> dict[str, int]:
    """Magic paths for the commander whose name ends at `name_end`.

    Paths at zero are omitted, so an empty dict means no magic at all. H is
    holiness, which makes a priest rather than a caster — the distinction that
    decides whether Preach or Research appears in the unit's order menu.
    """
    return {p: data[name_end + OFF_PATHS + i]
            for i, p in enumerate(PATH_ORDER)
            if data[name_end + OFF_PATHS + i]}


def read_heroic_ability(data: bytes, commander_id: int,
                        commander_name: str) -> HeroicAbility | None:
    """Read a named commander's exact heroic ability from their `.trn` block.

    A targeted id+encoded-name match covers own commanders omitted from the
    conservative run-based name-table scanner.  Ambiguous or unknown ids are
    refused rather than mapped to an attractive but unverified label.
    """
    encoded_name = bytes(ord(char) ^ 0x4F for char in commander_name) + b"\x4f"
    needle = struct.pack("<I", commander_id) + encoded_name
    matches: list[int] = []
    pos = 0
    while True:
        pos = data.find(needle, pos)
        if pos < 0:
            break
        matches.append(pos + len(needle))
        pos += 1
    if len(matches) != 1:
        return None
    off = matches[0] + OFF_HEROIC_ABILITY
    if off + 2 > len(data):
        return None
    ability_id = struct.unpack_from("<H", data, off)[0]
    if ability_id == 0:
        return None
    name = HEROIC_ABILITY_NAMES.get(ability_id)
    return (HeroicAbility(ability_id, name) if name is not None else None)


def read_commander_paths(data: bytes) -> dict[int, dict[str, int]]:
    """{commander id: paths} for every commander with a name-table entry."""
    out: dict[int, dict[str, int]] = {}
    for run in read_name_tables(data):
        for e in run:
            _name, name_end = _decode_xor_string(data, e.offset + 4)
            paths = read_paths(data, name_end)
            if paths:
                out[e.commander_id] = paths
    return out


def read_pretender_dominion(data: bytes, nation_id: int) -> int | None:
    """Current base Dominion for ``nation_id`` from its pretender record.

    Nation records point to the pretender commander id. The corresponding
    commander record stores Dominion immediately before its ten path bytes.
    Returning ``None`` on an absent or ambiguous record is deliberate:
    substituting a province's candle count would look reasonable but be wrong.
    """
    pretender_id = pretender_ids(data).get(nation_id)
    if pretender_id is None:
        return None
    matches = [entry for run in read_name_tables(data) for entry in run
               if entry.commander_id == pretender_id]
    if len(matches) != 1:
        return None
    _name, name_end = _decode_xor_string(data, matches[0].offset + 4)
    off = name_end + OFF_DOMINION
    if off >= len(data):
        return None
    value = data[off]
    return value if value <= 20 else None


def read_name_tables(data: bytes) -> list[list[CommanderName]]:
    """Every id -> name table in the file, as separate runs.

    There is more than one, and they share a format: a province table (id =
    province id) and a commander table both appear, each a run of ascending
    ids. They are returned separately rather than merged, because id 93 means
    "Marignon the province" in one and nothing in the other.

    Entries are found by their shape rather than by a fixed location: a u32 id
    followed immediately by a decodable XOR string. Requiring a run of at least
    three consecutive ascending ids is what separates the real table from the
    many four-byte values in the file that happen to be followed by plausible
    bytes.
    """
    out: list[list[CommanderName]] = []
    run: list[CommanderName] = []
    i = 0
    while i < len(data) - 8:
        cid = struct.unpack_from("<I", data, i)[0]
        if not (1 <= cid <= 60000):
            i += 1
            continue
        # A name's first byte is an encoded letter, never zero. Without this
        # the scan settles one byte early, decodes the preceding padding as a
        # leading "O" (0x00 ^ 0x4F), and reads the id from the wrong place —
        # which is how province names ended up in a commander table.
        if data[i + 4] == 0:
            i += 1
            continue
        name, after = _decode_xor_string(data, i + 4)
        if not (2 <= len(name) <= 40) or not _plausible_name(name):
            i += 1
            continue
        entry = CommanderName(commander_id=cid, name=name, offset=i)
        # Entries sit in fixed-size slots, so a real table shows both ascending
        # ids and a near-constant stride. Requiring the stride is what rejects
        # province names, which are also XOR strings and which an id-only test
        # happily swallowed (yielding "OMud Wood" — the leading O being a zero
        # padding byte decoded as text, the giveaway of a misaligned read).
        if (run and cid > run[-1].commander_id
                and _SLOT_MIN <= i - run[-1].offset <= _SLOT_MAX):
            run.append(entry)
        else:
            if len(run) >= _MIN_RUN:
                out.append(run)
            run = [entry]
        i = after
    if len(run) >= _MIN_RUN:
        out.append(run)
    return out


def read_commander_names(data: bytes) -> dict[int, str]:
    """{commander id: name}, for commanders only.

    The commander table is identified by content rather than by position: it is
    the run that contains the pretender ids the nation records point at. That
    is self-validating — if the run picked has no pretenders in it, it is not
    the commander table and nothing is returned.
    """
    wanted = set(pretender_ids(data).values())
    if not wanted:
        return {}
    best: list[CommanderName] = []
    for run in read_name_tables(data):
        hits = sum(1 for e in run if e.commander_id in wanted)
        if hits > sum(1 for e in best if e.commander_id in wanted):
            best = run
    if not any(e.commander_id in wanted for e in best):
        return {}
    return {e.commander_id: e.name for e in best}


def _plausible_name(name: str) -> bool:
    """A commander name is letters, spaces and the odd apostrophe or hyphen."""
    if not name[:1].isalpha():
        return False
    return all(c.isalpha() or c in " '-" for c in name)


def pretender_ids(data: bytes) -> dict[int, int]:
    """{nation_id: pretender commander id}.

    Each nation record names its own pretender. Verified against the fixture:
    Marignon 61 -> 297 Sugaar, Xibalba 74 -> 181 Ahluic, Machaka 76 -> 115
    Frasrutar, Nidavangr 81 -> 41 Inberke, Ys 85 -> 42 Soggoth, Oceania 87 ->
    43 Tukulti'ninurta — the six the player identified by sight.
    """
    out: dict[int, int] = {}
    for nation_id, _gold, _gem_base, header in read_nation_roster(data):
        # header, not gem_base: the pretender id sits beside the nation id, and
        # the gems are a record further on.
        pid = struct.unpack_from("<H", data, header + _OFF_PRETENDER)[0]
        if 1 <= pid <= 60000:
            out[nation_id] = pid
    return out


def find_commanders(data: bytes, type_ids: set[int],
                    nation_id: int | None = None) -> list[Commander]:
    """Stat records whose unit type is one of `type_ids`.

    Takes the set of commander-capable unit types from the reference data
    rather than trying to recognise a commander from its bytes: the record
    shape is shared with ordinary troops, so the type id is the only reliable
    discriminator available today.
    """
    out: list[Commander] = []
    for off in range(len(data) - 173):
        typ = struct.unpack_from("<H", data, off + OFF_TYPE)[0]
        if typ not in type_ids:
            continue
        nat = struct.unpack_from("<H", data, off + OFF_NATION)[0]
        raw_loc = struct.unpack_from("<h", data, off + OFF_LOCATION)[0]
        home = struct.unpack_from("<H", data, off + OFF_HOME)[0]
        # A commander who is not on the map carries a negative sentinel here,
        # not 0 — Sugaar reads -7 while dormant. Home is the field to sanity
        # check a record on, since it is always a real province.
        prov = raw_loc if 1 <= raw_loc <= 5000 else None
        if nation_id is not None and nat != nation_id:
            continue
        # 0 means the commander is not on the map — a dormant pretender is the
        # normal case. Kept rather than filtered: "Sugaar exists and is
        # nowhere" is a different fact from "Sugaar does not exist", and only
        # the caller knows which it needs.
        if not (1 <= home <= 5000):
            continue
        out.append(Commander(
            type_id=typ, nation_id=nat, province_id=prov, home_province=home,
            hp=struct.unpack_from("<H", data, off + OFF_HP)[0],
            age=data[off + OFF_AGE], offset=off))
    return out
