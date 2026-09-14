"""Parser for Dominions 6 `.trn` turn files.

Binary format documented in knowledge/trn_format.md.  All strings are XOR-encoded
with key 0x4F; null terminator = byte 0x4F.

Key confirmed offsets / structures:
  Header:
    0x00        u8   file-type marker (0x01)
    0x03-0x05   ascii "DOM" (NOT XOR'd)
    0x0A        u32 LE  province count
    0x0E        u32 LE  turn number
    0x26        xstr game name (XOR-encoded, 0x4F-terminated)

  Province records  (scan for pattern: ff ff 00 00 ff XX XX 1a 02):
    +0  ff ff          record-start marker
    +2  00 00          fixed zeros
    +4  ff             fixed byte
    +5  int16 LE       val2 (purpose unknown; ranges -48..+46)
    +7  1a 02          fixed marker
    +9  int32 LE       province ID
    +13 xstr\0         province name 1
    +?  xstr\0         province name 2 (often same)
    Then "body" starts at after_name2:
      body+26      : u16  commanders queued for recruitment
      body+28      : u16  troops queued for recruitment
      body+24      : i16  unrest
      body+34      : i16  owner nation_id (0 = independent)   <- ALL provinces
      body+38      : i16  dominion owner nation_id (-1 = none)
      body+45..50  : i8   negated scales (order, prod, heat, growth, luck, magic)
      body+53      : u8   dominion strength (0-10)
      body+70      : i16  population / 10        <- ALL provinces
      body+76/+78  : i16 + i16 corpse components; panel displays their sum
      body+44      : u8   province defence
      body+90      : u32  terrain bitfield  <- verified against the .map file
      body+102+    : u16  adjacent province ids, 0-terminated

    body+32 is the raw resource-calculator input, not the displayed total.
    body+51 is not displayed supply. See _parse_province.

    Capitals are identified by name, not by a byte signature: a capital carries
    its nation's name ("Marignon", "Pangaea"). See _mark_capitals(). The game
    itself labels them "Nation Capital" on the population tooltip, so an
    explicit flag does exist in the format somewhere; it has not been found yet.

  Nation records (one per nation, contiguous, ordered by nation id):
    +0     u16  nation id
    +6     u16  gold (treasury)
    +815   0xFF-filled array (size varies by game)
    +3815  14 zero bytes
    +3829  9 x int32 LE gems: Fire, Air, Water, Earth, Astral, Death, Nature,
           Glamour, Blood
    The gems sit at the END of the record, so a nation's id and its gems are
    ~3.8 KB apart with the next nation's id in between. See read_nation_roster.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

_XOR_KEY = 0x4F
_XOR_NULL = 0x4F  # 0x00 XOR 0x4F


#: Whole-buffer XOR table. Decoding a 177 KB file byte by byte at every offset
#: is quadratic; one translate() is a single pass, and because 0x4F ^ 0x4F is
#: 0x00 the result is NUL-delimited strings ready to split.
_XOR_TABLE = bytes(b ^ 0x4F for b in range(256))


def _decode_xor_string(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a XOR-encoded null-terminated string starting at `offset`.

    Returns (decoded_string, offset_after_null_terminator).
    """
    chars: list[str] = []
    i = offset
    while i < len(data):
        b = data[i]
        if b == _XOR_NULL:
            i += 1
            break
        chars.append(chr(b ^ _XOR_KEY))
        i += 1
    return "".join(chars), i


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TrnGems:
    """Player gem stockpile from the .trn file.

    Dom6 .trn stores 9 gem types (confirmed via early_vanheim.trn glamour test):
      Fire, Air, Water, Earth, Astral, Death, Nature, Glamour, Blood
    Glamour is a Dom6-new gem type inserted between Nature and Blood.
    Order is identical to Dom6 in-memory layout.
    """
    fire:    int = 0
    air:     int = 0
    water:   int = 0
    earth:   int = 0
    astral:  int = 0
    death:   int = 0
    nature:  int = 0
    glamour: int = 0
    blood:   int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "fire":    self.fire,
            "air":     self.air,
            "water":   self.water,
            "earth":   self.earth,
            "astral":  self.astral,
            "death":   self.death,
            "nature":  self.nature,
            "glamour": self.glamour,
            "blood":   self.blood,
        }


@dataclass
class TrnProvince:
    province_id: int
    name: str               # primary name (first XOR string after record header)
    name2: str              # secondary name (often same; sometimes city/region name)
    population: int         # actual population (stored i16 × 10; body+70)
    val2: int               # purpose unknown; ranges -48..+46
    is_capital: bool = False
    owner_nation_id: int = 0    # owning nation (0 = independent); regular+14
    # Saved flat province-income change (event command ``landgold``). The
    # runtime income calculator reads the corresponding main-province +0x66.
    land_gold: int = 0          # body+30 i16
    # The fort's administrative controller. Ordinarily identical to owner,
    # but the income/resource calculators distinguish the two during sieges
    # and ownership transitions.
    administrative_owner: int = 0  # body+36 i16
    unrest: int = 0             # body+24 i16
    # Recruitment queue counts. Non-zero in exactly the six capitals across all
    # 166 provinces of the host's ftherlnd and zero in the other 160, which is
    # what recruitment should look like — you can only recruit in a fort.
    #
    # Checked against our own .2h queue, which lists what is actually queued:
    #
    #   turn 3    Witch Hunter + 3 Knights of the Chalice   1 cmd, 3 troops
    #   turn 4-6  1 Knight of the Chalice                   0 cmd, 1 troop
    #   live      1 Knight of the Chalice                   0 cmd, 1 troop
    #
    # and +26/+28 read 1/3, 0/1, 0/1 respectively. The live case is the strongest
    # of these because ftherlnd and the .2h describe the same instant, whereas a
    # snapshot's .trn is written at turn start and its .2h saved mid-turn.
    #
    # ONE ANOMALY, recorded rather than smoothed over: at turn 2 the .2h holds 3
    # commanders and 3 troops while +26/+28 read 2 and 3. The troop count agrees
    # and the commander count is short by one. Timing is the likely cause, since
    # those two files describe different moments of that turn, but it is not
    # proven, so treat commanders_queued as the less certain of the two.
    commanders_queued: int = 0  # body+26 u16
    troops_queued: int = 0      # body+28 u16
    dominion_owner: int = -1    # nation_id with dominion (-1 = none); body+38 i16
    order_scale: int = 0        # body+45 i8 negated
    productivity_scale: int = 0 # body+46 i8 negated
    heat_scale: int = 0         # negated body+47 (positive = Heat, negative = Cold)
    growth_scale: int = 0       # body+48 i8 (Growth1 → -3; factor unclear)
    luck_scale: int = 0         # body+49 i8 negated
    magic_scale: int = 0        # body+50 i8 negated
    dominion_strength: int = 0  # body+53 u8
    terrain_flags: int = 0      # body+90 u32 — the .map's ORIGINAL terrain
    current_terrain: int = 0    # body+82 u32 — what the game displays
    wall_integrity: int = 0     # body+72 u16 - fort walls; 1500 Citadel, 200 Palisade
    # The panel renderer sums two signed runtime fields and prints
    # ``Corpses: %d``. The serialized record preserves the same two components.
    # This is raw private state: visibility is decided above the parser because
    # hidden and legitimately-known zero are both serialized as zero.
    corpse_count_raw: int = 0   # body+76 i16 + body+78 i16, tail-shifted
    # body+80: a fort is being built here. Confirmed by prediction: it read 0
    # before a Palisades build started, 1 for four turns while building, and
    # returned to 0 the turn the palisade completed — at which point fort_type
    # went 0 -> 1 and wall_integrity 0 -> 200.
    #
    # An earlier reading as "months elapsed" was withdrawn when its prediction of
    # 2 failed on the second turn of the build. A flag was the surviving
    # hypothesis and this is the observation that promoted it, rather than it
    # being adopted because nothing else fitted.
    under_construction: bool = False
    province_defense: int = 0   # body+44 u8

    # Magic sites, by id -> reference.sqlite3 magic_sites. Only sites the
    # player can actually see are populated: their own provinces, plus thrones,
    # which the game marks on the map for everyone from turn 1.
    sites: list[int] = field(default_factory=list)

    # F9's throne claimant, distinct from ordinary province ownership. The
    # client loads this signed u16 into runtime province+0x186c and the turn
    # file serializes it at body+0x109 plus the same variable tail shift used
    # by population/terrain. Zero means that the throne is unclaimed.
    throne_claimant_nation_id: int = 0

    fort_type: int = 0          # body+43; 0 = no fort. Differs by nation.

    # CONFIRMED at turn 4. Every province in the sample had both flags or
    # neither, so they could not be told apart until one had a temple and no
    # laboratory: The Obsidian Waste, after Turgis finished building there,
    # reads +41=0 and +42=1. Whichever it was named both.
    has_laboratory: int = 0     # body+41
    has_temple: int = 0         # body+42
    neighbours: list[int] = field(default_factory=list)  # body+102 u16 list
    map_x: int = 0              # body+98, map-image pixels
    map_y: int = 0              # body+100

    # Input to the resource calculator, not the displayed resource total.
    # `0x46caf0` reads the same value at province+0x68, then applies scales,
    # unrest, sites and low population before `0x46d110` redistributes through
    # forts. The neutral historical name remains API-compatible.
    raw_b32: int = 0
    # Still unidentified, and conclusively not displayed supplies.
    raw_b51: int = 0

    # 0 normally, 20 for records whose tail is shifted. Surfaced so the
    # UI can show which provinces needed the correction.
    tail_shift: int = 0


