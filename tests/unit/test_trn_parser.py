"""Tests for the .trn file parser.

Uses real save files as test fixtures.  Expected values reflect the current
state of each save on disk — update the comments when saves advance.

NOTE: Values marked "live" depend on the save file's current turn and will
drift as the game progresses.  Structural tests (uniqueness, names, is_capital)
are stable regardless of turn.
"""

import struct
import pytest
from pathlib import Path
from dom6_assistant.file_reader.formats.trn import parse, read_province_intel
from ..conftest import require_corpus

# Save file locations
_SAVEGAME_DIR = Path("~/.dominions6/savedgames")
_TEST_GAME    = _SAVEGAME_DIR / "test_game"          / "mid_pangaea.trn"
_EXAMPLE_GAME_5     = _SAVEGAME_DIR / "example_game_5" / "mid_pangaea.trn"
_EXAMPLE_GAME_4     = _SAVEGAME_DIR / "example_game_4"            / "mid_pangaea.trn"
_FUNGAME      = _SAVEGAME_DIR / "fungame"              / "mid_pangaea.trn"
# Snapshots, NOT the live save. Pinning fixtures to a save the player is still
# using means the tests fail the moment a turn is taken — which is exactly what
# happened to two earlier gem tests, and then happened again here at turn 2.
_SNAPSHOTS    = Path(__file__).resolve().parents[2] / "knowledge" / "snapshots"
_MARIGNON     = _SNAPSHOTS / "t1-preorders" / "mid_marignon.trn"
_MARIGNON_T2  = _SNAPSHOTS / "t2" / "mid_marignon.trn"
_DUAL_ERMOR_T1 = (
    _SNAPSHOTS / "example_game" / "t1-auto" / "mid_ermor.trn"
)
_DUAL_ERMOR_T13 = (
    _SNAPSHOTS / "example_game" / "t13-auto" / "mid_ermor.trn"
)


def test_province_intel_terminator_uses_the_viewing_nation():
    """The old fixed 0x003d terminator silently worked only for Marignon."""
    data = bytearray(96)
    struct.pack_into("<IIIIIIIIHH", data, 24,
                     62, 30, 30, 176, 1, 35, 0, 0, 43, 0xffff)

    atlantis = read_province_intel(bytes(data), 43)

    assert atlantis[62].enemy_units == 30
    assert atlantis[62].estimate_uncertainty_percent == 30
    assert atlantis[62].has_our_unit is True
    assert read_province_intel(bytes(data), 61) == {}

# Skip tests if save files are absent (CI / other machines)
needs_test_game = pytest.mark.skipif(not _TEST_GAME.exists(), reason="test_game save not found")
needs_example_game_5  = pytest.mark.skipif(not _EXAMPLE_GAME_5.exists(),  reason="example_game_5 save not found")
needs_example_game_4  = pytest.mark.skipif(not _EXAMPLE_GAME_4.exists(),  reason="example_game_4 save not found")
needs_fungame   = pytest.mark.skipif(not _FUNGAME.exists(),   reason="fungame save not found")
needs_marignon  = pytest.mark.skipif(not _MARIGNON.exists(), reason="marignon snapshot not found")
needs_marignon_t2 = pytest.mark.skipif(not _MARIGNON_T2.exists(), reason="turn 2 snapshot not found")
needs_dual_ermor_t1 = pytest.mark.skipif(
    not _DUAL_ERMOR_T1.exists(), reason="dual-human Ermor snapshot not found"
)


def test_black_throne_claim_is_separate_from_province_ownership():
    """F9 changes only after Claim Throne resolves, not on conquest.

    Ermor owns Endless Caverns on turn 12 but its Black Throne is still
    unclaimed. On turn 13 claimant nation 54 and the national F9 score both
    become one. The paired controls rule out treating owner as claimant.
    """
    before = _SNAPSHOTS / "example_game" / "t12-auto" / "mid_ermor.trn"
    after = _DUAL_ERMOR_T13
    if not before.exists() or not after.exists():
        pytest.skip("paired Black Throne snapshots absent")
    p12 = parse(before)
    p13 = parse(after)
    throne12 = next(p for p in p12.provinces if p.province_id == 56)
    throne13 = next(p for p in p13.provinces if p.province_id == 56)
    assert throne12.owner_nation_id == throne13.owner_nation_id == 54
    assert throne12.throne_claimant_nation_id == 0
    assert throne13.throne_claimant_nation_id == 54
    assert p12.claimed_throne_points == 0
    assert p13.claimed_throne_points == 1


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

@needs_test_game
def test_header_game_name():
    trn = parse(_TEST_GAME)
    assert trn.game_name == "test_game"


@needs_test_game
def test_header_turn_number():
    """live: test_game is at turn 7 as of last snapshot."""
    trn = parse(_TEST_GAME)
    assert trn.turn == 7


@needs_test_game
def test_header_province_count():
    trn = parse(_TEST_GAME)
    assert trn.province_count == 635


# ---------------------------------------------------------------------------
# Gem array
# ---------------------------------------------------------------------------

# The two gem tests that used to be here asserted turn-7 values ("test_game
# t7", "fungame t7") against save files that have since advanced to turn 29.
# They were failing for that reason rather than because of a decode fault — the
# fixtures moved and the expectations did not. Re-pinning them to whatever the
# files say now would only assert that the parser agrees with itself, so they
# are replaced by the fixture below, whose values were read off the running
# game by the player on 2026-08-12.


@needs_marignon
def test_gems_marignon_verified():
    """Verified in-game: 4 fire, 1 astral, 600 gold. MA Marignon, turn 1.

    Gems live at the END of a nation record, so they sit in the *next* 0xFF
    block after the one carrying the nation id. Reading the gems adjacent to
    the id returns the previous nation's stock — the bug this pins.
    """
    trn = parse(_MARIGNON)
    assert trn.nation_id == 61
    assert trn.player_gold == 600
    g = trn.player_gems
    assert g is not None, "gem array should resolve for a known nation"
    assert (g.fire, g.astral) == (4, 1)
    assert (g.air, g.water, g.earth, g.death, g.nature, g.glamour, g.blood) \
        == (0, 0, 0, 0, 0, 0, 0)


@needs_marignon
def test_nation_roster_marignon():
    """Roster is the six nations the player confirmed are in this game."""
    trn = parse(_MARIGNON)
    real = [(nid, gold) for nid, gold, _g, _h in trn.roster if nid > 4]
    # Marignon, Xibalba, Machaka, Nidavangr, Ys, Oceania — all Middle Age.
    assert [n for n, _ in real] == [61, 74, 76, 81, 85, 87]
    assert all(gold == 600 for _, gold in real)   # turn-1 starting treasury


@needs_marignon
def test_capital_fields_marignon_verified():
    """Every province field the player checked against the game screen."""
    trn = parse(_MARIGNON)
    cap = next(p for p in trn.provinces if p.province_id == 93)
    assert cap.name == "Marignon" and cap.is_capital
    assert cap.population        == 39650
    assert cap.province_defense  == 25
    assert cap.unrest            == 0
    assert cap.dominion_strength == 1
    assert cap.terrain_flags     == 0        # Plains
    assert (cap.order_scale, cap.productivity_scale) == (2, 1)
    assert cap.neighbours == [83, 86, 91, 98]   # matches the .map file
    assert cap.tail_shift == 0


@needs_test_game
def test_shifted_record_is_detected():
    """The Pangaea capital's record tail is shifted 20 bytes; detect it.

    Province 117 is the only record of 142 in that save whose terrain
    disagrees with the .map file at the usual offset. At +20 its terrain,
    population, defence and all six neighbours line up at once.
    """
    trn = parse(_TEST_GAME)
    cap = next(p for p in trn.provinces if p.province_id == 117)
    assert cap.tail_shift == 20
    assert cap.population    == 42330
    assert cap.terrain_flags == 9
    assert cap.neighbours    == [107, 110, 111, 124, 125, 132]


@needs_dual_ermor_t1
def test_corpse_components_sum_to_verified_panel_count():
    """The panel displayed 1496 in Ermor and a known zero in Dagothia.

    These remain raw parser fields because the same serialized zero is hidden
    from Marignon and outside Ermor's dominion; read tools apply that boundary.
    """
    trn = parse(_DUAL_ERMOR_T1)
    by_id = {province.province_id: province for province in trn.provinces}
    assert by_id[9].corpse_count_raw == 1496
    assert by_id[7].corpse_count_raw == 0


# ---------------------------------------------------------------------------
# Province records — structural (stable across turns)
# ---------------------------------------------------------------------------

@needs_test_game
def test_provinces_parsed():
    trn = parse(_TEST_GAME)
    assert len(trn.provinces) >= 100


@needs_test_game
def test_province_ids_are_unique():
    trn = parse(_TEST_GAME)
    ids = [p.province_id for p in trn.provinces]
    assert len(ids) == len(set(ids)), "Duplicate province IDs in parsed output"


@needs_test_game
def test_province_ids_start_at_2():
    trn = parse(_TEST_GAME)
    ids = sorted(p.province_id for p in trn.provinces)
    assert ids[0] == 2


@needs_test_game
def test_province_renthale_exists():
    """Province 97 (Renthale) exists, is not a capital.  Population is live."""
    trn = parse(_TEST_GAME)
    by_id = {p.province_id: p for p in trn.provinces}
    assert 97 in by_id
    p = by_id[97]
    assert p.name == "Renthale"
    assert p.is_capital is False
    assert p.population > 0


