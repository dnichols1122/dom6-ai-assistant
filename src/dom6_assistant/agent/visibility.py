"""The player-visibility boundary.

Everything the assistant is allowed to know passes through this module. It
exists because the files on disk know far more than the player does, and the
difference is not subtle.

**What the files actually contain.** Measured on the live save at turn 23:

    our .trn      366 unit records across 29 nations
    ftherlnd    5,014 unit records across 65 nations

`ftherlnd` is the host's master state — every nation's armies, gold and hidden
movement, with no fog at all. That one is easy: it is never opened here.

The `.trn` is the harder case, because it is *nearly* the player's view and
reads like it should be safe. It is not. Province 56 (Scytha) reads owner 0 and
population 0 in our file — we have no vision of it whatsoever — and carries 76
unit records belonging to nation 76. Province 152 holds 40 units of nation 74 on
the same terms. Province 38 (Ripewoods) holds 31 units of nation 81, and the
player confirmed while looking at their screen that Ripewoods is not on their
map at all. A whole enemy army, in a province the player cannot see, sitting in
the file the client renders from.

So the client is given more than it draws, and "it came out of our own .trn"
is not an argument that the player can see it.

**The rules, and why each is the shape it is.**

*Units: our own nation, or nothing.* Not "units in provinces we can see" —
because even in a province we do have vision of, the game does not show the
player a roster. It shows a fuzzy estimate: the player reported Omfolia as
"about 30 enemy units" while the true count was different. There is no context
in which exact foreign unit records are legitimate, so there is no context in
which this module returns one.

*Province ownership: non-zero is knowledge, zero is ambiguous.* Where our .trn
names an owner it is telling us something the player's map shows — Emerald Lake
reads 87 in our file and in the host's, and the player can see it. Where it
reads 0 the province is either genuinely independent or simply unexplored, and
those are indistinguishable from this field alone. Reporting that ambiguity
honestly leaks nothing; guessing which one it is would.

*Population: absent, not zero.* 77 provinces read population 0 in our file and
a real population in the host's. Zero here means "not known", and passing it
along as a number would hand the assistant a fact that is not merely unknown
but false. It is reported as None.

*Enemy army estimates: the displayed number, never the true one.* The panel
tells the player "The province contains about 60 enemy units", and that exact
figure is stored in a province intelligence record — `enemy_strength` returns
it. Four were checked against the screen and all four agree: Citala 60,
Kratas 40, Omfolia 30, The Dawn Land 40.

That this is served while a unit roster is not is the whole distinction the
module turns on. It is not "is the number in the file" — both are. It is
whether the player has been told. The estimate is what the game chose to show;
the roster is what it chose to withhold, and `foreign_units` still refuses.

A province with no intelligence record raises rather than returning 0. We have
not scouted it, which is a different fact from it being empty, and answering
zero is a lie in the direction that gets an army killed.

That last point is the one to hold on to. Every leak available here comes
disguised as a reasonable convenience — and so does every false reassurance.
"""

from __future__ import annotations

from collections import Counter
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from dom6_assistant.file_reader.formats import commanders as C
from dom6_assistant.file_reader.formats import battles as B
from dom6_assistant.file_reader.formats import messages as M
from dom6_assistant.file_reader.formats import diplomacy as D
from dom6_assistant.file_reader.formats import trn as T
from dom6_assistant.file_reader.formats import units as U
from dom6_assistant.orders import orders_2h as O

#: Files that hold every nation's hidden state. Never opened at play time.
#: `ftherlnd` is the host master; `.d6m` are the map's full province data.
FORBIDDEN_NAMES = frozenset({"ftherlnd"})
FORBIDDEN_SUFFIXES = frozenset({".d6m"})

#: Unit record offset for "province I am currently in". Not on TrnUnit because
#: nothing needed it until now; read here relative to the located record, which
#: keeps the no-absolute-offsets rule intact.
OFF_CURRENT_PROVINCE = 4

#: Where a unit was recruited. Zero only on a foreign unit (fogged) or a
#: false positive — see `own_units`.
OFF_HOME_PROVINCE = 6

#: The token a commander with no followers carries, and a unit following
#: nobody.
NO_WARBAND = 0xFFFFFFFF


class VisibilityError(RuntimeError):
    """Raised when something asks for information the player does not have.

    This is deliberately an error and not an empty result. A tool that quietly
    returns nothing teaches the model that the question was reasonable and the
    board was empty; a tool that refuses teaches it that the question was the
    wrong one. The distinction matters most for the case this project cares
    about — an assistant that has silently been fed enemy positions plays
    beautifully and has learned nothing transferable.
    """


@dataclass(frozen=True)
class ProvinceView:
    """A province as the player sees it, with unknowns as None rather than 0."""

    province_id: int
    name: str | None
    owner_nation_id: int | None  # None when 0: independent OR unexplored
    owner_is_known: bool
    is_ours: bool
    is_capital: bool
    population: int | None  # None when the file reads 0 (= not known)
    unrest: int | None
    province_defense: int | None
    fort_type: int | None
    #: Exact current wall value for our completed forts. It is not exposed for
    #: foreign provinces; stray non-fort records can contain nonzero bytes at
    #: the same offset and are not evidence of a hidden fort.
    wall_integrity: int | None
    has_temple: bool | None
    has_laboratory: bool | None
    #: True while a fort is being built. This is the exact client-visible
    #: body+80 flag, not an estimate of the remaining construction time.
    under_construction: bool | None
    dominion_owner: int | None
    dominion_strength: int | None
    #: Administrative control matters during sieges and ownership transitions.
    #: It is exposed only for our provinces; the corresponding foreign value
    #: is not a fact the province panel promises to the player.
    administrative_owner: int | None
    terrain_flags: int | None
    current_terrain_flags: int | None
    #: Public map-image coordinates. These are useful for strategic geometry;
    #: actual movement legality still comes from the decoded neighbour graph.
    map_x: int
    map_y: int
    #: Site ids serialized into this player's .trn. The client populates these
    #: for discovered sites and public thrones only; hidden sites remain absent.
    site_ids: tuple[int, ...]
    #: Public F9 state. This is deliberately not inferred from current land
    #: ownership: conquering a throne province and claiming its throne are
    #: separate actions.
    throne_claimant_nation_id: int | None
    #: Only for provinces we own. The panel shows a province's scales to its
    #: owner; in someone else's territory the reading is not the player's to
    #: have, and it is also not trustworthy — Citala and Kratas both read +2
    #: while behaving as though Order did not apply to them at all.
    order_scale: int | None
    productivity_scale: int | None
    heat_scale: int | None
    growth_scale: int | None
    luck_scale: int | None
    magic_scale: int | None

    @property
    def status(self) -> str:
        if self.is_ours:
            return "ours"
        if self.owner_is_known:
            return f"nation {self.owner_nation_id}"
        return "independent or unexplored"


