"""Turn stored intent into a `.2h` file.

The `.2h` is **derived state**. Every materialisation starts from the pristine
file the game wrote and applies the complete current intent, so the result is a
pure function of the intent rows: same rows in, same bytes out, every time.

Why not patch in place as decisions arrive? Three reasons, and it is worth being
precise about which are real today.

The stale-field argument is the obvious one and is **currently overstated**. It
was tested: ordering a move to 93 and then reconsidering to defend leaves no
stale destination, because `set_order` zeroes the destination for targetless
orders. Sequential in-place edits produce identical bytes. The hazard is real for
every field the editor does *not* yet own — formation, stance, target and the
placement grid all live in the same block, and a commander who stops leading a
squad has no code path today that clears them. It becomes real the moment the
tool surface grows, which it is about to.

The two reasons that hold right now:

* **Idempotency.** The same intent rows produce the same bytes, so a file can be
  discarded and rebuilt at any point without knowing what was done to it. That
  is what makes automating this safe.
* **Auditability.** Intent carries the reasoning that produced it. A file cannot,
  and reading orders back out of one tells you what was decided but never why.

**The pristine base.** The game rewrites the `.2h` whenever the player saves, so
the base is whatever the game last wrote for the current turn. It is copied on
first use and kept alongside the save, because once we have written to the file
the original is gone and there is no way to recover it from the modified copy.

**What is not written.** Only orders whose codes were established by experiment,
via OrdersEditor, which refuses anything else. The trailer is never touched: the
game ignores it on load, and inventing a value would be worse than leaving the
original honestly stale.

**The host accepts these files.** This was briefly recorded here as open, which
was wrong. An edited `.2h` — stale trailer left alone — was written setting
Estorgant to sneak to Kratas, the player reloaded to confirm the client showed
it, then ended that turn with no further changes. The order resolved: the
Troubadour's record sits at province 91 (Kratas), home 93 (Marignon), in both
the turn 2 and turn 3 files.

So both halves are confirmed, client and host, and the trailer genuinely does not
need recomputing. What is still worth doing is verifying each *individual* order
resolved as intended, since a wrong order does not fail loudly — it produces a
turn that quietly did something else. `materialize()` therefore reports what it
wrote rather than asserting success.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path

from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.reference import empowerment_cost as EC
from dom6_assistant.reference import leadership as LD
from dom6_assistant.reference import ritual_range as RTR
from dom6_assistant.reference import wish as WISH
from .orders_2h import (
    BUILDING_NAMES, CARRIED_GEM_PATHS, EQUIPMENT_SLOTS, FORMATION_NAMES,
    MAGIC_PATH_NAMES,
    ORDER_CODES,
    ORDER_SPECS, PARAM_BUILDING, PARAM_CURRENT_PROVINCE, PARAM_ITEM,
    PARAM_MAGIC_PATH, PARAM_NATION, PARAM_NONE, PARAM_PROVINCE,
    PARAM_PROVINCE_OR_ZERO,
    PLACEMENT_EDGE,
    RITUAL_ORDER_CODES, SINGLE_ROUND_CODES, SQUAD_SLOT_EMPTY, STANCE_CODES,
    TARGET_CODES_V2,
    WISH_RESULT_CODES,
    OrderTableNotLocated, OrdersEditor, find_order_blocks,
    normalize_order_parameter, read_carried_gems, read_equipment,
    read_empowerment_path, read_forge_fields, read_h2_units,
    read_ritual_fields,
)

BASE_SUFFIX = ".2h.pristine"
FORT_CONSTRUCTION_ORDERS = frozenset({"build_palisades", "upgrade_fortress"})
# Exact amounts the game's own saves reserve in the remaining-gold field.
# Temple is fixed by t3 (739 treasury - 600 temple = 139); Palisades by its
# binary definition/menu; Laboratory by the live calculator; Fortress by the
# controlled turn-25 save (6925 -> 6325).
CONSTRUCTION_GOLD_COSTS = {
    "build_laboratory": 600,
    "build_temple": 600,
    "build_palisades": 1000,
    "upgrade_fortress": 600,
}


@dataclass
class MaterializeResult:
    """What a materialisation did, including what it refused to do."""
    path: Path | None = None
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    changed_bytes: int = 0
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return self.path is not None and not self.skipped

    def __str__(self) -> str:
        head = f"{len(self.written)} order(s) -> {self.path}"
        if self.skipped:
            head += f"; {len(self.skipped)} SKIPPED"
        return head


@dataclass(frozen=True)
class SubmissionResult:
    """The exact file mutation made by the local End Turn operation."""
    path: Path
    changed_offsets: tuple[int, ...]
    already_submitted: bool
    h2_sha256: str


#: SHA-256 of the last file materialisation wrote, stored beside the base.
#: Without it there is no way to tell our own output from a file the player
#: saved over it, and those need opposite treatment: ours is derived state to
#: be rebuilt, theirs is orders to be preserved.
WRITTEN_SUFFIX = ".2h.pristine.written"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_written(h2_path: Path, data: bytes) -> None:
    h2_path.with_suffix(WRITTEN_SUFFIX).write_text(_digest(data))


def _last_written(h2_path: Path) -> str | None:
    marker = h2_path.with_suffix(WRITTEN_SUFFIX)
    if not marker.exists():
        return None
    try:
        return marker.read_text().strip()
    except OSError:
        return None


def matches_last_written(h2_path: str | Path) -> bool:
    """Whether the live orders file is exactly our last materialisation."""
    path = Path(h2_path)
    if not path.exists():
        return False
    return _digest(path.read_bytes()) == _last_written(path)


def submit_turn_file(h2_path: str | Path) -> SubmissionResult:
    """Mark a materialized local `.2h` finished without touching its trailer.

    Readiness and decision-fingerprint policy belongs to the agent tool.  This
    function owns the file transaction and updates the materialization marker
    so the submitted bytes remain recognizably ours rather than looking like a
    player save that should become a new pristine base.
    """
    path = Path(h2_path)
    original = path.read_bytes()
    submitted = H2.mark_turn_submitted(original)
    offsets = H2.submission_flag_offsets(original)
    changed = tuple(offset for offset in offsets
                    if original[offset] != submitted[offset])
    if submitted != original:
        path.with_suffix(path.suffix + ".bak").write_bytes(original)
        path.write_bytes(submitted)
        _record_written(path, submitted)
    return SubmissionResult(
        path=path,
        changed_offsets=changed,
        already_submitted=not changed,
        h2_sha256=_digest(submitted),
    )


def base_is_stale(h2_path: Path) -> str | None:
    """Why the stored base no longer describes the current file, or None.

    Two independent reasons, and the second is the one that bites:

    * the turn advanced, so the base describes armies, research and a treasury
      that no longer exist;
    * the live file is neither the base nor the last thing we wrote, which
      means **the player saved their own orders over it**. Rebuilding from the
      old base then silently discards whatever they did — and the result is a
      perfectly consistent file, so nothing downstream can notice.
    """
    base = h2_path.with_suffix(BASE_SUFFIX)
    if not base.exists():
        return "no base has been captured for this file yet"
    current_data = h2_path.read_bytes()
    base_data = base.read_bytes()
    # Both .trn and .2h headers store the turn as u32 at 14. A pristine file is
    # meaningful only within its turn: the live game can advance several turns
    # without the assistant materialising anything.
    current_turn = (struct.unpack_from("<I", current_data, 14)[0]
                    if len(current_data) >= 18 else None)
    base_turn = (struct.unpack_from("<I", base_data, 14)[0]
                 if len(base_data) >= 18 else None)
    if current_turn != base_turn:
        return f"the turn advanced from {base_turn} to {current_turn}"
    current = _digest(current_data)
    if current == _digest(base_data):
        return None
    if current == _last_written(h2_path):
        return None
    return ("the orders file has been written by something other than this "
            "assistant since the base was captured — most likely the player "
            "saving in game")


def pristine_base(h2_path: Path) -> Path:
    """The untouched `.2h` for this turn, re-captured whenever it goes stale.

    Once we write to the live file the original is unrecoverable, so the copy
    has to happen before the first write. It must NOT happen again for a file
    we wrote ourselves — that would "preserve" our own output as the base and
    bake in the staleness this design exists to prevent — but it must happen
    when anyone else writes, which is what `base_is_stale` separates.
    """
    base = h2_path.with_suffix(BASE_SUFFIX)
    if base_is_stale(h2_path) is not None:
        shutil.copy2(h2_path, base)
        h2_path.with_suffix(WRITTEN_SUFFIX).unlink(missing_ok=True)
    return base


def reset_base(h2_path: Path) -> None:
    """Declare the current `.2h` fresh: drop the base and our write marker.

    The explicit form of what `base_is_stale` detects automatically. Detection
    covers the ordinary cases; this covers the ones it cannot see — a file
    restored from a backup, a save copied in from elsewhere, or simply an
    operator who knows the file is authoritative and wants to say so.
    """
    h2_path.with_suffix(BASE_SUFFIX).unlink(missing_ok=True)
    h2_path.with_suffix(WRITTEN_SUFFIX).unlink(missing_ok=True)


def current_intent(conn: sqlite3.Connection, game_id: int,
                   turn: int) -> list[sqlite3.Row]:
    """The orders that should be in the file: latest row per commander."""
    conn.row_factory = sqlite3.Row
    return list(conn.execute(
        "SELECT * FROM current_orders WHERE game_id=? AND turn=? "
        "ORDER BY commander_id", (game_id, turn)))


def materialize(conn: sqlite3.Connection, game_id: int, turn: int,
                h2_path: str | Path, *, dry_run: bool = False,
                out_path: str | Path | None = None,
                reference_conn: sqlite3.Connection | None = None,
                ) -> MaterializeResult:
    """Rebuild the `.2h` from stored intent.

    `dry_run` reports what would happen without writing, which is the mode any
    caller should reach for first: a wrong order does not fail loudly, it
    produces a turn that quietly does the wrong thing.
    """
    h2_path = Path(h2_path)
    result = MaterializeResult()
    rows = current_intent(conn, game_id, turn)
    battle = _rows(conn, "current_battle_intent", game_id, turn)
    positions = _rows(conn, "current_battle_position_intent", game_id, turn)
    carried = _rows(conn, "current_carried_gem_intent", game_id, turn)
    scripts = _rows(conn, "current_battle_script_intent", game_id, turn)
    troops = _rows(conn, "current_troop_assignment_intent", game_id, turn,
                   order_by="unit_instance_id")
    squad_creations = _rows(
        conn, "current_squad_creation_intent", game_id, turn,
        order_by="target_commander_id, target_squad")
    equipment = _rows(conn, "current_equipment_intent", game_id, turn)
    shape_changes = _rows(
        conn, "current_shape_change_intent", game_id, turn)
    recruits = _rows(conn, "recruit_intent_v2", game_id, turn,
                     order_by="province_id, position")
    research = _rows(conn, "current_research_intent", game_id, turn,
                     order_by="id")
    defence = _rows(conn, "current_province_defence_intent", game_id, turn,
                    order_by="province_id")
    bids = (_rows(conn, "current_mercenary_bid_intent", game_id, turn,
                  order_by="slot")
            if _has_table(conn, "mercenary_bid_intent") else [])
    diplomacy = (_rows(conn, "current_diplomacy_intent", game_id, turn,
                       order_by="target_nation_id")
                 if _has_table(conn, "diplomacy_intent") else [])
    if not (rows or battle or positions or carried or scripts or troops
            or squad_creations
            or equipment or shape_changes or recruits or research or defence or bids
            or diplomacy):
        return result

    base = pristine_base(h2_path)
    editor = OrdersEditor(base)
    original = base.read_bytes()
    before_orders = find_order_blocks(original)
    # Equipment changes alter both worn slots and the shared item treasury.
    # Apply them before ritual payload reservation so an item unequipped and
    # sent in the same planning phase behaves exactly as it does in the client.
    _apply_equipment(conn, editor, game_id, turn, result)
    from dom6_assistant.file_reader.formats import h2
    try:
        remaining_gems: list[int] | None = list(h2.gem_remaining(original))
    except h2.QueueWriteRefused:
        remaining_gems = None
    fort_context = _fort_context(h2_path, base, rows)
    # Dispel resolves its target's chain slot here rather than at record time,
    # so the enchantment chain is read from the turn file as it stands now.
    global_chain_data: bytes | None = None
    has_ritual_intent = any(
        r["order_name"] in RITUAL_ORDER_CODES for r in rows)
    ritual_locations: dict[int, int] = {}
    ritual_commander_types: dict[int, int] = {}
    ritual_commander_items: dict[int, tuple[int, ...]] = {}
    ritual_nation_id: int | None = None
    ritual_provinces = None
    if has_ritual_intent:
        chain_trn = h2_path.with_suffix(".trn")
        if chain_trn.exists():
            global_chain_data = chain_trn.read_bytes()
            try:
                from dom6_assistant.file_reader.formats import trn as T

                parsed_ritual_turn = T.parse(chain_trn)
                ritual_provinces = parsed_ritual_turn.provinces
                ritual_nation_id = parsed_ritual_turn.nation_id
                ritual_locations = _commander_locations(h2_path, base)
                ritual_commander_types = _commander_types(h2_path, base)
                ritual_commander_items = _commander_items_from_data(editor.data)
            except (OSError, ValueError):
                ritual_provinces = None
    commander_paths = (_commander_paths(h2_path, base)
                       if any(r["order_name"] == "empowerment"
                              or read_empowerment_path(
                                  original,
                                  before_orders[r["commander_id"]].name_end)
                                 is not None
                              for r in rows
                              if r["commander_id"] in before_orders)
                       else {})
    fort_writes: list[tuple[int, int]] = []
    game = conn.execute(
        "SELECT nation_id FROM games WHERE id=?", (game_id,)).fetchone()
    nation_id = (
        int(game[0])
        if game is not None and game[0] is not None
        else ritual_nation_id
    )

    for row in rows:
        name = row["commander_name"] or f"commander {row['commander_id']}"
        old_block = before_orders.get(row["commander_id"])
        old_ritual = (read_ritual_fields(original, old_block.name_end)
                      if old_block is not None else None)
        old_forge = (read_forge_fields(original, old_block.name_end)
                     if old_block is not None else None)
        is_ritual = row["order_name"] in RITUAL_ORDER_CODES
        is_forge = row["order_name"] == "forge_magic_item"
        is_empower = row["order_name"] == "empowerment"
        forge_detail = None
        empower_detail = None

        # The national pool stores gems REMAINING after orders.  Work from the
        # pristine value and apply a per-commander delta: replacing a ritual
        # first refunds the old reservation, then charges the new one.  That
        # preserves forging, carried gems, and rituals belonging to commanders
        # this intent set does not touch.
        candidate_gems = (list(remaining_gems)
                          if remaining_gems is not None else None)
        ritual_detail = None
        # A forge reserves gems in the same pool a ritual does, so replacing
        # either has to refund the old reservation before charging the new
        # one. The old forge's cost is only recorded in the file itself.
        if old_forge is not None or is_forge:
            if candidate_gems is None:
                result.skipped.append(
                    f"{name}: forge gem treasury could not be located")
                continue
            if reference_conn is None:
                result.skipped.append(
                    f"{name}: forge write needs the reference item database")
                continue
            if old_forge is not None:
                old_paths = _forge_paths(reference_conn, old_forge.item_id)
                if old_paths is None:
                    result.skipped.append(
                        f"{name}: magic paths for the forge already in the file "
                        f"(item {old_forge.item_id}) is unknown, so its "
                        "reservation cannot be refunded")
                    continue
                if len(old_paths) == 2 and old_forge.secondary_gem_cost <= 0:
                    result.skipped.append(
                        f"{name}: multi-path forge item {old_forge.item_id} has "
                        "no secondary reservation at +124, so it cannot be "
                        "refunded safely")
                    continue
                if len(old_paths) == 1 and old_forge.secondary_gem_cost:
                    result.skipped.append(
                        f"{name}: single-path forge item {old_forge.item_id} "
                        "unexpectedly has a secondary reservation")
                    continue
                candidate_gems[old_paths[0]] += old_forge.gem_cost
                if len(old_paths) == 2:
                    candidate_gems[old_paths[1]] += old_forge.secondary_gem_cost
            if is_forge:
                forge_detail = conn.execute(
                    "SELECT * FROM forge_intent WHERE order_intent_id=?",
                    (row["id"],)).fetchone()
                if forge_detail is None:
                    result.skipped.append(
                        f"{name}: forge order has no forge_intent details")
                    continue
                expected_paths = _forge_paths(
                    reference_conn, int(forge_detail["item_id"]))
                secondary_path = forge_detail["secondary_gem_path"]
                paths_and_costs = [
                    (int(forge_detail["gem_path"]),
                     int(forge_detail["gem_cost"]))
                ]
                if secondary_path is not None:
                    paths_and_costs.append(
                        (int(secondary_path),
                         int(forge_detail["secondary_gem_cost"])))
                if expected_paths is None or tuple(
                        path for path, _cost in paths_and_costs) != expected_paths:
                    result.skipped.append(
                        f"{name}: recorded forge paths "
                        f"{[path for path, _cost in paths_and_costs]} do not "
                        f"match item {forge_detail['item_id']}'s reference paths "
                        f"{list(expected_paths) if expected_paths else None}")
                    continue
                forge_unaffordable = False
                for path, cost in paths_and_costs:
                    if (not 0 <= path < len(candidate_gems) or cost <= 0):
                        result.skipped.append(
                            f"{name}: invalid forge reservation {cost} on path {path}")
                        forge_unaffordable = True
                        break
                    if candidate_gems[path] < cost:
                        result.skipped.append(
                            f"{name}: forging {forge_detail['item_name']} needs "
                            f"{cost} {h2.GEM_PATHS[path]} gems but only "
                            f"{candidate_gems[path]} remain")
                        forge_unaffordable = True
                        break
                if forge_unaffordable:
                    continue
                for path, cost in paths_and_costs:
                    candidate_gems[path] -= cost
        old_empower_path = (read_empowerment_path(original, old_block.name_end)
                            if old_block is not None else None)
        if old_empower_path is not None and candidate_gems is not None:
            # No cost is stored, so recompute what it must have reserved from
            # the commander's current level in that path.
            paths = commander_paths.get(row["commander_id"], {})
            # Path names are not initials: Astral's wire/commander letter is
            # S, while A means Air.  Keep the file's FAWESDNGB ordering
            # explicit so every refund queries the level it actually bought.
            letter = "FAWESDNGB"[old_empower_path]
            current = int(paths.get(letter, 0))
            try:
                refund = EC.empowerment_cost(current + 1)
            except EC.EmpowermentCostUnknown as exc:
                result.skipped.append(
                    f"{name}: an empowerment already in the file cannot be "
                    f"priced for refund — {exc}")
                continue
            candidate_gems[old_empower_path] += refund
        if is_empower:
            if candidate_gems is None:
                result.skipped.append(
                    f"{name}: empowerment gem treasury could not be located")
                continue
            empower_detail = conn.execute(
                "SELECT * FROM empowerment_intent WHERE order_intent_id=?",
                (row["id"],)).fetchone()
            if empower_detail is None:
                result.skipped.append(
                    f"{name}: empowerment order has no empowerment_intent "
                    "details")
                continue
            path = int(empower_detail["gem_path"])
            cost = int(empower_detail["gem_cost"])
            if candidate_gems[path] < cost:
                result.skipped.append(
                    f"{name}: empowerment needs {cost} {h2.GEM_PATHS[path]} "
                    f"gems but only {candidate_gems[path]} remain")
                continue
            candidate_gems[path] -= cost
        if old_ritual is not None or is_ritual:
            if candidate_gems is None:
                result.skipped.append(
                    f"{name}: ritual gem treasury could not be located")
                continue
            if reference_conn is None:
                result.skipped.append(
                    f"{name}: ritual write needs the reference spell database")
                continue
            if old_ritual is not None:
                old_path = _spell_primary_path(reference_conn,
                                               old_ritual.spell_id)
                if old_path is None:
                    result.skipped.append(
                        f"{name}: primary path for existing ritual spell "
                        f"{old_ritual.spell_id} is unknown")
                    continue
                candidate_gems[old_path] += old_ritual.gem_cost
            if is_ritual:
                ritual_detail = conn.execute(
                    "SELECT * FROM ritual_intent WHERE order_intent_id=?",
                    (row["id"],)).fetchone()
                if ritual_detail is None:
                    result.skipped.append(
                        f"{name}: ritual order has no ritual_intent details")
                    continue
                spell_id = int(ritual_detail["spell_id"])
                if RTR.spell_province_range(reference_conn, spell_id) is not None:
                    if ritual_provinces is None:
                        result.skipped.append(
                            f"{name}: ranged ritual target cannot be "
                            "revalidated because the current province graph "
                            "is unavailable")
                        continue
                    try:
                        RTR.validate_ritual_target(
                            reference_conn, spell_id, ritual_provinces,
                            ritual_locations.get(row["commander_id"]),
                            ritual_detail["target_province"], nation_id)
                    except ValueError as exc:
                        result.skipped.append(
                            f"{name}: {ritual_detail['spell_name']} — {exc}")
                        continue
                if RTR.spell_source_requirements(reference_conn, spell_id):
                    if ritual_provinces is None:
                        result.skipped.append(
                            f"{name}: ritual source restrictions cannot be "
                            "revalidated because the current province graph "
                            "is unavailable")
                        continue
                    try:
                        RTR.validate_ritual_source(
                            reference_conn, spell_id, ritual_provinces,
                            ritual_locations.get(row["commander_id"]),
                            ritual_commander_types.get(row["commander_id"]),
                            ritual_commander_items.get(
                                row["commander_id"], ()))
                    except ValueError as exc:
                        result.skipped.append(
                            f"{name}: {ritual_detail['spell_name']} — {exc}")
                        continue
                path = int(ritual_detail["gem_path"])
                cost = int(ritual_detail["gem_cost"])
                if not 0 <= path < len(candidate_gems):
                    result.skipped.append(
                        f"{name}: invalid ritual gem path {path}")
                    continue
                if candidate_gems[path] < cost:
                    result.skipped.append(
                        f"{name}: ritual needs {cost} "
                        f"{h2.GEM_PATHS[path]} gems but only "
                        f"{candidate_gems[path]} remain")
                    continue
                candidate_gems[path] -= cost

        if row["order_name"] in FORT_CONSTRUCTION_ORDERS:
            if isinstance(fort_context, str):
                result.skipped.append(
                    f"{name}: {row['order_name']} — {fort_context}")
                continue
            owned, locations = fort_context
            province_id = locations.get(row["commander_id"])
            if province_id is None:
                result.skipped.append(
                    f"{name}: {row['order_name']} — commander location is "
                    "unknown, so the required province construction selector "
                    "cannot be written")
                continue
            fort_writes.append((province_id, row["destination"]))
        try:
            # Item transport reserves its payload by zeroing the treasury slot.
            # Replacing or cancelling the order must restore that item before
            # any new item/equipment intent is applied.
            if old_ritual is not None and old_ritual.target_item_id is not None:
                editor.refund_ritual_item(old_ritual.target_item_id)
            if is_empower:
                assert empower_detail is not None
                editor.set_empowerment(row["commander_id"],
                                       int(empower_detail["gem_path"]))
            elif is_forge:
                assert forge_detail is not None
                editor.set_forge(row["commander_id"],
                                 int(forge_detail["item_id"]),
                                 int(forge_detail["gem_cost"]),
                                 int(forge_detail["secondary_gem_cost"]))
            elif is_ritual:
                assert ritual_detail is not None
                target_item = ritual_detail["target_item_id"]
                if target_item is not None:
                    editor.reserve_ritual_item(int(target_item))
                slot = ritual_detail["target_province"]
                if ritual_detail["target_global_effect_id"] is not None:
                    # Active-global rituals write the target's chain slot into
                    # the same field a province ritual uses. Re-resolve the
                    # stable effect identity now because a vacated slot can be
                    # reused by an unrelated cast after intent was recorded.
                    from dom6_assistant.file_reader.formats import trn as T

                    wanted = int(ritual_detail["target_global_effect_id"])
                    if global_chain_data is None:
                        result.skipped.append(
                            f"{name}: no .trn beside the .2h, so the global "
                            "target's chain slot cannot be resolved")
                        continue
                    active = T.read_global_effects(global_chain_data)
                    match = [row for row in active if row.effect_id == wanted]
                    if not match:
                        result.skipped.append(
                            f"{name}: global enchantment {wanted} is no longer "
                            "active, so the ritual has no target")
                        continue
                    slot = match[0].slot
                editor.set_ritual(
                    row["commander_id"], int(ritual_detail["spell_id"]),
                    int(ritual_detail["gem_cost"]),
                    slot,
                    target_commander_runtime_index=(
                        _ritual_recipient_runtime_index(
                            editor, ritual_detail["target_commander_id"])),
                    target_unit_runtime_index=_ritual_unit_runtime_index(
                        editor, nation_id,
                        ritual_detail["target_unit_instance_id"]),
                    target_item_id=target_item,
                    wish_result=ritual_detail["wish_result"],
                    wish_item_id=ritual_detail["wish_item_id"],
                    wish_unit_id=ritual_detail["wish_unit_id"],
                    wish_nation_id=ritual_detail["wish_nation_id"],
                    monthly=bool(ritual_detail["monthly"]))
            else:
                editor.set_order(row["commander_id"], row["order_name"],
                                 parameter=row["destination"])
        except (ValueError, OrderTableNotLocated) as exc:
            # Refused rather than approximated. An order we cannot write
            # correctly must not be written at all — a half-applied order set is
            # worse than none, because the turn still resolves.
            result.skipped.append(f"{name}: {row['order_name']} — {exc}")
            continue
        if candidate_gems is not None and (old_ritual is not None or is_ritual
                                           or old_forge is not None
                                           or is_forge or is_empower
                                           or old_empower_path is not None):
            remaining_gems = candidate_gems
        if is_empower:
            assert empower_detail is not None
            path_name = h2.GEM_PATHS[int(empower_detail["gem_path"])]
            result.written.append(
                f"{name}: empower to {path_name} "
                f"{empower_detail['target_level']} for "
                f"{empower_detail['gem_cost']} {path_name} gems")
            continue
        if is_forge:
            assert forge_detail is not None
            costs = [
                f"{forge_detail['gem_cost']} "
                f"{h2.GEM_PATHS[int(forge_detail['gem_path'])]}"
            ]
            if forge_detail["secondary_gem_path"] is not None:
                costs.append(
                    f"{forge_detail['secondary_gem_cost']} "
                    f"{h2.GEM_PATHS[int(forge_detail['secondary_gem_path'])]}")
            result.written.append(
                f"{name}: forge {forge_detail['item_name']} "
                f"({forge_detail['item_id']}) for {' + '.join(costs)} gems")
            continue
        if is_ritual:
            target = ritual_detail["target_province"]
            repeat = " monthly" if ritual_detail["monthly"] else " once"
            where = f" -> province {target}" if target is not None else ""
            result.written.append(
                f"{name}: {ritual_detail['spell_name']} ({ritual_detail['spell_id']})"
                f"{where}{repeat}"
                + (f" carrying item {ritual_detail['target_item_id']}"
                   if ritual_detail["target_item_id"] is not None else "")
                + (f" wishing for item {ritual_detail['wish_item_id']}"
                   if ritual_detail["wish_item_id"] is not None else "")
                + (f" wishing for unit {ritual_detail['wish_unit_id']}"
                   if ritual_detail["wish_unit_id"] is not None else "")
                + (f" targeting nation {ritual_detail['wish_nation_id']}"
                   if ritual_detail["wish_nation_id"] is not None else "")
                + (f" wishing for {ritual_detail['wish_result']}"
                   if ritual_detail["wish_result"] is not None
                   and ritual_detail["wish_item_id"] is None else ""))
            continue
        parameter = normalize_order_parameter(row["order_name"],
                                              row["destination"])
        suffix = _parameter_suffix(row["order_name"], parameter)
        result.written.append(f"{name}: {row['order_name']}{suffix}")

    troop_locations = (_commander_locations(h2_path, base)
                       if squad_creations else {})
    _apply_troop_assignments(
        conn, editor, game_id, turn, result,
        commander_provinces=troop_locations)
    has_formations = any(row["formation"] is not None for row in battle)
    _apply_battle(
        conn, editor, game_id, turn, result,
        reference_conn=reference_conn,
        commander_types=(
            _commander_types(h2_path, base) if has_formations else {}),
        commander_experience=(
            _commander_experience(h2_path, base) if has_formations else {}),
    )
    _apply_battle_positions(conn, editor, game_id, turn, result)
    _apply_battle_scripts(conn, editor, game_id, turn, result)
    _apply_shape_changes(
        conn, editor, game_id, turn, h2_path, result,
        reference_conn=reference_conn)
    remaining_gems = _apply_carried_gems(
        conn, editor, original, remaining_gems, game_id, turn, result)

    data = bytes(editor.data)
    if remaining_gems is not None:
        try:
            data = h2.set_gem_remaining(data, remaining_gems)
        except h2.QueueWriteRefused as exc:
            result.skipped.append(f"gem commitments — {exc}")
    if fort_writes:
        from dom6_assistant.file_reader.formats import h2
        assert not isinstance(fort_context, str)
        owned, _locations = fort_context
        for province_id, fort_definition in fort_writes:
            try:
                data = h2.set_fort_construction(
                    data, province_id, fort_definition, owned)
            except h2.QueueWriteRefused as exc:
                result.skipped.append(
                    f"fort construction in {province_id} — {exc}")
    data, defence_gold_delta = _apply_province_defence(
        conn, original, data, game_id, turn, h2_path, result)
    data, bid_gold_delta = _apply_mercenary_bids(
        conn, original, data, game_id, turn, h2_path, result,
        reference_conn=reference_conn)
    reserved_gold_delta = defence_gold_delta + bid_gold_delta
    data = _apply_research(conn, data, game_id, turn, h2_path, result)
    # Recruitment is applied to the bytes rather than through OrdersEditor,
    # because it SPLICES: the file grows four bytes per unit, so an editor
    # holding fixed block offsets cannot express it.
    data = _apply_recruitment(
        conn, data, game_id, turn, h2_path, result,
        reference_conn=reference_conn)
    data = _apply_diplomacy(conn, original, data, game_id, turn, h2_path, result)
    data = _apply_gold_commitments(
        original, data, rows, recruits, reserved_gold_delta, result)
    # One intent set is one transaction.  Every helper above works on local
    # bytes, so a late failure (most importantly combined gold/gem overspend)
    # must discard *all* earlier successful mutations.  Writing the affordable
    # subset while silently dropping the rest is never a valid turn plan.
    if result.skipped:
        result.aborted = True
        result.written.clear()
        return result

    overlap = min(len(original), len(data))
    result.changed_bytes = (
        sum(original[i] != data[i] for i in range(overlap))
        + abs(len(original) - len(data)))
    if not dry_run:
        target = Path(out_path) if out_path else h2_path
        if target.exists():
            target.with_suffix(target.suffix + ".bak").write_bytes(
                target.read_bytes())
        target.write_bytes(data)
        result.path = target
        # Remember our own output so the next call can tell it apart from the
        # player saving over it. Only meaningful when we wrote the live file;
        # an out_path is a throwaway copy and its base belongs to h2_path.
        if out_path is None:
            _record_written(h2_path, data)
    return result


def _fort_context(h2_path: Path, base: Path, rows: list[sqlite3.Row]):
    """Owned provinces and known commander locations for fort side effects."""
    if not any(row["order_name"] in FORT_CONSTRUCTION_ORDERS for row in rows):
        return ([], {})
    trn_path = h2_path.with_suffix(".trn")
    if not trn_path.exists():
        return "no .trn beside the .2h"
    try:
        from dom6_assistant.agent.visibility import PlayerView
        from dom6_assistant.file_reader.formats import trn as T
        parsed = T.parse(trn_path)
        view = PlayerView(h2_path.parent, parsed.nation_id,
                          trn_name=trn_path.name)
        locations = {
            commander.commander_id: commander.province_id
            for commander in view.own_commanders(base)
            if commander.province_id is not None
        }
        owned = sorted(
            province.province_id for province in parsed.provinces
            if province.owner_nation_id == parsed.nation_id)
        return owned, locations
    except (OSError, ValueError) as exc:
        return f"could not derive fort-construction province: {exc}"


def _commander_paths(h2_path: Path, base: Path) -> dict[int, dict[str, int]]:
    """Own commanders' magic paths, for pricing an empowerment refund."""
    trn_path = h2_path.with_suffix(".trn")
    if not trn_path.exists():
        return {}
    try:
        from dom6_assistant.agent.visibility import PlayerView
        from dom6_assistant.file_reader.formats import trn as T
        parsed = T.parse(trn_path)
        view = PlayerView(h2_path.parent, parsed.nation_id,
                          trn_name=trn_path.name)
        return {c.commander_id: dict(c.paths) for c in view.own_commanders(base)}
    except (OSError, ValueError):
        return {}


