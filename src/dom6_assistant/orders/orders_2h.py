"""The commander order table inside a `.2h` file.

Layout, established by controlled diffs against a live game — one field changed
per save, everything else held constant:

    slot stride   219 bytes
    slot + 0      u16   order parameter (province, item, path, or building)
    slot + 48     u8    order code   (see ORDER_CODES)
    slot + 34     u16   NOT an order field — see below

The first slot observed sits at ``FIRST_SLOT``. Slots run in ascending
commander id, one per commander the player controls.

Evidence, in the order it was obtained:

1. Estorgant sneaking to 86 vs 98 — three bytes changed in 11,804: the
   destination at +11484, and the two-byte trailer. That confirmed the
   destination field and showed nothing binds a commander to a slot by id, so
   the association is positional.
2. The same file edited by hand from 98 to 91, trailer left stale, loaded fine
   and showed Sneak to Kratas. That is what makes writing possible at all.
3. Dapamort ordered to Move to 98 — a second destination appeared at +11265,
   219 bytes earlier, and +48 within each slot read 1 for his Move against 2
   for Estorgant's Sneak. That gave both the stride and the order code.

`slot + 34` is **not** order-related, despite appearing to be. It went 0 -> 2137
in the save where Dapamort was first given a Move, which looked like a movement
field. Ordering Estorgant to Move left it untouched, and all three slots now
hold distinct values that no order changes. Whatever it is, it is initialised
once and the writer must leave it alone.

**Unconfirmed:** that slots are allocated only to the player's own commanders,
in ascending id order. It fits the two observed slots exactly (Dapamort 116
before Estorgant 182, adjacent slots) and it explains why the five rival
pretenders in the name table get no slots. But two points define a lot of
lines, so `slot_for_commander` refuses to extrapolate past what the file
actually shows unless the caller opts in.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dom6_assistant.reference import wish as WISH

# A commander's block: u32 id, then the XOR-encoded name, then a fixed tail.
# The block is VARIABLE length -- 210 + len(name) -- so the order fields are
# measured from the byte after the name terminator, not from the block start.
#
#   block +0            u32   commander id
#   block +4            xstr  name, 0x4F-terminated
#   name_end + 116      u16   order-specific parameter
#   name_end + 164      u8    order code
#
# This replaces a hardcoded table offset that was correct only for the turn-1
# file. The table moves as the file grows, and writing at a stale offset
# patches arbitrary bytes with no error. Anchoring on the commander id means
# orders are addressed by *who* rather than by position, so nothing depends on
# slot ordering either.
OFF_PARAMETER   = 116      # from name_end
# Compatibility alias for older callers and evidence notes. This field is only
# a destination for movement orders; treating every value as a province is the
# exact bug the typed order specifications below prevent.
OFF_DESTINATION = OFF_PARAMETER
OFF_ORDER_CODE  = 164      # from name_end

# Ritual orders occupy the otherwise-unused gap immediately after the shared
# parameter and before the five-round combat script.  Controlled turn-26/30
# saves established every value below:
#
#   Distill Gold once       spell 759, cost 10, no target, code 9
#   Distill Gold monthly    same fields, code 60
#   Augury -> province 86   spell 1283, cost 2, target 86, code 9
#
# Transport-ritual saves establish the first trailing signed word as the
# recipient commander's runtime handle. The second remains the -1 sentinel.
OFF_RITUAL_COST = 120      # u32, gems reserved now
OFF_RITUAL_PROVINCE = 124  # province/global slot, or unit runtime handle
OFF_RITUAL_SENTINELS = 128 # commander/unit runtime handle, then i32 -1
RITUAL_ORDER_CODES = {"cast_ritual": 9, "monthly_ritual": 60}
# Gift of Reason and Divine Name select an ordinary troop rather than a
# province, commander, or global enchantment. The client writes the troop's
# runtime handle into *both* +124 and +128.
# Keep this exact-spell allowlist at the wire layer: this module deliberately
# has no dependency on the reference database needed to classify effects.
UNIT_TARGET_RITUAL_SPELL_IDS = frozenset({1327, 1349})
# Carrier Eagle and Teleport Item store the selected treasury item at +132.
# Their controlled orders use the same province/recipient/item layout despite
# Carrier Eagle having map range 4 and Teleport Item having map range 6.
ITEM_TRANSPORT_RITUAL_SPELL_IDS = frozenset({1285, 1320})
# Wish's free-text prompt is resolved by the client before the order is saved.
# For a named magic item, +124 is the result-family discriminator 10001 and
# +128 is the stable item id; the original text is not serialized at all. This is
# deliberately an exact-spell rule because other effect-34 result families use
# different values in the same overloaded fields.
WISH_RITUAL_SPELL_IDS = frozenset({WISH.WISH_SPELL_ID})
WISH_ITEM_RESULT_CODE = WISH.SPECS["magic_item"].code
WISH_RESULT_CODES = WISH.RESULT_CODES
RITUAL_ORDER_NAMES = {value: key for key, value in RITUAL_ORDER_CODES.items()}
# Battlefield placement: two signed per-squad arrays spanning -12..+12, with
# (0, 0) at centre.
#
#     X   -12 west/left ....... 0 ....... +12 east/right
#     Y   -12 north/top ....... 0 ....... +12 south/bottom
#
# Confirmed by moving one squad to three positions in turn: north (Y -12),
# then west (X -12, Y back to 0), then bottom-right (+12, +12). Each move
# changed only the bytes it should have. Turgis's Crossbowmen sit at X -7,
# which the player independently describes as about halfway west.
OFF_PLACE_X     = 176      # i8[5], negative = west
OFF_PLACE_Y     = 181      # i8[5], negative = north
PLACEMENT_EDGE  = 12       # magnitude at the battlefield border
OFF_STANCE      = 186      # from name_end: per-squad combat stance
OFF_TARGET      = 191      # from name_end: per-squad target preference
OFF_FORMATIONS  = 200      # from name_end: one u8 per squad, in squad order

# Squad combat orders. Two squads set to Hold and Attack both wrote 6 at +186
# while their targets differed, which is what separates stance from target.
STANCE_CODES:  dict[str, int] = {"hold_and_attack": 6}
TARGET_CODES:  dict[str, int] = {"archers": 2, "fliers": 4, "large_monsters": 8}

# Troops are bound to a commander by a shared token, not by position. The
# commander carries it at name_end+4 and every unit under him carries the same
# value in its 173-byte record. Commanders leading nobody hold 0xFFFFFFFF.
#
# RESOLVED: a **mounted** unit occupies two records, rider and mount. Two
# Knights of the Chalice wrote the token into four records while three
# Pikeneers wrote three, and moving a single Knight moved exactly two adjacent
# records. Knights of the Chalice ride Destriers; Pikeneers walk.
#
# Consequence for writing: reassigning a mounted unit means moving BOTH
# records. Moving only the rider would leave the mount behind, and nothing in
# the file would flag it.
NO_SQUAD = 0xFFFFFFFF

# Squad formations, confirmed by changing one squad at a time and watching
# which byte moved. Turgis leads Pikeneers then Crossbowmen; setting the
# Pikeneers to Double Line moved +200, and setting the Crossbowmen moved +201.
# So this is an array indexed by squad, not a single per-commander value.
FORMATION_CODES: dict[str, int] = {
    "box":         0,
    "line":        1,
    # Tomaso's Pikeneer squad repeatedly writes 2.  The client formation menu
    # and its enum-label formatter place Sparse Line between Line (1) and
    # Skirmish (3), independently fixing the missing name.
    "sparse_line": 2,
    "skirmish":    3,
    "double_line": 4,
}
FORMATION_NAMES = {v: k for k, v in FORMATION_CODES.items()}

ORDER_CODES: dict[str, int] = {
    "defend": 0,
    "move":   1,
    # 2 is BOTH sneak and Hide — one order, told apart by the destination.
    # Clodius's menu read "Hide" and the file holds code 2 with destination 0,
    # while Estorgant sneaking to Kratas held code 2 with destination 91. So
    # destination 0 means stay hidden here and a province means sneak there.
    #
    # `hide` is a public alias of the same verified code. display_name() still
    # decides which label applies from the parameter, so reverse lookup does
    # not answer "hide" for a Sneak that carries a province.
    "sneak":  2,
    "hide":   2,
    "patrol": 3,
    "research": 4,
    "empowerment": 5,          # parameter = magic path index; Fire = 0
    "preach": 6,
    "blood_hunt": 8,
    "forge_magic_item": 11,    # parameter = ITEM id; 1 = Fire Sword
    # Controlled dead-god save: setting Femur to Call God changed his order
    # from Reanimate Warriors (23, parameter 0) to code 14, parameter 54. The
    # parameter is Ermor's own nation id and identifies the god being recalled.
    "call_god": 14,
    # Construction submenu ids are the final strategic order codes directly.
    # Static disassembly of the 6.35 menu builder at 0x4eb4fa passes id 0x12
    # beside "Build lab (%d gold)". Build Temple=19 and fort construction=20
    # were already controlled-save observations.
    "build_laboratory": 18,
    "build_temple": 19,
    # 20 with destination 1 = Build Palisades, set via the Construct Building
    # submenu on Turgis in The Obsidian Waste and read back from the .2h. The
    # destination field carries the BUILDING, not a province, so the submenu's
    # entries are a code plus an index rather than one code each.
    "build_palisades": 20,
    # Controlled game save: upgrading the completed Palisades in province 98
    # writes code 20, parameter 2. It also writes the province queue's separate
    # construction selector; materialize.py owns that required side effect.
    "upgrade_fortress": 20,
    # The client order-name formatter maps these codes directly.  The first
    # dual-human controls supplied Warriors (Femur, H2) and Lictors (Ychekhes,
    # H3).  The turn-70 Ermor controls then put Domex (H1), Femur (H2), and
    # Ychekhes (H3) on each common legal choice in turn, fixing the complete
    # observed code run 21-25.  These are strategic Reanimate orders, not
    # rituals: they take no target or gems and persist like other local orders.
    "reanimate_ghouls": 21,
    "reanimate_soulless": 22,
    "reanimate_warriors": 23,
    "reanimate_horsemen": 24,
    "reanimate_lictors": 25,
    "become_prophet": 10,
    "pillage": 30,
    "instill_uprising": 34,
    # Top-level menu ids are order code + 100: this relation reproduces the
    # controlled Assassinate (0x94 -> 48) and Seduce (0xac -> 72) codes. The
    # 6.35 menu builder pairs "Demolish Fort" with 0x90 and "Demolish Lab"
    # with 0x91, fixing the file codes as 44 and 45.
    "demolish_fort": 44,
    "demolish_laboratory": 45,
    # The top-level menu pairs "Attack Current Province" with id 147, and
    # top-level ids are file order + 100. Four independently selected dual-
    # human controls wrote code 47 with the commander's current province in
    # +116, including commanders previously set to Hide rather than Assassinate.
    "attack_current_province": 47,
    "assassinate": 48,
    # Uvekhtu wrote 69 while standing on the newly conquered Black Throne.
    # The client formatter independently labels code 69 "Claim throne".
    "claim_throne": 69,
    "seduce": 72,
    "search_magic_sites_auto": 85,
}

# The shared u16 at +116 changes type with the order code. These types are
# evidence-backed, not inferred from menu position:
#
# * move/sneak: province id (with explicit 0 meaning Hide for code 2)
# * empowerment: standard magic-path index; Fire = 0 was observed directly
# * forge: reference item id; four distinct item ids were observed
# * build 20: building index; Palisades = 1 and Fortress upgrade = 2
# * local actions: 0
PARAM_NONE = "none"
PARAM_PROVINCE = "province"
PARAM_PROVINCE_OR_ZERO = "province_or_zero"
PARAM_CURRENT_PROVINCE = "current_province"
PARAM_MAGIC_PATH = "magic_path"
PARAM_ITEM = "item"
PARAM_BUILDING = "building"
PARAM_SPELL = "spell"
PARAM_NATION = "nation"

MAGIC_PATH_CODES = {
    "fire": 0, "air": 1, "water": 2, "earth": 3, "astral": 4,
    "death": 5, "nature": 6, "glamour": 7, "blood": 8,
}
MAGIC_PATH_NAMES = {v: k for k, v in MAGIC_PATH_CODES.items()}
BUILDING_CODES = {"palisades": 1, "fortress": 2}
BUILDING_NAMES = {v: k for k, v in BUILDING_CODES.items()}
FIXED_BUILDING_ORDERS = {
    "build_palisades": BUILDING_CODES["palisades"],
    "upgrade_fortress": BUILDING_CODES["fortress"],
}
# Structurally decoded, but public writes stay disabled until their treasury
# delta and complete eligibility calculations are evidence-backed.
#: Orders whose economics are decoded but whose public writer is still
#: withheld. Forging left this set once its gem cost and reservation were
#: confirmed against the game; empowerment remains, its cost scaling unread.
ECONOMIC_ORDERS_PENDING = frozenset({"empowerment"})


@dataclass(frozen=True)
class StrategicOrderSpec:
    """A verified order code and the meaning of its shared parameter."""
    code: int
    parameter_kind: str = PARAM_NONE
    fixed_parameter: int | None = None


ORDER_SPECS: dict[str, StrategicOrderSpec] = {
    name: StrategicOrderSpec(
        code,
        (PARAM_PROVINCE if name == "move" else
         PARAM_PROVINCE_OR_ZERO if name == "sneak" else
         PARAM_CURRENT_PROVINCE if name == "attack_current_province" else
         PARAM_MAGIC_PATH if name == "empowerment" else
         PARAM_ITEM if name == "forge_magic_item" else
         PARAM_NATION if name == "call_god" else
         PARAM_BUILDING if name in FIXED_BUILDING_ORDERS else PARAM_NONE),
        FIXED_BUILDING_ORDERS.get(name),
    )
    for name, code in ORDER_CODES.items()
}

# Compatibility vocabulary used by a few callers. It now includes every order
# whose controlled save wrote 0, including Blood Hunt and automatic site search.
TARGETLESS = frozenset(
    name for name, spec in ORDER_SPECS.items()
    if spec.parameter_kind == PARAM_NONE)
ORDER_NAMES: dict[int, str] = {}
for _name, _code in ORDER_CODES.items():
    ORDER_NAMES.setdefault(_code, _name)
ORDER_NAMES.update(RITUAL_ORDER_NAMES)

# Kept as a named compatibility constant for callers that used the earlier
# readback-only symbol. The executable's menu action value 0x72 is not the
# serialized order code: the controlled client-authored `.2h` writes 14.
CALL_GOD_ORDER_CODE = ORDER_CODES["call_god"]


def display_name(code: int, parameter: int) -> str:
    """The label the game's own menu shows, which is not always the code's name.

    Only code 2 differs so far: it is Hide standing still and sneak with a
    destination. Kept separate from ORDER_NAMES so the code-to-name mapping stays
    one-to-one — a reverse lookup that answered "hide" for every sneak would be
    wrong far more often than it was right.
    """
    if code == ORDER_CODES["sneak"]:
        return "sneak" if parameter else "hide"
    if code == ORDER_CODES["build_palisades"]:
        if parameter == BUILDING_CODES["palisades"]:
            return "build_palisades"
        if parameter == BUILDING_CODES["fortress"]:
            return "upgrade_fortress"
        return f"construct_building({parameter})"
    return ORDER_NAMES.get(code, f"unknown({code})")


@dataclass(frozen=True)
class RitualFields:
    """The fields written beside a code-9/code-60 strategic order."""
    spell_id: int
    gem_cost: int
    target_province: int | None
    monthly: bool
    target_commander_runtime_index: int | None = None
    target_unit_runtime_index: int | None = None
    target_item_id: int | None = None
    wish_result_code: int | None = None
    wish_result: str | None = None
    wish_payload: int | None = None
    wish_item_id: int | None = None
    wish_unit_id: int | None = None
    wish_nation_id: int | None = None
    wish_amount: int | None = None


def read_ritual_fields(data: bytes, name_end: int) -> RitualFields | None:
    """Decode one ritual, or return ``None`` when this is another order."""
    code = data[name_end + OFF_ORDER_CODE]
    if code not in RITUAL_ORDER_NAMES:
        return None
    spell_id = struct.unpack_from("<H", data, name_end + OFF_PARAMETER)[0]
    gem_cost = struct.unpack_from("<I", data, name_end + OFF_RITUAL_COST)[0]
    province = struct.unpack_from("<I", data,
                                  name_end + OFF_RITUAL_PROVINCE)[0]
    recipient = struct.unpack_from("<i", data,
                                   name_end + OFF_RITUAL_SENTINELS)[0]
    payload = struct.unpack_from("<i", data,
                                 name_end + OFF_RITUAL_SENTINELS + 4)[0]
    unit_runtime = None
    target_province = province or None
    commander_runtime = recipient if recipient >= 0 else None
    wish_result_code = None
    wish_result = None
    wish_payload = None
    wish_item = None
    wish_unit = None
    wish_nation = None
    wish_amount = None
    if spell_id in WISH_RITUAL_SPELL_IDS:
        # t70-auto-3: wishing for Atlas of Creation (item 441) writes
        # 10001/441/-1.  Neither value is a province or commander handle.
        wish_result_code = province
        wish_payload = recipient
        selection = WISH.selection_from_wire(province, recipient)
        wish_result = (
            selection.result
            if not selection.result.startswith("unknown_") else None
        )
        if wish_result is not None:
            kind = WISH.SPECS[wish_result].payload_kind
            if recipient >= 0 and kind == "item":
                wish_item = recipient
            elif recipient >= 0 and kind in {"unit", "optional_unit"}:
                wish_unit = recipient
            elif recipient >= 0 and kind == "nation":
                wish_nation = recipient
            elif recipient >= 0 and kind == "amount":
                wish_amount = recipient
        target_province = None
        commander_runtime = None
    if spell_id in UNIT_TARGET_RITUAL_SPELL_IDS:
        # t48-auto-2 Gift of Reason writes the selected Lictor's runtime
        # handle (53) to both fields. Treating +124 as province 53 produced a
        # plausible-looking but false province target.
        if recipient >= 0 and province == recipient:
            unit_runtime = recipient
            target_province = None
            commander_runtime = None
    target_item = (
        payload if spell_id in ITEM_TRANSPORT_RITUAL_SPELL_IDS
        and payload >= 0 else None
    )
    return RitualFields(
        spell_id=spell_id,
        gem_cost=gem_cost,
        target_province=target_province,
        monthly=code == RITUAL_ORDER_CODES["monthly_ritual"],
        target_commander_runtime_index=commander_runtime,
        target_unit_runtime_index=unit_runtime,
        target_item_id=target_item,
        wish_result_code=wish_result_code,
        wish_result=wish_result,
        wish_payload=wish_payload,
        wish_item_id=wish_item,
        wish_unit_id=wish_unit,
        wish_nation_id=wish_nation,
        wish_amount=wish_amount,
    )


#: A forge order shares the ritual layout: the item id sits in the same +116
#: parameter a ritual uses for its spell. +120 stores the primary-path cost;
#: +124 stores the secondary-path cost (zero for a single-path item). +128 and
#: +132 remain zero where a ritual puts recipient sentinels.
FORGE_ORDER_CODE = ORDER_CODES["forge_magic_item"]


@dataclass(frozen=True)
class ForgeFields:
    """One forge order: what is being made and what it reserves."""
    item_id: int
    gem_cost: int
    secondary_gem_cost: int = 0


def read_forge_fields(data: bytes, name_end: int) -> ForgeFields | None:
    """Decode one forge order, or return ``None`` for any other order.

    Needed for the same reason `read_ritual_fields` is: replacing a forge has
    to refund its gem reservation before charging the new one, and the only
    record of the old reservation is the file itself.
    """
    if data[name_end + OFF_ORDER_CODE] != FORGE_ORDER_CODE:
        return None
    return ForgeFields(
        item_id=struct.unpack_from("<H", data, name_end + OFF_PARAMETER)[0],
        gem_cost=struct.unpack_from("<I", data,
                                    name_end + OFF_RITUAL_COST)[0],
        secondary_gem_cost=struct.unpack_from(
            "<I", data, name_end + OFF_RITUAL_PROVINCE)[0])


def read_empowerment_path(data: bytes, name_end: int) -> int | None:
    """The magic path of an empowerment order, or ``None`` for anything else.

    There is no cost to read: an empowerment leaves +120 at zero. Refunding a
    replaced one therefore needs the commander's level in this path plus the
    published price chart, not the file.
    """
    if data[name_end + OFF_ORDER_CODE] != ORDER_CODES["empowerment"]:
        return None
    return struct.unpack_from("<H", data, name_end + OFF_PARAMETER)[0]


def normalize_order_parameter(order: str, parameter: int | None) -> int:
    """Validate and normalize the u16 parameter for a strategic order."""
    spec = ORDER_SPECS[order]
    kind = spec.parameter_kind
    if kind == PARAM_NONE:
        if parameter not in (None, 0):
            raise ValueError(
                f"{order} takes no parameter; the game writes 0 there")
        return 0
    if kind == PARAM_BUILDING:
        if parameter is not None and parameter != spec.fixed_parameter:
            raise ValueError(
                f"{order} has verified building parameter "
                f"{spec.fixed_parameter}, got {parameter}")
        return int(spec.fixed_parameter)
    if parameter is None:
        raise ValueError(f"{order} needs a {kind} parameter")
    if kind == PARAM_PROVINCE and not (1 <= parameter <= 5000):
        raise ValueError(f"implausible province id {parameter}")
    if kind == PARAM_PROVINCE_OR_ZERO and not (0 <= parameter <= 5000):
        raise ValueError(f"implausible province id {parameter}")
    if kind == PARAM_CURRENT_PROVINCE and not (1 <= parameter <= 5000):
        raise ValueError(f"implausible current province id {parameter}")
    if kind == PARAM_ITEM and not (1 <= parameter <= 0xFFFE):
        raise ValueError(f"implausible item id {parameter}")
    if kind == PARAM_MAGIC_PATH and parameter not in MAGIC_PATH_NAMES:
        raise ValueError(
            f"magic path index must be 0-{max(MAGIC_PATH_NAMES)}, got "
            f"{parameter}")
    if kind == PARAM_NATION and not (1 <= parameter <= 0xFFFE):
        raise ValueError(f"implausible nation id {parameter}")
    return parameter


# ---------------------------------------------------------------------------
# Battle orders. Decoded from one save where the player set a different value in
# every field, which is what made each byte readable.

# ONE stance vocabulary covers both a commander's own battle order and his
# squads'. Retreat is 7 in both places, which is what shows they are the same
# table rather than two that happen to overlap. Some values have only ever been
# seen in one position — Cast Spells and Stay Behind Troops on commanders, Fire
# and Guard Commander on squads — but nothing suggests separate namespaces.
#
# A commander's own order sits at +198 with its target at +199. The squad
# equivalents are arrays of five at +186 and +191, indexed by squad slot.
STANCE_CODES: dict[str, int] = {
    "none": 0,
    "attack": 1,                    # commander and squad
    "fire": 2,                      # squad
    "guard_commander": 4,           # squad
    "cast_spells": 5,               # commander
    "hold_and_attack": 6,           # squad
    "retreat": 7,                   # both
    "fire_and_keep_distance": 9,    # squad
    "stay_behind_troops": 10,       # commander
    "hold_and_fire": 11,            # squad
    "advance_and_cast_spells": 13,  # commander
}

# Kept as an alias: the squad arrays and the commander field share this table.
SQUAD_STANCE_CODES = STANCE_CODES

# Target, used identically at +199 for a commander and at +191 per squad. Not contiguous, so it is not a list index:
# archers through closest run 2-5, then large monsters jumps to 8 and rearmost
# to 9. Codes 6 and 7 are unobserved and deliberately absent rather than guessed.
TARGET_CODES_V2: dict[str, int] = {
    "none": 0,
    "archers": 2,
    "cavalry": 3,
    "fliers": 4,
    "closest": 5,
    "large_monsters": 8,
    "rearmost": 9,
}

SQUAD_TARGET_CODES = TARGET_CODES_V2

# The byte namespace is shared, but the client menus are not.  Keeping these
# sets explicit prevents a technically writable byte from becoming a nonsense
# order such as Guard Commander on the commander himself or Cast Spells on an
# ordinary troop squad.
COMMANDER_STANCES = frozenset({
    "none", "attack", "cast_spells", "retreat", "stay_behind_troops",
    "advance_and_cast_spells",
})
SQUAD_STANCES = frozenset({
    "none", "attack", "fire", "guard_commander", "hold_and_attack",
    "retreat", "fire_and_keep_distance", "hold_and_fire",
})
TARGETABLE_STANCES = frozenset({
    "attack", "fire", "hold_and_attack", "fire_and_keep_distance",
    "hold_and_fire",
})


def read_own_battle_order(data: bytes, name_end: int) -> tuple[int, int]:
    """The commander's own battle order and target, as raw codes.

    This was briefly recorded as "not stored in the order block", which was
    wrong: the search that concluded it excluded offsets 176-206 as squad
    arrays, and +198 sits inside that window. Ten commanders given ten different
    orders each moved exactly this byte and nothing else.
    """
    return data[name_end + OFF_OWN_STANCE], data[name_end + OFF_OWN_TARGET]

# +116 is NOT a destination. It is a general PARAMETER slot whose meaning is
# decided by the order code, and reading it as a province for every order would
# quietly produce nonsense:
#
#   move / sneak        province id
#   build (20)          building index      Palisades = 1, Fortress = 2
#   forge (11)          ITEM id             Fire Sword = 1, confirmed against
#                                           the reference items table
#   empowerment (5)     magic path index    Fire = 0
#
# Three different orders in one save all wrote a small integer there meaning
# three different things.
# Magic items carried by this commander: u32 item ids, one per equipment slot,
# starting at +24. The Fire Sword (id 1) went into slot 0 and the Enchanted Helmet
# (186) into slot 6, and the reference table types them "1-h wpn" and "helm", so
# the slot index is the equipment position rather than an order of acquisition.
#
# ONLY MAGIC ITEMS APPEAR HERE. Turgis carries a Full Helmet, a Broad Sword and a
# one-use Lance — all ordinary starting gear — and every one of his slots reads
# zero. So an empty slot means "nothing magical", not "nothing", and the unit's
# base equipment has to come from its type.
# Magic items a commander carries, as u16 item ids at fixed per-slot offsets.
#
# The observed map, from one save in which six items of six different types were
# equipped on the same commander:
#
#   +24  1-h wpn      +40  missile    +56  boots
#   +26  shield       +48  helm       +58  misc
#                     +54  armor      +60  misc
#
# Spacing is irregular — 2, 22, 6, 4, 2 — so this is NOT a contiguous array and
# must not be walked as one. Reading it as a u32 array happened to work while
# only two items were equipped, because the neighbouring halves were zero; the
# third item exposed that immediately. The same mistake as the carried-gem
# "stride 4", and for the same reason: a sparse sample hides the real spacing.
#
# ONLY MAGIC ITEMS APPEAR. Turgis carries a Full Helmet, Broad Sword and one-use
# Lance, all ordinary starting gear, and every one of these offsets reads zero.
#
EQUIPMENT_SLOTS: dict[str, int] = {
    "weapon": 24,
    "shield": 26,
    "ranged": 40,
    "helm":   48,
    "armor":  54,
    "boots":  56,
    "misc1":  58,
    "misc2":  60,
}

ITEM_TYPES_BY_SLOT: dict[str, frozenset[str]] = {
    "weapon": frozenset({"1-h wpn", "2-h wpn"}),
    "shield": frozenset({"shield"}),
    "ranged": frozenset({"missile"}),
    "helm": frozenset({"helm", "crown"}),
    "armor": frozenset({"armor", "barding"}),
    "boots": frozenset({"boots"}),
    "misc1": frozenset({"misc"}),
    "misc2": frozenset({"misc"}),
}


def read_equipment(data: bytes, name_end: int) -> dict[str, int]:
    """Magic items carried, keyed by slot. Empty slots are omitted.

    CALLER BEWARE: find_order_blocks() returns province blocks as well as
    commander blocks — "Marignon" and "The Obsidian Waste" appear alongside real
    commanders — and a province block has a different layout. Reading these
    offsets on one yields values that are sometimes valid item ids and always
    meaningless. Filter to commanders before calling.
    """
    out = {}
    for slot, off in EQUIPMENT_SLOTS.items():
        v = struct.unpack_from("<H", data, name_end + off)[0]
        if v and v != 0xFFFF:
            out[slot] = v
    return out


OFF_EQUIPMENT    = 24    # first slot; see EQUIPMENT_SLOTS

# Gems handed to this commander to carry into battle, distinct from the national
# pool. Values sit at +165 with a stride of 4.
#
# Gems handed to this commander to carry into battle, separate from the national
# pool: NINE CONSECUTIVE BYTES at +165, in the game's path order.
#
# This took two wrong turns worth recording. Only fire, astral and blood were
# ever held, and those are indices 0, 4 and 8 — exactly four apart — so the array
# read as "stride 4 with slots 0, 1, 2" and appeared not to match the path order
# at all. Giving a single astral pearl then put the value in the second observed
# slot, which looked like proof of a fixed array whose slot 1 was astral.
#
# Both readings were artefacts of a sample that only ever contained every fourth
# path. The player's note that gems fill in F A W E S D N G B order resolved it
# immediately: at stride 1, astral is index 4 and blood is index 8, and all three
# saves then decode exactly.
#
# The lesson generalises past this field: a stride inferred from evenly spaced
# observations is not a stride, it is the spacing of the sample.
OFF_CARRIED_GEMS = 165
CARRIED_GEM_PATHS = ("fire", "air", "water", "earth", "astral",
                     "death", "nature", "glamour", "blood")


def read_carried_gems(data: bytes, name_end: int) -> dict[str, int]:
    """Gems this commander carries, keyed by path. Empty paths are omitted."""
    return {p: data[name_end + OFF_CARRIED_GEMS + i]
            for i, p in enumerate(CARRIED_GEM_PATHS)
            if data[name_end + OFF_CARRIED_GEMS + i]}


def read_placement(data: bytes, name_end: int, squad: int) -> tuple[int, int]:
    """One squad's signed battlefield coordinates, ``(x, y)``."""
    if not 0 <= squad < 5:
        raise ValueError(f"squad slot must be 0-4, got {squad}")
    x = struct.unpack_from("<b", data, name_end + OFF_PLACE_X + squad)[0]
    y = struct.unpack_from("<b", data, name_end + OFF_PLACE_Y + squad)[0]
    return x, y