@dataclass(frozen=True)
class UnitView:
    """One of our own units. Foreign units never reach this type."""

    instance_id: int
    runtime_index: int
    type_id: int
    nation_id: int
    province_id: int
    #: Recruitment province from the signed +6 field. Mercenaries carry a
    #: negative sentinel because they were never recruited in a province.
    home_province_id: int | None
    is_mercenary: bool
    hp: int
    age: int
    experience: int
    kills: int
    afflictions: int
    squad_id: int
    warband: int
    is_mount: bool
    has_fought: bool
    is_pretender: bool


@dataclass(frozen=True)
class BattleForceView:
    """One side of a battle report the player can open."""

    nation_id: int
    role: str
    total_units: int
    mounts_not_counted: int
    unit_types: tuple[tuple[int, int], ...]
    survivors_total: int
    deaths_total: int
    deaths_by_type: tuple[tuple[int, int], ...]
    kills_by_type: tuple[tuple[int, int], ...]
    routed_survivors_total: int | None


@dataclass(frozen=True)
class BattleExceptionalGroupView:
    """Add-on summary group, conditionally attributable by exact accounting."""

    group_id: int
    total_units: int
    unit_types: tuple[tuple[int, int], ...]
    role: str | None
    survivors_total: int | None
    deaths_total: int | None
    deaths_by_type: tuple[tuple[int, int], ...]
    attribution_exact: bool
    attribution_note: str


@dataclass(frozen=True)
class BattleReportView:
    """Visibility-safe aggregate of one embedded battle roster."""

    province_id: int | None
    source_offset: int
    forces: tuple[BattleForceView, ...]
    roles_decoded: bool
    outcome_exact: bool
    our_losses_total: int | None
    enemy_losses_total: int | None
    kills_by_our_type: tuple[tuple[int, int], ...]
    enemy_losses_by_type: tuple[tuple[int, int], ...]
    winner_nation_id: int | None
    winner_role: str | None
    exceptional_groups: tuple[BattleExceptionalGroupView, ...]
    outcome_note: str


@dataclass(frozen=True)
class DiplomacyView:
    """One F4 relation row, restricted to the current player's knowledge."""

    nation_id: int
    controller: str
    status: str
    contact: bool
    defeated: bool
    waiting_for_response: bool
    nap_phase: str | None
    nap_notice_turns: int | None
    relation_code: int
    reciprocal_code: int


@dataclass(frozen=True)
class CommanderView:
    """One of our commanders. Rival commanders never reach this type.

    The `.2h` block's first post-name u32 joins to the same handle at -32 in
    the commander's `.trn` unit record.  `province_id` and `type_id` therefore
    remain exact even when the commander leads no troops.
    """

    commander_id: int
    name: str
    paths: dict[str, int]
    order: str | None
    destination: int | None
    is_pretender: bool
    type_id: int | None = None
    unit_instance_id: int | None = None
    hp: int | None = None
    age: int | None = None
    experience: int | None = None
    heroic_ability_id: int | None = None
    heroic_ability: str | None = None
    # Raw strategic-order parameter. Unlike destination, zero is meaningful
    # for Fire empowerment and must not be collapsed to None.
    order_parameter: int = 0
    province_id: int | None = None
    province_name: str | None = None
    troops: int = 0
    #: Why the location is what it is, so a None is never mistaken for "nowhere".
    location_basis: str = ""


def _check_path(path: Path) -> Path:
    """Refuse full-information files before they can be read.

    Enforced on the path rather than at each call site, so a future read tool
    cannot reach `ftherlnd` by taking a filename argument. The check is on the
    name because that is what the game fixes it as — the host writes exactly
    one such file per game and never renames it.
    """
    if path.name in FORBIDDEN_NAMES or path.suffix in FORBIDDEN_SUFFIXES:
        raise VisibilityError(
            f"{path.name} is a full-information file holding every nation's "
            "hidden state; it is build-time only and must never be read at "
            "play time. Use the .trn, which is what the player's client sees."
        )
    return path