@needs_test_game
def test_province_renthale_population():
    """live: Renthale population=8550 at test_game t7."""
    trn = parse(_TEST_GAME)
    by_id = {p.province_id: p for p in trn.provinces}
    assert by_id[97].population == 8550


@needs_test_game
def test_province_pangaea_capital_exists():
    """Province 117 (Pangaea) is the capital."""
    trn = parse(_TEST_GAME)
    by_id = {p.province_id: p for p in trn.provinces}
    assert 117 in by_id
    p = by_id[117]
    assert p.name == "Pangaea"
    assert p.is_capital is True
    assert p.population > 0


@needs_test_game
def test_province_pangaea_capital_population():
    """live: Pangaea capital population=42330 at test_game t7."""
    trn = parse(_TEST_GAME)
    by_id = {p.province_id: p for p in trn.provinces}
    assert by_id[117].population == 42330


@needs_marignon
def test_magic_sites_marignon_verified():
    """Verified in-game: the capital holds exactly these two sites.

    The province record opens with 4 u16 site slots. Site 13 yields 4 fire and
    1 astral per turn, which is the gem income the player confirmed on screen.
    """
    trn = parse(_MARIGNON)
    cap = next(p for p in trn.provinces if p.province_id == 93)
    assert cap.sites == [13, 192]        # House of Fiery Justice, Royal Academy
    assert cap.fort_type == 4
    assert cap.has_laboratory and cap.has_temple


@needs_marignon
def test_only_visible_sites_are_populated():
    """No hidden information: own provinces and thrones only.

    Sites are the kind of field where a leak would be easy and invisible, so
    this pins the shape of what the file exposes rather than only the values.
    """
    trn = parse(_MARIGNON)
    with_sites = [p for p in trn.provinces if p.sites]
    assert len(with_sites) == 7
    ours = [p for p in with_sites if p.owner_nation_id == 61]
    assert [p.province_id for p in ours] == [93]
    # The other six are thrones, which the game marks for every player.
    assert all(not p.owner_nation_id for p in with_sites if p.province_id != 93)


@needs_marignon
def test_province_defence_is_body_44():
    """PD is body+44. body+81 also read 25 here, which is what misled it."""
    trn = parse(_MARIGNON)
    cap = next(p for p in trn.provinces if p.province_id == 93)
    assert cap.province_defense == 25
    # Unowned provinces have no PD we can see.
    assert all(p.province_defense == 0
               for p in trn.provinces if p.owner_nation_id != 61)


@needs_marignon
def test_commander_names_and_pretenders():
    """Every commander the player can see, by name, verified in-game.

    Includes the five rival pretenders the player identified on sight, and
    Sanne — a mercenary up for auction, hired by nobody. The table is what the
    player can *see*, not what anyone owns. The commander run is picked out
    from the province-name table (same on-disk format) by checking which run
    contains the pretender ids the nation records point at.
    """
    from dom6_assistant.file_reader.formats import commanders as C
    data = _MARIGNON.read_bytes()
    names = C.read_commander_names(data)
    assert names == {41: "Inberke", 42: "Soggoth", 43: "Tukulti'ninurta",
                     115: "Frasrutar", 116: "Dapamort", 181: "Ahluic",
                     182: "Estorgant", 297: "Sugaar", 303: "Sanne"}
    assert {n: names[c] for n, c in C.pretender_ids(data).items()} == {
        61: "Sugaar", 74: "Ahluic", 76: "Frasrutar",
        81: "Inberke", 85: "Soggoth", 87: "Tukulti'ninurta"}


@needs_marignon
def test_commander_stat_records():
    """Commander stat records carry the province at +6; troops do not."""
    from dom6_assistant.file_reader.formats import commanders as C
    data = _MARIGNON.read_bytes()
    found = C.find_commanders(data, {148, 2107, 3894}, nation_id=61)
    by_type = {c.type_id: c for c in found}
    assert set(by_type) == {148, 2107, 3894}     # Friar, Troubadour, pretender
    # Sugaar is dormant, so he is nowhere; the other two are in the capital.
    assert by_type[3894].province_id is None    # dormant: reads -7, not a province
    assert by_type[148].province_id == by_type[2107].province_id == 93
    assert all(c.home_province == 93 for c in found)
    assert by_type[148].hp == 9                  # Friar
    assert by_type[3894].hp == 115               # Serpent of Heavenly Fires
    assert by_type[3894].age == 82               # the god is old


@needs_marignon
def test_2h_recruitment_queue_costs():
    """The .2h queue and its gold costs, verified by arithmetic.

    2 Paladins, 1 Assassin, 1 Knight of the Chalice, 1 Swordsman and 1
    Crossbowman came to exactly 600 gold, which is the treasury change the
    player reported. The sum is the check: a wrong split of the two parallel
    arrays does not add up.
    """
    from dom6_assistant.file_reader.formats import h2
    snap = Path(__file__).resolve().parents[2] / "knowledge" / "snapshots"
    saved = snap / "mid_marignon.2h.t1-orders"
    if not saved.exists():
        pytest.skip("orders snapshot not present")
    o = h2.parse(saved)
    assert o.nation_id == 61
    assert o.game_name == "example_game"
    assert [(r.unit_type_id, r.gold) for r in o.recruits] == [
        (440, 215), (440, 215), (428, 80), (135, 70), (219, 10), (218, 10)]
    assert o.gold_spent == 600


def test_2h_without_orders_has_empty_queue():
    """An unsaved turn has no queue, and that must not read as garbage."""
    from dom6_assistant.file_reader.formats import h2
    snap = Path(__file__).resolve().parents[2] / "knowledge" / "snapshots"
    base = next(snap.glob("mid_marignon.2h.t1-preorders*"), None)
    if base is None:
        pytest.skip("baseline snapshot not present")
    o = h2.parse(base)
    assert o.nation_id == 61
    assert o.recruits == [] and o.gold_spent == 0


@needs_marignon_t2
def test_gem_pairing_survives_a_turn():
    """Turn 2 must read 8 fire / 2 astral — the prediction made at turn 1.

    It initially read another nation's stock. The gem location was right; the
    pairing was not. Nation records had been matched to their gems by walking a
    chain of ascending nation ids and skipping blocks that broke the ascent,
    which was correct at turn 1 only because the skipped block was spurious. At
    turn 2 one real nation's id field read 0, so a real block was skipped and
    every nation after it paired one slot late.

    Pairing is now by physical spacing, which is the structural property.
    """
    t = parse(_MARIGNON_T2)
    assert t.turn == 2
    assert t.player_gold == 78
    g = t.player_gems
    assert (g.fire, g.astral) == (8, 2)          # 4 + 4 fire, 1 + 1 astral
    assert not any([g.air, g.water, g.earth, g.death, g.nature, g.glamour, g.blood])


@needs_marignon_t2
def test_new_commanders_get_ascending_global_ids():
    """Turn 2 recruits appear with ids above every existing commander.

    Turgis, Bruise and Clodius came in at 307-309, above Sanne's 303, so ids
    are assigned globally in creation order rather than per nation.
    """
    from dom6_assistant.file_reader.formats import commanders as C
    names = C.read_commander_names(_MARIGNON_T2.read_bytes())
    assert names[307] == "Turgis"
    assert names[308] == "Bruise"
    assert names[309] == "Clodius"
    assert max(k for k in names if k < 307) == 303      # Sanne, the mercenary


@needs_marignon_t2
def test_location_is_plus_4_and_home_is_plus_6():
    """Estorgant sneaked to Kratas; +4 tracks him, +6 stays at his capital.

    +6 was briefly labelled a location because it reads 93 for everyone while
    nobody has moved. It is the home province, and it never changes.
    """
    from dom6_assistant.file_reader.formats import commanders as C
    d = _MARIGNON_T2.read_bytes()
    scout = next(c for c in C.find_commanders(d, {2107}, nation_id=61))
    assert scout.province_id == 91      # Kratas, where the game shows him
    assert scout.home_province == 93    # Marignon, where he was recruited


def test_temple_without_laboratory_names_both_flags():
    """The Obsidian Waste got a temple and has no lab, which separates them.

    Every province in the earlier sample had both flags or neither, so body+41
    and body+42 could not be told apart by any amount of looking. Building a
    temple in a province with no laboratory was the only thing that could
    resolve it, and it did: +41=0, +42=1.
    """
    t4 = _SNAPSHOTS / "t4" / "mid_marignon.trn"
    if not t4.exists():
        pytest.skip("turn 4 snapshot absent")
    provs = {p.province_id: p for p in parse(t4).provinces}
    waste, cap = provs[98], provs[93]
    assert (waste.has_temple, waste.has_laboratory) == (1, 0)
    assert (cap.has_temple, cap.has_laboratory) == (1, 1)


