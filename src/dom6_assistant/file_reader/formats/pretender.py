"""Lossless parser/editor for saved pretender templates.

The files live in ``savedgames/newlords/<nation>_<n>.2h``. Unlike turn-order
``.2h`` files, each is a small, self-contained pretender design. The client
serializes a static chassis record as well as the selected design, so editing
an existing template is deliberately separate from constructing an arbitrary
chassis record from scratch.

This module implements the decoded format: identity, awakening, Dominion,
final paths, scales, blessings, name, checksum, byte-preserving edits of an
existing template, and construction from the client's serialized 6.36 chassis
payloads.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace
from enum import IntEnum
from pathlib import Path
from typing import Mapping, Sequence
from dom6_assistant.paths import default_newlords_dir

_XOR_KEY = 0x4F
_XOR_NULL = 0x4F

# Names of different lengths shift the design block. In every controlled file:
# ... run of ff ... | 7 zero bytes | dominion | 9 paths | ... | scales
_FF_RUN_MIN = 3
_GAP_AFTER_FF = 7
_DOMINION_TO_SCALES = 0x36
_SEARCH_LIMIT = 0x01C0

OFF_NATION_ID = 0x1A
OFF_CHASSIS_ID_1 = 0x5D
OFF_CHASSIS_ID_2 = 0x92

# Current (6.36) saved-god header through the fixed 0x1bd record marker.  The
# only design-specific field in this section is the nation u32 at +0x1a.
_CURRENT_HEADER = bytes.fromhex(
    "010204444f4d1d6202007c0200000000000000000000ffffffff0000000000000000"
    "00000000212a3823203d2b3c4f7878000000000231000000000000bd010000"
)
_STATIC_BLOCK_LENGTH = 169
_STATIC_CHASSIS_OFFSET = 28
_STATIC_NATION_OFFSET = 60
_LIVE_SUFFIX_LENGTH = 205
_LIVE_DOMINION_OFFSET = 153
_PRE_SCALE_MARKER = b"\x02\x31"

PATH_NAMES = (
    "fire", "air", "water", "earth", "astral",
    "death", "nature", "glamour", "blood",
)
SCALE_NAMES = ("order", "productivity", "heat", "growth", "luck", "magic")


class Awakening(IntEnum):
    """Client values stored in a saved pretender."""

    AWAKE = 0
    DORMANT = 1
    IMPRISONED = 2


@dataclass(frozen=True)
class _Layout:
    dominion: int
    scales: int
    tail: int
    blessing_count: int
    blessings: int
    tail_name: int
    tail_name_end: int
    tail_paths: int
    first_name: int
    first_name_end: int


@dataclass
class Pretender:
    """A decoded saved pretender design.

    ``paths`` contains only non-zero final levels. ``scales`` always contains
    all six signed scales. ``_template`` is excluded from representation and
    comparison; it permits exact edits without exposing binary details.
    """

    name: str
    nation_slug: str = ""
    nation_id: int = 0
    chassis_id: int = 0
    serialized_form_id: int = 0
    awakening: Awakening = Awakening.AWAKE
    dominion: int = 0
    paths: dict[str, int] = field(default_factory=dict)
    scales: dict[str, int] = field(default_factory=dict)
    blessing_ids: tuple[int, ...] = ()
    holy_path: int = 0
    checksum: int = 0
    checksum_valid: bool = False
    _template: bytes | None = field(default=None, repr=False, compare=False)

    def summary(self) -> str:
        paths = ", ".join(f"{v} {k}" for k, v in self.paths.items()) or "no paths"
        scales = ", ".join(f"{k} {v:+d}" for k, v in self.scales.items() if v)
        return (
            f"{self.name} ({self.nation_slug}): chassis {self.chassis_id}, "
            f"{self.awakening.name.lower()}, dominion {self.dominion}, {paths}"
            + (f", scales {scales}" if scales else "")
        )

    def edited(self, **changes: object) -> "Pretender":
        """Return a copy while retaining the original binary template."""

        return replace(self, **changes)

    def to_bytes(self) -> bytes:
        """Serialize this design."""

        if self._template is None:
            return build_template(self)
        return edit_template(self._template, self)


def _glibc_rand_table() -> tuple[int, ...]:
    """First 256 glibc random() values for seed 1, as embedded by the client."""

    state = [0] * 600
    state[0] = 1
    for i in range(1, 31):
        state[i] = (16807 * state[i - 1]) % 2147483647
    for i in range(31, 34):
        state[i] = state[i - 31]
    for i in range(34, len(state)):
        state[i] = (state[i - 31] + state[i - 3]) & 0xFFFFFFFF
    return tuple((state[i] >> 1) & 0x7FFFFFFF for i in range(344, 600))


_CHECKSUM_TABLE = _glibc_rand_table()


def calculate_checksum(payload: bytes) -> int:
    """Return the u16 checksum the client appends to a saved template."""

    state = 0
    for byte in payload:
        state = (state + _CHECKSUM_TABLE[(byte + state) & 0xFF]) & 0xFFFFFFFF
    return state & 0xFFFF


def append_checksum(payload: bytes) -> bytes:
    return payload + struct.pack("<H", calculate_checksum(payload))


def verify_checksum(data: bytes) -> bool:
    return (
        len(data) >= 2
        and calculate_checksum(data[:-2])
        == struct.unpack_from("<H", data, len(data) - 2)[0]
    )


def _decode_xor_string(data: bytes, offset: int) -> tuple[str, int]:
    try:
        end = data.index(_XOR_NULL, offset)
    except ValueError as exc:
        raise ValueError(f"Unterminated XOR string at {offset:#x}") from exc
    # The game stores one byte per character. latin-1 is lossless for unknown
    # high bytes and raw 00 correctly decodes to uppercase O.
    decoded = bytes(byte ^ _XOR_KEY for byte in data[offset:end]).decode("latin-1")
    return decoded, end


def _encode_xor_string(value: str) -> bytes:
    raw = value.encode("latin-1")
    if b"\x00" in raw:
        raise ValueError("Pretender names cannot contain NUL")
    return bytes(byte ^ _XOR_KEY for byte in raw) + bytes((_XOR_NULL,))


def _find_dominion_offset(data: bytes) -> int:
    """Offset of the Dominion byte via the final pre-stat ``ff`` run."""

    ff_end = None
    i = 0
    limit = min(len(data), _SEARCH_LIMIT)
    while i < limit:
        if data[i] == 0xFF:
            j = i
            while j < len(data) and data[j] == 0xFF:
                j += 1
            if j - i >= _FF_RUN_MIN:
                ff_end = j
            i = j
        else:
            i += 1
    if ff_end is None:
        raise ValueError("No pre-stat ff anchor; not a saved pretender template?")
    off = ff_end + _GAP_AFTER_FF
    if data[ff_end:off] != b"\x00" * _GAP_AFTER_FF:
        raise ValueError("Pretender stat anchor is not followed by seven zero bytes")
    return off


def _layout(data: bytes, *, require_first_name: bool = True) -> _Layout:
    if len(data) < 80 or data[3:6] != b"DOM":
        raise ValueError(f"Not a saved pretender template: DOM magic is {data[3:6]!r}")

    dominion = _find_dominion_offset(data)
    scales = dominion + _DOMINION_TO_SCALES
    tail = scales + len(SCALE_NAMES)
    if tail + 21 > len(data) - 2 or data[tail] != _XOR_NULL:
        raise ValueError("Saved pretender tail does not follow the scale block")

    awakening = struct.unpack_from("<i", data, tail + 1)[0]
    if awakening not in set(Awakening):
        raise ValueError(f"Unknown awakening value {awakening}")
    if data[tail + 5:tail + 13] != b"\x00" * 8:
        raise ValueError("Saved pretender reserved tail bytes are non-zero")

    blessing_count = struct.unpack_from("<i", data, tail + 13)[0]
    if not 0 <= blessing_count <= 100:
        raise ValueError(f"Implausible blessing count {blessing_count}")
    if struct.unpack_from("<i", data, tail + 17)[0] != 0:
        raise ValueError("Saved pretender blessing reserved word is non-zero")
    blessings = tail + 21
    tail_name = blessings + blessing_count * 4
    tail_name_value, tail_name_end = _decode_xor_string(data, tail_name)

    # Name terminator, two empty strings, ac 0b, marker 123, ten paths.
    marker = tail_name_end + 1
    if data[marker:marker + 8] != b"\x4f\x4f\xac\x0b\x7b\x00\x00\x00":
        raise ValueError("Unknown saved pretender post-name marker")
    tail_paths = marker + 8
    if data[tail_paths + 10:tail_paths + 14] != b"\x89\x5a\x00\x00":
        raise ValueError("Unknown saved pretender final marker")
    if tail_paths + 16 != len(data):
        raise ValueError("Unexpected bytes after saved pretender final marker")

    encoded_name = _encode_xor_string(tail_name_value)
    first_name = data.find(encoded_name, 0, dominion)
    if first_name < 0 and require_first_name:
        raise ValueError("Earlier serialized copy of pretender name is missing")
    return _Layout(
        dominion=dominion,
        scales=scales,
        tail=tail,
        blessing_count=blessing_count,
        blessings=blessings,
        tail_name=tail_name,
        tail_name_end=tail_name_end,
        tail_paths=tail_paths,
        first_name=first_name,
        first_name_end=(first_name + len(encoded_name) if first_name >= 0 else -1),
    )


def parse_bytes(
    data: bytes,
    nation_slug: str = "",
    *,
    require_valid_checksum: bool = True,
) -> Pretender:
    layout = _layout(data)
    checksum = struct.unpack_from("<H", data, len(data) - 2)[0]
    checksum_valid = verify_checksum(data)
    if require_valid_checksum and not checksum_valid:
        raise ValueError(
            f"Saved pretender checksum mismatch: stored {checksum:#06x}, "
            f"calculated {calculate_checksum(data[:-2]):#06x}"
        )

    name, _ = _decode_xor_string(data, layout.tail_name)
    main_levels = data[layout.dominion + 1:layout.dominion + 10]
    paths = {
        path_name: value
        for path_name, value in zip(PATH_NAMES, main_levels)
        if value
    }
    scales = {
        scale_name: -struct.unpack_from("b", data, layout.scales + index)[0]
        for index, scale_name in enumerate(SCALE_NAMES)
    }
    blessing_ids = tuple(
        struct.unpack_from("<i", data, layout.blessings + index * 4)[0]
        for index in range(layout.blessing_count)
    )
    tail_levels = data[layout.tail_paths:layout.tail_paths + 10]
    if tail_levels[:9] != main_levels:
        raise ValueError("The two saved final-path arrays disagree")

    chassis_1 = struct.unpack_from("<H", data, OFF_CHASSIS_ID_1)[0]
    # Usually equal to chassis_id, but shape-changing chassis prove this is a
    # serialized current/display form rather than an integrity copy.  Dragon
    # 3710, for example, stores Storm Father 3711 here.
    serialized_form_id = struct.unpack_from("<H", data, OFF_CHASSIS_ID_2)[0]

    return Pretender(
        name=name,
        nation_slug=nation_slug,
        nation_id=struct.unpack_from("<I", data, OFF_NATION_ID)[0],
        chassis_id=chassis_1,
        serialized_form_id=serialized_form_id,
        awakening=Awakening(struct.unpack_from("<i", data, layout.tail + 1)[0]),
        dominion=data[layout.dominion],
        paths=paths,
        scales=scales,
        blessing_ids=blessing_ids,
        holy_path=tail_levels[9],
        checksum=checksum,
        checksum_valid=checksum_valid,
        _template=data,
    )


def _validated_levels(values: Mapping[str, int], names: Sequence[str], kind: str) -> bytes:
    unknown = set(values) - set(names)
    if unknown:
        raise ValueError(f"Unknown {kind} names: {sorted(unknown)}")
    levels = []
    for name in names:
        value = int(values.get(name, 0))
        if not 0 <= value <= 10:
            raise ValueError(f"{kind} {name} must be between 0 and 10, got {value}")
        levels.append(value)
    return bytes(levels)


def _design_tail(
    design: Pretender,
    path_levels: bytes,
    awakening: Awakening,
    scale_bytes: bytes,
) -> bytes:
    blessing_ids = tuple(int(value) for value in design.blessing_ids)
    if any(value < 0 or value > 0x7FFFFFFF for value in blessing_ids):
        raise ValueError("Blessing ids must be non-negative signed i32 values")
    encoded_name = _encode_xor_string(design.name)

    tail = bytearray(_PRE_SCALE_MARKER)
    tail.extend(scale_bytes)
    tail.append(_XOR_NULL)
    tail.extend(struct.pack("<i", int(awakening)))
    tail.extend(b"\x00" * 8)
    tail.extend(struct.pack("<i", len(blessing_ids)))
    tail.extend(b"\x00" * 4)
    for blessing_id in blessing_ids:
        tail.extend(struct.pack("<i", blessing_id))
    tail.extend(encoded_name)
    tail.extend(b"\x4f\x4f\xac\x0b\x7b\x00\x00\x00")
    tail.extend(path_levels)
    tail.append(int(design.holy_path))
    tail.extend(b"\x89\x5a\x00\x00")
    return bytes(tail)


def _validated_design_fields(design: Pretender) -> tuple[Awakening, bytes, bytes]:
    if not design.name:
        raise ValueError("Pretender name cannot be empty")
    _encode_xor_string(design.name)  # also validates the on-disk encoding
    if not 1 <= int(design.dominion) <= 10:
        raise ValueError(f"Dominion must be between 1 and 10, got {design.dominion}")
    try:
        awakening = Awakening(design.awakening)
    except ValueError as exc:
        raise ValueError(f"Unknown awakening value {design.awakening}") from exc

    path_levels = _validated_levels(design.paths, PATH_NAMES, "magic path")
    if not 0 <= int(design.holy_path) <= 10:
        raise ValueError(f"Holy path must be between 0 and 10, got {design.holy_path}")

    unknown_scales = set(design.scales) - set(SCALE_NAMES)
    if unknown_scales:
        raise ValueError(f"Unknown scale names: {sorted(unknown_scales)}")
    scale_bytes = bytearray()
    for name in SCALE_NAMES:
        value = int(design.scales.get(name, 0))
        if not -3 <= value <= 3:
            raise ValueError(f"Scale {name} must be between -3 and 3, got {value}")
        scale_bytes.extend(struct.pack("b", -value))
    return awakening, path_levels, bytes(scale_bytes)


def build_template(design: Pretender) -> bytes:
    """Build a saved-god file from a client-serialized 6.36 chassis payload."""

    from dom6_assistant.reference.pretender_chassis_payloads import get_chassis_payload

    if not 0 <= int(design.nation_id) <= 499:
        raise ValueError(f"Nation id must be between 0 and 499, got {design.nation_id}")
    awakening, path_levels, scale_bytes = _validated_design_fields(design)
    chassis = get_chassis_payload(design.chassis_id)

    header = bytearray(_CURRENT_HEADER)
    struct.pack_into("<I", header, OFF_NATION_ID, int(design.nation_id))

    static_block = bytearray(chassis.static_block)
    if len(static_block) != _STATIC_BLOCK_LENGTH:
        raise ValueError(f"Bad serialized static block for chassis {design.chassis_id}")
    # The client's generic unit constructor starts reciprocal shape-changers
    # in their display form.  The saved-god writer restores the selected
    # chassis in the primary word while retaining that display form in the
    # later word (e.g. Dragon 3710 / Storm Father 3711).
    struct.pack_into("<H", static_block, _STATIC_CHASSIS_OFFSET, int(design.chassis_id))
    struct.pack_into("<H", static_block, _STATIC_NATION_OFFSET, int(design.nation_id))

    old_name_end = chassis.live_block.index(_XOR_NULL) + 1
    live_suffix = bytearray(chassis.live_block[old_name_end:])
    if len(live_suffix) != _LIVE_SUFFIX_LENGTH:
        raise ValueError(f"Bad serialized live block for chassis {design.chassis_id}")
    live_suffix[_LIVE_DOMINION_OFFSET] = int(design.dominion)
    live_suffix[_LIVE_DOMINION_OFFSET + 1:_LIVE_DOMINION_OFFSET + 10] = path_levels

    payload = bytearray(header)
    payload.extend(static_block)
    payload.extend(_encode_xor_string(design.name))
    payload.extend(live_suffix)
    payload.extend(_design_tail(design, path_levels, awakening, scale_bytes))
    return append_checksum(bytes(payload))


def edit_template(template: bytes, design: Pretender) -> bytes:
    """Apply a design to its original nation/chassis template.

    This is intentionally strict. Nation or chassis changes require the
    client's static chassis serializer and are not represented by two id-word
    edits.
    """

    original = parse_bytes(template)
    if design.nation_id != original.nation_id:
        raise ValueError("Cannot change nation without rebuilding the static nation block")
    if design.chassis_id != original.chassis_id:
        raise ValueError("Cannot change chassis without rebuilding the static chassis block")
    awakening, path_levels, scale_bytes = _validated_design_fields(design)
    blessing_ids = tuple(int(value) for value in design.blessing_ids)
    encoded_name = _encode_xor_string(design.name)

    # Replace the earlier variable-length name first. That moves the anchor,
    # so all subsequent offsets are deliberately found again.
    old_layout = _layout(template)
    payload = bytearray(
        template[:old_layout.first_name]
        + encoded_name
        + template[old_layout.first_name_end:-2]
    )
    layout = _layout(bytes(payload) + b"\x00\x00", require_first_name=False)

    payload[layout.dominion] = int(design.dominion)
    payload[layout.dominion + 1:layout.dominion + 10] = path_levels
    payload[layout.scales:layout.scales + 6] = scale_bytes

    tail = bytearray((_XOR_NULL,))
    tail.extend(struct.pack("<i", int(awakening)))
    tail.extend(b"\x00" * 8)
    tail.extend(struct.pack("<i", len(blessing_ids)))
    tail.extend(b"\x00" * 4)
    for blessing_id in blessing_ids:
        tail.extend(struct.pack("<i", blessing_id))
    tail.extend(encoded_name)
    tail.extend(b"\x4f\x4f\xac\x0b\x7b\x00\x00\x00")
    tail.extend(path_levels)
    tail.append(int(design.holy_path))
    tail.extend(b"\x89\x5a\x00\x00")

    payload = payload[:layout.tail] + tail
    return append_checksum(bytes(payload))


def parse(path: str | Path) -> Pretender:
    p = Path(path)
    slug = p.stem.rsplit("_", 1)[0]
    return parse_bytes(p.read_bytes(), nation_slug=slug)


def parse_all(newlords_dir: str | Path | None = None) -> list[Pretender]:
    """Parse every saved pretender, defaulting to the game's newlords dir."""

    directory = (
        Path(newlords_dir)
        if newlords_dir is not None
        else default_newlords_dir()
    )
    return [parse(path) for path in sorted(directory.glob("*.2h"))]