@dataclass(frozen=True)
class TrnGlobalEffect:
    """One active enchantment/effect record from the player-visible `.trn`."""

    effect_id: int
    spell_id: int
    caster_nation_id: int
    cast_province_id: int
    state: int
    value1: int
    value2: int
    #: Set when the file carries this enchantment more than once with
    #: disagreeing `value1`.  The caster's own turn 29 does exactly that -- one
    #: copy holds the true overcast 17, another holds the masked 1 -- and
    #: nothing yet identifies which section the client reads.
    overcast_ambiguous: bool = False
    #: Stable slot identifier within the chain. Removing an enchantment
    #: leaves a gap rather than renumbering the survivors.
    slot: int = 0
    #: Every distinct `value1` the file carries for this enchantment, in file
    #: order.  Nothing is discarded here: for our *own* global the overcast is
    #: information we are entitled to -- we chose the investment -- so the
    #: visibility layer, which knows whose nation this is, resolves it rather
    #: than the decoder throwing it away.
    value1_variants: tuple[int, ...] = ()

    @property
    def identity(self) -> tuple[int, int, int, int, int]:
        """Everything except the two viewer-dependent value fields."""
        return (
            self.effect_id,
            self.spell_id,
            self.caster_nation_id,
            self.cast_province_id,
            self.state,
        )


@dataclass(frozen=True)
class TrnLocalEnchantment:
    """One structural 44-byte persistent-enchantment record.

    Effect-82 province enchantments and effect-81 global bookkeeping share the
    structure. The raw record is present in both human players' turn files, so
    effect classification and visibility belong above this parser.
    """

    caster_runtime_index: int
    effect_argument: int
    spell_id: int
    caster_nation_id: int
    province_id: int
    months_left: int


@dataclass
class TrnFile:
    """Parsed content of a `.trn` file."""
    game_name: str
    turn: int
    province_count: int         # from header
    player_gems: Optional[TrnGems]
    player_gold: Optional[int] = None
    research_points: Optional[int] = None
    research_speed: Optional[int] = None   # unresolved; see _OFF_UNKNOWN_596
    research_levels: Optional[list[int]] = None     # per RESEARCH_SCHOOLS
    research_progress: Optional[list[int]] = None   # into the next level
    # Strictly ascending ids immediately before the research state. This is
    # the complete learned-spell list, including individually researched
    # Level-9 spells while the corresponding school level remains 8.
    learned_spell_ids: Optional[tuple[int, ...]] = None
    # The .trn stores seven u32 progress values. The .2h queue anchor copies
    # only the first seven u16 words of that byte sequence, interleaving low
    # and high halves. Keep those signature words separately from the actual
    # display-order progress values.
    research_progress_raw: Optional[list[int]] = None
    nation_id: Optional[int] = None
    roster: list[tuple[int, int, int, int]] = field(default_factory=list)
    provinces: list[TrnProvince] = field(default_factory=list)
    global_effects: list[TrnGlobalEffect] = field(default_factory=list)
    local_enchantments: list[TrnLocalEnchantment] = field(default_factory=list)
    # Signed per-item world state, indexed directly by reference item id.
    # -99 is the ordinary unmade state, -98 is a yearning artifact, positive
    # values count extant copies, and zero is a spent/destroyed state. The
    # complete 2,000-byte array is player-visible because both human .trn
    # files serialize the same bytes; the forge screen consumes it directly.
    item_states: Optional[tuple[int, ...]] = None
    # F9's total claimed Ascension points for this player. Kept as an
    # independent cross-check on the per-throne claimant records.
    claimed_throne_points: Optional[int] = None


# ---------------------------------------------------------------------------
# Internal parsing
# ---------------------------------------------------------------------------

def _parse_header(data: bytes) -> tuple[str, int, int]:
    """Return (game_name, turn_number, province_count)."""
    # Magic check: bytes 0x03–0x05 must be "DOM"
    if data[3:6] != b"DOM":
        raise ValueError(f"Not a .trn file: missing DOM magic (got {data[3:6]!r})")

    province_count = struct.unpack_from("<I", data, 0x0A)[0]
    turn = struct.unpack_from("<I", data, 0x0E)[0]
    game_name, _ = _decode_xor_string(data, 0x26)
    return game_name, turn, province_count


# Province record signature: ff ff 00 00 ff __ __ 1a 02
_PROV_SIG_PRE  = b"\xff\xff\x00\x00\xff"   # 5 bytes at offset 0
_PROV_SIG_POST = b"\x1a\x02"               # 2 bytes at offset 7


def _find_province_records(data: bytes) -> list[tuple[int, int]]:
    """Scan for province record headers.

    Returns list of (file_offset, province_id) pairs.
    Province ID is read as int32 LE at header_offset + 9.
    """
    results: list[tuple[int, int]] = []
    i = 0
    end = len(data) - 13
    while i < end:
        if (data[i : i + 5] == _PROV_SIG_PRE and
                data[i + 7 : i + 9] == _PROV_SIG_POST):
            prov_id = struct.unpack_from("<i", data, i + 9)[0]
            if 1 <= prov_id <= 10000:   # sanity-filter wild reads
                results.append((i, prov_id))
            i += 9
        else:
            i += 1
    return results


def _safe_i16(data: bytes, off: int) -> int:
    if off + 2 > len(data):
        return 0
    return struct.unpack_from("<h", data, off)[0]


def _safe_u8(data: bytes, off: int) -> int:
    if off >= len(data):
        return 0
    return data[off]


def _safe_u32(data: bytes, off: int) -> int:
    if off + 4 > len(data):
        return 0
    return struct.unpack_from("<I", data, off)[0]


# Highest documented terrain bit (Recommended Starting Position and below;
# see ref.map_terrain_types). Anything above this is not a terrain mask, and
# is the clearest sign a record's tail is being read at the wrong offset.
TERRAIN_MAX = (1 << 27) - 1

# Province ids never approach this; a "neighbour" above it means the tail is
# being read at the wrong offset. The old bound of 5000 let a map coordinate
# (1644) pass as a province id, which is how a 4-byte shift went unnoticed.
_MAX_PROVINCE_ID = 1000

TAIL_SHIFT = 20        # extra bytes some records carry before their tail

# Shifts to try. The tail's distance from the record start is not constant:
# the Marignon capital needs 0 at turn 1 and 4 from turn 5 on, and a Pangaea
# capital needs 20. Whatever varies is between the scales and the population.
_TAIL_SHIFTS = (0, 4, 8, 12, 16, 20, 24, 28)
TAIL_SHIFT_NONE = 0    # kept explicit so the candidate list reads clearly