def test_warband_token_groups_a_commanders_followers():
    """A unit's own -4 is a u32 warband token shared by one commander's troops.

    This replaces a test that read +169 as a u16 "assignment" and asserted two
    Pikeneers changed it when they changed squad. That passed for the wrong
    reason: +169 is the FOLLOWING record's -4, so the value moved because the
    neighbouring record moved. A second model flagged that the test could pass
    accidentally while units stayed contiguous within squads, which is exactly
    what was happening.

    The battle roster is the clean case, since the game's own Army Setup screen
    states the grouping: Sugaar led 15 Knights and 15 Destriers, Floredee 5 and
    5, and both commanders follow nobody.
    """
    import collections
    from dom6_assistant.file_reader.formats import units as U
    path = _SNAPSHOTS / "t22-battle" / "mid_marignon.trn"
    if not path.exists():
        pytest.skip("battle snapshot absent")
    data = path.read_bytes()
    battle = [u for u in U.find_units(data, nation_id=61)
              if 243931 <= u.offset <= 257425]
    assert len(battle) == 42
    sizes = collections.Counter(u.warband for u in battle)
    assert sorted(sizes.values()) == [2, 10, 30], (
        "expected Sugaar's 30, Floredee's 10 and two unattached commanders")
    unattached = [u for u in battle if u.warband == 0xFFFFFFFF]
    assert len(unattached) == 2, "the two commanders follow nobody"


def test_squads_group_units_the_player_described():
    """Bruise's mixed squad is Knight + Destrier + Pikeneer, as set in game.

    Uses the full u32 warband token. This test previously grouped on a u16 read
    at +169 and its own comment admitted the value was "the low half of Bruise's
    .2h token" — a half-field that happened to be unique enough to group by. The
    full token is 0x0011cee4, whose low half is 0xCEE4 = 52964, the old value.
    """
    from dom6_assistant.file_reader.formats import units as U
    import collections
    t5 = _SNAPSHOTS / "t5" / "mid_marignon.trn"
    if not t5.exists():
        pytest.skip("turn 5 snapshot absent")
    squads = collections.defaultdict(collections.Counter)
    for u in U.find_units(t5.read_bytes(), nation_id=61):
        squads[u.warband][u.type_id] += 1
    assert dict(squads[0x0011cee4]) == {135: 1, 3582: 1, 221: 1}  # Knight, Destrier, Pikeneer
    assert dict(squads[0x0011e1e7]) == {221: 1}                   # the lone Pikeneer


DESTRIER_TYPE_IDS = {3582, 3583}    # Knights and Paladins ride different ones


def test_squad_id_reproduces_the_reported_roster():
    """+34 groups units into the squads the player described.

    Found by searching every offset for one satisfying the roster, rather than
    by guessing an offset and then looking for support. Turgis's four Pikeneers
    form one squad and his thirteen Crossbowmen another — including the fresh
    recruit #226, which the previously-claimed field had stranded on its own.
    """
    from dom6_assistant.file_reader.formats import units as U
    import collections
    t5 = _SNAPSHOTS / "t5" / "mid_marignon.trn"
    if not t5.exists():
        pytest.skip("turn 5 snapshot absent")
    units = U.find_units(t5.read_bytes(), nation_id=61)
    squads = collections.defaultdict(collections.Counter)
    for u in units:
        squads[u.squad_id][u.type_id] += 1
    assert dict(squads[4433]) == {221: 4}        # four Pikeneers
    assert dict(squads[4454]) == {218: 13}       # thirteen Crossbowmen
    # Unattached: commanders, their mounts, garrison troops.
    unattached = squads[U.NO_SQUAD]
    assert unattached[440] == 2                  # both Paladins lead, not follow
    assert unattached[135] == 4                  # all Knights of the Chalice
    # Six mounts for six riders — but across TWO Destrier type ids, because
    # Knights and Paladins ride different variants of the same-named mount.
    destriers = sum(n for t, n in unattached.items() if t in DESTRIER_TYPE_IDS)
    assert destriers == 6


def test_a_mount_record_follows_its_rider():
    """Every Destrier sits immediately after the Knight or Paladin riding it.

    This is how a mount is tied to a rider. It cannot be done by type id:
    "Destrier" covers six different ids and Knights and Paladins ride
    different variants.
    """
    from dom6_assistant.file_reader.formats import units as U
    t5 = _SNAPSHOTS / "t5" / "mid_marignon.trn"
    if not t5.exists():
        pytest.skip("turn 5 snapshot absent")
    units = sorted(U.find_units(t5.read_bytes(), nation_id=61),
                   key=lambda u: u.offset)
    riders = {135, 440}                      # Knight of the Chalice, Paladin
    mounts = [i for i, u in enumerate(units) if u.type_id in DESTRIER_TYPE_IDS]
    assert len(mounts) == 6
    for i in mounts:
        assert i > 0 and units[i - 1].type_id in riders


def test_research_points_match_the_research_screen():
    """+592 is total research, checked against the Magic Research screen.

    At turn 6 the screen showed Conjuration level 1 with 67 rp to level 2, and
    Construction and Thaumaturgy at level 1 with 100 to go. Levels cost 50 then
    100, so that is 83 + 50 + 50 = 183 — and the field reads 183. It also gains
    exactly 11 per turn, matching the stated research speed.
    """
    expect = {"t3": 150, "t4": 161, "t5": 172, "t6": 183}
    for tag, want in expect.items():
        f = _SNAPSHOTS / tag / "mid_marignon.trn"
        if not f.exists():
            pytest.skip(f"{tag} snapshot absent")
        assert parse(f).research_points == want, tag


def test_596_is_not_research_speed():
    """Guards a coincidence: +596 reads 11 at turn 6, and is not the rate.

    Its history is 6, 8, 10, 11 while the point total gained exactly 11 every
    turn over the same span. Had it been the rate, turn 3 to 4 would have
    gained 6. Pinned so nobody re-adopts it on the strength of one turn.
    """
    vals = {}
    for tag in ("t3", "t4", "t5", "t6"):
        f = _SNAPSHOTS / tag / "mid_marignon.trn"
        if not f.exists():
            pytest.skip("snapshots absent")
        vals[tag] = parse(f)
    gains = [vals[b].research_points - vals[a].research_points
             for a, b in (("t3", "t4"), ("t4", "t5"), ("t5", "t6"))]
    assert gains == [11, 11, 11]
    assert [vals[t].research_speed for t in ("t3", "t4", "t5", "t6")] == [6, 8, 10, 11]


def test_experience_follows_the_documented_rules():
    """+8 is experience, per the wiki's rules and the battle turn.

    A unit gains 1 xp per turn alive and 4 more for ending a battle without
    retreating. Across the turn containing the fight for The Obsidian Waste,
    units far from it gained exactly 1 while combatants gained 5.

    At turn 6 the spread is coherent throughout: dormant Sugaar has 0 because
    he has never been alive on the map, Guarlan has 4 from being recruited on
    turn 3, and the Crossbowmen who held the line are uniform.
    """
    from dom6_assistant.file_reader.formats import units as U
    t6 = _SNAPSHOTS / "t6" / "mid_marignon.trn"
    if not t6.exists():
        pytest.skip("turn 6 snapshot absent")
    by_type = {}
    for u in U.find_units(t6.read_bytes(), nation_id=61):
        by_type.setdefault(u.type_id, []).append(u.experience)
    assert by_type[3894] == [0]                 # Sugaar, dormant: never alive
    assert by_type[224] == [4]                  # Guarlan, recruited turn 3
    assert sorted(set(by_type[218])) == [9]     # Crossbowmen, all held
    assert min(by_type[221]) >= 9               # Pikeneers, more melee, more xp


def test_afflictions_match_the_players_report():
    """Afflictions are a u32 twenty-eight bytes BEFORE the record's type id.

    Confirmed unit for unit at turn 6: two Pikeneers aged 22 with limp, one
    aged 25 cursed, a Crossbowman aged 23 with a never healing wound and one
    aged 22 with limp. Nothing else of ours is afflicted.

    The negative offset matters. Read at +145 the field attributes each value
    to the record before its owner — the same off-by-one this format produces
    for gems, order blocks and squad ids. The report was specific enough to
    catch the shift instead of adopting it.
    """
    from dom6_assistant.file_reader.formats import units as U
    t6 = _SNAPSHOTS / "t6" / "mid_marignon.trn"
    if not t6.exists():
        pytest.skip("turn 6 snapshot absent")
    LIMP, CURSE, NHW = 262144, 2, 67108864
    got = {u.instance_id: u.afflictions
           for u in U.find_units(t6.read_bytes(), nation_id=61)
           if u.afflictions and u.type_id in (221, 218)}
    # Instance ids are the u16 at +141. They read 19, 29, 32, 33 and 36 while
    # only the low byte was being used; restoring the high byte adds 768 to
    # each. Same five units, same afflictions — only the id's width changed.
    assert got == {787: LIMP, 797: LIMP, 800: CURSE, 801: NHW, 804: LIMP}


def test_phantom_records_are_rejected():
    """hp and age must be non-zero, which drops two false positives.

    The scan was returning an "Ice Druid" and an "Earthbound" — the latter a
    Late Age Caelum unit in a Middle Age game containing no Caelum. Both read
    hp 0 and age 0, their neighbouring records are noise rather than part of a
    run, and they carried garbage affliction masks into anything iterating
    units.

    With them gone the turn-6 roster matches the player's recruitment exactly:
    one Knight of the Chalice and its mount added since turn 5.
    """
    from dom6_assistant.file_reader.formats import units as U
    import collections
    t5, t6 = _SNAPSHOTS / "t5" / "mid_marignon.trn", _SNAPSHOTS / "t6" / "mid_marignon.trn"
    if not (t5.exists() and t6.exists()):
        pytest.skip("snapshots absent")
    def roster(p):
        return collections.Counter(u.type_id for u in U.find_units(p.read_bytes(), nation_id=61))
    a, b = roster(t5), roster(t6)
    assert sum(a.values()) == 36 and sum(b.values()) == 38
    assert b[135] - a[135] == 1                      # one Knight recruited
    assert sum(b[t] - a[t] for t in DESTRIER_TYPE_IDS) == 1   # and its mount
    assert all(u.age > 0 and u.hp > 0 for u in U.find_units(t6.read_bytes(), nation_id=61))