OFF_OWN_STANCE   = 198   # the commander's own battle order
OFF_OWN_TARGET   = 199
OFF_SQUAD_SLOTS  = 4     # five entries of (u16 squad id, u16 handle marker)
SQUAD_SLOT_EMPTY = 0xFFFF
# The high half of a squad handle is not universal or reliably player-wide.
# Ermor writes 15, Marignon 17, and Dashtaghnay simultaneously holds Caelum
# squads with 6 and 7. It is a small allocation namespace rather than a nation
# id; new squads reuse the greatest marker evidenced by assigned own troops.
SQUAD_MARKER_MIN = 1
SQUAD_MARKER_MAX = 0xFF
# The low half is an ordinary nonzero u16; FFFF alone is the empty sentinel.
# The later Caelum controls include client allocations 15332 and 9917, while
# earlier games reached 64360. The game also accepted our synthetic 52964 when
# it collided with neither the current .2h nor .trn. Thus collision avoidance,
# not a narrower cosmetic range or a second allocation table, is operative.
SQUAD_ID_MIN = 1
SQUAD_ID_MAX = 0xFFFE
OFF_SPELL_QUEUE  = 136   # five i16 single-round order slots
OFF_STANCE       = 186   # five u8, one per squad slot
OFF_TARGET       = 191
OFF_FORMATION    = 200

