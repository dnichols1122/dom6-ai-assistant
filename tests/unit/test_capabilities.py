"""The trait model must predict the order menus the game actually shows.

This is the claim worth testing. If traits determine orders, then knowing a
unit's traits should tell us what its menu contains without opening it — and
that is what makes the model worth having, since it generalises to nations we
have never harvested.

Every assertion here compares two independently sourced things: traits derived
from the reference data, and order labels read off the game's own menus.
"""
import sqlite3
from pathlib import Path

import pytest

DB = Path("knowledge/game.sqlite3")
CTX = "owned-capital-fully-built"


@pytest.fixture
def db():
    if not DB.exists():
        pytest.skip("game.sqlite3 absent")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def traits(conn, unit):
    return {r[0] for r in conn.execute(
        "SELECT trait FROM unit_traits WHERE unit_name=?", (unit,))}


def labels(conn, unit):
    return {r[0] for r in conn.execute(
        "SELECT order_label FROM order_capabilities "
        "WHERE unit_name=? AND province_context=?", (unit, CTX))}


def harvested(conn):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT unit_name FROM order_capabilities WHERE province_context=?",
        (CTX,))]


def test_priests_and_only_priests_get_preach(db):
    """Preach appears exactly for units with the priest trait.

    It appears greyed out where dominion is already maxed, which is why the test
    checks presence rather than availability: greyed means the unit HAS the
    capability and context blocks it, and conflating the two would make the
    registry refuse a legal order elsewhere.
    """
    for unit in harvested(db):
        has_preach = any(label.startswith("Preach") for label in labels(db, unit))
        is_priest = "priest" in traits(db, unit)
        if unit == "Serpent of Heavenly Fires":
            continue          # the pretender's own menu omits the Sacred block
        assert has_preach == is_priest, (
            f"{unit}: preach={has_preach} but priest={is_priest}")


def test_hide_requires_stealth_but_stealth_is_not_enough(db):
    """Stealth is necessary for Hide and not sufficient — squad composition matters.

    This started as "only scouts get Hide" and the test failed on the Friar, which
    is stealthy and has no Hide. That was the model being wrong rather than the
    data: `stealthy` is a value (Scout 50, Troubadour 70, Friar 40, Assassin 65),
    not a flag, and it does not by itself grant the order.

    The likely missing condition is that a commander cannot hide while leading
    non-stealthy troops — Dapamort the Friar leads Pikeneers, while the Scout and
    Troubadour were fresh recruits leading nobody. That is recorded as an
    unverified lesson, so this test asserts only the direction that is certain:
    Hide never appears without stealth.
    """
    for unit in harvested(db):
        lab = labels(db, unit)
        if "Hide" in lab:
            assert "stealthy" in traits(db, unit), (
                f"{unit} offers Hide without the stealthy trait")
            assert "Hide and Wait" in lab, f"{unit} has Hide without Hide and Wait"


def test_full_magic_block_requires_an_elemental_path(db):
    """Research, Cast Ritual, Forge and Alchemy track the caster trait, not priesthood.

    This is the distinction that took a correction to get right: H is holiness
    and makes a priest, not a caster. The Friar and the Paladin are priests with
    no elemental path, and the game gives them none of these four — which is the
    single clearest confirmation that separating H from the paths was correct.
    """
    CASTER_ONLY = {"Research", "Cast Ritual Spell", "Forge Magic Item", "Alchemy"}
    for unit in harvested(db):
        lab = labels(db, unit)
        overlap = lab & CASTER_ONLY
        is_caster = "caster" in traits(db, unit)
        if unit == "Serpent of Heavenly Fires":
            continue          # pretender traits are not derived from recruitable rows
        assert bool(overlap) == is_caster, (
            f"{unit}: has {sorted(overlap)} but caster={is_caster}")
        if is_caster:
            assert overlap == CASTER_ONLY, f"{unit} missing {sorted(CASTER_ONLY - overlap)}"


def test_priests_without_a_path_get_preach_but_not_research(db):
    """The Friar and Paladin case, stated directly rather than by implication."""
    for unit in ("Friar", "Paladin"):
        if unit not in harvested(db):
            pytest.skip(f"{unit} not harvested")
        t, lab = traits(db, unit), labels(db, unit)
        assert "priest" in t and "caster" not in t
        assert any(label.startswith("Preach") for label in lab)
        assert "Research" not in lab


def test_every_capability_row_cites_evidence(db):
    """A capability with no evidence is a guess wearing a fact's clothes."""
    bad = list(db.execute(
        "SELECT unit_name, order_label FROM order_capabilities "
        "WHERE evidence IS NULL OR evidence = ''"))
    assert not bad, f"rows without evidence: {bad[:5]}"


def test_context_is_recorded_for_every_row(db):
    """An order's absence means nothing without knowing where the unit stood."""
    bad = list(db.execute(
        "SELECT unit_name FROM order_capabilities "
        "WHERE province_context IS NULL OR province_context = ''"))
    assert not bad