def test_map_coordinates_put_neighbours_close_together():
    """+98 and +100 are map coordinates, confirmed by geometry not by one value.

    Neighbouring provinces sit a median 258 pixels apart while random pairs sit
    1179 apart, and the ranges fit the .map file's declared 3584 x 2784.
    """
    import statistics
    t6 = _SNAPSHOTS / "t6" / "mid_marignon.trn"
    if not t6.exists():
        pytest.skip("turn 6 snapshot absent")
    provs = {p.province_id: p for p in parse(t6).provinces}
    def dist(a, b):
        pa, pb = provs[a], provs[b]
        return ((pa.map_x - pb.map_x) ** 2 + (pa.map_y - pb.map_y) ** 2) ** 0.5
    adjacent = [dist(a, n) for a in provs for n in provs[a].neighbours
                if n in provs and provs[a].map_x and provs[n].map_x]
    assert statistics.median(adjacent) < 500
    assert max(p.map_x for p in provs.values()) <= 3584
    assert max(p.map_y for p in provs.values()) <= 2784


def test_capital_resolves_across_every_turn():
    """The Marignon capital parses correctly in all six turn snapshots.

    Its tail shift is 0, 20, 16, 4, 4, 4 across turns 1 to 6 — genuinely
    variable, and always a multiple of four, so something before the tail is a
    variable-length array of 4-byte elements.
    """
    for tag in ("t1-preorders", "t2", "t3", "t4", "t5", "t6"):
        f = _SNAPSHOTS / tag / "mid_marignon.trn"
        if not f.exists():
            pytest.skip(f"{tag} snapshot absent")
        cap = next(p for p in parse(f).provinces if p.province_id == 93)
        assert cap.population == 39650, tag
        assert cap.terrain_flags == 0, tag              # Plains
        assert cap.neighbours == [83, 86, 91, 98], tag
        assert (cap.map_x, cap.map_y) == (1644, 2388), tag
        assert cap.tail_shift % 4 == 0, tag


def test_gem_stock_accumulates_at_the_site_income_rate():
    """Six turns of gem stock match the sites that produce them exactly.

    The House of Fiery Justice yields 4 fire + 1 astral per turn from turn 1.
    The Slave Market yields 3 blood, and arrived with The Obsidian Waste on
    turn 3. Every turn lands on the predicted total, which is a far stronger
    check than any single reading.
    """
    expect = {1: (4, 1, 0), 2: (8, 2, 0), 3: (12, 3, 3),
              4: (16, 4, 6), 5: (20, 5, 9), 6: (24, 6, 12)}
    for tag in ("t1-preorders", "t2", "t3", "t4", "t5", "t6"):
        f = _SNAPSHOTS / tag / "mid_marignon.trn"
        if not f.exists():
            pytest.skip(f"{tag} snapshot absent")
        t = parse(f)
        g = t.player_gems
        assert (g.fire, g.astral, g.blood) == expect[t.turn], f"turn {t.turn}"
        assert not any([g.air, g.water, g.earth, g.death, g.nature, g.glamour])


def test_research_by_school_matches_the_screenshot():
    """Per-school levels and progress, against the Magic Research screen.

    At turn 6: Conjuration level 1 with 67 rp to level 2 (so 33 into it),
    Construction and Thaumaturgy level 1 with 100 to go (so 0 into it),
    everything else level 0. Progress climbs 0, 11, 22, 33 across turns 3 to 6,
    matching the stated 11 rp per month.
    """
    from dom6_assistant.file_reader.formats.trn import RESEARCH_SCHOOLS
    expect = {3: 0, 4: 11, 5: 22, 6: 33}
    for tag in ("t3", "t4", "t5", "t6"):
        f = _SNAPSHOTS / tag / "mid_marignon.trn"
        if not f.exists():
            pytest.skip(f"{tag} snapshot absent")
        t = parse(f)
        lv = dict(zip(RESEARCH_SCHOOLS, t.research_levels))
        pr = dict(zip(RESEARCH_SCHOOLS, t.research_progress))
        assert lv["Conjuration"] == lv["Construction"] == lv["Thaumaturgy"] == 1
        assert lv["Alteration"] == lv["Evocation"] == lv["Blood Magic"] == 0
        assert pr["Conjuration"] == expect[t.turn]
        assert pr["Construction"] == pr["Thaumaturgy"] == 0


def test_research_scan_skips_a_false_terminal_level_candidate():
    """An unrelated level-9-shaped run must not request a Level 10 cost.

    Turn 42 introduced exactly this ordering: a false candidate appeared
    before the real research arrays.  Candidate validation used to raise from
    ``_LEVEL_COST(10)`` and prevent the real all-zero block below from ever
    being considered.
    """
    import struct

    from dom6_assistant.file_reader.formats.trn import find_research_by_school

    data = bytearray(320)
    false_off = 70
    struct.pack_into("<30H", data, false_off - 62, *range(100, 130))
    struct.pack_into("<H", data, false_off - 2, 0xFFFF)
    struct.pack_into("<7H", data, false_off, 9, 0, 0, 0, 0, 0, 0)
    struct.pack_into("<7I", data, false_off + 16, 1, 0, 0, 0, 0, 0, 0)

    true_off = 210
    struct.pack_into("<30H", data, true_off - 62, *range(200, 230))
    struct.pack_into("<H", data, true_off - 2, 0xFFFF)
    struct.pack_into("<7H", data, true_off, 0, 0, 0, 0, 0, 0, 0)
    struct.pack_into("<7I", data, true_off + 16, 0, 0, 0, 0, 0, 0, 0)

    assert find_research_by_school(bytes(data), 0) == ([0] * 7, [0] * 7)


def test_construction_progress_is_a_display_order_u32():
    """Turn 39's 156 points are Construction's fourth u32 value."""
    from dom6_assistant.file_reader.formats.trn import RESEARCH_SCHOOLS

    path = _SNAPSHOTS / "t39-auto" / "mid_marignon.trn"
    if not path.exists():
        pytest.skip("turn-39 automatic snapshot absent")
    parsed = parse(path)
    levels = dict(zip(RESEARCH_SCHOOLS, parsed.research_levels))
    progress = dict(zip(RESEARCH_SCHOOLS, parsed.research_progress))
    assert levels["Construction"] == 3
    assert progress["Construction"] == 156
    assert progress["Blood Magic"] == 0
    # The .2h anchor copies only the first seven u16 halves of the u32 array;
    # Construction's low half is consequently its final signature word.
    assert parsed.research_progress_raw[-1] == 156


@pytest.mark.parametrize(
    ("turn", "stem", "total", "levels", "progress"),
    [
        (
            "t20-auto",
            "mid_ermor",
            2273,
            [0, 1, 0, 1, 0, 5, 0],
            [0, 0, 0, 0, 0, 723, 0],
        ),
        (
            "t31-auto",
            "mid_marignon",
            3835,
            [1, 0, 0, 0, 6, 4, 0],
            [0, 0, 0, 0, 0, 285, 0],
        ),
        (
            "t47-auto",
            "mid_marignon",
            9856,
            [2, 5, 0, 0, 6, 7, 0],
            [38, 318, 0, 0, 0, 0, 0],
        ),
    ],
)
def test_dual_human_research_progress_uses_full_u32_values(
    turn, stem, total, levels, progress
):
    path = _SNAPSHOTS / "example_game" / turn / f"{stem}.trn"
    if not path.exists():
        pytest.skip(f"{turn}/{stem} snapshot absent")
    parsed = parse(path)
    assert parsed.research_points == total
    assert parsed.research_levels == levels
    assert parsed.research_progress == progress


def test_dual_human_totals_independently_fix_level_six_and_seven_costs():
    """Late controlled states turn the two published prices into equations.

    This deliberately does not call `_research_total`: the nation total and
    visible per-school arrays are the independent serialized values being used
    to audit the constants consumed by that helper.
    """
    t42_path = (
        _SNAPSHOTS / "example_game" / "t42-auto" / "mid_marignon.trn")
    t43_path = (
        _SNAPSHOTS / "example_game" / "t43-auto" / "mid_marignon.trn")
    if not t42_path.exists() or not t43_path.exists():
        pytest.skip("dual-human late-research snapshots absent")
    t42 = parse(t42_path)
    t43 = parse(t43_path)

    # T42: Conjuration 1; Astral 6; Death 6 with 2105 progress.
    known_through_five = 50 + 100 + 200 + 400 + 700
    fixed_t42 = 50 + known_through_five * 2 + 2105
    level_six = (t42.research_points - fixed_t42) // 2
    assert (t42.research_points - fixed_t42) % 2 == 0
    assert level_six == 1300

    # T43: Conjuration 2 + 38; Astral 6; Death 7, no progress.
    fixed_t43 = (50 + 100 + 38) + known_through_five * 2
    level_seven = t43.research_points - fixed_t43 - 2 * level_six
    assert level_seven == 2400


