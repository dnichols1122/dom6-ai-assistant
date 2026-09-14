"""Parity tests for the ported Dominions 6.36 Wish parser/result builder."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.reference import wish as W


DB = Path("knowledge/reference/reference.sqlite3")


@pytest.fixture
def conn():
    if not DB.exists():
        pytest.skip("reference database is absent")
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


@pytest.mark.parametrize(
    ("text", "result", "payload"),
    [
        ("nothing at all", "nothing", -1),
        ("ultimate power", "magic_power", -1),
        ("power", "physical_power", -1),
        ("divine authority", "divine_power", -1),
        ("blood", "blood_slaves", -1),
        ("magic resources", "gems", -1),
        ("huge nation", "provinces", -1),
        ("stop starvation", "food", 1),
        ("combat skill", "experience", 500),
        ("money", "gold", 5000),
        ("copper", "lesser_gold", 3000),
        ("commanders", "commanders", 1),
        ("sword", "magic_item", 5),
        ("staff", "magic_item", 80),
        ("weapon", "magic_item", 20),
    ],
)
def test_parser_aliases_build_canonical_results(conn, text, result, payload):
    selected = W.parse_client_text(conn, text, randbelow=lambda _n: 0)
    assert selected is not None
    assert (selected.result, selected.payload) == (result, payload)


def test_parser_precedence_and_contextual_rejection(conn):
    # Exact unit/item names precede prose aliases.
    atlas = W.parse_client_text(conn, "Atlas of Creation")
    assert atlas is not None
    assert (atlas.result, atlas.payload) == ("magic_item", 441)
    assert W.parse_client_text(conn, "remove curse", caster_is_cursed=False) is None
    assert W.parse_client_text(
        conn, "remove curse", caster_is_cursed=True
    ).result == "remove_curse"


def test_named_unit_is_typed_and_no_wish_is_rejected(conn):
    horror = conn.execute(
        "SELECT id FROM units WHERE horror>2 AND coalesce(nowish,0)=0 LIMIT 1"
    ).fetchone()
    assert horror is not None
    selected = W.build_typed(conn, unit_id=int(horror["id"]))
    assert selected.result == "horror"
    assert selected.payload == horror["id"]

    forbidden = conn.execute(
        "SELECT id FROM units WHERE nowish>0 LIMIT 1"
    ).fetchone()
    assert forbidden is not None
    with pytest.raises(ValueError, match="No Wish"):
        W.build_typed(conn, unit_id=int(forbidden["id"]))


def test_client_random_item_distribution_is_tier_then_item(conn):
    rolls = iter((2, 0))  # Construction 5, then first eligible item in tier.
    selected = W.build_typed(
        conn, random_request="item", randbelow=lambda _n: next(rolls)
    )
    assert selected.result == "magic_item"
    constlevel = conn.execute(
        "SELECT constlevel FROM items WHERE id=?", (selected.payload,)
    ).fetchone()[0]
    assert constlevel == 5


def test_random_artifact_requires_and_honours_world_availability(conn):
    with pytest.raises(ValueError, match="artifact availability"):
        W.build_typed(conn, random_request="artifact", randbelow=lambda _n: 0)
    states = [-99] * 530
    candidates = conn.execute(
        "SELECT id FROM items WHERE constlevel=9 ORDER BY id"
    ).fetchall()
    assert len(candidates) > 1
    states[int(candidates[0]["id"])] = 1
    selected = W.build_typed(
        conn, random_request="artifact", item_states=states,
        randbelow=lambda _n: 0,
    )
    assert selected.payload == int(candidates[1]["id"])


@pytest.mark.parametrize(
    ("rolls", "result", "payload"),
    [
        ((33, 0), "abundant_gold", 7500),
        ((33, 50), "death", -1),
    ],
)
def test_something_independent_random_branches(conn, rolls, result, payload):
    values = iter(rolls)
    selected = W.build_typed(
        conn, random_request="something", randbelow=lambda _n: next(values)
    )
    assert (selected.result, selected.payload) == (result, payload)


def test_wire_interpretation_preserves_typed_payloads():
    assert W.selection_from_wire(10004, 7500).result == "abundant_gold"
    assert W.selection_from_wire(10014, 22).as_dict()["nation_id"] == 22
    assert W.selection_from_wire(10018, -1).result == "horror"
