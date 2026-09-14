"""Troop records in `.trn` files.

Every soldier is stored individually rather than as a (type, count) squad —
which makes sense, because each one can pick up its own afflictions, diseases
and experience. A 20-strong squad is 20 records.

Layout established against a controlled fixture: `example_game`,
MA Marignon, turn 1, untouched — 20 Pikeneer (type 221) and 15 Crossbowman
(type 218) under one commander, nothing moved or damaged.

    record size: 173 bytes, laid out contiguously per squad

    +0    u16   unit type id      -> reference.sqlite3 units.id
    +2    u16   hit points        (10 for both fixture types; all undamaged)
    +4    u16   unknown, 93 in every fixture record
    +6    u16   unknown, 93 in every fixture record
    +30   u8    age (21-29 for soldiers, 82 for the pretender)

    +36   u16   0 for plain troops; 0xFFFF for commanders, for mounts, and for
                mounted troops such as Knights of the Chalice. NOT an
                is-commander flag — Knights are troops and read 0xFFFF.

    +38   u16   KILL COUNT (mirrored at +40). Confirmed three ways at turn 6:
                Clodius the Assassin reads 6 and the Hall of Fame screen lists
                him with 6 kills; the Crossbowmen sum to 6, exactly the kills
                the player reported for that squad; and the surviving Pikeneers
                sum to 6 of their reported 12, the remainder having died at The
                Obsidian Waste and taken their kills with them.

    +49   u16   0xFFFF on unmounted units, 0xFFFE on every mounted rider
                (Knights of the Chalice, Paladins), and a distinct small value
                on each Destrier - 2997, 4356, 6983, 6985, 2560, 6312, 6314.
                That shape is a rider-mount link seen from the mount's side,
                which would give a second route to the pairing that record
                adjacency currently supplies.

    +53   u16   0 everywhere except the dormant pretender, where it holds 3894,
                his own unit type id.
    +32   u16   owner nation id   (61 = MA Marignon, confirmed)
    +34   i32   -1 marker         (empty slot: affliction or item?)
    +49   i32   -1 marker         (empty slot)
    +141  u8    unit instance id  (Pikeneer 13-32, Crossbowman 33-47)
    +30   u16   age               (NOT u8 — pretenders are 300-2046 years old,
                                     which is why +31 read 1 or 7 and looked
                                     like a separate chassis field)
    +141  u16   instance id       (spans +142; NOT id byte + size)
    +34   u16   SQUAD ID. Found by searching every offset for a field that
                satisfies the player's stated roster rather than by guessing:
                Turgis's four Pikeneers must agree, his thirteen Crossbowmen
                must agree, and the two groups must differ. Exactly one
                non-trivial offset passes, and its grouping reproduces both
                squads including the freshly recruited Crossbowman #226 that
                +169 had stranded. 0xFFFF means attached to no commander —
                commanders themselves, their mounts, garrison troops and PD.

                INCOMPLETE: the three Pikeneers who routed home from The
                Obsidian Waste read 0 in turns 3, 4 and 5 alike, and the player
                has since placed them under two commanders in three separate
                squads. So +34 separates Turgis's two squads correctly and 17
                of 20 squadded units, but does not distinguish these three. It
                is a real grouping field and not the whole story.

    MOUNTS      A mount's record immediately FOLLOWS its rider's in file order,
                every time: Knight#1 then Destrier#4, Paladin#169 then
                Destrier#170, Knight#74 then Destrier#79. That is the link,
                and it is needed because "Destrier" covers six different type
                ids — Knights and Paladins ride different variants — so a mount
                cannot be recognised from its type alone.

    +169  u16   NOT a squad id — retracted. It changes when a unit is
                reassigned, which a cross-turn diff proved, but equal values do
                NOT mean same squad: Turgis's four Pikeneers are one squad in
                game and carry four different values here, while twelve of his
                thirteen Crossbowmen share one. The diff established that the
                field tracks assignment, and that was over-read as identity. Confirmed by diffing turn 4 against turn 5 over
                a turn in which the player moved exactly two Pikeneers from one
                commander to another and split them across two squads: those
                two records, and only those two, changed here, and they came
                out holding DIFFERENT values from each other. A per-commander
                field could not do that.

                The low half of a commander's .2h token equals the id of one of
                his squads, which is why earlier readings looked like they were
                tracking commanders.

Only 19 of the 173 bytes are non-zero in the fixture. That is expected — a
fresh unit has no experience, no afflictions, no items and no orders — but it
does mean most of the record is still unmapped, and fields will only reveal
themselves in a game where units have taken damage and been given orders.

Size is not in this record at all — it comes from the reference data, as every
other stat does.

Confidence: type id, nation id and instance id are confirmed. HP matches
the reference data but every fixture unit shares one value, so they are
consistent rather than proven. Age is a guess from its range and variance.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

RECORD_SIZE = 173

OFF_EXPERIENCE = 8     # u8; see the module docstring
OFF_KILLS      = 38    # u16, mirrored at +40

# Afflictions: u32, 28 bytes BEFORE the record's type id. Confirmed unit for
# unit against the player's report at turn 6 — two Pikeneers aged 22 with limp,
# one aged 25 cursed, a Crossbowman aged 23 with a never healing wound and one
# aged 22 with limp. All five match, and nothing else is afflicted.
#
# The negative offset is not a mistake. Reading it at +145 attributes every
# value to the record before its owner, which is the same off-by-one this
# format has produced for gems, for order blocks and for squad ids. Here the
# ground truth was specific enough to see the shift rather than adopt it.
# Four bytes immediately before the affliction mask are the live-unit handle.
# It is the missing commander join: the same u32 is the first field after a
# commander's name in both the `.trn` commander block and our `.2h` order
# block.  Turn 30 joins all 18 Marignon commanders uniquely, including the 13
# who lead no troops.  Examples (handle -> type/current province):
#
#   779  -> Dapamort, Friar 148, Marignon 93
#   6312 -> Turgis, Paladin 440, Obsidian Waste 98
#   6935 -> Sugaar, Serpent 3894, Marignon 93
#
# This extends the record's owned prefix from -28 to -32.  It is not the unit
# instance id at +141; the two occasionally happen to be equal and that
# coincidence is why the field needs its own name.
OFF_RUNTIME_INDEX = -32  # u32; joins a named commander/order block to this unit
OFF_AFFLICTIONS = -28  # u32
OFF_SQUAD      = 34    # u16; units sharing this value form one squad
# RETRACTED: OFF_ASSIGNMENT at +169 read as a u16. +169 is the FOLLOWING
# record's -4, and that field is a u32 warband token. Reading half of the next
# unit's token as this unit's "assignment" produced a value that changed when
# squads changed - because the neighbouring record moved too - which is why it
# survived so long. A unit's OWN token is at -4.
OFF_WARBAND_OWN = -4   # u32; 0xFFFFFFFF when following no commander
NO_SQUAD     = 0xFFFF  # attached to no commander: garrison, PD, mounts,
                       # freshly recruited troops, and commanders themselves


OFF_TYPE_ID = 0
OFF_HP = 2
OFF_HOME = 6      # u16; where the unit was recruited, never 0 on a real one
OFF_AGE = 30
OFF_NATION = 32
# CONFIRMED against the player's roster: "every Knight of the Chalice and every
# Paladin rides a Destrier, and thus is technically two units". Marignon fields
# 5 Knights and 2 Paladins, so 7 riders and 7 Destriers, and these two fields
# split the 38 units exactly that way with no exceptions:
#
#   +51 reads 0 on all 7 Destriers and 0xFFFF on all 31 other units
#   +167 reads 1 on exactly those 7 riders and 0 everywhere else
#
# They are complementary halves of the same relationship, which is why neither
# was believable alone — a lone flag matching 7 units could be anything, but a
# flag marking the mounts and a flag marking exactly their riders could not.
# The two are not equally solid, and turn 9 showed the difference:
#
#   +51  clean at every turn checked (3, 6, 7, 8, 9) — it reads 0 on exactly the
#        Destriers and 0xFFFF on everything else, with no exceptions.
#   +167 has one false positive at turn 9. Destrier instance 4366 reads 1 while
#        also being a mount, so the file briefly claims a horse rides a horse.
#        Its paired Knight (4357) carries the flag as well, so the pair holds it
#        twice rather than the flag having moved.
#
# Riders and mounts sit in ADJACENT records — Knight or Paladin immediately
# followed by its Destrier — which is the reliable structure and the reason the
# counts balance at 10 and 10 despite that stray bit. Anything that must be
# exact should pair by adjacency and treat +167 as corroboration, not as truth.
OFF_IS_MOUNT  = 51     # u16: 0 = this unit is a mount, 0xFFFF = it is not

# RECORD OWNERSHIP. The 173-byte stride runs from one type id to the next, but a
# unit's OWN fields span type-28 .. type+144. Everything from +145 to +172 is the
# FOLLOWING record's -28 .. -1.
#
#   current +145 = next -28   affliction bitmask
#   current +158 = next -15   "has fought" bit, 0x20
#   current +162 = next -11   pretender bit, 0x04
#   current +167 = next -6    mount marker
#   current +169 = next -4    u32 warband/commander token
#
# This was established the hard way. "+167 == 1 means this unit rides a mount"
# was committed and tested, balanced 7 riders against 7 mounts, and was WRONG:
# it is the next record's mount marker, and it lands on riders only because a
# mount's record immediately follows its rider's. Reattributed it agrees 38 of 38
# at turn 9 and 58 of 58 at turn 18, with no exceptions — where the old reading
# was imbalanced at both turns and had been patched by excluding mounts from the
# rider set rather than by asking why.
#
# The same boundary explains +158, +162 and +169, all of which were separately
# catalogued as unknown per-unit fields. The runtime index at -32 and
# afflictions at -28 are part of that same prefix; "fields sit one record away
# from their owner" was written down as a standing prior and then walked into
# several more times.
OFF_OWN_FIRST = -32    # a unit's own fields start here
OFF_OWN_LAST  = 144    # ... and end here; beyond is the next record

OFF_HAS_FOUGHT = -15   # bit 0x20: killed something or was wounded
OFF_PRETENDER  = -11   # bit 0x04
OFF_MOUNT_MARK = -6    # u16 == 1 on a mount
OFF_WARBAND    = -4    # u32 token, 0xFFFFFFFF when unattached

OFF_INSTANCE_ID = 141
# RETRACTED: +142 was read as size. It matched for Friar, Pikeneer and
# Crossbowman — all size 3 — which is most of a Marignon army, so it looked
# right. It is not: Knight of the Chalice, Paladin, Assassin and Witch Hunter
# are all size 3 too and read 10, 24, 27 and 27. The byte varies among units of
# identical size, so it was never size.
#
# It is the high byte of the instance id, which is a u16 at +141: 38/38 unique
# across a Marignon roster and monotonically ascending in file order (785, 787,
# 790 ... 6991). Size is not stored in the unit record at all, and does not need
# to be — it comes from the reference data, like every other stat.


@dataclass
class TrnUnit:
    """One soldier from a .trn file."""
    instance_id: int          # u16 at +141; unique within the game, ascending
    runtime_index: int        # u32 at -32; commander/order linkage handle
    type_id: int              # -> reference.sqlite3 units.id
    nation_id: int
    hp: int
    age: int
    experience: int           # +8
    kills: int                # +38
    afflictions: int          # bitmask; join reference.sqlite3 afflictions
    squad_id: int             # +34; 0xFFFF = attached to no commander
    warband: int              # -4; u32 token shared by a commander's followers
    is_mount: bool            # +51; this unit is somebody's mount
    has_fought: bool          # -15 bit 0x20; killed something or was wounded
    is_pretender: bool        # -11 bit 0x04
    offset: int               # file offset, for debugging

    def __str__(self) -> str:
        return (f"unit#{self.instance_id} type={self.type_id} "
                f"nation={self.nation_id} hp={self.hp}")


def _plausible(data: bytes, off: int) -> bool:
    """Does a 173-byte window at `off` look like a troop record?

    Guards against the many two-byte coincidences in a 135 KB file: a real
    record has a sane type id, a nation id in range, and a non-zero instance
    id. Requiring all three together is what makes the scan reliable — any one
    alone matches noise. (An earlier attempt keyed on a lone value and matched
    the province index instead.)
    """
    if off + RECORD_SIZE > len(data):
        return False
    type_id = struct.unpack_from("<H", data, off + OFF_TYPE_ID)[0]
    nation = struct.unpack_from("<H", data, off + OFF_NATION)[0]
    instance = struct.unpack_from("<H", data, off + OFF_INSTANCE_ID)[0]
    hp = struct.unpack_from("<H", data, off + OFF_HP)[0]
    age = struct.unpack_from("<H", data, off + OFF_AGE)[0]
    # A live unit has hit points and an age. Both phantoms this rejects — an
    # "Ice Druid" and an "Earthbound", the latter a Late Age Caelum unit in a
    # Middle Age game with no Caelum in it — read hp 0 and age 0, and their
    # neighbouring records are noise rather than part of a run. Requiring only
    # a plausible type and nation let them through, and they carried garbage
    # affliction masks into anything that iterated units.
    # Nation 0 is INDEPENDENTS, and excluding it hid every independent army in
    # the game. Copper Canyons held 37 of them - 3 commanders, 18 heavy infantry,
    # 12 light infantry, 4 militia - and a filter of 1 <= nation <= 500 rejected
    # all 37, which is why they appeared to be "stored nowhere" through searches
    # of the .trn, ftherlnd, the .2h and the .map file.
    #
    # They live in ftherlnd and NOT in our .trn, which is fog of war working
    # correctly: we cannot see them, so our file does not carry them.
    # NOTE: home (+6) is deliberately NOT tested here. It is zero on every
    # FOREIGN unit — we do not know where an enemy was recruited, so the fog
    # blanks it — and requiring it cut a turn-23 scan from 366 records to 132,
    # taking with it the 76 units of nation 76 that the visibility boundary
    # exists to hide. The guard belongs on our OWN units, and lives in
    # PlayerView.own_units.
    return (1 <= type_id <= 4500 and 0 <= nation <= 500 and instance > 0
            and hp > 0 and age > 0)


def parse_unit(data: bytes, off: int) -> TrnUnit:
    return TrnUnit(
        instance_id=struct.unpack_from("<H", data, off + OFF_INSTANCE_ID)[0],
        runtime_index=(struct.unpack_from("<I", data,
                                         off + OFF_RUNTIME_INDEX)[0]
                       if off + OFF_RUNTIME_INDEX >= 0 else 0xFFFFFFFF),
        type_id=struct.unpack_from("<H", data, off + OFF_TYPE_ID)[0],
        nation_id=struct.unpack_from("<H", data, off + OFF_NATION)[0],
        hp=struct.unpack_from("<H", data, off + OFF_HP)[0],
        age=struct.unpack_from("<H", data, off + OFF_AGE)[0],
        experience=data[off + OFF_EXPERIENCE],
        kills=struct.unpack_from('<H', data, off + OFF_KILLS)[0],
        is_mount=struct.unpack_from('<H', data, off + OFF_IS_MOUNT)[0] == 0,
        has_fought=bool(data[off + OFF_HAS_FOUGHT] & 0x20),
        is_pretender=bool(data[off + OFF_PRETENDER] & 0x04),
        afflictions=(struct.unpack_from('<I', data, off + OFF_AFFLICTIONS)[0]
                     if off + OFF_AFFLICTIONS >= 0 else 0),
        squad_id=struct.unpack_from("<H", data, off + OFF_SQUAD)[0],
        warband=(struct.unpack_from("<I", data, off + OFF_WARBAND_OWN)[0]
                 if off + OFF_WARBAND_OWN >= 0 else 0xFFFFFFFF),
        offset=off,
    )


def find_units(data: bytes, nation_id: int | None = None) -> list[TrnUnit]:
    """All troop records in a .trn.

    Records sit contiguously in runs, so the scan anchors on one plausible
    record and then walks the 173-byte stride in both directions. That is far
    more reliable than testing every offset independently, because a genuine
    neighbour is strong evidence and an isolated match is not.
    """
    seen: dict[int, TrnUnit] = {}
    off = 0
    while off < len(data) - RECORD_SIZE:
        if not _plausible(data, off):
            off += 1
            continue
        # Walk the run backwards to its start, then forwards to its end.
        start = off
        while start - RECORD_SIZE >= 0 and _plausible(data, start - RECORD_SIZE):
            start -= RECORD_SIZE
        cur = start
        run = 0
        while _plausible(data, cur):
            unit = parse_unit(data, cur)
            if nation_id is None or unit.nation_id == nation_id:
                seen.setdefault(unit.offset, unit)
            cur += RECORD_SIZE
            run += 1
        off = max(cur, off + 1)
    return sorted(seen.values(), key=lambda u: u.offset)


def parse(path: str | Path, nation_id: int | None = None) -> list[TrnUnit]:
    return find_units(Path(path).read_bytes(), nation_id=nation_id)
