"""Reader for Dominions 6 `.2h` orders files.

The file the player writes and the host reads. Two things about it shape
everything else:

* It is written **only on save**. Orders issued in game live in RAM until then,
  so a diff has to be taken after saving, not after issuing orders.
* Saving grows it enormously — 537 bytes for an untouched turn 1, 11,804 after
  one save with orders — because it carries a copy of much of the player's
  state rather than a small orders delta.

The recruitment and nation research queues are decoded. Recruitment also gives
something more useful than the queue itself: **the true gold cost of every unit
queued**.

That matters because the scraped reference data does not contain unit costs at
all. `BaseU.csv` carries only `basecost`, the game's raw modifier — 10020 for
both Paladin and Knight of the Chalice, which really cost 215 and 70 — and
dom6inspector computes the displayed price from stats at render time. So costs
read here are the only authoritative ones available without reimplementing the
game's cost formula, and every save teaches us a few more.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"\x01\x02\x04DOM"

OFF_NATION    = 26
OFF_SUBMITTED = 30
OFF_GAME_NAME = 38
# The queue does NOT sit at a fixed offset. It was read at an absolute 151 for
# a long time and that is wrong: in the turn-22 and turn-23 files the structure
# ahead of it is 13 bytes longer and the queue starts at 164. Reading 151 there
# happened to return the right answer only because those queues are empty, so
# it found zeros either way — a non-empty queue in that layout would have been
# read as garbage.
#
# It is anchored instead on the four bytes that immediately precede it. Not a
# unique sequence — it recurs once or twice more later in every file — but its
# FIRST occurrence is the right one in all 46 saves with a queue, at 147 in the
# usual layout and 160 in the longer one. The search is bounded to the header
# so a later recurrence cannot be picked up if the real one is ever absent.
_QUEUE_ANCHOR = bytes.fromhex("6301ffff")
_ANCHOR_SEARCH_LIMIT = 400
OFF_QUEUE     = 151     # the usual result; kept for reference, not used to read
_MAX_QUEUE    = 64      # sanity bound; a real queue is a handful of entries

# Research is a nation-level queue of up to nine signed values. School ids 0-6
# mean "the next level of this school at this point in the queue". Level 9
# selections store their spell id directly in the same slot. The tenth signed
# word is the -1 terminator.
RESEARCH_QUEUE_CAPACITY = 9
RESEARCH_SCHOOL_COUNT = 7
RESEARCH_TARGET_MAX = 5000

# The queue is not at an absolute offset. It follows a self-checking copy of
# current research state:
#
#   i16 -1, u16 levels[7], u16 0, u16 progress_signature_words[7],
#   9 unknown u16,
#   i16 queue[9], i16 -1
#
# The .trn stores actual progress as display-order u32[7]. The .2h prefix copies
# only the first seven u16 words of that packed byte sequence. The prefix is
# built from those matching player-visible signature words and was unique in 24
# saved-order snapshots spanning turns 3-25. The distance from its start to the
# first queue entry is stable even as learned-spell and recruitment blocks ahead
# of it grow and shrink.
_RESEARCH_QUEUE_FROM_STATE = 50


class ResearchQueueNotLocated(RuntimeError):
    """The `.2h` research block did not match the current `.trn` state."""


def _research_state_signature(levels: list[int] | tuple[int, ...],
                              progress: list[int] | tuple[int, ...]) -> bytes:
    if len(levels) != RESEARCH_SCHOOL_COUNT:
        raise ValueError(f"expected 7 research levels, got {len(levels)}")
    if len(progress) != RESEARCH_SCHOOL_COUNT:
        raise ValueError(f"expected 7 research progress values, got {len(progress)}")
    if not all(0 <= value <= 0xFFFF for value in (*levels, *progress)):
        raise ValueError("research levels and progress must fit unsigned 16-bit fields")
    return (b"\xff\xff" + struct.pack("<7H", *levels) + b"\x00\x00"
            + struct.pack("<7H", *progress))


def research_queue_offset(data: bytes,
                          levels: list[int] | tuple[int, ...],
                          progress: list[int] | tuple[int, ...]) -> int:
    """Locate the first research-queue slot from matching `.trn` state.

    Refuses zero or multiple matches. A stale `.2h` can legitimately describe
    older research than the current `.trn`; writing it by approximation would
    be worse than leaving the research selection untouched.
    """
    signature = _research_state_signature(levels, progress)
    hits: list[int] = []
    at = 0
    while True:
        at = data.find(signature, at)
        if at < 0:
            break
        hits.append(at)
        at += 1
    if len(hits) != 1:
        raise ResearchQueueNotLocated(
            f"research-state anchor occurs {len(hits)} times; expected exactly one")
    return hits[0] + _RESEARCH_QUEUE_FROM_STATE


def read_research_queue(data: bytes,
                        levels: list[int] | tuple[int, ...],
                        progress: list[int] | tuple[int, ...]) -> list[int]:
    """Return queued school codes or Level 9 spell ids through the first -1."""
    start = research_queue_offset(data, levels, progress)
    queue: list[int] = []
    for slot in range(RESEARCH_QUEUE_CAPACITY + 1):
        value = struct.unpack_from("<h", data, start + slot * 2)[0]
        if value == -1:
            return queue
        if not 0 <= value <= RESEARCH_TARGET_MAX:
            raise ResearchQueueNotLocated(
                f"research queue slot {slot} contains invalid target {value}")
        if slot == RESEARCH_QUEUE_CAPACITY:
            break
        queue.append(value)
    raise ResearchQueueNotLocated(
        "research queue has no -1 terminator after its nine slots")


def set_research_queue(data: bytes, targets: list[int],
                       levels: list[int] | tuple[int, ...],
                       progress: list[int] | tuple[int, ...]) -> bytes:
    """Replace the mixed school-level and Level 9 spell research queue.

    Only occupied slots and the first terminator are written. The client leaves
    inconsistent zero/-1 values in unused trailing slots across saves, and the
    first -1 is what actually terminates the queue. Values 0-6 are school ids;
    larger values are direct spell ids, as confirmed with Tartarian Gate 1080
    and Ghost Riders 1078.
    """
    if len(targets) > RESEARCH_QUEUE_CAPACITY:
        raise ValueError(
            f"research queue holds at most {RESEARCH_QUEUE_CAPACITY} entries")
    if not all(isinstance(target, int) and not isinstance(target, bool)
               and 0 <= target <= RESEARCH_TARGET_MAX for target in targets):
        raise ValueError(
            f"research targets must be integers in the range 0-{RESEARCH_TARGET_MAX}")
    start = research_queue_offset(data, levels, progress)
    out = bytearray(data)
    for slot, target in enumerate(targets):
        struct.pack_into("<h", out, start + slot * 2, target)
    struct.pack_into("<h", out, start + len(targets) * 2, -1)
    return bytes(out)


def _queue_offset(data: bytes) -> int | None:
    """Where the FIRST recruitment queue starts, or None if there is none.

    Kept for the single-province case and for tests. `_queue_offsets` is what
    a correct reader wants: there is one queue per province represented by
    the inherited orders file, not one per file. A newly conquered province
    may already be owned while lacking a block.
    """
    offsets = _queue_offsets(data)
    return offsets[0] if offsets else None


def _queue_offsets(data: bytes) -> list[int]:
    """Every queue block, in file order.

    There is **one block per represented province**, but representation is
    sparse in mature saves rather than one block per owned province. This is
    the thing an absolute offset hid completely. Confirmed by queueing a Royal
    Guard in Marignon and a Knight of the Chalice in the Obsidian Waste in the
    same save: three anchors appeared for three represented provinces, the second holding
    134/50 and the third 135/70, with the province id sitting at a negative
    offset ahead of each — 86, 93, 98, ascending.

    Early samples often happened to represent every owned province. Turn-47
    dual-human controls disprove that as a format rule: Ermor has two blocks
    for nine owned provinces and Marignon three for twenty. The exact owner is
    joined by PlayerView to the adjacent province-name record.
    """
    out: list[int] = []
    at = 0
    while True:
        at = data.find(_QUEUE_ANCHOR, at)
        if at < 0:
            return out
        out.append(at + len(_QUEUE_ANCHOR))
        at += len(_QUEUE_ANCHOR)


@dataclass
class RecruitOrder:
    unit_type_id: int
    gold: int
    is_commander: bool = False


@dataclass
class ProvinceQueue:
    """One province's recruitment queue.

    `province_id` is None when the caller did not supply the owned-province
    list to match blocks against — the ids are in the file, but at a varying
    negative offset ahead of each block rather than a fixed one, so matching by
    order against the .trn is the reliable route.
    """
    province_id: int | None
    recruits: list[RecruitOrder] = field(default_factory=list)

    @property
    def gold(self) -> int:
        return sum(r.gold for r in self.recruits)


@dataclass
class OrdersFile:
    path: Path | None
    nation_id: int
    game_name: str
    queues: list[ProvinceQueue] = field(default_factory=list)

    @property
    def recruits(self) -> list[RecruitOrder]:
        """Everything queued, across every province."""
        return [r for q in self.queues for r in q.recruits]

    @property
    def gold_spent(self) -> int:
        return sum(r.gold for r in self.recruits)


def _decode_xor_string(data: bytes, offset: int) -> str:
    out: list[str] = []
    i = offset
    while i < len(data) and data[i] != 0x4F:
        out.append(chr(data[i] ^ 0x4F))
        i += 1
    return "".join(out)


def _read_queue(data: bytes, start: int) -> list[RecruitOrder]:
    """One block: commander ids, troop ids, then one cost per entry."""
    if start + OFF_COMMANDER_QUEUE_COUNT < 0:
        return []
    commander_count = struct.unpack_from(
        "<H", data, start + OFF_COMMANDER_QUEUE_COUNT)[0]
    troop_count = struct.unpack_from(
        "<H", data, start + OFF_TROOP_QUEUE_COUNT)[0]
    count = commander_count + troop_count
    if count == 0:
        return []
    if count > _MAX_QUEUE // 2 or start + count * 4 > len(data):
        return []
    ids = [struct.unpack_from("<H", data, start + i * 2)[0]
           for i in range(count)]
    costs = [struct.unpack_from("<H", data, start + (count + i) * 2)[0]
             for i in range(count)]
    if (not all(1 <= unit_id <= 5000 for unit_id in ids)
            or not all(0 <= cost <= 20000 for cost in costs)):
        return []
    return [
        RecruitOrder(unit_type_id=unit_id, gold=cost,
                     is_commander=index < commander_count)
        for index, (unit_id, cost) in enumerate(zip(ids, costs))
    ]


def parse_bytes(data: bytes,
                owned_provinces: list[int] | None = None) -> OrdersFile:
    """Parse a `.2h`.

    `owned_provinces` — our province ids in ascending order, from the matching
    `.trn` — lets each queue be attributed to its province. Without it the
    queues still parse, they are simply unlabelled.
    """
    if data[:len(MAGIC)] != MAGIC:
        raise ValueError(f"not a .2h file: magic {data[:len(MAGIC)]!r}")

    # The queue is two parallel arrays: type ids from OFF_QUEUE, then one gold
    # cost each, immediately after. The split point is not stored, so it is
    # found by reading ids until the run stops looking like unit ids and
    # checking that the same number of plausible costs follows. Verified
    # against the game: 2 Paladins, 1 Assassin, 1 Knight of the Chalice, 1
    # Swordsman and 1 Crossbowman came to 215+215+80+70+10+10 = 600, exactly
    # the player's treasury change. The sum is the check that makes this safe —
    # a wrong split does not add up.
    starts = _queue_offsets(data)
    # PlayerView supplies the exact sparse province ids in file order after
    # joining each anchor to its adjacent province-name record. Only pair when
    # the counts agree; an unattributed queue is safer than a wrong province.
    labels: list[int | None] = [None] * len(starts)
    if owned_provinces and len(owned_provinces) == len(starts):
        labels = list(owned_provinces)

    queues = [ProvinceQueue(province_id=label, recruits=_read_queue(data, s))
              for s, label in zip(starts, labels)]

    return OrdersFile(
        path=None,
        nation_id=struct.unpack_from("<H", data, OFF_NATION)[0],
        game_name=_decode_xor_string(data, OFF_GAME_NAME),
        queues=queues,
    )


def parse(path: str | Path,
          owned_provinces: list[int] | None = None) -> OrdersFile:
    p = Path(path)
    out = parse_bytes(p.read_bytes(), owned_provinces=owned_provinces)
    out.path = p
    return out


#: The gold left after everything queued is paid for — what the recruitment
#: screen shows as remaining. A u32 twelve bytes into a nation-specific
#: pattern. The four bytes between the fixed pieces are the nation id; the
#: original Marignon-only anchor accidentally hard-coded nation 61 there.
#:
#: The game maintains this itself, and an editor that splices the queue without
#: touching it leaves the player looking at a stale figure. That is exactly what
#: happened: replacing a 120-gold queue with a 20-gold one left the screen
#: still reading 6157 rather than 6257, because 6157 was simply still sitting
#: here. It is not derived from the queue at load time — it IS the number.
_GOLD_PREFIX = bytes.fromhex("1a02ffffffff")
_GOLD_SUFFIX = bytes.fromhex("0231")
OFF_GOLD_REMAINING = 12


def _gold_remaining_offset(data: bytes | bytearray) -> int | None:
    """Locate the unique remaining-gold value for this file's nation."""
    if len(data) < OFF_NATION + 2:
        return None
    nation_id = struct.unpack_from("<H", data, OFF_NATION)[0]
    pattern = _GOLD_PREFIX + struct.pack("<I", nation_id) + _GOLD_SUFFIX
    at = data.find(pattern)
    if (at < 0 or data.find(pattern, at + 1) >= 0
            or at + OFF_GOLD_REMAINING + 4 > len(data)):
        return None
    return at + OFF_GOLD_REMAINING

