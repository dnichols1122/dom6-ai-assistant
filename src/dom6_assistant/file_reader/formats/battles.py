"""Player-visible battle records embedded in a `.trn` file.

A resolved battle stores the two starting armies as one contiguous run of the
same 173-byte unit records used by the live roster.  A compact summary after
that roster records attacker/defender starting counts, survivors and kills.
The two structures are joined here so callers never need to infer battle
results from the current live roster.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from dom6_assistant.file_reader.formats import units as U

OFF_CURRENT_PROVINCE = 4
MAX_PROVINCE_ID = 5000

# Header immediately before the compact Battle Summary rows.  It occurs once
# per resolved battle in the controlled turns, including twice on turn 4.
SUMMARY_MAGIC = bytes.fromhex("1e e3 01 00 42 43 44 17 19 64")
SUMMARY_TERMINATOR = 0xFF
# The game reader accepts compact-summary group ids 0 through 5. Groups 0-3
# have understood force/casualty roles; 4-5 remain explicit exceptional data.
MAX_SUMMARY_GROUP = 5


@dataclass(frozen=True)
class BattleRoster:
    """One battle's starting records, in their original file order."""

    offset: int
    province_id: int | None
    units: tuple[U.TrnUnit, ...]

    @property
    def nation_ids(self) -> tuple[int, ...]:
        return tuple(sorted({unit.nation_id for unit in self.units}))


@dataclass(frozen=True)
class RawBattleSummaryRow:
    """One undecided row from the compact summary.

    Most rows encode ``count, unknown, kills`` as three little-endian u16s.
    Commander rows insert a leading zero and encode kills in one byte.  The
    starting roster resolves which layout applies without guessing from the
    unit type.
    """

    group: int
    type_id: int
    payload: bytes


@dataclass(frozen=True)
class BattleSummaryRow:
    group: int
    type_id: int
    count: int
    unknown: int
    kills: int


@dataclass(frozen=True)
class BattleSummary:
    offset: int
    end_offset: int
    rows: tuple[RawBattleSummaryRow, ...]


@dataclass(frozen=True)
class ResolvedBattleSummary:
    """Summary whose group 0/2 forces have been joined to roster nations."""

    offset: int
    attacker_nation_id: int
    defender_nation_id: int
    rows: tuple[BattleSummaryRow, ...]


def _contiguous_runs(records: list[U.TrnUnit]) -> list[list[U.TrnUnit]]:
    runs: list[list[U.TrnUnit]] = []
    for unit in records:
        if (not runs
                or unit.offset - runs[-1][-1].offset != U.RECORD_SIZE):
            runs.append([])
        runs[-1].append(unit)
    return runs


def find_battle_rosters(data: bytes, player_nation_id: int,
                        *, live_offsets: set[int] | None = None
                        ) -> list[BattleRoster]:
    """Locate battle-report rosters involving the player.

    The controlled province battles on turns 3, 22 and 38 share four structural
    properties: one uninterrupted 173-byte run, exactly two nations including
    the player, one common battle province, and at least one displayed
    (non-mount) combatant on each side.  The two special turn-4 encounters use
    province zero, so zero is retained as an unknown/non-map target.

    `live_offsets` excludes the current roster selected independently by the
    visibility layer.  That makes a coincidentally single-province live army
    insufficient to manufacture a battle report.
    """
    live = live_offsets or set()
    reports: list[BattleRoster] = []
    for run in _contiguous_runs(U.find_units(data)):
        if len(run) < 2 or any(unit.offset in live for unit in run):
            continue
        nations = {unit.nation_id for unit in run}
        if len(nations) != 2 or player_nation_id not in nations:
            continue
        provinces = {
            struct.unpack_from("<H", data, unit.offset + OFF_CURRENT_PROVINCE)[0]
            for unit in run
        }
        if len(provinces) != 1:
            continue
        province = next(iter(provinces))
        if province > MAX_PROVINCE_ID:
            continue
        if any(not any(unit.nation_id == nation and not unit.is_mount
                       for unit in run) for nation in nations):
            continue
        reports.append(BattleRoster(
            offset=run[0].offset,
            province_id=province or None,
            units=tuple(run),
        ))
    return reports


