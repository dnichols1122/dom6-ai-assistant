"""Battle Summary decoding against player-supplied screen ground truth."""
from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.agent.visibility import PlayerView
from dom6_assistant.file_reader.formats import battles as B

SNAPSHOTS = Path("knowledge/snapshots")
GAME_DB = Path("knowledge/game.sqlite3")
NATION = 61


def _view(label: str) -> PlayerView:
    path = SNAPSHOTS / label
    if not (path / "mid_marignon.trn").exists():
        pytest.skip(f"{label} snapshot absent")
    return PlayerView(path, NATION)


def _force(report, nation_id):
    return next(force for force in report.forces
                if force.nation_id == nation_id)


@pytest.mark.parametrize(("label", "province", "record_counts"), [
    ("t3", 98, {61: 37, 0: 33}),
    ("t22-battle", 86, {61: 42, 0: 37}),
    ("t38-kratas-resolved", 91, {61: 39, 0: 53}),
])
def test_battle_roster_locator_finds_the_three_controlled_field_battles(
        label, province, record_counts):
    view = _view(label)
    live = {unit.offset for unit in view._current_unit_records()}

    reports = B.find_battle_rosters(
        view.data, NATION, live_offsets=live)

    selected = [report for report in reports
                if report.province_id == province]
    assert len(selected) == 1
    assert Counter(unit.nation_id for unit in selected[0].units) == record_counts


def test_kratas_summary_matches_the_player_screen_line_for_line():
    report = _view("t38-kratas-resolved").battle_reports()[0]
    ours = _force(report, NATION)
    enemy = _force(report, 0)

    assert report.province_id == 91
    assert ours.total_units == 22
    assert ours.mounts_not_counted == 17
    assert dict(ours.unit_types) == {
        133: 1, 134: 1, 135: 16, 220: 1, 221: 1,
        224: 1, 3894: 1,
    }
    assert enemy.total_units == 40
    assert enemy.mounts_not_counted == 13
    assert dict(enemy.unit_types) == {
        18: 2, 19: 11, 39: 24, 45: 2, 240: 1,
    }
    assert report.outcome_exact
    assert report.roles_decoded
    assert ours.role == "attacker"
    assert enemy.role == "defender"
    assert report.winner_role == "attacker"
    assert report.winner_nation_id == NATION
    assert report.our_losses_total == 0
    assert report.enemy_losses_total == 36
    assert ours.survivors_total == 22
    assert enemy.survivors_total == 4
    assert enemy.routed_survivors_total == 4
    # Fourteen Destrier kills are credited to their Knight riders.
    assert dict(report.kills_by_our_type) == {135: 33, 3894: 3}
    assert dict(report.enemy_losses_by_type) == {
        18: 2, 19: 8, 39: 24, 45: 1, 240: 1,
    }


def test_deaths_and_routed_survivors_are_separate():
    report = _view("t3").battle_reports()[0]
    ours = _force(report, NATION)
    enemy = _force(report, 0)

    assert report.province_id == 98
    assert ours.total_units == 36
    assert enemy.total_units == 33
    assert report.outcome_exact
    assert report.winner_role == "attacker"
    assert dict(ours.deaths_by_type) == {218: 2, 221: 11}
    assert ours.deaths_total == 13 and ours.routed_survivors_total == 0
    assert dict(enemy.deaths_by_type) == {18: 6, 29: 7, 33: 4}
    assert enemy.deaths_total == 17
    assert enemy.survivors_total == 16
    assert enemy.routed_survivors_total == 16
    assert dict(report.kills_by_our_type) == {218: 6, 221: 12}