def _commander_locations(h2_path: Path, base: Path) -> dict[int, int]:
    """Exact own-commander locations for writes with province side effects."""
    trn_path = h2_path.with_suffix(".trn")
    if not trn_path.exists():
        return {}
    try:
        from dom6_assistant.agent.visibility import PlayerView
        from dom6_assistant.file_reader.formats import trn as T
        parsed = T.parse(trn_path)
        view = PlayerView(h2_path.parent, parsed.nation_id,
                          trn_name=trn_path.name)
        return {
            commander.commander_id: commander.province_id
            for commander in view.own_commanders(base)
            if commander.province_id is not None
        }
    except (OSError, ValueError):
        return {}


def _commander_types(h2_path: Path, base: Path) -> dict[int, int]:
    """Exact own-commander chassis ids for ritual caster restrictions."""
    trn_path = h2_path.with_suffix(".trn")
    if not trn_path.exists():
        return {}
    try:
        from dom6_assistant.agent.visibility import PlayerView
        from dom6_assistant.file_reader.formats import trn as T
        parsed = T.parse(trn_path)
        view = PlayerView(h2_path.parent, parsed.nation_id,
                          trn_name=trn_path.name)
        return {
            commander.commander_id: commander.type_id
            for commander in view.own_commanders(base)
            if commander.type_id is not None
        }
    except (OSError, ValueError):
        return {}


