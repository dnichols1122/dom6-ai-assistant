"""Recipient-filtered turn message decoding."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.agent import province_wards as PW
from dom6_assistant.agent.session import open_session
from dom6_assistant.agent.visibility import PlayerView
from dom6_assistant.file_reader.formats import messages as M

SNAPSHOTS = Path("knowledge/snapshots")
GAME_DB = Path("knowledge/game.sqlite3")
NATION = 61

SEEKING_ARROW_INTERCEPTION = (
    "Sugaar has cast Seeking Arrow.\n\n"
    "Sugaar was hit by a powerful frost blast shortly after casting the spell  "
    "but Sugaar survived.\n\n"
    "The spell was destroyed by something that protects the province of Icden."
)


def _seeking_arrow_interception() -> M.MessageRecord:
    return M.MessageRecord(
        offset=100,
        end_offset=500,
        message_id=0,
        fields=(125, 14, 0, 0, 0, 0, 0, 34),
        recipient_nation_id=61,
        source_nation_id=-1,
        type_id=2,
        text=SEEKING_ARROW_INTERCEPTION,
    )


def test_province_protection_interception_preserves_fact_vs_inference():
    record = _seeking_arrow_interception()
    assert record.kind == "province_spell_intercepted"
    assert (record.commander_id, record.province_id) == (125, 14)

    observation = PW.interception_from_message(record, {"Icden": 34})
    assert observation is not None
    assert observation["attacking_spell"] == "Seeking Arrow"
    assert observation["caster_province_id"] == 14
    assert observation["province_id"] == 34
    assert observation["province_name_from_report"] == "Icden"
    assert observation["ward_identity_explicitly_reported"] is False
    assert observation["retaliation_reported"] == "powerful frost blast"
    assert observation["candidate_wards"] == [{
        "spell_id": 1196,
        "spell": "Frost Dome",
        "confidence": "inferred",
        "basis": (
            "the retaliation was a powerful frost blast; Frost Dome is the "
            "matching known province-protection ritual"
        ),
    }]


def test_province_protection_memory_is_idempotent_and_does_not_claim_duration():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE games (id INTEGER PRIMARY KEY);
        INSERT INTO games(id) VALUES(1);
        CREATE TABLE province_protection_observation (
            id INTEGER PRIMARY KEY,
            game_id INTEGER NOT NULL,
            observed_turn INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            province_id INTEGER NOT NULL,
            province_name TEXT,
            attacking_spell TEXT,
            confirmed_effect TEXT NOT NULL,
            retaliation_reported TEXT,
            candidate_wards_json TEXT NOT NULL DEFAULT '[]',
            evidence TEXT NOT NULL,
            UNIQUE(game_id, observed_turn, message_id)
        );
    """)
    record = _seeking_arrow_interception()
    PW.sync_observations(conn, 1, 69, [record], {"Icden": 34})
    PW.sync_observations(conn, 1, 69, [record], {"Icden": 34})

    remembered = PW.remembered_observations(conn, 1, 34)
    assert len(remembered) == 1
    assert remembered[0]["observed_turn"] == 69
    assert remembered[0]["active_now"] is None
    assert remembered[0]["remaining_duration"] is None
    assert remembered[0]["candidate_wards"][0]["confidence"] == "inferred"


def _view(label: str) -> PlayerView:
    path = SNAPSHOTS / label
    if not (path / "mid_marignon.trn").exists():
        pytest.skip(f"{label} snapshot absent")
    return PlayerView(path, NATION)


def test_event_and_hero_messages_match_turn_six_screen_ground_truth():
    records = M.find_message_records(_view("t6").data, NATION)
    messages = [record for record in records if record.is_turn_message]

    assert [record.type_id for record in messages] == [1, 2]
    event, hero = messages
    assert event.province_id == 93
    assert event.body_and_effects == (
        "An unexpected event has occured in Marignon.\n\nA vengeful crone has cursed the land",
        ("Misfortune +3", "1 units have been cursed"),
    )
    assert hero.commander_id == 309
    assert hero.province_id == 83
    assert "Stories of Clodius's brave deeds" in hero.text
    assert "heroic toughness" in hero.text


def test_proclamation_and_two_special_battles_are_independently_decoded():
    messages = _view("t4").turn_messages()

    assert [record.kind for record in messages] == ["proclamation", "battle", "battle"]
    assert "A proclamation from Oceania!" in messages[0].text
    assert "failed to seduce Hunerik" in messages[1].text
    assert "tried to assassinate Zerphiric" in messages[2].text
    assert messages[1].province_id == 91
    assert messages[2].province_id == 83


