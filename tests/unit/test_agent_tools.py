"""The tool surface: what it returns, and what it refuses.

Write tests run against a copy of the save and the database, because a test
that records an order into the live game is a test that plays the game.

The refusal tests carry most of the weight here. A tool that accepts a bad call
and does something reasonable-looking is the specific failure this surface is
built to prevent: Dominions resolves the turn either way, so a wrong order
produces a position that makes no sense several turns later with nothing in
between to explain it.
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.agent.registry import (
    Param,
    Tool,
    ToolError,
    ToolRegistry,
    parse_text_call,
)
from dom6_assistant.agent.session import open_session
from dom6_assistant.file_reader.formats import diplomacy as D
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.orders import materialize as M
from dom6_assistant.orders import orders_2h as O
from dom6_assistant.reference import research_rate as RR

SAVE = Path.home() / ".dominions6/savedgames/example_game"
GAME_DB = Path("knowledge/game.sqlite3")
STABLE_LIVE_BASE = Path("knowledge/snapshots/t30-ritual-augury-copper-canyons")


@pytest.fixture
def sandbox(tmp_path):
    """A throwaway copy of the stable turn-30 save and database."""
    source = STABLE_LIVE_BASE if STABLE_LIVE_BASE.exists() else SAVE
    if not source.exists() or not GAME_DB.exists():
        pytest.skip("test save or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_marignon.trn", "mid_marignon.2h"):
        shutil.copy2(source / name, save / name)
    db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, db)
    return save, db


#: Ground truth read off the panel by the player is true for ONE turn. The
#: live save keeps advancing as the game is played, so any test asserting a
#: specific number has to name the turn it came from — three of these broke
#: the day the save reached turn 25 while still being perfectly correct about
#: turn 23. Smoke tests stay on the live save, where "it still runs against
#: whatever the game wrote most recently" is exactly what they are for.
PINNED = Path("knowledge/snapshots/t23-quiet")

#: Snapshots kept for one thing each, because the live save does not hold
#: them at every turn: mercenaries are on offer at 25 and gone by 23, the item
#: stash is empty once everything is worn, and a full spell queue only exists
#: on a turn somebody set one.
MERCS = Path("knowledge/snapshots/t25-mercenaries")
STASH = Path("knowledge/snapshots/t19-equipment")
SPELLS = Path("knowledge/snapshots/t18-gems")
RESEARCH_FULL = Path("knowledge/snapshots/t25-research-full9")
RESEARCH_LEVEL9 = Path("knowledge/snapshots/t25-research-conj4-8-tartarian-ghostriders")
RITUAL_BASELINE = Path("knowledge/snapshots/t30-ritual-before-augury")
CONSTRUCTION_STATE = Path("knowledge/snapshots/t25-research-before")
TROOP_STATE = Path("knowledge/snapshots/t30-ritual-augury-copper-canyons")
GLOBAL_STATE = Path("knowledge/snapshots/t15")
PD_BASELINE = Path("knowledge/snapshots/t30-pd-baseline")
DIPLOMACY_STATE = Path("knowledge/snapshots/t38-diplomacy-baseline")
SHAPE_CHANGED_STATE = Path("knowledge/snapshots/example_game/t1-auto-2")
SHAPE_SERPENT_STATE = Path("knowledge/snapshots/example_game/t1-auto")
INCOMING_NAP_STATE = Path("knowledge/snapshots/example_game/t15-auto")
ACCEPTED_NAP_STATE = Path("knowledge/snapshots/example_game/t15-auto-2")
DECLINED_NAP_STATE = Path("knowledge/snapshots/example_game/t15-auto-3")
DUAL_RESEARCH_STATE = Path("knowledge/snapshots/example_game/t47-auto")
CLAIMED_THRONE_STATE = Path("knowledge/snapshots/example_game/t13-auto")
DEAD_GOD_STATE = Path("knowledge/snapshots/example_game/t46-auto-5")
CALL_GOD_STATE = Path("knowledge/snapshots/example_game/t46-auto-6")
ONGOING_CALL_GOD_STATE = Path("knowledge/snapshots/example_game/t47-auto-3")
REANIMATION_STATE = Path("knowledge/snapshots/example_game/t70-auto")
REANIMATION_FINAL_STATE = Path("knowledge/snapshots/example_game/t70-auto-6")
CAELUM_MULTI_MARKER_STATE = Path(
    "knowledge/snapshots/example_game_2/t3-auto-2")


def _session_on(tmp_path, snapshot):
    if not (snapshot / "mid_marignon.trn").exists() or not GAME_DB.exists():
        pytest.skip(f"{snapshot} or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_marignon.trn", "mid_marignon.2h"):
        shutil.copy2(snapshot / name, save / name)
    db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, db)
    # The source database is the real campaign journal and legitimately gains
    # new turn-30 intents as features are confirmed in game. A pinned save must
    # not inherit those unrelated later intents: they change "first free squad"
    # and make get_orders assertions depend on when the suite was run.
    conn = sqlite3.connect(db)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_intent'"
            )
        ]
        for table in tables:
            conn.execute(f'DELETE FROM "{table}"')
        conn.commit()
    finally:
        conn.close()
    return open_session(game_db=db, save_dir=save)


def test_list_commanders_exposes_current_shape_and_instant_semantics(tmp_path):
    if not (SHAPE_CHANGED_STATE / "mid_ermor.2h").exists() or not GAME_DB.exists():
        pytest.skip("controlled Change Shape snapshot or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_ermor.trn", "mid_ermor.2h"):
        shutil.copy2(SHAPE_CHANGED_STATE / name, save / name)
    db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, db)
    session = open_session(
        "example_game::mid_ermor", game_db=db, save_dir=save)
    try:
        result = session.call("list_commanders", {})
    finally:
        session.close()
    assert result["ok"] is True
    mambo = next(
        row for row in result["result"]["commanders"]
        if row["commander_id"] == 124)
    assert (mambo["unit_type_id"], mambo["hp"]) == (653, 18)
    assert mambo["shape_change"] == {
        "available": True,
        "instantaneous": True,
        "consumes_turn": False,
        "alternate_form": {
            "unit_type_id": 654,
            "unit_type": "Serpent King",
        },
    }


def _dual_session_on(tmp_path, snapshot, stem, nation,
                     game_name="example_game"):
    if not (snapshot / f"{stem}.2h").exists() or not GAME_DB.exists():
        pytest.skip("controlled Change Shape snapshot or game database absent")
    save = tmp_path / "shape-save"
    save.mkdir()
    for name in (f"{stem}.trn", f"{stem}.2h"):
        shutil.copy2(snapshot / name, save / name)
    db = tmp_path / "shape-game.sqlite3"
    shutil.copy2(GAME_DB, db)
    conn = sqlite3.connect(db)
    try:
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE '%_intent'")
        ]
        for table in tables:
            conn.execute(f'DELETE FROM "{table}"')
        conn.commit()
    finally:
        conn.close()
    return open_session(
        f"{game_name}::{stem}", game_db=db, save_dir=save,
        nation_id=nation)


def _shape_session_on(tmp_path, snapshot):
    return _dual_session_on(tmp_path, snapshot, "mid_ermor", 54)


@pytest.mark.parametrize(
    ("stem", "nation_id"),
    [("mid_ermor", 54), ("mid_marignon", 61)],
)
def test_dual_turn47_verification_has_no_tool_errors(tmp_path, stem, nation_id):
    """Mature sparse saves exercise research, PD, diplomacy, and squad joins."""
    from dom6_assistant.web.routes.agent import _verification_snapshot

    session = _dual_session_on(
        tmp_path, DUAL_RESEARCH_STATE, stem, nation_id)
    try:
        audit = _verification_snapshot(session)
    finally:
        session.close()
    failures = [
        call["response"]
        for section in audit["sections"]
        for call in section["calls"]
        if not call["response"]["ok"]
    ]
    assert failures == []


def test_corpse_count_respects_dominion_and_preserves_known_zero(tmp_path):
    session = _dual_session_on(tmp_path, SHAPE_SERPENT_STATE, "mid_ermor", 54)
    try:
        capital = ok(session.call("get_province", {"province_id": 9}))
        dagothia = ok(session.call("get_province", {"province_id": 7}))
        outside_dominion = ok(
            session.call("get_province", {"province_id": 6})
        )
        listed = {
            province["province_id"]: province["corpse_count"]
            for province in ok(session.call("list_provinces", {"scope": "all"}))
        }
    finally:
        session.close()
    assert capital["corpse_count"] == 1496
    assert dagothia["corpse_count"] == 0
    assert outside_dominion["corpse_count"] is None
    assert listed[9] == 1496
    assert listed[7] == 0
    assert listed[6] is None


def test_corpse_zero_is_hidden_from_nation_without_sensing_trait(tmp_path):
    session = _dual_session_on(
        tmp_path, SHAPE_SERPENT_STATE, "mid_marignon", 61
    )
    try:
        capital = ok(session.call("get_province", {"province_id": 8}))
    finally:
        session.close()
    assert capital["is_ours"] is True
    assert capital["corpse_count"] is None


def test_throne_tool_exposes_f9_claimant_not_just_land_owner(tmp_path):
    session = _dual_session_on(
        tmp_path, CLAIMED_THRONE_STATE, "mid_ermor", 54
    )
    try:
        out = ok(session.call("get_thrones"))
    finally:
        session.close()
    black = next(row for row in out["thrones"] if row["site_id"] == 1401)
    assert black["known_owner_nation_id"] == 54
    assert black["claimant_nation_id"] == 54
    assert black["claimed_by_us"] is True
    assert black["level"] == 1
    assert out["claimed_by_us_count"] == 1
    assert out["claimed_by_us_points"] == 1
    assert out["claimed_by_us_points_from_list"] == 1


def test_change_shape_materializes_exact_client_bytes_and_preserves_order(tmp_path):
    session = _shape_session_on(tmp_path, SHAPE_CHANGED_STATE)
    try:
        recorded = session.call("change_shape", {
            "commander_id": 124,
            "rationale": "Use the tougher serpent combat form.",
        })
        assert recorded["ok"] is True
        assert recorded["result"]["source_form"]["hp"] == 18
        assert recorded["result"]["target_form"]["hp"] == 32
        # An instantaneous action has its own intent and never replaces the
        # strategic commander order.
        assert session.ctx.game_db.execute(
            "SELECT COUNT(*) FROM order_intent WHERE game_id=? AND turn=?",
            (session.ctx.game_id, session.turn)).fetchone()[0] == 0
        assert session.ctx.game_db.execute(
            "SELECT COUNT(*) FROM shape_change_intent WHERE game_id=? AND turn=?",
            (session.ctx.game_id, session.turn)).fetchone()[0] == 1
        audited = session.call("get_orders", {})
        assert audited["ok"] is True
        shape_row = next(
            row for row in audited["result"] if row["order"] == "shape_change")
        assert shape_row["source_form"] == {"unit_type_id": 653, "hp": 18}
        assert shape_row["target_form"] == {"unit_type_id": 654, "hp": 32}

        preview = session.call("materialize_orders", {})
        assert preview["ok"] is True
        assert preview["result"]["changed_bytes"] == 2
        written = session.call("materialize_orders", {"confirm": True})
        assert written["ok"] is True

        actual = session.ctx.h2_path.read_bytes()
        expected = (SHAPE_SERPENT_STATE / "mid_ermor.2h").read_bytes()
        # The client alone rewrites the ignored trailer/checksum byte.
        assert actual[:-1] == expected[:-1]
        mambo = next(
            row for row in session.call("list_commanders", {})["result"]["commanders"]
            if row["commander_id"] == 124)
        assert (mambo["unit_type_id"], mambo["hp"], mambo["order_in_file"]) == (
            654, 32, "defend")

        # The history carries the paired effective HP, so toggling back is
        # exact and simply restores the pristine-base state.
        reverse = session.call("change_shape", {
            "commander_id": 124,
            "rationale": "Return to the leadership form.",
        })
        assert reverse["ok"] is True
        assert reverse["result"]["target_form"]["hp"] == 18
        restored = session.call("materialize_orders", {"confirm": True})
        assert restored["ok"] is True
        assert session.ctx.h2_path.read_bytes() == (
            SHAPE_CHANGED_STATE / "mid_ermor.2h").read_bytes()
    finally:
        session.close()


def test_change_shape_refuses_unobserved_effective_target_hp(tmp_path):
    session = _shape_session_on(tmp_path, SHAPE_SERPENT_STATE)
    try:
        refused = session.call("change_shape", {
            "commander_id": 124,
            "rationale": "Try the other form without a seeded observation.",
        })
    finally:
        session.close()
    assert refused["ok"] is False
    assert "has not been observed" in refused["error"]
    assert "Raw chassis HP is unsafe" in refused["error"]


@pytest.fixture
def pinned(tmp_path):
    """A throwaway copy of the turn-23 snapshot the panel readings came from."""
    if not (PINNED / "mid_marignon.trn").exists() or not GAME_DB.exists():
        pytest.skip("pinned snapshot or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_marignon.trn", "mid_marignon.2h"):
        shutil.copy2(PINNED / name, save / name)
    db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, db)
    s = open_session(game_db=db, save_dir=save)
    yield s
    s.close()


@pytest.fixture
def session(sandbox):
    save, db = sandbox
    s = open_session(game_db=db, save_dir=save)
    yield s
    s.close()


@pytest.fixture
def diplomacy_state(tmp_path):
    s = _session_on(tmp_path, DIPLOMACY_STATE)
    yield s
    s.close()


@pytest.fixture
def research_full(tmp_path):
    """The controlled nine-entry research queue from turn 25."""
    s = _session_on(tmp_path, RESEARCH_FULL)
    yield s
    s.close()


@pytest.fixture
def research_level9(tmp_path):
    """A mixed ordinary-school and individual Level 9 spell queue."""
    s = _session_on(tmp_path, RESEARCH_LEVEL9)
    yield s
    s.close()


@pytest.fixture
def ritual_baseline(tmp_path):
    """Thaumaturgy 3, Sugaar researching, and no gems yet reserved."""
    s = _session_on(tmp_path, RITUAL_BASELINE)
    yield s
    s.close()


@pytest.fixture
def construction_state(tmp_path):
    """Pinned before Sugaar moved home: mage in 86, Palisades in 98."""
    s = _session_on(tmp_path, CONSTRUCTION_STATE)
    yield s
    s.close()


@pytest.fixture
def troop_state(tmp_path):
    """Turn 30 has unattached troops and sparse, occupied squad slots."""
    s = _session_on(tmp_path, TROOP_STATE)
    yield s
    s.close()


@pytest.fixture
def global_state(tmp_path):
    """Turn 15 contains the foreign Tapestry of Dreams global."""
    s = _session_on(tmp_path, GLOBAL_STATE)
    yield s
    s.close()


@pytest.fixture
def pd_state(tmp_path):
    """Turn-30 baseline before any same-turn defence purchases."""
    s = _session_on(tmp_path, PD_BASELINE)
    yield s
    s.close()


def ok(result):
    assert result["ok"], f"{result['tool']} failed: {result.get('error')}"
    return result["result"]


def refused(result, kind=None):
    assert not result["ok"], f"expected refusal, got {result.get('result')}"
    if kind:
        assert result["kind"] == kind, result
    return result["error"]


# -- read ------------------------------------------------------------------


def test_turn_summary_reports_the_current_turn(session):
    s = ok(session.call("get_turn_summary"))
    assert s["turn"] == session.turn
    assert s["nation_id"] == 61
    assert s["provinces_owned"] >= 1
    assert isinstance(s["gold"], int)
    assert s["known_thrones"] == 6
    assert isinstance(s["claimed_throne_points"], int)


def test_global_enchantments_are_named_without_leaking_cast_location(global_state):
    out = ok(global_state.call("get_global_enchantments"))
    assert out["count"] == 1
    effect = out["global_enchantments"][0]
    assert (effect["spell_id"], effect["spell"], effect["caster_nation_id"]) == (
        937,
        "Tapestry of Dreams",
        87,
    )
    assert "cast_province" not in effect


def test_thrones_and_owned_fort_walls_are_visible_without_hidden_owners(session):
    thrones = ok(session.call("get_thrones"))
    assert thrones["count"] == 6
    assert all(row["throne"].startswith("The Throne of") for row in thrones["thrones"])
    assert all(row["known_owner_nation_id"] is None for row in thrones["thrones"])
    capital = ok(session.call("get_province", {"province_id": 93}))
    assert capital["wall_integrity"] == 1500
    assert {site["name"] for site in capital["visible_magic_sites"]} == {
        "The House of Fiery Justice",
        "The Royal Academy",
    }


def test_decoded_province_conditions_and_site_yields_are_tool_visible(session):
    capital = ok(session.call("get_province", {"province_id": 93}))
    assert capital["terrain"]["types"] == ["Plains"]
    assert capital["map_position"] == {"x": 1644, "y": 2388}
    assert capital["owner_nation"] == "Marignon"
    assert capital["scales"] == {
        "order": 2,
        "productivity": 1,
        "heat": 0,
        "growth": 0,
        "luck": 0,
        "magic": 0,
    }
    assert capital["dominion_owner_nation_id"] == 61
    assert capital["administrative_owner_nation_id"] == 61
    justice = next(
        site
        for site in capital["visible_magic_sites"]
        if site["name"] == "The House of Fiery Justice"
    )
    assert justice["gem_income"] == {"fire": 4, "astral": 1}


def test_magic_economy_separates_stock_commitments_and_site_income(session):
    magic = ok(session.call("get_magic_economy"))
    assert magic["stock_at_turn_start"]["fire"] == 41
    assert magic["uncommitted"]["fire"] == 39
    assert magic["income"] == {"fire": 4, "astral": 1, "blood": 3}


def test_province_detail_reports_the_panel_estimate(pinned):
    """Citala's panel read "about 60 enemy units" at turn 23."""
    detail = ok(pinned.call("get_province", {"province_id": 83}))
    assert detail["enemy_units"] == 60
    assert detail["estimate_uncertainty_percent"] == 30
    assert detail["intel_from_our_own_unit"] is True
    assert detail["army_composition_report"] == (
        "mainly Militias, Knights and Longbowmen"
    )
    assert "about 60 enemy units" in detail["scout_report_text"]


def test_an_unscouted_province_says_unknown_not_zero(session):
    """Silence, or a 0, would read as 'no enemies'. It has to say so."""
    detail = ok(session.call("get_province", {"province_id": 56}))
    assert "unknown" in str(detail["enemy_units"])
    assert "NOT mean it is empty" in str(detail["enemy_units"])