def _commander_experience(h2_path: Path, base: Path) -> dict[int, int]:
    """Own commander raw XP for final-leadership formation checks."""
    trn_path = h2_path.with_suffix(".trn")
    if not trn_path.exists():
        return {}
    try:
        from dom6_assistant.agent.visibility import PlayerView
        from dom6_assistant.file_reader.formats import trn as T
        parsed = T.parse(trn_path)
        view = PlayerView(h2_path.parent, parsed.nation_id,
                          trn_name=trn_path.name)
        by_instance = {unit.instance_id: unit for unit in view.own_units()}
        return {
            commander.commander_id: int(
                by_instance[commander.unit_instance_id].experience or 0)
            for commander in view.own_commanders(base)
            if commander.unit_instance_id in by_instance
        }
    except (OSError, ValueError):
        return {}


def _commander_items(base: Path) -> dict[int, tuple[int, ...]]:
    """Currently worn item ids for ritual caster-trait restrictions."""
    try:
        data = base.read_bytes()
        return {
            commander_id: tuple(
                read_equipment(data, block.name_end).values())
            for commander_id, block in find_order_blocks(data).items()
        }
    except (OSError, ValueError, OrderTableNotLocated):
        return {}


def _commander_items_from_data(
    data: bytes | bytearray,
) -> dict[int, tuple[int, ...]]:
    """Worn item ids from an in-progress materialization."""
    try:
        return {
            commander_id: tuple(read_equipment(data, block.name_end).values())
            for commander_id, block in find_order_blocks(data).items()
        }
    except (ValueError, OrderTableNotLocated):
        return {}