#: Separate queue lengths immediately ahead of the first id. The arrays are
#: physically flat, but commanders come first and these two counts tell the
#: game where the commander queue ends and the ordinary troop queue begins.
#: A game-authored turn-1 save with three of each stores 3 at both offsets.
OFF_COMMANDER_QUEUE_COUNT = -32
OFF_TROOP_QUEUE_COUNT = -30
# Backward-compatible name for code that specifically means ordinary troops.
OFF_QUEUE_COUNT = OFF_TROOP_QUEUE_COUNT
#: Province-local fort definition selected for a code-20 construction order.
#: A generated commander order with parameter 2 but this byte left at 0 loaded
#: as Build Palisades in an already-palisaded province and crashed when edited.
#: The controlled saves carry 1 for Palisades and 2 for Fortress here.
OFF_FORT_CONSTRUCTION = 22
#: Desired province-defence level for this turn.  Same-turn controlled saves
#: put Copper Canyons 1 -> 2 and Marignon 25 -> 26 here, at the matching
#: province queue anchor in each case.  The client permits purchases through
#: 100 and stores the complete target, not the number of points just bought.
OFF_PROVINCE_DEFENCE = -14
MAX_PROVINCE_DEFENCE = 100


class QueueWriteRefused(RuntimeError):
    """A recruitment change this writer will not make."""