def test_scout_report_lists_only_what_we_have_scouted(pinned):
    report = ok(pinned.call("scout_report"))
    scouted = {r["province_id"]: r for r in report["scouted"]}
    assert scouted[83]["enemy_units"] == 60
    assert scouted[83]["army_composition_report"] == (
        "mainly Militias, Knights and Longbowmen"
    )
    assert scouted[91]["enemy_units"] == 40
    assert scouted[91]["reported_commander"] == "Hunerik the Priest"
    assert scouted[91]["estimate_uncertainty_percent"] == 10
    # 56 holds 76 enemy units in the file and we have no vision of it.
    assert 56 not in scouted


def test_find_province_returns_ids_not_just_names(session):
    hits = ok(session.call("find_province", {"name": "Marig"}))
    assert hits and all("province_id" in h for h in hits)


def test_lookup_unit_never_reports_an_unverified_cost(session):
    """basecost is wrong for sacred and magical units, so it is not served.

    Knight of the Chalice costs 70 in game and 20 in the reference field. The
    tool must return the observed price or None — never the reference one.
    """
    rows = ok(session.call("lookup_unit", {"name": "Knight of the Chalice"}))
    r = next(x for x in rows if x["name"] == "Knight of the Chalice")
    assert r["gold"] in (70, None)
    assert r["gold"] != 20, "served the unreliable reference cost"
    assert "map_move" not in r and "leader" not in r


def test_lookup_unit_keeps_stats_that_were_verified(session):
    """Combat stats match records decoded from the save, so they pass through."""
    rows = ok(session.call("lookup_unit", {"name": "Earth Serpent"}))
    assert any(r["hp"] == 210 for r in rows)


def test_lookup_refuses_an_unknown_name(session):
    refused(session.call("lookup_unit", {"name": "Zzzznotaunit"}), "bad_call")


def test_lookup_needs_one_of_name_or_id(session):
    refused(session.call("lookup_unit", {}), "bad_call")


def test_item_and_spell_paths_both_render(session):
    """The two reference tables use different path encodings.

    `spells` stores integers indexing FAWESDNGBH with -1 for "none"; `items`
    stores the letters themselves. Assuming one encoding for both raised a
    TypeError on the first item lookup that was ever made.
    """
    item = ok(session.call("lookup_item", {"name": "Fire Sword"}))[0]
    assert item["path"] == "F1"
    spell = ok(session.call("lookup_spell", {"name": "Fire Darts"}))[0]
    assert spell["path"] == "F1"


def test_item_gem_cost_comes_from_the_path_level(session):
    """Costs were unknown until the player read them off the forge screen.

    Ten items at path level 1 — three Fire, seven Astral, across every
    equipment slot — all cost 5 gems, which is what establishes that cost
    depends on the path requirement and never on the item.
    """
    item = ok(session.call("lookup_item", {"name": "Fire Sword"}))[0]
    assert item["gem_cost"] == {"fire": 5}
    assert "forge screens" in item["gem_cost_source"]


def test_a_rebated_item_reports_both_prices(session):
    """The screen shows the full price and the discount lands at forge time.

    Reporting only one number would leave the model unable to reconcile the
    other against what it sees, whichever we chose.
    """
    item = ok(session.call("lookup_item", {"name": "Sword of Justice"}))[0]
    assert item["gem_cost"] == {"fire": 15, "astral": 15}
    # The client routine shows the nation fields are a one-gem reduction per
    # required path, not the 20% inferred from level-one controlled orders.
    assert item["gem_cost_charged"] == {"fire": 14, "astral": 14}
    assert "one-gem reduction" in item["national_rebate"]


def test_a_two_path_item_renders_both(session):
    item = ok(session.call("lookup_item", {"name": "Sword of Justice"}))[0]
    assert item["path"] == "F3 S3"


def test_reference_detail_tools_expose_effects_and_base_equipment(session):
    knight = ok(session.call("lookup_unit", {"name": "Knight of the Chalice", "detail": "full"}))[0]
    assert knight["resource_cost"] == 58
    assert knight["recruitment_point_cost"] == 51
    assert {row["name"] for row in knight["weapons"]} >= {"Lance", "Broad Sword"}

    sword = ok(session.call("lookup_item", {"name": "Fire Sword", "detail": "full"}))[0]
    assert sword["weapon"]["effect"]["effect"].startswith("Damage")
    assert "Slashing Damage" in " ".join(sword["weapon"]["effect"]["modifiers"])

    augury = ok(session.call("lookup_spell", {"name": "Augury", "detail": "full"}))[0]
    assert augury["kind"] == "ritual"
    assert augury["effect"] == "Remote Site Search"


def test_site_weapon_and_armor_have_direct_lookup_tools(session):
    site = ok(session.call("lookup_magic_site", {"name": "The House of Fiery Justice"}))[0]
    assert site["gem_income"] == {"fire": 4, "astral": 1}
    weapon = ok(session.call("lookup_weapon", {"name": "Fire Sword"}))[0]
    assert weapon["attack_modifier"] == 1
    armor = ok(session.call("lookup_armor", {"name": "Full Helmet"}))[0]
    assert armor["protection_by_zone"]


def test_static_nation_lookup_exposes_public_rosters_not_turn_state(session):
    nation = ok(session.call("lookup_nation", {"reference_id": 61, "detail": "full"}))[0]
    assert nation["id"] == 61
    fort_troops = {unit["name"] for unit in nation["native_rosters"]["fort_troops"]}
    assert {"Pikeneer", "Crossbowman", "Knight of the Chalice"} <= fort_troops
    assert "static extracted game data" in nation["source"]


def test_research_and_caster_discovery_are_grounded_in_current_state(session):
    research = ok(session.call("get_research", {}))
    assert research["points_per_turn_exact"] is True
    assert "modeled_points_per_turn" in research
    assert research["points_per_turn"] == research["modeled_points_per_turn"]
    assert "one point per displayed experience star" in research["modeled_points_basis"]
    experienced = [row for row in research["researchers"] if row["experience"] >= 15]
    assert experienced
    for row in experienced:
        assert row["experience_stars"] == RR.experience_stars(row["experience"])
        assert row["from_experience"] == row["experience_stars"]
    assert research["unmodeled_modifiers"] == []
    assert "Inspiring Researcher" in research["modeled_points_basis"]

    alteration = ok(session.call("list_research_options", {"school": "alteration"}))
    assert alteration["targets"] == [{"school": "Alteration", "level": 2}]
    assert any(spell["name"] == "Blur" for spell in alteration["spells"])

    castable = ok(session.call("list_castable_spells", {"commander_id": 308}))
    by_name = {spell["name"]: spell for spell in castable["spells"]}
    assert by_name["Fire Flies"]["target"] == "battlefield"
    assert by_name["Distill Gold"]["order_supported"] is True
    assert by_name["Distill Gold"]["affordable"] is True
    assert by_name["Distill Gold"]["castable_now"] is True
    sugaar = ok(session.call("list_castable_spells", {"commander_id": 297, "kind": "ritual"}))
    sugaar_by_name = {spell["name"]: spell for spell in sugaar["spells"]}
    assert sugaar_by_name["Carrier Birds"]["target"] == (
        "province_plus_commander_and_carried_gems"
    )
    assert sugaar_by_name["Carrier Birds"]["payload"] == "caster_carried_gems"
    assert sugaar_by_name["Carrier Birds"]["order_supported"] is True
    assert sugaar_by_name["Carrier Birds"]["castable_now"] is False
    assert all(
        "target or payload layout" not in blocker
        for blocker in sugaar_by_name["Carrier Birds"]["why_not_castable_now"]
    )
    assert "Quick Roots" not in by_name  # nation-restricted to another nation
    assert "Cave Collapse" not in by_name  # pathless internal/event record


def test_forge_candidates_carry_their_cost(session):
    forge = ok(session.call("list_forgeable_items", {"commander_id": 308}))
    fire_sword = next(item for item in forge["items"] if item["name"] == "Fire Sword")
    assert fire_sword["base_paths_met"] is True
    assert fire_sword["researched"] is True
    assert fire_sword["gem_cost"] == {"fire": 5}


def test_divine_tool_does_not_conflate_three_point_systems(session):
    divine = ok(session.call("list_divine_spells", {"commander_id": 110}))
    note = divine["unmodelled_selector"]
    assert "No second selector has been observed" in note
    assert "magic paths and levels" in note
    assert "temple-raised holy recruitment allowance" in note
    assert "province-local dominion candles" in note


def test_commander_action_options_apply_decoded_prerequisites(session):
    options = ok(session.call("get_commander_action_options", {"commander_id": 308}))
    available = {row["order"] for row in options["available"]}
    unavailable = {row["order"] for row in options["known_unavailable"]}
    assert {"defend", "research", "move"} <= available
    assert {"assassinate", "become_prophet"} <= unavailable
    assert set(options["encoding_only"]) == {"empowerment"}


def test_live_verification_snapshot_is_exact_and_model_free(session):
    from dom6_assistant.web.routes.agent import (
        _audit_call,
        _verification_snapshot,
    )

    audit = _verification_snapshot(session)
    assert audit["turn"] == 30
    assert audit["source"]["model_contacted"] is False
    assert len(audit["tool_catalog"]) == len(session.registry) == 76
    assert {section["id"] for section in audit["sections"]} >= {
        "orientation",
        "map",
        "forces",
        "orders",
        "owned-provinces",
        "commanders",
    }
    responses = [call["response"] for section in audit["sections"] for call in section["calls"]]
    assert responses and all(response["ok"] for response in responses)

    refused = _audit_call(session, "record_order", {"commander_id": 308})
    assert refused["response"]["kind"] == "write_refused"


def test_verification_page_is_mounted_on_both_front_ends():
    from dom6_assistant.ui.app import app as ui_app
    from dom6_assistant.web.app import create_app

    for app in (ui_app, create_app()):
        paths = {route.path for route in app.routes}
        assert {"/verify", "/api/agent/verification", "/api/agent/inspect"} <= paths


def test_list_order_types_matches_what_can_be_recorded(session):
    """The advertised list and the enforced list must be the same list."""
    result = ok(session.call("list_order_types"))
    listed = set(result["orders"])
    assert listed == set(O.ORDER_CODES) - O.ECONOMIC_ORDERS_PENDING
    assert set(result["encoding_only"]) == O.ECONOMIC_ORDERS_PENDING


def test_ermor_reanimation_and_claim_throne_orders_are_writable(tmp_path):
    snapshot = Path("knowledge/snapshots/example_game/t12-auto")
    session = _dual_session_on(tmp_path, snapshot, "mid_ermor", 54)
    try:
        expected = {
            131: "reanimate_warriors",
            94: "reanimate_lictors",
            3: "claim_throne",
        }
        for commander_id, order in expected.items():
            options = ok(session.call(
                "get_commander_action_options", {"commander_id": commander_id}))
            available = {row["order"] for row in options["available"]}
            assert order in available
            recorded = ok(session.call("record_order", {
                "commander_id": commander_id,
                "order": order,
                "rationale": "Preserve the controlled client order.",
            }))
            assert recorded["order"] == order

        ok(session.call("materialize_orders", {"confirm": True}))
        by_id = {
            order.commander_id: order
            for order in O.read_orders(session.ctx.h2_path)
        }
        assert {commander_id: by_id[commander_id].order_name
                for commander_id in expected} == expected
    finally:
        session.close()


def test_complete_ermor_reanimation_menu_and_player_visible_yields(tmp_path):
    session = _dual_session_on(tmp_path, REANIMATION_STATE, "mid_ermor", 54)
    try:
        expected = {
            # H1 Domex
            181: {
                "reanimate_ghouls": "6+",
                "reanimate_soulless": "8+",
                "reanimate_warriors": "3+",
            },
            # H2 Femur
            131: {
                "reanimate_ghouls": "7+",
                "reanimate_soulless": "16+",
                "reanimate_warriors": "5+",
                "reanimate_horsemen": "1+",
            },
            # H3 Ychekhes
            94: {
                "reanimate_ghouls": "8+",
                "reanimate_soulless": "24+",
                "reanimate_warriors": "7+",
                "reanimate_horsemen": "2+",
                "reanimate_lictors": "1",
            },
        }
        all_reanimation = {
            "reanimate_ghouls", "reanimate_soulless",
            "reanimate_warriors", "reanimate_horsemen",
            "reanimate_lictors",
        }
        for commander_id, legal in expected.items():
            options = ok(session.call(
                "get_commander_action_options", {"commander_id": commander_id}))
            available = {row["order"]: row for row in options["available"]}
            unavailable = {
                row["order"] for row in options["known_unavailable"]}
            assert all_reanimation & set(available) == set(legal)
            assert all_reanimation - set(legal) <= unavailable
            for order, displayed_yield in legal.items():
                details = available[order]["eligibility"]["reanimation"]
                assert details["displayed_yield"] == displayed_yield
                assert details["gem_cost"] == 0
                assert details["persists_each_turn"] is True
    finally:
        session.close()


def test_reanimation_writer_matches_final_client_authored_h1_h2_h3_state(tmp_path):
    session = _dual_session_on(tmp_path, REANIMATION_STATE, "mid_ermor", 54)
    try:
        for commander_id, order in {
            181: "reanimate_warriors",
            131: "reanimate_horsemen",
            94: "reanimate_lictors",
        }.items():
            ok(session.call("record_order", {
                "commander_id": commander_id,
                "order": order,
                "rationale": "Match the controlled final Reanimation menu state.",
            }))
        ok(session.call("materialize_orders", {"confirm": True}))
        assert session.ctx.h2_path.read_bytes()[:-2] == (
            REANIMATION_FINAL_STATE / "mid_ermor.2h").read_bytes()[:-2]
    finally:
        session.close()


def test_call_god_is_exposed_only_for_a_dead_pretender_and_matches_client(tmp_path):
    session = _dual_session_on(tmp_path, DEAD_GOD_STATE, "mid_ermor", 54)
    try:
        overview = ok(session.call("get_nation_overview", {}))
        assert overview["pretender"]["commander_id"] == 124
        assert overview["pretender"]["status"] == "dead"
        assert overview["pretender"]["dead"] is True

        options = ok(session.call(
            "get_commander_action_options", {"commander_id": 131}))
        call_god = next(
            row for row in options["available"] if row["order"] == "call_god")
        assert "commander has a Holy path" in call_god["eligibility"]["checks"]

        recorded = ok(session.call("record_order", {
            "commander_id": 131,
            "order": "call_god",
            "rationale": "Recall Mambo from the dead.",
        }))
        assert recorded["pretender_nation_id"] == 54
        ok(session.call("materialize_orders", {"confirm": True}))
        written = session.ctx.h2_path.read_bytes()
        expected = (CALL_GOD_STATE / "mid_ermor.2h").read_bytes()
        assert written[:-2] == expected[:-2]
    finally:
        session.close()


def test_call_god_persists_while_recall_points_accumulate(tmp_path):
    session = _dual_session_on(
        tmp_path, ONGOING_CALL_GOD_STATE, "mid_ermor", 54)
    try:
        overview = ok(session.call("get_nation_overview", {}))
        pretender = overview["pretender"]
        assert (pretender["status"], pretender["dead"]) == ("dead", True)
        assert pretender["call_god_recall"] == {
            "ordinary_target_points": 50,
            "exact_accumulated_points": None,
            "active_priests": [{
                "commander_id": 131,
                "name": "Femur",
                "holy_level": 2,
                "ordinary_contribution_range": [1, 3],
            }],
            "current_turn_contribution_range": [1, 3],
            "tracked_resolved_contribution_range": [0, 0],
            "possible_accumulated_points_range": None,
            "tracking_started_turn": 47,
            "tracking_through_resolved_turn": 46,
            "tracking_complete_since_death": False,
            "note": (
                "Dominions does not show the exact accumulated randomized "
                "total to players. These bounds are player-equivalent "
                "bookkeeping from observed orders: an ordinary Holy H priest "
                "contributes H-1 through H+1 points per resolved turn. A "
                "complete possible total is reported only when every planning "
                "turn since the observed death was tracked. Disciple status "
                "and special Call God bonuses can modify the requirement or "
                "contribution."
            ),
        }
        commanders = ok(session.call("list_commanders", {}))["commanders"]
        assert not any(row["commander_id"] == 124 for row in commanders)
        femur = next(row for row in commanders if row["commander_id"] == 131)
        assert femur["order_in_file"] == "call_god"
    finally:
        session.close()


def test_call_god_tracks_player_knowable_bounds_across_resolved_turns(tmp_path):
    """An alive baseline makes the subsequent hidden-total bounds complete."""
    root = Path("knowledge/snapshots/example_game")
    required = [
        root / name
        for name in ("t45-auto-5", "t46-auto-6", "t47-auto-3")
    ]
    if not all((folder / "mid_ermor.2h").exists() for folder in required):
        pytest.skip("controlled Call God history is absent")

    save = tmp_path / "call-god-save"
    save.mkdir()
    db = tmp_path / "call-god.sqlite3"
    shutil.copy2(GAME_DB, db)

    def open_at(folder):
        for name in ("mid_ermor.trn", "mid_ermor.2h"):
            shutil.copy2(folder / name, save / name)
        return open_session(
            "example_game::mid_ermor", game_db=db, save_dir=save,
            nation_id=54,
        )

    for folder in required[:2]:
        session = open_at(folder)
        try:
            ok(session.call("get_nation_overview", {}))
        finally:
            session.close()

    session = open_at(required[2])
    try:
        recall = ok(session.call("get_nation_overview", {}))["pretender"][
            "call_god_recall"]
        assert recall["tracked_resolved_contribution_range"] == [1, 3]
        assert recall["possible_accumulated_points_range"] == [1, 3]
        assert recall["current_turn_contribution_range"] == [1, 3]
        assert recall["tracking_started_turn"] == 46
        assert recall["tracking_through_resolved_turn"] == 46
        assert recall["tracking_complete_since_death"] is True
    finally:
        session.close()


@pytest.mark.parametrize(
    ("snapshot", "stem", "nation_id", "commander_id"),
    [
        (Path("knowledge/snapshots/t1-orders"), "mid_marignon", 61, 116),
        (Path("knowledge/snapshots/example_game/t43-auto"),
         "mid_ermor", 54, 131),
    ],
)
def test_call_god_is_not_offered_for_dormant_or_alive_pretender(
        tmp_path, snapshot, stem, nation_id, commander_id):
    session = _dual_session_on(tmp_path, snapshot, stem, nation_id)
    try:
        overview = ok(session.call("get_nation_overview", {}))
        assert overview["pretender"]["dead"] is False
        options = ok(session.call(
            "get_commander_action_options", {"commander_id": commander_id}))
        unavailable = {
            row["order"]: row["reason"] for row in options["known_unavailable"]
        }
        assert "alive or dormant" in unavailable["call_god"]
    finally:
        session.close()