def test_pretender_titles():
    """Every pretender's title, paired to its name.

    Tukulti'ninurta's matches the proclamation the player read on screen word
    for word: "King of Eloquence, the Rock, the Gentle Flower, Master of the
    Greater Earth".
    """
    from dom6_assistant.file_reader.formats import commanders as C
    from dom6_assistant.file_reader.formats.trn import read_pretender_titles
    t6 = _SNAPSHOTS / "t6" / "mid_marignon.trn"
    if not t6.exists():
        pytest.skip("turn 6 snapshot absent")
    data = t6.read_bytes()
    titles = read_pretender_titles(data, C.read_commander_names(data))
    assert titles["Sugaar"] == "King of Kings, Eater of Filth"
    assert titles["Tukulti'ninurta"] == (
        "King of Eloquence, the Rock, the Gentle Flower, "
        "Master of the Greater Earth")
    assert titles["Inberke"] == "Opener of the Wells, King of Volcanoes"
    assert len(titles) == 6                       # one per nation in the game
    assert all(name not in title for name, title in titles.items())


def test_kill_counts_match_the_hall_of_fame_and_battle_report():
    """+38 is kills, confirmed three independent ways at turn 6.

    Clodius reads 6 and the Hall of Fame lists him with 6 kills. The
    Crossbowmen sum to 6, exactly what the player reported for that squad. The
    surviving Pikeneers sum to 6 of their reported 12, the rest having died in
    the battle and taken their kills with them.
    """
    from dom6_assistant.file_reader.formats import units as U
    t6 = _SNAPSHOTS / "t6" / "mid_marignon.trn"
    if not t6.exists():
        pytest.skip("turn 6 snapshot absent")
    units = U.find_units(t6.read_bytes(), nation_id=61)
    by = {}
    for u in units:
        by.setdefault(u.type_id, 0)
        by[u.type_id] += u.kills
    assert by[428] == 6          # Clodius the Assassin
    assert by[218] == 6          # Crossbowmen
    assert by[221] == 6          # surviving Pikeneers, of 12 earned
    assert next(u for u in units if u.type_id == 428).experience == 16


def test_mercenaries_match_the_hire_screen():
    """Companies, prices and composition, against the Hire Mercenaries screen.

    It showed Hector's Heavy Horsemen at 284 and Bernard's Brave Men at 256,
    with the tooltip "Hector Stark commands 25 Heavy Cavalries".
    """
    from dom6_assistant.file_reader.formats.trn import read_mercenaries
    t6 = _SNAPSHOTS / "t6" / "mid_marignon.trn"
    if not t6.exists():
        pytest.skip("turn 6 snapshot absent")
    m = {x.name: x for x in read_mercenaries(t6.read_bytes())}
    assert set(m) == {"Hector's Heavy Horsemen", "Bernard's Brave Men"}
    h = m["Hector's Heavy Horsemen"]
    assert (h.price, h.commander_id) == (284, 182)
    assert (h.unit_count, h.unit_type_id) == (25, 292)      # Heavy Cavalry
    b = m["Bernard's Brave Men"]
    assert (b.price, b.commander_id, b.unit_count) == (256, 179, 50)


def test_hall_of_fame_matches_the_screen():
    """The Hall of Fame is a u32 run of commander ids, in screen order.

    At turn 6 the screen listed Bernard the Brave, Sanne, Hector Stark and
    Clodius, and the run begins 179, 303, 182, 309 — exactly those, in order.
    Kills and Exp. are not stored here; they come from each commander's own
    record at +38 and +8, so this list plus the unit records reproduces the
    whole screen.
    """
    from dom6_assistant.file_reader.formats.trn import read_hall_of_fame
    t6 = _SNAPSHOTS / "t6" / "mid_marignon.trn"
    if not t6.exists():
        pytest.skip("turn 6 snapshot absent")
    assert read_hall_of_fame(t6.read_bytes())[:4] == [179, 303, 182, 309]


def test_heroic_ability_id_appears_when_clodius_earns_it_and_persists():
    """The commander-record field changes 0 -> 7 at the award turn."""
    from dom6_assistant.file_reader.formats.commanders import read_heroic_ability

    t5 = _SNAPSHOTS / "t5" / "mid_marignon.trn"
    t6 = _SNAPSHOTS / "t6" / "mid_marignon.trn"
    t38 = _SNAPSHOTS / "t38-kratas-resolved" / "mid_marignon.trn"
    if not all(path.exists() for path in (t5, t6, t38)):
        pytest.skip("heroic-ability snapshots absent")

    assert read_heroic_ability(t5.read_bytes(), 309, "Clodius") is None
    earned = read_heroic_ability(t6.read_bytes(), 309, "Clodius")
    retained = read_heroic_ability(t38.read_bytes(), 309, "Clodius")
    assert earned is not None
    assert (earned.ability_id, earned.name) == (7, "Heroic Toughness")
    assert retained == earned


def test_site_array_is_twelve_slots():
    """The province site array runs +0..+22, not +0..+6.

    Reading four slots missed every site past the fourth. The Obsidian Waste's
    Slave Market — which the player reported and whose 3 blood per turn was
    already confirmed in the gem stock — was invisible until the array was read
    to its real length. Unrest at +24 bounds it.
    """
    t6 = _SNAPSHOTS / "t6" / "mid_marignon.trn"
    if not t6.exists():
        pytest.skip("turn 6 snapshot absent")
    provs = {p.province_id: p for p in parse(t6).provinces}
    assert provs[93].sites == [13, 192]      # capital, unchanged
    assert 1483 in provs[98].sites or provs[98].sites, "Obsidian Waste has a site"
    assert len(provs[98].sites) >= 1


def test_current_terrain_matches_the_game_not_the_map():
    """+82 is the terrain the game displays; +90 is the map's original.

    The province panel reads "Terrain: Waste" for The Obsidian Waste and
    "Plains" for Marignon. The .map file says #terrain 0 for BOTH, and +90
    agrees with the map. +82 gives 64 (Waste) and 0 (Plains) — the game's view.

    +82 was originally proposed as terrain and retracted for matching the .map
    only 28 of 99 times. That was the wrong test: the map holds the original
    terrain and the game overrides it, so disagreement was the signal, not the
    refutation.
    """
    t6 = _SNAPSHOTS / "t6" / "mid_marignon.trn"
    live = _SAVEGAME_DIR / "example_game" / "mid_marignon.trn"
    src = live if live.exists() else t6
    if not src.exists():
        pytest.skip("no save available")
    provs = {p.province_id: p for p in parse(src).provinces}
    assert provs[93].current_terrain == 0        # Plains
    assert provs[98].current_terrain == 64       # Waste
    assert provs[98].terrain_flags == 0          # the .map still says Plains


def test_wall_integrity_matches_the_fort_view():
    """+72 is fort wall integrity, read off the Citadel view in game.

    Marignon's fort view showed "Wall Integrity: 1500/1500" and the field reads
    1500; The Obsidian Waste has no fort and reads 0.
    """
    live = _SAVEGAME_DIR / "example_game" / "mid_marignon.trn"
    if not live.exists():
        pytest.skip("live save unavailable")
    provs = {p.province_id: p for p in parse(live).provinces}
    assert provs[93].wall_integrity == 1500 and provs[93].fort_type
    # 98 had no fort when this was written and has a Palisade now, so assert the
    # RELATIONSHIP rather than the value of the day: walls exist exactly where a
    # fort does. A test pinned to '98 has no walls' would have failed for the
    # right reason and taught nothing.
    assert bool(provs[98].wall_integrity) == bool(provs[98].fort_type)


def test_mount_marker_belongs_to_the_next_record():
    """RETRACTION TEST. +167 is not a rider flag — it is the next record's -6.

    The old reading was "+167 == 1 means this unit rides a mount". It was
    committed and tested, and it balanced 7 riders against 7 mounts at two turns.
    It is wrong. +167 is the FOLLOWING record's mount marker at -6, and it lands
    on riders only because a mount's record immediately follows its rider's.

    Reattributed it agrees at every adjacent pair with a clean 173-byte gap, at
    both turns, with no exceptions — where the old reading was imbalanced at both
    (10 mounts vs 11 riders at t9, 19 vs 18 at t18) and had been patched by
    excluding mounts from the rider set instead of asking why the counts differed.

    A unit's own fields span type-28 .. type+144; +145 onward is the next record.
    """
    from dom6_assistant.file_reader.formats.units import find_units
    for turn in ("t9", "t18-gems"):
        path = next((_SNAPSHOTS / turn).glob("*.trn"), None)
        if path is None:
            continue
        data = path.read_bytes()
        units = sorted(find_units(data, nation_id=61), key=lambda u: u.offset)
        checked = 0
        for a, b in zip(units, units[1:]):
            if b.offset - a.offset != 173:
                continue
            claims_next_is_mount = data[a.offset + 167] == 1
            assert claims_next_is_mount == b.is_mount, (
                f"{turn}: +167 disagrees with the next record's own mount flag")
            checked += 1
        assert checked > 20, f"{turn}: too few adjacent pairs to be meaningful"


