"""Research points produced per turn, checked against our own game.

The rule is 2 points per non-Holy magic path level plus 5 per researcher.
Twenty-one consecutive turns of the Marignon save reproduce exactly, which is
what distinguishes this from the several plausible formulas that fit one turn.
"""
import struct
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.agent.visibility import PlayerView
from dom6_assistant.file_reader.formats import trn as T
from dom6_assistant.orders.orders_2h import find_order_blocks
from dom6_assistant.reference import research_rate as RR

SNAPSHOTS = Path("knowledge/snapshots")
GAME_DB = Path("knowledge/game.sqlite3")
REFERENCE_DB = Path("knowledge/reference/reference.sqlite3")


def test_the_documented_shape():
    rate = RR.researcher_rate(1, "test", {"F": 3, "H": 2})
    assert rate.path_levels == 3            # Holy excluded
    assert (rate.base, rate.from_paths) == (5, 6)
    assert rate.total == 11


def test_holy_levels_never_count():
    """Our Witch Hunters carry H1 that has never contributed."""
    assert RR.researcher_rate(1, "x", {"H": 3}).total == 5
    assert RR.path_levels({"H": 9}) == 0


def test_drain_cannot_push_a_researcher_negative():
    heavy = RR.researcher_rate(1, "x", {"F": 1}, magic_scale=-20)
    assert heavy.total == 0


def test_decoded_chassis_and_item_modifiers_are_composed():
    rate = RR.researcher_rate(
        1, "penalized scholar", {"F": 3}, magic_study=-8,
        item_bonuses={"Owl Quill": 6})
    assert rate.from_unit == -8
    assert rate.from_items == 6
    assert rate.total == 9


def test_inspiring_researcher_is_a_per_researcher_province_term():
    rate = RR.researcher_rate(
        1, "inspired mage", {"A": 2}, inspiring_researcher=2)
    assert rate.from_inspiring_researcher == 2
    assert rate.total == 11


def test_binary_reference_identifies_every_inspiring_researcher_source():
    if not REFERENCE_DB.exists():
        pytest.skip("reference database absent")
    import sqlite3

    connection = sqlite3.connect(REFERENCE_DB)
    try:
        sources = dict(connection.execute(
            "SELECT id, inspiringres FROM units "
            "WHERE COALESCE(inspiringres, 0) != 0"))
    finally:
        connection.close()
    assert sources[251] == 1  # Great Sage, controlled below
    assert len(sources) == 12


def test_caelum_control_matches_inspiring_researcher_one_exactly():
    root = SNAPSHOTS / "example_game_2"
    states = [root / "t1-auto-3", root / "t2-auto", root / "t3-auto"]
    if not GAME_DB.exists() or not all(
        (state / "early_caelum.trn").exists() for state in states
    ):
        pytest.skip("controlled Inspiring Researcher sequence absent")

    totals = []
    rates = []
    for state in states:
        session = open_session(
            "example_game_2::early_caelum", save_dir=state, nation_id=24)
        try:
            result = session.call("get_research", {})["result"]
            totals.append(result["research_points"])
            rates.append(result["points_per_turn"])
            assert result["points_per_turn_exact"] is True
            assert result["inspiring_researcher_sources"] == [{
                "commander_id": 99,
                "commander": "Anyc",
                "unit_type_id": 251,
                "province_id": 8,
                "value": 1,
            }]
        finally:
            session.close()

    assert rates == [61, 75, 89]
    assert [totals[1] - totals[0], totals[2] - totals[1]] == rates[:2]


@pytest.mark.parametrize(
    ("experience", "stars"),
    [
        (0, 0),
        (14, 0),
        (15, 1),
        (49, 1),
        (50, 2),
        (99, 2),
        (100, 3),
        (199, 3),
        (200, 4),
        (399, 4),
        (400, 5),
        (999, 5),
    ],
)
def test_experience_star_thresholds_add_one_research_each(experience, stars):
    assert RR.experience_stars(experience) == stars
    rate = RR.researcher_rate(1, "veteran", {"F": 1}, experience=experience)
    assert rate.experience == experience
    assert rate.experience_stars == stars
    assert rate.from_experience == stars
    assert rate.total == 7 + stars