def test_claim_throne_refuses_the_already_claimed_f9_state(tmp_path):
    session = _dual_session_on(
        tmp_path, CLAIMED_THRONE_STATE, "mid_ermor", 54
    )
    try:
        options = ok(session.call(
            "get_commander_action_options", {"commander_id": 3}
        ))
        unavailable = {row["order"]: row["reason"]
                       for row in options["known_unavailable"]}
        assert "already claimed by us" in unavailable["claim_throne"]
        error = refused(session.call("record_order", {
            "commander_id": 3,
            "order": "claim_throne",
            "rationale": "This must be refused as redundant.",
        }))
        assert "already claimed by us" in error
    finally:
        session.close()


def test_assassin_can_attack_its_current_foreign_province(tmp_path):
    snapshot = Path("knowledge/snapshots/example_game/t17-auto-2")
    session = _dual_session_on(tmp_path, snapshot, "mid_marignon", 61)
    try:
        options = ok(session.call(
            "get_commander_action_options", {"commander_id": 12}))
        available = {row["order"] for row in options["available"]}
        assert "attack_current_province" in available

        recorded = ok(session.call("record_order", {
            "commander_id": 12,
            "order": "attack_current_province",
            "rationale": "Attack the local defenders after infiltrating.",
        }))
        assert recorded["current_province_id"] == 22
        ok(session.call("materialize_orders", {"confirm": True}))
        order = next(
            row for row in O.read_orders(session.ctx.h2_path)
            if row.commander_id == 12)
        assert (order.order_name, order.destination) == (
            "attack_current_province", 22)

        error = refused(session.call("record_order", {
            "commander_id": 12,
            "order": "attack_current_province",
            "destination": 23,
            "rationale": "A caller must not redirect this fixed order.",
        }))
        assert "current province" in error
    finally:
        session.close()


# -- write -----------------------------------------------------------------


def test_record_order_requires_a_rationale(session):
    err = refused(
        session.call("record_order", {"commander_id": 308, "order": "defend"}), "bad_call"
    )
    assert "rationale" in err


def test_record_order_rejects_a_commander_that_is_not_ours(session):
    err = refused(
        session.call(
            "record_order",
            {
                "commander_id": 41,
                "order": "defend",
                "rationale": "Inberke is another nation's pretender.",
            },
        ),
        "bad_call",
    )
    assert "not one of ours" in err


def test_record_order_rejects_an_unverified_order(session):
    err = refused(
        session.call(
            "record_order",
            {"commander_id": 308, "order": "teleport_army", "rationale": "invented order"},
        ),
        "bad_call",
    )
    assert "not been verified" in err


def test_move_without_a_destination_is_refused(session):
    """The dangerous case: it would write cleanly and do nothing."""
    err = refused(
        session.call(
            "record_order", {"commander_id": 308, "order": "move", "rationale": "advance"}
        ),
        "bad_call",
    )
    assert "destination" in err


def test_berytian_sailing_route_is_exposed_and_writer_matches_client(tmp_path):
    """Messeis carried 45 size-3 troops across one sea and attacked land 16."""
    root = Path("knowledge/snapshots/example_game_2")
    baseline = root / "t2-auto"
    authored = root / "t2-auto-2"
    if not (baseline / "early_berytos.2h").exists() or not GAME_DB.exists():
        pytest.skip("controlled Berytos sailing snapshots absent")

    save = tmp_path / "sailing-save"
    save.mkdir()
    for name in ("early_berytos.trn", "early_berytos.2h"):
        shutil.copy2(baseline / name, save / name)
    db = tmp_path / "sailing.sqlite3"
    shutil.copy2(GAME_DB, db)
    connection = sqlite3.connect(db)
    try:
        for table in [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE '%_intent'")
        ]:
            connection.execute(f'DELETE FROM "{table}"')
        connection.commit()
    finally:
        connection.close()

    sailing = open_session(
        "example_game_2::early_berytos", game_db=db, save_dir=save,
        nation_id=29,
    )
    try:
        options = ok(sailing.call(
            "get_movement_options", {"commander_id": 92}))
        detail = options["sailing"]
        assert detail["ship_size_capacity"] == 999
        assert detail["ship_size_used"] == 135
        assert detail["maximum_transportable_unit_size"] == 4
        assert detail["largest_follower_size"] == 3
        assert detail["followers"] == 45
        assert detail["slowest_map_move"] == 14
        destination = next(
            row for row in detail["destinations"]
            if row["province_id"] == 16)
        assert destination["province_id"] == 16
        assert destination["province"] == "The Soaked Earth"
        assert destination["mode"] == "sailing"
        assert destination["route"][0] == 19
        assert destination["route"][-1] == 16
        assert destination["sea_provinces_crossed"] == 1
        assert destination["embark_disembark_movement_cost"] == 6

        captain = next(
            row for row in ok(sailing.call("list_commanders", {}))["commanders"]
            if row["commander_id"] == 92)
        assert captain["abilities"]["sailing_ship_size"] == 999
        assert captain["abilities"]["sailing_max_unit_size"] == 4

        recorded = ok(sailing.call("record_order", {
            "commander_id": 92,
            "order": "move",
            "destination": 16,
            "rationale": "Sail across Utterdeep to attack the far coast.",
        }))
        assert recorded["movement"] == destination
        ok(sailing.call("materialize_orders", {"confirm": True}))
        assert sailing.ctx.h2_path.read_bytes()[:-2] == (
            authored / "early_berytos.2h").read_bytes()[:-2]
    finally:
        sailing.close()


def test_caelian_fliers_get_unified_multi_province_routes(tmp_path):
    """Flying uses route costs; it is not an adjacent-province exception."""
    root = Path("knowledge/snapshots/example_game_2/t3-auto")
    if not (root / "early_caelum.2h").exists() or not GAME_DB.exists():
        pytest.skip("controlled Caelum movement snapshot absent")
    save = tmp_path / "flying-save"
    save.mkdir()
    for name in ("early_caelum.trn", "early_caelum.2h"):
        shutil.copy2(root / name, save / name)
    live_maps = Path.home() / ".dominions6/savedgames/example_game_2"
    for name in (
        "__randommap_example_game_2.map",
        "__under_example_game_2.map",
    ):
        if (live_maps / name).exists():
            shutil.copy2(live_maps / name, save / name)
    db = tmp_path / "flying.sqlite3"
    shutil.copy2(GAME_DB, db)

    flying = open_session(
        "example_game_2::early_caelum", game_db=db, save_dir=save,
        nation_id=24,
    )
    try:
        options = ok(flying.call("get_movement_options", {"commander_id": 5}))
        assert options["ordinary"]["profile"]["mode"] == "flying"
        routes = {row["province_id"]: row for row in options["destinations"]}
        assert routes[12]["route"] == [8, 13, 12]
        assert routes[12]["movement_cost"] == 15
        assert routes[21]["route"] == [8, 16, 21]
        assert routes[21]["movement_cost"] == 15
        assert 4 not in routes  # Flying cannot cross a sea.

        ground = ok(flying.call("get_movement_options", {"commander_id": 99}))
        assert ground["ordinary"]["profile"]["mode"] == "ground"
        assert all(len(row["route"]) == 2 for row in ground["destinations"])
        assert 4 not in {row["province_id"] for row in ground["destinations"]}
    finally:
        flying.close()


def test_destination_zero_means_none_on_a_targetless_order(session):
    """0 is the game's own "no destination" sentinel, not province 0.

    A live run sent destination 0 on five defend orders and had all five
    rejected as "no province 0 in this game" — a correct call turned into an
    error, which is the coercion mistake pointing the other way.
    """
    ok(
        session.call(
            "record_order",
            {
                "commander_id": 17,
                "order": "defend",
                "destination": 0,
                "rationale": "hold the capital",
            },
        )
    )
    row = next(o for o in ok(session.call("get_orders")) if o["commander_id"] == 17)
    assert row["destination"] is None


def test_destination_zero_still_fails_an_order_that_needs_a_target(session):
    """ "None" is what 0 means, so a move with 0 is a move with no target."""
    err = refused(
        session.call(
            "record_order",
            {"commander_id": 17, "order": "move", "destination": 0, "rationale": "advance"},
        ),
        "bad_call",
    )
    assert "destination" in err


def test_move_to_a_province_that_does_not_exist_is_refused(session):
    refused(
        session.call(
            "record_order",
            {"commander_id": 308, "order": "move", "destination": 99999, "rationale": "advance"},
        ),
        "bad_call",
    )


def test_local_actions_do_not_invent_a_destination(session):
    """These act in the commander's current province and write parameter 0."""
    for commander, order in (
        (309, "assassinate"),
        (236, "seduce"),
        (308, "pillage"),
        (236, "instill_uprising"),
        (308, "blood_hunt"),
        (308, "search_magic_sites_auto"),
    ):
        out = ok(
            session.call(
                "record_order",
                {
                    "commander_id": commander,
                    "order": order,
                    "rationale": "exercise the verified local action",
                },
            )
        )
        assert out["order"] == order and "destination" not in out


def test_record_order_will_not_forge(session):
    """Forging is writable now, but not through this path.

    record_order has nowhere to put the gem cost, so recording a forge here
    would write the order and leave the national pool untouched — a file that
    forges an item it never paid for. The refusal names the tool that can.
    """
    err = refused(
        session.call(
            "record_order",
            {
                "commander_id": 308,
                "order": "forge_magic_item",
                "item_id": 1,
                "rationale": "forge a Fire Sword",
            },
        ),
        "bad_call",
    )
    assert "use forge_item" in err
    assert "reserves gems" in err


def test_empowerment_is_refused_until_gem_accounting_is_generalized(session):
    err = refused(
        session.call(
            "record_order",
            {
                "commander_id": 308,
                "order": "empowerment",
                "magic_path": "fire",
                "rationale": "gain Fire magic",
            },
        ),
        "bad_call",
    )
    assert "gem reservation" in err and "read-only" in err


def test_build_palisades_supplies_the_fixed_building_index(construction_state):
    out = ok(
        construction_state.call(
            "record_order",
            {"commander_id": 297, "order": "build_palisades", "rationale": "fortify this province"},
        )
    )
    assert out["building"] == "palisades"
    row = next(o for o in ok(construction_state.call("get_orders")) if o["commander_id"] == 297)
    assert row["building"] == "palisades"


def test_hide_is_stationary_code_two_not_a_destination(session):
    out = ok(
        session.call(
            "record_order", {"commander_id": 235, "order": "hide", "rationale": "remain concealed"}
        )
    )
    assert out["eligibility"]["status"] == "verified"
    assert "Stealth" in " ".join(out["eligibility"]["checks"])
    assert "destination" not in out
    ok(session.call("materialize_orders", {"confirm": True}))
    block = O.find_order_blocks(session.ctx.h2_path.read_bytes())[235]
    assert (block.order_code, block.parameter, block.order_name) == (2, 0, "hide")


def test_construction_and_demolition_check_visible_province_state(construction_state):
    lab = ok(
        construction_state.call(
            "record_order",
            {
                "commander_id": 297,
                "order": "build_laboratory",
                "rationale": "establish a lab in Copper Canyons",
            },
        )
    )
    assert lab["eligibility"]["status"] == "verified"

    fort = ok(
        construction_state.call(
            "record_order",
            {"commander_id": 307, "order": "demolish_fort", "rationale": "remove the palisade"},
        )
    )
    assert "province has a fort" in fort["eligibility"]["checks"]

    laboratory = ok(
        construction_state.call(
            "record_order",
            {
                "commander_id": 116,
                "order": "demolish_laboratory",
                "rationale": "remove the capital laboratory",
            },
        )
    )
    assert "province has a laboratory" in laboratory["eligibility"]["checks"]


def test_proven_illegal_construction_and_demolition_are_refused(construction_state):
    err = refused(
        construction_state.call(
            "record_order",
            {
                "commander_id": 307,
                "order": "build_laboratory",
                "rationale": "a priest is not a mage",
            },
        ),
        "bad_call",
    )
    assert "non-Holy magic path" in err

    err = refused(
        construction_state.call(
            "record_order",
            {"commander_id": 307, "order": "build_palisades", "rationale": "cannot stack forts"},
        ),
        "bad_call",
    )
    assert "already has a fort" in err

    err = refused(
        construction_state.call(
            "record_order",
            {"commander_id": 297, "order": "demolish_fort", "rationale": "nothing to demolish"},
        ),
        "bad_call",
    )
    assert "no fort" in err

    err = refused(
        construction_state.call(
            "record_order",
            {
                "commander_id": 307,
                "order": "demolish_laboratory",
                "rationale": "nothing to demolish",
            },
        ),
        "bad_call",
    )
    assert "no laboratory" in err


@pytest.mark.parametrize(
    ("commander", "order", "argument", "code", "parameter"),
    [
        (297, "build_palisades", {}, 20, 1),
    ],
)
def test_typed_strategic_orders_materialize(
    construction_state, commander, order, argument, code, parameter
):
    payload = {
        "commander_id": commander,
        "order": order,
        "rationale": "typed parameter round trip",
        **argument,
    }
    ok(construction_state.call("record_order", payload))
    result = ok(construction_state.call("materialize_orders", {"confirm": True}))
    assert not result["skipped"]
    block = O.find_order_blocks(construction_state.ctx.h2_path.read_bytes())[commander]
    assert (block.order_code, block.parameter) == (code, parameter)


def test_fortress_upgrade_writes_selector_and_reserves_gold(construction_state):
    from dom6_assistant.file_reader.formats import h2

    before = construction_state.ctx.h2_path.read_bytes()
    starting_gold = h2.gold_remaining(before)
    out = ok(
        construction_state.call(
            "record_order",
            {
                "commander_id": 307,
                "order": "upgrade_fortress",
                "rationale": "upgrade the Palisades to a Fortress",
            },
        )
    )
    assert out["building"] == "fortress"
    ok(construction_state.call("materialize_orders", {"confirm": True}))
    data = construction_state.ctx.h2_path.read_bytes()
    block = O.find_order_blocks(data)[307]
    assert (block.order_code, block.parameter, block.order_name) == (20, 2, "upgrade_fortress")
    assert h2.fort_construction(data, 98, [86, 93, 98]) == 2
    assert h2.gold_remaining(data) == starting_gold - 600


def test_fortress_upgrade_refuses_an_unfortified_province(construction_state):
    err = refused(
        construction_state.call(
            "record_order",
            {
                "commander_id": 297,
                "order": "upgrade_fortress",
                "rationale": "there is no Palisades here",
            },
        ),
        "bad_call",
    )
    assert "Palisades" in err and "no fort" in err


@pytest.mark.parametrize(
    ("commander_id", "order", "cost"),
    [(297, "build_laboratory", 600), (87, "build_temple", 600), (297, "build_palisades", 1000)],
)
def test_every_construction_order_reserves_its_gold(construction_state, commander_id, order, cost):
    from dom6_assistant.file_reader.formats import h2

    before = h2.gold_remaining(construction_state.ctx.h2_path.read_bytes())
    ok(
        construction_state.call(
            "record_order",
            {
                "commander_id": commander_id,
                "order": order,
                "rationale": "verify construction gold reservation",
            },
        )
    )
    ok(construction_state.call("materialize_orders", {"confirm": True}))
    after = h2.gold_remaining(construction_state.ctx.h2_path.read_bytes())
    assert after == before - cost


def test_recording_then_reading_back_preserves_the_reasoning(session):
    ok(
        session.call(
            "record_order",
            {
                "commander_id": 308,
                "order": "move",
                "destination": 86,
                "rationale": "Bruise reinforces Copper Canyons.",
            },
        )
    )
    orders = ok(session.call("get_orders"))
    row = next(o for o in orders if o["commander_id"] == 308)
    assert row["rationale"] == "Bruise reinforces Copper Canyons."
    assert row["destination"] == 86


def test_recording_twice_replaces_rather_than_duplicates(session):
    for dest in (86, 98):
        ok(
            session.call(
                "record_order",
                {
                    "commander_id": 308,
                    "order": "move",
                    "destination": dest,
                    "rationale": f"to {dest}",
                },
            )
        )
    orders = [o for o in ok(session.call("get_orders")) if o["commander_id"] == 308]
    assert len(orders) == 1 and orders[0]["destination"] == 98


def test_materialize_defaults_to_a_dry_run(session):
    h2 = session.ctx.h2_path
    before = h2.read_bytes()
    ok(
        session.call(
            "record_order",
            {"commander_id": 308, "order": "move", "destination": 86, "rationale": "reinforce"},
        )
    )
    result = ok(session.call("materialize_orders"))
    assert result["dry_run"] is True
    assert h2.read_bytes() == before, "dry run wrote to the file"


def test_materialize_with_confirm_writes_and_round_trips(session):
    h2 = session.ctx.h2_path
    ok(
        session.call(
            "record_order",
            {"commander_id": 308, "order": "move", "destination": 86, "rationale": "reinforce"},
        )
    )
    result = ok(session.call("materialize_orders", {"confirm": True}))
    assert result["dry_run"] is False and not result["skipped"]
    blocks = O.find_order_blocks(h2.read_bytes())
    assert blocks[308].order_name == "move"
    assert blocks[308].destination == 86


def test_materialize_leaves_other_commanders_alone(session):
    """A rebuild must not disturb orders it was not asked about.

    Compared over OUR commanders only. An unfiltered block scan also returns
    province records — id 98 comes back as a commander named "The Obsidian
    Waste" — and their "destination" is really recruitment queue bytes, which
    a materialisation legitimately moves.
    """
    h2 = session.ctx.h2_path
    ours = {c.commander_id for c in session.ctx.view.own_commanders(h2)}
    before = O.find_order_blocks(h2.read_bytes())
    ok(
        session.call(
            "record_order",
            {"commander_id": 308, "order": "move", "destination": 86, "rationale": "reinforce"},
        )
    )
    ok(session.call("materialize_orders", {"confirm": True}))
    after = O.find_order_blocks(h2.read_bytes())
    for cid, block in before.items():
        if cid == 308 or cid not in ours:
            continue
        assert after[cid].order_code == block.order_code, f"disturbed {cid}"
        assert after[cid].destination == block.destination


def test_record_orders_batches_in_one_call(session):
    """Ten round trips is most of an hour on a local model."""
    payload = json.dumps(
        [
            {
                "commander_id": 308,
                "order": "move",
                "destination": 86,
                "rationale": "reinforce Copper Canyons",
            },
            {"commander_id": 110, "order": "research", "rationale": "keep research moving"},
        ]
    )
    out = ok(session.call("record_orders", {"orders": payload}))
    assert out["recorded"] == 2 and not out["rejected"]
    recorded = {o["commander_id"] for o in ok(session.call("get_orders"))}
    assert {308, 110} <= recorded