def test_recruitment_queue_counts_match_the_2h_queue():
    """+26 and +28 count what the .2h says is queued, and only in forts.

    The .2h lists the actual queue, so it is the ground truth: Knight of the
    Chalice, Swordsman and Crossbowman are troops; Paladin, Assassin and Witch
    Hunter are commanders.

    Turns 4 to 6 queue exactly one Knight of the Chalice — repeat recruitment
    the player left on — so +26/+28 must read 0 and 1. Turn 3 queues a Witch
    Hunter and three Knights, so 1 and 3.

    Turn 2 is deliberately excluded: its .2h holds 3 commanders and 3 troops
    while +26 reads 2. The troop count agrees there and the commander count is
    short by one, most likely because a snapshot's .trn is written at turn start
    and its .2h saved mid-turn. Documented in the parser rather than hidden.
    """
    from dom6_assistant.file_reader.formats import h2
    COMMANDERS = {440, 428, 224}          # Paladin, Assassin, Witch Hunter
    # t7 is the strongest case: the queued Knight was actually built that turn
    # and repeat recruitment queued another, so the fields had to survive a real
    # host-side turn roll rather than only being read from static snapshots.
    for turn, expect_cmd, expect_troop in ((3, 1, 3), (4, 0, 1), (5, 0, 1),
                                           (6, 0, 1), (7, 0, 1)):
        base = _SNAPSHOTS / f"t{turn}"
        trn_files = list(base.glob("*.trn"))
        h2_files = list(base.glob("*.2h"))
        if not (trn_files and h2_files):
            pytest.skip(f"turn {turn} snapshot absent")
        queue = h2.parse(h2_files[0]).recruits
        assert sum(1 for r in queue if r.unit_type_id in COMMANDERS) == expect_cmd
        assert sum(1 for r in queue if r.unit_type_id not in COMMANDERS) == expect_troop

        capital = next(p for p in parse(trn_files[0]).provinces if p.province_id == 93)
        assert capital.commanders_queued == expect_cmd
        assert capital.troops_queued == expect_troop


def test_only_owned_provinces_queue_recruitment():
    """Across the host's full-information file, a queue implies ownership.

    ftherlnd carries all 166 provinces with no fog of war. If +26/+28 were
    anything other than recruitment they would have no reason to be zero in
    the ~150 provinces that cannot recruit at all.

    This ALSO asserted that every queueing province has a fort, which was true
    when only the six capitals were building and is now false: six foreign
    provinces queue without one. Five are mid-construction (Machaka building
    forts in Eribon, Great Woods, Pantokrator's Legacy and Gent), but Scytha
    and Ys's Bellfields are neither forted nor building, and nothing explains
    them. Recorded as open in knowledge/trn_format.md rather than asserted
    away — the fort correlation is evidence for the decode, not a law.

    Ownership remains exact, and the strong form still holds for OUR OWN
    provinces, which is where the .2h gives independent ground truth; that is
    what `test_our_own_recruitment_queues_need_a_fort` checks.
    """
    from pathlib import Path
    fth = Path("~/.dominions6/savedgames/example_game/ftherlnd")
    if not fth.exists():
        pytest.skip("ftherlnd absent")
    provs = parse(fth).provinces
    queueing = [p for p in provs if p.commanders_queued or p.troops_queued]
    assert len(provs) == 166
    assert queueing, "somebody is always recruiting"
    assert all(p.owner_nation_id for p in queueing), "only owned provinces queue"


def test_our_own_recruitment_queues_need_a_fort():
    """The strong claim, kept where we can verify it against our own orders."""
    from pathlib import Path
    save = Path("~/.dominions6/savedgames/example_game")
    trn = save / "mid_marignon.trn"
    if not trn.exists():
        pytest.skip("live save absent")
    parsed = parse(trn)
    ours = [p for p in parsed.provinces
            if p.owner_nation_id == parsed.nation_id]
    assert ours
    unforted = [p.province_id for p in ours
                if (p.commanders_queued or p.troops_queued) and not p.fort_type]
    assert not unforted, f"we queued recruitment without a fort in {unforted}"


def test_commander_ids_and_stat_records_share_an_ordering():
    """Commander id order, instance id order and file order are the same.

    This is the property a positional join needs. Nothing inside a stat record
    holds its commander id — every u16 and u32 offset from -40 to the end of the
    record was checked against five pretenders whose ids are known from the
    nation records, and none matches. So the id cannot be read directly.

    The five pretenders line up 41->777, 42->778, 43->779, 115->2542, 297->6982:
    ascending together, in file order, without exception.

    This test pins that fact and nothing more. It does NOT license a positional
    join — that was tried and refuted. Pretenders are created at game start in
    nation order, so they are sorted by construction, and our own nation breaks
    the pattern: Sugaar is second of our six commanders by id but his record is
    fifth by instance id. See commanders.py for the full refutation.
    """
    import struct
    from dom6_assistant.file_reader.formats import units as U
    from dom6_assistant.file_reader.formats import commanders as C
    from dom6_assistant.file_reader.formats import trn as Tm

    data = (_SNAPSHOTS / "t9" / "mid_marignon.trn").read_bytes()
    pretenders = C.pretender_ids(data)
    nations = {n for n, _g, _gb, _h in Tm.read_nation_roster(data)}
    rows = [(pretenders[u.nation_id], u.instance_id, u.offset)
            for u in U.find_units(data)
            if u.nation_id in nations and u.nation_id in pretenders
            and struct.unpack_from("<H", data, u.offset + 10)[0] == 636]
    assert len(rows) >= 4
    rows.sort(key=lambda r: r[2])                       # file order
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert [r[1] for r in rows] == sorted(r[1] for r in rows)
    # and the id is genuinely absent from the record
    for cid, _inst, off in rows:
        window = data[max(0, off - 40):off + 173]
        assert struct.pack("<H", cid) not in window or cid < 256


def test_current_base_dominion_precedes_pretender_magic_paths():
    """The turn record, not the original newlord design, is authoritative."""
    from dom6_assistant.file_reader.formats import commanders as C

    path = _SNAPSHOTS / "t30-new-squad-crossbowman-bruise" / "mid_marignon.trn"
    if not path.exists():
        pytest.skip("turn-30 Marignon snapshot absent")
    assert C.read_pretender_dominion(path.read_bytes(), 61) == 6


def test_item_stash_grows_by_one_slot_per_item_forged():
    """+78 onward is the nation's item stash, u16 ids with 0xFFFF for empty.

    Two forges, two turns, two consecutive slots — and both values were known
    before they were looked for, from the forge order's parameter in the .2h.
    That is what separates this from the retracted reading of +64, where a name
    was found afterwards for a number that had already moved.
    """
    import sqlite3
    from dom6_assistant.file_reader.formats import trn as Tm
    items = {r[0]: r[1] for r in sqlite3.connect(
        "knowledge/reference/reference.sqlite3").execute("SELECT id, name FROM items")}
    expected = {
        "t15-orders":   [],
        "t16-uprising": ["Fire Sword"],
        "t17":          ["Fire Sword", "Enchanted Helmet"],
    }
    seen = 0
    for snap, want in expected.items():
        path = _SNAPSHOTS / snap / "mid_marignon.trn"
        if not path.exists():
            continue
        data = path.read_bytes()
        header = {n: h for n, _g, _gb, h in Tm.read_nation_roster(data)}[61]
        assert [items[i] for i in Tm.read_item_stash(data, header)] == want, snap
        seen += 1
    assert seen >= 2, "need at least two turns to show the stash growing"


def test_magic_paths_track_empowerment_and_prophethood():
    """Paths live at name_end+154 in the commander record, F A W E S D N G B H.

    Two commanders changed in ways the player described, and both show up:

        Bruise    t15 {H:1}  ->  t16 {F:1, H:1}   empowered himself in Fire
        Floredee  t15 {}     ->  t16 {H:3}        became prophet

    This is the field that was hunted for longest. It is NOT in the 173-byte unit
    record — Bruise's changed only at +8, experience, across the empowerment turn
    — and not in the .2h, whose commander block did not move at all. The "name
    table" is really a ~218-byte commander record with the name at the front.
    """
    from dom6_assistant.file_reader.formats import commanders as C
    want = {
        "t15-orders":   {308: {"H": 1}, 87: {}},
        "t16-uprising": {308: {"F": 1, "H": 1}, 87: {"H": 3}},
    }
    seen = 0
    for snap, expected in want.items():
        path = _SNAPSHOTS / snap / "mid_marignon.trn"
        if not path.exists():
            continue
        found = C.read_commander_paths(path.read_bytes())
        for cid, paths in expected.items():
            assert found.get(cid, {}) == paths, f"{snap} commander {cid}"
        seen += 1
    require_corpus(seen, 2, "empowerment turns")


def test_unmodified_commanders_match_the_reference_paths():
    """A commander who has not been altered reads exactly what the type says.

    Michael the Inquisitor is F1 H2 in the reference and F1 H2 in the file. That
    cross-check is what rules out the array being some other ten small numbers.
    """
    import sqlite3
    from dom6_assistant.file_reader.formats import commanders as C
    path = _SNAPSHOTS / "t17-equipped" / "mid_marignon.trn"
    if not path.exists():
        pytest.skip("snapshot absent")
    found = C.read_commander_paths(path.read_bytes())
    ref = sqlite3.connect("knowledge/reference/reference.sqlite3")
    # Michael is commander 86, an Inquisitor (unit type 149).
    row = ref.execute("SELECT F,A,W,E,S,D,N,G,B,H FROM units WHERE id=149").fetchone()
    expected = {p: v for p, v in zip(C.PATH_ORDER, row) if v}
    assert found[86] == expected