def test_scouting_records_are_not_misreported_as_turn_messages():
    records = M.find_message_records(_view("t38-kratas-resolved").data, NATION)

    assert len(records) == 8
    assert sum(record.type_id == M.SCOUT_REPORT_TYPE for record in records) == 7
    messages = _view("t38-kratas-resolved").turn_messages()
    assert len(messages) == 1
    assert messages[0].kind == "battle"
    assert messages[0].province_id == 91
    assert "There was a battle in Kratas." in messages[0].text

    # Turn five has only persistent scouting data and no Messages-screen rows.
    assert _view("t5").turn_messages() == []


def test_scouting_records_remain_available_as_exact_panel_prose():
    reports = _view("t23-quiet").scout_reports()

    assert "mainly Militias, Knights and Longbowmen" in reports[83].text
    assert "commanded by Hunerik the Priest" in reports[91].text


def test_controlled_incoming_nap_is_recipient_filtered_and_structured():
    source = SNAPSHOTS / "example_game" / "t15-auto"
    if not (source / "mid_marignon.trn").exists():
        pytest.skip("controlled incoming-NAP snapshot absent")
    marignon = PlayerView(source, 61, "mid_marignon.trn").turn_messages()
    proposal = next(row for row in marignon if row.type_id == M.NAP_PROPOSAL_TYPE)
    assert proposal.kind == "incoming_nap_proposal"
    assert proposal.diplomatic_action == "propose_nap"
    assert proposal.message_id == 0
    assert proposal.recipient_nation_id == 61
    assert proposal.source_nation_id == 54
    assert proposal.fields == (0,) * 8
    assert "proposes peace and a non-aggression pact" in proposal.text
    assert all(
        row.type_id != M.NAP_PROPOSAL_TYPE
        for row in PlayerView(source, 54, "mid_ermor.trn").turn_messages()
    )


def test_controlled_decline_delivery_and_relation_resolution_are_exact():
    source = SNAPSHOTS / "example_game" / "t16-auto"
    if not (source / "mid_ermor.trn").exists():
        pytest.skip("controlled declined-NAP delivery snapshot absent")
    ermor = PlayerView(source, 54, "mid_ermor.trn")
    decline = next(
        row for row in ermor.turn_messages()
        if row.diplomatic_action == "decline_nap"
    )
    assert decline.kind == "incoming_nap_declined"
    assert decline.message_id == 0
    assert decline.recipient_nation_id == 54
    assert decline.source_nation_id == 61
    assert decline.fields == (0,) * 8
    assert decline.body_and_effects[1] == ("Dominion -1",)
    assert "head of our herald has been returned" in decline.text

    relation = ermor.diplomatic_relations()[0]
    assert relation.nation_id == 61
    assert relation.status == "apprehensive"
    assert relation.waiting_for_response is False
    marignon = PlayerView(source, 61, "mid_marignon.trn")
    assert marignon.diplomatic_relations()[0].status == "apprehensive"

    # The report proves a one-point penalty, but not a net turn-over-turn
    # candle change: ordinary dominion spread can occur during the same host.
    before = PlayerView(
        SNAPSHOTS / "example_game" / "t15-auto-3",
        54,
        "mid_ermor.trn",
    )
    assert next(p for p in before.provinces() if p.province_id == 9).dominion_strength == 2
    assert next(p for p in ermor.provinces() if p.province_id == 9).dominion_strength == 2


def test_casting_a_global_is_announced_differently_to_caster_and_observer():
    """Neither announcement is a broadcast, and they carry different detail.

    Prediction C5 said a global cast would use the -2 broadcast sentinel, like
    throne claims and worldwide events.  It does not.  Both announcements are
    ordinary per-nation records, and the asymmetry is the finding: the caster
    is told *which* spell, and everyone else is told only that a global was
    cast at all.
    """
    source = SNAPSHOTS / "example_game" / "t28-auto"
    if not (source / "mid_ermor.trn").exists():
        pytest.skip("global-cast snapshot absent")

    caster = next(
        row
        for row in PlayerView(source, 54, "mid_ermor.trn").turn_messages()
        if row.type_id == 2
    )
    assert caster.kind == "global_enchantment_cast"
    assert caster.is_worldwide is False
    assert caster.recipient_nation_id == 54
    assert "Mambo has cast Foul Air." in caster.text
    # Same selectors as a heroic-ability record: the mage and their province.
    assert caster.commander_id == 124
    assert caster.province_id == 9

    observer = next(
        row
        for row in PlayerView(source, 61, "mid_marignon.trn").turn_messages()
        if "dire portent" in row.text
    )
    assert observer.type_id == 0
    assert observer.is_worldwide is False
    assert observer.recipient_nation_id == 61
    assert observer.source_nation_id == -1
    # The observer is not told the spell -- only that some global was cast.
    assert "has cast a global enchantment" in observer.text
    assert "Foul Air" not in observer.text