def _parse_province(data: bytes, rec_off: int,
                    expected_terrain: Optional[int] = None) -> tuple[TrnProvince, int]:
    """Parse one province record. Returns (province, body_offset).

    The body offset is returned so capitals can have their shifted fields
    re-read once capital identity is known — see _mark_capitals().
    """
    val2     = struct.unpack_from("<h", data, rec_off + 5)[0]
    prov_id  = struct.unpack_from("<i", data, rec_off + 9)[0]

    name1, after_name1 = _decode_xor_string(data, rec_off + 13)
    name2, body        = _decode_xor_string(data, after_name1)

    # CORRECTED: there is no capital-vs-non-capital offset shift for the owner
    # field. Each candidate offset was checked across all 28 sample .trn files
    # and 6 nations against the player's known nation id (from the filename):
    #
    #     body+14 -> matched in 0 files
    #     body+34 -> matched in every file, 1-38 provinces, scaling with turn
    #
    # The previous code read body+14 normally and body+34 only where its capital
    # heuristic fired, so it was right *only* where that heuristic was wrong —
    # which is why "capitals" and "owned provinces" tracked each other. The
    # 20-byte capital header in knowledge/trn_format.md was inferred from one
    # file and does not generalise; body[0]==14 holds in 2 of 28 files.
    #
    # Capitals are now identified after parsing, from game state rather than a
    # byte signature. See _mark_capitals().

    # All body-relative offsets are from `body` (= after_name2), confirmed via
    # Frida + in-game value calibration (debug_enabled_nosteam t9, 13 provinces).
    # Population is at body+70 for ALL province types — the capital extra header
    # is prepended at body+0..+19, but body+70 still gives pop/10.
    owner_nation_id   = _safe_i16(data, body + 34)
    land_gold          = _safe_i16(data, body + 30)
    administrative_owner = _safe_i16(data, body + 36)
    unrest            = _safe_i16(data, body + 24)
    commanders_queued = struct.unpack_from("<H", data, body + 26)[0]
    troops_queued     = struct.unpack_from("<H", data, body + 28)[0]
    dominion_owner    = _safe_i16(data, body + 38)
    dominion_strength =  _safe_u8(data, body + 53)

    # body+32 is the RAW resource input, not the displayed total. The game
    # shows Marignon as 144 while this holds 119 because `0x46caf0` applies
    # Order and Productivity sequentially (119 -> 126 -> 144), and `0x46d110`
    # can then draw resources through a fort. body+51 is still unidentified
    # and is conclusively not displayed supplies. The neutral names remain for
    # compatibility and to prevent either input being shown as a panel value.
    raw_b32           = _safe_i16(data, body + 32)
    raw_b51           =  _safe_u8(data, body + 51)

    # The record opens with the province's magic sites: 4 u16 slots, which is
    # the maximum a province can hold. Verified against the game: the Marignon
    # capital reads 13 and 192 — The House of Fiery Justice and The Royal
    # Academy, exactly the two the player sees on its province screen. Every
    # non-zero slot across the whole fixture resolves to a real site id, and
    # the only other provinces carrying one are the six thrones, which the game
    # shows to everybody. Nothing here is hidden information.
    # TWELVE slots, not four. Offsets +0 through +22 all hold valid magic
    # site ids across the corpus — 352 values at +0 and 20-30 at each later
    # slot, ~100% of them resolving in ref.magic_sites. Reading only four
    # missed every site past the fourth and left +10 and +22 recorded as
    # unexplained "loose bytes" for weeks. Unrest at +24 bounds the array.
    sites = [v for v in (_safe_i16(data, body + i * 2) & 0xFFFF for i in range(12))
             if v]

    # Owned-province block. Contiguous and only ever non-zero for provinces we
    # hold, which is itself the tell that these belong together.
    has_laboratory   = _safe_u8(data, body + 41)
    has_temple       = _safe_u8(data, body + 42)
    fort_type        = _safe_u8(data, body + 43)

    # CORRECTED: province defence is body+44, not body+81.
    #
    # body+81 also read 25 in the Marignon capital, which is what first sold
    # it, and it was marked derived rather than confirmed on the strength of
    # that single coincidence. Across 494 owned provinces in every save,
    # body+44 yields 1 value above 99 and body+81 yields 13, and body+81 is
    # zero for 207 of them — implausible, since an owned province almost always
    # carries some defence. body+44 also sits inside the owned-province block
    # next to the fort and the scales, where a defence value belongs.
    province_defense =  _safe_u8(data, body + 44)

    # Some records carry 20 extra bytes before their tail, so population,
    # defence, terrain and the neighbour list all move together.
    #
    # This was previously guessed at, first as "capitals shift" and then as
    # "take whichever population looks bigger". Both were wrong, and the second
    # was worse: reading population at body+90 returns the terrain bits times
    # ten, which for the Pangaea capital is a number that looks like a
    # population (42330) and is not one.
    #
    # It is now *detected* rather than guessed, because the game ships an
    # oracle: the .map file lists every province's terrain in plain text.
    # Whichever shift reproduces the documented terrain is the right one. In
    # the Pangaea save exactly one province of 142 needs the shift, and at
    # extra=20 its population, defence and all six neighbours fall into place
    # at once — four independent fields agreeing is not a coincidence.
    #
    # What causes the extra 20 bytes is still unknown. Detection does not
    # require knowing, which is the point.
    def _tail(extra: int) -> tuple[int, int, list[int], int, int]:
        pop = (_safe_i16(data, body + 70 + extra) & 0xFFFF) * 10
        wall = _safe_i16(data, body + 72 + extra) & 0xFFFF   # fort wall integrity
        ter = _safe_u32(data, body + 90 + extra)      # map's original terrain
        cur = _safe_u32(data, body + 82 + extra)      # terrain the game shows
        mx  = _safe_i16(data, body + 98 + extra) & 0xFFFF
        my  = _safe_i16(data, body + 100 + extra) & 0xFFFF
        nb: list[int] = []
        for i in range(0, 64, 2):
            v = _safe_i16(data, body + 102 + extra + i) & 0xFFFF
            if v == 0 or v > _MAX_PROVINCE_ID:
                break
            nb.append(v)
        return pop, ter, nb, mx, my, cur, wall

    # 4 and 24 were added when the Marignon capital gained four extra bytes
    # at turn 6, shifting its coordinates into the neighbour list.
    candidates = [(extra, _tail(extra)) for extra in _TAIL_SHIFTS]
    # Score every candidate on its own merits, then let the map file break
    # ties. Terrain alone cannot decide it: the Marignon capital is Plains,
    # which is terrain 0, and 0 matches at any zero-filled offset — so an
    # oracle-first rule picked a shift that produced no population, no
    # coordinates and no neighbours, and was "confirmed" by a zero.
    def _score(cand) -> tuple[int, ...]:
        pop, ter, nb, mx, my, _cur, _w = cand[1]
        return (1 if ter <= TERRAIN_MAX else 0,
                1 if (mx or my) else 0,          # a real province has a position
                1 if nb else 0,
                1 if _plausible_population(pop) else 0,
                len(nb))

    best = max(_score(c) for c in candidates)
    tied = [c for c in candidates if _score(c) == best]
    if expected_terrain is not None:
        matching = [c for c in tied if c[1][1] == expected_terrain]
        chosen = matching[0] if matching else tied[0]
    else:
        chosen = tied[0]

    tail_shift, (population, terrain_flags, neighbours, map_x, map_y,
                 current_terrain, wall_integrity) = chosen
    corpse_count_raw = (
        _safe_i16(data, body + 76 + tail_shift)
        + _safe_i16(data, body + 78 + tail_shift)
    )
    throne_claimant_nation_id = _safe_i16(
        data, body + 0x109 + tail_shift
    )

    # body+80 is NOT shift-adjusted: it sits below the tail. Confirmed by
    # prediction — 0 before a Palisades build, 1 for four turns during it, and
    # back to 0 the turn it finished, when fort_type went 0 -> 1 and
    # wall_integrity 0 -> 200.
    under_construction = bool(_safe_u8(data, body + 80))

    # Scale bytes: stored as negated signed bytes (e.g. Heat1 → body[47] = -1 raw i8 = 0xFF)
    def _signed_neg(off: int) -> int:
        raw = _safe_u8(data, body + off)
        s = raw if raw < 128 else raw - 256   # reinterpret as signed
        return -s

    order_scale        = _signed_neg(45)
    productivity_scale = _signed_neg(46)
    heat_scale         = _signed_neg(47)
    growth_raw         = _safe_u8(data, body + 48)   # Growth1 → -3 raw; factor unclear
    luck_scale         = _signed_neg(49)
    magic_scale        = _signed_neg(50)

    return TrnProvince(
        province_id=prov_id,
        name=name1,
        name2=name2,
        population=population,
        val2=val2,
        is_capital=False,          # assigned later by _mark_capitals()
        owner_nation_id=owner_nation_id,
        land_gold=land_gold,
        administrative_owner=administrative_owner,
        unrest=unrest,
        commanders_queued=commanders_queued,
        troops_queued=troops_queued,
        dominion_owner=dominion_owner,
        order_scale=order_scale,
        productivity_scale=productivity_scale,
        heat_scale=heat_scale,
        growth_scale=growth_raw,
        luck_scale=luck_scale,
        magic_scale=magic_scale,
        dominion_strength=dominion_strength,
        terrain_flags=terrain_flags,
        province_defense=province_defense,
        raw_b32=raw_b32,
        raw_b51=raw_b51,
        neighbours=neighbours,
        map_x=map_x,
        map_y=map_y,
        current_terrain=current_terrain,
        wall_integrity=wall_integrity,
        corpse_count_raw=corpse_count_raw,
        under_construction=under_construction,
        sites=sites,
        throne_claimant_nation_id=throne_claimant_nation_id,
        fort_type=fort_type,
        has_laboratory=has_laboratory,
        has_temple=has_temple,
        tail_shift=tail_shift,
    ), body




def _plausible_population(value: int) -> bool:
    """Could this be a real province population?

    Capitals in the sample run 20k-45k; ordinary provinces a few thousand. Zero
    is the tell-tale of reading at the wrong offset for an owned province, which
    always has people in it.
    """
    return 100 <= value <= 200_000


def _nation_name_key(name: str) -> str:
    """Normalise a name for capital matching: letters only, lowercased."""
    return "".join(c for c in name.lower() if c.isalpha())


def _load_nation_name_keys() -> frozenset[str]:
    """Normalised names of every nation, for identifying capital provinces.

    Reads knowledge/reference/nations.json (produced by
    `python -m dom6_assistant.reference.build_index` from `--listnations`).
    Missing file just disables name-based detection rather than failing.

    Very short names are excluded: "Man", "Ind", "Ur" and "Gath" are ordinary
    enough words to appear as unrelated province names, and a false capital
    silently shifts that province's population and terrain by 20 bytes.
    """
    path = Path(__file__).resolve().parents[3].parent / "knowledge" / "reference" / "nations.json"
    if not path.is_file():
        path = Path(__file__).resolve().parents[4] / "knowledge" / "reference" / "nations.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    return frozenset(k for k in (_nation_name_key(r["name"]) for r in rows)
                     if len(k) >= 5)


_NATION_NAME_KEYS = _load_nation_name_keys()


def _mark_capitals(data: bytes, provinces: list[TrnProvince],
                   bodies: dict[int, int]) -> None:
    """Flag each nation's capital and re-read the fields that shift, in place.

    Two problems solved together, because they are entangled.

    *Detection.* There is no reliable byte signature. body[0]==14 (from
    knowledge/trn_format.md) holds in only 2 of 28 sample files, and the old
    body[0]!=0 test flagged up to 37 "capitals" per game.

    Capitals are instead identified by name: a capital province carries its
    nation's name ("Pangaea", "Caelum", "Marignon"). Dominion strength was
    tried first and is unsound — any province can be pushed to maximum dominion
    given time, and it disagreed with the name test in 27 of 62 cases,
    including a Pangaea capital sitting at dominion 10 that it still missed.

    Matching against *every* nation name rather than just the owner's is
    deliberate: a captured capital keeps its name and its capital-only
    buildings, so it is still structurally a capital even under new ownership.

    *The shift.* Capitals really do carry 20 extra bytes, but only the fields
    from population onward move — the owner at body+34 does not. Verified
    against live values: Renthale (non-capital) reads 8550 at body+70, while
    the Pangaea capital reads 42330 at body+90, not body+70.

    Fields past population (terrain, province defence) are shifted on the same
    assumption. That is consistent but unverified — there is no live capital
    ground truth for them yet.
    """
    caps = [p for p in provinces if _nation_name_key(p.name) in _NATION_NAME_KEYS
            or _nation_name_key(p.name2) in _NATION_NAME_KEYS]

    for cap in caps:
        cap.is_capital = True
        body = bodies.get(cap.province_id)
        if body is None:
            continue
        # RETRACTED: capitals do not re-read their fields at a +20 shift.
        #
        # The "capital population lives at body+90" reading is disproved.
        # body+90 is the terrain bitfield, verified as a u32 against the game's
        # own .map file across all 99 surface provinces of the Marignon fixture
        # — including its capital, which is what the shift was invented for.
        # The Pangaea capital's "population 42330 at body+90" was that
        # province's terrain bits multiplied by ten.
        #
        # So population is read at body+70 for every province, capital or not,
        # and the Marignon capital confirms it in-game at 39650.
        #
        # One thing the shift was papering over is still unexplained: the
        # Pangaea capital reads 550 at body+70, which is far too small for a
        # capital. That is a real open question, and it now has one fewer wrong
        # answer attached to it. Not re-read here: guessing an offset that
        # happens to look plausible is what produced the retracted claim.
        pass