class SubmissionStateError(RuntimeError):
    """The two independently located turn-submission flags are not writable."""


def set_recruits(data: bytes, province_id: int, units: list[RecruitOrder],
                 owned_provinces: list[int]) -> bytes:
    """Replace one province's recruitment queue. Returns the new bytes.

Lengths may differ from what is already queued: the block is spliced and
    the counts at `OFF_COMMANDER_QUEUE_COUNT` and `OFF_TROOP_QUEUE_COUNT` are
    updated, so the file grows or shrinks by four bytes per unit. Commanders
    are placed before troops. That is what the game does — queueing two units
    into an empty save grew it by exactly eight bytes.

    The cost is written as given rather than recomputed here. It is the
    caller's job to supply the price the game charges — `unit_cost` computes it
    and `observed_unit_costs` records it — because a wrong number here may
    desync the treasury, and this function cannot tell a right one from a
    wrong one.
    """
    starts = _queue_offsets(data)
    ordered = list(owned_provinces)
    if len(starts) != len(ordered):
        raise QueueWriteRefused(
            f"{len(starts)} queue blocks against {len(ordered)} represented "
            "provinces, so a "
            "block cannot be attributed to a province safely")
    if province_id not in ordered:
        raise QueueWriteRefused(
            f"province {province_id} has no represented block: {ordered}")

    start = starts[ordered.index(province_id)]
    existing = _read_queue(data, start)
    for unit in units:
        if not (1 <= unit.unit_type_id <= 5000):
            raise QueueWriteRefused(f"implausible unit id {unit.unit_type_id}")
        if not (0 <= unit.gold <= 20000):
            raise QueueWriteRefused(f"implausible cost {unit.gold}")
    if len(units) > _MAX_QUEUE // 2:
        raise QueueWriteRefused(
            f"{len(units)} is more than this reader will parse back")

    old_count = len(existing)
    commanders = [unit for unit in units if unit.is_commander]
    troops = [unit for unit in units if not unit.is_commander]
    ordered_units = commanders + troops
    new_count = len(ordered_units)
    body = b"".join(
        struct.pack("<H", unit.unit_type_id) for unit in ordered_units)
    body += b"".join(struct.pack("<H", unit.gold) for unit in ordered_units)

    # Splice rather than overwrite. The block is followed immediately by three
    # u32 fields and a marker, so a longer queue has to push them along; the
    # file grows by four bytes per added unit, which is exactly what the game
    # itself did when the player queued two units into an empty save.
    out = bytearray(data[:start] + body + data[start + old_count * 4:])
    struct.pack_into("<H", out, start + OFF_COMMANDER_QUEUE_COUNT,
                     len(commanders))
    struct.pack_into("<H", out, start + OFF_TROOP_QUEUE_COUNT, len(troops))
    return bytes(out)