def test_record_orders_keeps_the_good_ones_and_reports_the_rest(session):
    """Rejecting the batch would make the model re-derive orders it got right."""
    payload = json.dumps(
        [
            {"commander_id": 308, "order": "defend", "rationale": "hold"},
            {"commander_id": 41, "order": "defend", "rationale": "not ours"},
            {"commander_id": 110, "order": "move", "rationale": "no destination"},
        ]
    )
    out = ok(session.call("record_orders", {"orders": payload}))
    assert out["recorded"] == 1
    assert len(out["rejected"]) == 2
    assert {r["commander_id"] for r in out["rejected"]} == {41, 110}


def test_record_orders_names_a_wrong_key(session):
    """The model has been seen sending "action" instead of "order".

    The resulting error was «order '' has not been verified», which describes
    the symptom rather than the mistake — no use to a small model.
    """
    payload = json.dumps([{"commander_id": 17, "action": "defend", "rationale": "hold the line"}])
    out = ok(session.call("record_orders", {"orders": payload}))
    assert out["recorded"] == 0
    assert "action" in out["rejected"][0]["error"]
    assert "'order'" in out["rejected"][0]["error"]


def test_record_orders_rejects_malformed_json(session):
    err = refused(session.call("record_orders", {"orders": "[{oops}]"}), "bad_call")
    assert "JSON" in err


def test_write_lesson_requires_evidence(session):
    refused(
        session.call("write_lesson", {"topic": "test", "lesson": "something", "evidence": ""}),
        "bad_call",
    )


def test_notes_persist_and_read_back(session):
    ok(session.call("write_note", {"note": "Watch province 56.", "tag": "threat"}))
    notes = ok(session.call("read_scratchpad", {"tag": "threat"}))
    assert any(n["note"] == "Watch province 56." for n in notes)


# -- coverage --------------------------------------------------------------

#: Every tool, and one call that must succeed against the live save. Adding a
#: tool without adding it here fails `test_every_tool_is_exercised`, which is
#: the point: a tool nobody has ever called is a tool nobody knows is broken.
#: `record_gap` and `write_lesson` write to shared tables, so they are covered
#: by their own tests above rather than run here.
SMOKE: dict[str, dict] = {
    "search_illwiki": {"query": "blood fire", "limit": 3},
    "read_illwiki_page": {"page_id": "dom6:abysia-ma", "section": "magic access"},
    "get_turn_summary": {},
    "list_provinces": {},
    "get_province": {"province_id": 93},
    "scout_report": {},
    "list_battle_options": {},
    "get_battle_setup": {"commander_id": 308},
    "get_battle_reports": {},
    "get_turn_messages": {},
    "get_diplomatic_relations": {},
    "get_recruitment_queue": {},
    "get_province_defence": {},
    "get_neighbours": {"province_id": 93},
    "get_movement_options": {"commander_id": 308},
    "get_item_treasury": {},
    "get_magic_economy": {},
    "get_mercenaries": {},
    "get_nation_overview": {},
    "get_global_enchantments": {},
    "get_thrones": {},
    "get_research": {},
    "get_recruitment_options": {"province_id": 93},
    "get_commander_action_options": {"commander_id": 308},
    "change_shape": {
        "commander_id": 124,
        "rationale": "exercise the verified instantaneous form transition",
    },
    "set_diplomatic_action": {
        "nation_id": 85,
        "action": "clear",
        "rationale": "smoke-test the reversible no-pending-action path",
    },
    "set_battle_order": {"commander_id": 308, "stance": "attack", "rationale": "advance"},
    "set_formation": {"commander_id": 308, "squad": 1, "formation": "line", "rationale": "line"},
    "set_battle_position": {
        "commander_id": 308,
        "squad": 1,
        "x": -6,
        "y": 4,
        "rationale": "left flank",
    },
    "set_carried_gems": {
        "commander_id": 308,
        "path": "fire",
        "amount": 1,
        "rationale": "combat magic",
    },
    "set_battle_script": {
        "commander_id": 308,
        "rounds": '["hold_one_turn", "Fire Flies"]',
        "rationale": "delay then cast",
    },
    "assign_troops": {
        "commander_id": 308,
        "squad": 1,
        "unit_instance_ids": "[790]",
        "rationale": "smoke reassignment",
    },
    "detach_troops": {
        "unit_instance_ids": "[790]",
        "rationale": "smoke garrison detachment",
    },
    "create_squad": {
        "commander_id": 308,
        "unit_instance_ids": "[966]",
        "rationale": "smoke new squad",
    },
    # 186 is the Enchanted Helmet, which we actually hold. This read 187
    # until equip_item began checking ownership — an item we have never
    # forged writes cleanly into the .2h and then simply does not appear.
    "equip_item": {"commander_id": 308, "slot": "helm", "item_id": 186, "rationale": "protection"},
    "transfer_item": {
        "source_commander_id": 308,
        "source_slot": "helm",
        "destination_commander_id": 110,
        "destination_slot": "helm",
        "rationale": "move protection to the priest",
    },
    "find_province": {"name": "Marignon"},
    "list_commanders": {},
    "list_units": {},
    "lookup_unit": {"name": "Militia"},
    "lookup_spell": {"name": "Fire Darts"},
    "lookup_item": {"name": "Fire Sword"},
    "lookup_magic_site": {"name": "The House of Fiery Justice"},
    "lookup_weapon": {"name": "Fire Sword"},
    "lookup_armor": {"name": "Full Helmet"},
    "lookup_nation": {"name": "Marignon"},
    "list_research_options": {"school": "alteration"},
    "list_castable_spells": {"commander_id": 308},
    "list_forgeable_items": {"commander_id": 308},
    "list_divine_spells": {"commander_id": 110},
    "list_recruitable": {"province_id": 93},
    "get_province_economics": {},
    "plan_recruitment": {"province_id": 93},
    "queue_recruits": {
        "province_id": 93,
        "units": '[{"unit_type_id": 219, "count": 1}]',
        "rationale": "smoke",
    },
    "set_province_defence": {"province_id": 86, "target": 2, "rationale": "smoke"},
    # Filled in by test_tool_smoke from whatever is actually up for auction:
    # the live save has companies on some turns and none on others, so this
    # is the one tool with no argument set that stays valid across turns.
    "place_mercenary_bid": {},
    # Same: which items are forgeable depends on current research and which
    # mages are alive, so the arguments come from the live save.
    "forge_item": {},
    # And which commander can be empowered depends on who is standing in a
    # province with a laboratory, and on what gems are left.
    "empower_commander": {},
    "queue_research": {"targets": "[]", "rationale": "smoke"},
    "cast_ritual": {"commander_id": 297, "spell_id": 759, "rationale": "convert spare fire gems"},
    "read_scratchpad": {},
    "read_player_guidance": {},
    "read_lessons": {},
    "get_orders": {},
    "get_decision_history": {},
    "get_turn_completion": {},
    "list_order_types": {},
    "record_order": {"commander_id": 308, "order": "defend", "rationale": "hold position"},
    "record_orders": {
        "orders": '[{"commander_id": 110, "order": "research", "rationale": "keep researching"}]'
    },
    "clear_order": {"commander_id": 308},
    "materialize_orders": {},
    # Requires a fully reasoned/materialized turn and is covered by the
    # dedicated completion tests rather than the generic live-save smoke call.
    "complete_turn": {
        "summary": "smoke turn", "outstanding_risks": "none identified"
    },
    # Requires complete_turn and is prepared specially by test_tool_smoke.
    "submit_turn": {"confirm": True},
    "write_note": {"note": "smoke test note"},
    "write_lesson": {"topic": "test", "lesson": "smoke", "evidence": "test"},
    "record_gap": {"subject": "smoke", "reason": "test"},
}


# -- what the newest tools return -----------------------------------------


def test_troop_instances_and_squad_rosters_expose_safe_assignment_ids(troop_state):
    instances = ok(
        troop_state.call(
            "list_units",
            {
                "province_id": 93,
                "include_instances": True,
            },
        )
    )
    by_id = {unit["instance_id"]: unit for unit in instances}
    assert by_id[800]["name"] == "Pikeneer"
    assert by_id[800]["assignment"] is None
    assert by_id[800]["experience"] == 33
    assert by_id[800]["afflictions"] == {"mask": 2, "names": ["curse"]}
    assert by_id[800]["has_fought"] is True
    assert by_id[800]["home_province_id"] == 93
    assert by_id[800]["home_province"] == "Marignon"
    assert by_id[800]["is_mercenary"] is False
    assert by_id[790]["assignment"] == {
        "commander_id": 116,
        "commander_name": "Dapamort",
        "squad": 0,
    }
    setup = ok(troop_state.call("get_battle_setup", {"commander_id": 308}))
    squad = next(row for row in setup["squads"] if row["slot"] == 1)
    assert [(unit["instance_id"], unit["name"]) for unit in squad["units"]] == [(796, "Pikeneer")]

    commanders = ok(troop_state.call("list_commanders"))["commanders"]
    bruise = next(row for row in commanders if row["commander_id"] == 308)
    assert {"experience", "kills", "afflictions", "has_fought"} <= set(bruise)
    assert (bruise["home_province_id"], bruise["home_province"]) == (93, "Marignon")


def test_assign_troops_records_materializes_and_reads_back(troop_state):
    recorded = ok(
        troop_state.call(
            "assign_troops",
            {
                "commander_id": 308,
                "squad": 1,
                "unit_instance_ids": "[800]",
                "rationale": "join Bruise's capital infantry",
            },
        )
    )
    assert recorded["units"] == [
        {
            "instance_id": 800,
            "unit_type_id": 221,
            "name": "Pikeneer",
            "includes_mount": False,
        }
    ]
    intent = next(
        row for row in ok(troop_state.call("get_orders")) if row["order"] == "troop_assignment"
    )
    assert (intent["unit_instance_id"], intent["commander_id"], intent["squad"]) == (800, 308, 1)

    result = ok(troop_state.call("materialize_orders", {"confirm": True}))
    assert any("Pikeneer #800 -> Bruise squad 1" in row for row in result["written"])
    setup = ok(troop_state.call("get_battle_setup", {"commander_id": 308}))
    squad = next(row for row in setup["squads"] if row["slot"] == 1)
    assert {unit["instance_id"] for unit in squad["units"]} == {796, 800}


def test_assign_troops_allows_a_mixed_type_squad(troop_state):
    recorded = ok(troop_state.call(
        "assign_troops",
        {"commander_id": 297, "squad": 0, "unit_instance_ids": "[800]",
         "rationale": "add infantry to the mixed squad"},
    ))
    assert recorded["units"][0]["name"] == "Pikeneer"
    result = ok(troop_state.call("materialize_orders", {"confirm": True}))
    assert not result["skipped"]
    setup = ok(troop_state.call("get_battle_setup", {"commander_id": 297}))
    squad = next(row for row in setup["squads"] if row["slot"] == 0)
    assert 800 in {unit["instance_id"] for unit in squad["units"]}


def test_assign_troops_refuses_cross_province(troop_state):
    error = refused(
        troop_state.call(
            "assign_troops",
            {
                "commander_id": 308,
                "squad": 1,
                "unit_instance_ids": "[2202]",
                "rationale": "wrong province",
            },
        )
    )
    assert "province 98" in error


def test_assign_troops_reports_and_moves_a_mount_with_its_rider(troop_state):
    recorded = ok(
        troop_state.call(
            "assign_troops",
            {
                "commander_id": 297,
                "squad": 0,
                "unit_instance_ids": "[2332]",
                "rationale": "join the cavalry",
            },
        )
    )
    assert recorded["units"][0]["includes_mount"] is True
    result = ok(troop_state.call("materialize_orders", {"confirm": True}))
    assert any("Knight of the Chalice #2332 plus mount" in row for row in result["written"])


def test_detach_troops_records_materializes_and_reads_pending_state(tmp_path):
    baseline = Path("knowledge/snapshots/example_game_2/t3-auto")
    expected = Path("knowledge/snapshots/example_game_2/t3-auto-2")
    if not (baseline / "early_caelum.2h").exists() or not GAME_DB.exists():
        pytest.skip("Caelum detachment controls absent")
    save = tmp_path / "caelum-save"
    save.mkdir()
    for name in ("early_caelum.trn", "early_caelum.2h"):
        shutil.copy2(baseline / name, save / name)
    db = tmp_path / "caelum-game.sqlite3"
    shutil.copy2(GAME_DB, db)
    connection = sqlite3.connect(db)
    try:
        for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE '%_intent'"):
            connection.execute(f'DELETE FROM "{table}"')
        connection.commit()
    finally:
        connection.close()
    session = open_session(
        "example_game_2::early_caelum", game_db=db, save_dir=save,
        nation_id=24)
    try:
        recorded = ok(session.call("detach_troops", {
            "unit_instance_ids": json.dumps(list(range(151, 161))),
            "rationale": "hold ten Spire Horn Warriors in reserve",
        }))
        assert recorded["destination"] == "province_garrison"
        assert {row["source_commander_id"] for row in recorded["units"]} == {5}
        assert {row["source_squad"] for row in recorded["units"]} == {0}
        orders = ok(session.call("get_orders", {}))
        detached = [row for row in orders if row["order"] == "troop_detachment"]
        assert {row["unit_instance_id"] for row in detached} == set(range(151, 161))
        instances = ok(session.call("list_units", {"include_instances": True}))
        staged = [row for row in instances if 151 <= row["instance_id"] <= 160]
        assert all(row["recorded_destination"] == "province_garrison"
                   for row in staged)

        result = ok(session.call("materialize_orders", {"confirm": True}))
        assert not result["skipped"]
        actual = session.ctx.h2_path.read_bytes()
    finally:
        session.close()
    assert actual[:-1] == (expected / "early_caelum.2h").read_bytes()[:-1]


def test_create_squad_records_materializes_and_reads_back(troop_state):
    recorded = ok(
        troop_state.call(
            "create_squad",
            {
                "commander_id": 308,
                "unit_instance_ids": "[966]",
                "rationale": "form a crossbow squad under Bruise",
            },
        )
    )
    assert recorded["squad"] == 0
    assert 1 <= recorded["squad_id"] <= 0xFFFE
    assert recorded["position"] == {"x": 0, "y": 0}
    intents = ok(troop_state.call("get_orders"))
    created = next(row for row in intents if row["order"] == "squad_creation")
    assert (created["commander_id"], created["squad"], created["squad_id"]) == (
        308,
        0,
        recorded["squad_id"],
    )

    result = ok(troop_state.call("materialize_orders", {"confirm": True}))
    assert not result["skipped"]
    assert any("commander 308 squad 0: created" in row for row in result["written"])
    setup = ok(troop_state.call("get_battle_setup", {"commander_id": 308}))
    squad = next(row for row in setup["squads"] if row["slot"] == 0)
    assert squad["position"] == {"x": 0, "y": 0}
    assert [(unit["instance_id"], unit["name"]) for unit in squad["units"]] == [
        (966, "Crossbowman")
    ]


def test_create_first_squad_under_troopless_commander(troop_state):
    """The runtime-index stat join replaces the old follower-location crutch."""
    guarlan = next(
        c for c in ok(troop_state.call("list_commanders"))["commanders"] if c["name"] == "Guarlan"
    )
    assert guarlan["province_id"] == 93
    assert guarlan["troops_led"] == 0

    recorded = ok(
        troop_state.call(
            "create_squad",
            {
                "commander_id": guarlan["commander_id"],
                "unit_instance_ids": "[966]",
                "rationale": "give the troopless Witch Hunter a ranged squad",
            },
        )
    )
    assert recorded["squad"] == 0
    result = ok(troop_state.call("materialize_orders", {"confirm": True}))
    assert not result["skipped"]
    setup = ok(troop_state.call("get_battle_setup", {"commander_id": guarlan["commander_id"]}))
    squad = next(row for row in setup["squads"] if row["slot"] == 0)
    assert [(unit["instance_id"], unit["name"]) for unit in squad["units"]] == [
        (966, "Crossbowman")
    ]


def test_create_squad_accepts_mixed_types(troop_state):
    recorded = ok(troop_state.call(
        "create_squad",
        {"commander_id": 308, "unit_instance_ids": "[966, 967]",
         "rationale": "form a mixed reserve squad"},
    ))
    assert len(recorded["units"]) == 2
    result = ok(troop_state.call("materialize_orders", {"confirm": True}))
    assert not result["skipped"]
    setup = ok(troop_state.call("get_battle_setup", {"commander_id": 308}))
    squad = next(row for row in setup["squads"] if row["slot"] == recorded["squad"])
    assert {unit["instance_id"] for unit in squad["units"]} == {966, 967}


def test_reassigning_every_pending_troop_cancels_new_squad(troop_state):
    """A revised consolidation plan must not recreate an empty old squad."""
    created = ok(troop_state.call(
        "create_squad",
        {"commander_id": 308, "unit_instance_ids": "[966]",
         "rationale": "initially form a separate ranged squad"},
    ))
    assert created["squad"] == 0

    ok(troop_state.call(
        "assign_troops",
        {"commander_id": 308, "squad": 1, "unit_instance_ids": "[966]",
         "rationale": "consolidate the ranged troop into the existing squad"},
    ))
    orders = ok(troop_state.call("get_orders"))
    assert not any(row["order"] == "squad_creation" for row in orders)

    result = ok(troop_state.call("materialize_orders", {"confirm": True}))
    assert not result["skipped"]
    assert not any("created" in row for row in result["written"])
    setup = ok(troop_state.call(
        "get_battle_setup", {"commander_id": 308}))
    assert all(row["slot"] != 0 for row in setup["squads"])
    squad = next(row for row in setup["squads"] if row["slot"] == 1)
    assert 966 in {unit["instance_id"] for unit in squad["units"]}


def test_create_squad_allocates_two_pending_slots_in_multi_marker_caelum(
        tmp_path):
    session = _dual_session_on(
        tmp_path, CAELUM_MULTI_MARKER_STATE, "early_caelum", 24,
        game_name="example_game_2")
    try:
        created = []
        for instance_id in (151, 152):
            created.append(ok(session.call("create_squad", {
                "commander_id": 5,
                "unit_instance_ids": json.dumps([instance_id]),
                "rationale": "Exercise the controlled multi-marker allocator.",
            })))
        assert [row["squad"] for row in created] == [2, 3]

        ok(session.call("materialize_orders", {"confirm": True}))
        data = session.ctx.h2_path.read_bytes()
        block = O.find_order_blocks(data, valid_ids={5})[5]
        slots = {row["slot"]: row for row in O.read_squad_slots(
            data, block.name_end)}
        assert slots[2]["marker"] == slots[3]["marker"] == 7
        units = {unit.instance_id: unit
                 for unit in O.read_h2_units(data, 24)
                 if not unit.is_mount}
        assert units[151].warband == slots[2]["token"]
        assert units[152].warband == slots[3]["token"]
    finally:
        session.close()