def test_our_own_overcast_survives_the_duplicate_list(tmp_path):
    """Withholding our own investment would lose information, not protect it.

    Turn 29 carries the enchantment twice in the caster's file, holding 17 and
    the masked 1.  For a *foreign* global the overcast stays hidden, because we
    genuinely cannot know it.  For our own there is nothing to protect -- we
    chose the investment and a human would simply remember it -- so the true
    figure is reported, with the disagreement disclosed rather than buried.
    """
    session = _session_at(tmp_path, "t29-auto", "mid_ermor", 54)
    try:
        ours = session.call("get_global_enchantments", {})
    finally:
        session.close()
    assert ours["ok"], ours.get("error")
    effect = ours["result"]["global_enchantments"][0]
    assert effect["spell"] == "Foul Air"
    assert effect["turns_remaining"] == 998
    assert effect["overcast"] == 17
    assert effect["overcast_variants"] == [17, 1]
    assert "differing overcast values" in effect["overcast_note"]

    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    foreign = _session_at(elsewhere, "t29-auto", "mid_marignon", 61)
    try:
        theirs = foreign.call("get_global_enchantments", {})
    finally:
        foreign.close()
    other = theirs["result"]["global_enchantments"][0]
    assert other["caster_nation"] == "Ermor"
    assert other["overcast"] is None
    assert "Arcane Analysis" in other["overcast_note"]


def test_dispel_is_writable_and_targets_the_enchantment_by_identity(tmp_path):
    """The writer stores what to remove, not where it currently sits.

    Slots are stable but a vacated one is reused by the next global cast, so
    recording a slot would let an unrelated cast silently retarget the order.
    The intent holds the effect id and materialisation resolves the slot
    against the chain as it stands then.
    """
    session = _session_at(tmp_path, "t46-auto-2", "mid_ermor", 54)
    try:
        active = session.call("get_global_enchantments", {})
        # Burden of Time sits at slot 2 but ordinal 1 in this save.
        recorded = session.call(
            "cast_ritual",
            {
                "commander_id": 124,
                "spell_id": 1180,
                "rationale": "remove our own Burden of Time",
                "target_global": 29,
            },
        )
        missing = session.call(
            "cast_ritual",
            {
                "commander_id": 124,
                "spell_id": 1180,
                "rationale": "target something that is not up",
                "target_global": 999,
            },
        )
        untargeted = session.call(
            "cast_ritual",
            {
                "commander_id": 124,
                "spell_id": 1180,
                "rationale": "no target at all",
            },
        )
    finally:
        session.close()

    assert active["ok"], active.get("error")
    assert {e["effect_id"] for e in active["result"]["global_enchantments"]} == {17, 29}

    assert recorded["ok"], recorded.get("error")
    assert recorded["result"]["target_global"] == 29
    assert recorded["result"]["target_global_slot"] == 2

    assert not missing["ok"]
    assert "no active global enchantment with effect_id 999" in missing["error"]
    assert not untargeted["ok"]
    assert "requires target_global" in untargeted["error"]


def test_casting_a_global_enchantment_is_no_longer_refused(tmp_path):
    """Three controlled casts wrote +124/+128/+132 as 0/-1/-1, so it is safe.

    The refusal existed because a missing province-range attribute could not
    distinguish a global from Dispel or Gift of Reason.  Globals are now
    verified targetless; the other families stay refused.
    """
    session = _session_at(tmp_path, "t46-auto-2", "mid_ermor", 54)
    try:
        # Burden of Time: a Death 7 global, and Mambo is the Death 7 pretender.
        global_cast = session.call(
            "cast_ritual",
            {
                "commander_id": 124,
                "spell_id": 1352,
                "rationale": "re-establish the enchantment we just removed",
            },
        )
        with_province = session.call(
            "cast_ritual",
            {
                "commander_id": 124,
                "spell_id": 1352,
                "rationale": "a global takes no province",
                "target_province": 9,
            },
        )
    finally:
        session.close()
    assert global_cast["ok"], global_cast.get("error")
    assert global_cast["result"]["spell"] == "Burden of Time"
    assert global_cast["result"]["target_province"] is None
    assert global_cast["result"]["target_global"] is None
    assert not with_province["ok"]
    assert "takes no target_province" in with_province["error"]