def fort_construction(data: bytes, province_id: int,
                      owned_provinces: list[int]) -> int:
    """Province queue's selected fort definition, zero when none."""
    starts = _queue_offsets(data)
    ordered = list(owned_provinces)
    if len(starts) != len(ordered) or province_id not in ordered:
        raise QueueWriteRefused(
            f"cannot attribute a construction selector to province {province_id}")
    return data[starts[ordered.index(province_id)] + OFF_FORT_CONSTRUCTION]


def set_fort_construction(data: bytes, province_id: int, fort_definition: int,
                          owned_provinces: list[int]) -> bytes:
    """Set the required province-local half of a code-20 fort order."""
    if not 1 <= fort_definition <= 31:
        raise QueueWriteRefused(
            f"implausible fort definition {fort_definition}")
    starts = _queue_offsets(data)
    ordered = list(owned_provinces)
    if len(starts) != len(ordered):
        raise QueueWriteRefused(
            f"{len(starts)} queue blocks against {len(ordered)} represented provinces")
    if province_id not in ordered:
        raise QueueWriteRefused(f"province {province_id} has no represented block")
    out = bytearray(data)
    out[starts[ordered.index(province_id)] + OFF_FORT_CONSTRUCTION] = (
        fort_definition)
    return bytes(out)