def test_mixed_troop_and_commander_type_in_one_summary_row():
    """Jaguar Warrior is 36 troops plus one same-type PD commander."""
    path = SNAPSHOTS / "example_game_2" / "t3-auto"
    if not (path / "early_berytos.trn").exists():
        pytest.skip("controlled Berytos sailing battle absent")
    view = PlayerView(path, 29, trn_name="early_berytos.trn")
    report = view.battle_reports()[0]
    ours = _force(report, 29)
    enemy = _force(report, 0)

    assert report.province_id == 16
    assert ours.role == "attacker" and enemy.role == "defender"
    assert report.winner_role == "defender"
    assert ours.total_units == 46
    assert ours.deaths_total == 13
    assert ours.routed_survivors_total == 33
    assert dict(enemy.unit_types) == {1610: 33, 1611: 37, 1612: 1}
    assert dict(enemy.deaths_by_type) == {1611: 6}
    assert dict(report.kills_by_our_type) == {2255: 4, 2257: 1}


def test_two_special_battles_have_independent_outcomes_and_attribution():
    reports = _view("t4").battle_reports()

    assert len(reports) == 2
    failed_seduction, assassination = reports
    assert failed_seduction.source_offset < assassination.source_offset
    assert failed_seduction.winner_role == "defender"
    assert failed_seduction.our_losses_total == 1
    assert failed_seduction.enemy_losses_total == 0
    assert dict(_force(failed_seduction, 0).kills_by_type) == {18: 1}

    assert assassination.winner_role == "attacker"
    assert assassination.our_losses_total == 0
    assert assassination.enemy_losses_total == 6
    assert dict(assassination.enemy_losses_by_type) == {
        23: 1, 3822: 4, 3823: 1}
    assert dict(assassination.kills_by_our_type) == {428: 6}
    assert len(assassination.exceptional_groups) == 1
    group = assassination.exceptional_groups[0]
    assert group.group_id == 4
    assert dict(group.unit_types) == {3822: 4, 3823: 1}
    assert group.role == "defender"
    assert group.attribution_exact
    assert group.survivors_total == 0 and group.deaths_total == 5
    assert dict(group.deaths_by_type) == {3822: 4, 3823: 1}


def test_public_battle_tool_names_only_the_visible_aggregate(tmp_path):
    source = SNAPSHOTS / "t38-kratas-resolved"
    if not (source / "mid_marignon.trn").exists() or not GAME_DB.exists():
        pytest.skip("Kratas snapshot or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_marignon.trn", "mid_marignon.2h"):
        shutil.copy2(source / name, save / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, game_db)
    session = open_session(game_db=game_db, save_dir=save)
    try:
        envelope = session.call("get_battle_reports", {})
    finally:
        session.close()

    assert envelope["ok"], envelope.get("error")
    reports = envelope["result"]["reports"]
    assert len(reports) == 1
    report = reports[0]
    assert report["province"] == "Kratas"
    ours, enemy = report["forces"]
    assert ours["ours"] and ours["total"] == 22
    assert {row["name"]: row["count"] for row in ours["units"]} == {
        "Man at Arms": 1,
        "Royal Guard": 1,
        "Knight of the Chalice": 16,
        "Halberdier": 1,
        "Pikeneer": 1,
        "Witch Hunter": 1,
        "Serpent of Heavenly Fires": 1,
    }
    assert enemy["nation"] == "Independents" and enemy["total"] == 40
    assert ours["role"] == "attacker" and enemy["role"] == "defender"
    assert enemy["routed_survivors"] == 4
    assert report["outcome"]["enemy_losses_total"] == 36
    assert report["outcome"]["winner_role"] == "attacker"
    assert report["outcome"]["winner_is_ours"] is True
    assert report["outcome"]["winner_nation"] == "Marignon"
    assert {row["name"]: row["deaths"]
            for row in report["outcome"]["enemy_losses_by_type"]} == {
        "Militia": 2,
        "Heavy Cavalry": 8,
        "Heavy Infantry": 24,
        "Mounted Commander": 1,
        "Priest": 1,
    }


def test_quiet_turn_has_no_battle_report():
    reports = _view("t30-ritual-augury-copper-canyons").battle_reports()
    assert reports == []


def test_preorders_turn_without_a_live_roster_still_reports_no_battle():
    reports = _view("t1-preorders").battle_reports()
    assert reports == []
