"""The visibility boundary must fail loudly, not degrade.

These tests are written against the leaks that are actually present in the live
save rather than against invented cases, because the real ones are worse than
anything that would have been made up. Province 56 holds 76 enemy units in a
province with no vision; the player confirmed, looking at their screen, that
Ripewoods is not on their map, and it holds 31.

The tests that matter here are the ones asserting something is NOT returned. A
leak does not throw and does not look wrong in the output — it looks like an
assistant that plays well.
"""
import struct
from pathlib import Path

import pytest

from dom6_assistant.agent.visibility import PlayerView, VisibilityError
from dom6_assistant.file_reader.formats import units as U

SAVE = Path.home() / ".dominions6/savedgames/example_game"
OUR_NATION = 61


@pytest.fixture
def view():
    if not SAVE.exists():
        pytest.skip("live save absent")
    return PlayerView(SAVE, OUR_NATION)


def test_ftherlnd_is_refused_by_name(view):
    """The one file that must never be read at play time."""
    with pytest.raises(VisibilityError, match="full-information"):
        PlayerView(SAVE, OUR_NATION, trn_name="ftherlnd")


def test_map_data_files_are_refused():
    """.d6m carries the map's full province data, same problem."""
    if not SAVE.exists():
        pytest.skip("live save absent")
    with pytest.raises(VisibilityError):
        PlayerView(SAVE, OUR_NATION,
                   trn_name="__randommap_example_game.d6m")


def test_own_units_are_only_ours(view):
    """The whitelist, stated as the only thing it has to do."""
    units = view.own_units()
    assert units, "expected our own units"
    assert {u.nation_id for u in units} == {OUR_NATION}


def test_the_file_really_does_contain_foreign_units(view):
    """The leak is real, so the test proving it is closed is worth something.

    If this ever fails it means the .trn stopped carrying foreign records, and
    the filtering above became untestable rather than unnecessary — worth
    knowing either way.
    """
    all_units = U.find_units(view.data)
    foreign = [u for u in all_units if u.nation_id not in (0, OUR_NATION)]
    assert foreign, "no foreign records: the boundary is no longer being tested"


def test_no_foreign_unit_reaches_the_caller(view):
    """Every unit the view yields, by every route it yields them."""
    for u in view.own_units():
        assert u.nation_id == OUR_NATION
    for p in view.own_provinces():
        for u in view.units_in_province(p.province_id):
            assert u.nation_id == OUR_NATION


def test_units_in_a_province_we_cannot_see_are_not_served(view):
    """Scytha: owner 0, population 0, and 76 units of nation 76 in the file.

    This is the case that makes 'it came out of our own .trn' an unsafe
    argument, so it is asserted directly rather than left to the general rule.
    """
    hidden = _provinces_with_foreign_units(view)
    assert hidden, "expected at least one unseen province holding enemy units"
    for pid, count in hidden.items():
        assert not view.units_in_province(pid), (
            f"province {pid} leaked {count} foreign units")


def test_foreign_units_method_refuses_with_a_reason(view):
    with pytest.raises(VisibilityError, match="fuzzy estimate"):
        view.foreign_units()


#: What the province panel showed, read off the screen at TURN 23. Pinned to
#: that snapshot rather than the live save: the estimates move as the game
#: does — Omfolia went 30 to 40 across one turn — so asserting them against
#: whatever turn happens to be current tests the calendar, not the decode.
SCREEN_ESTIMATES = {83: 60, 91: 40, 85: 30, 79: 40}
TURN_23 = Path("knowledge/snapshots/t23-quiet")


def test_enemy_strength_matches_what_the_panel_showed():
    """Four provinces, four numbers, read off the game by the player."""
    if not (TURN_23 / "mid_marignon.trn").exists():
        pytest.skip("turn 23 snapshot absent")
    at_turn_23 = PlayerView(TURN_23, OUR_NATION)
    for province_id, shown in SCREEN_ESTIMATES.items():
        assert at_turn_23.enemy_strength(province_id).enemy_units == shown