def province_defence(data: bytes, province_id: int,
                     owned_provinces: list[int]) -> int:
    """Desired PD in one owned province, including this turn's purchases."""
    starts = _queue_offsets(data)
    ordered = list(owned_provinces)
    if len(starts) != len(ordered):
        raise QueueWriteRefused(
            f"{len(starts)} queue blocks against {len(ordered)} represented provinces")
    if province_id not in ordered:
        raise QueueWriteRefused(f"province {province_id} has no represented block")
    return data[starts[ordered.index(province_id)] + OFF_PROVINCE_DEFENCE]


def set_province_defence(data: bytes, province_id: int, target: int,
                         owned_provinces: list[int]) -> bytes:
    """Set an owned province's complete PD target for this turn."""
    if (not isinstance(target, int) or isinstance(target, bool)
            or not 0 <= target <= MAX_PROVINCE_DEFENCE):
        raise QueueWriteRefused(
            f"province defence target must be 0-{MAX_PROVINCE_DEFENCE}, "
            f"got {target!r}")
    starts = _queue_offsets(data)
    ordered = list(owned_provinces)
    if len(starts) != len(ordered):
        raise QueueWriteRefused(
            f"{len(starts)} queue blocks against {len(ordered)} represented provinces")
    if province_id not in ordered:
        raise QueueWriteRefused(f"province {province_id} has no represented block")
    out = bytearray(data)
    out[starts[ordered.index(province_id)] + OFF_PROVINCE_DEFENCE] = target
    return bytes(out)


def province_defence_cost(level: int) -> int:
    """Total gold represented by PD levels 1..``level``.

    The client charges the new level on every increment: 1 -> 2 cost two gold,
    25 -> 26 cost 26, and 26 -> 100 cost 4699.  Taking the difference between
    two triangular totals therefore gives both reservations and refunds.
    """
    if (not isinstance(level, int) or isinstance(level, bool)
            or not 0 <= level <= MAX_PROVINCE_DEFENCE):
        raise ValueError(
            f"province defence level must be 0-{MAX_PROVINCE_DEFENCE}, "
            f"got {level!r}")
    return level * (level + 1) // 2


# The national gem treasury is nine signed 32-bit counts in FAWESDNGB order.
# Its absolute offset moves as variable-length blocks ahead of it change.  The
# four zero bytes and one byte below follow the array, then a u16 holding the
# player nation id plus one.  Immediately before the array are thirteen zero
# bytes, one save-state flag byte (0 or 1), and 0xff.  That complete shape
# locates the array exactly once in all 278 expanded `.2h` files on hand,
# across the older single-player Marignon layout and both players in the
# dual-human game.  It also locates Ermor's legitimate all-zero turn-4 pool.
# The tiny turn-1 bootstrap files contain no writable order structure and no
# match, which is the correct refusal.
GEM_PATHS = ("fire", "air", "water", "earth", "astral",
             "death", "nature", "glamour", "blood")
_GEM_TREASURY_SUFFIX = b"\x00" * 4 + b"\x01"
_GEM_TREASURY_BYTES = len(GEM_PATHS) * 4