def test_global_extra_gems_materialize_exactly_like_the_client_control(tmp_path):
    """Intent acceptance is not enough: write the order and debit the pool.

    The controlled player-authored Foul Air invested seven gems above its
    75-gem base. The generated file must reproduce the complete client file
    apart from its ordinary unconstrained final trailer byte.
    """
    from dom6_assistant.file_reader.formats import h2

    session = _session_at(tmp_path, "t27-auto", "mid_ermor", 54)
    try:
        recorded = session.call(
            "cast_ritual",
            {
                "commander_id": 124,
                "spell_id": 1339,
                "extra_gems": 7,
                "rationale": "protect Foul Air with the controlled investment",
            },
        )
        queued = session.call("get_orders", {})
        materialized = session.call("materialize_orders", {"confirm": True})
        generated = session.ctx.h2_path.read_bytes()
    finally:
        session.close()

    assert recorded["ok"], recorded.get("error")
    assert recorded["result"]["base_gem_cost"] == 75
    assert recorded["result"]["extra_gems"] == 7
    assert recorded["result"]["gem_cost"] == 82
    assert recorded["result"]["overcast"] == 17  # 7 gems + 5 * (D7 - D5)
    ritual_order = queued["result"][-1]
    assert ritual_order["base_gem_cost"] == 75
    assert ritual_order["extra_gems"] == 7
    assert ritual_order["target_global"] is None
    assert materialized["ok"], materialized.get("error")
    assert not materialized["result"]["skipped"]
    assert materialized["result"]["aborted"] is False

    controlled = (
        SNAPSHOTS / "example_game" / "t27-auto-2" / "mid_ermor.2h"
    ).read_bytes()
    assert generated[:-1] == controlled[:-1]
    assert h2.gem_remaining(generated)[5] == 0


def test_dispel_extra_gems_materialize_exactly_like_the_client_control(tmp_path):
    """Dispel's extra pearls belong in +120 and the national reservation."""
    from dom6_assistant.file_reader.formats import h2

    session = _session_at(tmp_path, "t28-auto-2", "mid_marignon", 61)
    try:
        recorded = session.call(
            "cast_ritual",
            {
                "commander_id": 38,
                "spell_id": 1180,
                "target_global": 10,
                "extra_gems": 2,
                "rationale": "controlled attempt against Foul Air",
            },
        )
        materialized = session.call("materialize_orders", {"confirm": True})
        generated = session.ctx.h2_path.read_bytes()
    finally:
        session.close()

    assert recorded["ok"], recorded.get("error")
    assert recorded["result"]["base_gem_cost"] == 30
    assert recorded["result"]["extra_gems"] == 2
    assert recorded["result"]["gem_cost"] == 32
    assert recorded["result"]["overcast"] == 2
    assert recorded["result"]["target_global_slot"] == 0
    assert materialized["ok"], materialized.get("error")
    assert not materialized["result"]["skipped"]

    controlled = (
        SNAPSHOTS / "example_game" / "t28-auto-3" / "mid_marignon.2h"
    ).read_bytes()
    assert generated[:-1] == controlled[:-1]
    assert h2.gem_remaining(generated)[4] == 0


def test_globals_and_dispel_refuse_monthly(tmp_path):
    global_root = tmp_path / "global"
    global_root.mkdir()
    global_session = _session_at(global_root, "t27-auto", "mid_ermor", 54)
    try:
        global_monthly = global_session.call(
            "cast_ritual",
            {
                "commander_id": 124,
                "spell_id": 1339,
                "monthly": True,
                "rationale": "must be refused",
            },
        )
        global_with_selector = global_session.call(
            "cast_ritual",
            {
                "commander_id": 124,
                "spell_id": 1339,
                "target_global": 10,
                "rationale": "must be refused",
            },
        )
    finally:
        global_session.close()

    dispel_root = tmp_path / "dispel"
    dispel_root.mkdir()
    dispel_session = _session_at(
        dispel_root, "t28-auto-2", "mid_marignon", 61)
    try:
        dispel_monthly = dispel_session.call(
            "cast_ritual",
            {
                "commander_id": 38,
                "spell_id": 1180,
                "target_global": 10,
                "monthly": True,
                "rationale": "must be refused",
            },
        )
    finally:
        dispel_session.close()

    assert not global_monthly["ok"]
    assert "cannot be set to Monthly Ritual" in global_monthly["error"]
    assert not global_with_selector["ok"]
    assert "does not select an active global" in global_with_selector["error"]
    assert not dispel_monthly["ok"]
    assert "cannot be set to Monthly Ritual" in dispel_monthly["error"]