def test_the_estimate_changes_as_the_game_does(view):
    """A live estimate is still readable; it simply is not turn 23's number."""
    live = view.enemy_strength(85).enemy_units
    assert isinstance(live, int) and live > 0


def test_instant_shape_change_uses_live_h2_form_not_turn_start_trn():
    """Mambo changes immediately; the `.trn` remains on the old chassis.

    The controlled pair differs only in the embedded unit's type/HP and the
    ignored trailer.  Serving the `.trn` values after the click made the web
    view disagree with the game until the next turn.
    """
    root = Path("knowledge/snapshots/example_game")
    before = root / "t1-auto"
    after = root / "t1-auto-2"
    if not (before / "mid_ermor.2h").exists() or not (after / "mid_ermor.2h").exists():
        pytest.skip("controlled Change Shape snapshots absent")

    def mambo(snapshot: Path):
        observed = PlayerView(snapshot, 54, trn_name="mid_ermor.trn")
        return next(
            commander
            for commander in observed.own_commanders(snapshot / "mid_ermor.2h")
            if commander.commander_id == 124
        )

    serpent = mambo(before)
    human = mambo(after)
    assert (serpent.type_id, serpent.hp) == (654, 32)
    assert (human.type_id, human.hp) == (653, 18)


@pytest.mark.parametrize(
    ("stem", "nation_id", "province_ids"),
    [
        ("mid_ermor", 54, (9, 34)),
        ("mid_marignon", 61, (3, 8, 10)),
    ],
)
def test_sparse_late_game_order_blocks_join_their_adjacent_province(
    stem, nation_id, province_ids
):
    root = Path("knowledge/snapshots/example_game/t47-auto")
    h2_path = root / f"{stem}.2h"
    if not h2_path.exists():
        pytest.skip("controlled dual-human turn 47 snapshot absent")
    observed = PlayerView(root, nation_id, trn_name=f"{stem}.trn")
    assert observed.order_file_province_ids(h2_path.read_bytes()) == province_ids


def test_an_unscouted_province_is_unknown_not_empty(view):
    """Answering 0 is a lie in the direction that gets an army killed.

    Province 56 holds 76 units of nation 76 and we have no vision of it at all.
    """
    with pytest.raises(VisibilityError, match="not scouted"):
        view.enemy_strength(56)


def test_the_estimate_is_not_the_true_count():
    """The served number must be the displayed one, never the roster.

    Citala's panel says 60. If the served figure ever equals the exact number
    of foreign records the file holds for a province, the boundary has been
    crossed by something that still looks like a reasonable answer.
    """
    import struct
    from collections import Counter
    if not (TURN_23 / "mid_marignon.trn").exists():
        pytest.skip("turn 23 snapshot absent")
    observed = PlayerView(TURN_23, OUR_NATION)
    true_counts = Counter()
    for u in U.find_units(observed.data):
        if u.nation_id == OUR_NATION:
            continue
        true_counts[struct.unpack_from(
            "<H", observed.data, u.offset + 4)[0]] += 1
    for province_id in SCREEN_ESTIMATES:
        # The file simply holds no records for these provinces, which is why
        # the estimate has to come from the intelligence record.
        assert true_counts[province_id] == 0


def test_intel_flags_where_we_have_our_own_unit():
    """+16 is 1 exactly where one of our units is standing.

    Citala, Omfolia and Kratas hold our Scout, Assassin and Troubadour; the
    player noted that Kratas — where the Troubadour is — also names the enemy
    commander on the panel, which the others do not.
    """
    if not (TURN_23 / "mid_marignon.trn").exists():
        pytest.skip("turn 23 snapshot absent")
    observed = PlayerView(TURN_23, OUR_NATION)
    for province_id in (83, 85, 91):
        intel = observed.enemy_strength(province_id)
        assert intel.has_our_unit, f"province {province_id}"
        assert observed.units_in_province(province_id)
    for province_id in (75, 79, 94):
        assert not observed.enemy_strength(province_id).has_our_unit
        assert not observed.units_in_province(province_id)