def test_drain_immune_researcher_ignores_drain_penalty():
    ordinary = RR.researcher_rate(
        1, "ordinary", {"F": 1}, magic_scale=-3)
    mundane = RR.researcher_rate(
        2, "mundane", {"F": 1}, magic_scale=-3, drain_immune=True)

    assert ordinary.from_scale == -3
    assert mundane.from_scale == 0
    assert mundane.total == ordinary.total + 3


def test_a_non_mage_gets_no_scale_bonus():
    """The scale applies to mages; a pathless commander is unaffected."""
    assert RR.researcher_rate(1, "x", {}, magic_scale=3).total == 5


#: (turn of the .2h, snapshot holding it, expected points produced that turn).
#: Each is a turn where only one save exists, so the submitted researcher list
#: is unambiguous.
CONFIRMED = [
    (9, "t10", 11), (11, "t12", 18), (12, "t13", 25), (14, "t15", 32),
    (15, "t15-misc", 24), (16, "t16-uprising", 24), (17, "t17-equipped", 24),
    (18, "t18-singleround", 7), (19, "t19-equipment", 7),
    (21, "t21-scouted", 7), (22, "t22-battle", 7),
]


@pytest.mark.parametrize(("turn", "snapshot", "expected"), CONFIRMED)
def test_reproduces_the_observed_research(turn, snapshot, expected):
    """Sum the researchers in that turn's orders and match the game."""
    folder = SNAPSHOTS / snapshot
    h2_path, trn_path = folder / "mid_marignon.2h", folder / "mid_marignon.trn"
    if not (h2_path.exists() and trn_path.exists()):
        pytest.skip(f"{snapshot} absent")
    data = h2_path.read_bytes()
    assert struct.unpack_from("<I", data, 14)[0] == turn

    parsed = T.parse(trn_path)
    view = PlayerView(folder, parsed.nation_id, trn_name="mid_marignon.trn")
    commanders = {c.commander_id: c for c in view.own_commanders(h2_path)}

    produced = 0
    for cid, block in find_order_blocks(data).items():
        if block.order_name != "research":
            continue
        commander = commanders.get(cid)
        if commander is None:
            continue
        produced += RR.researcher_rate(
            cid, commander.name, commander.paths).total
    assert produced == expected


def test_the_level_cost_table_matches_the_published_one():
    """Levels 5-9 were extrapolated and wrong at every one of them.

    `25n^2 - 25n + 50` fits levels 1-3, which is why it survived, and gives
    550 against 700 at level 5 and 1850 against 8100 at level 9.
    """
    from dom6_assistant.file_reader.formats import trn as T
    published = {1: 50, 2: 100, 3: 200, 4: 400,
                 5: 700, 6: 1300, 7: 2400, 8: 4400, 9: 8100}
    for level, cost in published.items():
        assert T._LEVEL_COST(level) == cost


def test_beyond_level_nine_is_refused_rather_than_extrapolated():
    """Extrapolation is exactly what was wrong here before."""
    from dom6_assistant.file_reader.formats import trn as T
    with pytest.raises(ValueError, match="no research cost is known"):
        T._LEVEL_COST(10)


def test_magic_and_drain_are_not_symmetric():
    """Magic needs friendly dominion; Drain applies regardless."""
    friendly = RR.researcher_rate(1, "x", {"F": 3}, magic_scale=3)
    hostile = RR.researcher_rate(1, "x", {"F": 3}, magic_scale=3,
                                 friendly_dominion=False)
    drained = RR.researcher_rate(1, "x", {"F": 3}, magic_scale=-3,
                                 friendly_dominion=False)
    assert friendly.total == 14
    assert hostile.total == 11        # the Magic bonus does not apply
    assert drained.total == 8         # the Drain penalty still does
