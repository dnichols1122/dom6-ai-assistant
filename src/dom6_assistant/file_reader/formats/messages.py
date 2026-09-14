"""Player-visible message records embedded in a ``.trn`` file.

The turn writer serializes the report records selected for the receiving
player.  Each record has a fixed metadata header followed by a chained-XOR
string; the list ends with a signed ``-1``.  Type 5 records are persistent
scouting reports used by province panels, while the other observed types are
notifications shown on the Messages screen.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

MESSAGE_FIELD_COUNT = 8
MESSAGE_HEADER_SIZE = 41
MAX_MESSAGE_INDEX = 9999
MAX_MESSAGE_TEXT_BYTES = 0xDAC
MAX_MESSAGE_TYPE = 127
SCOUT_REPORT_TYPE = 5
NAP_PROPOSAL_TYPE = 20
WAR_DECLARATION_TYPE = 21
# Announcements every player receives -- worldwide events, throne claims, arena
# results -- are addressed to this sentinel rather than to a nation, and the
# same record is replicated into every player's file.
BROADCAST_RECIPIENT = -2
# Some records address their nation as 1000 + id. The offset's meaning is not
# known; the pretender-awakening announcement is the only observed instance.
# No real nation id reaches 1000 (MAX_NATION_ID is 499), so the two encodings
# cannot collide.
RECIPIENT_FLAG_OFFSET = 1000

MESSAGE_KINDS = {
    0: "proclamation",
    1: "event",
    2: "heroic_ability",
    4: "battle",
    5: "scout_report",
    7: "research",
    10: "retreat",
    20: "incoming_nap_proposal",
    21: "incoming_war_declaration",
}

# An unsolicited approach from another nation gets its own message type; a
# reply to a proposal we sent comes back as an ordinary type-0 report instead.
DIPLOMATIC_TYPES = {
    NAP_PROPOSAL_TYPE: "propose_nap",
    WAR_DECLARATION_TYPE: "declare_war",
}

# Replies to a proposal we sent share type 0 with ordinary proclamations; the
# decoded action, not the type, names them.
RESPONSE_KINDS = {
    "accept_nap": "incoming_nap_accepted",
    "decline_nap": "incoming_nap_declined",
}


class MessageDecodeError(ValueError):
    """Raised when more than one plausible serialized message list exists."""


@dataclass(frozen=True)
class MessageRecord:
    """One recipient-filtered report record from a player turn file."""

    offset: int
    end_offset: int
    message_id: int
    fields: tuple[int, ...]
    recipient_nation_id: int
    source_nation_id: int
    type_id: int
    text: str

    @property
    def kind(self) -> str:
        response = RESPONSE_KINDS.get(self.diplomatic_action or "")
        if response is not None:
            return response
        # Type 2 is "a notable thing one of our own commanders did", which
        # covers heroic abilities, completing a global enchantment, and casting
        # a dispel. The selectors are identical -- fields[0] commander,
        # fields[1] province -- so only the label separates, by the prose.
        if self.type_id == 2:
            if "destroyed by something that protects the province" in self.text:
                return "province_spell_intercepted"
            if "A global enchantment for" in self.text:
                return "global_enchantment_cast"
            if " has cast Disenchantment." in self.text:
                return "own_global_disenchantment"
            if " has cast Arcane Analysis." in self.text:
                return "own_arcane_analysis"
            if " has cast Wish." in self.text:
                return "own_wish"
            if " has cast " in self.text:
                return "own_ritual_cast"
        # A dispel happened somewhere in the world. Neither report ever names
        # the dispeller. These are NOT ownership notices: the success notice
        # reaches every nation except the dispeller, including one that owns
        # neither the global nor the attempt.
        if self.type_id == 0:
            if (
                "major disturbance in the divine dominions of the world"
                in self.text
                and "dominion has grown stronger" in self.text
            ):
                return "wish_divine_power_worldwide"
            if (
                "major disturbance in ownership of the world's provinces"
                in self.text
                and "suddenly owns provinces" in self.text
            ):
                return "wish_provinces_worldwide"
            if (
                "tried to dispel the global enchantment" in self.text
                and "permanently weakened" in self.text
            ):
                return "global_disenchantment_failed"
            if "tried to dispel the global enchantment" in self.text:
                return "global_dispel_failed"
            if "has been dispelled" in self.text:
                return "global_was_dispelled"
        return MESSAGE_KINDS.get(self.type_id, "unknown")

    @property
    def global_enchantment_name(self) -> str | None:
        """Explicit global named by a controlled dispel/probe report."""
        patterns = (
            r"disturbance in the (.+?)\.",
            r"The (.+?) was successfully probed\.",
            r"dispelling of (.+?) was successful\.",
            r"^(.+?) has been dispelled\.",
        )
        for pattern in patterns:
            match = re.search(pattern, self.text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    @property
    def global_strength_estimate(self) -> int | None:
        """Approximate pearls reported by a successful Arcane Analysis."""
        match = re.search(
            r"strength that equals about (\d+) astral pearls", self.text,
            flags=re.IGNORECASE,
        )
        return int(match.group(1)) if match else None

    @property
    def diplomatic_action(self) -> str | None:
        """Player-visible diplomacy semantic, where a controlled record pins it."""
        if self.type_id in DIPLOMATIC_TYPES and self.source_nation_id >= 5:
            return DIPLOMATIC_TYPES[self.type_id]
        # Both replies to our proposal arrive as a type-0 record from the
        # responding nation rather than a distinct type, so the reply is
        # identified by the fixed prose the client wrote for each branch.
        if self.type_id == 0 and self.source_nation_id >= 5:
            if (
                "sent a herald to negotiate" in self.text
                and "there shall be no non-aggression pacts between" in self.text
                and "##EFF Dominion -1" in self.text
            ):
                return "decline_nap"
            if (
                "herald sent to" in self.text
                and "non-aggression pact between" in self.text
                and "has been accepted" in self.text
                and "##EFF Non-aggression pact with" in self.text
            ):
                return "accept_nap"
        return None

    @property
    def is_turn_message(self) -> bool:
        return self.type_id != SCOUT_REPORT_TYPE

    @property
    def is_worldwide(self) -> bool:
        """Announced to every player, not addressed to us in particular."""
        return self.recipient_nation_id == BROADCAST_RECIPIENT

    @property
    def province_id(self) -> int | None:
        """Known province selector for decoded record types."""
        if self.type_id == 1:
            return self.fields[0] or None
        if self.type_id in (2, 4):
            return self.fields[1] or None
        if self.type_id in (5, 10):
            return self.fields[0] or None
        return None

    @property
    def commander_id(self) -> int | None:
        """Known commander selector for heroic-ability announcements."""
        if self.type_id == 2:
            return self.fields[0] or None
        return None

    @property
    def body_and_effects(self) -> tuple[str, tuple[str, ...]]:
        """Split event presentation markup without discarding its contents."""
        body, marker, effect_text = self.text.partition("##EFF")
        effects = (
            tuple(line.strip() for line in effect_text.splitlines() if line.strip())
            if marker
            else ()
        )
        return body.strip(), effects


def _decode_text(data: bytes, offset: int) -> tuple[str, int] | None:
    """Decode one chained-XOR, NUL-terminated message string."""
    state = 0x78
    decoded = bytearray()
    end = min(len(data), offset + MAX_MESSAGE_TEXT_BYTES)
    pos = offset
    while pos < end:
        encrypted = data[pos]
        pos += 1
        value = encrypted ^ state
        if value == 0:
            try:
                text = decoded.decode("utf-8")
            except UnicodeDecodeError:
                return None
            if not text:
                return None
            visible = sum(char.isprintable() or char in "\n\r\t" for char in text)
            if visible / len(text) < 0.98:
                return None
            return text, pos
        decoded.append(value)
        state = (state + value) & 0xFF
    return None


def _record_at(
    data: bytes, offset: int, nation_id: int, previous_id: int = -1
) -> MessageRecord | None:
    if offset + MESSAGE_HEADER_SIZE > len(data):
        return None
    message_id = struct.unpack_from("<i", data, offset)[0]
    if not previous_id < message_id <= MAX_MESSAGE_INDEX:
        return None
    recipient, source = struct.unpack_from("<hh", data, offset + 36)
    type_id = data[offset + 40]
    # Requiring the current player is both a strong structural discriminator
    # and the visibility boundary: records addressed to anyone else can never
    # be returned even if a future `.trn` happens to retain one. Broadcasts are
    # the one exception, and they are not a loophole -- the host replicates the
    # identical record into every player's file precisely because everyone sees
    # it. Rejecting them lost the announcements outright *and* truncated the
    # surrounding list, because the walk below stops at the first record it
    # cannot parse.
    if (
        recipient not in (nation_id, BROADCAST_RECIPIENT, nation_id + RECIPIENT_FLAG_OFFSET)
        or not -2 <= source <= 5000
    ):
        return None
    if type_id > MAX_MESSAGE_TYPE:
        return None
    decoded = _decode_text(data, offset + MESSAGE_HEADER_SIZE)
    if decoded is None:
        return None
    text, end_offset = decoded
    return MessageRecord(
        offset=offset,
        end_offset=end_offset,
        message_id=message_id,
        fields=struct.unpack_from("<8I", data, offset + 4),
        recipient_nation_id=recipient,
        source_nation_id=source,
        type_id=type_id,
        text=text,
    )


def find_message_records(data: bytes, nation_id: int) -> list[MessageRecord]:
    """Locate the one structurally valid recipient-filtered message list.

    Every record start within the true list also looks like a shorter list, so
    contained suffixes are discarded.  Ambiguous non-contained regions fail
    closed instead of choosing whichever happens to contain more text.
    """
    candidates: list[tuple[int, int, list[MessageRecord]]] = []
    for start in range(0, len(data) - MESSAGE_HEADER_SIZE + 1):
        first = _record_at(data, start, nation_id)
        if first is None:
            continue
        rows: list[MessageRecord] = []
        pos = start
        previous_id = -1
        while True:
            if pos + 4 <= len(data) and struct.unpack_from("<i", data, pos)[0] == -1:
                candidates.append((start, pos + 4, rows))
                break
            row = _record_at(data, pos, nation_id, previous_id)
            if row is None:
                break
            rows.append(row)
            previous_id = row.message_id
            pos = row.end_offset

    maximal = [
        candidate
        for candidate in candidates
        if not any(
            other_start <= candidate[0]
            and candidate[1] <= other_end
            and (other_start, other_end) != (candidate[0], candidate[1])
            for other_start, other_end, _rows in candidates
        )
    ]
    if not maximal:
        return []
    if len(maximal) != 1:
        locations = ", ".join(str(candidate[0]) for candidate in maximal)
        raise MessageDecodeError(f"ambiguous player-message regions at offsets {locations}")
    return maximal[0][2]