def test_unexplored_provinces_report_unknown_not_empty(view):
    """Population 0 in the file means 'not known', and must not read as 0.

    Ripewoods is the anchor: the player confirmed it is not on their map.
    """
    ripewoods = [p for p in view.provinces() if p.name == "Ripewoods"]
    if not ripewoods:
        pytest.skip("Ripewoods not in this save")
    p = ripewoods[0]
    assert p.population is None, "unknown population leaked as a number"
    assert p.owner_nation_id is None
    assert not p.owner_is_known
    assert p.status == "independent or unexplored"


def test_known_owners_are_still_reported(view):
    """The boundary must not be so strict it blinds us to our own map.

    Emerald Lake reads nation 87 in our file and in the host's, and the player
    can see it. A boundary that hid this would be safe and useless.
    """
    known = [p for p in view.provinces() if p.owner_is_known and not p.is_ours]
    assert known, "no foreign owners visible at all — boundary is too strict"


def test_our_own_provinces_are_fully_visible(view):
    ours = view.own_provinces()
    assert ours, "expected to own provinces"
    for p in ours:
        assert p.owner_nation_id == OUR_NATION
        assert p.is_ours and p.status == "ours"


def test_wrong_nation_is_refused(view):
    """Filtering against the wrong whitelist would look like working software."""
    wrong = PlayerView(SAVE, 999)
    with pytest.raises(VisibilityError, match="refusing"):
        wrong.verify_nation()


def test_right_nation_passes(view):
    view.verify_nation()


#: Pretenders belonging to other nations, present in the .trn name table that
#: `read_commander_names` returns. Named explicitly because this was a real
#: leak in the first version of list_commanders, not a hypothetical one.
RIVAL_PRETENDERS = {"Inberke", "Soggoth", "Tukulti'ninurta", "Frasrutar",
                    "Ahluic"}


def test_commanders_exclude_rival_pretenders(view):
    """The .trn name tables span every nation, so the naive read leaks.

    The run containing our own Sugaar also contains Xibalba's Ahluic. This
    asserts the specific names, because a count-based check would pass just as
    happily on the wrong ten commanders.
    """
    names = {c.name for c in view.own_commanders()}
    assert names, "expected our commanders"
    assert not (names & RIVAL_PRETENDERS), (
        f"leaked rival pretenders: {sorted(names & RIVAL_PRETENDERS)}")


def test_the_raw_table_really_does_contain_rivals(view):
    """Proves the filter above is load-bearing rather than decorative."""
    from dom6_assistant.file_reader.formats import commanders as C
    raw = set(C.read_commander_names(view.data).values())
    assert raw & RIVAL_PRETENDERS, (
        "the raw name table no longer holds rival pretenders — the commander "
        "filter is now untested rather than unnecessary")


def test_no_commander_in_our_orders_file_is_dropped(view):
    """Every block in our own .2h is ours, minus the province false positives.

    Requiring a match against the .trn name table silently dropped Urraca,
    Clodius and Guarlan — three of thirteen — because their entries do not sit
    in a run the name-table scanner recognises. A commander the assistant
    cannot see is one that never gets an order all game, which is a failure
    that looks like nothing at all.
    """
    from dom6_assistant.orders import orders_2h as O
    h2 = view.trn_path.with_suffix(".2h")
    if not h2.exists():
        pytest.skip("no .2h")
    blocks = O.find_order_blocks(h2.read_bytes())
    provinces = {p.province_id: p.name for p in view.provinces()}
    expected = {cid for cid, b in blocks.items()
                if provinces.get(cid) != b.commander_name}
    assert {c.commander_id for c in view.own_commanders()} == expected
    assert {"Urraca", "Clodius", "Guarlan"} <= {c.name for c in view.own_commanders()}


def test_a_commander_with_no_name_table_entry_still_joins_to_stats(view):
    """The `.2h` handle works even when the run-based name scan misses."""
    by_name = {c.name: c for c in view.own_commanders()}
    clodius = by_name.get("Clodius")
    if clodius is None:
        pytest.skip("Clodius not in this save")
    assert clodius.province_id == 85 and clodius.type_id == 428
    # Age is NOT pinned: this reads the live save, and he has a birthday every
    # twelve turns. The join is what the test is for, and the join is what hp
    # and a plausible age demonstrate.
    assert clodius.hp == 20
    assert 20 <= clodius.age <= 60
    assert "runtime-index join" in clodius.location_basis