def find_battle_summaries(data: bytes) -> list[BattleSummary]:
    """Locate structurally valid compact Battle Summary tables.

    Groups are phases: 0 attacker start, 1 attacker after combat, 2 defender
    start, 3 defender after combat. Groups 4-5 are exceptional/add-on groups
    whose gameplay meaning is deliberately left unresolved.
    """
    out: list[BattleSummary] = []
    start = 0
    while True:
        marker = data.find(SUMMARY_MAGIC, start)
        if marker < 0:
            break
        pos = marker + len(SUMMARY_MAGIC)
        rows: list[RawBattleSummaryRow] = []
        valid = True
        previous_group = -1
        while pos < len(data) and data[pos] != SUMMARY_TERMINATOR:
            group = data[pos]
            pos += 1
            if group > MAX_SUMMARY_GROUP or group < previous_group:
                valid = False
                break
            previous_group = group
            if pos >= len(data):
                valid = False
                break
            if data[pos] == 0x81:
                if pos + 9 > len(data):
                    valid = False
                    break
                type_id = struct.unpack_from("<H", data, pos + 1)[0]
                payload = data[pos + 3:pos + 9]
                pos += 9
            else:
                if pos + 7 > len(data):
                    valid = False
                    break
                type_id = data[pos]
                payload = data[pos + 1:pos + 7]
                pos += 7
            if not (1 <= type_id <= 5000):
                valid = False
                break
            rows.append(RawBattleSummaryRow(group, type_id, payload))
            if len(rows) > 500:
                valid = False
                break
        groups = {row.group for row in rows}
        if (valid and pos < len(data) and data[pos] == SUMMARY_TERMINATOR
                and {0, 2}.issubset(groups)):
            out.append(BattleSummary(marker, pos + 1, tuple(rows)))
        start = marker + 1
    return out


def pair_rosters_and_summaries(
        rosters: list[BattleRoster], summaries: list[BattleSummary]
        ) -> list[tuple[BattleRoster, BattleSummary]]:
    """Pair each roster with the first following summary before the next.

    This ordering is what makes two battle messages in one turn independently
    attributable; no final/live kill counter is involved.
    """
    ordered_rosters = sorted(rosters, key=lambda row: row.offset)
    ordered_summaries = sorted(summaries, key=lambda row: row.offset)
    pairs: list[tuple[BattleRoster, BattleSummary]] = []
    used: set[int] = set()
    for i, roster in enumerate(ordered_rosters):
        roster_end = roster.units[-1].offset + U.RECORD_SIZE
        next_roster = (ordered_rosters[i + 1].offset
                       if i + 1 < len(ordered_rosters) else 1 << 62)
        candidates = [summary for summary in ordered_summaries
                      if summary.offset >= roster_end
                      and summary.offset < next_roster
                      and summary.offset not in used]
        if candidates:
            chosen = candidates[0]
            used.add(chosen.offset)
            pairs.append((roster, chosen))
    return pairs


def _row_layout(row: RawBattleSummaryRow, expected_count: int
                ) -> tuple[BattleSummaryRow, str] | None:
    normal = struct.unpack("<HHH", row.payload)
    candidates = [(normal, "normal")]
    if row.payload[0] == 0:
        padded = struct.unpack("<HHB", row.payload[1:])
        candidates.append((padded, "padded"))
    # A province-defence commander can use the same unit type as ordinary
    # troops. The client then merges both classes into one summary row:
    # byte 0 is the troop count, byte 1 the commander count, bytes 2-3 the
    # unresolved value, and bytes 4/5 their respective credited kills. The
    # Berytos sailing battle has 36 Jaguar Tribe Warrior troops plus one
    # Warrior commander and serializes `24 01 00 00 00 00`.
    if row.payload[0] and row.payload[1]:
        mixed = (
            row.payload[0] + row.payload[1],
            struct.unpack_from("<H", row.payload, 2)[0],
            row.payload[4] + row.payload[5],
        )
        candidates.append((mixed, "mixed"))
    matches = [(values, layout) for values, layout in candidates
               if values[0] == expected_count]
    if not matches:
        return None
    # If both layouts happen to give the same count, accept only if their
    # semantic values agree.  Ambiguity must not manufacture casualties.
    distinct = {values for values, _layout in matches}
    if len(distinct) != 1:
        return None
    values, layout = matches[0]
    return (BattleSummaryRow(row.group, row.type_id, *values), layout)


