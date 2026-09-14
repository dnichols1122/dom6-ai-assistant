"""Diplomatic relation matrix embedded in a player ``.trn``.

The turn writer emits marker ``0x1e1f``, followed by sparse
``(row nation:i16, column nation:i16, relation:i8)`` entries and an i16
``-1`` terminator.  Omitted entries are zero.  Although the serialized matrix
contains relations between other players, callers at play time must expose
only the current player's row (and the reciprocal cell used by the F4 panel).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

RELATION_MARKER = 0x1E1F
NEXT_SECTION_MARKER = 0x1E47
MAX_NATION_ID = 499
SAME_GOD = -99
CONTROLLER_NAMES = {-1: "defeated", 1: "human", 2: "AI"}
H2_TRAILER_MARKER = 0x3102
MAX_OUTGOING_DIPLOMACY = 32
MAX_MISSIVE_BYTES = 0xDAD
ACTION_NAMES = {3: "declare_war", 4: "propose_nap", 5: "accept_nap", 6: "decline_nap"}
NATION_STATE_END_MARKER = 0x2286
MAX_DIPLOMACY_ARRAY_ENTRIES = 500


class DiplomacyDecodeError(ValueError):
    """Raised when the relation section cannot be identified uniquely."""


@dataclass(frozen=True)
class RelationMatrix:
    """A structurally verified sparse matrix; zero cells are omitted."""

    offset: int
    end_offset: int
    entries: tuple[tuple[int, int, int], ...]

    def code(self, row_nation_id: int, column_nation_id: int) -> int:
        for row, column, value in self.entries:
            if row == row_nation_id and column == column_nation_id:
                return value
        return 0

    @property
    def participant_nation_ids(self) -> tuple[int, ...]:
        """Nations represented by the matrix's same-god diagonal."""
        return tuple(
            sorted(
                row
                for row, column, value in self.entries
                if row == column and value == SAME_GOD and row >= 5
            )
        )


@dataclass(frozen=True)
class OutgoingDiplomacyRecord:
    """One diplomacy missive serialized in the player's ``.2h``."""

    slot: int
    target_nation_id: int
    type_id: int
    extra: int
    text: str

    @property
    def action(self) -> str:
        return ACTION_NAMES.get(self.type_id, "unknown")


def _matrix_at(data: bytes, marker_offset: int) -> RelationMatrix | None:
    pos = marker_offset + 4
    entries: list[tuple[int, int, int]] = []
    pairs: set[tuple[int, int]] = set()
    while pos + 2 <= len(data):
        row = struct.unpack_from("<h", data, pos)[0]
        if row == -1:
            # The immediately following 0x1e47 matrix is an independent
            # section and gives us a strong boundary check. This also permits
            # turn 1, whose diplomacy matrix is legitimately empty.
            if (
                pos + 6 > len(data)
                or struct.unpack_from("<I", data, pos + 2)[0] != NEXT_SECTION_MARKER
            ):
                return None
            return RelationMatrix(marker_offset, pos + 2, tuple(entries))
        if pos + 5 > len(data):
            return None
        column = struct.unpack_from("<h", data, pos + 2)[0]
        value = struct.unpack_from("<b", data, pos + 4)[0]
        pair = (row, column)
        if (
            not 0 <= row <= MAX_NATION_ID
            or not 0 <= column <= MAX_NATION_ID
            or pair in pairs
        ):
            return None
        pairs.add(pair)
        entries.append((row, column, value))
        pos += 5
    return None


def find_relation_matrix(data: bytes) -> RelationMatrix:
    """Find the unique ``0x1e1f`` diplomatic-relation section."""
    marker = struct.pack("<I", RELATION_MARKER)
    candidates: list[RelationMatrix] = []
    start = 0
    while True:
        offset = data.find(marker, start)
        if offset < 0:
            break
        found = _matrix_at(data, offset)
        if found is not None:
            candidates.append(found)
        start = offset + 1
    if len(candidates) != 1:
        offsets = [candidate.offset for candidate in candidates]
        raise DiplomacyDecodeError(
            f"expected one diplomatic relation matrix, found {len(candidates)} at {offsets}"
        )
    return candidates[0]


def nap_state(code: int) -> tuple[str | None, int | None]:
    """Return ``(phase, notice)`` for a directional F4 relation byte."""
    if code < -10 and code != SAME_GOD:
        return "active", abs(code) - 10
    if -10 <= code < 0:
        return "ending", abs(code)
    return None, None