def test_create_squad_refuses_attached_troops(troop_state):
    error = refused(
        troop_state.call(
            "create_squad",
            {
                "commander_id": 308,
                "unit_instance_ids": "[796]",
                "rationale": "already attached",
            },
        )
    )
    assert "unattached garrison" in error


def test_new_squad_can_be_configured_before_materializing(troop_state):
    ok(
        troop_state.call(
            "create_squad",
            {
                "commander_id": 308,
                "unit_instance_ids": "[966]",
                "rationale": "form a ranged squad",
            },
        )
    )
    ok(
        troop_state.call(
            "set_battle_position",
            {
                "commander_id": 308,
                "squad": 0,
                "x": -6,
                "y": 4,
                "rationale": "deploy the new squad on the left",
            },
        )
    )
    ok(
        troop_state.call(
            "set_battle_order",
            {
                "commander_id": 308,
                "squad": 0,
                "stance": "hold_and_fire",
                "rationale": "use crossbows at range",
            },
        )
    )
    result = ok(troop_state.call("materialize_orders", {"confirm": True}))
    assert not result["skipped"]
    setup = ok(troop_state.call("get_battle_setup", {"commander_id": 308}))
    squad = next(row for row in setup["squads"] if row["slot"] == 0)
    assert squad["position"] == {"x": -6, "y": 4}
    assert squad["stance"] == "hold_and_fire"


def test_neighbours_are_undirected(pinned):
    """Marignon borders four provinces, and each of them borders Marignon.

    The map stores 50 of its 870 links in one direction only, so adjacency
    read straight out of the table is asymmetric — a province would border
    its neighbour without being bordered back, and a move that the game
    accepts would look illegal.
    """
    out = ok(pinned.call("get_neighbours", {"province_id": 93}))
    got = {n["province_id"]: n for n in out["neighbours"]}
    assert set(got) == {83, 86, 91, 98}
    assert got[86]["status"] == "ours" and got[98]["status"] == "ours"
    for neighbour in got:
        back = ok(pinned.call("get_neighbours", {"province_id": neighbour}))
        assert 93 in {n["province_id"] for n in back["neighbours"]}, neighbour


def test_neighbours_report_connection_counts(pinned):
    """Chokepoints are the point: a neighbour's own degree has to come back."""
    out = ok(pinned.call("get_neighbours", {"province_id": 93}))
    got = {n["province_id"]: n["connections"] for n in out["neighbours"]}
    assert got == {83: 6, 86: 5, 91: 5, 98: 4}


def test_item_treasury_lists_only_unworn_items(tmp_path):
    """Current `.2h` equipment wins over the stale turn-start `.trn` stash.

    The `.trn` lists four items that were unequipped when the turn arrived.
    This save then equips all four, and its `.2h` current-treasury slots are
    four zeroes. Reporting the `.trn` list would offer four already-worn items.
    """
    s = _session_on(tmp_path, STASH)
    try:
        items = ok(s.call("get_item_treasury", {}))["items"]
        assert items == []
    finally:
        s.close()


def test_mercenaries_report_the_whole_auction(tmp_path):
    """All four companies at turn 25, with their contract state.

    This asserted two companies until the record's own constant replaced the
    name-shape test that found them. "The Bowmen" and "Ferrus, the Iron
    Wizard" were always in the file and never reported, so the assistant was
    shown half an auction with nothing to say half was missing — and the
    company it could not see is exactly the one a rival is about to lose.

    "Boggit's Elite Warriors" was also the wrong name; the company is "Fordo
    Boggit's Elite Warriors" in the game's own mercenary table.
    """
    s = _session_on(tmp_path, MERCS)
    try:
        companies = ok(s.call("get_mercenaries", {}))["companies"]
        by_name = {c["company"]: c for c in companies}
        assert set(by_name) == {
            "Fordo Boggit's Elite Warriors",
            "The Bowmen",
            "Ferrus, the Iron Wizard",
            "Magnus's Crossbows",
        }

        boggit = by_name["Fordo Boggit's Elite Warriors"]
        assert (boggit["asking_price"], boggit["troops"]) == (250, 50)
        assert boggit["troop_type"] == "Burgmeister Guard"
        assert boggit["available"] is True
        assert "contracted_to" not in boggit

        # A company under contract is still listed: it can be bid on, and the
        # month it comes free is the thing worth planning around.
        bowmen = by_name["The Bowmen"]
        assert bowmen["available"] is False
        assert bowmen["contract_months_left"] == 1
        assert bowmen["contracted_to"]
    finally:
        s.close()


def test_single_round_orders_name_their_spells(tmp_path):
    """All five slots, mixing cast orders with the game's fixed choices."""
    s = _session_on(tmp_path, SPELLS)
    try:
        setup = ok(s.call("get_battle_setup", {"commander_id": 9}))
        assert setup["single_round_orders"] == [
            "cast Fire Flies",
            "hold one turn",
            "hold or cast a spell",
            "attack one turn",
            "fly attack one turn",
        ]
    finally:
        s.close()


def test_nation_overview_names_our_own_pretender(pinned):
    """Our pretender is not in the .trn name table — that table is foreign.

    Sugaar is commander 297 in both files and named in only the .2h, so
    reading the name from the .trn returns None for the one commander the
    player is most certain to know.
    """
    out = ok(pinned.call("get_nation_overview", {}))
    assert out["pretender"] == {
        "commander_id": 297,
        "name": "Sugaar",
        "title": "King of Kings, Eater of Filth",
        "status": "available_or_dormant",
        "dead": False,
        "basis": "pretender id has a writable commander block",
        "call_god_recall": None,
    }
    assert out["prophet"] == {"commander_id": 87, "name": "Floredee"}


def test_hall_of_fame_is_ten_entries(pinned):
    """Fixed size. The greedy read that preceded it gave 10 to 19, with
    duplicate ids, depending on what followed the table in the file."""
    hof = ok(pinned.call("get_nation_overview", {}))["hall_of_fame"]
    assert len(hof) == 10
    assert [h["rank"] for h in hof] == list(range(1, 11))
    ids = [h["commander_id"] for h in hof]
    assert len(set(ids)) == 10


def test_named_heroic_ability_is_exposed_for_our_commander(tmp_path):
    snapshot = Path("knowledge/snapshots/t6")
    s = _session_on(tmp_path, snapshot)
    try:
        commanders = ok(s.call("list_commanders", {}))["commanders"]
        clodius = next(row for row in commanders if row["commander_id"] == 309)
        assert clodius["heroic_ability"] == {
            "ability_id": 7,
            "name": "Heroic Toughness",
        }

        hall = ok(s.call("get_nation_overview", {}))["hall_of_fame"]
        clodius_hall = next(row for row in hall if row["commander_id"] == 309)
        assert clodius_hall["ours"]
        assert clodius_hall["heroic_ability"] == {
            "ability_id": 7,
            "name": "Heroic Toughness",
        }
    finally:
        s.close()


def test_equip_item_refuses_an_item_we_do_not_own(session):
    """The .2h takes the order cleanly and the item simply never appears."""
    error = refused(
        session.call(
            "equip_item", {"commander_id": 308, "slot": "helm", "item_id": 500, "rationale": "test"}
        )
    )
    assert "do not own" in error


def test_equip_item_refuses_a_non_atomic_commander_transfer(session):
    """Setting the target without clearing the source duplicates the item."""
    error = refused(
        session.call(
            "equip_item",
            {"commander_id": 307, "slot": "helm", "item_id": 186, "rationale": "reassign"},
        )
    )
    assert "already worn" in error and "transfer" in error


def test_research_reports_levels_and_the_gap_to_the_next(pinned):
    """Turn 23: Conjuration 2 with 195 banked of the 200 level 3 costs.

    Those five remaining points are the whole reason the level cost curve is
    worth getting right — turn 24 rolls over to level 3 with 2 left, which is
    195 + 7 - 200, and only a level-3 cost of 200 gives that remainder.
    """
    out = ok(pinned.call("get_research", {}))
    assert out["research_points"] == 445
    by_school = {s["school"]: s for s in out["schools"]}
    conj = by_school["Conjuration"]
    assert (conj["level"], conj["progress"]) == (2, 195)
    assert conj["next_level_costs"] == 200
    assert conj["points_to_next_level"] == 5
    assert by_school["Construction"]["level"] == 1
    assert by_school["Evocation"]["level"] == 0


@pytest.mark.parametrize(
    ("stem", "nation_id", "total", "school", "level", "progress"),
    [
        ("mid_ermor", 54, 9433, "Conjuration", 5, 1283),
        ("mid_marignon", 61, 9856, "Alteration", 5, 318),
    ],
)
def test_dual_human_profiles_read_their_own_complete_research(
    tmp_path, stem, nation_id, total, school, level, progress
):
    session = _dual_session_on(
        tmp_path, DUAL_RESEARCH_STATE, stem, nation_id
    )
    try:
        out = ok(session.call("get_research", {}))
    finally:
        session.close()
    assert out["research_points"] == total
    row = next(entry for entry in out["schools"] if entry["school"] == school)
    assert (row["level"], row["progress"]) == (level, progress)


def test_research_reports_the_saved_nine_entry_queue(research_full):
    out = ok(research_full.call("get_research", {}))
    assert [(entry["school"], entry["target_level"]) for entry in out["queue"]] == [
        ("Alteration", 1),
        ("Alteration", 2),
        ("Evocation", 1),
        ("Conjuration", 4),
        ("Construction", 2),
        ("Enchantment", 1),
        ("Thaumaturgy", 2),
        ("Blood Magic", 1),
        ("Conjuration", 5),
    ]
    assert out["queue_capacity"] == 9


def test_queue_research_derives_levels_and_refuses_level_nine(research_full):
    result = ok(
        research_full.call(
            "queue_research",
            {
                "targets": '["Alteration", "Alteration", "Evocation"]',
                "rationale": "unlock alteration before evocation",
            },
        )
    )
    assert [(entry["school"], entry["target_level"]) for entry in result["queue"]] == [
        ("Alteration", 1),
        ("Alteration", 2),
        ("Evocation", 1),
    ]

    refused_message = refused(
        research_full.call(
            "queue_research",
            {
                "targets": "[" + ",".join(['"Conjuration"'] * 6) + "]",
                "rationale": "attempt level nine",
            },
        )
    )
    assert "Level 9" in refused_message and "spell" in refused_message


def test_level_nine_spells_share_the_research_queue(research_level9):
    out = ok(research_level9.call("get_research", {}))
    assert [(entry["kind"], entry.get("spell_id")) for entry in out["queue"][-2:]] == [
        ("level_9_spell", 1080),
        ("level_9_spell", 1078),
    ]
    assert [entry["spell"] for entry in out["queue"][-2:]] == ["Tartarian Gate", "Ghost Riders"]
    assert all(entry["verified_level_9"] for entry in out["queue"][-2:])


def test_queue_research_validates_level_nine_order(research_level9):
    targets = (["Conjuration"] * 5) + [1080, "Ghost Riders"]
    result = ok(
        research_level9.call(
            "queue_research",
            {
                "targets": json.dumps(targets),
                "rationale": "reach eight, then select two spells",
            },
        )
    )
    assert [(entry["kind"], entry.get("spell_id")) for entry in result["queue"][-2:]] == [
        ("level_9_spell", 1080),
        ("level_9_spell", 1078),
    ]

    error = refused(
        research_level9.call("queue_research", {"targets": "[1080]", "rationale": "skip to nine"})
    )
    assert "reaches only Level 3" in error


def test_cast_augury_records_and_materializes_complete_ritual(ritual_baseline):
    from dom6_assistant.file_reader.formats import h2
    from dom6_assistant.orders.orders_2h import (
        find_order_blocks,
        read_ritual_fields,
    )

    recorded = ok(
        ritual_baseline.call(
            "cast_ritual",
            {
                "commander_id": 297,
                "spell_id": 1283,
                "target_province": 86,
                "rationale": "search Copper Canyons for fire sites",
            },
        )
    )
    assert recorded["spell"] == "Augury"
    assert recorded["gem_cost"] == 2
    assert recorded["target_name"] == "Copper Canyons"
    assert recorded["target_range"] == {"distance": 1, "maximum": 5}

    intent = ok(ritual_baseline.call("get_orders", {}))[-1]
    assert (intent["spell_id"], intent["target_province"], intent["monthly"]) == (1283, 86, False)

    result = ok(ritual_baseline.call("materialize_orders", {"confirm": True}))
    assert not result["skipped"]
    data = ritual_baseline.ctx.h2_path.read_bytes()
    block = find_order_blocks(data)[297]
    ritual = read_ritual_fields(data, block.name_end)
    assert ritual is not None
    assert (ritual.spell_id, ritual.gem_cost, ritual.target_province) == (1283, 2, 86)
    assert h2.gem_remaining(data)[0] == 39
    shown = next(
        c
        for c in ok(ritual_baseline.call("list_commanders", {}))["commanders"]
        if c["commander_id"] == 297
    )
    assert shown["order_parameter_in_file"] == {
        "spell_id": 1283,
        "spell": "Augury",
        "gem_cost": 2,
        "base_gem_cost": 2,
        "extra_gems": 0,
        "target_province": 86,
        "target_province_name": "Copper Canyons",
        "monthly": False,
    }


def test_cast_ritual_refuses_a_province_beyond_its_map_range(
        ritual_baseline, monkeypatch):
    """Exercise the public refusal even though this fixture owns no distant land.

    Augury's real extracted range is covered separately as five.  Narrowing that
    one datum to zero lets this integration fixture reach the range check with
    its adjacent, friendly target instead of failing the friendly-only rule
    first.
    """
    from dom6_assistant.reference import ritual_range

    original = ritual_range._spell_attribute

    def narrowed_augury_range(reference_conn, spell_id, attribute):
        if spell_id == 1283 and attribute == ritual_range.PROVINCE_RANGE_ATTRIBUTE:
            return 0
        return original(reference_conn, spell_id, attribute)

    monkeypatch.setattr(
        ritual_range, "_spell_attribute", narrowed_augury_range)
    result = ritual_baseline.call("cast_ritual", {
        "commander_id": 297,
        "spell_id": 1283,
        "target_province": 86,
        "rationale": "attempt an Augury beyond the test range",
    })
    assert not result["ok"]
    assert "1 province" in result["error"]
    assert "maximum range 0" in result["error"]


def test_materializer_revalidates_a_recorded_ritual_range(
        ritual_baseline, monkeypatch):
    from dom6_assistant.reference import ritual_range

    ok(ritual_baseline.call("cast_ritual", {
        "commander_id": 297,
        "spell_id": 1283,
        "target_province": 86,
        "rationale": "record a legal nearby Augury",
    }))
    original = ritual_range._spell_attribute

    def narrowed_augury_range(reference_conn, spell_id, attribute):
        if spell_id == 1283 and attribute == ritual_range.PROVINCE_RANGE_ATTRIBUTE:
            return 0
        return original(reference_conn, spell_id, attribute)

    monkeypatch.setattr(
        ritual_range, "_spell_attribute", narrowed_augury_range)

    result = ok(ritual_baseline.call(
        "materialize_orders", {"confirm": True}))
    assert result["aborted"] is True
    assert any("maximum range 0" in skipped for skipped in result["skipped"])


def test_inherited_transport_ritual_names_its_recipient(tmp_path):
    session = _session_on(
        tmp_path, Path("knowledge/snapshots/t35-teleport-five-gems")
    )
    try:
        commanders = ok(session.call("list_commanders", {}))["commanders"]
    finally:
        session.close()
    bretaigne = next(row for row in commanders if row["commander_id"] == 126)
    assert bretaigne["order_parameter_in_file"] == {
        "spell_id": 1288,
        "spell": "Teleport Gems",
        "gem_cost": 2,
        "base_gem_cost": 2,
        "extra_gems": 0,
        "monthly": False,
        "target_province": 86,
        "target_province_name": "Copper Canyons",
        "target_commander": 87,
        "target_commander_name": "Floredee",
    }


def test_inherited_dispel_names_global_slot_and_enchantment(tmp_path):
    session = _dual_session_on(
        tmp_path,
        Path("knowledge/snapshots/example_game/t28-auto-3"),
        "mid_marignon",
        61,
    )
    try:
        commanders = ok(session.call("list_commanders", {}))["commanders"]
    finally:
        session.close()
    francor = next(row for row in commanders if row["commander_id"] == 38)
    assert francor["order_parameter_in_file"] == {
        "spell_id": 1180,
        "spell": "Dispel",
        "gem_cost": 32,
        "base_gem_cost": 30,
        "extra_gems": 2,
        "monthly": False,
        "target_global_slot": 0,
        "target_global": 10,
        "target_global_spell_id": 1339,
        "target_global_spell": "Foul Air",
        "overcast": 2,
    }


def test_effect_21_reviver_is_targetless_monthly_and_writable(tmp_path):
    from dom6_assistant.orders.orders_2h import find_order_blocks, read_ritual_fields

    session = _dual_session_on(
        tmp_path,
        Path("knowledge/snapshots/example_game/t18-auto-2"),
        "mid_ermor",
        54,
    )
    try:
        spells = ok(
            session.call(
                "list_castable_spells",
                {"commander_id": 74, "kind": "ritual"},
            )
        )["spells"]
        dusk = next(row for row in spells if row["id"] == 389)
        assert (dusk["target"], dusk["order_supported"]) == ("none", True)

        recorded = ok(
            session.call(
                "cast_ritual",
                {
                    "commander_id": 74,
                    "spell_id": 389,
                    "monthly": True,
                    "rationale": "continue reviving Dusk Elders",
                },
            )
        )
        assert (recorded["spell"], recorded["gem_cost"], recorded["monthly"]) == (
            "Revive Dusk Elder",
            20,
            True,
        )
        ok(session.call("materialize_orders", {"confirm": True}))
        data = session.ctx.h2_path.read_bytes()
        fields = read_ritual_fields(data, find_order_blocks(data)[74].name_end)
        assert fields is not None
        assert fields.spell_id == 389
        assert fields.gem_cost == 20
        assert fields.target_province is None
        assert fields.target_commander_runtime_index is None
        assert fields.monthly is True
    finally:
        session.close()