SPELL_QUEUE_EMPTY = -1

# Single-round orders, one per slot. A POSITIVE value is a spell id and means
# "cast a specific spell"; the negatives are the built-in options.
#
# Decoded by setting one order on each of five commanders in a single save, so
# no two shared a code. -2 and -4 are unaccounted for and are deliberately absent
# rather than filled in by counting.
SINGLE_ROUND_CODES: dict[str, int] = {
    "hold_one_turn": -3,
    "hold_or_cast_a_spell": 0,
    "attack_one_turn": -5,
    "fly_attack_one_turn": -6,
}


def describe_single_round(value: int, spell_names: dict[int, str] | None = None) -> str:
    """Human-readable label for one queued single-round order."""
    if value == SPELL_QUEUE_EMPTY:
        return "empty"
    if value > 0:
        name = (spell_names or {}).get(value)
        return f"cast {name}" if name else f"cast spell {value}"
    for label, code in SINGLE_ROUND_CODES.items():
        if code == value:
            return label.replace("_", " ")
    return f"unknown({value})"


def read_spell_queue(data: bytes, name_end: int) -> list[int]:
    """The five single-round order slots, as signed values.

    A positive value is a SPELL ID, confirmed against the reference spell table:
    Sugaar queued Summon Hawk, Summon Storm Power and Air Shield and the slots
    read 927, 928 and 245, which are exactly those three spells' ids. Urraca's
    Fire Flies read 244.

    A negative value is a built-in single-round order rather than a spell. -1 is
    an empty slot. -3, -5 and -6 have been seen alongside a queue described as
    "hold one turn, cast a spell, fly attack", but five slots came back filled
    for four described orders, so the individual mapping is NOT established and
    is deliberately not guessed here.
    """
    return [struct.unpack_from("<h", data, name_end + OFF_SPELL_QUEUE + 2 * i)[0]
            for i in range(5)]