def _apply_shape_changes(
        conn: sqlite3.Connection, editor: OrdersEditor,
        game_id: int, turn: int, h2_path: Path,
        result: MaterializeResult, *,
        reference_conn: sqlite3.Connection | None) -> None:
    """Apply instantaneous form state without touching strategic orders."""
    rows = _rows(conn, "current_shape_change_intent", game_id, turn)
    if not rows:
        return
    if reference_conn is None:
        result.skipped.append(
            "Change Shape: reference unit database is required to revalidate "
            "the form relationship")
        return
    trn_path = h2_path.with_suffix(".trn")
    if not trn_path.exists():
        result.skipped.append(
            "Change Shape: no .trn beside the .2h, so nation ownership "
            "cannot be verified")
        return
    from dom6_assistant.file_reader.formats import trn as T
    nation_id = T.parse(trn_path).nation_id
    if nation_id is None:
        result.skipped.append(
            "Change Shape: player nation id is unavailable")
        return
    for row in rows:
        name = row["commander_name"] or f"commander {row['commander_id']}"
        source = reference_conn.execute(
            "SELECT name, shapechange FROM units WHERE id=?",
            (row["source_type_id"],),
        ).fetchone()
        target = reference_conn.execute(
            "SELECT name FROM units WHERE id=?",
            (row["target_type_id"],),
        ).fetchone()
        if source is None or target is None:
            result.skipped.append(
                f"{name}: Change Shape references an unknown source or target "
                "unit type")
            continue
        if int(source["shapechange"] or 0) != int(row["target_type_id"]):
            result.skipped.append(
                f"{name}: type {row['source_type_id']} no longer has verified "
                f"Change Shape target {row['target_type_id']}")
            continue
        try:
            changed = editor.set_shape(
                int(row["commander_id"]), int(nation_id),
                int(row["target_type_id"]), int(row["target_hp"]),
                source_type_id=int(row["source_type_id"]),
                source_hp=int(row["source_hp"]))
        except (ValueError, OrderTableNotLocated) as exc:
            result.skipped.append(f"{name}: Change Shape — {exc}")
            continue
        state = ("changed from pristine base" if changed else
                 "requested form restored from pristine base")
        result.written.append(
            f"{name}: Change Shape to {target['name']} "
            f"(type {row['target_type_id']}, {row['target_hp']} HP; {state})")


def _apply_province_defence(
        conn: sqlite3.Connection, original: bytes, data: bytes,
        game_id: int, turn: int, h2_path: Path,
        result: MaterializeResult) -> tuple[bytes, int]:
    """Write complete owned-province PD targets and return their gold delta."""
    from dom6_assistant.agent.visibility import PlayerView, VisibilityError
    from dom6_assistant.file_reader.formats import h2, trn as T

    rows = _rows(conn, "current_province_defence_intent", game_id, turn,
                 order_by="province_id")
    if not rows:
        return data, 0
    trn_path = h2_path.with_suffix(".trn")
    if not trn_path.exists():
        result.skipped.append(
            "province defence: no .trn beside the .2h, so ownership and the "
            "turn-start minimum cannot be verified")
        return data, 0

    parsed = T.parse(trn_path)
    starts = {
        province.province_id: province.province_defense
        for province in parsed.provinces
        if province.owner_nation_id == parsed.nation_id
    }
    owned = sorted(starts)
    try:
        view = PlayerView(h2_path.parent, parsed.nation_id,
                          trn_name=trn_path.name)
        order_file_provinces = list(
            view.order_file_province_ids(original))
    except (OSError, ValueError, VisibilityError) as exc:
        result.skipped.append(
            f"province defence: cannot attribute province-local .2h blocks "
            f"safely ({exc})")
        return data, 0
    delta = 0
    for row in rows:
        province_id = int(row["province_id"])
        target = int(row["target"])
        if province_id not in starts:
            result.skipped.append(
                f"province defence in {province_id}: province is not ours; "
                f"ours are {owned}")
            continue
        if province_id not in order_file_provinces:
            result.skipped.append(
                f"province defence in {province_id}: newly conquered province "
                "has no writable block in the inherited .2h")
            continue
        turn_start = int(starts[province_id])
        if target < turn_start:
            result.skipped.append(
                f"province defence in {province_id}: target {target} is below "
                f"the turn-start level {turn_start}; only this turn's purchases "
                "can be refunded")
            continue
        try:
            old_target = h2.province_defence(
                original, province_id, order_file_provinces)
            candidate = h2.set_province_defence(
                data, province_id, target, order_file_provinces)
        except h2.QueueWriteRefused as exc:
            result.skipped.append(
                f"province defence in {province_id} — {exc}")
            continue
        delta += (h2.province_defence_cost(target)
                  - h2.province_defence_cost(old_target))
        data = candidate
        change = target - turn_start
        committed = (h2.province_defence_cost(target)
                     - h2.province_defence_cost(turn_start))
        result.written.append(
            f"province {province_id}: defence {turn_start} -> {target} "
            f"({change:+d} point(s), {committed} gold committed)")
    return data, delta


def _apply_mercenary_bids(
        conn: sqlite3.Connection, original: bytes, data: bytes,
        game_id: int, turn: int, h2_path: Path,
        result: MaterializeResult, *,
        reference_conn: sqlite3.Connection | None = None) -> tuple[bytes, int]:
    """Write standing mercenary bids and return their gold delta.

    Works by delta from the pristine file, as rituals and province defence do:
    a replaced bid refunds its old reservation before the new one is charged,
    so editing a bid cannot double-charge and a bid the assistant did not
    touch is preserved.
    """
    from dom6_assistant.file_reader.formats import h2, trn as T
    from dom6_assistant.reference import mercenary_cost as MC

    if not _has_table(conn, "mercenary_bid_intent"):
        return data, 0
    rows = _rows(conn, "current_mercenary_bid_intent", game_id, turn,
                 order_by="slot")
    if not rows:
        return data, 0
    trn_path = h2_path.with_suffix(".trn")
    if not trn_path.exists():
        result.skipped.append(
            "mercenary bids: no .trn beside the .2h, so the auction cannot be "
            "verified")
        return data, 0

    trn_data = trn_path.read_bytes()
    parsed_turn = T.parse(trn_path)
    companies = T.read_mercenaries(trn_data)
    nation_id = parsed_turn.nation_id
    if nation_id is None:
        result.skipped.append(
            "mercenary bids: the player nation could not be identified from "
            f"{trn_path.name!r}")
        return data, 0
    try:
        existing = {bid.slot: bid for bid in h2.read_mercenary_bids(original)}
    except h2.QueueWriteRefused as exc:
        result.skipped.append(f"mercenary bids — {exc}")
        return data, 0

    delta = 0
    for row in rows:
        slot = int(row["slot"])
        amount = row["amount"]
        company = str(row["company"])
        if slot >= len(companies):
            result.skipped.append(
                f"mercenary bid on {company}: auction slot {slot} no longer "
                f"exists; {len(companies)} company/companies are on offer")
            continue
        # The company at that slot must still be the one the decision was
        # about. The auction turns over between turns, and a stale intent row
        # would otherwise bid this turn's gold on a different company.
        if companies[slot].name != company:
            result.skipped.append(
                f"mercenary bid on {company}: slot {slot} now holds "
                f"{companies[slot].name!r}; refusing to bid on a company the "
                "decision was not about")
            continue
        if amount is not None:
            try:
                minimum = MC.calculate_minimum_bid(
                    companies[slot], nation_id, reference_conn)
            except MC.MinimumBidUnavailable as exc:
                quoted_minimum = row["quoted_minimum"]
                if quoted_minimum is not None:
                    minimum_amount = int(quoted_minimum)
                else:
                    result.skipped.append(
                        f"mercenary bid on {company}: {exc}; refusing to "
                        "spend gold")
                    continue
            else:
                minimum_amount = minimum.amount
            if int(amount) < minimum_amount:
                result.skipped.append(
                    f"mercenary bid on {company}: {int(amount)} gold is below "
                    f"the current nation-adjusted minimum of {minimum_amount}")
                continue
        was = existing.get(slot)
        try:
            if amount is None:
                candidate = h2.set_mercenary_bid(data, slot, None)
            else:
                candidate = h2.set_mercenary_bid(
                    data, slot, int(amount), int(row["province_id"]))
        except h2.QueueWriteRefused as exc:
            result.skipped.append(f"mercenary bid on {company} — {exc}")
            continue
        delta += (0 if amount is None else int(amount)) - (
            was.amount if was is not None else 0)
        data = candidate
        if amount is None:
            result.written.append(
                f"mercenary bid on {company} withdrawn"
                + (f" (was {was.amount} gold)" if was is not None else ""))
        else:
            result.written.append(
                f"mercenary bid on {company}: {int(amount)} gold, arriving in "
                f"province {int(row['province_id'])}")
    return data, delta


def _apply_gold_commitments(original: bytes, data: bytes,
                            strategic_rows: list[sqlite3.Row],
                            recruit_rows: list[sqlite3.Row],
                            reserved_delta: int,
                            result: MaterializeResult) -> bytes:
    """Adjust stored gold by deltas, preserving unrelated player commitments.

    `reserved_delta` is the net gold newly reserved by every writer that keeps
    its own running total — province defence and mercenary bids — as opposed
    to recruitment and construction, whose costs are recomputed here.
    """
    from dom6_assistant.file_reader.formats import h2
    from .orders_2h import find_order_blocks

    before_remaining = h2.gold_remaining(original)
    if before_remaining is None:
        if (recruit_rows or reserved_delta
                or any(r["order_name"] in CONSTRUCTION_GOLD_COSTS
                       for r in strategic_rows)):
            result.skipped.append("gold commitments — remaining-gold field absent")
        return data

    recruitment_delta = 0
    if recruit_rows:
        recruitment_delta = (
            h2.parse_bytes(data).gold_spent
            - h2.parse_bytes(original).gold_spent)

    before_orders = find_order_blocks(original)
    construction_delta = 0
    for row in strategic_rows:
        before = before_orders.get(row["commander_id"])
        old_cost = (CONSTRUCTION_GOLD_COSTS.get(before.order_name, 0)
                    if before is not None else 0)
        new_cost = CONSTRUCTION_GOLD_COSTS.get(row["order_name"], 0)
        construction_delta += new_cost - old_cost

    delta = recruitment_delta + construction_delta + reserved_delta
    if not delta:
        return data
    remaining = before_remaining - delta
    try:
        return h2.set_gold_remaining_value(data, remaining)
    except h2.QueueWriteRefused as exc:
        result.skipped.append(f"gold commitments — {exc}")
        return data


def _parameter_suffix(order: str, parameter: int) -> str:
    """Human-readable materialisation preview for the typed +116 field."""
    kind = ORDER_SPECS[order].parameter_kind
    if kind == PARAM_NONE:
        return ""
    if kind in (PARAM_PROVINCE, PARAM_PROVINCE_OR_ZERO):
        return f" -> province {parameter}" if parameter else " -> hide"
    if kind == PARAM_CURRENT_PROVINCE:
        return f" -> current province {parameter}"
    if kind == PARAM_ITEM:
        return f" -> item {parameter}"
    if kind == PARAM_MAGIC_PATH:
        return f" -> {MAGIC_PATH_NAMES.get(parameter, f'path {parameter}')}"
    if kind == PARAM_BUILDING:
        return f" -> {BUILDING_NAMES.get(parameter, f'building {parameter}')}"
    if kind == PARAM_NATION:
        return f" -> own nation {parameter}"
    return f" -> parameter {parameter}"


def _forge_paths(reference_conn: sqlite3.Connection,
                 item_id: int) -> tuple[int, ...] | None:
    """FAWESDNGB path indices for a forge, primary then secondary."""
    from dom6_assistant.file_reader.formats.h2 import GEM_PATHS
    from dom6_assistant.reference.forge_cost import PATH_NAMES
    row = reference_conn.execute(
        "SELECT mainpath, mainlevel, secondarypath, secondarylevel "
        "FROM items WHERE id=?",
        (item_id,)).fetchone()
    if row is None or not row["mainpath"] or int(row["mainlevel"] or 0) <= 0:
        return None
    letters = [row["mainpath"]]
    if row["secondarypath"] and int(row["secondarylevel"] or 0) > 0:
        letters.append(row["secondarypath"])
    names = [PATH_NAMES.get(letter) for letter in letters]
    if any(name not in GEM_PATHS for name in names):
        return None
    return tuple(GEM_PATHS.index(name) for name in names)


def _spell_primary_path(reference_conn: sqlite3.Connection,
                        spell_id: int) -> int | None:
    """The gem path a ritual reserves, from the authoritative spell row."""
    row = reference_conn.execute(
        "SELECT path1 FROM spells WHERE id=?", (spell_id,)).fetchone()
    if row is None:
        return None
    value = int(row[0])
    return value if 0 <= value <= 8 else None


def record_order(conn: sqlite3.Connection, game_id: int, turn: int,
                 commander_id: int, order_name: str, *,
                 commander_name: str | None = None,
                 parameter: int | None = None,
                 destination: int | None = None,
                 rationale: str | None = None) -> int:
    """Record one decision. Returns the new row id.

    Validates the order name here as well as at write time, so a bad decision is
    rejected when it is made rather than surfacing later as a skipped row in a
    materialisation the caller may not be reading closely.
    """
    if order_name not in ORDER_CODES:
        raise ValueError(
            f"order {order_name!r} is not verified against the game. "
            f"Known: {sorted(ORDER_CODES)}")
    if parameter is not None and destination is not None:
        raise ValueError("give parameter or legacy destination, not both")
    if parameter is None:
        parameter = destination
    parameter = normalize_order_parameter(order_name, parameter)
    cur = conn.execute(
        "INSERT INTO order_intent(game_id, turn, commander_id, commander_name, "
        "order_name, destination, rationale) VALUES(?,?,?,?,?,?,?)",
        (game_id, turn, commander_id, commander_name, order_name,
         parameter, rationale))
    conn.commit()
    return int(cur.lastrowid)