def find_controller_states(
    data: bytes, nation_ids: tuple[int, ...], *, before_offset: int | None = None
) -> dict[int, str]:
    """Read the public F4 controller state for the supplied participants.

    A nation-state record starts with treasury and pretender fields, then
    repeats its nation id at ``+6`` and ``+14``; its signed controller state is
    at ``+16``.  Joining only ids already proven by the relation matrix avoids
    treating unrelated records as nations.  Treasury and the rest of these
    records intentionally never leave this decoder.
    """
    out: dict[int, str] = {}
    limit = find_relation_matrix(data).offset if before_offset is None else before_offset
    for nation_id in nation_ids:
        matches: list[int] = []
        for base in range(0, min(limit, len(data) - 18)):
            if (
                struct.unpack_from("<H", data, base + 6)[0] == nation_id
                and struct.unpack_from("<H", data, base + 14)[0] == nation_id
                and struct.unpack_from("<h", data, base + 16)[0] in CONTROLLER_NAMES
            ):
                matches.append(base)
        if not matches:
            raise DiplomacyDecodeError(
                f"F4 controller record for nation {nation_id} was not found"
            )
        # Resolved battle/report state can append historical copies of the same
        # nation record later in the turn file. The live nation-state region is
        # first, matching find_waiting_targets() and the current-player research
        # block. Requiring global uniqueness made late-game Marignon fail as
        # soon as a report copy repeated its own controller record.
        code = struct.unpack_from("<h", data, matches[0] + 16)[0]
        out[nation_id] = CONTROLLER_NAMES[code]
    return out


def _compact_int_at(data: bytes, offset: int) -> tuple[int, int]:
    """Decode the client's compact signed integer used by sparse arrays."""
    if offset >= len(data):
        raise DiplomacyDecodeError("truncated compact integer")
    first = struct.unpack_from("<b", data, offset)[0]
    if -126 <= first <= 127:
        return first, offset + 1
    marker = data[offset]
    if marker == 0x81 and offset + 3 <= len(data):
        return struct.unpack_from("<h", data, offset + 1)[0], offset + 3
    if marker == 0x80 and offset + 5 <= len(data):
        return struct.unpack_from("<i", data, offset + 1)[0], offset + 5
    raise DiplomacyDecodeError(f"invalid compact integer marker {marker:#x}")


def _sparse_u8_array_at(data: bytes, offset: int) -> tuple[bytes, int]:
    """Decode one versioned, trailing-zero-elided 500-byte nation array."""
    if offset >= len(data) or not 1 <= data[offset] <= 8:
        raise DiplomacyDecodeError("sparse nation array has no version tag")
    length, payload = _compact_int_at(data, offset + 1)
    if not 0 <= length <= MAX_DIPLOMACY_ARRAY_ENTRIES:
        raise DiplomacyDecodeError(f"invalid sparse nation array length {length}")
    end = payload + length
    if end > len(data):
        raise DiplomacyDecodeError("truncated sparse nation array")
    return data[payload:end], end


def find_waiting_targets(data: bytes, nation_id: int) -> tuple[int, ...]:
    """Nations for whose NAP reply this player's F4 screen says ``waiting``.

    The 6.36 panel reads runtime nation ``+0x1120[target] == 1`` before it
    considers the ordinary relation byte. The turn serializer writes that
    500-byte array as the middle of three versioned, trailing-zero-elided byte
    arrays immediately before the nation-state record's ``0x2286`` terminator.

    Only ``nation_id``'s own record is decoded. The `.trn` also serializes
    other players' nation records, but their waiting arrays are not this
    player's information and must never be exposed.
    """
    matrix = find_relation_matrix(data)
    participants = set(matrix.participant_nation_ids)
    if nation_id not in participants:
        # Turn-one files can carry the structurally valid but still-empty
        # relation section before participant diagonals are populated. There
        # cannot be an outstanding reply in that bootstrap state.
        if not participants:
            return ()
        raise DiplomacyDecodeError(f"nation {nation_id} is not a participant")

    record_offsets: dict[int, int] = {}
    for base in range(0, min(matrix.offset, len(data) - 18)):
        candidate = struct.unpack_from("<H", data, base + 6)[0]
        if candidate not in participants or candidate in record_offsets:
            continue
        if (
            struct.unpack_from("<H", data, base + 14)[0] == candidate
            and struct.unpack_from("<h", data, base + 16)[0] in CONTROLLER_NAMES
        ):
            record_offsets[candidate] = base
    if nation_id not in record_offsets:
        raise DiplomacyDecodeError(f"nation-state record {nation_id} not found")
    start = record_offsets[nation_id]
    end = min(
        [offset for offset in record_offsets.values() if offset > start]
        + [matrix.offset]
    )

    candidates: list[bytes] = []
    marker = struct.pack("<H", NATION_STATE_END_MARKER)
    for offset in range(start, max(start, end - 2)):
        try:
            _first, after_first = _sparse_u8_array_at(data, offset)
            waiting, after_waiting = _sparse_u8_array_at(data, after_first)
            _third, after_third = _sparse_u8_array_at(data, after_waiting)
        except DiplomacyDecodeError:
            continue
        if data[after_third : after_third + 2] == marker:
            candidates.append(waiting)
    if len(candidates) != 1:
        raise DiplomacyDecodeError(
            f"expected one waiting-array triple for nation {nation_id}, "
            f"found {len(candidates)}"
        )
    waiting = candidates[0]
    return tuple(
        target
        for target, value in enumerate(waiting)
        if target in participants and value == 1
    )