def read_squad_slots(data: bytes, name_end: int) -> list[dict[str, int]]:
    """Authoritative occupied squad slots, including sparse slot indices.

    Combat-array bytes survive when a squad is removed. The five records at
    +4 do not: an occupied slot is ``(u16 squad_id, u16 handle_marker)`` and an
    empty one is ``FFFF/FFFF``. The second u16 is the high half of every
    follower's u32 warband token. It belongs to the squad token, not reliably
    to the player: controlled Caelum has marker 6 in Dashtaghnay's slot 0 and
    marker 7 in his slot 1 at the same time. Bruise on turn 30 is the decisive
    sparse case: slot 0 is empty while slot 1 remains occupied, even though
    both slots retain old stance/target/placement bytes.
    """
    out = []
    for slot in range(5):
        squad_id, marker = struct.unpack_from(
            "<HH", data, name_end + OFF_SQUAD_SLOTS + 4 * slot)
        if (squad_id, marker) == (SQUAD_SLOT_EMPTY, SQUAD_SLOT_EMPTY):
            continue
        if (squad_id == SQUAD_SLOT_EMPTY
                or not SQUAD_MARKER_MIN <= marker <= SQUAD_MARKER_MAX):
            raise ValueError(
                f"malformed squad slot {slot}: id={squad_id}, marker={marker}")
        out.append({
            "slot": slot,
            "squad_id": squad_id,
            "marker": marker,
            "token": (marker << 16) | squad_id,
        })
    return out


def _squad_token_slots(data: bytes, unit_tokens: set[int],
                       requested_commanders: set[int] | None = None
                       ) -> dict[int, tuple[int, int]]:
    """Map evidenced warband tokens to their authoritative commander slots."""
    requested_commanders = requested_commanders or set()
    token_slots: dict[int, tuple[int, int]] = {}
    for commander_id, block in find_order_blocks(data).items():
        try:
            slots = read_squad_slots(data, block.name_end)
        except ValueError:
            continue
        for row in slots:
            token = row["token"]
            # The order-block locator also sees province-name records. A slot
            # is genuine only if own troops carry it, or its commander is the
            # explicit target of an operation that may create a new squad.
            if token not in unit_tokens and commander_id not in requested_commanders:
                continue
            if token in token_slots:
                raise ValueError(f"squad token {token:#x} belongs to two slots")
            token_slots[token] = (commander_id, row["slot"])
    return token_slots


def infer_squad_marker(data: bytes, nation_id: int) -> int:
    """Return the client's evidenced marker namespace for a new squad.

    Assigned own troop records provide the independent half of the join: their
    warband token's high u16 must be one of the marker values observed in real
    commander slot records. Refuse rather than borrowing another nation's
    marker when a player has no assigned troops from which to infer it.

    The controlled Caelum position has simultaneous markers 6 and 7. Creating
    two further squads in consecutive saves assigns marker 7 to both, while
    leaving the marker-6 squad intact. Thus allocation reuses the greatest
    active marker; it does not require a single nation-wide value or increment
    the marker per squad. Marignon independently reuses its active marker 17.
    """
    markers = {
        unit.warband >> 16
        for unit in read_h2_units(data, nation_id)
        if unit.warband != NO_SQUAD
        and SQUAD_MARKER_MIN <= unit.warband >> 16 <= SQUAD_MARKER_MAX
    }
    if not markers:
        raise ValueError(
            f"could not infer a squad marker for nation {nation_id}; "
            "no assigned own troops carry one")
    return max(markers)


def read_squad_orders(data: bytes, name_end: int) -> list[dict[str, int]]:
    """Per-squad battle orders, one dict per occupied squad slot."""
    out = []
    for occupied in read_squad_slots(data, name_end):
        i = occupied["slot"]
        stance = data[name_end + OFF_STANCE + i]
        x, y = read_placement(data, name_end, i)
        out.append({
            "slot": i,
            "squad_id": occupied["squad_id"],
            "marker": occupied["marker"],
            "token": occupied["token"],
            "stance": stance,
            "target": data[name_end + OFF_TARGET + i],
            "formation": data[name_end + OFF_FORMATION + i],
            "x": x,
            "y": y,
        })
    return out


def read_h2_units(data: bytes, nation_id: int):
    """Genuine unit records in an orders file for one nation.

    `.2h` embeds the same 173-byte unit records as `.trn`. The generic scanner
    intentionally admits a handful of shape-compatible records; one belonging
    to us is genuine only when its home province at +6 is a real province, the
    same invariant PlayerView applies to the `.trn` roster.
    """
    from dom6_assistant.file_reader.formats import units as U
    # Home is read SIGNED and only zero is rejected. A mercenary has no home
    # province and carries a negative sentinel; reading unsigned turned -2 into
    # 65534 and dropped every mercenary here, the same way it once hid a
    # 126-unit company from `own_units`. Zero is the phantom marker.
    out = []
    for unit in U.find_units(data, nation_id=nation_id):
        home = struct.unpack_from("<h", data, unit.offset + 6)[0]
        if home == 0 or home > 5000:
            continue
        out.append(unit)
    return out


def read_commander_h2_unit(data: bytes, nation_id: int,
                           commander_id: int):
    """Return the unique embedded unit joined to one commander block.

    Commander ids and unit instance ids are different namespaces.  The wire
    join is the first u32 after the commander's encoded name, matched against
    the unit record's runtime index at -32.  Pretenders make the distinction
    especially visible: Mambo's instance id is the 65535 sentinel while his
    runtime index is 3157.
    """
    block = find_order_blocks(data, valid_ids={commander_id}).get(commander_id)
    if block is None:
        raise OrderTableNotLocated(
            f"no order block for commander {commander_id} in this file")
    runtime_index = struct.unpack_from("<I", data, block.name_end)[0]
    matches = [
        unit for unit in read_h2_units(data, nation_id)
        if unit.runtime_index == runtime_index
    ]
    if len(matches) != 1:
        raise ValueError(
            f"commander {commander_id} runtime index {runtime_index} matched "
            f"{len(matches)} embedded own-unit records; expected exactly one")
    return matches[0]


def allocate_squad_id(data: bytes, nation_id: int, commander_id: int,
                      slot: int, instance_ids: list[int], *,
                      reserved_ids: set[int] | None = None) -> int:
    """Choose a stable-looking, collision-free id for a new squad.

    The chosen id is recorded as intent, so this only runs at decision time.
    Hashing the immutable inputs distributes ids through the observed client
    range; linear probing makes collision handling deterministic.
    """
    if not 0 <= slot < 5:
        raise ValueError(f"squad slot must be 0-4, got {slot}")
    if not instance_ids:
        raise ValueError("give at least one unit instance")

    used: set[int] = set(reserved_ids or ())
    for block in find_order_blocks(data).values():
        try:
            used.update(row["squad_id"]
                        for row in read_squad_slots(data, block.name_end))
        except (IndexError, struct.error, ValueError):
            continue
    for unit in read_h2_units(data, nation_id):
        if (unit.warband != NO_SQUAD
                and SQUAD_MARKER_MIN <= unit.warband >> 16 <= SQUAD_MARKER_MAX):
            used.add(unit.warband & 0xFFFF)

    payload = struct.pack("<IB", commander_id, slot)
    payload += b"".join(struct.pack("<H", value)
                        for value in sorted(instance_ids))
    start = int.from_bytes(
        hashlib.sha256(data + payload).digest()[:4], "little")
    span = SQUAD_ID_MAX - SQUAD_ID_MIN + 1
    for step in range(span):
        candidate = SQUAD_ID_MIN + ((start + step) % span)
        if candidate not in used:
            return candidate
    raise ValueError("no collision-free squad id remains in the verified range")


class OrderTableNotLocated(RuntimeError):
    """Raised when a commander's order block cannot be found in this file."""