def test_the_removal_notice_goes_to_every_nation_except_the_dispeller():
    """It is a world event, not an ownership notice.

    Ermor dispelled its *own* Burden of Time.  Ermor received only the caster's
    report, while Marignon -- which owned neither the enchantment nor the
    dispel -- was told it had been removed.  So the type-0 notice reaches
    everyone but the dispeller, and naming it after ownership was wrong.
    """
    root = SNAPSHOTS / "example_game"
    if not (root / "t47-auto" / "mid_marignon.trn").exists():
        pytest.skip("self-dispel snapshot absent")

    uninvolved = next(
        row
        for row in PlayerView(root / "t47-auto", 61, "mid_marignon.trn").turn_messages()
        if row.kind == "global_was_dispelled"
    )
    assert uninvolved.text == "Burden of Time has been dispelled."
    assert uninvolved.recipient_nation_id == 61
    assert uninvolved.is_worldwide is False  # per-nation copy, not the -2 sentinel

    # The dispeller, who here also owned the enchantment, gets only its own
    # report -- no duplicate victim notice.
    ermor = PlayerView(root / "t47-auto", 54, "mid_ermor.trn").turn_messages()
    assert [row.kind for row in ermor if "dispel" in row.text.lower()] == [
        "own_ritual_cast"
    ]
    assert not any(row.kind == "global_was_dispelled" for row in ermor)


def test_a_successful_dispel_is_still_anonymous_to_the_victim():
    """Finishing the job is no less deniable than probing.

    The failed attempt told Ermor only that "someone" tried.  A success is just
    as anonymous -- "Foul Air has been dispelled." names the enchantment and
    nobody else.  The dispeller's own report does name the target this time,
    which the failure's did not.
    """
    source = SNAPSHOTS / "example_game" / "t46-auto-2"
    if not (source / "mid_ermor.trn").exists():
        pytest.skip("successful-dispel snapshot absent")

    victim = next(
        row
        for row in PlayerView(source, 54, "mid_ermor.trn").turn_messages()
        if row.kind == "global_was_dispelled"
    )
    assert victim.type_id == 0
    assert victim.text == "Foul Air has been dispelled."
    assert "Marignon" not in victim.text and "Francor" not in victim.text

    dispeller = next(
        row
        for row in PlayerView(source, 61, "mid_marignon.trn").turn_messages()
        if row.kind == "own_ritual_cast"
    )
    assert "The dispelling of Foul Air was successful." in dispeller.text


def test_a_failed_dispel_tells_each_side_something_different():
    """The target learns it was attacked but never by whom.

    Ermor's report names neither the nation nor the caster -- "Someone tried".
    Marignon's names its own mage and says the dispel was overpowered.  So a
    failed dispel is deniable: the attacker is identified to nobody.
    """
    source = SNAPSHOTS / "example_game" / "t29-auto"
    if not (source / "mid_ermor.trn").exists():
        pytest.skip("failed-dispel snapshot absent")

    attacked = next(
        row
        for row in PlayerView(source, 54, "mid_ermor.trn").turn_messages()
        if row.kind == "global_dispel_failed"
    )
    assert attacked.type_id == 0
    assert attacked.source_nation_id == -1
    assert attacked.is_worldwide is False
    assert "Someone tried to dispel" in attacked.text
    assert "Marignon" not in attacked.text
    assert "Francor" not in attacked.text

    dispeller = next(
        row
        for row in PlayerView(source, 61, "mid_marignon.trn").turn_messages()
        if row.type_id == 2
    )
    assert dispeller.kind == "own_ritual_cast"
    assert "Francor has cast Dispel." in dispeller.text
    assert "overpowered by the global enchantment" in dispeller.text


def test_broadcasts_are_recipient_minus_two_and_do_not_truncate_the_list():
    """A broadcast record used to be dropped, taking real messages with it.

    Worldwide events, throne claims and arena results are addressed to the
    sentinel -2 rather than to a nation, and the host replicates the identical
    record into every player's file.  Requiring `recipient == nation_id`
    discarded them -- and because the list walk stops at the first record it
    cannot parse, a broadcast sitting mid-list also silently truncated the
    player's own messages around it.

    Turn 20 is the exact case: Marignon's two province events precede the
    broadcast, and only the longer post-broadcast run survived.
    """
    source = SNAPSHOTS / "example_game" / "t20-auto"
    if not (source / "mid_marignon.trn").exists():
        pytest.skip("worldwide-event snapshot absent")
    marignon = PlayerView(source, 61, "mid_marignon.trn").turn_messages()
    assert [row.message_id for row in marignon] == [0, 1, 2]
    assert [row.is_worldwide for row in marignon] == [False, False, True]
    # The two that the truncation had been eating.
    assert "shards of a broken mirror" in marignon[0].text
    assert "festival was held in the honor of Sugaar" in marignon[1].text

    broadcast = marignon[2]
    assert broadcast.recipient_nation_id == M.BROADCAST_RECIPIENT
    assert broadcast.body_and_effects[1] == ("Worldwide: Units have been healed",)

    # The identical record reaches the other player: that is what makes reading
    # it a broadcast rather than a visibility leak.
    ermor = PlayerView(source, 54, "mid_ermor.trn").turn_messages()
    assert [row.is_worldwide for row in ermor] == [True]
    assert ermor[0].text == broadcast.text
    assert ermor[0].message_id == broadcast.message_id