class PlayerView:
    """Read-only access to a save, filtered to what the player can see.

    Construct it with the save directory and our own nation id. Everything it
    returns is either the player's own information or a province fact their map
    already shows them.
    """

    def __init__(self, save_dir: str | Path, nation_id: int, trn_name: str | None = None) -> None:
        self.save_dir = Path(save_dir)
        self.nation_id = int(nation_id)
        self._trn_path = self.save_dir / trn_name if trn_name else self._find_trn()
        _check_path(self._trn_path)
        self._data: bytes | None = None
        self._parsed: T.TrnFile | None = None
        self._units: list[UnitView] | None = None
        # The `.trn` is immutable for a turn, but the `.2h` is not: equipment,
        # orders and instantaneous actions such as Change Shape rewrite it.
        # Include the file stat in the cache key so a long-lived web session
        # never keeps serving the form that existed before the latest save.
        self._commanders: dict[tuple[str, int, int], list[CommanderView]] = {}
        self._intel: dict[int, T.ProvinceIntel] | None = None
        self._message_records: list[M.MessageRecord] | None = None

    def _find_trn(self) -> Path:
        candidates = sorted(self.save_dir.glob("*.trn"))
        if not candidates:
            raise FileNotFoundError(f"no .trn in {self.save_dir}")
        if len(candidates) > 1:
            raise ValueError(
                f"{len(candidates)} .trn files in {self.save_dir}; name one "
                "explicitly so the wrong nation's turn is not read"
            )
        return candidates[0]

    # -- raw access ------------------------------------------------------

    @property
    def trn_path(self) -> Path:
        return self._trn_path

    @property
    def data(self) -> bytes:
        if self._data is None:
            self._data = _check_path(self._trn_path).read_bytes()
        return self._data

    @property
    def parsed(self) -> T.TrnFile:
        if self._parsed is None:
            self._parsed = T.parse(_check_path(self._trn_path))
        return self._parsed

    @property
    def turn(self) -> int:
        return self.parsed.turn

    @property
    def game_name(self) -> str:
        return self.parsed.game_name

    def verify_nation(self) -> None:
        """Fail if the .trn is not the nation we were told to play.

        A mismatch means every visibility decision below is being made against
        the wrong whitelist, which is the one failure here that would look like
        working software.
        """
        actual = self.parsed.nation_id
        if actual is not None and actual != self.nation_id:
            raise VisibilityError(
                f"{self._trn_path.name} belongs to nation {actual}, not "
                f"{self.nation_id}; refusing to filter against the wrong nation"
            )

    # -- units -----------------------------------------------------------

    def own_units(self) -> list[UnitView]:
        """Every unit belonging to us. The whitelist is applied here, once.

        Cached: locating units is a full scan of a ~190 KB file, and the tools
        above this call it once per question. The view is read-only for the
        duration of a turn, so the cache cannot go stale within one.
        """
        if self._units is not None:
            return self._units
        out: list[UnitView] = []
        for u in self._current_unit_records():
            if u.nation_id != self.nation_id:  # belt and braces
                continue
            # Every unit of ours was recruited somewhere, so its home province
            # is never zero. Turn 24 produced five records that passed every
            # other test and claimed to be ours — type ids 15, 25, 41, 59, 105
            # with hit points 24, 47, 66, 86, 113, both marching upwards, the
            # signature of some other ascending array being read as units. They
            # sat in provinces we do not own. Not one genuine unit of ours has
            # home 0 in any save on hand.
            #
            # This cannot go in the scanner: a FOREIGN unit legitimately reads
            # home 0, because the fog blanks where an enemy was recruited, and
            # filtering there hid the very records the boundary exists to
            # refuse.
            # The discriminator is home == 0 exactly, NOT "outside 1..5000".
            # Read signed: a mercenary carries a NEGATIVE home, because it was
            # never recruited anywhere. Reading unsigned made -2 look like
            # 65534 and rejected it, so hiring Nergash's Damned Legion put 125
            # Longdead and their commander in our army and none of them in the
            # assistant's view of it — 126 units, invisible, with no error.
            #
            # Across every save on hand the split is exact: before the company
            # arrived, our records held 1-7 home-zero phantoms and no negative
            # homes at all; from the turn it arrived, zero phantoms and exactly
            # 126 negatives, which is the company and nothing else.
            home = struct.unpack_from("<h", self.data, u.offset + OFF_HOME_PROVINCE)[0]
            if home == 0 or home > 5000:
                continue
            out.append(
                UnitView(
                    instance_id=u.instance_id,
                    runtime_index=u.runtime_index,
                    type_id=u.type_id,
                    nation_id=u.nation_id,
                    province_id=struct.unpack_from(
                        "<H", self.data, u.offset + OFF_CURRENT_PROVINCE
                    )[0],
                    home_province_id=home if home > 0 else None,
                    is_mercenary=home < 0,
                    hp=u.hp,
                    age=u.age,
                    experience=u.experience,
                    kills=u.kills,
                    afflictions=u.afflictions,
                    squad_id=u.squad_id,
                    warband=u.warband,
                    is_mount=u.is_mount,
                    has_fought=u.has_fought,
                    is_pretender=u.is_pretender,
                )
            )
        self._units = out
        return out

    def _current_unit_records(self) -> list[U.TrnUnit]:
        """Select the live roster region, excluding battle-report copies.

        A turn containing a battle has another ordinary 173-byte unit roster
        near the end of the file.  Participant runtime handles and unit fields
        are convincing there precisely because it is a real roster, but it is
        historical report data rather than current state.  The live region is
        the unit-record cluster that joins the most handles from our current
        `.2h` commander table; a battle cluster contains only its participants.
        Ties select the earlier cluster, as observed for every turn on hand.
        """
        h2_path = self._default_h2()
        if h2_path is None:
            raise VisibilityError(
                "no .2h beside the .trn, so current units cannot be separated "
                "from battle-report rosters"
            )
        order_data = h2_path.read_bytes()
        handles = {
            struct.unpack_from("<I", order_data, block.name_end)[0]
            for block in O.find_order_blocks(order_data).values()
        }
        records = U.find_units(self.data)
        if not records:
            return []

        # A live region contains several nation/squad runs and the plausibility
        # scanner may reject the small headers between them.  Across the corpus
        # the widest such internal gap is eleven record strides; battle rosters
        # begin tens of kilobytes later.  Sixteen strides therefore joins the
        # live sub-runs without merging a later report region.
        clusters: list[list[U.TrnUnit]] = []
        for unit in records:
            if not clusters or unit.offset - clusters[-1][-1].offset > 16 * U.RECORD_SIZE:
                clusters.append([])
            clusters[-1].append(unit)

        candidates: list[tuple[int, int, list[U.TrnUnit]]] = []
        for cluster in clusters:
            own = []
            for unit in cluster:
                if unit.nation_id != self.nation_id:
                    continue
                home = struct.unpack_from("<h", self.data, unit.offset + OFF_HOME_PROVINCE)[0]
                if home == 0 or home > 5000:
                    continue
                own.append(unit)
            matches = sum(unit.runtime_index in handles for unit in own)
            if own:
                candidates.append((matches, -cluster[0].offset, own))
        if not candidates or max(row[0] for row in candidates) == 0:
            raise VisibilityError(
                "no own unit-record region joins the current .2h commander "
                "table; refusing to mix possible battle-report records into "
                "live state"
            )
        return max(candidates, key=lambda row: (row[0], row[1]))[2]

    def units_in_province(self, province_id: int) -> list[UnitView]:
        """Our units in one province. Never anyone else's, even in our own."""
        return [u for u in self.own_units() if u.province_id == province_id]

    def foreign_units(self, *_args: Any, **_kwargs: Any) -> None:
        """Always refuses. Present so the refusal is discoverable, not implicit.

        The records exist and are trivially readable — that is precisely why
        this method is here rather than simply absent. Someone extending this
        class should meet the reason, not an apparent oversight.
        """
        raise VisibilityError(
            "foreign unit records are in the .trn but are not visible to the "
            "player: the game shows a fuzzy estimate ('about 30 enemy units'), "
            "never a roster. Our own .trn holds 76 units of nation 76 in a "
            "province we have no vision of at all."
        )

    def battle_reports(self) -> list[BattleReportView]:
        """Battle summaries the current turn explicitly shows the player.

        Enemy unit records remain forbidden as live intelligence.  A battle
        report is a different visibility class: its starting-force table is
        deliberately shown after combat.  Only aggregate type counts leave
        this method; per-instance enemy HP, experience and afflictions do not.

        The compact summary following each roster gives attacker/defender
        roles, survivors and kills independently for every report.  This is
        why casualties remain attributable when a turn contains two battles;
        the current live roster and its cumulative kill counters are not used.
        """
        # Most turns are quiet. Locate the distinctive two-force roster first
        # so an old/pre-order `.2h` cannot make an empty report call fail while
        # trying to select a live roster it does not need.
        candidate_rosters = B.find_battle_rosters(self.data, self.nation_id)
        if not candidate_rosters:
            return []

        h2_path = self._default_h2()
        if h2_path is None:
            raise VisibilityError(
                "no .2h beside the .trn, so battle copies cannot be separated from the live roster"
            )
        live_records = self._current_unit_records()
        live_offsets = {unit.offset for unit in live_records}
        rosters = [
            roster
            for roster in candidate_rosters
            if not any(unit.offset in live_offsets for unit in roster.units)
        ]

        out: list[BattleReportView] = []
        summaries = B.find_battle_summaries(self.data)
        pairs = B.pair_rosters_and_summaries(rosters, summaries)
        if len(pairs) != len(rosters):
            raise VisibilityError(
                "a visible battle roster has no matching stored summary; "
                "refusing to silently omit or guess that report"
            )
        for roster, raw_summary in pairs:
            summary = B.resolve_battle_summary(raw_summary, roster)
            if summary is None:
                raise VisibilityError(
                    "a stored battle summary does not match its participant "
                    "roster; refusing to guess roles or casualties"
                )

            rows_by_group = {
                group: [row for row in summary.rows if row.group == group]
                for group in range(B.MAX_SUMMARY_GROUP + 1)
            }
            winner = None
            if roster.province_id is not None:
                current = next(
                    (
                        province
                        for province in self.parsed.provinces
                        if province.province_id == roster.province_id
                    ),
                    None,
                )
                current_owner = current.owner_nation_id if current is not None else None
                if current_owner in (summary.attacker_nation_id, summary.defender_nation_id):
                    winner = current_owner
            if winner is None:
                attacker_after = sum(row.count for row in rows_by_group[1])
                defender_after = sum(row.count for row in rows_by_group[3])
                if bool(attacker_after) != bool(defender_after):
                    winner = (
                        summary.attacker_nation_id if attacker_after else summary.defender_nation_id
                    )

            forces: list[BattleForceView] = []
            for role, nation_id, start_group, after_group in (
                ("attacker", summary.attacker_nation_id, 0, 1),
                ("defender", summary.defender_nation_id, 2, 3),
            ):
                records = [unit for unit in roster.units if unit.nation_id == nation_id]
                starts = Counter({row.type_id: row.count for row in rows_by_group[start_group]})
                survivors = Counter({row.type_id: row.count for row in rows_by_group[after_group]})
                deaths = starts - survivors
                kills = Counter(
                    {row.type_id: row.kills for row in rows_by_group[after_group] if row.kills}
                )
                survivors_total = sum(survivors.values())
                forces.append(
                    BattleForceView(
                        nation_id=nation_id,
                        role=role,
                        total_units=sum(starts.values()),
                        mounts_not_counted=sum(unit.is_mount for unit in records),
                        unit_types=tuple(sorted(starts.items())),
                        survivors_total=survivors_total,
                        deaths_total=sum(deaths.values()),
                        deaths_by_type=tuple(sorted(deaths.items())),
                        kills_by_type=tuple(sorted(kills.items())),
                        routed_survivors_total=(
                            survivors_total
                            if winner is not None and nation_id != winner
                            else (0 if winner is not None else None)
                        ),
                    )
                )
            ours = next(force for force in forces if force.nation_id == self.nation_id)
            enemy = next(force for force in forces if force.nation_id != self.nation_id)
            exceptional: list[BattleExceptionalGroupView] = []
            for group in sorted({row.group for row in summary.rows if row.group >= 4}):
                counts = tuple(sorted((row.type_id, row.count) for row in rows_by_group[group]))
                role = None
                survivors_total = None
                deaths_total = None
                deaths_by_type: tuple[tuple[int, int], ...] = ()
                attribution_exact = False
                attribution_note = (
                    "This add-on summary group cannot be assigned to a main "
                    "side from the currently observed records.")

                # In the controlled assassination, group 4 accounts for the
                # exact five kills omitted from the ordinary defender block:
                # the attacking Assassin is credited with six, the main
                # defender block lost one, group 4 starts with five, and no
                # group-5 survivors exist.  This conservation identity proves
                # defender-side attribution and five deaths without guessing
                # whether the client calls the Commoners guards or bystanders.
                if group == 4 and not rows_by_group[5]:
                    attacker = next(force for force in forces
                                    if force.role == "attacker")
                    defender = next(force for force in forces
                                    if force.role == "defender")
                    credited = sum(kills for _type_id, kills
                                   in attacker.kills_by_type)
                    total = sum(count for _type_id, count in counts)
                    if credited == defender.deaths_total + total:
                        role = "defender"
                        survivors_total = 0
                        deaths_total = total
                        deaths_by_type = counts
                        attribution_exact = True
                        attribution_note = (
                            "Exact kill conservation: attacker credited kills "
                            "equal ordinary defender deaths plus every unit in "
                            "this group, and no survivor group is present.")
                exceptional.append(
                    BattleExceptionalGroupView(
                        group_id=group,
                        total_units=sum(count for _type_id, count in counts),
                        unit_types=counts,
                        role=role,
                        survivors_total=survivors_total,
                        deaths_total=deaths_total,
                        deaths_by_type=deaths_by_type,
                        attribution_exact=attribution_exact,
                        attribution_note=attribution_note,
                    )
                )
            losses_by_role = {
                force.role: Counter(dict(force.deaths_by_type))
                for force in forces
            }
            for group in exceptional:
                if group.role is not None and group.deaths_total is not None:
                    losses_by_role[group.role].update(dict(group.deaths_by_type))
            our_losses_by_type = losses_by_role[ours.role]
            enemy_losses_by_type = losses_by_role[enemy.role]
            winner_role = next((force.role for force in forces if force.nation_id == winner), None)
            out.append(
                BattleReportView(
                    province_id=roster.province_id,
                    source_offset=roster.offset,
                    forces=tuple(forces),
                    roles_decoded=True,
                    outcome_exact=winner is not None,
                    our_losses_total=sum(our_losses_by_type.values()),
                    enemy_losses_total=sum(enemy_losses_by_type.values()),
                    kills_by_our_type=ours.kills_by_type,
                    enemy_losses_by_type=tuple(sorted(enemy_losses_by_type.items())),
                    winner_nation_id=winner,
                    winner_role=winner_role,
                    exceptional_groups=tuple(exceptional),
                    outcome_note=(
                        "Stored Battle Summary rows provide each side's starting "
                        "force, deaths, surviving routs and credited kills. "
                        "Each report has its own summary, so multiple battles are "
                        "attributed independently. Add-on group losses are "
                        "included only when exact kill conservation proves a "
                        "main-side relationship."
                    ),
                )
            )
        return out

    def _messages(self) -> list[M.MessageRecord]:
        """Recipient-filtered serialized reports, decoded once per view."""
        if self._message_records is None:
            self._message_records = M.find_message_records(
                self.data, self.nation_id)
        return self._message_records

    def scout_reports(self) -> dict[int, M.MessageRecord]:
        """Persistent province-panel scouting prose keyed by province."""
        return {
            record.province_id: record
            for record in self._messages()
            if not record.is_turn_message and record.province_id is not None
        }

    def turn_messages(self) -> list[M.MessageRecord]:
        """Notifications addressed to this player on the Messages screen.

        The same serialized table contains type-5 scouting reports that feed
        province panels across turns.  Those are already exposed as structured
        estimates by :meth:`intel` and are intentionally not mixed into the
        current turn's notifications here.
        """
        return [
            record
            for record in self._messages()
            if record.is_turn_message
        ]

    def diplomatic_relations(self) -> list[DiplomacyView]:
        """Relations displayed on this player's F4 nations panel.

        The serialized matrix is global, so only our row and the one reciprocal
        cell used by the client are allowed through.  Contact is not a hidden
        flag: the client computes it from borders between the two nations (or
        their disciple-team partners).  For the ordinary non-team case this is
        exactly an adjacency between one of our provinces and a province whose
        foreign owner the map already shows.
        """
        matrix = D.find_relation_matrix(self.data)
        controllers = D.find_controller_states(
            self.data, matrix.participant_nation_ids, before_offset=matrix.offset
        )
        waiting_targets = set(D.find_waiting_targets(self.data, self.nation_id))
        provinces = {province.province_id: province for province in self.parsed.provinces}
        our_team = {
            nation_id
            for nation_id in matrix.participant_nation_ids
            if matrix.code(self.nation_id, nation_id) == D.SAME_GOD
        } | {self.nation_id}

        def has_border(other_nation_id: int) -> bool:
            other_team = {
                nation_id
                for nation_id in matrix.participant_nation_ids
                if matrix.code(other_nation_id, nation_id) == D.SAME_GOD
            } | {other_nation_id}
            for province in provinces.values():
                if province.owner_nation_id not in our_team:
                    continue
                for neighbour_id in province.neighbours:
                    neighbour = provinces.get(neighbour_id)
                    if neighbour is not None and neighbour.owner_nation_id in other_team:
                        return True
            return False

        out: list[DiplomacyView] = []
        for other in matrix.participant_nation_ids:
            if other == self.nation_id:
                continue
            ours = matrix.code(self.nation_id, other)
            reciprocal = matrix.code(other, self.nation_id)
            defeated = controllers[other] == "defeated"
            phase, notice = D.nap_state(ours)
            contact = has_border(other)
            if defeated:
                status = "defeated"
            elif ours == D.SAME_GOD:
                status = "same_god"
            elif phase == "active":
                status = f"NAP-{notice}"
            elif phase == "ending":
                status = f"NAP-{notice}*"
            elif other in waiting_targets:
                status = "waiting"
            elif not contact:
                status = "no_contact"
            elif reciprocal > 0:
                status = "war"
            elif reciprocal < 0:
                status = "unknown"
            elif ours > 0:
                # Our row says war and theirs does not. Every transition ever
                # observed wrote both rows together, so this is an unmodelled
                # state rather than a peaceful one, and the one shape an
                # untested auto-flip could plausibly take. Reporting
                # `apprehensive` here would be the worst available answer.
                status = "unknown"
            else:
                status = "apprehensive"
            out.append(
                DiplomacyView(
                    nation_id=other,
                    controller=controllers[other],
                    status=status,
                    contact=contact,
                    defeated=defeated,
                    waiting_for_response=other in waiting_targets,
                    nap_phase=phase,
                    nap_notice_turns=notice,
                    relation_code=ours,
                    reciprocal_code=reciprocal,
                )
            )
        return out

    def order_file_province_ids(self, h2_data: bytes) -> tuple[int, ...]:
        """Owned provinces represented by province-local `.2h` blocks.

        These blocks are sparse in mature saves: turn-47 Ermor owns nine
        provinces but has two blocks, while Marignon owns twenty and has
        three. Each queue anchor lives inside the matching province-name
        record, so join it to the nearest preceding exact ``(id, name)`` pair
        from the visible province table rather than assuming every owned
        province has a block. This also handles a freshly conquered province,
        which can already be owned in `.trn` while absent from inherited `.2h`.
        """
        from dom6_assistant.file_reader.formats import h2

        owned = {
            (province.province_id, province.name): province.province_id
            for province in self.own_provinces()
        }
        province_blocks = sorted(
            (
                block
                for block in O.find_order_blocks(h2_data).values()
                if (block.commander_id, block.commander_name) in owned
            ),
            key=lambda block: block.offset,
        )
        represented: list[int] = []
        for anchor in h2._queue_offsets(h2_data):
            candidates = [
                block
                for block in province_blocks
                if 0 < anchor - block.name_end <= 128
            ]
            if not candidates:
                raise VisibilityError(
                    f"province-local order block at {anchor} has no adjacent "
                    "owned province-name record")
            block = max(candidates, key=lambda candidate: candidate.offset)
            province_id = block.commander_id
            if province_id in represented:
                raise VisibilityError(
                    f"two province-local order blocks resolved to province {province_id}")
            represented.append(province_id)
        return tuple(represented)

    # -- provinces -------------------------------------------------------

    def provinces(self) -> list[ProvinceView]:
        return [self._view(p) for p in self.parsed.provinces]

    def province(self, province_id: int) -> ProvinceView | None:
        for p in self.parsed.provinces:
            if p.province_id == province_id:
                return self._view(p)
        return None

    def own_provinces(self) -> list[ProvinceView]:
        return [p for p in self.provinces() if p.is_ours]

    def _view(self, p: T.TrnProvince) -> ProvinceView:
        owner = p.owner_nation_id
        known = bool(owner)  # 0 = independent or unexplored
        ours = owner == self.nation_id
        # Fields the game only fills in once we have vision. Reported as None
        # rather than 0 so the model cannot read "unknown" as "empty" — the
        # difference between an unscouted province and a depopulated one.
        pop = p.population if p.population else None
        return ProvinceView(
            province_id=p.province_id,
            name=p.name or None,
            owner_nation_id=owner if known else None,
            owner_is_known=known,
            is_ours=ours,
            is_capital=bool(p.is_capital),
            population=pop,
            unrest=p.unrest if known or ours else None,
            province_defense=p.province_defense if ours else None,
            fort_type=p.fort_type,
            wall_integrity=(p.wall_integrity if ours and p.fort_type > 0 else None),
            has_temple=bool(p.has_temple) if p.has_temple is not None else None,
            has_laboratory=(bool(p.has_laboratory) if p.has_laboratory is not None else None),
            under_construction=(bool(p.under_construction) if ours else None),
            dominion_owner=p.dominion_owner,
            dominion_strength=p.dominion_strength,
            administrative_owner=(p.administrative_owner if ours else None),
            terrain_flags=p.terrain_flags,
            current_terrain_flags=p.current_terrain,
            map_x=p.map_x,
            map_y=p.map_y,
            site_ids=tuple(p.sites),
            throne_claimant_nation_id=(
                p.throne_claimant_nation_id
                if p.throne_claimant_nation_id > 0 else None
            ),
            order_scale=p.order_scale if ours else None,
            productivity_scale=p.productivity_scale if ours else None,
            heat_scale=p.heat_scale if ours else None,
            # The serialized Growth/Death byte has the opposite sign from the
            # province-panel convention. Economics already applies this same
            # normalization before invoking the decoded calculators.
            growth_scale=-p.growth_scale if ours else None,
            luck_scale=p.luck_scale if ours else None,
            magic_scale=p.magic_scale if ours else None,
        )

    # -- commanders ------------------------------------------------------

    def own_commanders(self, h2_path: Path | None = None) -> list[CommanderView]:
        """Our commanders, and provably only ours.

        This one needs care, because the obvious route leaks. The `.trn` name
        tables are *not* per-nation: the run holding our own pretender Sugaar
        (297) also holds Xibalba's Ahluic (181), and a neighbouring run holds
        four more rival pretenders — Inberke, Soggoth, Tukulti'ninurta and
        Frasrutar. `read_commander_names` picks whichever run contains the most
        pretender ids, so it returns a cross-nation table by design. Serving it
        hands the model rival pretenders' names and exact magic paths, which is
        a build the player cannot see.

        The `.2h` settles it. That file is *our* orders, so any commander with
        a block in it is ours — and the set it excludes here is exactly the
        five rival pretenders, which is a strong check on the rule rather than
        a hopeful one.

        Two false positives have to be cleared first, both from province names
        sharing the commander table's shape:

        * a run with no pretender in it is a province table, not a commander
          table — that is what makes id 93 "Marignon" a province rather than a
          commander;
        * matching must be on the (id, name) pair, because province id 86 and
          commander id 86 both exist, and matching on id alone yields a
          commander named "Copper Canyons" ordered to province 19208.

        **The stat-record link.** The first u32 after the name in the `.2h`
        block is a runtime-unit handle.  The identical value occurs at -32
        relative to exactly one of our `.trn` unit records.  That record gives
        the commander's type, current province, HP and age.  The join resolves
        every one of the 18 turn-30 commanders, including all troopless ones.
        It was missed while searches looked for the *commander id* in the stat
        record; the file stores this separate handle instead.

        The five `.2h` squad-slot records at `name_end + 4` remain the source
        of troop counts. Each occupied record is
        `(u16 squad_id, u16 marker)`, the same combined u32 token the squad's
        units carry. The marker is per token rather than per player:
        controlled Caelum has simultaneous values 6 and 7 in one commander's
        two squads.

        Verified against what the player reported from the Army Setup screen:
        Floredee's token resolves to 5 Knights of the Chalice and their 5
        Destriers, Sugaar's to 15 and 15 — which is exactly the correction the
        player made when an earlier adjacency-based guess said 3 and 17.

        The invariant that makes it sound: every occupied squad must resolve,
        and all of a commander's resolved squads must be in exactly one
        province. Empty slots are FFFF/FFFF. Combat arrays are not occupancy
        evidence because their bytes survive after a squad is removed.
        """
        h2 = h2_path or self._default_h2()
        if h2 is None or not h2.exists():
            raise VisibilityError(
                "no .2h alongside the .trn, and it is the only file that "
                "identifies which commanders are ours; the .trn name tables "
                "span every nation."
            )
        _check_path(h2)
        # Cached per .2h path. Building this scans the whole .trn for name
        # tables and the whole .2h for order blocks, and record_order calls it
        # once per order — so a batch of ten was ten full scans of a 190 KB
        # file. Keyed by path because a caller may point at a different orders
        # file, and the answer genuinely differs then.
        h2_stat = h2.stat()
        key = (str(h2), h2_stat.st_mtime_ns, h2_stat.st_size)
        if key in self._commanders:
            return self._commanders[key]
        pretenders = set(C.pretender_ids(self.data).values())
        # Province names, to reject the false positives rather than to admit
        # the true ones. Requiring a match against the commander table was the
        # wrong way round: it silently dropped Urraca, Clodius and Guarlan,
        # who are in our .2h and are ours, but whose .trn entries do not sit in
        # a run the name-table scanner recognises. A commander the assistant
        # cannot see is one that never gets an order all game.
        province_names = {p.province_id: p.name for p in self.parsed.provinces}

        order_data = h2.read_bytes()
        blocks = O.find_order_blocks(order_data)
        # A shape change is immediate.  The next-turn `.trn` therefore still
        # carries the form from turn start, while the live `.2h` embedded unit
        # record carries the form and HP the player is looking at right now.
        # Join it by the same runtime handle used for commander attribution.
        h2_units_by_runtime: dict[int, list] = {}
        for unit in O.read_h2_units(order_data, self.nation_id):
            h2_units_by_runtime.setdefault(unit.runtime_index, []).append(unit)
        warbands = self._warband_provinces()
        units_by_runtime: dict[int, list[UnitView]] = {}
        for unit in self.own_units():
            units_by_runtime.setdefault(unit.runtime_index, []).append(unit)
        names = {p.province_id: p.name for p in self.provinces()}

        out: list[CommanderView] = []
        for cid, block in sorted(blocks.items()):
            # The province name table shares this record shape, so id 93 with
            # the name "Marignon" is a province, not a commander called after
            # one. Matching on the PAIR is what distinguishes them — id alone
            # yields a commander named "Copper Canyons" ordered to province
            # 19208.
            if province_names.get(cid) == block.commander_name:
                continue
            runtime_index = struct.unpack_from("<I", order_data, block.name_end)[0]
            stat_matches = units_by_runtime.get(runtime_index, [])
            stat = stat_matches[0] if len(stat_matches) == 1 else None
            live_matches = h2_units_by_runtime.get(runtime_index, [])
            live_stat = live_matches[0] if len(live_matches) == 1 else None
            province = stat.province_id if stat is not None else None
            troops, troop_basis = 0, ""
            try:
                squad_slots = O.read_squad_slots(order_data, block.name_end)
            except ValueError as exc:
                squad_slots = []
                troop_basis = f"squad-slot structure is malformed ({exc}); location unknown"
            tokens = [slot["token"] for slot in squad_slots]
            resolved = [warbands.get(token) for token in tokens]
            if troop_basis:
                pass
            elif not tokens:
                troop_basis = "leads no troops"
            elif any(found is None for found in resolved):
                missing = [token for token, found in zip(tokens, resolved) if found is None]
                troop_basis = (
                    f"squad token(s) {missing} match no unit we can see; troop count may be stale"
                )
            else:
                known = [found for found in resolved if found is not None]
                provinces = {found[0] for found in known}
                if len(provinces) == 1:
                    troop_province = provinces.pop()
                    troops = sum(found[1] for found in known)
                    troop_basis = f"leads {troops} troops in {len(known)} occupied squad slot(s)"
                    if province is not None and troop_province != province:
                        troop_basis += (
                            f"; follower province {troop_province} "
                            f"disagrees with commander province "
                            f"{province}"
                        )
                else:
                    troop_basis = (
                        "occupied squads resolve to multiple provinces; troop count unavailable"
                    )
            if stat is not None:
                live_note = (
                    "; live type/HP overlaid from the embedded .2h unit record"
                    if live_stat is not None else ""
                )
                basis = (
                    f"exact .2h/.trn runtime-index join ({runtime_index})"
                    f"{live_note}; {troop_basis}"
                )
            elif stat_matches:
                basis = (
                    f"runtime-index {runtime_index} matched multiple own "
                    f"unit records; location refused; {troop_basis}"
                )
            else:
                basis = (
                    f"runtime-index {runtime_index} matched no own unit "
                    f"record; location refused; {troop_basis}"
                )
            spec = O.ORDER_SPECS.get(block.order_name)
            destination = (
                block.parameter
                if spec is not None
                and spec.parameter_kind in (O.PARAM_PROVINCE, O.PARAM_PROVINCE_OR_ZERO)
                and block.parameter
                else None
            )
            heroic = C.read_heroic_ability(self.data, cid, block.commander_name)
            out.append(
                CommanderView(
                    commander_id=cid,
                    name=block.commander_name,
                    # The `.2h` is our own record and carries the same path bytes
                    # as the `.trn` commander block. Reading it also covers the
                    # final entries a run-based `.trn` table scan can miss.
                    paths=C.read_paths(order_data, block.name_end),
                    order=block.order_name,
                    destination=destination,
                    is_pretender=cid in pretenders,
                    type_id=(live_stat.type_id if live_stat is not None else
                             stat.type_id if stat is not None else None),
                    unit_instance_id=(stat.instance_id if stat is not None else None),
                    hp=(live_stat.hp if live_stat is not None else
                        stat.hp if stat is not None else None),
                    age=stat.age if stat is not None else None,
                    experience=stat.experience if stat is not None else None,
                    heroic_ability_id=(heroic.ability_id if heroic is not None else None),
                    heroic_ability=(heroic.name if heroic is not None else None),
                    order_parameter=block.parameter,
                    province_id=province,
                    province_name=names.get(province) if province else None,
                    troops=troops,
                    location_basis=basis,
                )
            )
        self._commanders[key] = out
        return out

    def own_pretender_state(self, h2_path: Path | None = None) -> dict[str, Any]:
        """Return the own god's strategic availability without guessing.

        The nation record supplies the stable pretender commander id. A live
        or dormant god has a normal writable `.2h` commander block; killed
        Mambo does not, while every ordinary surviving commander still does.
        An empty/unlocatable table is unknown rather than evidence of death.
        """
        pretender_id = C.pretender_ids(self.data).get(self.nation_id)
        out: dict[str, Any] = {
            "commander_id": pretender_id,
            "status": "unknown",
            "dead": None,
            "name": None,
        }
        if pretender_id is None:
            out["basis"] = "own nation record has no decoded pretender id"
            return out
        h2 = h2_path or self._default_h2()
        if h2 is None or not h2.exists():
            out["basis"] = "no current .2h order table is available"
            return out
        _check_path(h2)
        blocks = O.find_order_blocks(h2.read_bytes())
        if not blocks:
            out["basis"] = (
                "the current .2h commander table could not be located; "
                "dead state is not inferred from an empty parse"
            )
            return out
        block = blocks.get(pretender_id)
        if block is not None:
            out.update({
                "status": "available_or_dormant",
                "dead": False,
                "name": block.commander_name,
                "basis": "pretender id has a writable commander block",
            })
        else:
            out.update({
                "status": "dead",
                "dead": True,
                "basis": (
                    "pretender id is absent from an otherwise valid own "
                    f"commander table containing {len(blocks)} blocks"
                ),
            })
        return out

    def _warband_provinces(self) -> dict[int, tuple[int, int]]:
        """{warband token: (province, troop count)} for warbands in one place.

        A warband split across provinces is excluded rather than guessed at.
        It has not been observed — every linked commander's followers sat in a
        single province in every snapshot checked — but a commander mid-move
        is exactly the case that would produce one, and picking whichever
        province happened to hold more of them would place a commander
        somewhere they are not.
        """
        groups: dict[int, list] = {}
        for unit in self.own_units():
            if unit.warband == NO_WARBAND:
                continue
            groups.setdefault(unit.warband, []).append(unit)
        out: dict[int, tuple[int, int]] = {}
        for token, members in groups.items():
            places = {u.province_id for u in members}
            if len(places) == 1:
                out[token] = (places.pop(), len(members))
        return out

    def _default_h2(self) -> Path | None:
        candidate = self._trn_path.with_suffix(".2h")
        return candidate if candidate.exists() else None

    def intel(self) -> dict[int, T.ProvinceIntel]:
        """Province intelligence, as the panel reports it. Cached per turn."""
        if self._intel is None:
            self._intel = T.read_province_intel(self.data, self.nation_id)
        return self._intel

    def enemy_strength(self, province_id: int) -> T.ProvinceIntel:
        """The displayed estimate for a province — "about N enemy units".

        This is the number on the player's own province panel, not the truth.
        Confirmed against four provinces read off the screen: Citala 60,
        Kratas 40, Omfolia 30, The Dawn Land 40.

        Serving it is legitimate exactly because it is what the game chose to
        show. The true rosters are also in the file and are still refused —
        `foreign_units` has not changed. The distinction this class turns on is
        not "is it in the file" but "has the player been told".

        Raises for a province with no record, because that means we have never
        scouted it. That is a different fact from "it is empty", and answering
        0 would be a lie in the direction that gets an army killed.
        """
        found = self.intel().get(province_id)
        if found is None:
            raise VisibilityError(
                f"no intelligence on province {province_id} — we have not "
                "scouted it. This does not mean it is empty; we simply do not "
                "know. Send a scout."
            )
        return found

    # -- iteration helper ------------------------------------------------

    def __iter__(self) -> Iterator[ProvinceView]:
        return iter(self.provinces())

    def __repr__(self) -> str:
        return (
            f"PlayerView({self._trn_path.name}, nation={self.nation_id}, turn={self.parsed.turn})"
        )