@dataclass
class Order:
    """One commander's order."""
    commander_id: int
    commander_name: str
    offset: int                # block start (the id)
    name_end: int              # byte after the name terminator
    destination: int
    order_code: int
    formations: list[int] = field(default_factory=list)

    @property
    def order_name(self) -> str:
        return display_name(self.order_code, self.destination)

    @property
    def parameter(self) -> int:
        """Raw +116 value; ``destination`` remains as a compatibility alias."""
        return self.destination

    @property
    def parameter_kind(self) -> str:
        if self.order_code in RITUAL_ORDER_NAMES:
            return PARAM_SPELL
        spec = ORDER_SPECS.get(self.order_name)
        return spec.parameter_kind if spec else "unknown"

    def __str__(self) -> str:
        kind = self.parameter_kind
        if kind == PARAM_NONE:
            return f"{self.commander_name}: {self.order_name}"
        if kind in (PARAM_PROVINCE, PARAM_PROVINCE_OR_ZERO,
                    PARAM_CURRENT_PROVINCE):
            if not self.parameter:
                return f"{self.commander_name}: {display_name(self.order_code, 0)}"
            value = f"province {self.parameter}"
        elif kind == PARAM_ITEM:
            value = f"item {self.parameter}"
        elif kind == PARAM_MAGIC_PATH:
            value = MAGIC_PATH_NAMES.get(self.parameter,
                                         f"path {self.parameter}")
        elif kind == PARAM_BUILDING:
            value = BUILDING_NAMES.get(self.parameter,
                                       f"building {self.parameter}")
        elif kind == PARAM_SPELL:
            value = f"spell {self.parameter}"
        elif kind == PARAM_NATION:
            value = f"nation {self.parameter}"
        else:
            value = f"parameter {self.parameter}"
        return f"{self.commander_name}: {self.order_name} -> {value}"


def _plausible_name(chars: str) -> bool:
    """Does this look like a person's name rather than a misaligned read?

    A letters-only test is not enough. Reading one byte early decodes zero
    padding as "O", so junk like "GMOOOOOOOLOOOOzNOOClodius" passes it — every
    character is a letter and it even ends in a real name. Requiring the shape
    of a name (a capital, then mostly lower case, with no run of capitals in
    the middle) rejects those without needing to know the real names first.
    """
    if not chars or not chars[0].isupper():
        return False
    if not all(c.isalpha() or c in " '-" for c in chars):
        return False
    # Two capitals in a row at the start is the signature of a one-byte-early
    # read: the padding decodes to "O" and the real name's own capital follows,
    # giving "OMarignon". No real name here starts that way.
    if len(chars) > 1 and chars[1].isupper():
        return False
    body = chars[1:]
    if sum(c.isupper() for c in body) > sum(c.islower() for c in body):
        return False
    return "OO" not in chars


def find_order_blocks(data: bytes,
                      valid_ids: set[int] | None = None) -> dict[int, Order]:
    """Every commander order block in the file, keyed by commander id.

    Scans every offset rather than skipping past a match: an earlier version
    advanced to the end of each block it parsed and stepped straight over the
    first two commanders.

    **Pass `valid_ids`.** The province name table has the same shape as the
    commander table — u32 id followed by an XOR name — so an unfiltered scan
    returns provinces as commanders ("Marignon", "The Obsidian Waste"). That
    went unnoticed for a long time because every caller looked up commanders by
    an id it already knew; only code that *iterates* blocks sees the problem.

    `dom6_assistant.file_reader.formats.commanders.read_commander_names` on the
    matching .trn gives the right set: it identifies the commander run by
    checking which one contains the pretender ids the nation records point at,
    which is self-validating in a way a name-shape test is not.
    """
    out: dict[int, Order] = {}
    for i in range(len(data) - 8):
        cid = struct.unpack_from("<I", data, i)[0]
        if not (1 <= cid <= 60000):
            continue
        # A name's first byte is an encoded letter, never zero. Without this the
        # scan settles one byte early and decodes the preceding zero padding as
        # a leading "O", producing junk like "OCOOOOIOOOOMOOOOfNOOSugaar" that
        # still passes a letters-only test because O is a letter. The .trn name
        # scanner got this guard; this one did not.
        if data[i + 4] == 0:
            continue
        j, chars = i + 4, []
        while j < len(data) and data[j] != 0x4F:
            c = data[j] ^ 0x4F
            if c > 127 or not (chr(c).isalpha() or chr(c) in " '-"):
                chars = None
                break
            chars.append(chr(c))
            j += 1
        if not chars or not (2 <= len(chars) <= 40):
            continue
        name = "".join(chars)
        if not _plausible_name(name):
            continue
        end = j + 1
        if end + OFF_ORDER_CODE >= len(data):
            continue
        if valid_ids is not None and cid not in valid_ids:
            continue
        out.setdefault(cid, Order(
            commander_id=cid, commander_name=name, offset=i, name_end=end,
            destination=struct.unpack_from("<H", data, end + OFF_DESTINATION)[0],
            order_code=data[end + OFF_ORDER_CODE],
            # Squad count is not known, so this reads the tail of the block.
            # Trailing zeros are indistinguishable from squads set to Box,
            # which is why the count has to come from elsewhere before
            # formations can be safely written.
            formations=list(data[end + OFF_FORMATIONS:end + OFF_FORMATIONS + 8])))
    return out


def read_orders(path_or_bytes: str | Path | bytes,
                only: set[int] | None = None) -> list[Order]:
    """Commander orders in a .2h, in commander-id order."""
    data = (path_or_bytes if isinstance(path_or_bytes, bytes)
            else Path(path_or_bytes).read_bytes())
    blocks = find_order_blocks(data)
    return [blocks[k] for k in sorted(blocks) if only is None or k in only]