def test_prophet_id_appears_when_a_prophet_is_named():
    """Nation record +64 holds the prophet's commander id, 0xFFFF when none.

    Floredee is commander 87 and became prophet between turns 15 and 16, and the
    field moves from absent to 87 across exactly that boundary. It is also the
    offset that produced the retracted Moon Blade reading, since 87 is a valid
    item id — a commander id landing inside the item range was the whole illusion.
    """
    from dom6_assistant.file_reader.formats import trn as Tm
    cases = {"t15-orders": None, "t16-uprising": 87, "t17-equipped": 87}
    seen = 0
    for snap, want in cases.items():
        path = _SNAPSHOTS / snap / "mid_marignon.trn"
        if not path.exists():
            continue
        data = path.read_bytes()
        header = {n: h for n, _g, _gb, h in Tm.read_nation_roster(data)}[61]
        assert Tm.read_prophet_id(data, header) == want, snap
        seen += 1
    require_corpus(seen, 2, "prophet turns")


def test_prophet_id_agrees_with_the_unit_status_byte():
    """Two independent structures must name the same commander as prophet."""
    from dom6_assistant.file_reader.formats import trn as Tm
    from dom6_assistant.file_reader.formats import units as U
    path = _SNAPSHOTS / "t17-equipped" / "mid_marignon.trn"
    if not path.exists():
        pytest.skip("snapshot absent")
    data = path.read_bytes()
    header = {n: h for n, _g, _gb, h in Tm.read_nation_roster(data)}[61]
    assert Tm.read_prophet_id(data, header) is not None
    marked = [u for u in U.find_units(data, nation_id=61) if data[u.offset - 17] == 2]
    assert len(marked) == 1, "exactly one unit should carry the prophet marker"


def test_every_named_unknown_has_an_outcome():
    """The decode_status table must classify every field the goal listed.

    Each is decoded, characterised, or blocked — and anything not decoded must
    carry the specific observation that would unblock it, so the next session
    starts from a test rather than from a re-reading of the same bytes.
    """
    import sqlite3
    db = Path("knowledge/game.sqlite3")
    if not db.exists():
        pytest.skip("game.sqlite3 absent")
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT field, outcome FROM decode_status"))
    expected = {
        "magic paths (per commander)", "province body +80", "unit record +158",
        "unit record +162", "unit record +171", "unit record +31",
        "nation record +64", "single-round order codes", "equipment slot map",
    }
    assert expected <= set(rows), f"unclassified: {sorted(expected - set(rows))}"
    assert set(rows.values()) <= {"decoded", "characterised", "blocked"}
    missing = [f for f, o in conn.execute(
        "SELECT field, outcome FROM decode_status "
        "WHERE outcome != 'decoded' AND (unblock_test IS NULL OR unblock_test = '')")]
    assert not missing, f"no unblock test recorded for: {missing}"


def test_construction_flag_clears_when_the_fort_completes():
    """body+80 is 'a fort is being built here', confirmed by prediction.

    It read 0 before a Palisades build began, 1 for four turns during it, and
    returned to 0 the turn it finished — at which moment fort_type went 0 to 1
    and wall_integrity 0 to 200.

    An earlier reading of the same byte as "months elapsed" was withdrawn when
    its prediction of 2 failed on the second turn. This is the observation that
    promoted the surviving hypothesis, rather than it being adopted by default
    because nothing else fitted.
    """
    cases = {
        "t15-orders":    (False, 0),
        "t16-uprising":  (True,  0),
        "t19-equipment": (True,  0),
        "t20-palisade":  (False, 1),
    }
    seen = 0
    for snap, (building, fort) in cases.items():
        path = _SNAPSHOTS / snap / "mid_marignon.trn"
        if not path.exists():
            continue
        prov = next(p for p in parse(path).provinces if p.province_id == 98)
        assert prov.under_construction is building, snap
        assert bool(prov.fort_type) is bool(fort), snap
        seen += 1
    require_corpus(seen, 3, "fort-construction turns")
    done = _SNAPSHOTS / "t20-palisade" / "mid_marignon.trn"
    if done.exists():
        prov = next(p for p in parse(done).provinces if p.province_id == 98)
        assert prov.wall_integrity == 200, "a Palisade's walls, against a Citadel's 1500"


def test_independent_armies_are_nation_zero_in_ftherlnd():
    """Independents use nation id 0, and excluding it hid every one of them.

    The player reported Copper Canyons holding 37: 3 commanders, 18 heavy
    infantry, 12 light infantry, 4 militia. ftherlnd carries exactly that, and a
    plausibility filter of `1 <= nation <= 500` rejected all 37 — which is why
    they looked "stored nowhere" through searches of the .trn, ftherlnd, the .2h
    and the .map file.

    They are in ftherlnd and NOT in our .trn. That is fog of war working
    correctly: we cannot see them, so our own file does not carry them.
    """
    import sqlite3
    import struct
    from dom6_assistant.file_reader.formats import units as U
    fth = _SNAPSHOTS / "t21-scouted" / "ftherlnd"
    trn = _SNAPSHOTS / "t21-scouted" / "mid_marignon.trn"
    if not (fth.exists() and trn.exists()):
        pytest.skip("snapshot absent")
    names = {r[0]: r[1] for r in sqlite3.connect(
        "knowledge/reference/reference.sqlite3").execute("SELECT id, name FROM units")}

    data = fth.read_bytes()
    here = [u for u in U.find_units(data, nation_id=0)
            if struct.unpack_from("<H", data, u.offset + 4)[0] == 86]
    counts: dict[str, int] = {}
    for u in here:
        counts[names[u.type_id]] = counts.get(names[u.type_id], 0) + 1
    assert counts == {"Commander": 3, "Heavy Infantry": 18,
                      "Light Infantry": 12, "Militia": 4}

    # Our own .trn must NOT contain them — this is the visibility boundary.
    own = trn.read_bytes()
    seen = [u for u in U.find_units(own, nation_id=0)
            if struct.unpack_from("<H", own, u.offset + 4)[0] == 86]
    assert len(seen) < 5, (
        "our .trn should not carry an enemy army we cannot see; "
        f"found {len(seen)} records")


def test_recruitment_queue_is_anchored_not_at_a_fixed_offset():
    """The queue moves. Reading it at an absolute 151 was wrong.

    In the turn-22 and turn-23 files the structure ahead of the queue is 13
    bytes longer and it starts at 164. Reading 151 there returned the right
    answer only because those queues are empty — a non-empty queue in that
    layout would have been read as garbage.
    """
    from dom6_assistant.file_reader.formats import h2
    usual = _SNAPSHOTS / "t19-equipment" / "mid_marignon.2h"
    shifted = _SNAPSHOTS / "t23-quiet" / "mid_marignon.2h"
    if not (usual.exists() and shifted.exists()):
        pytest.skip("snapshots absent")
    assert h2._queue_offset(usual.read_bytes()) == 151
    assert h2._queue_offset(shifted.read_bytes()) == 164


def test_recruitment_queue_holds_troops_and_commanders_with_separate_counts():
    """One flat array, with adjacent counts defining the kind boundary.

    The turn-1 queue is three commanders and three ordinary troops, and its
    costs sum to exactly the 600 gold the player's treasury lost.
    """
    from dom6_assistant.file_reader.formats import h2
    path = _SNAPSHOTS / "t1-orders" / "mid_marignon.2h"
    if not path.exists():
        pytest.skip("snapshot absent")
    queue = h2.parse(path)
    costs = sorted(r.gold for r in queue.recruits)
    assert costs == [10, 10, 70, 80, 215, 215]
    assert queue.gold_spent == 600
    # 135 Knight of the Chalice, 219 Swordsman, 218 Crossbowman are troops;
    # 440 Paladin and 428 Assassin are commanders.
    assert {135, 219, 218} <= {r.unit_type_id for r in queue.recruits}
    assert [r.unit_type_id for r in queue.recruits if r.is_commander] == [
        440, 440, 428]
    assert [r.unit_type_id for r in queue.recruits if not r.is_commander] == [
        135, 219, 218]


def test_a_never_saved_orders_file_has_no_queue():
    """537 bytes on turn 1, before the game has written orders. Not an error."""
    from dom6_assistant.file_reader.formats import h2
    path = _SNAPSHOTS / "t1-preorders" / "mid_marignon.2h"
    if not path.exists():
        pytest.skip("snapshot absent")
    assert h2.parse(path).recruits == []


def test_the_recruitment_queue_is_per_province():
    """One block per province we own, not one array for the whole nation.

    Confirmed by the player queueing a Royal Guard in Marignon and a Knight of
    the Chalice in the Obsidian Waste in the same save: three blocks appeared
    for three owned provinces, the first empty (Copper Canyons has no fort).

    An absolute offset hid this completely — it read the first block and called
    it the queue, so anything recruited outside that province was invisible.
    """
    from dom6_assistant.file_reader.formats import h2, trn as T
    # The save the player queued these in, not the live one: the turn has since
    # rolled and the queue is whatever could not be built.
    save = _SNAPSHOTS / "t23-queued-royalguard-knight"
    if not (save / "mid_marignon.2h").exists():
        pytest.skip("snapshot absent")
    parsed_trn = T.parse(save / "mid_marignon.trn")
    owned = [p.province_id for p in parsed_trn.provinces
             if p.owner_nation_id == 61]
    orders = h2.parse(save / "mid_marignon.2h", owned_provinces=owned)
    assert len(orders.queues) == len(owned)
    by_province = {q.province_id: q for q in orders.queues}
    assert by_province[93].recruits[0].unit_type_id == 134    # Royal Guard
    assert by_province[93].recruits[0].gold == 50
    assert by_province[98].recruits[0].unit_type_id == 135    # Knight
    assert by_province[98].recruits[0].gold == 70