def _decode_missive(data: bytes, offset: int, limit: int) -> tuple[str, int] | None:
    state = 0x78
    plain = bytearray()
    pos = offset
    while pos < min(limit, offset + MAX_MISSIVE_BYTES):
        encrypted = data[pos]
        pos += 1
        value = encrypted ^ state
        if value == 0:
            try:
                return plain.decode("utf-8"), pos
            except UnicodeDecodeError:
                return None
        plain.append(value)
        state = (state + value) & 0xFF
    return None


def _encode_missive(text: str) -> bytes:
    plain = text.encode("utf-8")
    if not plain or len(plain) + 1 > MAX_MISSIVE_BYTES:
        raise ValueError(
            f"diplomatic missive must be 1-{MAX_MISSIVE_BYTES - 1} UTF-8 bytes"
        )
    state = 0x78
    encoded = bytearray()
    for value in plain:
        encoded.append(value ^ state)
        state = (state + value) & 0xFF
    encoded.append(state)  # plaintext NUL XOR the current state
    return bytes(encoded)


def _outgoing_records_at(
    data: bytes, start: int, terminator: int
) -> list[OutgoingDiplomacyRecord] | None:
    rows: list[OutgoingDiplomacyRecord] = []
    pos = start
    previous_slot = -1
    while pos < terminator:
        if pos + 11 > terminator:
            return None
        slot = struct.unpack_from("<i", data, pos)[0]
        target = struct.unpack_from("<H", data, pos + 4)[0]
        type_id = data[pos + 6]
        extra = struct.unpack_from("<I", data, pos + 7)[0]
        if (
            not previous_slot < slot < MAX_OUTGOING_DIPLOMACY
            or not 5 <= target <= MAX_NATION_ID
            or type_id not in ACTION_NAMES
        ):
            return None
        decoded = _decode_missive(data, pos + 11, terminator)
        if decoded is None:
            return None
        message, pos = decoded
        if not message:
            return None
        rows.append(OutgoingDiplomacyRecord(slot, target, type_id, extra, message))
        previous_slot = slot
    return rows if pos == terminator else None


def find_outgoing_diplomacy(data: bytes) -> tuple[int, int, list[OutgoingDiplomacyRecord]]:
    """Locate and decode the final outgoing-diplomacy list in a ``.2h``.

    Returns ``(list_start, terminator_offset, records)``.  The game writes a
    one-byte flag and marker ``0x3102`` immediately after the list terminator;
    the two-byte file trailer follows that marker.
    """
    if len(data) < 11 or struct.unpack_from("<I", data, len(data) - 6)[0] != H2_TRAILER_MARKER:
        raise DiplomacyDecodeError(".2h has no final 0x3102 diplomacy trailer")
    terminator = len(data) - 11
    if struct.unpack_from("<i", data, terminator)[0] != -1:
        raise DiplomacyDecodeError("outgoing diplomacy list has no final -1 terminator")

    candidates: list[tuple[int, list[OutgoingDiplomacyRecord]]] = []
    # The preceding variable-length list ends with its own -1. It is the
    # unambiguous left anchor for this list, including the empty case.
    for previous in range(0, terminator, 1):
        if data[previous : previous + 4] != b"\xff\xff\xff\xff":
            continue
        start = previous + 4
        rows = _outgoing_records_at(data, start, terminator)
        if rows is not None:
            candidates.append((start, rows))
    if len(candidates) != 1:
        raise DiplomacyDecodeError(
            "expected one outgoing diplomacy list, found "
            f"{len(candidates)} at {[start for start, _rows in candidates]}"
        )
    start, rows = candidates[0]
    return start, terminator, rows


def set_outgoing_diplomacy(
    data: bytes, records: list[OutgoingDiplomacyRecord]
) -> bytes:
    """Replace the complete ``.2h`` diplomacy list, preserving its trailer."""
    start, terminator, _old = find_outgoing_diplomacy(data)
    if len(records) > MAX_OUTGOING_DIPLOMACY:
        raise ValueError(f"at most {MAX_OUTGOING_DIPLOMACY} diplomacy records fit")
    payload = bytearray()
    for slot, record in enumerate(records):
        if not 5 <= record.target_nation_id <= MAX_NATION_ID:
            raise ValueError(f"invalid diplomacy target {record.target_nation_id}")
        if record.type_id not in ACTION_NAMES:
            raise ValueError(f"unknown diplomacy action type {record.type_id}")
        payload += struct.pack(
            "<iHBI", slot, record.target_nation_id, record.type_id, record.extra
        )
        payload += _encode_missive(record.text)
    payload += struct.pack("<i", -1)
    return data[:start] + bytes(payload) + data[terminator + 4 :]