# Gem array location: player's nation block is preceded by an FF-run of
# nation-specific length, followed by exactly 14 zero bytes.
# gem_base = FF_block_end + 14.  Then 9 x int32 LE gem values follow in
# Dom6 display order: Fire, Air, Water, Earth, Astral, Death, Nature, Glamour, Blood.
#
# FF-block length is fixed per nation but varies across nations.
_GEM_SEPARATOR_BYTES = 14     # zero bytes between the 0xFF array and the gems
_NATION_FF_MIN       = 400    # the per-nation 0xFF array; size varies by game
_NATION_BLOCK_MIN_STRIDE = 2000  # real nation records are ~3877 bytes apart
_OFF_NATION_ID       = 48     # from a record's gem-array offset
_OFF_GOLD            = 54
# Confirmed against the Magic Research screen at turn 6: Conjuration 83 (50
# starting + 33 earned), Construction 50 and Thaumaturgy 50 total 183, and
# +592 reads exactly 183. It also gains 11 per turn from turn 3 onward, which
# matches the screen's "Research speed: 11 rp per month".
_OFF_RESEARCH_POINTS = 592   # u16, total accumulated across all paths

# NOT research speed, despite reading 11 at turn 6 and matching the screen.
# Its history is 6, 8, 10, 11 while the point total gained exactly 11 every
# turn across the same span — if this were the rate, turn 3 to 4 would have
# gained 6. So the turn-6 agreement is a coincidence, and one that would have
# shipped had the value not been checked against its own history.
_OFF_UNKNOWN_596     = 596
# Controlled Black Throne claim: Ermor's value is 0 through turn 12, changes
# to 1 on turn 13, and remains 1 thereafter. This is exactly the Ascension
# point total shown by F9, and agrees independently with province claimant 54
# on the level-one Black Throne.
_OFF_CLAIMED_THRONE_POINTS = 598

# Magic schools, in the order the research screen lists them.
RESEARCH_SCHOOLS = ("Conjuration", "Alteration", "Evocation", "Construction",
                    "Enchantment", "Thaumaturgy", "Blood Magic")
# Levels are u16[7] in display order. After one u16 zero pad, progress is
# u32[7] in the SAME order. The former reader treated the first seven u16
# halves as seven values and then permuted them. It therefore sampled only the
# low/high halves of Conjuration through Construction and happened to work
# whenever all live progress occupied one of those low halves.
_RESEARCH_PROGRESS_OFFSET = 16
_RESEARCH_SPELL_RUN_MIN = 20


def research_state_words(progress: list[int] | tuple[int, ...]) -> list[int]:
    """The seven u16 words copied into the `.2h` research queue anchor."""
    if len(progress) != len(RESEARCH_SCHOOLS):
        raise ValueError(f"expected 7 research progress values, got {len(progress)}")
    packed = struct.pack("<7I", *progress)
    return list(struct.unpack_from("<7H", packed))
# Research points to go from level n-1 to level n: 50, 100, 150, ...
#: Research points to buy the nth level of one school, at STANDARD research
#: difficulty. Levels 1-7 are confirmed against our own saves; 8-9 come from
#: the published table.
#:
#: Not a formula. It was carried as `25n^2 - 25n + 50` — which happens to give
#: 50, 100, 200 for the first three and then diverges badly: 550 against 700 at
#: level 5, and 1850 against 8100 at level 9.
_LEVEL_COSTS = {1: 50, 2: 100, 3: 200, 4: 400,
                5: 700, 6: 1300, 7: 2400, 8: 4400, 9: 8100}


def _LEVEL_COST(level: int) -> int:
    """Research points to buy the `level`-th level of one school.

    Level 3 = 200 is confirmed by rollover: Conjuration read 195 progress at
    turn 23, research ran at 7 points a turn, and turn 24 reads level 3 with 2
    progress. 195 + 7 - 200 = 2.

    **Level 4 is 400, and was 350 here until turn 35 proved otherwise.** It had
    been extrapolated from a curve and carried as unverified because this save
    had never passed level 3. The moment Conjuration reached 4,
    `find_research_by_school` stopped finding anything at all, because the
    arrays no longer summed to the nation's total.

    That failure is the one to remember: a wrong cost here does not produce a
    wrong number, it produces `None` for every research reading in the game,
    silently disabling research display, ritual validation and the research
    queue writer at once.

    Levels 5-9 originally came from the published table rather than the
    extrapolation, which was wrong at every one of them. Turn 42 carried 449
    points toward Construction 5,
    then produced 255: turn 43 reached level 5 and put the four-point overflow
    into Conjuration, directly fixing that cost at 700. The dual-human turn-42
    Marignon state independently fixes level 6 at 1300: after subtracting the
    known level-1..5 costs and visible progress, its two level-6 schools leave
    exactly 2600 points. Turn 43 then has one level-6 and one level-7 school;
    subtracting that confirmed 1300 isolates level 7 at 2400. Only levels 8-9
    remain the published-table portion without a save cross-check.

    **This assumes STANDARD research difficulty.** The game offers Very Easy
    through Very Difficult with different costs, and the setting is not
    something we read from the save. Our game is Standard because four of its
    levels are confirmed against it.
    """
    if level in _LEVEL_COSTS:
        return _LEVEL_COSTS[level]
    # Past level 9 only mods go, and no table covers it. Refuse rather than
    # extrapolate: extrapolation is exactly what was wrong here before.
    raise ValueError(
        f"no research cost is known for level {level}; the published table "
        f"covers 1-9 at standard difficulty")


def _research_total(levels: list[int], progress: list[int]) -> int:
    """Total points implied by a levels/progress pair."""
    total = 0
    for lv, pr in zip(levels, progress):
        total += sum(_LEVEL_COST(n) for n in range(1, lv + 1)) + pr
    return total


_LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: Characters a pretender title may contain. Anything else marks the boundary
#: between the title and whatever binary precedes it in the same record.
_TITLE_CHARS = frozenset(_LETTERS + " ,'-")

#: Same idea for a mercenary company name. Taken from the 78 names in the
#: reference table rather than guessed: they use space, apostrophe, comma and
#: hyphen, and one ("Gunter Blukraft's Sonnenkinder") carries an accented
#: letter, so the Latin-1 letters are admitted too.
_ACCENTED = "".join(chr(c) for c in range(0xC0, 0x100) if chr(c).isalpha())
_COMPANY_CHARS = frozenset(_LETTERS + _ACCENTED + " ',-")


def _legal_suffix(chunk: bytes, allowed: frozenset) -> str:
    """The longest tail of `chunk` made only of `allowed` characters.

    Text does not necessarily start where its NUL-delimited chunk does: a
    record may hold binary before the string. Scanning back from the end finds
    the text without decoding at every offset in the file, which is what made
    these functions quadratic.
    """
    text = chunk.decode("latin-1")
    start = len(text)
    while start > 0 and text[start - 1] in allowed:
        start -= 1
    return text[start:]


def _title_suffix(chunk: bytes) -> str:
    return _legal_suffix(chunk, _TITLE_CHARS)


def _strip_padding(text: str) -> str:
    """Drop leading padding from a decoded title.

    Zero bytes XOR to "O", so a title preceded by padding decodes as
    "OOOOKing of Kings". Stripping every leading O is wrong — it turns
    "Opener of the Wells" into "pener" — so instead find the first position
    that actually starts a word: an upper-case letter followed by a lower-case
    one. That keeps Opener and drops the padding in every other case.
    """
    for k in range(len(text) - 4):
        # A title starts either at a capitalised word or at a lower-case
        # "the ", as in "the Everburning One, Seducer of Life".
        if text[k].isupper() and text[k + 1].islower():
            return text[k:]
        if text[k:k + 4] == "the " and (k == 0 or text[k - 1] == "O"):
            return text[k:]
    return text


#: Slots in the Hall of Fame. The game's default; see read_hall_of_fame.
HALL_OF_FAME_SIZE = 10


def read_hall_of_fame(data: bytes) -> list[int]:
    """Commander ids in the Hall of Fame, in the order the game lists them.

    Exactly ten u32 ids preceded by 0xFFFFFFFF. Confirmed against the Hall of
    Fame screen at turn 6, which showed Bernard the Brave, Sanne, Hector Stark
    and Clodius in that order — and the run begins 179, 303, 182, 309, exactly
    those four.

    **The length is fixed and must be enforced.** This originally read "the
    longest run of plausible ids", which is not a boundary — it stops wherever
    the following bytes first fall outside 1..60000, so it returned 10 entries
    at turn 12, 11 at turns 22 and 24, and 19 at turn 23. The over-read is
    visible as duplicates (turn 23 gave 30 and 21 twice), which real ids in a
    ranking cannot be.

    Ten is the boundary because turn 12 terminates there on its own, and
    because the first ten are unique in every snapshot checked (t6, t12, t22,
    t23, t24) while entry eleven onward is not. It is also the game's default
    `--hofsize`. A game hosted with a larger hall would need this raised; that
    is a setting we cannot read from the .trn, so it is asserted here rather
    than inferred, and the run is truncated rather than trusted.

    The screen's Kills and Exp. columns are not stored here: they come from
    each commander's own record, at +38 and +8, both already decoded. So this
    list plus the unit records reproduces the whole screen.
    """
    best: list[int] = []
    for off in range(4, len(data) - 8):
        if struct.unpack_from("<I", data, off - 4)[0] != 0xFFFFFFFF:
            continue
        run: list[int] = []
        p = off
        while p + 4 <= len(data):
            v = struct.unpack_from("<I", data, p)[0]
            if not (1 <= v <= 60000):
                break
            run.append(v)
            p += 4
        if len(run) > len(best):
            best = run
    return best[:HALL_OF_FAME_SIZE]