def gem_treasury_offset(data: bytes) -> int:
    """Locate the first national-gem count, refusing ambiguity."""
    hits: list[int] = []
    at = 0
    while True:
        at = data.find(_GEM_TREASURY_SUFFIX, at)
        if at < 0:
            break
        candidate = at - _GEM_TREASURY_BYTES
        at += 1
        if candidate < 15 or candidate + _GEM_TREASURY_BYTES + 7 > len(data):
            continue
        if data[candidate - 15] != 0xff:
            continue
        if data[candidate - 14] not in (0, 1):
            continue
        if data[candidate - 13:candidate] != b"\x00" * 13:
            continue
        nation_plus_one = struct.unpack_from(
            "<H", data, candidate + _GEM_TREASURY_BYTES + 5)[0]
        if not 1 <= nation_plus_one <= 501:
            continue
        values = struct.unpack_from("<9i", data, candidate)
        if any(value < 0 or value > 1_000_000 for value in values):
            continue
        hits.append(candidate)
    if len(hits) != 1:
        raise QueueWriteRefused(
            f"gem-treasury structure occurs {len(hits)} times; expected exactly one")
    return hits[0]


def submission_flag_offsets(data: bytes) -> tuple[int, int]:
    """Return the independently anchored header and treasury state bytes.

    Pressing End Turn changes both bytes from one to zero.  The first is fixed
    in the file header; the second moves with the variable-length structures
    before the national gem treasury.  Locating both rather than searching for
    a loose byte pattern makes a mixed or bootstrap file a refusal instead of
    a guessed write.
    """
    if not data.startswith(MAGIC) or len(data) <= OFF_SUBMITTED:
        raise SubmissionStateError("not an expanded Dominions .2h file")
    try:
        treasury = gem_treasury_offset(data)
    except QueueWriteRefused as exc:
        raise SubmissionStateError(
            "the expanded turn-state structure could not be located; "
            "a first-turn bootstrap file must be opened and saved in game once"
        ) from exc
    return OFF_SUBMITTED, treasury - 14


def turn_is_submitted(data: bytes) -> bool:
    """Whether both game-authored submission flags say the turn is finished."""
    header, treasury = submission_flag_offsets(data)
    flags = (data[header], data[treasury])
    if flags not in ((0, 0), (1, 1)):
        raise SubmissionStateError(
            f"submission flags disagree or are invalid: {flags}; expected "
            "both zero (submitted) or both one (unfinished)"
        )
    return flags == (0, 0)


def mark_turn_submitted(data: bytes) -> bytes:
    """Set the two decoded End Turn flags, preserving every other byte.

    The client's final two-byte trailer is deliberately left stale.  Dominions
    has been confirmed to load and host edited order files without validating
    it, while inventing a replacement would destroy useful provenance.
    """
    header, treasury = submission_flag_offsets(data)
    # Validate the pair before making either write.  This also makes a repeat
    # call safely idempotent.
    if turn_is_submitted(data):
        return data
    out = bytearray(data)
    out[header] = 0
    out[treasury] = 0
    return bytes(out)


def gem_remaining(data: bytes) -> tuple[int, ...]:
    """National gems left after current orders reserve their costs."""
    start = gem_treasury_offset(data)
    values = struct.unpack_from("<9i", data, start)
    if any(value < 0 or value > 1_000_000 for value in values):
        raise QueueWriteRefused(
            f"implausible gem treasury {dict(zip(GEM_PATHS, values))}")
    return values


def set_gem_remaining(data: bytes, values: list[int] | tuple[int, ...]) -> bytes:
    """Write all nine remaining-gem counts after commitment deltas."""
    if len(values) != len(GEM_PATHS):
        raise QueueWriteRefused(
            f"expected {len(GEM_PATHS)} gem paths, got {len(values)}")
    if any(not isinstance(value, int) or isinstance(value, bool)
           or value < 0 or value > 1_000_000 for value in values):
        raise QueueWriteRefused(
            f"implausible gem treasury {dict(zip(GEM_PATHS, values))}")
    out = bytearray(data)
    struct.pack_into("<9i", out, gem_treasury_offset(data), *values)
    return bytes(out)


# The current-turn magic-item treasury follows a unique eight-byte nation
# state marker. Unlike the turn-start copy in the .trn, this array changes as
# the player equips and removes items in the planning screen. It is a sequence
# of u16 item ids/zeroed worn slots terminated by 0xFFFF. Turns 34-44 keep the
# marker stable while the absolute start moves 1941 -> 2460; the three clean
# turn-44 controls independently put Just Man's Cross and Chi Shoes into their
# expected zeroed slots when worn.
_ITEM_STASH_PREFIX = bytes.fromhex("01000000fc000000")
_ITEM_STASH_MAX_SLOTS = 64


