from __future__ import annotations

import base64
import json
import sqlite3
import struct
import zlib

import pytest

from dom6_assistant.agent.character_cards import (
    CharacterCardError,
    active_context,
    bind_character,
    ensure_schema,
    import_card,
    list_cards,
    parse_character_card,
)


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    return db


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _png(chara: dict, *, ccv3: dict | None = None) -> bytes:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    chunks = [_chunk(b"IHDR", ihdr)]
    legacy = base64.b64encode(json.dumps(chara).encode())
    chunks.append(_chunk(b"tEXt", b"chara\0" + legacy))
    if ccv3 is not None:
        modern = base64.b64encode(json.dumps(ccv3).encode())
        chunks.append(_chunk(b"tEXt", b"ccv3\0" + modern))
    chunks.append(_chunk(b"IEND", b""))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def test_parses_v1_v2_and_prefers_ccv3_png() -> None:
    v1 = parse_character_card(json.dumps({
        "name": "V1", "description": "old",
    }).encode())
    assert (v1.spec, v1.spec_version, v1.name) == (
        "chara_card_v1", "1.0", "V1")

    v2_obj = {
        "spec": "chara_card_v2", "spec_version": "2.0",
        "data": {"name": "V2", "personality": "careful"},
    }
    v2 = parse_character_card(json.dumps(v2_obj).encode())
    assert v2.name == "V2"
    assert v2.data["personality"] == "careful"

    v3_obj = {
        "spec": "chara_card_v3", "spec_version": "3.0",
        "data": {"name": "Modern", "nickname": "M"},
    }
    png = parse_character_card(_png(v2_obj, ccv3=v3_obj), "modern.png")
    assert png.name == "Modern"
    assert png.spec == "chara_card_v3"
    assert png.avatar_bytes is not None


def test_rejects_bad_png_and_missing_name() -> None:
    with pytest.raises(CharacterCardError, match="name"):
        parse_character_card(b"{}")
    broken = bytearray(_png({"name": "Broken"}))
    broken[-1] ^= 1
    with pytest.raises(CharacterCardError, match="CRC"):
        parse_character_card(bytes(broken))


def test_import_deduplicates_and_keeps_creator_notes_out_of_prompt(tmp_path) -> None:
    db = _db()
    raw = {
        "spec": "chara_card_v2", "spec_version": "2.0",
        "data": {
            "name": "The Steward",
            "description": "A deliberate war planner.",
            "personality": "Patient, suspicious of unnecessary risk.",
            "scenario": "You advise {{user}} while ruling a kingdom.",
            "system_prompt": "Speak as {{char}}.",
            "post_history_instructions": "End with a clear decision.",
            "first_mes": "The realm awaits, {{user}}.",
            "alternate_greetings": ["Another path awaits, {{user}}."],
            "creator_notes": "SECRET DISPLAY-ONLY NOTES",
            "tags": ["strategist"],
        },
    }
    card = parse_character_card(json.dumps(raw).encode())
    first = import_card(db, card, filename="steward.json", asset_dir=tmp_path)
    second = import_card(db, card, filename="again.json", asset_dir=tmp_path)
    assert first["id"] == second["id"]
    assert second["deduplicated"] is True
    assert len(list_cards(db)) == 1

    bind_character(db, "pretender:17", first["id"], "strategy_and_voice")
    context = active_context(db, "pretender:17", "Design a god", [], user_name="Alex")
    assert "A deliberate war planner" in context.persona_prompt
    assert "Speak as The Steward" in context.persona_prompt
    assert "SECRET DISPLAY-ONLY NOTES" not in context.persona_prompt
    assert context.post_history_prompt == "End with a clear decision."
    assert context.first_message == "The realm awaits, Alex."
    assert context.alternate_greetings == ("Another path awaits, Alex.",)


def test_lore_retrieval_is_keyword_selective_constant_and_visible() -> None:
    db = _db()
    raw = {
        "spec": "chara_card_v2", "spec_version": "2.0",
        "data": {
            "name": "Archivist",
            "character_book": {
                "token_budget": 500,
                "entries": [
                    {"keys": ["bless"], "content": "Inspect exact bless effects.",
                     "comment": "bless rule", "priority": 5},
                    {"keys": ["war"], "secondary_keys": ["Ermor"],
                     "selective": True, "content": "Ermor war doctrine."},
                    {"constant": True, "content": "Stay in character.",
                     "position": "before_char"},
                    {"keys": ["secret"], "content": "Should not match."},
                ],
            },
        },
    }
    saved = import_card(db, parse_character_card(json.dumps(raw).encode()))
    bind_character(db, "turn:9", saved["id"], "voice_only")
    context = active_context(
        db, "turn:9", "Compare this bless before war with Ermor", [], record=True)
    assert "Stay in character" in context.lore_before
    assert "Inspect exact bless effects" in context.lore_after
    assert "Ermor war doctrine" in context.lore_after
    assert "Should not match" not in context.lore_after
    assert {item["name"] for item in context.matched_lore} == {
        "bless rule", "", ""}


def test_binding_is_revision_pinned() -> None:
    db = _db()
    saved = import_card(db, parse_character_card(b'{"name":"Pinned"}'))
    binding = bind_character(db, "turn:2", saved["id"], "full_character")
    assert binding["revision_id"] == saved["revision_id"]
    assert binding["influence_mode"] == "full_character"


def _fresh_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    from dom6_assistant.agent.character_cards import ensure_schema
    ensure_schema(db)
    return db