#: Every mercenary record carries this constant at `name_end + 48`. It is what
#: identifies a record, and it replaced a name-shape test that silently hid
#: most of the auction — see `read_mercenaries`.
_MERCENARY_MARKER = 26812

OFF_MERC_COMMANDER = 0
OFF_MERC_EMPLOYER = 4
OFF_MERC_PRICE = 6
OFF_MERC_NATION_PERCENTAGES = 8
MERC_NATION_PERCENTAGE_PAIRS = 7
OFF_MERC_CONTRACT_MONTHS = 38
OFF_MERC_MIN_MEN = 40
OFF_MERC_UNIT_TYPE = 42
OFF_MERC_UNIT_COUNT = 44
OFF_MERC_MARKER = 48

#: Bytes of fields after a company's name, before the next company's name.
#: Exact across every multi-company snapshot: turns 6, 7, 12, 18, 25 and 30.
MERCENARY_RECORD_BODY = 54

#: An unhired company's employer field.
MERCENARY_UNEMPLOYED = 0xFFFF


@dataclass
class Mercenary:
    """A mercenary company in the auction, hired or not.

    `price` is the company's asking price as the file states it. The seven
    nation/percentage pairs at +8..+35 are overrides used by the client's
    minimum-bid calculator. For example, both Nergash's Damned Legion and
    Ghoul Father carry ``(61, 200)``, making their 350 and 300 asking prices
    display as 700 and 600 respectively to MA Marignon.
    """
    name: str
    commander_id: int
    price: int
    unit_type_id: int
    unit_count: int
    employer_nation_id: Optional[int]
    contract_months: int
    nation_bid_percentages: tuple[tuple[int, int], ...] = ()

    @property
    def available(self) -> bool:
        """True when nobody currently holds the contract."""
        return self.employer_nation_id is None


def read_mercenaries(data: bytes) -> list[Mercenary]:
    """Every company in the auction, in file order.

    Layout, from the byte after the company name:

        +0   u16  commander id
        +4   u16  employer nation id, 0xFFFF when free to hire
        +6   u16  asking price in gold
        +8   7 * (i16 nation id, i16 percentage) nation-specific bid prices
        +38  u16  months left on the current contract
        +40  u16  minimum men
        +42  u16  unit type id
        +44  u16  unit count
        +48  u16  26812, constant in every record observed

    Confirmed against the Hire Mercenaries screen twice. At turn 6 it showed
    "Hector's Heavy Horsemen" at 284 gold with "Hector Stark commands 25 Heavy
    Cavalries". At turn 30 the player reported Hell Hooves led by Berenger with
    30 Heavy Cavalries, "on contract to Machaka for 1 more month" — and that
    record reads employer 76, which is Machaka, with 1 at +38.

    **This previously found only companies whose names contain an
    apostrophe-s**, because that was used to tell a company name from every
    other string in the file. Real companies are called "Hell Hooves", "The
    Bowmen" and "Ship Wreckers", so the auction was reported with entries
    missing and nothing to say any were missing: 2 of 4 at turn 25, 2 of 5 at
    turn 12, 1 of 3 at turn 18. The record's own constant is the right
    discriminator and costs nothing to check.
    """
    # Split once rather than decoding at every offset. The per-offset scan was
    # quadratic and took 78 seconds on a 191 KB file — long enough to hang the
    # page that calls it. See read_pretender_titles for the same fix.
    plain = data.translate(_XOR_TABLE)
    found: list[tuple[int, bytes, tuple[int, ...]]] = []
    pos = 0
    for chunk in plain.split(b"\x00"):
        after = pos + len(chunk) + 1
        start, pos = after, after
        if start + OFF_MERC_MARKER + 2 > len(data):
            continue
        if struct.unpack_from(
                "<H", data, start + OFF_MERC_MARKER)[0] != _MERCENARY_MARKER:
            continue
        fields = tuple(
            struct.unpack_from("<H", data, start + off)[0]
            for off in (OFF_MERC_COMMANDER, OFF_MERC_EMPLOYER, OFF_MERC_PRICE,
                        OFF_MERC_CONTRACT_MONTHS, OFF_MERC_UNIT_TYPE,
                        OFF_MERC_UNIT_COUNT))
        cid, _employer, price, months, utype, ucount = fields
        # The marker alone is a strong test, but the fields still have to be
        # possible. A lone mercenary commander such as Dagan, the Renegade
        # Sage brings no troops at all, so zero men is legal and was wrongly
        # excluded before.
        if not (1 <= cid <= 60000 and 1 <= price <= 20000
                and 0 <= utype <= 4500 and 0 <= ucount <= 500
                and 0 <= months <= 24):
            continue
        found.append((start, chunk, fields))

    out: list[Mercenary] = []
    for index, (start, chunk, fields) in enumerate(found):
        cid, employer, price, months, utype, ucount = fields
        if index:
            # Records are contiguous: a company's 54 bytes of fields are
            # followed immediately by the next company's name. Slicing from
            # there is exact, where a character heuristic is not — the tail of
            # the previous record decodes as plausible letters and had been
            # arriving as part of the name ("Oo'NOOOHell Hooves").
            name_at = found[index - 1][0] + MERCENARY_RECORD_BODY
            text = plain[name_at:start - 1].decode("latin-1")
        else:
            # Nothing precedes the first record to chain from, so fall back to
            # the longest legal tail with its zero-byte padding stripped.
            text = _strip_padding(_legal_suffix(chunk, _COMPANY_CHARS))
        text = text.strip()
        if not 3 <= len(text) <= 48:
            continue
        nation_percentages = []
        for pair in range(MERC_NATION_PERCENTAGE_PAIRS):
            pair_at = start + OFF_MERC_NATION_PERCENTAGES + pair * 4
            nation, percentage = struct.unpack_from("<hh", data, pair_at)
            if nation > 0:
                nation_percentages.append((nation, percentage))
        out.append(Mercenary(
            name=text, commander_id=cid, price=price,
            unit_type_id=utype, unit_count=ucount,
            employer_nation_id=(None if employer == MERCENARY_UNEMPLOYED
                                else employer),
            contract_months=months,
            nation_bid_percentages=tuple(nation_percentages)))
    return out


def read_prophet_id(data: bytes, header_offset: int) -> Optional[int]:
    """The nation's prophet, as a commander id. None when there is no prophet.

    Nation record +64, with 0xFFFF meaning none. It went from 0xFFFF to 87 on
    exactly the turn Floredee became prophet, and Floredee's commander id is 87.
    Cross-checked two ways: four of six nations name a prophet here, and in our
    own .trn exactly one unit carries the -17 == 2 status byte.

    This offset previously produced a retracted decode. It changed from 0xFFFF to
    87, and item 87 is "Moon Blade", so it was briefly read as a second magic-item
    slot — a name found after the fact for a number that had moved. It is a
    commander id that happens to fall inside the item id range.
    """
    v = struct.unpack_from("<H", data, header_offset + 64)[0]
    return None if v == 0xFFFF else v


def read_item_stash(data: bytes, header_offset: int,
                    max_slots: int = 64) -> list[int]:
    """The nation's magic item stash: u16 item ids from +78, 0xFFFF for empty.

    Confirmed by prediction rather than by pattern-matching. Guarlan forged a
    Fire Sword and +78 became 1, which is Fire Sword's id — and the id was
    already known from the forge order's parameter. The next turn an Enchanted
    Helmet was forged and +80 became 186, its id. Two items, two consecutive
    slots, both values predicted before they were looked for.

    Gem accounting agrees to the unit: fire went 60 to 9 across a turn costing 55
    fire gems with +4 income, and astral 16 to 12 across one costing 5 pearls
    with +1 income.

    **Do not extend this array backwards.** Offsets below +78 hold unrelated
    fields that happen to contain small numbers, and small numbers name items:
    +58 holds the pretender's commander id (297, which is "Boots of the
    Messenger") and +60 holds the nation id (61, "Wraith Sword"). An earlier
    reading of +64 as a second item slot was retracted for exactly this reason —
    with 529 items, roughly one in 124 arbitrary small values names something.

    A slot reads **0** once its item has been equipped on a commander: the
    array zeroes in place rather than compacting. Those are skipped rather than
    returned — 0 is not an item id, and handing it back had the treasury
    reporting four items when it held none. Turn 17 shows a Fire Sword and an
    Enchanted Helmet here; by turn 18 both slots read 0 and both items are on
    Bruise, which is what identified the meaning.

    Skipped rather than treated as a terminator, because a zeroed slot can sit
    ahead of a full one.
    """
    out: list[int] = []
    for i in range(max_slots):
        v = struct.unpack_from("<H", data, header_offset + 78 + 2 * i)[0]
        if v == 0xFFFF:
            break
        if v:
            out.append(v)
    return out