def record_shape_change(
        conn: sqlite3.Connection, game_id: int, turn: int,
        commander_id: int, source_type_id: int, source_hp: int,
        target_type_id: int, target_hp: int, *,
        commander_name: str | None = None,
        rationale: str | None = None) -> int:
    """Record desired instantaneous form state independently of orders."""
    for label, value in (
            ("source type", source_type_id), ("source HP", source_hp),
            ("target type", target_type_id), ("target HP", target_hp)):
        if not 1 <= int(value) <= 0xFFFF:
            raise ValueError(f"implausible {label} {value}")
    cur = conn.execute(
        "INSERT INTO shape_change_intent(game_id, turn, commander_id, "
        "commander_name, source_type_id, source_hp, target_type_id, "
        "target_hp, rationale) VALUES(?,?,?,?,?,?,?,?,?)",
        (game_id, turn, commander_id, commander_name,
         source_type_id, source_hp, target_type_id, target_hp, rationale))
    conn.commit()
    return int(cur.lastrowid)


def record_ritual(conn: sqlite3.Connection, game_id: int, turn: int,
                  commander_id: int, spell_id: int, gem_path: int,
                  gem_cost: int, *, commander_name: str | None = None,
                  spell_name: str | None = None,
                  target_province: int | None = None,
                  target_commander_id: int | None = None,
                  target_unit_instance_id: int | None = None,
                  target_item_id: int | None = None,
                  wish_item_id: int | None = None,
                  wish_unit_id: int | None = None,
                  wish_nation_id: int | None = None,
                  wish_result: str | None = None,
                  target_global_effect_id: int | None = None,
                  monthly: bool = False,
                  rationale: str | None = None) -> int:
    """Record a complete ritual in the ordinary commander-order history."""
    if not 1 <= spell_id <= 0xFFFE:
        raise ValueError(f"implausible spell id {spell_id}")
    if not 0 <= gem_path <= 8:
        raise ValueError(f"ritual gem path must be 0-8, got {gem_path}")
    if not 0 <= gem_cost <= 1_000_000:
        raise ValueError(f"implausible ritual gem cost {gem_cost}")
    if target_province is not None and not 1 <= target_province <= 5000:
        raise ValueError(
            f"implausible ritual target province {target_province}")
    if target_commander_id is not None and not 1 <= target_commander_id <= 0xFFFF:
        raise ValueError(
            f"implausible ritual target commander {target_commander_id}")
    if (target_unit_instance_id is not None
            and not 1 <= target_unit_instance_id < 0xFFFF):
        raise ValueError(
            f"implausible ritual target unit {target_unit_instance_id}")
    if (target_unit_instance_id is not None
            and (target_province is not None
                 or target_commander_id is not None
                 or target_item_id is not None
                 or wish_item_id is not None
                 or wish_unit_id is not None
                 or wish_nation_id is not None
                 or wish_result is not None
                 or target_global_effect_id is not None)):
        raise ValueError(
            "a ritual unit target cannot also name a province, commander, or "
            "global enchantment")
    if target_item_id is not None and not 1 <= target_item_id <= 1500:
        raise ValueError(f"implausible ritual target item {target_item_id}")
    if (target_item_id is not None
            and (target_province is None or target_commander_id is None
                 or target_global_effect_id is not None)):
        raise ValueError(
            "a ritual item payload requires a province and recipient commander "
            "and cannot also name a global enchantment")
    if wish_item_id is not None and not 1 <= wish_item_id <= 1500:
        raise ValueError(f"implausible Wish item {wish_item_id}")
    if wish_unit_id is not None and not 1 <= wish_unit_id <= 0x7FFFFFFF:
        raise ValueError(f"implausible Wish unit {wish_unit_id}")
    if wish_nation_id is not None and not 0 <= wish_nation_id <= 0x7FFFFFFF:
        raise ValueError(f"implausible Wish nation {wish_nation_id}")
    if (wish_item_id is not None
            and (target_province is not None
                 or target_commander_id is not None
                 or target_unit_instance_id is not None
                 or target_item_id is not None
                 or target_global_effect_id is not None)):
        raise ValueError(
            "a Wish item payload cannot also name a province, commander, "
            "troop, transported item, or global enchantment")
    wish_objects = sum(value is not None for value in (
        wish_item_id, wish_unit_id, wish_nation_id))
    if wish_objects > 1:
        raise ValueError("Wish accepts only one item, unit, or nation payload")
    if wish_result is not None:
        if wish_result not in WISH_RESULT_CODES:
            raise ValueError(
                f"unsupported Wish result {wish_result!r}; supported: "
                f"{sorted(WISH_RESULT_CODES)}")
        spec = WISH.SPECS[wish_result]
        selected = {
            "item": wish_item_id,
            "unit": wish_unit_id,
            "nation": wish_nation_id,
        }.get(spec.payload_kind)
        if spec.payload_kind in {"item", "unit", "nation"} and selected is None:
            raise ValueError(
                f"the {wish_result} Wish result requires its {spec.payload_kind} id")
        if (spec.payload_kind not in
                {"item", "unit", "optional_unit", "nation"}
                and wish_objects):
            raise ValueError(f"the {wish_result} Wish result takes no object id")
        if (target_province is not None or target_commander_id is not None
                or target_unit_instance_id is not None
                or target_item_id is not None
                or target_global_effect_id is not None):
            raise ValueError(
                "a Wish result cannot also name a province, commander, troop, "
                "transported item, or global enchantment")
    order_name = "monthly_ritual" if monthly else "cast_ritual"
    with conn:
        cur = conn.execute(
            "INSERT INTO order_intent(game_id, turn, commander_id, "
            "commander_name, order_name, destination, rationale) "
            "VALUES(?,?,?,?,?,?,?)",
            (game_id, turn, commander_id, commander_name, order_name,
             spell_id, rationale))
        row_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO ritual_intent(order_intent_id, spell_id, spell_name, "
            "gem_path, gem_cost, target_province, target_commander_id, "
            "target_unit_instance_id, target_item_id, wish_item_id, wish_unit_id, "
            "wish_nation_id, wish_result, target_global_effect_id, monthly) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row_id, spell_id, spell_name, gem_path, gem_cost,
             target_province, target_commander_id, target_unit_instance_id,
             target_item_id, wish_item_id, wish_unit_id, wish_nation_id, wish_result,
             target_global_effect_id, int(monthly)))
    return row_id


def _ritual_recipient_runtime_index(
        editor: OrdersEditor, target_commander_id: int | None) -> int | None:
    """Resolve the stable commander id to the transport ritual's wire value."""
    if target_commander_id is None:
        return None
    block = editor.blocks.get(int(target_commander_id))
    if block is None:
        raise ValueError(
            f"ritual recipient commander {target_commander_id} has no order "
            "block in the pristine .2h")
    return struct.unpack_from("<I", editor.data, block.name_end)[0]


def _ritual_unit_runtime_index(
        editor: OrdersEditor, nation_id: int | None,
        instance_id: int | None) -> int | None:
    """Resolve a stable troop instance to a unit ritual's wire selector."""
    if instance_id is None:
        return None
    if nation_id is None:
        raise ValueError(
            "ritual unit target cannot be resolved because the game's nation "
            "id is unknown")
    matches = [
        unit for unit in read_h2_units(bytes(editor.data), nation_id)
        if unit.instance_id == int(instance_id) and not unit.is_mount
    ]
    if len(matches) != 1:
        detail = "not found" if not matches else "not unique"
        raise ValueError(
            f"ritual target unit instance {instance_id} is {detail} in the .2h")
    return int(matches[0].runtime_index)


def _rows(conn: sqlite3.Connection, view: str, game_id: int, turn: int,
          order_by: str = "commander_id") -> list[sqlite3.Row]:
    """Intent from one of the tables or views, if it exists.

    `order_by` is a parameter because it was not, and that cost a silent
    failure: recruitment rows were fetched with `ORDER BY commander_id`, a
    column recruit_intent_v2 does not have, so every query raised and the
    except below turned it into "no recruitment intent". Nothing was written
    and nothing was reported.

    The except now covers only a MISSING table, which is the case it was for —
    an older database should still be able to write its main orders. Any other
    OperationalError is a bug and is allowed to surface.
    """
    conn.row_factory = sqlite3.Row
    if not _has_table(conn, view):
        return []
    return list(conn.execute(
        f"SELECT * FROM {view} WHERE game_id=? AND turn=? "
        f"ORDER BY {order_by}", (game_id, turn)))


def _label(row: sqlite3.Row) -> str:
    return row["commander_name"] or f"commander {row['commander_id']}"


def _apply_battle(conn: sqlite3.Connection, editor: OrdersEditor,
                  game_id: int, turn: int, result: MaterializeResult, *,
                  reference_conn: sqlite3.Connection | None = None,
                  commander_types: dict[int, int] | None = None,
                  commander_experience: dict[int, int] | None = None,
                  ) -> None:
    """Battle stances, targets and formations.

    A squad of None is the commander's own order at +198/+199; a number is a
    squad slot. Each field is applied independently so that a refused formation
    does not discard a good stance — but every refusal is reported, because a
    battle set up three-quarters of the way is a battle fought differently
    from the one that was planned.
    """
    for row in _rows(conn, "current_battle_intent", game_id, turn):
        name, squad = _label(row), row["squad"]
        where = "own order" if squad is None else f"squad {squad}"
        try:
            if row["stance"]:
                if squad is None:
                    editor.set_battle_order(row["commander_id"], row["stance"],
                                            row["target"])
                else:
                    editor.set_squad_order(row["commander_id"], squad,
                                           row["stance"], row["target"])
                target = f" targeting {row['target']}" if row["target"] else ""
                result.written.append(
                    f"{name} {where}: {row['stance']}{target}")
            if row["formation"] is not None and squad is not None:
                if reference_conn is None:
                    raise ValueError(
                        "formation write needs the reference unit/item database")
                type_id = (commander_types or {}).get(row["commander_id"])
                if type_id is None:
                    raise ValueError(
                        "commander type is unknown for formation eligibility")
                unit = reference_conn.execute(
                    "SELECT * FROM units WHERE id=?", (type_id,)).fetchone()
                if unit is None:
                    raise ValueError(
                        f"commander type {type_id} is absent from reference data")
                block = editor._block(row["commander_id"])
                items = [
                    reference_conn.execute(
                        "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                    for item_id in read_equipment(
                        editor.data, block.name_end).values()
                ]
                items = [item for item in items if item is not None]
                leadership = LD.leadership_for(
                    unit, items,
                    experience=(commander_experience or {}).get(
                        row["commander_id"], 0),
                )
                formation_name = FORMATION_NAMES.get(int(row["formation"]))
                if formation_name not in LD.available_formations(
                        leadership.normal):
                    raise ValueError(
                        f"formation {formation_name or row['formation']} "
                        f"requires normal Leadership "
                        f"{LD.ADVANCED_FORMATION_MIN_LEADERSHIP}; final value "
                        f"is {leadership.normal}")
                editor.set_formation(row["commander_id"], squad,
                                     row["formation"])
                result.written.append(
                    f"{name} {where}: formation {row['formation']}")
        except (ValueError, OrderTableNotLocated) as exc:
            result.skipped.append(f"{name} {where} — {exc}")


def _apply_equipment(conn: sqlite3.Connection, editor: OrdersEditor,
                     game_id: int, turn: int,
                     result: MaterializeResult) -> None:
    """Items into slots. Item 0 empties one."""
    rows = _rows(conn, "current_equipment_intent", game_id, turn)
    # Release cleared items first. This makes a pair of intents (clear source,
    # equip destination) an atomic commander-to-commander transfer during one
    # rebuild, independent of SQLite's otherwise unspecified view row order.
    rows.sort(key=lambda row: (int(row["item_id"]) != 0, int(row["id"])))
    for row in rows:
        name = _label(row)
        try:
            editor.transfer_equipment(row["commander_id"], row["slot"],
                                      row["item_id"])
        except (ValueError, OrderTableNotLocated) as exc:
            result.skipped.append(f"{name} {row['slot']} — {exc}")
            continue
        what = "cleared" if not row["item_id"] else f"item {row['item_id']}"
        result.written.append(f"{name} {row['slot']}: {what}")