def test_both_players_message_ids_reconstruct_a_gapless_host_sequence():
    """The completeness check that actually catches silent drops.

    The host numbers every report globally and writes each into the recipient's
    file only, so with both players' turns in hand the union of their ids
    should have no holes.  A hole means a record exists that neither decoder
    accepted -- which is exactly how the -2 broadcast and the 1000+id
    pretender-awakening recipients were found.

    Turn 20 has a genuine exception: index 3 is absent from *both* files, so
    the host allocated a number for something it never serialized to a player.
    That is a numbering gap, not a lost message; it is pinned here so it cannot
    be quietly used to excuse a future real one.
    """
    root = SNAPSHOTS / "example_game"
    if not (root / "t14-auto" / "mid_ermor.trn").exists():
        pytest.skip("two-player corpus absent")
    holes = {}
    for turn in range(1, 21):
        snapshot = root / f"t{turn}-auto"
        if not (snapshot / "mid_ermor.trn").exists():
            continue
        union: set[int] = set()
        for stem, nation in (("mid_ermor", 54), ("mid_marignon", 61)):
            union |= {
                row.message_id
                for row in M.find_message_records(
                    (snapshot / f"{stem}.trn").read_bytes(), nation
                )
            }
        if not union:
            continue
        missing = sorted(set(range(max(union) + 1)) - union)
        if missing:
            holes[turn] = missing
    assert holes == {20: [3]}, f"unexplained message-id holes: {holes}"


def test_pretender_awakening_addresses_its_nation_with_the_thousand_offset():
    """A second recipient encoding, found by the id-sequence check above."""
    source = SNAPSHOTS / "example_game" / "t14-auto"
    if not (source / "mid_marignon.trn").exists():
        pytest.skip("turn-14 snapshot absent")
    records = M.find_message_records((source / "mid_marignon.trn").read_bytes(), 61)
    awakening = next(row for row in records if row.message_id == 2)
    assert awakening.recipient_nation_id == 61 + M.RECIPIENT_FLAG_OFFSET
    assert awakening.type_id == 0
    assert not awakening.is_worldwide
    assert "has awakened" in awakening.text
    assert "Sugaar, God of Marignon" in awakening.text
    # It is ours, not a leak: Ermor's file has no such record.
    ermor = M.find_message_records((source / "mid_ermor.trn").read_bytes(), 54)
    assert all("has awakened" not in row.text for row in ermor)


def test_throne_claims_are_broadcasts_and_were_being_dropped_entirely():
    """The costliest instance of the bug: victory-condition announcements.

    Throne claims are how a game is won, and they arrive as broadcasts.  Every
    one of them was invisible to the assistant before recipient -2 was accepted.
    """
    claims = []
    for path in sorted(SNAPSHOTS.rglob("*.trn")):
        nation = {"mid_ermor": 54, "mid_marignon": 61}.get(path.stem)
        if nation is None:
            continue
        claims += [
            row
            for row in M.find_message_records(path.read_bytes(), nation)
            if row.is_worldwide and "has claimed" in row.text
        ]
    if not claims:
        pytest.skip("no throne claim in the corpus")
    assert all("Throne" in row.text for row in claims)
    assert all(row.recipient_nation_id == M.BROADCAST_RECIPIENT for row in claims)


def test_controlled_acceptance_delivery_and_active_nap_are_exact():
    source = SNAPSHOTS / "example_game" / "t16-auto-2"
    if not (source / "mid_ermor.trn").exists():
        pytest.skip("controlled accepted-NAP delivery snapshot absent")
    ermor = PlayerView(source, 54, "mid_ermor.trn")
    accept = next(
        row for row in ermor.turn_messages()
        if row.diplomatic_action == "accept_nap"
    )
    assert accept.kind == "incoming_nap_accepted"
    assert accept.type_id == 0
    assert accept.message_id == 0
    assert accept.recipient_nation_id == 54
    assert accept.source_nation_id == 61
    assert accept.fields == (0,) * 8
    assert accept.body_and_effects[1] == ("Non-aggression pact with Marignon",)
    assert "Our herald sent to Marignon has returned." in accept.text
    # Unlike the decline branch, acceptance carries no dominion penalty.
    assert "Dominion -1" not in accept.text

    # The acceptance is written symmetrically into both relation rows; the
    # standard pact is NAP-3, which the -13 code predicted before hosting.
    marignon = PlayerView(source, 61, "mid_marignon.trn")
    for view, other in ((ermor, 61), (marignon, 54)):
        relation = view.diplomatic_relations()[0]
        assert relation.nation_id == other
        assert relation.status == "NAP-3"
        assert relation.nap_phase == "active"
        assert relation.nap_notice_turns == 3
        assert relation.relation_code == -13
        assert relation.waiting_for_response is False

    # Only the proposer is told. Marignon chose the response, so the host
    # sends it no report at all.
    assert all(
        row.diplomatic_action is None for row in marignon.turn_messages()
    )