def read_pretender_titles(data: bytes,
                          names: Optional[dict[int, str]] = None
                          ) -> dict[str, str]:
    """{pretender name: title}, e.g. "Sugaar" -> "King of Kings, Eater of Filth".

    Titles live in a per-nation table whose records are variable length — a
    38-byte header plus four XOR strings, the first being the title. Nations
    without a pretender store empty strings, so their record is exactly 38
    bytes; a fixed-stride walk therefore reads the empty ones cleanly and then
    loses alignment at the first real title. (That is why this region looked
    like "394 fixed 38-byte records".)

    Rather than model the record, pair each title with the pretender name that
    follows it — the game writes them adjacently, and the names are already
    known from the commander table, so a wrong pairing cannot go unnoticed.
    """
    # The whole buffer is decoded once and split, rather than decoding at every
    # offset. Decoding per offset is quadratic and it showed: on a 177 KB .trn
    # this function took over 60 seconds, which was enough to hang the page
    # that calls it. XOR 0x4F turns the 0x4F terminator into 0x00, so after one
    # pass the strings are simply NUL-delimited and can be split in one go.
    plain = data.translate(_XOR_TABLE)
    titles: list[tuple[int, str]] = []
    pos = 0
    for chunk in plain.split(b"\x00"):
        if len(chunk) >= 12:
            # A title does not necessarily start where its chunk does. Sugaar's
            # reads b"\xe3DrOOOM~\xb1\xb0OOOOKing of Kings, Eater of Filth" —
            # binary before the text, which _strip_padding cannot remove
            # because it stops at the first capital-then-lower-case pair ("Dr").
            # Taking the longest trailing run of title-legal characters finds
            # the text without scanning every offset in the file.
            text = _strip_padding(_title_suffix(chunk))
            if (len(text) >= 12 and "OOO" not in text
                    and (" of " in text or " the " in text)):
                titles.append((pos, text))
        pos += len(chunk) + 1
    known = set((names or {}).values())
    out: dict[str, str] = {}
    for pos, title in titles:
        window = data[pos:pos + len(title) + 400]
        # Advance one byte at a time. Skipping to the end of each decoded
        # string steps straight over the name, because a long run of padding
        # decodes as one very long string.
        for j in range(len(window) - 4):
            nm, _ = _decode_xor_string(window, j)
            if nm in known and nm not in out:
                out[nm] = _strip_padding(title)
                break
    # A mercenary's own name can look like a title ("Bernard the Brave"); a
    # pretender's title never contains their name.
    return {k: v for k, v in out.items() if k not in v}


def find_research_state(
    data: bytes, expected_total: int | None = None
) -> Optional[tuple[list[int], list[int], tuple[int, ...]]]:
    """Return levels, progress and the complete learned-spell id list.

    The live state follows the player's long, strictly ascending learned-spell
    id list and its u16 ``0xffff`` terminator. Levels are u16[7], followed by a
    zero u16 and progress as u32[7]. Historical report copies can repeat a
    state later in the file; the first structurally valid state belongs to the
    current player view. When the old nation-record total is available it is
    retained as an independent arithmetic cross-check.
    """
    n = len(RESEARCH_SCHOOLS)
    matches: list[tuple[list[int], list[int], tuple[int, ...]]] = []
    for off in range(2, len(data) - (_RESEARCH_PROGRESS_OFFSET + n * 4)):
        if struct.unpack_from("<H", data, off - 2)[0] != 0xFFFF:
            continue
        levels = [struct.unpack_from("<H", data, off + i * 2)[0] for i in range(n)]
        if any(lv > 9 for lv in levels) or struct.unpack_from("<H", data, off + 14)[0]:
            continue
        progress = [struct.unpack_from("<I", data, off + _RESEARCH_PROGRESS_OFFSET + i * 4)[0]
                    for i in range(n)]
        if any(
            (lv == 9 and pr != 0)
            or (lv < 9 and pr >= _LEVEL_COST(lv + 1))
            for lv, pr in zip(levels, progress)
        ):
            continue
        # Count the strictly ascending u16 spell ids immediately before the
        # terminator. Short accidental integer runs are common; real controls
        # carry 34 ids even on turn 1 and hundreds in the late game.
        learned_reversed: list[int] = []
        previous = 0xFFFF
        pos = off - 4
        while pos >= 0:
            spell_id = struct.unpack_from("<H", data, pos)[0]
            if spell_id == 0xFFFF or spell_id >= previous:
                break
            learned_reversed.append(spell_id)
            previous = spell_id
            pos -= 2
        if len(learned_reversed) < _RESEARCH_SPELL_RUN_MIN:
            continue
        if expected_total is not None and _research_total(levels, progress) != expected_total:
            continue
        matches.append((levels, progress, tuple(reversed(learned_reversed))))
    if not matches:
        return None
    # Historical copies may repeat the same state. The earliest valid state is
    # the current player block; controlled dual-human files prove later copies
    # can include the other human's otherwise-private research state.
    return matches[0]


def find_research_by_school(
    data: bytes, expected_total: int | None = None
) -> Optional[tuple[list[int], list[int]]]:
    """Compatibility view of the structurally decoded research state."""
    state = find_research_state(data, expected_total)
    return (state[0], state[1]) if state is not None else None


def find_learned_spell_ids(
    data: bytes, expected_total: int | None = None
) -> Optional[tuple[int, ...]]:
    """Return every learned spell id, including individual Level-9 spells."""
    state = find_research_state(data, expected_total)
    return state[2] if state is not None else None


def spell_is_researched(
    levels: list[int] | None,
    learned_spell_ids: tuple[int, ...] | None,
    spell_id: int,
    school: int,
    research_level: int,
) -> bool:
    """Apply Dom6's ordinary-level versus individual-Level-9 rule."""
    if levels is None or not 0 <= school < len(levels):
        return False
    if research_level < 9:
        return levels[school] >= research_level
    if research_level == 9:
        return (
            levels[school] >= 9
            or (
                learned_spell_ids is not None
                and spell_id in learned_spell_ids
            )
        )
    return False


_GEM_LABELS = ("fire", "air", "water", "earth", "astral", "death",
               "nature", "glamour", "blood")