def _apply_battle_positions(conn: sqlite3.Connection, editor: OrdersEditor,
                            game_id: int, turn: int,
                            result: MaterializeResult) -> None:
    """Write signed battlefield coordinates for real squad slots."""
    for row in _rows(conn, "current_battle_position_intent", game_id, turn):
        name = _label(row)
        try:
            editor.set_placement(row["commander_id"], row["squad"],
                                 row["x"], row["y"])
        except (ValueError, OrderTableNotLocated) as exc:
            result.skipped.append(
                f"{name} squad {row['squad']} placement — {exc}")
            continue
        result.written.append(
            f"{name} squad {row['squad']}: position ({row['x']}, {row['y']})")


def _apply_troop_assignments(conn: sqlite3.Connection, editor: OrdersEditor,
                             game_id: int, turn: int,
                             result: MaterializeResult, *,
                             commander_provinces: dict[int, int] | None = None,
                             ) -> None:
    """Move unit instances, creating explicitly recorded squad slots first."""
    rows = _rows(conn, "current_troop_assignment_intent", game_id, turn,
                 order_by="unit_instance_id")
    creations = _rows(
        conn, "current_squad_creation_intent", game_id, turn,
        order_by="target_commander_id, target_squad")
    if not rows and not creations:
        return
    game = conn.execute(
        "SELECT nation_id FROM games WHERE id=?", (game_id,)).fetchone()
    nation_id = game[0] if game is not None else None
    if nation_id is None:
        result.skipped.append(
            "troop assignment — game has no nation id for ownership checks")
        return
    assignment_rows = [
        row for row in rows if row["destination"] != "garrison"]
    detachment_rows = [
        row for row in rows if row["destination"] == "garrison"]
    if creations and not assignment_rows:
        result.skipped.append(
            "squad creation — no troop assignment populates the new slot")
        return
    moves = [(int(row["unit_instance_id"]),
              int(row["target_commander_id"]), int(row["target_squad"]))
             for row in assignment_rows]
    new_squads = {
        (int(row["target_commander_id"]), int(row["target_squad"])):
        int(row["squad_id"])
        for row in creations
    }
    outcome = {"created_slots": [], "moved": [], "cleared_slots": []}
    if moves or new_squads:
        try:
            outcome = editor.assign_troops(
                int(nation_id), moves, new_squads=new_squads,
                commander_provinces=commander_provinces)
        except (ValueError, OrderTableNotLocated) as exc:
            result.skipped.append(f"troop assignment — {exc}")
            return
    by_instance = {int(row["unit_instance_id"]): row for row in rows}
    for created in outcome["created_slots"]:
        result.written.append(
            f"commander {created['commander_id']} squad {created['slot']}: "
            f"created with id {created['squad_id']}")
    for moved in outcome["moved"]:
        row = by_instance[moved["instance_id"]]
        mount = " plus mount" if moved["records"] == 2 else ""
        unit_name = row["unit_name"] or f"unit {moved['instance_id']}"
        commander_name = (row["commander_name"]
                          or f"commander {row['target_commander_id']}")
        result.written.append(
            f"{unit_name} "
            f"#{moved['instance_id']}{mount} -> "
            f"{commander_name} "
            f"squad {row['target_squad']}")
    for cleared in outcome["cleared_slots"]:
        result.written.append(
            f"commander {cleared['commander_id']} squad {cleared['slot']}: "
            "cleared after its last troop moved")
    if detachment_rows:
        try:
            detached = editor.detach_troops(
                int(nation_id),
                [int(row["unit_instance_id"]) for row in detachment_rows],
            )
        except (ValueError, OrderTableNotLocated) as exc:
            result.skipped.append(f"troop detachment — {exc}")
            return
        detached_by_instance = {
            int(row["unit_instance_id"]): row for row in detachment_rows}
        for unit in detached["detached"]:
            row = detached_by_instance[unit["instance_id"]]
            mount = " plus mount" if unit["records"] == 2 else ""
            name = row["unit_name"] or f"unit {unit['instance_id']}"
            result.written.append(
                f"{name} #{unit['instance_id']}{mount} -> province garrison")
        for cleared in detached["cleared_slots"]:
            result.written.append(
                f"commander {cleared['commander_id']} squad "
                f"{cleared['slot']}: cleared after its last troop detached")


def _apply_battle_scripts(conn: sqlite3.Connection, editor: OrdersEditor,
                          game_id: int, turn: int,
                          result: MaterializeResult) -> None:
    """Replace each commander's complete five-slot single-round script."""
    for row in _rows(conn, "current_battle_script_intent", game_id, turn):
        name = _label(row)
        try:
            queue = json.loads(row["queue_json"])
            editor.set_spell_queue(row["commander_id"], queue)
        except (json.JSONDecodeError, TypeError, ValueError,
                OrderTableNotLocated) as exc:
            result.skipped.append(f"{name} battle script — {exc}")
            continue
        result.written.append(
            f"{name}: {len(queue)} scripted battle round(s)")


def _apply_carried_gems(
        conn: sqlite3.Connection, editor: OrdersEditor, original: bytes,
        remaining: list[int] | None, game_id: int, turn: int,
        result: MaterializeResult) -> list[int] | None:
    """Set carried totals and adjust the national pool by their net delta.

    All current carried-gem intents are budgeted together. This matters when
    gems move between commanders: a refund on a later commander must be able to
    fund an earlier assignment regardless of row ordering.
    """
    rows = _rows(conn, "current_carried_gem_intent", game_id, turn,
                 order_by="commander_id, path")
    if not rows:
        return remaining
    if remaining is None:
        result.skipped.append(
            "carried gems — national remaining-gem treasury could not be located")
        return remaining

    blocks = find_order_blocks(original)
    deltas = [0] * len(CARRIED_GEM_PATHS)
    prepared: list[tuple[sqlite3.Row, str, int]] = []
    for row in rows:
        name = _label(row)
        path = int(row["path"])
        amount = int(row["amount"])
        block = blocks.get(row["commander_id"])
        if block is None:
            result.skipped.append(
                f"{name} carried gems — no commander order block")
            continue
        if not 0 <= path < len(CARRIED_GEM_PATHS):
            result.skipped.append(
                f"{name} carried gems — invalid path index {path}")
            continue
        path_name = CARRIED_GEM_PATHS[path]
        old = read_carried_gems(original, block.name_end).get(path_name, 0)
        deltas[path] += amount - old
        prepared.append((row, path_name, amount))

    candidate = [value - deltas[i] for i, value in enumerate(remaining)]
    shortages = [
        f"{CARRIED_GEM_PATHS[i]} needs {deltas[i]} more but only "
        f"{remaining[i]} remain"
        for i in range(len(candidate)) if candidate[i] < 0
    ]
    if shortages:
        result.skipped.append("carried gems — " + "; ".join(shortages))
        return remaining

    for row, path_name, amount in prepared:
        name = _label(row)
        try:
            editor.set_carried_gems(row["commander_id"], path_name, amount)
        except (ValueError, OrderTableNotLocated) as exc:
            result.skipped.append(f"{name} carried {path_name} gems — {exc}")
            continue
        result.written.append(f"{name}: carry {amount} {path_name} gem(s)")
    return candidate


def record_battle_order(conn: sqlite3.Connection, game_id: int, turn: int,
                        commander_id: int, *, squad: int | None = None,
                        stance: str | None = None, target: str | None = None,
                        formation: int | None = None,
                        commander_name: str | None = None,
                        rationale: str | None = None) -> int:
    """Record how a commander or one of his squads should fight."""
    if stance is not None and stance not in STANCE_CODES:
        raise ValueError(
            f"stance {stance!r} is not verified against the game. "
            f"Known: {sorted(STANCE_CODES)}")
    if target is not None and target not in TARGET_CODES_V2:
        raise ValueError(
            f"target {target!r} is not verified against the game. "
            f"Known: {sorted(TARGET_CODES_V2)}")
    if stance is None and formation is None:
        raise ValueError("nothing to record: give a stance or a formation")
    # Carry forward whatever this call does not mention. "Latest row wins" is
    # the right rule per FIELD and the wrong one per ROW: setting a squad's
    # formation would otherwise blank the stance recorded a moment earlier,
    # because the newer row simply has no stance in it. That silently sent a
    # squad into battle on the default order after being told to hold and fire.
    conn.row_factory = sqlite3.Row
    previous = conn.execute(
        "SELECT stance, target, formation FROM battle_intent "
        "WHERE game_id=? AND turn=? AND commander_id=? "
        "AND squad IS ? ORDER BY id DESC LIMIT 1",
        (game_id, turn, commander_id, squad)).fetchone()
    if previous is not None:
        if stance is None:
            stance = previous["stance"]
        if target is None:
            target = previous["target"]
        if formation is None:
            formation = previous["formation"]
    cur = conn.execute(
        "INSERT INTO battle_intent(game_id, turn, commander_id, "
        "commander_name, squad, stance, target, formation, rationale) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (game_id, turn, commander_id, commander_name, squad, stance, target,
         formation, rationale))
    conn.commit()
    return int(cur.lastrowid)


def record_battle_position(conn: sqlite3.Connection, game_id: int, turn: int,
                           commander_id: int, squad: int, x: int, y: int, *,
                           commander_name: str | None = None,
                           rationale: str | None = None) -> int:
    """Record one squad's desired -12..+12 battlefield position."""
    if not 0 <= squad < 5:
        raise ValueError(f"squad slot must be 0-4, got {squad}")
    for axis, value in (("x", x), ("y", y)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"placement {axis} must be an integer")
        if not -PLACEMENT_EDGE <= value <= PLACEMENT_EDGE:
            raise ValueError(
                f"placement {axis} must be -{PLACEMENT_EDGE}.."
                f"{PLACEMENT_EDGE}, got {value}")
    cur = conn.execute(
        "INSERT INTO battle_position_intent(game_id, turn, commander_id, "
        "commander_name, squad, x, y, rationale) VALUES(?,?,?,?,?,?,?,?)",
        (game_id, turn, commander_id, commander_name, squad, x, y, rationale))
    conn.commit()
    return int(cur.lastrowid)


def record_carried_gems(conn: sqlite3.Connection, game_id: int, turn: int,
                        commander_id: int, path: int, amount: int, *,
                        commander_name: str | None = None,
                        rationale: str | None = None) -> int:
    """Record a desired carried total for one commander and gem path."""
    if not 0 <= path < len(CARRIED_GEM_PATHS):
        raise ValueError(f"gem path must be 0-{len(CARRIED_GEM_PATHS) - 1}")
    if (not isinstance(amount, int) or isinstance(amount, bool)
            or not 0 <= amount <= 255):
        raise ValueError(f"carried gem amount must be 0-255, got {amount!r}")
    cur = conn.execute(
        "INSERT INTO carried_gem_intent(game_id, turn, commander_id, "
        "commander_name, path, amount, rationale) VALUES(?,?,?,?,?,?,?)",
        (game_id, turn, commander_id, commander_name, path, amount, rationale))
    conn.commit()
    return int(cur.lastrowid)


def record_battle_script(conn: sqlite3.Connection, game_id: int, turn: int,
                         commander_id: int, queue: list[int], *,
                         commander_name: str | None = None,
                         rationale: str | None = None) -> int:
    """Record one complete zero-to-five-entry single-round script."""
    if len(queue) > 5:
        raise ValueError(f"battle script holds at most 5 rounds, got {len(queue)}")
    fixed = set(SINGLE_ROUND_CODES.values())
    for position, value in enumerate(queue):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"battle script slot {position} is not an integer")
        if value <= 0 and value not in fixed:
            raise ValueError(f"single-round code {value} is not verified")
        if value > 0x7FFF:
            raise ValueError(f"spell id {value} does not fit the signed u16 slot")
    cur = conn.execute(
        "INSERT INTO battle_script_intent(game_id, turn, commander_id, "
        "commander_name, queue_json, rationale) VALUES(?,?,?,?,?,?)",
        (game_id, turn, commander_id, commander_name,
         json.dumps(queue), rationale))
    conn.commit()
    return int(cur.lastrowid)