def test_exactly_one_of_our_commanders_is_the_pretender(view):
    ours = view.own_commanders()
    pretenders = [c for c in ours if c.is_pretender]
    assert len(pretenders) == 1, f"expected one pretender, got {pretenders}"
    assert pretenders[0].name == "Sugaar"


def test_province_names_are_not_returned_as_commanders(view):
    """Province and commander id spaces collide; 86 is both.

    Matching on id alone produced a commander called "Copper Canyons" ordered
    to province 19208. Matching on (id, name) is what prevents it.
    """
    names = {c.name for c in view.own_commanders()}
    province_names = {p.name for p in view.provinces() if p.name}
    assert not (names & province_names), (
        f"province names leaked into commanders: {sorted(names & province_names)}")


def test_commanders_are_placed_by_the_troops_they_lead():
    """The warband token at name_end+4 matches a follower's own -4.

    Anchored on what the player read off the Army Setup screen: Floredee leads
    5 Knights of the Chalice and their 5 Destriers, Sugaar 15 and 15. That
    correction is the reason this test exists — an earlier attempt clustered
    units by file adjacency and reported 3 and 17.
    """
    if not (TURN_23 / "mid_marignon.trn").exists():
        pytest.skip("turn 23 snapshot absent")
    # This assertion belongs to the turn on which the Army Setup observation
    # was made. Sugaar later moved back to Marignon, so the live save is no
    # longer evidence for both commanders sharing Copper Canyons.
    at_turn_23 = PlayerView(TURN_23, OUR_NATION)
    by_name = {c.name: c for c in at_turn_23.own_commanders()}
    floredee, sugaar = by_name.get("Floredee"), by_name.get("Sugaar")
    if not (floredee and sugaar):
        pytest.skip("Floredee or Sugaar not in this save")
    assert floredee.troops == 10, "5 knights + 5 destriers"
    assert sugaar.troops == 30, "15 knights + 15 destriers"
    assert floredee.province_name == sugaar.province_name == "Copper Canyons"


def test_all_sparse_squad_slots_contribute_to_location_and_troop_count():
    """The first u32 is slot 0, not the commander's only warband token.

    On turn 23 Bruise has only sparse slot 1, while Turgis has slots 0 and 1.
    Reading only +4 left Bruise unplaced and counted only 4 of Turgis's 17
    troops. The complete five-record slot table resolves both exactly.
    """
    if not (TURN_23 / "mid_marignon.trn").exists():
        pytest.skip("turn 23 snapshot absent")
    at_turn_23 = PlayerView(TURN_23, OUR_NATION)
    by_name = {c.name: c for c in at_turn_23.own_commanders()}
    bruise, turgis = by_name["Bruise"], by_name["Turgis"]
    assert (bruise.province_name, bruise.troops) == ("Marignon", 1)
    assert (turgis.province_name, turgis.troops) == (
        "The Obsidian Waste", 17)
    assert "1 occupied squad" in bruise.location_basis
    assert "2 occupied squad" in turgis.location_basis
    assert "runtime-index join" in bruise.location_basis


def test_troopless_commanders_have_exact_locations(view):
    """The stat join closes the old 13-of-18 location gap."""
    troopless = [c for c in view.own_commanders() if c.troops == 0]
    assert troopless
    assert all(c.province_id is not None for c in troopless)
    assert all(c.type_id is not None for c in troopless)
    assert all("runtime-index join" in c.location_basis for c in troopless)


def test_every_commander_location_comes_from_the_exact_stat_join(view):
    for c in view.own_commanders():
        assert c.province_id is not None, c
        assert c.type_id is not None, c
        assert c.unit_instance_id is not None, c
        assert c.hp and c.age
        assert c.location_basis.startswith("exact .2h/.trn runtime-index join")