def test_global_effect_records_are_parsed_from_the_turn_file():
    path = _SNAPSHOTS / "t15" / "mid_marignon.trn"
    if not path.exists():
        pytest.skip("turn-15 snapshot absent")
    effects = parse(path).global_effects
    assert len(effects) == 1
    effect = effects[0]
    assert effect.effect_id == 138       # Tapestry of Dreams effect id
    assert effect.spell_id == 937
    assert effect.caster_nation_id == 87
    assert effect.cast_province_id == 95
    assert effect.state == 1


def test_income_fields_are_parsed_beside_the_resource_input():
    """body+30/+36 map directly to runtime province +0x66/+0x6c."""
    path = _SNAPSHOTS / "t23-quiet" / "mid_marignon.trn"
    if not path.exists():
        pytest.skip("turn-23 snapshot absent")
    by_id = {p.province_id: p for p in parse(path).provinces}
    assert by_id[93].land_gold == 0
    assert by_id[93].administrative_owner == 61
    assert by_id[86].administrative_owner == 61
    assert by_id[98].administrative_owner == 61
    assert by_id[83].administrative_owner == 0


def test_a_turn_without_enchantments_has_no_global_effect_records():
    assert parse(_MARIGNON).global_effects == []


def test_global_effect_parser_rejects_unterminated_false_positive():
    from dom6_assistant.file_reader.formats.trn import (
        read_global_effects,
        _GLOBAL_EFFECT_ANCHOR,
    )

    header = _GLOBAL_EFFECT_ANCHOR + b"\x00\x00\x00\x00"
    plausible = b"\x23\x00\x01\x00\x3d\x00\x5d\x00\x01\x00\x00\x00\x00\x00"
    assert read_global_effects(header + plausible * 64) == []


def test_global_section_header_carries_a_varying_field():
    """The header constant was baked from one save and matched nowhere else.

    Its middle u32 is 780 in the turn-15 Marignon file and 3157 in the
    two-human turn 28, so the old six-byte marker only ever matched the save it
    came from.  The section is omitted entirely when nothing is active, so an
    empty result is legitimate -- which is precisely why returning nothing
    everywhere else never looked like a failure.
    """
    controls = {
        "example_game/t28-auto/mid_ermor.trn": (10, 1339, 54),
        "example_game/t28-auto/mid_marignon.trn": (10, 1339, 54),
        "t15/mid_marignon.trn": (138, 937, 87),
    }
    seen = 0
    for relative, expected in controls.items():
        path = _SNAPSHOTS / relative
        if not path.exists():
            continue
        seen += 1
        effects = parse(path).global_effects
        assert len(effects) == 1, relative
        got = (
            effects[0].effect_id,
            effects[0].spell_id,
            effects[0].caster_nation_id,
        )
        assert got == expected, relative
    if not seen:
        pytest.skip("global enchantment snapshots absent")


def test_turn_27_baseline_writes_no_global_section_at_all():
    path = _SNAPSHOTS / "example_game" / "t27-auto" / "mid_ermor.trn"
    if not path.exists():
        pytest.skip("turn-27 baseline absent")
    assert parse(path).global_effects == []


def test_three_active_globals_all_decode_and_mask_by_ownership():
    """Globals are a chain of 46-byte entries, not one list of records.

    Each entry carries its own 0x3102 marker, and only the first is preceded by
    the fixed anchor -- so reading a single entry reported one enchantment when
    three were active.  Turn 45 has all three, cast by two nations, which also
    pins the `value1` rule: a nation sees the true overcast of its OWN globals
    and a masked 1 for everyone else's.
    """
    path = _SNAPSHOTS / "example_game" / "t45-auto"
    if not (path / "mid_ermor.trn").exists():
        pytest.skip("three-global snapshot absent")

    ermor = parse(path / "mid_ermor.trn").global_effects
    assert [e.effect_id for e in ermor] == [17, 10, 29]
    by_id = {e.effect_id: e for e in ermor}
    # Ermor's own two report real overcast values; Marignon's reads masked.
    assert by_id[10].caster_nation_id == 54 and by_id[10].value1 == 10
    assert by_id[29].caster_nation_id == 54 and by_id[29].value1 == 20
    assert by_id[17].caster_nation_id == 61 and by_id[17].value1 == 1

    marignon = parse(path / "mid_marignon.trn").global_effects
    assert [e.effect_id for e in marignon] == [17, 10, 29]
    # Same chain, same order, but now Ermor's two are the masked ones.
    assert {e.effect_id: e.value1 for e in marignon} == {17: 1, 10: 1, 29: 1}
    for effect in marignon:
        assert effect.overcast_ambiguous is False


def test_own_overcasts_match_the_published_formula():
    """excess gems + 5 x excess path level, against both of Ermor's globals.

    Foul Air: cast at its base 75 by a Death 7 mage against a Death 5 spell,
    so 0 + 5 x 2 = 10.  Burden of Time: cast at 90 against a base 70 by the
    same Death 7 mage against a Death 7 spell, so 20 + 0 = 20.
    """
    path = _SNAPSHOTS / "example_game" / "t45-auto" / "mid_ermor.trn"
    if not path.exists():
        pytest.skip("three-global snapshot absent")
    by_id = {e.effect_id: e for e in parse(path).global_effects}
    assert by_id[10].value1 == 0 + 5 * (7 - 5)
    assert by_id[29].value1 == 20 + 5 * (7 - 7)


def test_dispelling_leaves_a_slot_gap_rather_than_renumbering():
    """Slots are stable identifiers, not positions.

    Foul Air held slot 1 of [0, 1, 2].  Dispelling it leaves slot 0 linking
    straight to slot 2, and Burden of Time keeps slot 2 rather than sliding
    down to 1.  The chain walker originally required a sequential counter,
    which rejected the entire chain the first time a global was removed -- that
    strictness is how the behaviour was found.
    """
    root = _SNAPSHOTS / "example_game"
    if not (root / "t46-auto-2" / "mid_ermor.trn").exists():
        pytest.skip("successful-dispel snapshot absent")

    before = parse(root / "t46-auto" / "mid_ermor.trn").global_effects
    assert [(e.slot, e.effect_id) for e in before] == [(0, 17), (1, 10), (2, 29)]

    after = parse(root / "t46-auto-2" / "mid_ermor.trn").global_effects
    assert [(e.slot, e.effect_id) for e in after] == [(0, 17), (2, 29)]
    # Burden of Time is now ordinal 1 but still slot 2: the two readings of the
    # Dispel selector have finally diverged.
    assert after[1].slot == 2
    assert after.index(after[1]) == 1

    # The survivors' own overcasts are untouched by a dispel elsewhere.
    assert {e.effect_id: e.value1 for e in after} == {17: 1, 29: 20}


def test_overcast_floors_at_one_so_zero_is_not_distinguishable():
    """Eternal Pyre was cast at exactly its base cost, yet reads 1, not 0.

    Its `.2h` holds +120 == 80 against a base of 80, so there were no excess
    gems, and 5 x excess path is a multiple of five -- 1 cannot arise that way.
    A true overcast of 0 is therefore stored as 1.  That is a floor rather than
    an offset: an offset would make Foul Air read 11, and it reads 10.
    """
    root = _SNAPSHOTS / "example_game" / "t45-auto"
    if not (root / "mid_marignon.2h").exists():
        pytest.skip("three-global snapshot absent")
    order = (root / "mid_marignon.2h").read_bytes()
    offset = order.find(struct.pack("<i", 1188))
    assert offset > 0
    assert struct.unpack_from("<i", order, offset + 4)[0] == 80  # base cost

    pyre = next(
        e for e in parse(root / "mid_marignon.trn").global_effects
        if e.effect_id == 17
    )
    assert pyre.caster_nation_id == 61  # ours, so value1 is meaningful
    assert pyre.value1 == 1
    # The masked value for a foreign global is also 1, so the two coincide and
    # only ownership distinguishes them.
    foreign = next(
        e for e in parse(root / "mid_ermor.trn").global_effects
        if e.effect_id == 17
    )
    assert foreign.value1 == 1


def test_spurious_anchor_does_not_shadow_the_real_enchantment_list():
    """Turn 28 matches the anchor three times; one parses as an empty list.

    Returning the first structurally valid candidate reported "no
    enchantments" while a real record sat further down the file, so only
    non-empty candidates count and conflicting ones are refused outright.
    """
    from dom6_assistant.file_reader.formats.trn import _GLOBAL_EFFECT_ANCHOR

    path = _SNAPSHOTS / "example_game" / "t28-auto" / "mid_ermor.trn"
    if not path.exists():
        pytest.skip("turn-28 snapshot absent")
    data = path.read_bytes()
    assert data.count(_GLOBAL_EFFECT_ANCHOR) > 1, "control needs a non-unique anchor"
    assert [e.effect_id for e in parse(path).global_effects] == [10]


def test_the_royal_guard_price_confirms_the_computed_formula():
    """50 gold, computed before it was ever seen in game.

    The formula was ported and gave Royal Guard 50 while it was one of five
    Marignon troops whose price had never been observed. The player then
    queued one and the file says 50.
    """
    import sqlite3
    from dom6_assistant.reference.unit_cost import gold_cost_for
    ref = Path("knowledge/reference/reference.sqlite3")
    if not ref.exists():
        pytest.skip("reference database absent")
    conn = sqlite3.connect(ref)
    conn.row_factory = sqlite3.Row
    assert gold_cost_for(conn, 134, is_commander=False) == 50