def record_troop_assignments(
        conn: sqlite3.Connection, game_id: int, turn: int,
        units: list[tuple[int, int, str | None]], target_commander_id: int,
        target_squad: int, *, commander_name: str | None = None,
        rationale: str | None = None) -> list[int]:
    """Record several unit-instance destinations as one transaction."""
    if not units:
        raise ValueError("give at least one unit instance")
    ids = [instance_id for instance_id, _type_id, _name in units]
    if len(set(ids)) != len(ids):
        raise ValueError("unit instance ids must be unique")
    if not 0 <= target_squad < 5:
        raise ValueError(f"target squad must be 0-4, got {target_squad}")
    row_ids = []
    with conn:
        for instance_id, type_id, unit_name in units:
            if not 1 <= instance_id <= 0xFFFF:
                raise ValueError(f"implausible unit instance id {instance_id}")
            if not 1 <= type_id <= 5000:
                raise ValueError(f"implausible unit type id {type_id}")
            cur = conn.execute(
                "INSERT INTO troop_assignment_intent(game_id, turn, "
                "unit_instance_id, unit_type_id, unit_name, "
                "target_commander_id, commander_name, target_squad, rationale) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (game_id, turn, instance_id, type_id, unit_name,
                 target_commander_id, commander_name, target_squad, rationale))
            row_ids.append(int(cur.lastrowid))
    return row_ids


def record_troop_detachments(
        conn: sqlite3.Connection, game_id: int, turn: int,
        units: list[tuple[int, int, str | None]], *,
        rationale: str | None = None) -> list[int]:
    """Record troops' final destination as their current province garrison."""
    if not units:
        raise ValueError("give at least one unit instance")
    ids = [instance_id for instance_id, _type_id, _name in units]
    if len(set(ids)) != len(ids):
        raise ValueError("unit instance ids must be unique")
    row_ids = []
    with conn:
        for instance_id, type_id, unit_name in units:
            if not 1 <= instance_id <= 0xFFFF:
                raise ValueError(f"implausible unit instance id {instance_id}")
            if not 1 <= type_id <= 5000:
                raise ValueError(f"implausible unit type id {type_id}")
            cur = conn.execute(
                "INSERT INTO troop_assignment_intent(game_id, turn, "
                "unit_instance_id, unit_type_id, unit_name, destination, "
                "target_commander_id, target_squad, rationale) "
                "VALUES(?,?,?,?,?,'garrison',0,-1,?)",
                (game_id, turn, instance_id, type_id, unit_name, rationale))
            row_ids.append(int(cur.lastrowid))
    return row_ids


def record_squad_creation(
        conn: sqlite3.Connection, game_id: int, turn: int,
        units: list[tuple[int, int, str | None]], target_commander_id: int,
        target_squad: int, squad_id: int, *,
        commander_name: str | None = None,
        rationale: str | None = None) -> dict[str, int | list[int]]:
    """Record a new squad and its initial troop destinations atomically."""
    if not units:
        raise ValueError("give at least one unit instance")
    ids = [instance_id for instance_id, _type_id, _name in units]
    if len(set(ids)) != len(ids):
        raise ValueError("unit instance ids must be unique")
    if not 0 <= target_squad < 5:
        raise ValueError(f"target squad must be 0-4, got {target_squad}")
    if not 1 <= squad_id < SQUAD_SLOT_EMPTY:
        raise ValueError(f"implausible squad id {squad_id}")

    assignment_ids: list[int] = []
    with conn:
        creation = conn.execute(
            "INSERT INTO squad_creation_intent(game_id, turn, "
            "target_commander_id, commander_name, target_squad, squad_id, "
            "rationale) VALUES(?,?,?,?,?,?,?)",
            (game_id, turn, target_commander_id, commander_name, target_squad,
             squad_id, rationale))
        for instance_id, type_id, unit_name in units:
            if not 1 <= instance_id <= 0xFFFF:
                raise ValueError(f"implausible unit instance id {instance_id}")
            if not 1 <= type_id <= 5000:
                raise ValueError(f"implausible unit type id {type_id}")
            cur = conn.execute(
                "INSERT INTO troop_assignment_intent(game_id, turn, "
                "unit_instance_id, unit_type_id, unit_name, "
                "target_commander_id, commander_name, target_squad, rationale) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (game_id, turn, instance_id, type_id, unit_name,
                 target_commander_id, commander_name, target_squad, rationale))
            assignment_ids.append(int(cur.lastrowid))
    return {"creation_id": int(creation.lastrowid),
            "assignment_ids": assignment_ids}


def record_equipment(conn: sqlite3.Connection, game_id: int, turn: int,
                     commander_id: int, slot: str, item_id: int, *,
                     commander_name: str | None = None,
                     rationale: str | None = None) -> int:
    """Record an item going into a slot. 0 empties it."""
    if slot not in EQUIPMENT_SLOTS:
        raise ValueError(
            f"unknown slot {slot!r}. Mapped: {sorted(EQUIPMENT_SLOTS)}")
    cur = conn.execute(
        "INSERT INTO equipment_intent(game_id, turn, commander_id, "
        "commander_name, slot, item_id, rationale) VALUES(?,?,?,?,?,?,?)",
        (game_id, turn, commander_id, commander_name, slot, item_id,
         rationale))
    conn.commit()
    return int(cur.lastrowid)


def record_equipment_transfer(
    conn: sqlite3.Connection,
    game_id: int,
    turn: int,
    source_commander_id: int,
    source_slot: str,
    destination_commander_id: int,
    destination_slot: str,
    item_id: int,
    *,
    source_commander_name: str | None = None,
    destination_commander_name: str | None = None,
    rationale: str | None = None,
) -> tuple[int, int]:
    """Record both halves of a worn-item transfer in one DB transaction."""
    for slot in (source_slot, destination_slot):
        if slot not in EQUIPMENT_SLOTS:
            raise ValueError(
                f"unknown slot {slot!r}. Mapped: {sorted(EQUIPMENT_SLOTS)}")
    if (source_commander_id, source_slot) == (
            destination_commander_id, destination_slot):
        raise ValueError("source and destination equipment slots are identical")
    if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0:
        raise ValueError(f"transferred item id must be a positive integer, got {item_id!r}")
    with conn:
        source = conn.execute(
            "INSERT INTO equipment_intent(game_id, turn, commander_id, "
            "commander_name, slot, item_id, rationale) VALUES(?,?,?,?,?,?,?)",
            (game_id, turn, source_commander_id, source_commander_name,
             source_slot, 0, rationale))
        destination = conn.execute(
            "INSERT INTO equipment_intent(game_id, turn, commander_id, "
            "commander_name, slot, item_id, rationale) VALUES(?,?,?,?,?,?,?)",
            (game_id, turn, destination_commander_id,
             destination_commander_name, destination_slot, item_id,
             rationale))
    return int(source.lastrowid), int(destination.lastrowid)


def record_province_defence(conn: sqlite3.Connection, game_id: int, turn: int,
                            province_id: int, target: int, *,
                            rationale: str) -> int:
    """Record one complete PD target; latest row wins per province."""
    from dom6_assistant.file_reader.formats.h2 import MAX_PROVINCE_DEFENCE

    if not isinstance(province_id, int) or isinstance(province_id, bool):
        raise ValueError(f"province id must be an integer, got {province_id!r}")
    if (not isinstance(target, int) or isinstance(target, bool)
            or not 0 <= target <= MAX_PROVINCE_DEFENCE):
        raise ValueError(
            f"province defence target must be 0-{MAX_PROVINCE_DEFENCE}, "
            f"got {target!r}")
    if not rationale.strip():
        raise ValueError("rationale must not be empty")
    cur = conn.execute(
        "INSERT INTO province_defence_intent(game_id, turn, province_id, "
        "target, rationale) VALUES(?,?,?,?,?)",
        (game_id, turn, province_id, target, rationale.strip()))
    conn.commit()
    return int(cur.lastrowid)


def record_forge(conn: sqlite3.Connection, game_id: int, turn: int,
                 commander_id: int, item_id: int, gem_path: int,
                 gem_cost: int, *, commander_name: str | None = None,
                 item_name: str | None = None,
                 secondary_gem_path: int | None = None,
                 secondary_gem_cost: int = 0,
                 rationale: str | None = None) -> int:
    """Record a forge in the ordinary commander-order history.

    `gem_cost` is what the file RESERVES, which for one of our own rebated
    items is the discounted figure, not the price the forge screen shows.
    """
    if not 1 <= item_id <= 0xFFFE:
        raise ValueError(f"implausible item id {item_id}")
    if not 0 <= gem_path <= 8:
        raise ValueError(f"forge gem path must be 0-8, got {gem_path}")
    if not 0 <= gem_cost <= 0xFFFF:
        raise ValueError(f"implausible forge gem cost {gem_cost}")
    if secondary_gem_path is None:
        if secondary_gem_cost:
            raise ValueError(
                "secondary forge cost requires a secondary gem path")
    elif not 0 <= secondary_gem_path <= 8:
        raise ValueError(
            f"secondary forge gem path must be 0-8, got {secondary_gem_path}")
    if not 0 <= secondary_gem_cost <= 0xFFFF:
        raise ValueError(
            f"implausible secondary forge gem cost {secondary_gem_cost}")
    with conn:
        cur = conn.execute(
            "INSERT INTO order_intent(game_id, turn, commander_id, "
            "commander_name, order_name, destination, rationale) "
            "VALUES(?,?,?,?,?,?,?)",
            (game_id, turn, commander_id, commander_name, "forge_magic_item",
             item_id, rationale))
        row_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO forge_intent(order_intent_id, item_id, item_name, "
            "gem_path, gem_cost, secondary_gem_path, secondary_gem_cost) "
            "VALUES(?,?,?,?,?,?,?)",
            (row_id, item_id, item_name, gem_path, gem_cost,
             secondary_gem_path, secondary_gem_cost))
    return row_id


def record_empowerment(conn: sqlite3.Connection, game_id: int, turn: int,
                       commander_id: int, gem_path: int, target_level: int,
                       gem_cost: int, *, commander_name: str | None = None,
                       rationale: str | None = None) -> int:
    """Record an empowerment and the gems it reserves."""
    if not 0 <= gem_path <= 8:
        raise ValueError(f"magic path must be 0-8, got {gem_path}")
    if not 1 <= target_level <= 20:
        raise ValueError(f"implausible target path level {target_level}")
    if not 0 < gem_cost <= 1000:
        raise ValueError(f"implausible empowerment cost {gem_cost}")
    with conn:
        cur = conn.execute(
            "INSERT INTO order_intent(game_id, turn, commander_id, "
            "commander_name, order_name, destination, rationale) "
            "VALUES(?,?,?,?,?,?,?)",
            (game_id, turn, commander_id, commander_name, "empowerment",
             gem_path, rationale))
        row_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO empowerment_intent(order_intent_id, gem_path, "
            "target_level, gem_cost) VALUES(?,?,?,?)",
            (row_id, gem_path, target_level, gem_cost))
    return row_id


def record_mercenary_bid(conn: sqlite3.Connection, game_id: int, turn: int,
                         slot: int, company: str, amount: int | None,
                         province_id: int | None, *, rationale: str,
                         quoted_minimum: int | None = None) -> int:
    """Record one auction slot's bid; latest row wins per slot.

    `amount=None` withdraws. The company name is stored alongside the slot so
    materialisation can refuse a row whose slot now holds a different company:
    the auction turns over between turns, and a slot index alone would let a
    stale decision spend gold on whatever moved into that position.
    """
    if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
        raise ValueError(f"auction slot must be a non-negative integer, "
                         f"got {slot!r}")
    if not company.strip():
        raise ValueError("company must not be empty")
    if amount is None:
        if province_id is not None or quoted_minimum is not None:
            raise ValueError(
                "withdrawing a bid takes no province or minimum quote")
    else:
        if (not isinstance(amount, int) or isinstance(amount, bool)
                or not 1 <= amount <= 0xFFFF):
            raise ValueError(f"bid must be 1-65535 gold, got {amount!r}")
        if (not isinstance(province_id, int) or isinstance(province_id, bool)
                or province_id < 1):
            raise ValueError(
                f"arrival province must be a province id, got {province_id!r}")
        if quoted_minimum is None:
            raise ValueError(
                "a positive bid requires the calculated nation-adjusted minimum")
        if (not isinstance(quoted_minimum, int)
                or isinstance(quoted_minimum, bool)
                or not 1 <= quoted_minimum <= 0xFFFF):
            raise ValueError(
                f"quoted minimum must be 1-65535 gold, got "
                f"{quoted_minimum!r}")
        if amount < quoted_minimum:
            raise ValueError(
                f"bid {amount} is below quoted minimum {quoted_minimum}")
    if not rationale.strip():
        raise ValueError("rationale must not be empty")
    cur = conn.execute(
        "INSERT INTO mercenary_bid_intent(game_id, turn, slot, company, "
        "amount, province_id, quoted_minimum, rationale) VALUES(?,?,?,?,?,?,?,?)",
        (game_id, turn, slot, company.strip(), amount, province_id,
         quoted_minimum, rationale.strip()))
    conn.commit()
    return int(cur.lastrowid)