def test_cast_ritual_requires_the_correct_target_shape(ritual_baseline):
    error = refused(
        ritual_baseline.call(
            "cast_ritual",
            {
                "commander_id": 297,
                "spell_id": 1283,
                "target_province": 86,
                "extra_gems": 1,
                "rationale": "ordinary rituals have a fixed listed cost",
            },
        )
    )
    assert "only for world global enchantments and Dispel" in error

    error = refused(
        ritual_baseline.call(
            "cast_ritual", {"commander_id": 297, "spell_id": 1283, "rationale": "missing target"}
        )
    )
    assert "province-targeted" in error and "target_province" in error

    error = refused(
        ritual_baseline.call(
            "cast_ritual",
            {
                "commander_id": 297,
                "spell_id": 759,
                "target_province": 86,
                "rationale": "invalid target",
            },
        )
    )
    assert "takes no target_province" in error

    error = refused(
        ritual_baseline.call(
            "cast_ritual",
            {"commander_id": 297, "spell_id": 474, "rationale": "foreign national ritual"},
        )
    )
    assert "restricted to nation id(s) [72]" in error

    # Carrier Birds delivers to a commander. The payload field is decoded
    # now, so the refusal names the missing recipient rather than the missing
    # decode.
    error = refused(
        ritual_baseline.call(
            "cast_ritual",
            {
                "commander_id": 297,
                "spell_id": 1284,
                "target_province": 93,
                "rationale": "missing recipient",
            },
        )
    )
    assert "target_commander is required" in error


def test_cast_ritual_refuses_a_battle_spell(ritual_baseline):
    # Fire Darts is spell 598 in the reference database and not a ritual.
    fire_darts = ok(ritual_baseline.call("lookup_spell", {"name": "Fire Darts"}))[0]
    error = refused(
        ritual_baseline.call(
            "cast_ritual",
            {"commander_id": 297, "spell_id": fire_darts["id"], "rationale": "wrong spell class"},
        )
    )
    assert "not marked as a ritual" in error


def test_monthly_ritual_uses_code_60_and_reserves_only_one_cast(ritual_baseline):
    from dom6_assistant.file_reader.formats import h2
    from dom6_assistant.orders.orders_2h import (
        find_order_blocks,
        read_ritual_fields,
    )

    ok(
        ritual_baseline.call(
            "cast_ritual",
            {
                "commander_id": 297,
                "spell_id": 759,
                "monthly": True,
                "rationale": "convert fire income every month",
            },
        )
    )
    ok(ritual_baseline.call("materialize_orders", {"confirm": True}))
    data = ritual_baseline.ctx.h2_path.read_bytes()
    block = find_order_blocks(data)[297]
    ritual = read_ritual_fields(data, block.name_end)
    assert block.order_code == 60
    assert ritual is not None and ritual.monthly
    assert h2.gem_remaining(data)[0] == 31  # 41 - this month's 10 gems


def test_only_diplomacy_accepts_a_nation_id(session):
    """A target nation is valid only for the own-row diplomacy writer.

    In particular, no research or economy reader may turn the global nation
    records in a .trn into a rival-state oracle.
    """
    tools_with_nation_id = set()
    for tool in session.registry.names():
        params = {p.name for p in session.registry.get(tool).params}
        if "nation_id" in params:
            tools_with_nation_id.add(tool)
    assert tools_with_nation_id == {"set_diplomatic_action"}


# -- what is left after queueing ------------------------------------------


def test_queue_reports_what_gold_is_left(session):
    """Gold is the one budget that is exact, so it must actually subtract."""
    units = json.dumps([{"unit_type_id": 134, "count": 3}])  # Royal Guard, 50g
    ok(session.call("queue_recruits", {"province_id": 93, "units": units, "rationale": "budget"}))
    ok(session.call("materialize_orders", {"confirm": True}))
    gold = ok(session.call("plan_recruitment", {"province_id": 93}))["gold"]
    assert gold["committed_here"] == 150
    assert gold["remaining"] == gold["treasury"] - gold["newly_committed"]


def test_holy_points_are_a_budget_not_a_one_per_turn_rule(session):
    """Two sacreds spend two of the capital's six holy points.

    This asserted that a second sacred would WAIT, from a claim that a
    province could build one per turn. The player corrected it against the
    running game: six holy points a turn, one per sacred unit. What had
    actually delayed those units was resources going negative.
    """
    units = json.dumps(
        [
            {"unit_type_id": 217, "count": 1},  # Flagellant
            {"unit_type_id": 135, "count": 2},
        ]
    )  # Knight, sacred
    ok(session.call("queue_recruits", {"province_id": 93, "units": units, "rationale": "sacred"}))
    ok(session.call("materialize_orders", {"confirm": True}))
    holy = ok(session.call("plan_recruitment", {"province_id": 93}))["holy_points"]
    assert holy["cost_per_sacred_unit"] == 1
    assert holy["spent_by_queue"] == 3
    assert holy["available"] == 6
    assert holy["remaining"] == 3


def test_holy_allowance_is_current_computed_and_nation_wide(session):
    session.ctx.game_db.execute("DELETE FROM province_economics")
    session.ctx.game_db.commit()
    rows = ok(session.call("get_province_economics"))["provinces"]
    ours = {row["province_id"]: row for row in rows if row["province_id"] in (86, 93, 98)}
    assert {pid: row["holy_points"] for pid, row in ours.items()} == {86: 6, 93: 6, 98: 6}
    assert all(row["holy_points_exact"] for row in ours.values())
    capital = ours[93]
    assert capital["base_dominion"] == 6
    assert capital["maximum_dominion"] == 6
    assert capital["own_temples"] == 2
    assert capital["disciple_nations"] == 1

    options = ok(session.call("get_recruitment_options", {"province_id": 93}))
    holy = options["budgets"]["holy_points"]
    assert holy["available"] == 6 and holy["exact"] is True


def test_exceptional_sacreds_use_explicit_holy_cost(session):
    from dom6_assistant.agent.read_tools import _holy_cost

    assert _holy_cost(session.ctx, 135) == (True, 1)
    assert _holy_cost(session.ctx, 844) == (True, 2)
    assert _holy_cost(session.ctx, 3464) == (True, 3)
    assert _holy_cost(session.ctx, 218) == (False, 0)


def test_recruitment_options_price_every_budget(session):
    """Base-game resource and recruitment costs come from the executable."""
    out = ok(session.call("get_recruitment_options", {"province_id": 93}))
    assert out["treasury_gold"] > 0
    assert out["budgets"]["recruitment_points"]["available"] == 477
    assert out["budgets"]["commander_points"]["available"] == 3
    by_name = {u["name"]: u for u in out["can_build"]}

    knight = by_name["Knight of the Chalice"]
    assert knight["sacred"] is True and knight["holy_points"] == 1
    assert (knight["gold"], knight["resources"]) == (70, 58)
    assert knight["recruit_points"] == 51
    assert knight["commander_points"] == 0  # a troop, not a commander

    master = by_name["Grand Master"]
    assert master["is_commander"] is True and master["commander_points"] == 4
    assert master["recruit_points"] == 34
    assert by_name["Swordsman"]["holy_points"] == 0

    assert all(u["recruit_points"] is not None for u in out["can_build"])
    assert not any("RECRUITMENT POINT" in g for g in out["gaps"])


def test_resources_and_points_stay_exact_without_a_panel_reading(session):
    """Both budgets now compute from current save inputs."""
    session.ctx.game_db.execute("DELETE FROM province_economics")
    session.ctx.game_db.commit()
    out = ok(session.call("plan_recruitment", {"province_id": 86}))
    assert out["recruit_points_available"] == 94
    assert out["recruit_points_source"] == "computed from the .trn"
    assert out["resources_available"] == 42
    assert out["resources_exact"] is True
    assert out["resources"]["remaining"] == 42
    assert "current and computed" in out["note"]


def test_recruitment_point_remainder_is_exact(pinned):
    """The province formula and executable unit costs give an exact remainder."""
    out = ok(pinned.call("plan_recruitment", {"province_id": 93}))
    points = out["recruitment_points"]
    assert points["available"] == 477
    assert points["spent_by_queue"] == 0
    assert points["remaining"] == 477
    assert "why_unknown" not in points


def test_every_tool_is_exercised(session):
    """No tool ships without at least one call that is known to work."""
    registered = set(session.registry.names())
    missing = registered - set(SMOKE)
    assert not missing, f"tools with no smoke call: {sorted(missing)}"
    stale = set(SMOKE) - registered
    assert not stale, f"smoke calls for tools that no longer exist: {sorted(stale)}"


def _explicit_defend_every_commander(session, rationale="reviewed for handoff"):
    commanders = ok(session.call("list_commanders"))["commanders"]
    ok(session.call("record_orders", {"orders": json.dumps([
        {"commander_id": row["commander_id"], "order": "defend",
         "rationale": rationale}
        for row in commanders
    ])}))
    return commanders


def test_turn_completion_requires_materialization_and_becomes_stale(session):
    commanders = _explicit_defend_every_commander(session)
    refused = session.call("complete_turn", {
        "summary": "Hold the current position.",
        "outstanding_risks": "none identified",
    })
    assert refused["ok"] is False
    assert "last materialized output" in refused["error"]

    ok(session.call("materialize_orders", {"confirm": True}))
    completed = ok(session.call("complete_turn", {
        "summary": "Hold the current position.",
        "outstanding_risks": "none identified",
    }))
    assert completed["complete"] is True
    assert completed["commanders_explicitly_decided"] == len(commanders)
    assert ok(session.call("get_turn_completion"))["complete"] is True

    changed = commanders[0]
    ok(session.call("record_order", {
        "commander_id": changed["commander_id"], "order": "defend",
        "rationale": "changed rationale after the handoff",
    }))
    stale = ok(session.call("get_turn_completion"))
    assert stale["complete"] is False
    assert "decisions changed" in stale["reason"]


def test_submit_turn_marks_flags_and_allows_revision_before_hosting(
        session):
    commanders = _explicit_defend_every_commander(session)
    unconfirmed = session.call("submit_turn", {"confirm": False})
    assert unconfirmed["ok"] is False
    assert "requires confirm=true" in unconfirmed["error"]
    premature = session.call("submit_turn", {"confirm": True})
    assert premature["ok"] is False
    assert "not ready for submission" in premature["error"]

    ok(session.call("materialize_orders", {"confirm": True}))
    ok(session.call("complete_turn", {
        "summary": "Hold the current position.",
        "outstanding_risks": "none identified",
    }))
    before = session.ctx.h2_path.read_bytes()
    offsets = H2.submission_flag_offsets(before)
    trailer = before[-2:]

    submitted = ok(session.call("submit_turn", {"confirm": True}))
    after = session.ctx.h2_path.read_bytes()
    assert submitted["submitted"] is True
    assert submitted["already_submitted"] is False
    assert submitted["changed_offsets"] == list(offsets)
    assert submitted["trailer_preserved"] is True
    assert after[-2:] == trailer
    assert H2.turn_is_submitted(after) is True
    assert [i for i, (a, b) in enumerate(zip(before, after)) if a != b] \
        == list(offsets)
    assert M.matches_last_written(session.ctx.h2_path)

    status = ok(session.call("get_turn_completion"))
    assert status["complete"] is True
    assert status["submitted"] is True
    assert status["submitted_at"] is not None
    assert status["summary"] == "Hold the current position."
    assert len(commanders) > 0

    repeated = ok(session.call("submit_turn", {"confirm": True}))
    assert repeated["already_submitted"] is True

    changed = commanders[0]
    ok(session.call("record_order", {
        "commander_id": changed["commander_id"],
        "order": "defend",
        "rationale": "reconsidered after the first submission",
    }))
    reopened = ok(session.call("materialize_orders", {"confirm": True}))
    assert reopened["reopened_submitted_turn"] is True
    assert H2.turn_is_submitted(session.ctx.h2_path.read_bytes()) is False
    stale = ok(session.call("get_turn_completion"))
    assert stale["complete"] is False
    assert stale["submitted"] is False
    assert stale["submitted_at"] is None

    premature_resubmit = session.call("submit_turn", {"confirm": True})
    assert premature_resubmit["ok"] is False
    assert "not ready for submission" in premature_resubmit["error"]
    ok(session.call("complete_turn", {
        "summary": "Hold after revising the current submission.",
        "outstanding_risks": "none identified",
    }))
    resubmitted = ok(session.call("submit_turn", {"confirm": True}))
    assert resubmitted["already_submitted"] is False
    assert H2.turn_is_submitted(session.ctx.h2_path.read_bytes()) is True


def test_diplomacy_reader_and_nap_writer_round_trip(diplomacy_state):
    before = ok(diplomacy_state.call("get_diplomatic_relations", {}))
    ys = next(row for row in before["relations"] if row["nation_id"] == 85)
    assert ys["status"] == "apprehensive"
    assert ys["controller"] == "AI"
    assert ys["pending_action"] is None

    recorded = ok(
        diplomacy_state.call(
            "set_diplomatic_action",
            {
                "nation_id": 85,
                "action": "propose_nap",
                "rationale": "secure the new border at Kratas",
            },
        )
    )
    assert recorded["missive"] == "We propose peace and a non-aggression treaty"
    staged = ok(diplomacy_state.call("get_diplomatic_relations", {}))
    ys = next(row for row in staged["relations"] if row["nation_id"] == 85)
    assert ys["recorded_action"] == "propose_nap"
    assert ys["pending_action"] is None
    orders = ok(diplomacy_state.call("get_orders", {}))
    assert any(
        row["order"] == "diplomacy"
        and row["nation_id"] == 85
        and row["action"] == "propose_nap"
        for row in orders
    )
    preview = ok(diplomacy_state.call("materialize_orders", {}))
    assert any("nation 85: propose_nap" in row for row in preview["written"])
    ok(diplomacy_state.call("materialize_orders", {"confirm": True}))

    after = ok(diplomacy_state.call("get_diplomatic_relations", {}))
    ys = next(row for row in after["relations"] if row["nation_id"] == 85)
    assert ys["pending_action"] == "propose_nap"


def test_diplomacy_acceptance_matches_controlled_client_save(tmp_path):
    session = _dual_session_on(
        tmp_path, INCOMING_NAP_STATE, "mid_marignon", 61
    )
    try:
        before = ok(session.call("get_diplomatic_relations", {}))
        ermor = next(row for row in before["relations"] if row["nation_id"] == 54)
        assert ermor["incoming_action"] == "propose_nap"
        assert ermor["pending_action"] is None

        recorded = ok(session.call("set_diplomatic_action", {
            "nation_id": 54,
            "action": "accept_nap",
            "rationale": "accept the proposed terms",
        }))
        assert recorded["missive"] == "We accept the non-aggression treaty"
        preview = ok(session.call("materialize_orders", {}))
        assert "nation 54: accept_nap" in preview["written"]
        ok(session.call("materialize_orders", {"confirm": True}))

        after = ok(session.call("get_diplomatic_relations", {}))
        ermor = next(row for row in after["relations"] if row["nation_id"] == 54)
        assert ermor["incoming_action"] == "propose_nap"
        assert ermor["pending_action"] == "accept_nap"
        expected = (ACCEPTED_NAP_STATE / "mid_marignon.2h").read_bytes()
        assert session.ctx.h2_path.read_bytes()[:-2] == expected[:-2]

        # Responses are outgoing records until hosting, so they can be replaced
        # without changing the underlying relation this turn.
        decline = ok(session.call("set_diplomatic_action", {
            "nation_id": 54,
            "action": "decline_nap",
            "rationale": "reject the terms before submitting the turn",
        }))
        assert decline["missive"] == "We decline the non-aggression treaty"
        ok(session.call("materialize_orders", {"confirm": True}))
        expected = (DECLINED_NAP_STATE / "mid_marignon.2h").read_bytes()
        assert session.ctx.h2_path.read_bytes()[:-2] == expected[:-2]

        # It can also be cleared to leave the incoming proposal unanswered.
        ok(session.call("set_diplomatic_action", {
            "nation_id": 54,
            "action": "clear",
            "rationale": "reconsider before submitting the turn",
        }))
        ok(session.call("materialize_orders", {"confirm": True}))
        assert D.find_outgoing_diplomacy(session.ctx.h2_path.read_bytes())[2] == []
    finally:
        session.close()


@pytest.mark.parametrize("action", ["accept_nap", "decline_nap"])
def test_diplomacy_response_requires_current_incoming_offer(
    diplomacy_state, action
):
    refused = diplomacy_state.call(
        "set_diplomatic_action",
        {"nation_id": 85, "action": action, "rationale": "test"},
    )
    assert refused["ok"] is False
    assert "has not sent us a current NAP proposal" in refused["error"]


def test_diplomacy_declaration_requires_a_missive_and_writes_it_verbatim(diplomacy_state):
    """The declaration text is real delivered content, so it is never invented.

    A hosted control proved the stored missive is embedded verbatim in the
    target's message, and that the client's own default wording is written per
    nation in the binary.  The tool therefore refuses to compose one.
    """
    missing = diplomacy_state.call(
        "set_diplomatic_action",
        {
            "nation_id": 85,
            "action": "declare_war",
            "rationale": "open hostilities on the new border",
        },
    )
    assert missing["ok"] is False
    assert "requires a missive" in missing["error"]

    text = "A message from Sugaar\n\nYour heresy ends at our border."
    recorded = ok(
        diplomacy_state.call(
            "set_diplomatic_action",
            {
                "nation_id": 85,
                "action": "declare_war",
                "rationale": "open hostilities on the new border",
                "missive": text,
            },
        )
    )
    assert recorded["missive"] == text
    ok(diplomacy_state.call("materialize_orders", {"confirm": True}))
    rows = D.find_outgoing_diplomacy(diplomacy_state.ctx.h2_path.read_bytes())[2]
    assert [(row.target_nation_id, row.action) for row in rows] == [(85, "declare_war")]
    assert rows[0].text == text


def test_missive_is_rejected_for_actions_with_fixed_game_wording(diplomacy_state):
    refused = diplomacy_state.call(
        "set_diplomatic_action",
        {
            "nation_id": 85,
            "action": "propose_nap",
            "rationale": "test",
            "missive": "let us be friends",
        },
    )
    assert refused["ok"] is False
    assert "fixed wording" in refused["error"]


def test_diplomacy_writer_refuses_no_contact_and_defeated_nations(diplomacy_state):
    no_contact = diplomacy_state.call(
        "set_diplomatic_action",
        {"nation_id": 74, "action": "propose_nap", "rationale": "test"},
    )
    assert no_contact["ok"] is False
    assert "no contact" in no_contact["error"]
    defeated = diplomacy_state.call(
        "set_diplomatic_action",
        {"nation_id": 87, "action": "declare_war", "rationale": "test"},
    )
    assert defeated["ok"] is False
    assert "defeated" in defeated["error"]