def test_battle_report_roster_is_not_returned_as_live_state():
    snapshot = Path("knowledge/snapshots/t38-kratas-resolved")
    if not (snapshot / "mid_marignon.trn").exists():
        pytest.skip("turn-38 battle snapshot absent")
    battle_turn = PlayerView(snapshot, OUR_NATION)

    units = battle_turn.own_units()
    runtime_indexes = [unit.runtime_index for unit in units]

    assert len(units) == 109
    assert len(runtime_indexes) == len(set(runtime_indexes))
    assert all(
        commander.province_id is not None
        for commander in battle_turn.own_commanders())


def test_a_warband_split_across_provinces_is_not_placed(view):
    """Never observed, but a commander mid-move would produce one.

    Picking the province holding more of them would put a commander somewhere
    they are not, so such a warband is excluded from the mapping entirely.
    """
    placed = view._warband_provinces()
    groups: dict[int, set[int]] = {}
    for u in view.own_units():
        if u.warband != 0xFFFFFFFF:
            groups.setdefault(u.warband, set()).add(u.province_id)
    for token, provinces in groups.items():
        if len(provinces) > 1:
            assert token not in placed


def _provinces_with_foreign_units(view) -> dict[int, int]:
    """Provinces holding enemy records where our file gives us no vision."""
    seen: dict[int, int] = {}
    fogged = {p.province_id for p in view.provinces() if not p.owner_is_known}
    for u in U.find_units(view.data):
        if u.nation_id in (0, OUR_NATION):
            continue
        pid = struct.unpack_from("<H", view.data, u.offset + 4)[0]
        if pid in fogged:
            seen[pid] = seen.get(pid, 0) + 1
    # Any is enough. An earlier version required five, which held while a
    # 76-unit army sat in Scytha and stopped holding the turn that changed.
    # The rule being tested is "none of these reach the caller", and that does
    # not depend on how many there are.
    return seen


def test_a_mercenary_company_is_visible_to_us():
    """Hiring 125 Longdead put none of them in the assistant's own view.

    Our units were filtered on `1 <= home <= 5000` read UNSIGNED. A mercenary
    has no home province and carries a negative sentinel, so -2 read as 65534
    and the whole company was dropped: 125 Longdead and Nergash himself,
    invisible, with nothing reporting a problem. The real discriminator for
    the phantom records that filter exists to stop is `home == 0`.
    """
    save = Path("~/.dominions6/savedgames/example_game")
    trn = save / "mid_marignon.trn"
    if not trn.exists():
        pytest.skip("live save absent")
    view = PlayerView(save, 61, trn_name="mid_marignon.trn")
    units = view.own_units()
    longdead = [u for u in units if u.type_id == 195]
    if not longdead:
        pytest.skip("the mercenary company is no longer in our roster")

    assert len(longdead) == 125
    # they arrived where the winning bid said they would
    assert {u.province_id for u in longdead} == {86}

    commanders = view.own_commanders(save / "mid_marignon.2h")
    nergash = next((c for c in commanders if c.commander_id == 129), None)
    assert nergash is not None, "the company's commander is one of ours"
    assert nergash.province_id == 86
    assert all(c.province_id is not None for c in commanders)


def test_home_zero_records_are_still_refused():
    """The guard's real target, kept: phantoms claiming to be ours.

    Those records read type ids and hit points that both march upwards — the
    signature of an unrelated ascending array being parsed as units — and sat
    in provinces we do not own. No genuine unit of ours has home 0 in any save
    on hand, while every mercenary has a negative one.
    """
    import struct
    from dom6_assistant.file_reader.formats import units as U
    snapshot = Path("knowledge/snapshots/t30-pd-baseline")
    trn = next(iter(snapshot.glob("*.trn")), None) or Path(
        "knowledge/snapshots/t30-merc-bid-747/mid_marignon.trn")
    if not trn.exists():
        pytest.skip("snapshot absent")
    data = trn.read_bytes()
    phantoms = [u for u in U.find_units(data)
                if u.nation_id == 61
                and struct.unpack_from("<h", data, u.offset + 6)[0] == 0]
    assert phantoms, "this snapshot is the one that has them"

    view = PlayerView(trn.parent, 61, trn_name=trn.name)
    visible = {u.instance_id for u in view.own_units()}
    assert not any(p.instance_id in visible for p in phantoms)