def test_diplomacy_tool_exposes_controlled_decline_delivery(tmp_path):
    source = SNAPSHOTS / "example_game" / "t16-auto"
    if not (source / "mid_ermor.2h").exists() or not GAME_DB.exists():
        pytest.skip("controlled declined-NAP snapshot or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_ermor.trn", "mid_ermor.2h"):
        shutil.copy2(source / name, save / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, game_db)
    session = open_session(
        "example_game::mid_ermor",
        game_db=game_db,
        save_dir=save,
        nation_id=54,
    )
    try:
        messages = session.call("get_turn_messages", {})
        relations = session.call("get_diplomatic_relations", {})
    finally:
        session.close()
    assert messages["ok"], messages.get("error")
    decline = next(
        row for row in messages["result"]["messages"]
        if row["diplomatic_action"] == "decline_nap"
    )
    assert decline["kind"] == "incoming_nap_declined"
    assert decline["effects"] == ["Dominion -1"]
    relation = relations["result"]["relations"][0]
    assert relation["status"] == "apprehensive"
    assert relation["waiting_for_response"] is False
    assert relation["incoming_action"] == "decline_nap"
    assert relation["incoming_message_id"] == 0


def test_diplomacy_tool_exposes_controlled_acceptance_delivery(tmp_path):
    source = SNAPSHOTS / "example_game" / "t16-auto-2"
    if not (source / "mid_ermor.2h").exists() or not GAME_DB.exists():
        pytest.skip("controlled accepted-NAP snapshot or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_ermor.trn", "mid_ermor.2h"):
        shutil.copy2(source / name, save / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, game_db)
    session = open_session(
        "example_game::mid_ermor",
        game_db=game_db,
        save_dir=save,
        nation_id=54,
    )
    try:
        messages = session.call("get_turn_messages", {})
        relations = session.call("get_diplomatic_relations", {})
        # A pact is in force and the proposal has been answered, so every
        # further action toward Marignon must be refused, each for its own
        # reason rather than a generic one.
        refusals = {
            action: session.call(
                "set_diplomatic_action",
                {
                    "nation_id": 61,
                    "action": action,
                    "rationale": "controlled probe of the post-acceptance state",
                },
            )
            for action in ("propose_nap", "accept_nap", "decline_nap", "declare_war")
        }
    finally:
        session.close()
    assert messages["ok"], messages.get("error")
    accept = next(
        row for row in messages["result"]["messages"]
        if row["diplomatic_action"] == "accept_nap"
    )
    assert accept["kind"] == "incoming_nap_accepted"
    assert accept["effects"] == ["Non-aggression pact with Marignon"]
    assert accept["source_nation_id"] == 61
    assert "##EFF" not in accept["text"]
    relation = relations["result"]["relations"][0]
    assert relation["status"] == "NAP-3"
    assert relation["nap_phase"] == "active"
    assert relation["nap_notice_turns"] == 3
    assert relation["waiting_for_response"] is False
    assert relation["incoming_action"] == "accept_nap"
    assert relation["incoming_message_id"] == 0
    assert all(not envelope["ok"] for envelope in refusals.values())
    assert "already has NAP state NAP-3" in refusals["propose_nap"]["error"]
    assert "not sent us a current NAP proposal" in refusals["accept_nap"]["error"]
    assert "not sent us a current NAP proposal" in refusals["decline_nap"]["error"]
    # Declaring war under an active pact is legal -- it starts the notice
    # counting down -- so this refusal is about the missing declaration text,
    # not about the relation state.
    assert "requires a missive" in refusals["declare_war"]["error"]