@pytest.mark.parametrize("name", sorted(SMOKE))
def test_tool_smoke(session, name, tmp_path):
    """Each tool returns successfully on a call it should accept.

    `clear_order` runs after `record_order` in the same session only when
    parametrisation happens to order them that way, so it seeds its own row.
    """
    if name == "clear_order":
        session.call(
            "record_order", {"commander_id": 308, "order": "defend", "rationale": "seed for clear"}
        )
    args = SMOKE[name]
    if name == "place_mercenary_bid":
        args = _live_bid_args(session)
    if name == "forge_item":
        args = _live_forge_args(session)
    if name == "empower_commander":
        args = _live_empowerment_args(session)
    if name == "transfer_item":
        ok(session.call(
            "equip_item",
            {"commander_id": 308, "slot": "helm", "item_id": 186,
             "rationale": "seed the transfer source"},
        ))
    if name in ("complete_turn", "submit_turn"):
        commanders = ok(session.call("list_commanders"))["commanders"]
        orders = [
            {"commander_id": row["commander_id"], "order": "defend",
             "rationale": "explicit smoke-test decision"}
            for row in commanders
        ]
        ok(session.call("record_orders", {"orders": json.dumps(orders)}))
        ok(session.call("materialize_orders", {"confirm": True}))
        if name == "submit_turn":
            ok(session.call("complete_turn", {
                "summary": "smoke turn",
                "outstanding_risks": "none identified",
            }))
    if name == "change_shape":
        shape_session = _shape_session_on(tmp_path, SHAPE_CHANGED_STATE)
        try:
            result = shape_session.call(name, args)
        finally:
            shape_session.close()
    else:
        result = session.call(name, args)
    assert result["ok"], f"{name} failed: {result.get('error')}"


def _live_empowerment_args(session) -> dict:
    """An empowerment the live save can afford, or skip if there is none.

    Needs a commander in a province with a laboratory, and enough gems of some
    path for that commander's next level in it. Fixed-snapshot coverage lives
    in test_empowerment.py.
    """
    from dom6_assistant.reference import empowerment_cost as EC

    gems = ok(session.call("get_magic_economy", {})).get("uncommitted", {})
    letters = {
        "fire": "F",
        "air": "A",
        "water": "W",
        "earth": "E",
        "astral": "S",
        "death": "D",
        "nature": "N",
        "glamour": "G",
        "blood": "B",
    }
    for commander in ok(session.call("list_commanders", {}))["commanders"]:
        province = session.call("get_province", {"province_id": commander["province_id"]})
        if not province["ok"] or not province["result"].get("has_laboratory"):
            continue
        for path, letter in letters.items():
            have = int(gems.get(path, 0))
            target = int(commander.get("paths", {}).get(letter, 0)) + 1
            if have >= EC.empowerment_cost(target):
                return {
                    "commander_id": commander["commander_id"],
                    "path": path,
                    "rationale": "smoke empowerment",
                }
    pytest.skip("no affordable empowerment in the live save")


def _live_forge_args(session) -> dict:
    """A forge the live save will accept, or skip if nothing is forgeable.

    Fixed-snapshot coverage lives in test_forge_writer.py; this only checks the
    tool still runs against whatever the game wrote most recently. Multi-path
    items are skipped because the writer deliberately refuses them.
    """
    commanders = ok(session.call("list_commanders", {}))["commanders"]
    for commander in commanders:
        if not any(commander.get("paths", {}).values()):
            continue
        listing = session.call("list_forgeable_items", {"commander_id": commander["commander_id"]})
        if not listing["ok"] or not listing["result"]["laboratory_here"]:
            continue  # forging needs a lab in the mage's own province
        for item in listing["result"]["items"]:
            if len(item.get("gem_cost") or {}) == 1:
                return {
                    "commander_id": commander["commander_id"],
                    "item_id": item["id"],
                    "rationale": "smoke forge",
                }
    pytest.skip("nothing single-path is forgeable in the live save")


def _live_bid_args(session) -> dict:
    """A bid the live auction will actually accept, or skip if there is none.

    Mercenary offers turn over every few turns, so no fixed company name or
    price stays valid. The fixed-snapshot coverage for this tool lives in
    test_mercenary_bids.py; this only checks it still runs against whatever
    the game wrote most recently.
    """
    companies = ok(session.call("get_mercenaries", {}))["companies"]
    if not companies:
        pytest.skip("no mercenary companies on offer in the live save")
    first = companies[0]
    provinces = ok(session.call("list_provinces", {}))
    return {
        "company": first["company"],
        "amount": first["minimum_bid"],
        "province_id": provinces[0]["province_id"],
        "rationale": "smoke bid",
    }


# -- registry mechanics ----------------------------------------------------


def test_unknown_tool_lists_the_real_ones(session):
    err = refused(session.call("summon_dragon"), "bad_call")
    assert "get_turn_summary" in err


def test_unknown_argument_is_refused_not_ignored(session):
    """Silently dropping it would leave the model believing it had an effect."""
    err = refused(
        session.call("get_province", {"province_id": 93, "detail_level": "high"}), "bad_call"
    )
    assert "detail_level" in err


def test_integer_params_accept_digit_strings_but_not_names():
    p = Param("province_id", "integer", "id")
    assert p.validate("93") == 93
    assert p.validate(93) == 93
    with pytest.raises(ToolError):
        p.validate("Marignon")


def test_boolean_param_rejects_a_number():
    with pytest.raises(ToolError):
        Param("confirm", "boolean", "").validate(1)


def test_choices_are_enforced():
    p = Param("scope", "string", "", choices=("ours", "all"))
    assert p.validate("ours") == "ours"
    with pytest.raises(ToolError):
        p.validate("everything")


def test_a_crashing_tool_is_reported_not_raised():
    """A broken tool must not end the turn."""
    reg = ToolRegistry()

    def boom(ctx):
        raise RuntimeError("kaboom")

    reg.register(Tool(name="boom", description="", handler=boom))
    result = reg.call(None, "boom")
    assert result["ok"] is False and result["kind"] == "failed"
    assert "kaboom" in result["error"]


# -- text protocol, for models without native tool calling -----------------


def test_text_protocol_finds_a_call_in_prose():
    text = (
        "I should look at the turn first.\n"
        '{"tool": "get_turn_summary", "args": {}}\n'
        "Then I will decide."
    )
    assert parse_text_call(text) == ("get_turn_summary", {})


def test_text_protocol_handles_fenced_json():
    text = '```json\n{"tool": "get_province", "args": {"province_id": 93}}\n```'
    assert parse_text_call(text) == ("get_province", {"province_id": 93})


def test_text_protocol_accepts_arguments_as_an_alias():
    text = '{"tool": "find_province", "arguments": {"name": "Marignon"}}'
    assert parse_text_call(text) == ("find_province", {"name": "Marignon"})


def test_text_protocol_strips_leaked_control_tokens():
    """Asked to write calls as prose, a model still reaches for its own markers.

    Observed live: `<tool_call|>` wrapped around the JSON object. The call
    parses fine; the token was appearing in the narration shown to the user.
    """
    from dom6_assistant.agent.registry import split_text_call

    text = (
        'I will list the known provinces.\n<tool_call|>\n{"tool": "list_provinces", "args": {}}\n'
    )
    name, args, prose = split_text_call(text)
    assert (name, args) == ("list_provinces", {})
    assert prose == "I will list the known provinces."


def test_text_protocol_ignores_prose_with_no_call():
    assert parse_text_call("I think we should defend this turn.") is None


def test_text_protocol_does_not_repair_broken_json():
    """A malformed call must be reported, for the same reason args are not coerced."""
    assert parse_text_call('{"tool": "get_province", "args": {province_id: 93}}') is None


def test_text_protocol_skips_json_that_is_not_a_call():
    text = '{"note": "no tool here"} then {"tool": "get_orders", "args": {}}'
    assert parse_text_call(text) == ("get_orders", {})


# -- battle setup and equipment --------------------------------------------


def test_battle_order_round_trips_to_the_file(session):
    """Stance, target and formation, recorded then written then read back."""
    ok(
        session.call(
            "set_battle_order",
            {
                "commander_id": 308,
                "squad": 1,
                "stance": "hold_and_fire",
                "target": "archers",
                "rationale": "shoot their archers",
            },
        )
    )
    ok(
        session.call(
            "set_formation",
            {
                "commander_id": 308,
                "squad": 1,
                "formation": "double_line",
                "rationale": "spread against arrows",
            },
        )
    )
    ok(session.call("materialize_orders", {"confirm": True}))
    data = session.ctx.h2_path.read_bytes()
    end = O.find_order_blocks(data)[308].name_end
    squad = O.read_squad_orders(data, end)[0]
    assert squad["stance"] == O.STANCE_CODES["hold_and_fire"]
    assert squad["target"] == O.TARGET_CODES_V2["archers"]
    assert squad["formation"] == O.FORMATION_CODES["double_line"]


def test_none_explicitly_resets_stance_and_target(session):
    ok(session.call(
        "set_battle_order",
        {"commander_id": 308, "squad": 1, "stance": "hold_and_fire",
         "target": "archers", "rationale": "seed non-default bytes"},
    ))
    ok(session.call("materialize_orders", {"confirm": True}))
    ok(session.call(
        "set_battle_order",
        {"commander_id": 308, "squad": 1, "stance": "none",
         "target": "none", "rationale": "restore the client default"},
    ))
    ok(session.call("materialize_orders", {"confirm": True}))
    data = session.ctx.h2_path.read_bytes()
    end = O.find_order_blocks(data)[308].name_end
    squad = O.read_squad_orders(data, end)[0]
    assert squad["stance"] == 0
    assert squad["target"] == 0


def test_battle_stances_and_targets_enforce_subject_applicability(session):
    commander_error = refused(session.call(
        "set_battle_order",
        {"commander_id": 308, "stance": "hold_and_fire", "rationale": "invalid"},
    ), "bad_call")
    assert "commander stance" in commander_error
    squad_error = refused(session.call(
        "set_battle_order",
        {"commander_id": 308, "squad": 1, "stance": "cast_spells",
         "rationale": "invalid"},
    ), "bad_call")
    assert "squad stance" in squad_error
    target_error = refused(session.call(
        "set_battle_order",
        {"commander_id": 308, "squad": 1, "stance": "retreat",
         "target": "archers", "rationale": "invalid"},
    ), "bad_call")
    assert "does not accept a preferred target" in target_error


def test_setting_a_formation_does_not_clear_the_stance(session):
    """Latest-row-wins is right per field and wrong per row.

    Recording a formation for a squad blanked the stance recorded a moment
    earlier, because the newer row simply had no stance in it — sending a
    squad into battle on the default order after being told to hold and fire.
    """
    ok(
        session.call(
            "set_battle_order",
            {
                "commander_id": 308,
                "squad": 1,
                "stance": "hold_and_fire",
                "target": "archers",
                "rationale": "shoot",
            },
        )
    )
    ok(
        session.call(
            "set_formation",
            {"commander_id": 308, "squad": 1, "formation": "skirmish", "rationale": "spread"},
        )
    )
    written = ok(session.call("materialize_orders"))["written"]
    assert any("hold_and_fire" in w for w in written), written
    assert any("formation 3" in w for w in written), written


def test_position_gems_and_script_round_trip_together(session):
    from dom6_assistant.file_reader.formats import h2

    before_data = session.ctx.h2_path.read_bytes()
    before_fire = h2.gem_remaining(before_data)[0]
    before_end = O.find_order_blocks(before_data)[308].name_end
    old_carried = O.read_carried_gems(before_data, before_end).get("fire", 0)
    ok(
        session.call(
            "set_battle_position",
            {
                "commander_id": 308,
                "squad": 1,
                "x": -8,
                "y": 6,
                "rationale": "keep the mage behind the left wing",
            },
        )
    )
    ok(
        session.call(
            "set_carried_gems",
            {"commander_id": 308, "path": "fire", "amount": 1, "rationale": "fuel combat magic"},
        )
    )
    ok(
        session.call(
            "set_battle_script",
            {
                "commander_id": 308,
                "rounds": '["hold_one_turn", "Fire Flies", "attack_one_turn"]',
                "rationale": "wait, cast, then advance",
            },
        )
    )
    recorded = [row for row in ok(session.call("get_orders")) if row["commander_id"] == 308]
    assert {row["order"] for row in recorded} >= {
        "battle_position",
        "carried_gems",
        "battle_script",
    }
    result = ok(session.call("materialize_orders", {"confirm": True}))
    assert not result["skipped"]
    setup = ok(session.call("get_battle_setup", {"commander_id": 308}))
    assert setup["squads"][0]["position"] == {"x": -8, "y": 6}
    assert setup["carried_gems"]["fire"] == 1
    assert setup["single_round_orders"] == [
        "hold one turn",
        "cast Fire Flies",
        "attack one turn",
        "empty",
        "empty",
    ]
    assert h2.gem_remaining(session.ctx.h2_path.read_bytes())[0] == (
        before_fire - (1 - old_carried)
    )


def test_battle_script_refuses_rituals_and_unverified_fixed_codes(session):
    error = refused(
        session.call(
            "set_battle_script",
            {"commander_id": 308, "rounds": '["Distill Gold"]', "rationale": "invalid in battle"},
        ),
        "bad_call",
    )
    assert "ritual" in error
    refused(
        session.call(
            "set_battle_script",
            {"commander_id": 308, "rounds": '["dance_one_turn"]', "rationale": "invented"},
        ),
        "bad_call",
    )
    assert "restricted to nation" in refused(
        session.call(
            "set_battle_script",
            {
                "commander_id": 308,
                "rounds": '["Quick Roots"]',
                "rationale": "foreign national spell",
            },
        ),
        "bad_call",
    )
    assert "internal/event spell" in refused(
        session.call(
            "set_battle_script",
            {
                "commander_id": 308,
                "rounds": '["Cave Collapse"]',
                "rationale": "pathless internal record",
            },
        ),
        "bad_call",
    )


def test_battle_script_accepts_exact_eligible_divine_spells(session):
    recorded = ok(session.call(
        "set_battle_script",
        {"commander_id": 110, "rounds": '["Blessing"]',
         "rationale": "bless sacred troops"},
    ))
    assert recorded["rounds"] == [
        {"round": 1, "spell_id": 200, "spell": "Blessing"}
    ]
    error = refused(session.call(
        "set_battle_script",
        {"commander_id": 110, "rounds": '["Sacred Wind"]',
         "rationale": "wrong pretender path"},
    ), "bad_call")
    assert "not in our nation's Divine spell list" in error


def test_carried_gem_overspend_is_reported_by_materialization(session):
    ok(
        session.call(
            "set_carried_gems",
            {
                "commander_id": 308,
                "path": "air",
                "amount": 255,
                "rationale": "deliberate overspend test",
            },
        )
    )
    result = ok(session.call("materialize_orders"))
    assert any("carried gems" in message for message in result["skipped"])


def test_a_commanders_own_order_is_separate_from_their_squads(session):
    ok(
        session.call(
            "set_battle_order",
            {"commander_id": 308, "stance": "stay_behind_troops", "rationale": "stay out of melee"},
        )
    )
    ok(
        session.call(
            "set_battle_order",
            {"commander_id": 308, "squad": 1, "stance": "attack", "rationale": "squad advances"},
        )
    )
    ok(session.call("materialize_orders", {"confirm": True}))
    data = session.ctx.h2_path.read_bytes()
    end = O.find_order_blocks(data)[308].name_end
    assert O.read_own_battle_order(data, end)[0] == O.STANCE_CODES["stay_behind_troops"]
    assert O.read_squad_orders(data, end)[0]["stance"] == O.STANCE_CODES["attack"]


def test_an_unverified_stance_is_refused(session):
    err = refused(
        session.call(
            "set_battle_order",
            {"commander_id": 308, "stance": "do_a_barrel_roll", "rationale": "x"},
        ),
        "bad_call",
    )
    assert "not verified" in err


def test_a_squad_the_commander_does_not_lead_is_refused(session):
    """Writing to an empty slot would land in another field's space."""
    refused(
        session.call(
            "set_battle_order",
            {"commander_id": 308, "squad": 4, "stance": "attack", "rationale": "x"},
        ),
        "bad_call",
    )


def test_sparse_squad_slots_refuse_stale_slot_zero(session):
    """Bruise's removed first squad left battle bytes but no slot-id record."""
    setup = ok(session.call("get_battle_setup", {"commander_id": 308}))
    assert [squad["slot"] for squad in setup["squads"]] == [1]
    error = refused(
        session.call(
            "set_battle_position",
            {
                "commander_id": 308,
                "squad": 0,
                "x": -8,
                "y": 6,
                "rationale": "must reject the stale slot",
            },
        ),
        "bad_call",
    )
    assert "slot 0 is empty" in error and "[1]" in error


def test_equipping_an_item_that_does_not_exist_is_refused(session):
    refused(
        session.call(
            "equip_item", {"commander_id": 308, "slot": "helm", "item_id": 99999, "rationale": "x"}
        ),
        "bad_call",
    )


def test_an_item_is_refused_from_the_wrong_slot(session):
    """A mapped offset does not make an incompatible item safe there."""
    error = refused(
        session.call(
            "equip_item", {"commander_id": 308, "slot": "boots", "item_id": 186, "rationale": "x"}
        ),
        "bad_call",
    )
    assert "does not fit" in error and "boots" in error


def test_worn_item_transfer_materializes_both_halves_atomically(session):
    from dom6_assistant.file_reader.formats import h2

    ok(session.call(
        "equip_item",
        {"commander_id": 308, "slot": "helm", "item_id": 186,
         "rationale": "equip the transfer source"},
    ))
    ok(session.call("materialize_orders", {"confirm": True}))
    moved = ok(session.call(
        "transfer_item",
        {"source_commander_id": 308, "source_slot": "helm",
         "destination_commander_id": 110, "destination_slot": "helm",
         "rationale": "protect the priest"},
    ))
    assert moved["item_id"] == 186
    result = ok(session.call("materialize_orders", {"confirm": True}))
    assert not result["skipped"]

    data = session.ctx.h2_path.read_bytes()
    blocks = O.find_order_blocks(data)
    assert "helm" not in O.read_equipment(data, blocks[308].name_end)
    assert O.read_equipment(data, blocks[110].name_end)["helm"] == 186
    assert h2.read_item_stash(data).count(186) == 0


def test_two_pending_equips_cannot_spend_one_item_copy(session):
    ok(session.call(
        "equip_item",
        {"commander_id": 308, "slot": "helm", "item_id": 186,
         "rationale": "first claimant"},
    ))
    error = refused(session.call(
        "equip_item",
        {"commander_id": 110, "slot": "helm", "item_id": 186,
         "rationale": "second claimant"},
    ), "bad_call")
    assert "needs 2 copy/copies, owns 1" in error