def test_a_written_character_is_an_ordinary_v3_card(tmp_path):
    """Authoring goes through the same parser as importing, so a written
    character cannot end up in a shape a downloaded one could never reach."""
    from dom6_assistant.agent.character_cards import create_card, get_card

    db = _fresh_db()
    made = create_card(db, {
        "name": "The Cautious Queen",
        "description": "Never risks a pretender.",
        "personality": "Terse.",
        "tags": ["strategy"],
        "lore": [{"keys": ["pretender"], "content": "Prefers resilient chassis."}],
    }, asset_dir=tmp_path)

    assert made["spec"] == "chara_card_v3" and made["spec_version"] == "3.0"
    assert made["authored"] is True
    assert made["lore_entries"] == 1
    # It is retrievable by the same accessor an imported card uses.
    assert get_card(db, made["id"])["name"] == "The Cautious Queen"
    # And the untouched source is retained, as import does.
    stored = json.loads(db.execute(
        "SELECT raw_json FROM character_card WHERE id=?", (made["id"],)).fetchone()[0])
    assert stored["data"]["personality"] == "Terse."


def test_a_character_needs_a_name(tmp_path):
    from dom6_assistant.agent.character_cards import create_card

    db = _fresh_db()
    with pytest.raises(CharacterCardError, match="needs a name"):
        create_card(db, {"name": "   ", "description": "x"}, asset_dir=tmp_path)


def test_editing_saves_a_revision_rather_than_a_second_card(tmp_path):
    """Bindings name a revision, so an edit must not change what an existing
    conversation is already running on."""
    from dom6_assistant.agent.character_cards import (
        card_fields, create_card, get_binding, list_cards, update_card)

    db = _fresh_db()
    made = create_card(db, {"name": "Iron Prophet",
                            "personality": "Patient."}, asset_dir=tmp_path)
    bind_character(db, "open:general", made["id"], "strategy_and_voice")
    bound_at = get_binding(db, "open:general")["revision_id"]

    fields = card_fields(db, made["id"])
    fields["personality"] = "Patient. Now grim."
    updated = update_card(db, made["id"], fields, asset_dir=tmp_path)

    assert len(list_cards(db)) == 1, "an edit must not clone the card"
    assert updated["revision_id"] != bound_at
    # The existing binding still points at the revision it was made against.
    assert get_binding(db, "open:general")["revision_id"] == bound_at
    assert card_fields(db, made["id"])["personality"] == "Patient. Now grim."


def test_authored_lore_is_keyed_and_only_fires_on_its_trigger(tmp_path):
    from dom6_assistant.agent.character_cards import create_card

    db = _fresh_db()
    made = create_card(db, {
        "name": "Iron Prophet",
        "description": "Believes fortifications win wars.",
        "lore": [{"keys": ["siege"], "content": "Always values castle defence."}],
    }, asset_dir=tmp_path)
    bind_character(db, "open:general", made["id"], "strategy_and_voice")

    hit = active_context(db, "open:general", "how do I survive a siege?")
    assert "castle defence" in (hit.lore_after or "")
    miss = active_context(db, "open:general", "which nation has good archers?")
    assert "castle defence" not in (miss.lore_after or "")


def test_blank_lore_rows_are_dropped_rather_than_stored(tmp_path):
    """The editor adds empty rows as you type; an unfilled one is not an entry."""
    from dom6_assistant.agent.character_cards import create_card

    db = _fresh_db()
    made = create_card(db, {
        "name": "Sparse",
        "lore": [{"keys": ["a"], "content": "kept"},
                 {"keys": [], "content": "   "},
                 {"keys": ["b"], "content": ""}],
    }, asset_dir=tmp_path)
    assert made["lore_entries"] == 1


def test_the_player_persona_names_who_the_character_is_talking_to(tmp_path):
    """`{{user}}` used to expand to the placeholder "Player" because nothing
    ever supplied a name. A persona gives it someone in particular."""
    from dom6_assistant.agent.character_cards import (
        active_context, create_card, get_persona, save_persona)

    db = _fresh_db()
    card = create_card(db, {
        "name": "Iron Prophet",
        "personality": "Addresses {{user}} bluntly.",
        "first_mes": "Where are your walls, {{user}}?",
    }, asset_dir=tmp_path)
    bind_character(db, "open:general", card["id"], "strategy_and_voice")

    assert get_persona(db)["name"] == "Player"      # the default before setting one
    save_persona(db, "Tester", "A cautious veteran who favours Early Age nations.")

    context = active_context(db, "open:general", "how do I hold a siege?")
    assert "Tester" in context.persona_prompt
    assert context.first_message == "Where are your walls, Tester?"
    assert "cautious veteran" in context.player_persona
    # The description is offered as information, never as an instruction.
    assert "not an instruction" in context.player_persona
    assert "never overrides the harness rules" in context.player_persona


def test_a_persona_without_a_description_adds_nothing_to_the_prompt(tmp_path):
    from dom6_assistant.agent.character_cards import (
        active_context, create_card, save_persona)

    db = _fresh_db()
    card = create_card(db, {"name": "Terse", "personality": "Hi {{user}}."},
                       asset_dir=tmp_path)
    bind_character(db, "open:general", card["id"], "voice_only")
    save_persona(db, "Tester")           # a name, no description

    context = active_context(db, "open:general", "anything")
    assert context.player_persona == ""
    assert "Tester" in context.persona_prompt


def test_an_empty_persona_name_falls_back_rather_than_blanking_user(tmp_path):
    from dom6_assistant.agent.character_cards import get_persona, save_persona

    db = _fresh_db()
    assert save_persona(db, "   ")["name"] == "Player"
    assert get_persona(db)["name"] == "Player"