def item_stash_offset(data: bytes | bytearray) -> int:
    """Locate the current `.2h` magic-item treasury, refusing ambiguity."""
    hits: list[int] = []
    at = 0
    while True:
        at = data.find(_ITEM_STASH_PREFIX, at)
        if at < 0:
            break
        hits.append(at + len(_ITEM_STASH_PREFIX))
        at += 1
    if len(hits) != 1:
        raise QueueWriteRefused(
            f"item-treasury anchor occurs {len(hits)} times; expected exactly one")
    return hits[0]


def read_item_stash_slots(data: bytes | bytearray) -> list[int]:
    """Raw current item slots through the first 0xFFFF terminator.

    Zero is a real placeholder for an item currently worn by a commander and
    is retained here so a removed item can return to that slot. Use
    :func:`read_item_stash` for the player-facing unequipped list.
    """
    start = item_stash_offset(data)
    slots: list[int] = []
    for index in range(_ITEM_STASH_MAX_SLOTS):
        value = struct.unpack_from("<H", data, start + index * 2)[0]
        if value == 0xFFFF:
            return slots
        slots.append(value)
    raise QueueWriteRefused(
        f"item treasury has no terminator within {_ITEM_STASH_MAX_SLOTS} slots")


def read_item_stash(data: bytes | bytearray) -> list[int]:
    """Current unequipped magic-item ids in treasury order."""
    return [item_id for item_id in read_item_stash_slots(data) if item_id]


# Mercenary bids sit immediately before the pretender's design record, as two
# parallel ten-slot arrays.  The six-byte anchor below is invariant: it occurs
# exactly once in all 80 expanded Marignon `.2h` files on hand, spanning turns
# 1 through 30, and in every one of them the pretender's name begins exactly 46
# bytes later.  It is absent only from the 537-byte turn-1 stub, which carries
# no writable order structure at all.
#
# Established by one controlled save on turn 30: the player bid 747 on
# Nergash's Damned Legion, the only company on offer, and exactly three
# semantic values moved — amount slot 0 became 747, key slot 0 became 93 where
# it had been 0xFFFF, and remaining gold fell 7168 -> 6421, an exact 747
# reservation.
_MERCENARY_BID_ANCHOR = bytes.fromhex("7d3701000100")
MERCENARY_BID_SLOTS = 10
OFF_MERCENARY_BID_AMOUNTS = 6
OFF_MERCENARY_BID_PROVINCES = 26
MERCENARY_BID_PROVINCE_EMPTY = 0xFFFF


def _mercenary_province_offset(amounts_at: int) -> int:
    """The arrival-province array, from the amount array's own offset."""
    return (amounts_at - OFF_MERCENARY_BID_AMOUNTS
            + OFF_MERCENARY_BID_PROVINCES)


@dataclass
class MercenaryBid:
    """One standing bid: how much, and where the company would arrive.

    `province` is confirmed two ways. Holding the bid at 900 and changing only
    the selected land moved exactly one semantic byte, 93 -> 86, and the hire
    screen states in words that a successful bid delivers the company to the
    province named beside it. Both observed values — the capital with a
    Citadel and Copper Canyons with no fort — are provinces this nation owns.

    Nothing in this structure identifies the *company*: the slot index does,
    and it is the company's position in the auction list. Confirmed by a
    controlled save that added a second bid without disturbing the first.
    """
    slot: int
    amount: int
    province: int


def mercenary_bid_offset(data: bytes) -> int:
    """Locate the first bid-amount slot, refusing ambiguity."""
    hits: list[int] = []
    at = 0
    while True:
        at = data.find(_MERCENARY_BID_ANCHOR, at)
        if at < 0:
            break
        hits.append(at + OFF_MERCENARY_BID_AMOUNTS)
        at += 1
    if len(hits) != 1:
        raise QueueWriteRefused(
            f"mercenary-bid anchor occurs {len(hits)} times; expected exactly "
            f"one. This file's layout is not the confirmed one and a bid "
            f"written into it would land on unrelated bytes.")
    return hits[0]