@pytest.mark.parametrize(
    ("slot", "item_id", "target_name"),
    [("ranged", 143, "t44-auto-5"), ("boots", 288, "t44-auto-6")],
)
def test_new_equipment_slots_materialize_end_to_end(
        tmp_path, slot, item_id, target_name):
    source = Path("knowledge/snapshots/t44-auto-4")
    target = Path("knowledge/snapshots") / target_name / "mid_marignon.2h"
    if not (source / "mid_marignon.trn").exists() or not target.exists():
        pytest.skip("turn-44 equipment controls absent")
    s = _session_on(tmp_path, source)
    try:
        ok(s.call("equip_item", {
            "commander_id": 59, "slot": slot, "item_id": item_id,
            "rationale": "controlled equipment write",
        }))
        ok(s.call("materialize_orders", {"confirm": True}))
        written = s.ctx.h2_path.read_bytes()
        end = O.find_order_blocks(written)[59].name_end
        assert O.read_equipment(written, end)[slot] == item_id
        assert written[:-1] == target.read_bytes()[:-1]
        treasury = ok(s.call("get_item_treasury", {}))["items"]
        assert item_id not in {item["item_id"] for item in treasury}
    finally:
        s.close()


def test_equipment_round_trips(session):
    ok(
        session.call(
            "equip_item",
            {"commander_id": 308, "slot": "helm", "item_id": 186, "rationale": "better protection"},
        )
    )
    ok(session.call("materialize_orders", {"confirm": True}))
    data = session.ctx.h2_path.read_bytes()
    end = O.find_order_blocks(data)[308].name_end
    assert O.read_equipment(data, end)["helm"] == 186


def test_recruitment_queue_names_its_write_path(session):
    """The read tool must not retain its obsolete read-only warning."""
    queue = ok(session.call("get_recruitment_queue"))
    assert "queue_recruits" in queue["note"]
    assert isinstance(queue["gold_committed"], int)


def test_battle_options_match_what_is_enforced(session):
    """The advertised vocabulary and the enforced one must be the same."""
    opts = ok(session.call("list_battle_options"))
    assert set(opts["stances"]) == set(O.STANCE_CODES)
    assert set(opts["targets"]) == set(O.TARGET_CODES_V2)
    assert set(opts["equipment_slots"]) == set(O.EQUIPMENT_SLOTS)
    assert set(opts["single_round_choices"]) == set(O.SINGLE_ROUND_CODES)
    assert opts["carried_gem_paths"] == list(O.CARRIED_GEM_PATHS)
    assert opts["formations"] == O.FORMATION_CODES
    assert opts["formation_rule"] == {
        "basic_below_normal_leadership": 80,
        "basic": ["box", "skirmish"],
        "advanced_at_or_above_threshold": [
            "box", "double_line", "line", "skirmish", "sparse_line"],
        "uses_final_leadership": True,
    }
    assert "unidentified_formation_codes" not in opts


def test_advanced_formation_requires_final_leadership_80(session):
    # Dapamort has chassis Leadership 50 and one experience star (+25): final
    # Leadership 75, so only the two basic formations are legal.
    setup = ok(session.call("get_battle_setup", {"commander_id": 116}))
    assert setup["final_normal_leadership"] == 75
    assert setup["available_formations"] == ["box", "skirmish"]
    error = refused(session.call(
        "set_formation",
        {"commander_id": 116, "squad": 0, "formation": "line",
         "rationale": "should require stronger leadership"},
    ), "bad_call")
    assert "requires final normal Leadership 80" in error
    ok(session.call(
        "set_formation",
        {"commander_id": 116, "squad": 0, "formation": "box",
         "rationale": "use an available basic formation"},
    ))


def test_materialization_rechecks_formation_after_final_loadout(session):
    # Bypass the public writer to simulate a stale/legacy illegal intent. The
    # final materializer must still refuse it rather than trusting the row.
    M.record_battle_order(
        session.ctx.game_db, session.ctx.game_id, session.ctx.turn,
        116, squad=0, formation=O.FORMATION_CODES["line"],
        commander_name="Dapamort", rationale="legacy invalid formation",
    )
    result = ok(session.call("materialize_orders", {}))
    assert result["aborted"] is True
    assert any(
        "requires normal Leadership 80" in message
        for message in result["skipped"]
    )


def test_recruitable_includes_units_granted_by_sites(session):
    """The nation lists alone are wrong; sites unlock three more here.

    Grand Master and High Inquisitor come from The House of Fiery Justice, and
    the Architect from The Royal Academy — both in our capital. None of the
    three appears in any *_by_nation table, and all three have been recruited.
    """
    got = ok(session.call("list_recruitable", {"province_id": 93}))
    by_name = {e["name"]: e for e in got["recruitable"]}
    for name in ("Grand Master", "High Inquisitor", "Architect"):
        assert name in by_name, f"{name} missing"
        assert any("site:" in s for s in by_name[name]["sources"]), name


def test_recruitable_includes_troops_not_just_commanders(session):
    """The harvested recruitment screen only ever covered commanders."""
    got = ok(session.call("list_recruitable", {"province_id": 93}))
    troops = {e["name"] for e in got["recruitable"] if e["kind"] == "troop"}
    assert {"Knight of the Chalice", "Crossbowman", "Pikeneer"} <= troops


def test_recruitable_cost_is_null_rather_than_wrong(session):
    """basecost reads 10010 for both a Crossbowman (10) and a High Inquisitor
    (285), so an unobserved price is reported as unknown, never guessed."""
    got = ok(session.call("list_recruitable", {"province_id": 93}))
    by_name = {e["name"]: e for e in got["recruitable"]}
    assert by_name["Crossbowman"]["gold"] == 10
    assert by_name["High Inquisitor"]["gold"] == 285
    unpriced = [e for e in got["recruitable"] if e["gold"] is None]
    if unpriced:
        assert "costs_unknown" in got
        assert all(isinstance(e["gold"], type(None)) for e in unpriced)


# -- province defence ------------------------------------------------------


def test_province_defence_reads_turn_start_saved_and_recorded_targets(pd_state):
    before = ok(pd_state.call("get_province_defence"))
    copper = next(p for p in before["provinces"] if p["province_id"] == 86)
    assert copper == {
        "province_id": 86,
        "province_name": "Copper Canyons",
        "turn_start": 1,
        "orders_block_available": True,
        "target_in_orders_file": 1,
        "points_bought": 0,
        "gold_committed": 0,
        "recorded_target": None,
        "recorded_rationale": None,
    }
    assert before["maximum_purchasable"] == 100
    assert before["gold_uncommitted_in_orders_file"] == 7168

    written = ok(
        pd_state.call(
            "set_province_defence",
            {
                "province_id": 86,
                "target": 2,
                "rationale": "Secure the cap circle.",
            },
        )
    )
    assert written["points_bought"] == 1
    assert written["gold_committed_here"] == 2
    assert written["projected_gold_remaining"] == 7166

    pending = ok(pd_state.call("get_province_defence"))
    copper = next(p for p in pending["provinces"] if p["province_id"] == 86)
    assert copper["target_in_orders_file"] == 1
    assert copper["recorded_target"] == 2
    assert copper["recorded_rationale"] == "Secure the cap circle."

    orders = ok(pd_state.call("get_orders"))
    assert {
        "province_id": 86,
        "province_name": "Copper Canyons",
        "order": "province_defence",
        "target": 2,
        "rationale": "Secure the cap circle.",
    } in orders


def test_sparse_province_defence_blocks_use_adjacent_province_records(tmp_path):
    """Turn 38 has blocks for Kratas, Marignon, and the Obsidian Waste.

    The old count-based join assigned the first block to Copper Canyons by
    assuming a new conquest was necessarily absent. Its adjacent exact
    province record says Kratas; the identical PD=1 values had hidden the bad
    attribution.
    """
    s = _session_on(tmp_path, Path("knowledge/snapshots/t38-kratas-resolved"))
    try:
        result = ok(s.call("get_province_defence", {}))
        by_id = {row["province_id"]: row for row in result["provinces"]}
        assert set(by_id) == {86, 91, 93, 98}
        assert by_id[86]["target_in_orders_file"] is None
        assert by_id[91]["target_in_orders_file"] == 1
        assert by_id[93]["target_in_orders_file"] == 25
        assert by_id[98]["target_in_orders_file"] == 1
        assert by_id[86] == {
            "province_id": 86,
            "province_name": "Copper Canyons",
            "turn_start": 1,
            "orders_block_available": False,
            "target_in_orders_file": None,
            "points_bought": None,
            "gold_committed": None,
            "recorded_target": None,
            "recorded_rationale": None,
        }

        refused_result = s.call(
            "set_province_defence",
            {
                "province_id": 86,
                "target": 2,
                "rationale": "Protect Copper Canyons.",
            },
        )
        assert "no province-local block" in refused(refused_result)

        writable = ok(
            s.call(
                "set_province_defence",
                {
                    "province_id": 91,
                    "target": 2,
                    "rationale": "Protect Kratas.",
                },
            )
        )
        assert writable["projected_gold_delta_from_orders_file"] == 2
    finally:
        s.close()


def test_province_defence_materializes_and_matches_client_save(pd_state):
    baseline = (PD_BASELINE / "mid_marignon.2h").read_bytes()
    authored = (Path("knowledge/snapshots/t30-pd-copper2") / "mid_marignon.2h").read_bytes()

    ok(
        pd_state.call(
            "set_province_defence",
            {
                "province_id": 86,
                "target": 2,
                "rationale": "Secure the cap circle.",
            },
        )
    )
    result = ok(pd_state.call("materialize_orders", {"confirm": True}))
    assert result["skipped"] == []

    generated = pd_state.ctx.h2_path.read_bytes()
    assert generated[:-2] == authored[:-2]
    assert generated[-2:] == baseline[-2:]

    after = ok(pd_state.call("get_province_defence"))
    copper = next(p for p in after["provinces"] if p["province_id"] == 86)
    assert copper["target_in_orders_file"] == 2
    assert copper["points_bought"] == 1
    assert copper["gold_committed"] == 2
    assert after["gold_uncommitted_in_orders_file"] == 7166


def test_province_defence_replacement_can_refund_to_turn_start(pd_state):
    ok(
        pd_state.call(
            "set_province_defence",
            {
                "province_id": 86,
                "target": 2,
                "rationale": "Initial defensive allocation.",
            },
        )
    )
    replacement = ok(
        pd_state.call(
            "set_province_defence",
            {
                "province_id": 86,
                "target": 1,
                "rationale": "Keep the gold liquid instead.",
            },
        )
    )
    assert replacement["projected_gold_delta_from_orders_file"] == 0
    assert replacement["projected_gold_remaining"] == 7168

    orders = ok(pd_state.call("get_orders"))
    defence = [o for o in orders if o["order"] == "province_defence"]
    assert len(defence) == 1
    assert defence[0]["target"] == 1


def test_province_defence_refuses_illegal_or_unaffordable_targets(pd_state):
    below_start = pd_state.call(
        "set_province_defence",
        {
            "province_id": 93,
            "target": 24,
            "rationale": "Illegal refund.",
        },
    )
    assert "turn-start level 25" in refused(below_start)

    not_owned = pd_state.call(
        "set_province_defence",
        {
            "province_id": 56,
            "target": 1,
            "rationale": "Not ours.",
        },
    )
    assert "not ours" in refused(not_owned)

    over_cap = pd_state.call(
        "set_province_defence",
        {
            "province_id": 86,
            "target": 101,
            "rationale": "Too much.",
        },
    )
    assert "0-100" in refused(over_cap)

    first = ok(
        pd_state.call(
            "set_province_defence",
            {
                "province_id": 86,
                "target": 100,
                "rationale": "Maximum border defence.",
            },
        )
    )
    assert first["gold_committed_here"] == 5049
    assert first["projected_gold_remaining"] == 2119

    unaffordable = pd_state.call(
        "set_province_defence",
        {
            "province_id": 93,
            "target": 100,
            "rationale": "Also maximize the capital.",
        },
    )
    assert "only 7168 uncommitted" in refused(unaffordable)


# -- recruitment -----------------------------------------------------------


def test_recruits_queue_and_materialise(session):
    """Intent to .2h and back, with the gold figure recomputed."""
    from dom6_assistant.file_reader.formats import h2, trn as T

    before_data = session.ctx.h2_path.read_bytes()
    before_remaining = h2.gold_remaining(before_data)
    before_spent = h2.parse_bytes(before_data).gold_spent
    ok(
        session.call(
            "queue_recruits",
            {
                "province_id": 93,
                "units": '[{"unit_type_id": 219, "count": 3}]',
                "rationale": "cheap infantry to thicken the line",
            },
        )
    )
    ok(session.call("materialize_orders", {"confirm": True}))
    parsed_trn = T.parse(session.ctx.h2_path.with_suffix(".trn"))
    owned = [p.province_id for p in parsed_trn.provinces if p.owner_nation_id == 61]
    orders = h2.parse(session.ctx.h2_path, owned_provinces=owned)
    marignon = next(q for q in orders.queues if q.province_id == 93)
    assert [r.unit_type_id for r in marignon.recruits] == [219, 219, 219]
    assert h2.gold_remaining(session.ctx.h2_path.read_bytes()) == (
        before_remaining - (orders.gold_spent - before_spent)
    )


def test_recruitment_writes_commanders_to_the_commander_queue(session):
    """The flat arrays still require separate commander and troop counts."""
    from dom6_assistant.file_reader.formats import h2, trn as T

    recorded = ok(session.call(
        "queue_recruits",
        {
            "province_id": 93,
            "units": (
                '[{"unit_type_id": 219, "count": 2}, '
                '{"unit_type_id": 428, "count": 1}]'
            ),
            "rationale": "an assassin plus line troops",
        },
    ))
    assert recorded["commanders"] == 1
    assert recorded["troops"] == 2
    ok(session.call("materialize_orders", {"confirm": True}))

    parsed_trn = T.parse(session.ctx.h2_path.with_suffix(".trn"))
    owned = [p.province_id for p in parsed_trn.provinces
             if p.owner_nation_id == 61]
    queue = next(
        row for row in h2.parse(session.ctx.h2_path, owned_provinces=owned).queues
        if row.province_id == 93
    )
    assert [(r.unit_type_id, r.is_commander) for r in queue.recruits] == [
        (428, True), (219, False), (219, False)]


def test_over_queueing_is_allowed_and_reported(session):
    """Committing to more than a province can build is a tactic, not an error.

    The surplus carries to the next turn, which is how you commit to an
    expensive unit early. So this reports the shortfall and records it anyway.
    """
    result = ok(
        session.call(
            "queue_recruits",
            {
                "province_id": 98,
                "units": '[{"unit_type_id": 134, "count": 8}]',
                "rationale": "commit to Royal Guards over several turns",
            },
        )
    )
    assert result["queued"] == 8
    assert result["resources_available"] == 113
    assert result["resources_exact"] is True
    assert "next turn" in result.get("note", "")


def test_a_unit_the_province_cannot_recruit_is_refused(session):
    err = refused(
        session.call(
            "queue_recruits",
            {"province_id": 93, "units": '[{"unit_type_id": 1, "count": 1}]', "rationale": "x"},
        ),
        "bad_call",
    )
    assert "not recruitable" in err


def test_recruiting_in_a_province_we_do_not_own_is_refused(session):
    refused(
        session.call(
            "queue_recruits",
            {"province_id": 56, "units": '[{"unit_type_id": 219, "count": 1}]', "rationale": "x"},
        ),
        "bad_call",
    )


def test_plan_recruitment_shows_the_running_resource_cost(session):
    ok(
        session.call(
            "queue_recruits",
            {
                "province_id": 98,
                "units": '[{"unit_type_id": 218, "count": 3}]',
                "rationale": "crossbows",
            },
        )
    )
    ok(session.call("materialize_orders", {"confirm": True}))
    plan = ok(session.call("plan_recruitment", {"province_id": 98}))
    assert [q["resources_cumulative"] for q in plan["queued"]] == [8, 16, 24]
    assert [q["recruit_points_cumulative"] for q in plan["queued"]] == [9, 18, 27]
    assert plan["resources_available"] == 113
    assert plan["resources_exact"] is True
    assert "decoded Dominions 6.35" in plan["resources_source"]


def test_current_economic_calculators_reach_the_tool_surface(tmp_path):
    """Turn-30 panel ground truth for income and resources.

    These figures must come from the current save, not province_economics: the
    test overwrites that table with deliberately wrong values first.
    """
    s = _session_on(tmp_path, STABLE_LIVE_BASE)
    try:
        s.ctx.game_db.execute(
            "UPDATE province_economics SET income=998, resources=999, supplies=997"
        )
        s.ctx.game_db.commit()
        economics = ok(s.call("get_province_economics"))["provinces"]
        by_id = {row["province_id"]: row for row in economics}
        assert {pid: by_id[pid]["resources"] for pid in (86, 93, 98)} == {86: 42, 93: 207, 98: 113}
        assert {pid: by_id[pid]["income"] for pid in (86, 93, 98)} == {86: 75, 93: 560, 98: 53}
        assert {pid: by_id[pid]["supplies"] for pid in (86, 93, 98)} == {
            86: 420,
            93: 1_280,
            98: 344,
        }
        assert all(by_id[pid]["resources_exact"] for pid in (86, 93, 98))
        assert all(by_id[pid]["income_exact"] for pid in (86, 93, 98))
        assert all(by_id[pid]["supplies_exact"] for pid in (86, 93, 98))

        options = ok(s.call("get_recruitment_options", {"province_id": 93}))
        assert options["budgets"]["resources"]["available"] == 207
        assert options["budgets"]["resources"]["exact"] is True
    finally:
        s.close()


def test_province_economics_are_flagged_when_stale(pinned):
    """Panel-only figures can be stale; computed resources cannot be."""
    conn = pinned.ctx.game_db
    conn.execute("DELETE FROM province_economics WHERE province_id=93")
    insert = (
        "INSERT INTO province_economics(province_id, turn, income, "
        "resources, recruit_points, commander_points, supplies, source)"
        " VALUES(93,?,?,?,?,?,?,'test')"
    )

    conn.execute(insert, (pinned.turn - 2, 500, 200, 400, 2, 1000))
    conn.commit()
    row = ok(pinned.call("get_province_economics", {"province_id": 93}))["provinces"][0]
    assert (row["turn"], row["resources"]) == (pinned.turn - 2, 204)
    assert row["resources_recorded"] == 200
    assert row["resources_exact"] is True
    assert row["stale"] is True

    conn.execute(insert, (pinned.turn, 560, 206, 477, 3, 1280))
    conn.commit()
    row = ok(pinned.call("get_province_economics", {"province_id": 93}))["provinces"][0]
    assert (row["turn"], row["resources"]) == (pinned.turn, 204)
    assert row["resources_recorded"] == 206
    assert row["stale"] is False