class OrdersEditor:
    """Edits a .2h in memory, then writes it back.

    Never touches the trailer: the game ignores it on load, and inventing a
    value would be worse than leaving the original honestly stale.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = bytearray(self.path.read_bytes())
        self._original = bytes(self.data)
        self.blocks = find_order_blocks(bytes(self.data))

    def set_order(self, commander_id: int, order: str,
                  parameter: int | None = None) -> None:
        """Set one commander's order. Raises rather than guessing."""
        if order not in ORDER_CODES:
            raise ValueError(
                f"order {order!r} is not verified against the game. "
                f"Known: {sorted(ORDER_CODES)}. Adding one means running the "
                f"experiment, not assuming a code.")
        block = self.blocks.get(commander_id)
        if block is None:
            raise OrderTableNotLocated(
                f"no order block for commander {commander_id} in this file")
        parameter = normalize_order_parameter(order, parameter)
        # The client clears the ritual-only tail when a caster returns to an
        # ordinary order.  Leaving a target/cost behind is probably ignored by
        # non-ritual codes, but this writer does not preserve stale fields on
        # the strength of "probably".
        if (block.order_code in RITUAL_ORDER_NAMES
                or block.order_code == FORGE_ORDER_CODE):
            self.data[block.name_end + OFF_RITUAL_COST:
                      block.name_end + OFF_RITUAL_SENTINELS + 8] = b"\x00" * 16
        struct.pack_into("<H", self.data, block.name_end + OFF_PARAMETER,
                         parameter)
        # The client clears the two bytes immediately following the shared
        # parameter whenever an ordinary strategic order is selected. Fresh
        # commander blocks can contain the 0xffff sentinel here; preserving it
        # makes an otherwise-correct move differ from the client-authored
        # record. Every controlled ordinary-order save has zero padding.
        self.data[block.name_end + OFF_PARAMETER + 2:
                  block.name_end + OFF_PARAMETER + 4] = b"\x00\x00"
        self.data[block.name_end + OFF_ORDER_CODE] = ORDER_CODES[order]

    def set_shape(self, commander_id: int, nation_id: int,
                  target_type_id: int, target_hp: int, *,
                  source_type_id: int | None = None,
                  source_hp: int | None = None) -> bool:
        """Apply an instantaneous Change Shape to the embedded unit record.

        Returns false when the pristine base is already in the requested form,
        which makes repeated materialisation and toggling back to the base
        idempotent.  Optional source values make a stale or wrong-form base a
        hard error rather than changing whichever record happens to be there.
        """
        if not 1 <= target_type_id <= 0xFFFF:
            raise ValueError(f"implausible target unit type {target_type_id}")
        if not 1 <= target_hp <= 0xFFFF:
            raise ValueError(f"implausible target HP {target_hp}")
        unit = read_commander_h2_unit(
            bytes(self.data), nation_id, commander_id)
        current = (unit.type_id, unit.hp)
        target = (target_type_id, target_hp)
        if current == target:
            return False
        if source_type_id is not None and current[0] != source_type_id:
            raise ValueError(
                f"embedded unit is type {current[0]}, expected source type "
                f"{source_type_id} or target type {target_type_id}")
        if source_hp is not None and current[1] != source_hp:
            raise ValueError(
                f"embedded unit has {current[1]} HP, expected source HP "
                f"{source_hp} or target HP {target_hp}; wounded-form HP "
                "conversion has not been decoded")
        struct.pack_into("<HH", self.data, unit.offset,
                         target_type_id, target_hp)
        return True

    def set_ritual(self, commander_id: int, spell_id: int, gem_cost: int,
                   target_province: int | None = None, *,
                   target_commander_runtime_index: int | None = None,
                   target_unit_runtime_index: int | None = None,
                   target_item_id: int | None = None,
                   wish_result: str | None = None,
                   wish_item_id: int | None = None,
                   wish_unit_id: int | None = None,
                   wish_nation_id: int | None = None,
                   monthly: bool = False) -> None:
        """Set the complete observed ritual structure for one commander."""
        block = self._block(commander_id)
        if not 1 <= spell_id <= 0xFFFE:
            raise ValueError(f"implausible spell id {spell_id}")
        if not 0 <= gem_cost <= 0xFFFFFFFF:
            raise ValueError(f"implausible ritual gem cost {gem_cost}")
        if (target_unit_runtime_index is not None
                and (target_province is not None
                     or target_commander_runtime_index is not None
                     or target_item_id is not None
                     or wish_item_id is not None
                     or wish_unit_id is not None
                     or wish_nation_id is not None)):
            raise ValueError(
                "a ritual unit target cannot also name a province, commander, "
                "or item")
        if (target_item_id is not None
                and (target_province is None
                     or target_commander_runtime_index is None)):
            raise ValueError(
                "a ritual item payload requires a target province and commander")
        wish_objects = sum(value is not None for value in (
            wish_item_id, wish_unit_id, wish_nation_id))
        if wish_objects > 1:
            raise ValueError("Wish accepts only one item, unit, or nation payload")
        if wish_item_id is not None:
            if spell_id not in WISH_RITUAL_SPELL_IDS:
                raise ValueError(
                "an item-to-create payload is valid only for Wish")
            if (target_province is not None
                    or target_commander_runtime_index is not None
                    or target_item_id is not None):
                raise ValueError(
                    "Wish's item payload cannot also name a province, "
                    "commander, or transported item")
            if not 1 <= wish_item_id <= 1500:
                raise ValueError(f"implausible Wish item {wish_item_id}")
            if wish_result not in (None, "magic_item"):
                raise ValueError(
                    "wish_item_id can be combined only with the magic_item "
                    "Wish result")
            wish_result = "magic_item"
        if wish_unit_id is not None:
            if spell_id not in WISH_RITUAL_SPELL_IDS:
                raise ValueError("a Wish unit payload is valid only for Wish")
            if not 1 <= wish_unit_id <= 0x7FFFFFFF:
                raise ValueError(f"implausible Wish unit {wish_unit_id}")
            if wish_result not in ("unit", "horror"):
                raise ValueError(
                    "wish_unit_id requires the unit or horror Wish result")
        if wish_nation_id is not None:
            if spell_id not in WISH_RITUAL_SPELL_IDS:
                raise ValueError("a Wish nation payload is valid only for Wish")
            if not 0 <= wish_nation_id <= 0x7FFFFFFF:
                raise ValueError(f"implausible Wish nation {wish_nation_id}")
            if wish_result != "kill_pretender":
                raise ValueError(
                    "wish_nation_id requires the kill_pretender Wish result")
        if wish_result is not None:
            if spell_id not in WISH_RITUAL_SPELL_IDS:
                raise ValueError("wish_result is valid only for Wish")
            if (target_province is not None
                    or target_commander_runtime_index is not None
                    or target_unit_runtime_index is not None
                    or target_item_id is not None):
                raise ValueError(
                    "a Wish result cannot also name a province, commander, "
                    "troop, or transported item")
            if wish_result not in WISH_RESULT_CODES:
                raise ValueError(
                    f"unsupported Wish result {wish_result!r}; supported: "
                    f"{sorted(WISH_RESULT_CODES)}")
            spec = WISH.SPECS[wish_result]
            required = {
                "item": wish_item_id,
                "unit": wish_unit_id,
                "nation": wish_nation_id,
            }.get(spec.payload_kind)
            if spec.payload_kind in {"item", "unit", "nation"} and required is None:
                raise ValueError(
                    f"the {wish_result} Wish result requires its {spec.payload_kind} id")
            if spec.payload_kind == "optional_unit" and wish_unit_id is None:
                pass  # Generic Horror is deliberately serialized as -1.
            if (spec.payload_kind not in
                    {"item", "unit", "optional_unit", "nation"}
                    and wish_objects):
                raise ValueError(f"the {wish_result} Wish result takes no object id")
        elif spell_id in WISH_RITUAL_SPELL_IDS:
            raise ValueError(
                "Wish requires a decoded result payload")
        if target_unit_runtime_index is not None:
            if not 0 <= target_unit_runtime_index <= 0x7FFFFFFF:
                raise ValueError(
                    "implausible ritual unit runtime index "
                    f"{target_unit_runtime_index}")
            province = target_unit_runtime_index
        elif wish_result is not None:
            province = WISH_RESULT_CODES[wish_result]
        else:
            province = target_province or 0
        if (target_unit_runtime_index is None and wish_result is None
                and not 0 <= province <= 5000):
            raise ValueError(f"implausible ritual target province {province}")
        struct.pack_into("<H", self.data, block.name_end + OFF_PARAMETER,
                         spell_id)
        # +118/+119 are padding in all three controlled saves.
        self.data[block.name_end + OFF_PARAMETER + 2:
                  block.name_end + OFF_RITUAL_COST] = b"\x00\x00"
        struct.pack_into("<I", self.data, block.name_end + OFF_RITUAL_COST,
                         gem_cost)
        struct.pack_into("<I", self.data, block.name_end + OFF_RITUAL_PROVINCE,
                         province)
        wish_spec = WISH.SPECS[wish_result] if wish_result is not None else None
        recipient = (
            int(wish_item_id)
            if wish_item_id is not None
            else int(wish_unit_id)
            if wish_unit_id is not None
            else int(wish_nation_id)
            if wish_nation_id is not None
            else int(wish_spec.fixed_payload)
            if wish_spec is not None and wish_spec.payload_kind in {"fixed", "amount"}
            else target_unit_runtime_index
            if target_unit_runtime_index is not None
            else (-1 if target_commander_runtime_index is None
                  else target_commander_runtime_index)
        )
        if not -1 <= recipient <= 0x7FFFFFFF:
            raise ValueError(
                f"implausible ritual recipient runtime index {recipient}")
        payload = -1 if target_item_id is None else target_item_id
        if not -1 <= payload <= 1500:
            raise ValueError(f"implausible ritual target item {payload}")
        struct.pack_into("<ii", self.data,
                         block.name_end + OFF_RITUAL_SENTINELS,
                         recipient, payload)
        code_name = "monthly_ritual" if monthly else "cast_ritual"
        self.data[block.name_end + OFF_ORDER_CODE] = RITUAL_ORDER_CODES[code_name]

    def reserve_ritual_item(self, item_id: int) -> int:
        """Remove one selected item from the current treasury for transport.

        Carrier Eagle and Teleport Item keep the item's identity in the caster
        block at +132, while its treasury slot becomes zero. Both writes are
        required: leaving the item in the stash duplicates it when the ritual
        resolves.
        """
        from dom6_assistant.file_reader.formats import h2 as H2

        stash = H2.read_item_stash_slots(self.data)
        hits = [index for index, value in enumerate(stash) if value == item_id]
        if not hits:
            raise ValueError(
                f"item {item_id} is not in the current unequipped treasury")
        slot = hits[0]
        struct.pack_into(
            "<H", self.data, H2.item_stash_offset(self.data) + slot * 2, 0)
        return slot

    def refund_ritual_item(self, item_id: int) -> int:
        """Return a cancelled/replaced item-transport payload to the stash."""
        from dom6_assistant.file_reader.formats import h2 as H2

        stash = H2.read_item_stash_slots(self.data)
        if item_id in stash:
            raise ValueError(
                f"transported item {item_id} is already present in the treasury")
        slot = next((index for index, value in enumerate(stash) if value == 0), None)
        if slot is None:
            raise ValueError(
                f"no empty treasury slot can receive cancelled item {item_id}")
        struct.pack_into(
            "<H", self.data, H2.item_stash_offset(self.data) + slot * 2,
            item_id)
        return slot

    def set_forge(self, commander_id: int, item_id: int,
                  gem_cost: int, secondary_gem_cost: int = 0) -> None:
        """Set one commander to forge an item, with its gem reservation.

        The layout is the ritual's, so the ritual tail must be cleared: a
        commander switching from a ritual to a forge would otherwise keep a
        stale target province where the client expects zero.
        """
        block = self._block(commander_id)
        if not 1 <= item_id <= 0xFFFE:
            raise ValueError(f"implausible item id {item_id}")
        if not 0 <= gem_cost <= 0xFFFF:
            raise ValueError(f"implausible forge gem cost {gem_cost}")
        if not 0 <= secondary_gem_cost <= 0xFFFF:
            raise ValueError(
                f"implausible secondary forge gem cost {secondary_gem_cost}")
        struct.pack_into("<H", self.data, block.name_end + OFF_PARAMETER,
                         item_id)
        # +118/+119 are padding. +124 is the secondary-path reservation;
        # +128/+132 are zero where a ritual carries two sentinels.
        self.data[block.name_end + OFF_PARAMETER + 2:
                  block.name_end + OFF_RITUAL_COST] = b"\x00\x00"
        struct.pack_into("<I", self.data, block.name_end + OFF_RITUAL_COST,
                         gem_cost)
        struct.pack_into("<I", self.data,
                         block.name_end + OFF_RITUAL_PROVINCE,
                         secondary_gem_cost)
        self.data[block.name_end + OFF_RITUAL_SENTINELS:
                  block.name_end + OFF_RITUAL_SENTINELS + 8] = b"\x00" * 8
        self.data[block.name_end + OFF_ORDER_CODE] = FORGE_ORDER_CODE

    def set_empowerment(self, commander_id: int, path_index: int) -> None:
        """Empower one commander in one path.

        Unlike a forge or a ritual, the cost is NOT written into the block:
        every observed empowerment leaves +120 at zero while the national pool
        still pays. The caller adjusts the pool.
        """
        block = self._block(commander_id)
        if not 0 <= path_index < len(CARRIED_GEM_PATHS):
            raise ValueError(
                f"magic path index must be 0-{len(CARRIED_GEM_PATHS) - 1}, "
                f"got {path_index}")
        struct.pack_into("<H", self.data, block.name_end + OFF_PARAMETER,
                         path_index)
        # The ritual tail must be cleared, as for a forge: a caster switching
        # to empowerment would otherwise keep a stale cost and target.
        #
        # The length here is load-bearing. Assigning a differently-sized slice
        # to a bytearray RESIZES it, which shifts every following byte and
        # silently corrupts the file — this was written as 22 against an
        # 18-byte span and did exactly that.
        start = block.name_end + OFF_PARAMETER + 2
        end = block.name_end + OFF_RITUAL_SENTINELS + 8
        self.data[start:end] = b"\x00" * (end - start)
        self.data[block.name_end + OFF_ORDER_CODE] = ORDER_CODES["empowerment"]

    # -- battle setup ----------------------------------------------------
    #
    # Everything below writes a single byte at a known offset from `name_end`,
    # which is the whole reason these are safe to add: the offsets were each
    # established by changing one thing in game, saving, and diffing, and a
    # write puts the byte back where the diff found it. No field here is
    # written unless its code was observed — an unverified value would produce
    # a turn that resolves and does something else.

    def _block(self, commander_id: int) -> Order:
        block = self.blocks.get(commander_id)
        if block is None:
            raise OrderTableNotLocated(
                f"no order block for commander {commander_id} in this file")
        return block

    @staticmethod
    def _code(name: str, table: dict[str, int], what: str) -> int:
        if name not in table:
            raise ValueError(
                f"{what} {name!r} is not verified against the game. "
                f"Known: {sorted(table)}.")
        return table[name]

    def set_battle_order(self, commander_id: int, stance: str,
                         target: str | None = None) -> None:
        """The commander's OWN battle order, at +198 with its target at +199.

        Distinct from the squads he leads — those are `set_squad_order`. Ten
        commanders given ten different orders each moved exactly this byte and
        nothing else, which is what identified it.
        """
        block = self._block(commander_id)
        if stance not in COMMANDER_STANCES:
            raise ValueError(
                f"commander stance {stance!r} is not valid; known: "
                f"{sorted(COMMANDER_STANCES)}")
        if target not in (None, "none") and stance not in TARGETABLE_STANCES:
            raise ValueError(f"commander stance {stance!r} takes no target")
        self.data[block.name_end + OFF_OWN_STANCE] = self._code(
            stance, STANCE_CODES, "stance")
        if target is not None:
            self.data[block.name_end + OFF_OWN_TARGET] = self._code(
                target, TARGET_CODES_V2, "target")

    def set_squad_order(self, commander_id: int, squad: int, stance: str,
                        target: str | None = None) -> None:
        """One squad's stance at +186[squad] and target at +191[squad].

        `squad` is the slot, 0-4, in the order the game lists them. Five slots
        exist whether or not they are used; writing to an empty slot is
        refused, because a squad the commander does not lead cannot be given an
        order and doing so would silently write into another field's space.
        """
        block = self._block(commander_id)
        if not 0 <= squad < 5:
            raise ValueError(f"squad slot must be 0-4, got {squad}")
        slots = read_squad_orders(bytes(self.data), block.name_end)
        occupied = {row["slot"] for row in slots}
        if squad not in occupied:
            raise ValueError(
                f"commander {commander_id} has no squad in slot {squad}; "
                f"occupied slots are {sorted(occupied)}")
        if stance not in SQUAD_STANCES:
            raise ValueError(
                f"squad stance {stance!r} is not valid; known: "
                f"{sorted(SQUAD_STANCES)}")
        if target not in (None, "none") and stance not in TARGETABLE_STANCES:
            raise ValueError(f"squad stance {stance!r} takes no target")
        self.data[block.name_end + OFF_STANCE + squad] = self._code(
            stance, STANCE_CODES, "stance")
        if target is not None:
            self.data[block.name_end + OFF_TARGET + squad] = self._code(
                target, TARGET_CODES_V2, "target")

    def set_formation(self, commander_id: int, squad: int,
                      formation: int) -> None:
        """One squad's formation, at +200[squad]."""
        block = self._block(commander_id)
        if not 0 <= squad < 5:
            raise ValueError(f"squad slot must be 0-4, got {squad}")
        occupied = {
            row["slot"] for row in
            read_squad_orders(bytes(self.data), block.name_end)}
        if squad not in occupied:
            raise ValueError(
                f"commander {commander_id} has no squad in slot {squad}; "
                f"occupied slots are {sorted(occupied)}")
        if formation not in FORMATION_NAMES:
            raise ValueError(
                f"formation must be one of {sorted(FORMATION_NAMES)}; "
                f"got {formation}")
        self.data[block.name_end + OFF_FORMATION + squad] = formation

    def set_placement(self, commander_id: int, squad: int,
                      x: int, y: int) -> None:
        """Place one real squad on the confirmed signed -12..+12 grid."""
        block = self._block(commander_id)
        if not 0 <= squad < 5:
            raise ValueError(f"squad slot must be 0-4, got {squad}")
        slots = read_squad_orders(bytes(self.data), block.name_end)
        occupied = {row["slot"] for row in slots}
        if squad not in occupied:
            raise ValueError(
                f"commander {commander_id} has no squad in slot {squad}; "
                f"occupied slots are {sorted(occupied)}")
        for axis, value in (("x", x), ("y", y)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"placement {axis} must be an integer")
            if not -PLACEMENT_EDGE <= value <= PLACEMENT_EDGE:
                raise ValueError(
                    f"placement {axis} must be -{PLACEMENT_EDGE}.."
                    f"{PLACEMENT_EDGE}, got {value}")
        struct.pack_into("<b", self.data,
                         block.name_end + OFF_PLACE_X + squad, x)
        struct.pack_into("<b", self.data,
                         block.name_end + OFF_PLACE_Y + squad, y)

    def set_carried_gems(self, commander_id: int, path: str,
                         amount: int) -> None:
        """Set one path's carried-gem count in the nine-byte FAWESDNGB array."""
        block = self._block(commander_id)
        if path not in CARRIED_GEM_PATHS:
            raise ValueError(
                f"unknown gem path {path!r}. Known: {list(CARRIED_GEM_PATHS)}")
        if not isinstance(amount, int) or isinstance(amount, bool) or not 0 <= amount <= 255:
            raise ValueError(f"carried gem amount must be 0-255, got {amount!r}")
        self.data[block.name_end + OFF_CARRIED_GEMS
                  + CARRIED_GEM_PATHS.index(path)] = amount

    def set_spell_queue(self, commander_id: int, orders: list[int]) -> None:
        """Replace all five scripted rounds, padding unused slots with -1."""
        block = self._block(commander_id)
        if len(orders) > 5:
            raise ValueError(f"battle script holds at most 5 rounds, got {len(orders)}")
        verified_fixed = set(SINGLE_ROUND_CODES.values())
        for position, value in enumerate(orders):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"battle script slot {position} is not an integer")
            if value <= 0 and value not in verified_fixed:
                raise ValueError(
                    f"single-round code {value} is not verified; known fixed "
                    f"codes are {sorted(verified_fixed)}")
            if value > 0x7FFF:
                raise ValueError(f"spell id {value} does not fit the signed u16 slot")
        padded = orders + [SPELL_QUEUE_EMPTY] * (5 - len(orders))
        struct.pack_into("<5h", self.data, block.name_end + OFF_SPELL_QUEUE,
                         *padded)

    def assign_troops(
            self, nation_id: int,
            assignments: list[tuple[int, int, int]], *,
            new_squads: dict[tuple[int, int], int] | None = None,
            commander_provinces: Mapping[int, int] | None = None) -> dict:
        """Move unit instances into squad slots atomically.

        Each tuple is ``(instance_id, target_commander_id, target_slot)``.
        Existing targets retain the narrow same-type rule. ``new_squads`` may
        supply an evidence-backed u16 id for an empty ``(commander, slot)``;
        the slot's combat arrays are reset to the client-confirmed zero defaults.
        ``commander_provinces`` may provide exact locations derived from the
        commander's runtime-index join to the adjacent ``.trn``.  This permits
        creating the first squad under a troopless commander without guessing.
        Mounted riders take the adjacent mount record with them. If all records
        leave a source squad, only its authoritative +4 slot is cleared;
        combat-array bytes are left stale, matching game-authored sparse saves.
        """
        from dom6_assistant.file_reader.formats import units as U

        if not assignments:
            raise ValueError("give at least one unit assignment")
        instance_ids = [row[0] for row in assignments]
        if len(set(instance_ids)) != len(instance_ids):
            raise ValueError("a unit instance may appear only once per assignment")
        new_squads = new_squads or {}

        data = bytes(self.data)
        units = read_h2_units(data, nation_id)
        by_offset = {unit.offset: unit for unit in units}
        by_instance: dict[int, list] = {}
        for unit in units:
            if not unit.is_mount:
                by_instance.setdefault(unit.instance_id, []).append(unit)

        unit_tokens = {unit.warband for unit in units
                       if unit.warband != NO_SQUAD}
        # Existing targets already carry their complete per-squad token.  A
        # marker is needed only when populating an empty slot.  Inferring it
        # unconditionally made otherwise exact reassignment fail in Caelum's
        # controlled position merely because its two existing squads use
        # different markers (6 and 7).
        squad_marker = (
            infer_squad_marker(data, nation_id) if new_squads else None)
        requested_commanders = {row[1] for row in assignments}
        token_slots = _squad_token_slots(
            data, unit_tokens, requested_commanders)
        target_tokens: dict[tuple[int, int], int] = {}
        for token, key in token_slots.items():
            target_tokens[key] = token

        requested_targets = {(row[1], row[2]) for row in assignments}
        created_targets: dict[tuple[int, int], int] = {}
        for key, squad_id in new_squads.items():
            commander_id, slot = key
            if key not in requested_targets:
                raise ValueError(
                    f"new squad {key} has no unit assignment targeting it")
            if not 0 <= slot < 5:
                raise ValueError(f"new squad slot must be 0-4, got {slot}")
            if not 1 <= squad_id < SQUAD_SLOT_EMPTY:
                raise ValueError(f"implausible new squad id {squad_id}")
            self._block(commander_id)
            if key in target_tokens:
                raise ValueError(
                    f"commander {commander_id} squad slot {slot} is occupied")
            assert squad_marker is not None
            token = (squad_marker << 16) | squad_id
            if token in token_slots or token in unit_tokens:
                raise ValueError(f"new squad token {token:#x} is already in use")
            target_tokens[key] = token
            token_slots[token] = key
            created_targets[key] = token

        # Existing followers provide one independent location source.  The
        # caller may additionally provide the exact commander-stat join from
        # the .trn; disagreement is rejected below instead of choosing one.
        proven_provinces: dict[int, set[int]] = {}
        for token, (commander_id, _slot) in token_slots.items():
            if token in created_targets.values():
                continue
            for member in units:
                if member.warband == token and not member.is_mount:
                    proven_provinces.setdefault(commander_id, set()).add(
                        struct.unpack_from("<H", data, member.offset + 4)[0])
        for commander_id, province_id in (commander_provinces or {}).items():
            proven_provinces.setdefault(commander_id, set()).add(province_id)

        prepared = []
        for instance_id, commander_id, target_slot in assignments:
            matches = by_instance.get(instance_id, [])
            if len(matches) != 1:
                detail = "not found" if not matches else "not unique"
                raise ValueError(f"unit instance {instance_id} is {detail} in the .2h")
            unit = matches[0]
            target = target_tokens.get((commander_id, target_slot))
            occupied = sorted(
                slot for (cid, slot) in target_tokens if cid == commander_id)
            if target is None:
                raise ValueError(
                    f"commander {commander_id} has no occupied squad slot "
                    f"{target_slot}; occupied slots are {occupied}")
            if unit.warband == target:
                raise ValueError(
                    f"unit instance {instance_id} is already in that squad")
            if unit.warband != NO_SQUAD and unit.warband not in token_slots:
                raise ValueError(
                    f"unit instance {instance_id} has unknown source token "
                    f"{unit.warband:#x}")

            key = (commander_id, target_slot)
            is_new = key in created_targets
            target_members = [
                member for member in units
                if member.warband == target and not member.is_mount
            ]
            province = struct.unpack_from("<H", data, unit.offset + 4)[0]
            if is_new:
                if unit.warband != NO_SQUAD:
                    raise ValueError(
                        "new squads currently accept only unattached garrison "
                        f"troops; instance {instance_id} has a source squad")
                target_provinces = proven_provinces.get(commander_id, set())
                if len(target_provinces) != 1:
                    raise ValueError(
                        f"commander {commander_id} has no single proven province "
                        "from the stat join or existing squads; refusing to "
                        "guess their location")
            else:
                target_provinces = {
                    struct.unpack_from("<H", data, member.offset + 4)[0]
                    for member in target_members
                }
            if target_provinces != {province}:
                raise ValueError(
                    f"unit instance {instance_id} is in province {province}, "
                    f"but target squad is in {sorted(target_provinces)}")

            records = [unit]
            next_unit = by_offset.get(unit.offset + U.RECORD_SIZE)
            if next_unit is not None and next_unit.is_mount:
                if next_unit.warband != unit.warband:
                    raise ValueError(
                        f"rider {instance_id} and adjacent mount have different "
                        "source squads")
                mount_province = struct.unpack_from(
                    "<H", data, next_unit.offset + 4)[0]
                if mount_province != province:
                    raise ValueError(
                        f"rider {instance_id} and adjacent mount are in different "
                        "provinces")
                records.append(next_unit)
            prepared.append((unit, records, target))

        created = []
        for (commander_id, slot), token in created_targets.items():
            block = self._block(commander_id)
            squad_id = token & 0xFFFF
            struct.pack_into(
                "<HH", self.data,
                block.name_end + OFF_SQUAD_SLOTS + 4 * slot,
                squad_id, token >> 16)
            for offset in (OFF_PLACE_X, OFF_PLACE_Y, OFF_STANCE,
                           OFF_TARGET, OFF_FORMATION):
                self.data[block.name_end + offset + slot] = 0
            created.append({"commander_id": commander_id, "slot": slot,
                            "squad_id": squad_id, "token": token})

        final_tokens = {unit.offset: unit.warband for unit in units}
        moved = []
        for unit, records, target in prepared:
            source = unit.warband
            for record in records:
                final_tokens[record.offset] = target
            moved.append({
                "instance_id": unit.instance_id,
                "type_id": unit.type_id,
                "source_token": None if source == NO_SQUAD else source,
                "target_token": target,
                "records": len(records),
            })

        cleared = []
        for token, (commander_id, slot) in token_slots.items():
            if token in final_tokens.values():
                continue
            block = self._block(commander_id)
            struct.pack_into(
                "<HH", self.data,
                block.name_end + OFF_SQUAD_SLOTS + 4 * slot,
                SQUAD_SLOT_EMPTY, SQUAD_SLOT_EMPTY)
            cleared.append({"commander_id": commander_id, "slot": slot,
                            "token": token})

        for offset, token in final_tokens.items():
            if token != by_offset[offset].warband:
                struct.pack_into("<I", self.data, offset + U.OFF_WARBAND_OWN,
                                 token)
        return {"moved": moved, "created_slots": created,
                "cleared_slots": cleared}

    def detach_troops(self, nation_id: int,
                      instance_ids: list[int]) -> dict:
        """Detach troop instances into their unchanged province garrison.

        A garrison troop is represented by the same unit record with its u32
        warband token set to ``FFFFFFFF``. Mounted riders detach together with
        their immediately following mount. If the operation removes the last
        member of a squad, only that squad's authoritative slot token is
        cleared; its stale battle arrays are preserved exactly as the client
        does.
        """
        from dom6_assistant.file_reader.formats import units as U

        if not instance_ids:
            raise ValueError("give at least one unit instance to detach")
        if len(set(instance_ids)) != len(instance_ids):
            raise ValueError("a unit instance may appear only once per detachment")

        data = bytes(self.data)
        units = read_h2_units(data, nation_id)
        by_offset = {unit.offset: unit for unit in units}
        by_instance: dict[int, list] = {}
        for unit in units:
            if not unit.is_mount:
                by_instance.setdefault(unit.instance_id, []).append(unit)
        unit_tokens = {
            unit.warband for unit in units if unit.warband != NO_SQUAD
        }
        token_slots = _squad_token_slots(data, unit_tokens)

        prepared = []
        for instance_id in instance_ids:
            matches = by_instance.get(instance_id, [])
            if len(matches) != 1:
                detail = "not found" if not matches else "not unique"
                raise ValueError(
                    f"unit instance {instance_id} is {detail} in the .2h")
            unit = matches[0]
            if unit.warband == NO_SQUAD:
                raise ValueError(
                    f"unit instance {instance_id} is already in the garrison")
            if unit.warband not in token_slots:
                raise ValueError(
                    f"unit instance {instance_id} has unknown source token "
                    f"{unit.warband:#x}")
            records = [unit]
            next_unit = by_offset.get(unit.offset + U.RECORD_SIZE)
            if next_unit is not None and next_unit.is_mount:
                if next_unit.warband != unit.warband:
                    raise ValueError(
                        f"rider {instance_id} and adjacent mount have different "
                        "source squads")
                records.append(next_unit)
            prepared.append((unit, records))

        final_tokens = {unit.offset: unit.warband for unit in units}
        detached = []
        for unit, records in prepared:
            for record in records:
                final_tokens[record.offset] = NO_SQUAD
            commander_id, slot = token_slots[unit.warband]
            detached.append({
                "instance_id": unit.instance_id,
                "type_id": unit.type_id,
                "source_commander_id": commander_id,
                "source_slot": slot,
                "source_token": unit.warband,
                "records": len(records),
            })

        cleared = []
        for token, (commander_id, slot) in token_slots.items():
            if token in final_tokens.values():
                continue
            block = self._block(commander_id)
            struct.pack_into(
                "<HH", self.data,
                block.name_end + OFF_SQUAD_SLOTS + 4 * slot,
                SQUAD_SLOT_EMPTY, SQUAD_SLOT_EMPTY)
            cleared.append({
                "commander_id": commander_id, "slot": slot, "token": token,
            })

        for offset, token in final_tokens.items():
            if token != by_offset[offset].warband:
                struct.pack_into(
                    "<I", self.data, offset + U.OFF_WARBAND_OWN, token)
        return {"detached": detached, "cleared_slots": cleared}

    def set_equipment(self, commander_id: int, slot: str,
                      item_id: int) -> None:
        """Put an item in one of a commander's slots.

        Slots are u16 at the offsets in EQUIPMENT_SLOTS. Item 0 empties the
        slot. Whether the commander actually holds that item is NOT checked
        here — the treasury is in the .trn and this class only sees the .2h —
        so a caller that can check should.
        """
        block = self._block(commander_id)
        if slot not in EQUIPMENT_SLOTS:
            raise ValueError(
                f"unknown slot {slot!r}. Mapped: {sorted(EQUIPMENT_SLOTS)}.")
        if not 0 <= item_id <= 1500:
            raise ValueError(f"implausible item id {item_id}")
        struct.pack_into("<H", self.data,
                         block.name_end + EQUIPMENT_SLOTS[slot], item_id)

    def transfer_equipment(self, commander_id: int, slot: str,
                           item_id: int) -> None:
        """Equip/clear an item and update the current `.2h` treasury atomically."""
        from dom6_assistant.file_reader.formats import h2 as H2

        block = self._block(commander_id)
        if slot not in EQUIPMENT_SLOTS:
            raise ValueError(
                f"unknown slot {slot!r}. Mapped: {sorted(EQUIPMENT_SLOTS)}")
        if not 0 <= item_id <= 1500:
            raise ValueError(f"implausible item id {item_id}")
        equipment_at = block.name_end + EQUIPMENT_SLOTS[slot]
        current = struct.unpack_from("<H", self.data, equipment_at)[0]
        if current == 0xFFFF:
            current = 0
        if current == item_id:
            return

        stash = H2.read_item_stash_slots(self.data)
        source = None
        if item_id:
            hits = [index for index, value in enumerate(stash) if value == item_id]
            if not hits:
                raise ValueError(
                    f"item {item_id} is not in the current unequipped treasury")
            source = hits[0]
            stash[source] = 0
        if current:
            empty = next((index for index, value in enumerate(stash) if value == 0), None)
            if empty is None:
                raise ValueError(
                    f"no empty treasury slot is available for removed item {current}")
            stash[empty] = current

        stash_at = H2.item_stash_offset(self.data)
        for index, value in enumerate(stash):
            struct.pack_into("<H", self.data, stash_at + index * 2, value)
        struct.pack_into("<H", self.data, equipment_at, item_id)

    def _check_length(self) -> None:
        """Refuse to hand back a file whose length has moved.

        Every edit here is in-place: the `.2h` is only ever spliced by the
        recruitment writer, which works on raw bytes rather than this editor.
        Assigning a differently-sized slice to a bytearray resizes it, which
        shifts every following byte and produces a file that is structurally
        wrong everywhere after the edit while still loading. That happened
        once, from writing 22 zeros over an 18-byte span, and was invisible
        until a block comparison caught it.
        """
        if len(self.data) != len(self._original):
            raise ValueError(
                f"edit changed the file length from {len(self._original)} to "
                f"{len(self.data)}; every edit here must be in place, and a "
                "length change means a slice assignment was mis-sized")

    def changed_bytes(self) -> list[int]:
        return [i for i in range(len(self.data)) if self.data[i] != self._original[i]]

    def save(self, path: str | Path | None = None, backup: bool = True) -> Path:
        """Write out, backing up first: a bad order fails silently, not loudly."""
        self._check_length()
        target = Path(path) if path else self.path
        if backup and target.exists():
            target.with_suffix(target.suffix + ".bak").write_bytes(target.read_bytes())
        target.write_bytes(bytes(self.data))
        return target


def verify_roundtrip(original: bytes, written: bytes,
                     expected_changes: set[int]) -> list[str]:
    """Check that a write changed exactly what it meant to, and nothing else.

    Returns a list of problems; empty means clean. This exists because the diffs
    that produced this format were only readable *because* single changes were
    isolated — the same discipline applies to writing.
    """
    problems: list[str] = []
    if len(original) != len(written):
        problems.append(f"length changed: {len(original)} -> {len(written)}")
        return problems
    actual = {i for i in range(len(original)) if original[i] != written[i]}
    if unexpected := actual - expected_changes:
        problems.append(f"unexpected bytes changed: {sorted(unexpected)[:16]}")
    if missing := expected_changes - actual:
        problems.append(f"expected changes not applied: {sorted(missing)[:16]}")
    return problems