def resolve_battle_summary(summary: BattleSummary, roster: BattleRoster
                           ) -> ResolvedBattleSummary | None:
    """Join summary sides and row layouts to the exact starting roster."""
    roster_counts = {
        nation: {
            type_id: count for type_id, count in
            _type_counts(roster, nation).items()
        }
        for nation in roster.nation_ids
    }
    if len(roster_counts) != 2:
        return None
    raw_by_group = {
        group: [row for row in summary.rows if row.group == group]
        for group in range(MAX_SUMMARY_GROUP + 1)
    }

    resolutions: list[ResolvedBattleSummary] = []
    nations = roster.nation_ids
    for attacker, defender in ((nations[0], nations[1]),
                               (nations[1], nations[0])):
        decoded: list[BattleSummaryRow] = []
        layouts: dict[tuple[int, int], str] = {}
        valid = True
        for group, nation in ((0, attacker), (2, defender)):
            expected = roster_counts[nation]
            type_ids = [row.type_id for row in raw_by_group[group]]
            if (set(type_ids) != set(expected)
                    or len(type_ids) != len(set(type_ids))):
                valid = False
                break
            for row in raw_by_group[group]:
                found = _row_layout(row, expected[row.type_id])
                if found is None:
                    valid = False
                    break
                parsed, layout = found
                decoded.append(parsed)
                layouts[(group, row.type_id)] = layout
            if not valid:
                break
        if not valid:
            continue

        for group, start_group in ((1, 0), (3, 2)):
            starts = {row.type_id: row.count for row in decoded
                      if row.group == start_group}
            for row in raw_by_group[group]:
                if row.type_id not in starts:
                    valid = False
                    break
                layout = layouts[(start_group, row.type_id)]
                if layout == "normal":
                    values = struct.unpack("<HHH", row.payload)
                elif layout == "padded":
                    values = struct.unpack("<HHB", row.payload[1:])
                else:
                    values = (
                        row.payload[0] + row.payload[1],
                        struct.unpack_from("<H", row.payload, 2)[0],
                        row.payload[4] + row.payload[5],
                    )
                if values[0] > starts[row.type_id]:
                    valid = False
                    break
                decoded.append(BattleSummaryRow(
                    row.group, row.type_id, *values))
            if not valid:
                break
        if not valid:
            continue

        # Compact-summary row class 5 is accepted by the client reader but
        # does not occur in the corpus, so its layout and relationship to row
        # class 4 are unproved. This is an internal battle-report phase tag,
        # not a player squad, formation, main order, or targeting group.
        # Refuse that future report rather than silently dropping row class 5
        # and misreading class 4 as having no survivors.
        if raw_by_group[5]:
            continue

        # Group 4 has no nation/start roster. Its observed rows use the
        # ordinary layout; visibility may attribute it only when independent
        # kill conservation proves the relationship.
        for row in raw_by_group[4]:
            values = struct.unpack("<HHH", row.payload)
            decoded.append(BattleSummaryRow(row.group, row.type_id, *values))
        resolutions.append(ResolvedBattleSummary(
            summary.offset, attacker, defender,
            tuple(sorted(decoded, key=lambda row: (row.group, row.type_id)))))

    return resolutions[0] if len(resolutions) == 1 else None


def _type_counts(roster: BattleRoster, nation_id: int) -> dict[int, int]:
    counts: dict[int, int] = {}
    for unit in roster.units:
        if unit.nation_id == nation_id and not unit.is_mount:
            counts[unit.type_id] = counts.get(unit.type_id, 0) + 1
    return counts