def read_mercenary_bids(data: bytes) -> list[MercenaryBid]:
    """Every standing bid, as (slot, amount, key). Empty slots are omitted.

    An amount of 0 and a key of 0xFFFF both mean "no bid", and the two arrays
    must agree: a slot carrying one without the other means this reading is
    wrong about the layout, so it raises rather than reporting half a bid.
    """
    start = mercenary_bid_offset(data)
    provinces_at = _mercenary_province_offset(start)
    amounts = struct.unpack_from(f"<{MERCENARY_BID_SLOTS}H", data, start)
    provinces = struct.unpack_from(f"<{MERCENARY_BID_SLOTS}H", data,
                                   provinces_at)
    out: list[MercenaryBid] = []
    for slot, (amount, province) in enumerate(zip(amounts, provinces)):
        bid = amount != 0
        landed = province != MERCENARY_BID_PROVINCE_EMPTY
        if bid != landed:
            raise QueueWriteRefused(
                f"mercenary bid slot {slot} has amount {amount} and province "
                f"{province}; a bid must set both or neither")
        if bid:
            out.append(MercenaryBid(slot=slot, amount=amount,
                                    province=province))
    return out


def set_mercenary_bid(data: bytes, slot: int, amount: int | None,
                      province: int | None = None) -> bytes:
    """Place, replace or withdraw one bid. Does NOT touch remaining gold.

    `amount=None` withdraws, restoring both slots to the empty values the
    client writes. Gold is left to the caller because a bid is one commitment
    among several sharing that field, and reconciling them separately is how
    the recruitment and construction writers already lose track of each other.

    The slot is the company's position in the auction list, confirmed by a
    controlled save: with 900 already standing on the first company, bidding
    611 on the second wrote 611 into amount slot 1 and its province into
    province slot 1, left slot 0 untouched, and moved gold by exactly the new
    bid. Seven bytes changed in the whole file. Both arrays are indexed the
    same way, and a company under contract to another nation can be bid on.
    """
    if not 0 <= slot < MERCENARY_BID_SLOTS:
        raise QueueWriteRefused(
            f"mercenary bid slot must be 0-{MERCENARY_BID_SLOTS - 1}, "
            f"got {slot}")
    start = mercenary_bid_offset(data)
    provinces_at = _mercenary_province_offset(start)
    out = bytearray(data)
    if amount is None:
        if province is not None:
            raise QueueWriteRefused(
                "withdrawing a bid takes no province; pass amount and "
                "province together or neither")
        struct.pack_into("<H", out, start + 2 * slot, 0)
        struct.pack_into("<H", out, provinces_at + 2 * slot,
                         MERCENARY_BID_PROVINCE_EMPTY)
        return bytes(out)
    if province is None:
        raise QueueWriteRefused(
            "a bid must name the province the company would arrive in; the "
            "hire screen always has one selected")
    if not 1 <= amount <= 0xFFFF:
        raise QueueWriteRefused(f"implausible mercenary bid {amount}")
    if not 1 <= province < MERCENARY_BID_PROVINCE_EMPTY:
        raise QueueWriteRefused(f"implausible arrival province {province}")
    struct.pack_into("<H", out, start + 2 * slot, amount)
    struct.pack_into("<H", out, provinces_at + 2 * slot, province)
    return bytes(out)


def set_gold_remaining_value(data: bytes, remaining: int) -> bytes:
    """Write an already-computed remaining-gold value."""
    if not 0 <= remaining <= 0xFFFFFFFF:
        raise QueueWriteRefused(f"implausible remaining gold {remaining}")
    at = _gold_remaining_offset(data)
    if at is None:
        raise QueueWriteRefused("cannot locate the remaining-gold field")
    out = bytearray(data)
    struct.pack_into("<I", out, at, remaining)
    return bytes(out)


def gold_remaining(data: bytes) -> int | None:
    """The figure the recruitment screen shows as gold left, or None."""
    at = _gold_remaining_offset(data)
    if at is None:
        return None
    return int(struct.unpack_from("<I", data, at)[0])


def set_gold_remaining(data: bytes, treasury: int) -> bytes:
    """Rewrite the remaining-gold figure from the treasury and what is queued.

    Must be called after every queue change. The game does not recompute this
    on load — it displays it — so a spliced queue with an untouched figure
    shows the player a number describing a state that no longer exists.
    """
    at = _gold_remaining_offset(data)
    if at is None:
        raise QueueWriteRefused(
            "cannot locate the remaining-gold field; refusing to leave it "
            "stale, which would show a figure for a queue that is not there")
    spent = sum(r.gold for r in parse_bytes(data).recruits)
    remaining = treasury - spent
    if remaining < 0:
        raise QueueWriteRefused(
            f"queued units cost {spent} against a treasury of {treasury}")
    out = bytearray(data)
    struct.pack_into("<I", out, at, remaining)
    return bytes(out)
