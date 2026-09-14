"""Disenchantment and Arcane Analysis active-global selectors."""

import shutil
import sqlite3
import struct
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.agent.visibility import PlayerView
from dom6_assistant.file_reader.formats import trn as T
from dom6_assistant.orders.orders_2h import find_order_blocks


ROOT = Path("knowledge/snapshots/example_game_3")
GAME_DB = Path("knowledge/game.sqlite3")
STEM = "early_tienchi"
NATION_ID = 22
CASTER = 109  # Ren An
WELL_OF_MISERY_EFFECT = 33
DISENCHANTMENT = 1226
ARCANE_ANALYSIS = 1370


def _session(tmp_path: Path, snapshot: str, profile: str):
    source = ROOT / snapshot
    if not (source / f"{STEM}.2h").exists() or not GAME_DB.exists():
        pytest.skip("global-selector control snapshots are absent")
    save = tmp_path / profile
    save.mkdir()
    for name in (f"{STEM}.trn", f"{STEM}.2h"):
        shutil.copy2(source / name, save / name)
    db = tmp_path / f"{profile}.sqlite3"
    shutil.copy2(GAME_DB, db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO games(name, save_name, nation_id, nation_slug, save_dir) "
            "VALUES(?,?,?,?,?)",
            (profile, "example_game_3", NATION_ID, STEM, str(save)),
        )
        conn.commit()
    finally:
        conn.close()
    return open_session(profile, game_db=db, save_dir=save)


def _ok(envelope):
    assert envelope["ok"], envelope.get("error")
    return envelope["result"]


@pytest.mark.parametrize(
    ("snapshot", "spell_id", "gem_cost"),
    [
        ("t70-auto-2", DISENCHANTMENT, 50),
        ("t71-auto-2", ARCANE_ANALYSIS, 25),
    ],
)
def test_client_orders_store_well_of_misery_slot_zero(snapshot, spell_id, gem_cost):
    data = (ROOT / snapshot / f"{STEM}.2h").read_bytes()
    block = find_order_blocks(data)[CASTER]
    assert block.order_code == 9
    assert struct.unpack_from("<HxxIIii", data, block.name_end + 116) == (
        spell_id,
        gem_cost,
        0,   # Well of Misery's stable global-chain slot
        -1,
        -1,
    )
    active = T.read_global_effects(
        (ROOT / snapshot / f"{STEM}.trn").read_bytes())
    assert [(row.effect_id, row.slot) for row in active] == [
        (WELL_OF_MISERY_EFFECT, 0)
    ]


def test_arcane_analysis_is_an_individually_learned_level_nine_spell():
    parsed = T.parse(ROOT / "t71-auto" / f"{STEM}.trn")
    assert parsed.research_levels[5] == 8  # Thaumaturgy's ordinary school cap
    assert ARCANE_ANALYSIS in parsed.learned_spell_ids
    assert T.spell_is_researched(
        parsed.research_levels,
        parsed.learned_spell_ids,
        ARCANE_ANALYSIS,
        5,
        9,
    )


@pytest.mark.parametrize(
    ("baseline", "ordered", "spell_id", "supports_extra"),
    [
        ("t70-auto", "t70-auto-2", DISENCHANTMENT, True),
        ("t71-auto", "t71-auto-2", ARCANE_ANALYSIS, False),
    ],
)
def test_tool_reproduces_each_client_order(
    tmp_path, baseline, ordered, spell_id, supports_extra,
):
    session = _session(tmp_path, baseline, f"selector_{spell_id}")
    try:
        spells = _ok(session.call("list_castable_spells", {
            "commander_id": CASTER,
            "kind": "ritual",
        }))["spells"]
        brief = next(row for row in spells if row["id"] == spell_id)
        assert brief["order_supported"] is True
        assert brief["target"] == "active_global_enchantment"
        assert brief["target_parameter"] == "target_global"
        assert brief["extra_gems_supported"] is supports_extra
        assert brief["monthly_supported"] is False

        recorded = _ok(session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": spell_id,
            "target_global": WELL_OF_MISERY_EFFECT,
            "rationale": "reproduce the controlled global selector",
        }))
        assert recorded["target_global"] == WELL_OF_MISERY_EFFECT
        assert recorded["target_global_slot"] == 0
        materialized = _ok(session.call("materialize_orders", {"confirm": True}))
        assert not materialized["skipped"]
        actual = session.ctx.h2_path.read_bytes()
    finally:
        session.close()

    expected = (ROOT / ordered / f"{STEM}.2h").read_bytes()
    assert actual[:-1] == expected[:-1]


def test_arcane_analysis_rejects_extra_gems_and_all_selectors_reject_monthly(tmp_path):
    session = _session(tmp_path, "t71-auto", "selector_refusals")
    try:
        boosted_analysis = session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": ARCANE_ANALYSIS,
            "target_global": WELL_OF_MISERY_EFFECT,
            "extra_gems": 1,
            "rationale": "invalid boost",
        })
        monthly_analysis = session.call("cast_ritual", {
            "commander_id": CASTER,
            "spell_id": ARCANE_ANALYSIS,
            "target_global": WELL_OF_MISERY_EFFECT,
            "monthly": True,
            "rationale": "invalid monthly probe",
        })
    finally:
        session.close()
    assert not boosted_analysis["ok"]
    assert "Dispel/Disenchantment" in boosted_analysis["error"]
    assert not monthly_analysis["ok"]
    assert "active-global selector rituals" in monthly_analysis["error"]


def test_resolution_messages_and_arcane_estimate_are_structured(tmp_path):
    sauromatia = PlayerView(ROOT / "t71-auto", 9, "early_sauromatia.trn")
    attacked = next(
        row for row in sauromatia.turn_messages()
        if row.kind == "global_disenchantment_failed"
    )
    assert attacked.global_enchantment_name == "Well of Misery"
    assert attacked.global_strength_estimate is None

    tienchi = PlayerView(ROOT / "t72-auto", NATION_ID, f"{STEM}.trn")
    analysis = next(
        row for row in tienchi.turn_messages()
        if row.kind == "own_arcane_analysis"
    )
    assert analysis.global_enchantment_name == "Well of Misery"
    assert analysis.global_strength_estimate == 109

    session = _session(tmp_path, "t72-auto", "selector_resolution")
    try:
        research = _ok(session.call("get_research", {}))
        assert any(
            row["spell_id"] == ARCANE_ANALYSIS
            for row in research["learned_level_9_spells"]
        )
        messages = _ok(session.call("get_turn_messages", {}))["messages"]
        exposed = next(row for row in messages if row["kind"] == "own_arcane_analysis")
        assert exposed["global_enchantment"] == "Well of Misery"
        assert exposed["global_strength_estimate"] == 109

        active = _ok(session.call("get_global_enchantments", {}))[
            "global_enchantments"
        ][0]
        assert active["arcane_analysis"] == {
            "estimated_strength_astral_pearls": 109,
            "approximate": True,
            "source": "this turn's successful Arcane Analysis report",
        }
    finally:
        session.close()

    before = T.read_global_effects(
        (ROOT / "t70-auto" / "early_sauromatia.trn").read_bytes())[0]
    after = T.read_global_effects(
        (ROOT / "t71-auto" / "early_sauromatia.trn").read_bytes())[0]
    assert (before.value1, after.value1) == (121, 111)