def _session_at(tmp_path, snapshot, stem, nation_id):
    source = SNAPSHOTS / "example_game" / snapshot
    if not (source / f"{stem}.2h").exists() or not GAME_DB.exists():
        pytest.skip(f"{snapshot} snapshot or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in (f"{stem}.trn", f"{stem}.2h"):
        shutil.copy2(source / name, save / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, game_db)
    return open_session(
        f"example_game::{stem}", game_db=game_db, save_dir=save, nation_id=nation_id
    )


def test_neither_war_nor_a_pact_can_be_declared_during_a_countdown(tmp_path):
    """Both moves are closed by rule, not merely unobserved.

    A countdown is already a declaration, so war cannot be declared twice; and
    a pact cannot be proposed while one is expiring.  Suing for peace only
    becomes available once the war has actually begun, which is a separate
    state and a separate experiment.
    """
    session = _session_at(tmp_path, "t18-auto", "mid_ermor", 54)
    try:
        relations = session.call("get_diplomatic_relations", {})
        refusals = {
            action: session.call(
                "set_diplomatic_action",
                {
                    "nation_id": 61,
                    "action": action,
                    "rationale": "probe of the countdown state",
                    **({"missive": "A message from Ermor\n\nAgain."}
                       if action == "declare_war" else {}),
                },
            )
            for action in ("declare_war", "propose_nap")
        }
    finally:
        session.close()
    assert relations["result"]["relations"][0]["status"] == "NAP-2*"
    assert all(not envelope["ok"] for envelope in refusals.values())
    assert "countdown to war" in refusals["declare_war"]["error"]
    assert "declared against them twice" in refusals["declare_war"]["error"]
    assert "countdown to war" in refusals["propose_nap"]["error"]
    assert "sued for once the war has actually begun" in refusals["propose_nap"]["error"]


def test_suing_for_peace_is_permitted_once_the_war_has_begun(tmp_path):
    """The inverse of the decoded path, and the next controlled experiment.

    At war the relation carries no NAP phase, so a proposal is offered.  It is
    recorded but deliberately not claimed to change anything: like every other
    outgoing action it is a Send Messages record until the turn is hosted.
    """
    session = _session_at(tmp_path, "t20-auto", "mid_ermor", 54)
    try:
        relations = session.call("get_diplomatic_relations", {})
        peace = session.call(
            "set_diplomatic_action",
            {
                "nation_id": 61,
                "action": "propose_nap",
                "rationale": "the war is going badly; seek terms",
            },
        )
    finally:
        session.close()
    assert relations["result"]["relations"][0]["status"] == "war"
    assert peace["ok"], peace.get("error")
    assert peace["result"]["action"] == "propose_nap"
    assert peace["result"]["missive"] == "We propose peace and a non-aggression treaty"


def test_diplomacy_tool_exposes_controlled_incoming_nap(tmp_path):
    source = SNAPSHOTS / "example_game" / "t15-auto"
    if not (source / "mid_marignon.2h").exists() or not GAME_DB.exists():
        pytest.skip("controlled incoming-NAP snapshot or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_marignon.trn", "mid_marignon.2h"):
        shutil.copy2(source / name, save / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, game_db)
    session = open_session(
        "example_game::mid_marignon", game_db=game_db, save_dir=save,
        nation_id=61,
    )
    try:
        messages = session.call("get_turn_messages", {})
        relations = session.call("get_diplomatic_relations", {})
    finally:
        session.close()
    assert messages["ok"], messages.get("error")
    proposal = next(
        row for row in messages["result"]["messages"]
        if row["type_id"] == M.NAP_PROPOSAL_TYPE
    )
    assert proposal["kind"] == "incoming_nap_proposal"
    assert proposal["source_nation_id"] == 54
    assert relations["ok"], relations.get("error")
    relation = relations["result"]["relations"][0]
    assert relation["nation_id"] == 54
    assert relation["incoming_action"] == "propose_nap"
    assert relation["incoming_message_id"] == 0


def test_public_message_tool_exposes_full_event_effects(tmp_path):
    source = SNAPSHOTS / "t6"
    if not (source / "mid_marignon.trn").exists() or not GAME_DB.exists():
        pytest.skip("turn-six snapshot or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_marignon.trn", "mid_marignon.2h"):
        shutil.copy2(source / name, save / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, game_db)
    session = open_session(game_db=game_db, save_dir=save)
    try:
        envelope = session.call("get_turn_messages", {})
    finally:
        session.close()

    assert envelope["ok"], envelope.get("error")
    result = envelope["result"]
    assert result["count"] == 2
    event, hero = result["messages"]
    assert event["kind"] == "event"
    assert event["province"] == "Marignon"
    assert event["effects"] == ["Misfortune +3", "1 units have been cursed"]
    assert "##EFF" not in event["text"]
    assert hero["kind"] == "heroic_ability"
    assert hero["commander"] == "Clodius"