def record_research_queue(conn: sqlite3.Connection, game_id: int, turn: int,
                          targets: list[int], *, rationale: str) -> int:
    """Record one complete school/spell research queue; latest row wins."""
    from dom6_assistant.file_reader.formats.h2 import (
        RESEARCH_QUEUE_CAPACITY,
        RESEARCH_TARGET_MAX,
    )

    if len(targets) > RESEARCH_QUEUE_CAPACITY:
        raise ValueError(
            f"research queue holds at most {RESEARCH_QUEUE_CAPACITY} entries")
    if not all(isinstance(target, int) and not isinstance(target, bool)
               and 0 <= target <= RESEARCH_TARGET_MAX for target in targets):
        raise ValueError(
            f"research targets must be integers in the range 0-{RESEARCH_TARGET_MAX}")
    if not rationale.strip():
        raise ValueError("rationale must not be empty")
    cur = conn.execute(
        "INSERT INTO research_intent(game_id, turn, queue_json, rationale) "
        "VALUES(?,?,?,?)",
        (game_id, turn, json.dumps(targets), rationale.strip()))
    conn.commit()
    return int(cur.lastrowid)


def record_diplomatic_action(
    conn: sqlite3.Connection,
    game_id: int,
    turn: int,
    target_nation_id: int,
    action: str,
    missive: str | None,
    *,
    rationale: str,
) -> int:
    """Record one complete outgoing action; latest row wins per target."""
    if (
        not isinstance(target_nation_id, int)
        or isinstance(target_nation_id, bool)
        or not 5 <= target_nation_id <= 499
    ):
        raise ValueError(f"invalid diplomacy target {target_nation_id!r}")
    if action not in {
        "propose_nap",
        "accept_nap",
        "decline_nap",
        "declare_war",
        "clear",
    }:
        raise ValueError(f"unknown diplomatic action {action!r}")
    if action == "clear":
        if missive is not None:
            raise ValueError("clearing diplomacy takes no missive")
    elif not missive or not missive.strip():
        raise ValueError(f"{action} requires a non-empty missive")
    if not rationale.strip():
        raise ValueError("rationale must not be empty")
    cur = conn.execute(
        "INSERT INTO diplomacy_intent(game_id, turn, target_nation_id, "
        "action, missive, rationale) VALUES(?,?,?,?,?,?)",
        (
            game_id,
            turn,
            target_nation_id,
            action,
            missive,
            rationale.strip(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _apply_diplomacy(
    conn: sqlite3.Connection,
    original: bytes,
    data: bytes,
    game_id: int,
    turn: int,
    h2_path: Path,
    result: MaterializeResult,
) -> bytes:
    """Replace this player's outgoing diplomacy list from current intent."""
    from dom6_assistant.agent.visibility import PlayerView
    from dom6_assistant.file_reader.formats import (
        diplomacy as D,
        messages as Msg,
        trn as T,
    )

    if not _has_table(conn, "diplomacy_intent"):
        return data
    rows = _rows(
        conn,
        "current_diplomacy_intent",
        game_id,
        turn,
        order_by="target_nation_id",
    )
    if not rows:
        return data
    trn_path = h2_path.with_suffix(".trn")
    if not trn_path.exists():
        result.skipped.append("diplomacy: no .trn beside the .2h")
        return data
    parsed = T.parse(trn_path)
    try:
        existing = D.find_outgoing_diplomacy(original)[2]
        view = PlayerView(h2_path.parent, parsed.nation_id, trn_name=trn_path.name)
        relations = {row.nation_id: row for row in view.diplomatic_relations()}
        incoming_nap_sources = {
            message.source_nation_id
            for message in view.turn_messages()
            if message.type_id == Msg.NAP_PROPOSAL_TYPE
        }
    except (OSError, ValueError) as exc:
        result.skipped.append(f"diplomacy: cannot verify current state ({exc})")
        return data

    by_target = {record.target_nation_id: record for record in existing}
    for row in rows:
        target = int(row["target_nation_id"])
        action = str(row["action"])
        relation = relations.get(target)
        if relation is None:
            result.skipped.append(
                f"diplomacy toward nation {target}: nation is not a participant"
            )
            continue
        if action == "clear":
            removed = by_target.pop(target, None)
            result.written.append(
                f"nation {target}: cleared pending diplomatic action"
                + (f" {removed.action}" if removed is not None else "")
            )
            continue
        if relation.defeated or relation.status == "same_god" or not relation.contact:
            result.skipped.append(
                f"diplomacy toward nation {target}: action is no longer legal "
                f"in state {relation.status}"
            )
            continue
        if action == "propose_nap" and relation.nap_phase is not None:
            # Both an active pact and an expiring one block a proposal. Suing
            # for peace is only available once the war has actually begun.
            reason = (
                "is counting down to war"
                if relation.nap_phase == "ending"
                else "already exists"
            )
            result.skipped.append(
                f"diplomacy toward nation {target}: a {relation.status} {reason}"
            )
            continue
        if action in {"accept_nap", "decline_nap"} and target not in incoming_nap_sources:
            result.skipped.append(
                f"diplomacy toward nation {target}: there is no current incoming "
                "NAP proposal to answer"
            )
            continue
        if action == "declare_war" and (
            relation.status != "apprehensive" and relation.nap_phase != "active"
        ):
            # Declaring under an active pact is legal: it starts the notice
            # counting down rather than beginning the war immediately. An
            # expiring pact and an existing war are both closed by rule.
            result.skipped.append(
                f"diplomacy toward nation {target}: war is not available in "
                f"state {relation.status}"
            )
            continue
        missive = row["missive"]
        if not missive:
            result.skipped.append(
                f"diplomacy toward nation {target}: {action} has no missive"
            )
            continue
        type_id = {
            "declare_war": 3,
            "propose_nap": 4,
            "accept_nap": 5,
            "decline_nap": 6,
        }[action]
        by_target[target] = D.OutgoingDiplomacyRecord(
            slot=0,
            target_nation_id=target,
            type_id=type_id,
            extra=0,
            text=str(missive),
        )
        result.written.append(f"nation {target}: {action}")
    try:
        return D.set_outgoing_diplomacy(
            data, [by_target[target] for target in sorted(by_target)]
        )
    except (D.DiplomacyDecodeError, ValueError) as exc:
        result.skipped.append(f"diplomacy: cannot write outgoing list ({exc})")
        return data


def _apply_research(conn: sqlite3.Connection, data: bytes, game_id: int,
                    turn: int, h2_path: Path,
                    result: MaterializeResult) -> bytes:
    """Write the latest complete research queue using current `.trn` state."""
    from dom6_assistant.file_reader.formats import h2, trn as T

    rows = _rows(conn, "current_research_intent", game_id, turn,
                 order_by="id")
    if not rows:
        return data
    trn_path = h2_path.with_suffix(".trn")
    if not trn_path.exists():
        result.skipped.append(
            "research: no .trn beside the .2h, so its state anchor cannot be built")
        return data
    parsed = T.parse(trn_path)
    levels = parsed.research_levels
    progress = parsed.research_progress_raw
    if levels is None or progress is None:
        result.skipped.append(
            "research: current levels/progress could not be decoded from the .trn")
        return data
    try:
        targets = json.loads(rows[0]["queue_json"])
        if not isinstance(targets, list):
            raise ValueError("stored queue is not a JSON array")
        if not all(isinstance(target, int) and not isinstance(target, bool)
                   for target in targets):
            raise ValueError("stored research queue contains a non-integer target")
        data = h2.set_research_queue(data, targets, levels, progress)
    except (json.JSONDecodeError, ValueError,
            h2.ResearchQueueNotLocated) as exc:
        result.skipped.append(f"research — {exc}")
        return data

    seen = [0] * len(T.RESEARCH_SCHOOLS)
    labels: list[str] = []
    for target in targets:
        if target < len(T.RESEARCH_SCHOOLS):
            seen[target] += 1
            labels.append(
                f"{T.RESEARCH_SCHOOLS[target]} {levels[target] + seen[target]}")
        else:
            labels.append(f"Level 9 spell {target}")
    result.written.append(
        "research: " + (" -> ".join(labels) if labels else "queue cleared"))
    return data


def _reference_recruit_kind(reference_conn: sqlite3.Connection,
                            unit_type_id: int) -> str:
    """Classify legacy intent rows recorded before recruit kind was stored."""
    leader = reference_conn.execute(
        "SELECT 1 FROM ("
        "SELECT monster_number FROM fort_leader_types_by_nation "
        "UNION SELECT monster_number FROM nonfort_leader_types_by_nation "
        "UNION SELECT monster_number FROM coast_leader_types_by_nation"
        ") WHERE monster_number=? LIMIT 1", (unit_type_id,)).fetchone()
    if leader is not None:
        return "commander"
    site_columns = (
        "hcom1", "hcom2", "hcom3", "hcom4", "hcom5",
        "com1", "com2", "com3", "com4", "com5", "natcom",
    )
    predicate = " OR ".join(f"{column}=?" for column in site_columns)
    site = reference_conn.execute(
        f"SELECT 1 FROM magic_sites WHERE {predicate} LIMIT 1",
        (unit_type_id,) * len(site_columns),
    ).fetchone()
    return "commander" if site is not None else "troop"


def _apply_recruitment(conn: sqlite3.Connection, data: bytes, game_id: int,
                       turn: int, h2_path: Path,
                       result: MaterializeResult, *,
                       reference_conn: sqlite3.Connection | None = None) -> bytes:
    """Write each province's recruitment queue, then fix the gold figure.

    The gold figure is not optional. The game DISPLAYS the remaining-gold value
    stored in the file rather than deriving it from the queue, so a queue
    written without updating it leaves the player looking at a number for a
    state that no longer exists — which is exactly how the omission was caught.
    """
    from dom6_assistant.file_reader.formats import h2, trn as T

    rows = _rows(conn, "recruit_intent_v2", game_id, turn,
                 order_by="province_id, position")
    if not rows:
        return data

    trn = h2_path.with_suffix(".trn")
    if not trn.exists():
        result.skipped.append("recruitment: no .trn beside the .2h, so the "
                              "owned-province list cannot be established")
        return data
    parsed = T.parse(trn)
    nation = parsed.nation_id
    owned = [p.province_id for p in parsed.provinces
             if p.owner_nation_id == nation]

    by_province: dict[int, list] = {}
    for row in rows:
        by_province.setdefault(row["province_id"], []).append(row)

    for province_id, entries in sorted(by_province.items()):
        entries.sort(key=lambda r: r["position"])
        units = []
        for row in entries:
            kind = row["kind"] if "kind" in row.keys() else None
            if kind not in ("commander", "troop"):
                if reference_conn is None:
                    result.skipped.append(
                        f"recruitment in {province_id} — unit "
                        f"{row['unit_type_id']} has no stored commander/troop "
                        "kind and no reference database was supplied")
                    units = []
                    break
                kind = _reference_recruit_kind(
                    reference_conn, int(row["unit_type_id"]))
            units.append(h2.RecruitOrder(
                row["unit_type_id"], row["gold"] or 0,
                is_commander=kind == "commander"))
        if not units and entries:
            continue
        try:
            data = h2.set_recruits(data, province_id, units, owned)
        except h2.QueueWriteRefused as exc:
            result.skipped.append(f"recruitment in {province_id} — {exc}")
            continue
        result.written.append(
            f"province {province_id}: "
            f"{sum(unit.is_commander for unit in units)} commander(s), "
            f"{sum(not unit.is_commander for unit in units)} troop(s) queued")

    return data


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """Does this table OR VIEW exist?

    The view half matters: current_battle_intent and current_equipment_intent
    are views, and a check for type='table' alone reported them missing. That
    turned every battle order and every equipment change into a silent no-op
    while recruitment carried on working, which is the most confusing kind of
    breakage — three quarters of a materialisation succeeding.
    """
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,)).fetchone() is not None
