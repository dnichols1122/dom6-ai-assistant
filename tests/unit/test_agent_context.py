import sqlite3
from pathlib import Path
from types import SimpleNamespace

from dom6_assistant.agent.context import assemble_context, complete_context_run
from dom6_assistant.agent.decisions import decision_history


SCHEMA = Path("src/dom6_assistant/gamestate/schema.sql")


class FakeSession:
    def __init__(self, db):
        self.ctx = SimpleNamespace(
            game_db=db, game_id=1, nation_id=61)
        self.turn = 12

    def call(self, name, args=None):
        if name == "get_turn_summary":
            return {"ok": True, "result": {
                "turn": 12, "nation_id": 61, "gold": 500}}
        if name == "get_orders":
            return {"ok": True, "result": []}
        raise AssertionError(name)


def database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA.read_text())
    db.execute("INSERT INTO games(id,name,nation_id) VALUES(1,'test',61)")
    return db


def add_playbook(db, title, guidance, triggers, **values):
    import json
    db.execute(
        "INSERT INTO playbook_entry(title,guidance,triggers_json,priority,"
        "always_include,game_id,nation_id) VALUES(?,?,?,?,?,?,?)",
        (title, guidance, json.dumps(triggers), values.get("priority", 50),
         values.get("always_include", 0), values.get("game_id"),
         values.get("nation_id")),
    )


def test_explicit_triggers_are_whole_word_or_phrase_matches():
    db = database()
    add_playbook(db, "Mercenary bids", "Rival bids are hidden.", ["bid", "mercenary auction"])
    session = FakeSession(db)

    miss = assemble_context(session, "Our army is biding its time.", record=False)
    hit = assemble_context(session, "Should we bid in the mercenary auction?", record=False)

    assert miss.playbook == []
    assert [entry.title for entry in hit.playbook] == ["Mercenary bids"]
    assert hit.playbook[0].matched_triggers == ("bid", "mercenary auction")


def test_always_include_guidance_needs_no_trigger():
    db = database()
    add_playbook(
        db, "Scouting", "Sneak scouts into foreign provinces.", [],
        always_include=1,
    )

    bundle = assemble_context(FakeSession(db), "Unrelated question.", record=False)

    assert [entry.title for entry in bundle.playbook] == ["Scouting"]
    assert "always included" in bundle.rendered


def test_scope_and_open_scratchpad_are_respected():
    db = database()
    add_playbook(db, "Ours", "Use this.", ["research"], nation_id=61)
    add_playbook(db, "Other nation", "Do not use.", ["research"], nation_id=54)
    db.execute(
        "INSERT INTO scratchpad(game_id,turn,tag,note,pinned) "
        "VALUES(1,11,'plan','Keep researching Enchantment.',1)")
    db.execute(
        "INSERT INTO scratchpad(game_id,turn,tag,note,status) "
        "VALUES(1,10,'plan','Obsolete plan.','resolved')")

    bundle = assemble_context(FakeSession(db), "research priorities", record=False)

    assert [entry.title for entry in bundle.playbook] == ["Ours"]
    assert [note["note"] for note in bundle.scratchpad] == [
        "Keep researching Enchantment."]
    assert "Obsolete plan" not in bundle.rendered


def test_visible_previous_run_signals_trigger_the_next_invocation():
    db = database()
    add_playbook(db, "Dome warning", "Remote spells may be intercepted.", ["frost dome"])
    session = FakeSession(db)
    first = assemble_context(session, "Assess the turn.", record=True)
    complete_context_run(
        db, first.run_id, visible_response="We should investigate.",
        emitted_signals="I am considering a Frost Dome.",
        tool_names=["lookup_spell"],
    )

    second = assemble_context(session, "What next?", record=False)

    assert [entry.title for entry in second.playbook] == ["Dome warning"]


def test_each_injection_records_why_it_fired():
    db = database()
    add_playbook(db, "Auction", "Hidden rival bids.", ["mercenary"])
    bundle = assemble_context(FakeSession(db), "Hire a mercenary", record=True)

    row = db.execute(
        "SELECT * FROM context_injection_log WHERE context_run_id=?",
        (bundle.run_id,),
    ).fetchone()
    assert row["score"] == 150
    assert row["matched_triggers"] == '["mercenary"]'


def test_decision_history_links_revisions_and_prior_turns():
    db = database()
    db.executemany(
        "INSERT INTO order_intent(game_id,turn,commander_id,commander_name,"
        "order_name,destination,rationale) VALUES(1,?,?,?,?,?,?)",
        [
            (11, 7, "Aldric", "move", 20, "Secure the pass."),
            (11, 7, "Aldric", "defend", None, "Scouts changed the risk."),
            (12, 7, "Aldric", "research", None, "The pass is now secure."),
        ],
    )

    rows = decision_history(db, 1)
    newest, revised, oldest = rows

    assert newest["previous_decision"] == revised["decision_ref"]
    assert newest["supersedes"] is None
    assert revised["supersedes"] == oldest["decision_ref"]
    assert oldest["is_current_revision"] is False
    assert newest["rationale"] == "The pass is now secure."