def _nation_id_for_slug(slug: str) -> Optional[int]:
    """Map a .trn filename stem ("mid_marignon") to a nation id via nations.json."""
    if not slug:
        return None
    try:
        ref = Path(__file__).resolve().parents[4] / "knowledge" / "reference" / "nations.json"
        rows = json.loads(ref.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for row in rows:
        if row.get("file_name_base") == slug:
            return row["id"]
    era = {"early": 1, "mid": 2, "late": 3}.get(slug.split("_")[0])
    key = slug.split("_", 1)[-1].replace("_", "").lower()
    for row in rows:
        if row.get("era") == era and row["name"].lower().replace("'", "").replace(" ", "") == key:
            return row["id"]
    return None


def find_nation_blocks(data: bytes) -> list[int]:
    """Offsets of every per-nation record's gem array.

    Each nation record ends with a large 0xFF-filled array, then 14 zero bytes,
    then 9 x int32. Records sit in one contiguous run ordered by nation id.
    """
    out: list[int] = []
    i = 0
    while i < len(data) - 100:
        if data[i] != 0xFF:
            i += 1
            continue
        run_start = i
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i - run_start < _NATION_FF_MIN:
            continue
        base = i + _GEM_SEPARATOR_BYTES
        if base + 36 > len(data):
            continue
        if any(data[j] != 0 for j in range(i, base)):
            continue
        if all(0 <= v <= 99999 for v in struct.unpack_from("<9i", data, base)):
            out.append(base)
    return out


def read_nation_roster(data: bytes) -> list[tuple[int, int, int, int]]:
    """(nation_id, gold, gem_array_offset) for every nation in the game.

    Two things make this fiddly, and both are handled by leaning on the fact
    that nation records are *ordered by nation id*:

    First, a nation's gems sit at the END of its record, so they land in the
    next record's 0xFF block rather than beside its own id. Reading the gems
    next to an id yields the previous nation's stock.

    Second, the 0xFF array's size varies by game (3000 bytes in one save, ~890
    in another), so it cannot be filtered on. A permissive size threshold lets
    through unrelated blocks that happen to look similar, so the real records
    are picked out as the longest run of strictly ascending valid nation ids —
    a signature noise does not reproduce. Blocks outside that run are skipped
    when pairing, which is what stops a stray block from being mistaken for the
    next nation's record.
    """
    blocks = find_nation_blocks(data)
    if len(blocks) < 2:
        return []

    # Keep only genuine nation records. They sit ~3877 bytes apart; the scan
    # also turns up short candidates a few hundred bytes after a real one, and
    # those must not be counted, because a nation's gems live in the block
    # PHYSICALLY next to it.
    #
    # An earlier version filtered by "ids must ascend" instead, and skipped
    # non-ascending blocks. That worked at turn 1 only because the interloper
    # was spurious. At turn 2 one real nation's id field read 0, so the chain
    # skipped a real block and every nation after it paired one slot late —
    # which is how Marignon's gems came back as another nation's stock. Spacing
    # is the structural property; the id field is not always populated.
    kept: list[int] = [blocks[0]]
    for b in blocks[1:]:
        if b - kept[-1] >= _NATION_BLOCK_MIN_STRIDE:
            kept.append(b)

    roster: list[tuple[int, int, int, int]] = []
    for idx, base in enumerate(kept[:-1]):
        nation_id = struct.unpack_from("<H", data, base + _OFF_NATION_ID)[0]
        gold      = struct.unpack_from("<H", data, base + _OFF_GOLD)[0]
        if 1 <= nation_id <= 500:
            roster.append((nation_id, gold, kept[idx + 1], base))
    return roster


def nation_blocks_without_ids(data: bytes) -> list[int]:
    """Blocks that look like nation records but whose id field reads 0.

    `read_nation_roster` drops these, and that is a SILENT omission of a real
    nation. Machaka reads id 76 with 2,866 gold and the pretender name
    "Frasrutar" at turn 35, and at turn 37 the same block position reads id 0,
    gold 0 and no name — while the player confirms Frasrutar is alive and
    playing. A defeated nation looks different again: Oceania keeps its id, its
    gold and its pretender's name.

    So a zeroed id means neither death nor absence, and what it does mean is
    not known. This exists so a caller can see that the roster is incomplete
    rather than trusting a short list, which is the failure the roster's own
    docstring warns about at turn 2 and which recurred here.
    """
    out: list[int] = []
    blocks = find_nation_blocks(data)
    if len(blocks) < 2:
        return out
    kept: list[int] = [blocks[0]]
    for candidate in blocks[1:]:
        if candidate - kept[-1] >= _NATION_BLOCK_MIN_STRIDE:
            kept.append(candidate)
    for base in kept[:-1]:
        nation_id = struct.unpack_from("<H", data, base + _OFF_NATION_ID)[0]
        if not 1 <= nation_id <= 500:
            out.append(base)
    return out


def _find_gem_array(data: bytes, nation_name: str = "",
                    nation_id: Optional[int] = None) -> Optional[TrnGems]:
    """The player's gem stock, or None if it cannot be located unambiguously.

    Anchored on the player's own nation id rather than on a per-nation table of
    0xFF-run lengths. The roster in the Marignon fixture reads
    [1, 2, 3, 4, 61, 74, 76, 81, 85, 87] — four Special Monsters slots then
    Marignon, Xibalba, Machaka, Nidavangr, Ys and Oceania, which is exactly the
    six nations in that game, so the id field is not in doubt.

    Returning None when the anchor does not resolve is deliberate. The previous
    version fell back to "first block in a plausible size range", which cannot
    fail and therefore cannot warn — it silently returned another nation's gems.
    A missing value is visible; a confident wrong one is not.
    """
    if nation_id is None:
        nation_id = _nation_id_for_slug(nation_name)
    if nation_id is None:
        return None
    matches = [g for nid, _gold, g, _h in read_nation_roster(data) if nid == nation_id]
    if len(matches) != 1:            # 0 = not found, >1 = ambiguous
        return None
    return TrnGems(*struct.unpack_from("<9i", data, matches[0]))


def find_research(data: bytes, nation_id: Optional[int]) -> tuple[Optional[int], Optional[int]]:
    """(total research points, research per turn) for a nation.

    Only the total is confirmed. The second value is returned for inspection
    but is not the rate — see _OFF_UNKNOWN_596.

    The per-path split is NOT here either: Conjuration 83 / Construction 50 /
    Thaumaturgy 50 appear nowhere in the nation record, as points or as levels,
    so which school a point went into comes from somewhere else.
    """
    if nation_id is None:
        return None, None
    for nid, _gold, _gem, header in read_nation_roster(data):
        if nid == nation_id:
            return (struct.unpack_from("<H", data, header + _OFF_RESEARCH_POINTS)[0],
                    struct.unpack_from("<H", data, header + _OFF_UNKNOWN_596)[0])
    return None, None


def find_player_gold(data: bytes, nation_id: Optional[int]) -> Optional[int]:
    """Treasury, from the same nation record. 600 for every nation on turn 1."""
    if nation_id is None:
        return None
    golds = [gold for nid, gold, _g, _h in read_nation_roster(data) if nid == nation_id]
    return golds[0] if len(golds) == 1 else None


def find_claimed_throne_points(
    data: bytes, nation_id: Optional[int]
) -> Optional[int]:
    """The player's exact F9 claimed-Ascension-point total."""
    if nation_id is None:
        return None
    matches = [
        struct.unpack_from("<H", data, header + _OFF_CLAIMED_THRONE_POINTS)[0]
        for nid, _gold, _gem, header in read_nation_roster(data)
        if nid == nation_id
    ]
    return matches[0] if len(matches) == 1 else None


# Global enchantments are NOT one list of records. Each active enchantment is
# its own 46-byte entry, chained, and every entry carries its own 0x3102 marker:
#
#     i16 -1            terminator of the previous entry
#     u16 ?             0xffff on the first entry, 0 afterwards
#     i32 index         0, 1, 2, ...; -1 ends the chain
#     u16 0x3102        section marker
#     u16 <varies>      per-save value, 0c55/0c56 in turn 45
#     u16 0
#     ... 14-byte record ...
#     18 zero bytes
#
# Reading only the first entry is what made turn 45 report one enchantment when
# three were active: the second and third have `ff ff 00 00 01 00 00 00` where
# the first has `ff ff ff ff 00 00 00 00`, so a fixed anchor matches once.
_GLOBAL_EFFECT_ANCHOR = b"\xff\xff\xff\xff\x00\x00\x00\x00\x02\x31"
_GLOBAL_EFFECT_HEADER = len(_GLOBAL_EFFECT_ANCHOR) + 4
_GLOBAL_EFFECT_SIZE = 14
_GLOBAL_EFFECT_GAP = 18
_GLOBAL_SECTION_MARKER = 0x3102
_GLOBAL_CHAIN_LIMIT = 64


def _read_global_record(data: bytes, pos: int) -> TrnGlobalEffect | None:
    """One 14-byte enchantment record, or None if it is not plausible."""
    if pos + _GLOBAL_EFFECT_SIZE > len(data):
        return None
    (effect_id, spell_id, caster, province,
     state, value1, value2) = struct.unpack_from("<HHhhhhh", data, pos)
    if not (1 <= effect_id <= 2000 and 1 <= spell_id <= 5000
            and -1 <= caster <= 500 and -1 <= province <= 5000):
        return None
    return TrnGlobalEffect(
        effect_id=effect_id, spell_id=spell_id,
        caster_nation_id=caster, cast_province_id=province,
        state=state, value1=value1, value2=value2)


def _walk_global_chain(data: bytes, first_record: int) -> list[TrnGlobalEffect] | None:
    """Follow the chain from the first record to the -1 terminator.

    The link's integer is the **slot of the next entry**, not a running count.
    Slots are stable identifiers: dispelling the enchantment in slot 1 leaves
    slot 0 linking straight to slot 2 rather than renumbering it. Requiring a
    sequential counter here rejected the whole chain the first time a global
    was removed, which is how the behaviour was found.
    """
    effects: list[TrnGlobalEffect] = []
    pos = first_record
    slot = 0
    for _ in range(_GLOBAL_CHAIN_LIMIT):
        record = _read_global_record(data, pos)
        if record is None:
            return None
        effects.append(replace(record, slot=slot))
        link = pos + _GLOBAL_EFFECT_SIZE + _GLOBAL_EFFECT_GAP
        if link + 12 > len(data):
            return None
        if struct.unpack_from("<h", data, link)[0] != -1:
            return None
        next_slot = struct.unpack_from("<i", data, link + 4)[0]
        if next_slot == -1:
            return effects
        if not slot < next_slot <= _GLOBAL_CHAIN_LIMIT:
            return None
        if struct.unpack_from("<H", data, link + 8)[0] != _GLOBAL_SECTION_MARKER:
            return None
        slot = next_slot
        pos = link + 14
    return None


def read_global_effects(data: bytes) -> list[TrnGlobalEffect]:
    """Read the active enchantment chain from a `.trn`.

    Turn 15 decodes as Tapestry of Dreams (effect 138, spell 937) cast by
    nation 87; the controlled turn 45 decodes all three of Eternal Pyre (17),
    Foul Air (10) and Burden of Time (29), which the previous single-entry
    reader could not see.

    An empty list is a legitimate answer: the chain is omitted entirely when
    nothing is active.  That is why both earlier bugs here -- a header constant
    baked from one save, then reading only the first entry -- could return too
    few enchantments without ever raising.

    This list is player-visible on the Global Enchantments screen, so keeping
    it in the `.trn` parser does not cross the visibility boundary.  `value1`
    is the caster's overcast and is only truthful in that caster's own file;
    resolving that belongs to the visibility layer, which knows whose file this
    is.
    """
    candidates: list[list[TrnGlobalEffect]] = []
    pos = data.find(_GLOBAL_EFFECT_ANCHOR)
    while pos >= 0:
        chain = _walk_global_chain(data, pos + _GLOBAL_EFFECT_HEADER)
        if chain:
            candidates.append(chain)
        pos = data.find(_GLOBAL_EFFECT_ANCHOR, pos + 1)
    if not candidates:
        return []
    first = candidates[0]
    if any(
        [row.identity for row in other] != [row.identity for row in first]
        for other in candidates[1:]
    ):
        raise ValueError(
            f"{len(candidates)} global-enchantment chains disagree on which "
            "enchantments are active; refusing to choose one"
        )
    resolved: list[TrnGlobalEffect] = []
    for index, effect in enumerate(first):
        copies = [effect] + [other[index] for other in candidates[1:]]
        variants: tuple[int, ...] = tuple(dict.fromkeys(row.value1 for row in copies))
        ambiguous = len(variants) > 1 or len({row.value2 for row in copies}) > 1
        resolved.append(
            replace(effect, overcast_ambiguous=ambiguous, value1_variants=variants)
        )
    return resolved


# Persistent enchantments use this 44-byte record. Frost Dome and Trade Wind
# independently control every field and suffix. Eternal Pyre establishes that
# effect-81 global bookkeeping can use the structure too, so the raw parser is
# structural and the reference database filters effect 82 at the tool layer.
_LOCAL_ENCHANTMENT_MARKER = b"\x02\x31"
_LOCAL_ENCHANTMENT_SIZE = 44


def read_local_enchantments(data: bytes) -> list[TrnLocalEnchantment]:
    """Read structural persistent-enchantment records.

    These records occur in both human players' files, including their duration,
    so this raw function deliberately performs neither effect nor visibility
    filtering. Callers must use reference metadata and the player boundary.
    """

    found: list[TrnLocalEnchantment] = []
    seen: set[tuple[int, int, int, int, int, int]] = set()
    pos = data.find(_LOCAL_ENCHANTMENT_MARKER)
    while pos >= 0:
        if pos + _LOCAL_ENCHANTMENT_SIZE <= len(data):
            (marker, runtime, unknown, effect_argument, spell_id, nation_id,
             province_id, months_left) = struct.unpack_from(
                "<HHHHHHHH", data, pos)
            suffix = struct.unpack_from("<HHH", data, pos + 38)
            if (
                marker == _GLOBAL_SECTION_MARKER
                and runtime not in (0, 0xFFFE, 0xFFFF)
                and unknown == 0
                and 1 <= effect_argument < 0xFFFF
                and 1 <= spell_id <= 5000
                and 1 <= nation_id <= 500
                and 1 <= province_id <= _MAX_PROVINCE_ID
                and 1 <= months_left < 0xFFFF
                and suffix == (0xFFFF, 0, 0xFFFF)
            ):
                identity = (
                    runtime, effect_argument, spell_id, nation_id,
                    province_id, months_left,
                )
                if identity not in seen:
                    seen.add(identity)
                    found.append(TrnLocalEnchantment(*identity))
        pos = data.find(_LOCAL_ENCHANTMENT_MARKER, pos + 1)
    return found


ITEM_STATE_COUNT = 2000
ITEM_STATE_UNMADE = -99
ITEM_STATE_YEARNING = -98
_ITEM_STATE_TRAILER = b"\x1f\x1e\x00\x00"


def read_item_states(data: bytes) -> tuple[int, ...] | None:
    """Return the serialized per-item state array, indexed by item id.

    The 6.36 client initializes exactly 2,000 signed bytes to -99 and writes
    the array wholesale. A four-byte ``0xffffffff`` terminator immediately
    precedes it and the next world-state section begins with ``1f 1e 00 00``.
    This structural pair locates one array in every controlled player ``.trn``
    and in ``ftherlnd`` despite the absolute offset moving each turn.

    Values observed and confirmed against the client branches are -99
    (ordinary unmade), -98 (yearning), zero (spent/destroyed), and positive
    extant-copy counts capped at 125. Unknown layouts return ``None`` rather
    than interpreting an arbitrary 2,000-byte region as forge availability.
    """

    candidates: list[tuple[int, ...]] = []
    anchor = b"\xff\xff\xff\xff"
    anchor_at = data.find(anchor)
    while anchor_at >= 0:
        start = anchor_at + len(anchor)
        end = start + ITEM_STATE_COUNT
        if end + len(_ITEM_STATE_TRAILER) <= len(data):
            if data[end:end + 4] == _ITEM_STATE_TRAILER:
                raw = data[start:end]
                values = tuple(
                    value if value < 128 else value - 256 for value in raw)
                if all(
                    value in {ITEM_STATE_UNMADE, ITEM_STATE_YEARNING}
                    or 0 <= value <= 125
                    for value in values
                ):
                    candidates.append(values)
        anchor_at = data.find(anchor, anchor_at + 1)
    return candidates[0] if len(candidates) == 1 else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_bytes(data: bytes, nation_name: str = "",
                map_terrain: Optional[dict[int, int]] = None) -> TrnFile:
    """Parse raw .trn bytes and return a :class:`TrnFile`.

    Pass `nation_name` (e.g. "mid_pangaea") to locate the player's gem array.

    Pass `map_terrain` — `{province_id: terrain_bitmask}` from the game's .map
    file — to resolve records whose tail is shifted by 20 bytes. Without it the
    parser falls back to a heuristic that is right for every province in the
    sample except the one Pangaea capital that motivated all this, so supplying
    it is worth the two lines it costs. `parse()` does so automatically.
    """
    game_name, turn, province_count = _parse_header(data)

    rec_positions = _find_province_records(data)
    seen_ids: set[int] = set()
    provinces: list[TrnProvince] = []
    bodies: dict[int, int] = {}        # province_id -> body offset, for the capital re-read
    for rec_off, prov_id in rec_positions:
        if prov_id in seen_ids:
            continue
        seen_ids.add(prov_id)
        try:
            expected = map_terrain.get(prov_id) if map_terrain else None
            prov, body = _parse_province(data, rec_off, expected)
        except (struct.error, IndexError):
            continue
        provinces.append(prov)
        bodies[prov_id] = body

    nation_id   = _nation_id_for_slug(nation_name)
    player_gems = _find_gem_array(data, nation_name, nation_id)
    player_gold = find_player_gold(data, nation_id)
    research_points, research_speed = find_research(data, nation_id)
    research_state = find_research_state(data, research_points)
    if research_state is None and research_points is not None:
        # A zeroed/dead-pretender nation record can hide the old total while
        # leaving the exact player research block intact. Do not let a stale or
        # wrong total suppress the structurally identified own state.
        research_state = find_research_state(data)
    if research_state is not None:
        research_points = _research_total(
            research_state[0], research_state[1])

    # Capitals must be resolved after every province is known: the test is
    # "strongest dominion among a nation's provinces", which needs the full set.
    _mark_capitals(data, provinces, bodies)
    return TrnFile(
        game_name=game_name,
        turn=turn,
        province_count=province_count,
        player_gems=player_gems,
        player_gold=player_gold,
        research_points=research_points,
        research_speed=research_speed,
        research_levels=research_state[0] if research_state else None,
        research_progress=research_state[1] if research_state else None,
        learned_spell_ids=research_state[2] if research_state else None,
        research_progress_raw=(research_state_words(research_state[1])
                               if research_state else None),
        roster=read_nation_roster(data),
        nation_id=nation_id,
        provinces=provinces,
        global_effects=read_global_effects(data),
        local_enchantments=read_local_enchantments(data),
        item_states=read_item_states(data),
        claimed_throne_points=find_claimed_throne_points(data, nation_id),
    )


def parse(path: str | Path) -> TrnFile:
    """Parse a `.trn` file and return a :class:`TrnFile`.

    Picks up the game's .map files from the same directory when they are there
    (a random game writes them next to the save) and uses their terrain table
    to resolve province records whose tail is shifted by 20 bytes. Surface and
    underworld are merged, since province ids do not overlap between them.

    Raises :exc:`ValueError` if the file is not a valid .trn.
    Raises :exc:`OSError` / :exc:`FileNotFoundError` on I/O errors.
    """
    p = Path(path)
    terrain: dict[int, int] = {}
    try:
        from . import mapfile as _mapfile
        for mp in _mapfile.find_for_save(p.parent):
            terrain.update(_mapfile.parse(mp).terrain)
    except OSError:
        terrain = {}
    return parse_bytes(p.read_bytes(), nation_name=p.stem,
                       map_terrain=terrain or None)


# ---------------------------------------------------------------------------
# Province intelligence — what the province panel tells the player
# ---------------------------------------------------------------------------

#: The viewing nation followed by ``0xffff`` closes every intelligence record.
#: The first decode mistook Marignon's nation id 61 (``0x003d``) for a fixed
#: signature, which made every other nation's perfectly valid reports vanish.
#: Records are not at a fixed stride or indexed by province, so locate this
#: nation-specific terminator and read backwards.
_INTEL_SIG_OFFSET = 32          # the signature sits this far into the record


@dataclass
class ProvinceIntel:
    """What the player is told about a province they do not own.

    This is the *displayed* estimate, not the truth. The province panel reads
    "The province contains about 60 enemy units", and `enemy_units` is the
    number in that sentence — confirmed against four provinces read off the
    screen: Citala 60, Kratas 40, Omfolia 30, The Dawn Land 40.

    Serving this is legitimate where serving a unit count is not. The true
    rosters are in the file and the player cannot see them; this field is
    precisely what the game chose to show.
    """
    province_id: int
    estimate_uncertainty_percent: int  # +4: 50 dominion, 30 Scout, 10 Spy
    enemy_units: int            # +8, the number the panel prints
    has_our_unit: bool          # +16, see below
    raw: dict[str, int]         # the fields still undecoded, kept rather than dropped
    offset: int


def read_province_intel(data: bytes, nation_id: int) -> dict[int, ProvinceIntel]:
    """{province id: intelligence}, for provinces the player has scouted.

    Only provinces we have information on get a record — six in the save this
    was decoded from — so a province absent here is one we know nothing about,
    which is a different thing from one we know to be empty.

    **+16 is an intel-quality flag.** It reads 1 for Citala, Kratas and Omfolia
    and 0 for the other three, and those first three are exactly the provinces
    holding one of our stealthed units. It matches the panel text too: Kratas,
    where our Troubadour sits, names the enemy commander ("commanded by Hunerik
    the Priest") while the provinces without one do not.

    **+4 is the estimate's uncertainty percentage.** The controlled records
    line up with the game's three information tiers: 50 without a local unit,
    30 with a Scout, and 10 with a Spy.

    Fields +12, +20, +24 and +28 are kept in `raw` rather than reverse-mapped.
    Their exact player-visible interpretation is already serialized beside the
    record as a type-5 scouting sentence, including phrases such as "mainly
    Tritons" and named commanders. Serving that sentence avoids guessing from
    opaque selectors or leaking the exact hidden roster.
    """
    if not 0 <= nation_id <= 0xffff:
        raise ValueError(f"nation id {nation_id} does not fit the intel record")
    signature = struct.pack("<HH", nation_id, 0xffff)
    out: dict[int, ProvinceIntel] = {}
    i = 0
    while True:
        i = data.find(signature, i)
        if i < 0:
            break
        off = i - _INTEL_SIG_OFFSET
        i += len(signature)
        if off < 0 or off + _INTEL_SIG_OFFSET > len(data):
            continue
        pid = struct.unpack_from("<I", data, off)[0]
        if not (1 <= pid <= 5000):
            continue
        fields = {f"+{k}": struct.unpack_from("<I", data, off + k)[0]
                  for k in (12, 20, 24, 28)}
        out[pid] = ProvinceIntel(
            province_id=pid,
            estimate_uncertainty_percent=struct.unpack_from("<I", data, off + 4)[0],
            enemy_units=struct.unpack_from("<I", data, off + 8)[0],
            has_our_unit=bool(struct.unpack_from("<I", data, off + 16)[0]),
            raw=fields, offset=off)
    return out
