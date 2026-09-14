"""Mercenary bidding: the two ten-slot arrays in the `.2h`.

Three controlled turn-30 saves established this, each changing one thing.
The player bid 747 on Nergash's Damned Legion — the only company on offer —
raised the same bid to 900, then held 900 and moved the arrival province from
Marignon to Copper Canyons. That last save changed two bytes in the whole
file, one of them the trailer.
"""
import shutil
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.file_reader.formats import h2, trn
from dom6_assistant.orders import materialize as M
from dom6_assistant.reference import mercenary_cost as MC
from ..conftest import require_corpus


SNAPSHOTS = Path("knowledge/snapshots")
BID_747 = SNAPSHOTS / "t30-merc-bid-747" / "mid_marignon.2h"
BID_900 = SNAPSHOTS / "t30-merc-bid-900" / "mid_marignon.2h"
BID_900_COPPER = SNAPSHOTS / "t30-merc-bid-900-copper" / "mid_marignon.2h"
NO_BID = SNAPSHOTS / "t30-pd-baseline" / "mid_marignon.2h"

#: Marignon (Citadel) and Copper Canyons (no fort) — both owned.
MARIGNON, COPPER_CANYONS = 93, 86

#: Remaining gold at turn 30 before any bid, from the province-defence work.
TURN_30_GOLD = 7168


def _read(path: Path) -> bytes:
    if not path.exists():
        pytest.skip(f"{path} absent")
    return path.read_bytes()


@pytest.mark.parametrize(
    ("path", "amount", "province"),
    [(BID_747, 747, MARIGNON),
     (BID_900, 900, MARIGNON),
     (BID_900_COPPER, 900, COPPER_CANYONS)],
)
def test_reads_the_controlled_bids(path, amount, province):
    """Slot 0, the observed amount, and the arrival province with it."""
    bids = h2.read_mercenary_bids(_read(path))
    assert len(bids) == 1
    assert (bids[0].slot, bids[0].amount, bids[0].province) == (
        0, amount, province)


def test_a_file_with_no_bid_reads_empty():
    """Empty must be empty, not a zero-amount bid on company 0xFFFF."""
    assert h2.read_mercenary_bids(_read(NO_BID)) == []


def test_the_bid_reserves_its_full_amount_in_gold():
    """The saved figure is the treasury minus the whole bid, not a deposit.

    This is the same displayed-not-derived remaining-gold field recruitment
    and construction already share, so a bid has to be reconciled against it
    like any other commitment.
    """
    assert h2.gold_remaining(_read(NO_BID)) == TURN_30_GOLD
    assert h2.gold_remaining(_read(BID_747)) == TURN_30_GOLD - 747
    assert h2.gold_remaining(_read(BID_900)) == TURN_30_GOLD - 900


def test_raising_a_bid_moves_only_the_amount_gold_and_trailer():
    """747 -> 900 changed five bytes. The province is not tied to the amount.

    This is what separates the two arrays: had the province been a function of
    the bid, or had the game rewritten the pairing on every edit, it would
    have moved here. It did not.
    """
    a, b = _read(BID_747), _read(BID_900)
    changed = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    start = h2.mercenary_bid_offset(a)
    provinces_at = (start - h2.OFF_MERCENARY_BID_AMOUNTS
                    + h2.OFF_MERCENARY_BID_PROVINCES)

    assert len(a) == len(b)
    # amount slot 0, the two gold bytes, and the final trailer byte
    assert len(changed) == 5
    assert start in changed and start + 1 in changed
    assert not any(provinces_at <= i < provinces_at + 20 for i in changed)


def test_moving_the_arrival_province_moves_one_semantic_byte():
    """Holding the bid at 900 and changing the land moved 93 -> 86, and the
    trailer. Nothing else — not the amount, not the reserved gold.

    This is what names the field. The value 93 alone could not: it is this
    nation's capital and a plausible identifier for several other things at
    once, which is exactly the coincidence that produced five retractions
    elsewhere in this format.
    """
    a, b = _read(BID_900), _read(BID_900_COPPER)
    changed = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    provinces_at = (h2.mercenary_bid_offset(a)
                    - h2.OFF_MERCENARY_BID_AMOUNTS
                    + h2.OFF_MERCENARY_BID_PROVINCES)

    assert changed == [provinces_at, len(a) - 1]
    assert h2.gold_remaining(a) == h2.gold_remaining(b)


@pytest.mark.parametrize(
    ("province", "path"),
    [(MARIGNON, BID_900), (COPPER_CANYONS, BID_900_COPPER)],
)
def test_the_writer_reproduces_the_game_authored_saves(province, path):
    """Generated from a file with no bid, only the trailer may differ.

    The trailer is deliberately never reproduced: the client ignores it on
    load, and inventing a value would be worse than an honestly stale one.
    """
    base, game = _read(NO_BID), _read(path)
    made = h2.set_mercenary_bid(base, 0, 900, province)
    made = h2.set_gold_remaining_value(made, h2.gold_remaining(base) - 900)
    changed = [i for i in range(len(made)) if made[i] != game[i]]
    assert changed == [len(game) - 2, len(game) - 1]


def test_withdrawing_restores_both_empty_values():
    """Amount 0 and province 0xFFFF, and nothing else touched."""
    bid = _read(BID_900_COPPER)
    back = h2.set_mercenary_bid(bid, 0, None)
    start = h2.mercenary_bid_offset(bid)
    provinces_at = (start - h2.OFF_MERCENARY_BID_AMOUNTS
                    + h2.OFF_MERCENARY_BID_PROVINCES)

    assert h2.read_mercenary_bids(back) == []
    assert [i for i in range(len(back)) if back[i] != bid[i]] == [
        start, start + 1, provinces_at, provinces_at + 1]


def test_a_bid_without_a_province_is_refused():
    """The hire screen always has a land selected, so an amount alone is a
    caller mistake, not a partial order to be completed with a guess."""
    with pytest.raises(h2.QueueWriteRefused, match="must name the province"):
        h2.set_mercenary_bid(_read(NO_BID), 0, 900)


def test_the_anchor_is_unique_in_every_expanded_save():
    """Turns 1-30, every controlled snapshot, exactly one anchor each.

    An offset would not survive here: the same structure sits at 0x3fd at
    turn 1 and 0x947 at turn 30. `mercenary_bid_offset` refuses a file
    holding anything other than exactly one anchor rather than writing a bid
    into whatever happens to be at a remembered address.
    """
    seen = 0
    for path in sorted(SNAPSHOTS.rglob("*.2h")):
        data = path.read_bytes()
        if len(data) < 1000:          # the turn-1 stub predates the structure
            continue
        offset = h2.mercenary_bid_offset(data)
        # the pretender's name follows the two arrays immediately
        name_at = offset - h2.OFF_MERCENARY_BID_AMOUNTS + 46
        assert data[name_at] != 0x4F, path
        seen += 1
    require_corpus(seen, 61, "expanded saves")


def test_half_a_bid_is_refused_rather_than_reported():
    """An amount without a key means the layout reading is wrong.

    Reporting the amount alone would hand the assistant a bid it cannot act
    on and hide the fact that this file is not the shape we think it is.
    """
    data = bytearray(_read(NO_BID))
    offset = h2.mercenary_bid_offset(bytes(data))
    data[offset:offset + 2] = (500).to_bytes(2, "little")
    with pytest.raises(h2.QueueWriteRefused, match="both or neither"):
        h2.read_mercenary_bids(bytes(data))


# -- intent, materialisation and the public tool --------------------------

_SCHEMA = Path("src/dom6_assistant/gamestate/schema.sql")


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO games(id, name) VALUES(1, 'test')")
    conn.commit()
    return conn


@pytest.fixture
def bid_save(tmp_path):
    """The turn-30 auction with no bid standing, plus its .trn."""
    snapshot = SNAPSHOTS / "t30-pd-baseline"
    h2_path = tmp_path / "mid_marignon.2h"
    trn_path = tmp_path / "mid_marignon.trn"
    if not (snapshot / "mid_marignon.2h").exists():
        pytest.skip("turn-30 baseline absent")
    shutil.copy2(snapshot / "mid_marignon.2h", h2_path)
    # The baseline snapshot carries no .trn of its own; the auction it needs
    # is the turn-30 one, which the bid snapshots preserve.
    shutil.copy2(SNAPSHOTS / "t30-merc-bid-747" / "mid_marignon.trn", trn_path)
    return h2_path


@pytest.fixture
def bid_session(tmp_path):
    snapshot = SNAPSHOTS / "t30-merc-bid-747"
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2("knowledge/game.sqlite3", game_db)
    session = open_session(save_dir=tmp_path, game_db=game_db)
    yield session
    session.close()


def test_company_specific_percentages_are_decoded_from_the_turn():
    path = SNAPSHOTS / "t30-merc-bid-747" / "mid_marignon.trn"
    companies = trn.read_mercenaries(_read(path))
    assert companies[0].nation_bid_percentages == (
        (61, 200), (54, 50), (99, 50))


def test_exact_minimum_uses_specific_override_before_nation_modifier():
    companies = trn.read_mercenaries(_read(
        SNAPSHOTS / "t30-merc-bid-747" / "mid_marignon.trn"))
    nergash, hell_hooves = companies
    ref = sqlite3.connect("knowledge/reference/reference.sqlite3")
    try:
        marignon = MC.calculate_minimum_bid(nergash, 61, ref)
        # MA Ermor's nation-wide +100% would make ordinary companies cost
        # double, but Nergash explicitly offers it 50% and that overrides.
        ermor_nergash = MC.calculate_minimum_bid(nergash, 54, ref)
        ermor_ordinary = MC.calculate_minimum_bid(hell_hooves, 54, ref)
    finally:
        ref.close()

    assert (marignon.amount, marignon.percentage) == (700, 200)
    assert (ermor_nergash.amount, ermor_nergash.percentage) == (175, 50)
    assert (ermor_ordinary.amount, ermor_ordinary.percentage) == (1000, 200)


def test_ghoul_father_matches_the_live_hire_screen_control():
    path = SNAPSHOTS / "t38-diplomacy-baseline" / "mid_marignon.trn"
    companies = trn.read_mercenaries(_read(path))
    ghoul_father = next(c for c in companies if c.name == "Ghoul Father")
    minimum = MC.calculate_minimum_bid(ghoul_father, 61, None)

    assert ghoul_father.price == 300
    assert ghoul_father.nation_bid_percentages == ((61, 200),)
    assert minimum.amount == 600


def test_public_bid_uses_the_automatic_nation_specific_minimum(bid_session):
    auction = bid_session.call("get_mercenaries", {})
    assert auction["ok"], auction.get("error")
    nergash = auction["result"]["companies"][0]
    assert nergash["leader"] == {
        "commander_id": 129,
        "name": "Nergash",
        "unit_type_id": 310,
        "unit_type": "Necromancer",
    }
    assert nergash["standing_bid"] == {
        "amount": 747,
        "arrival_province_id": MARIGNON,
        "arrival_province": "Marignon",
    }

    base = {
        "company": "Nergash's Damned Legion", "amount": 900,
        "province_id": COPPER_CANYONS, "rationale": "hire the legion"}
    below = bid_session.call("place_mercenary_bid", {**base, "amount": 699})
    assert not below["ok"]
    assert "below our nation-adjusted minimum of 700" in below["error"]

    accepted = bid_session.call("place_mercenary_bid", base)
    assert accepted["ok"], accepted.get("error")
    assert accepted["result"]["minimum_bid"] == 700
    assert accepted["result"]["minimum_bid_percentage"] == 200
    assert "refunds the gold next turn" in accepted["result"]["warning"]

    refreshed = bid_session.call("get_mercenaries", {})
    assert refreshed["ok"], refreshed.get("error")
    assert refreshed["result"]["companies"][0]["recorded_bid"] == {
        "action": "bid",
        "amount": 900,
        "arrival_province_id": COPPER_CANYONS,
        "arrival_province": "Copper Canyons",
        "rationale": "hire the legion",
    }

    obsolete = bid_session.call("place_mercenary_bid", {
        **base, "minimum_bid_shown": 700})
    assert not obsolete["ok"]
    assert "has no parameter" in obsolete["error"]


def test_materialising_a_bid_reproduces_the_game_authored_save(db, bid_save):
    """Intent in, the game's own bytes out — trailer excepted."""
    M.record_mercenary_bid(db, 1, 30, 0, "Nergash's Damned Legion", 900, 86,
                           quoted_minimum=700,
                           rationale="undead legion is cheap for its size")
    result = M.materialize(db, 1, 30, bid_save)

    assert not result.skipped, result.skipped
    game = _read(BID_900_COPPER)
    written = bid_save.read_bytes()
    changed = [i for i in range(len(written)) if written[i] != game[i]]
    assert changed == [len(game) - 2, len(game) - 1]


def test_replacing_a_bid_refunds_before_charging(db, bid_save):
    """Two recorded bids on the same slot must not both reserve gold.

    This is the same failure the ritual writer had to avoid: a replacement
    that charges without refunding leaves the treasury short by the old
    reservation, and the file still looks perfectly valid.
    """
    for amount in (747, 900):
        M.record_mercenary_bid(db, 1, 30, 0, "Nergash's Damned Legion",
                               amount, 86, quoted_minimum=700,
                               rationale="raising the bid")
    M.materialize(db, 1, 30, bid_save)

    data = bid_save.read_bytes()
    assert h2.read_mercenary_bids(data)[0].amount == 900
    assert h2.gold_remaining(data) == TURN_30_GOLD - 900


def test_withdrawing_returns_the_gold(db, bid_save):
    M.record_mercenary_bid(db, 1, 30, 0, "Nergash's Damned Legion", 900, 86,
                           quoted_minimum=700,
                           rationale="bid")
    M.materialize(db, 1, 30, bid_save)
    M.record_mercenary_bid(db, 1, 30, 0, "Nergash's Damned Legion", None, None,
                           rationale="need the gold for defence instead")
    result = M.materialize(db, 1, 30, bid_save)

    data = bid_save.read_bytes()
    assert h2.read_mercenary_bids(data) == []
    assert h2.gold_remaining(data) == TURN_30_GOLD
    assert any("withdrawn" in line for line in result.written), result.written


def test_recorded_calculated_minimum_is_a_hard_floor(db):
    with pytest.raises(ValueError, match="requires the calculated"):
        M.record_mercenary_bid(
            db, 1, 30, 0, "Nergash's Damned Legion", 900, 86,
            rationale="missing internal calculation")

    with pytest.raises(ValueError, match="below quoted minimum"):
        M.record_mercenary_bid(
            db, 1, 30, 0, "Nergash's Damned Legion", 699, 86,
            quoted_minimum=700, rationale="too low")

    row_id = M.record_mercenary_bid(
        db, 1, 30, 0, "Nergash's Damned Legion", 900, 86,
        quoted_minimum=700, rationale="calculator-legal bid")
    row = db.execute(
        "SELECT quoted_minimum FROM mercenary_bid_intent WHERE id=?",
        (row_id,)).fetchone()
    assert row[0] == 700


def test_materializer_calculates_a_legacy_bid_without_a_screen_minimum(
        db, bid_save):
    db.execute(
        "INSERT INTO mercenary_bid_intent"
        "(game_id, turn, slot, company, amount, province_id, rationale) "
        "VALUES(?,?,?,?,?,?,?)",
        (1, 30, 0, "Nergash's Damned Legion", 900, 86,
         "legacy row from before minimums were captured"))
    db.commit()

    result = M.materialize(db, 1, 30, bid_save)

    assert not result.skipped
    assert any("900 gold" in line for line in result.written), result.written
    assert h2.read_mercenary_bids(bid_save.read_bytes())[0].amount == 900


def test_materializer_rechecks_the_current_nation_wide_minimum(
        db, bid_save):
    """The stored audit quote cannot bypass a changed/current price.

    Rename the same controlled files to MA Ermor so the public parser supplies
    nation 54. Ermor's attribute 271 is +100%, and Hell Hooves has no specific
    override, so its stored 500 becomes a legal minimum of 1000.
    """
    ermor_h2 = bid_save.with_name("mid_ermor.2h")
    ermor_trn = bid_save.with_name("mid_ermor.trn")
    shutil.copy2(bid_save, ermor_h2)
    shutil.copy2(bid_save.with_suffix(".trn"), ermor_trn)
    M.record_mercenary_bid(
        db, 1, 30, 1, "Hell Hooves", 999, 86,
        quoted_minimum=500, rationale="stale audit value")
    ref = sqlite3.connect("knowledge/reference/reference.sqlite3")
    try:
        result = M.materialize(
            db, 1, 30, ermor_h2, reference_conn=ref)
    finally:
        ref.close()

    assert result.aborted
    assert any("current nation-adjusted minimum of 1000" in line
               for line in result.skipped), result.skipped
    assert h2.read_mercenary_bids(ermor_h2.read_bytes()) == []


def test_a_slot_holding_a_different_company_is_refused(db, bid_save):
    """The auction turns over between turns, and intent outlives it.

    A slot index alone would let last turn's decision spend this turn's gold
    on whichever company moved into that position — a mistake that surfaces
    only when the wrong troops arrive.
    """
    M.record_mercenary_bid(db, 1, 30, 0, "Bernard's Brave Men", 900, 86,
                           quoted_minimum=700,
                           rationale="stale decision from an earlier turn")
    result = M.materialize(db, 1, 30, bid_save)

    assert not result.written
    assert any("Nergash's Damned Legion" in line and "refusing" in line
               for line in result.skipped), result.skipped
    assert h2.read_mercenary_bids(bid_save.read_bytes()) == []
    assert h2.gold_remaining(bid_save.read_bytes()) == TURN_30_GOLD


# -- two companies: what proves the slot is the auction position ----------

BID_TWO = SNAPSHOTS / "t30-merc-bid-two-companies" / "mid_marignon.2h"
OBSIDIAN_WASTE = 98


def test_a_second_bid_fills_the_second_slot():
    """Adding a bid on the second company left the first entirely alone.

    This is what names the slot. With one company on offer, slot 0 is the
    only slot the client could have used, so "slot = auction position" fitted
    the data without being shown by it. Bidding 611 on Hell Hooves — the
    second company, and one already under contract to Machaka — wrote slot 1
    and moved seven bytes in the whole file.
    """
    before, after = _read(BID_900_COPPER), _read(BID_TWO)
    bids = h2.read_mercenary_bids(after)

    assert [(b.slot, b.amount, b.province) for b in bids] == [
        (0, 900, COPPER_CANYONS), (1, 611, OBSIDIAN_WASTE)]
    # the standing bid is untouched, which is the half that matters
    assert h2.read_mercenary_bids(before)[0] == bids[0]
    assert h2.gold_remaining(after) == TURN_30_GOLD - 900 - 611


def test_both_arrays_are_indexed_by_the_same_slot():
    """Only slot 1 of each array moved, and nothing between them."""
    before, after = _read(BID_900_COPPER), _read(BID_TWO)
    start = h2.mercenary_bid_offset(before)
    provinces_at = (start - h2.OFF_MERCENARY_BID_AMOUNTS
                    + h2.OFF_MERCENARY_BID_PROVINCES)
    changed = {i for i in range(min(len(before), len(after)))
               if before[i] != after[i]}

    assert {start + 2, start + 3} <= changed          # amount slot 1
    assert {provinces_at + 2, provinces_at + 3} <= changed   # province slot 1
    assert not ({start, start + 1} & changed)         # amount slot 0 held
    assert not ({provinces_at, provinces_at + 1} & changed)  # province 0 held


def test_a_contracted_company_can_be_bid_on():
    """Hell Hooves was on contract to Machaka and still took a bid.

    The hire screen implies this and nothing had tested it. It matters because
    the tool must not refuse a company merely because someone else holds it —
    the month it comes free is exactly when a bid is worth placing.
    """
    from dom6_assistant.file_reader.formats.trn import read_mercenaries

    trn = SNAPSHOTS / "t30-merc-bid-two-companies" / "mid_marignon.trn"
    companies = read_mercenaries(_read(trn))
    hell_hooves = companies[1]

    assert hell_hooves.name == "Hell Hooves"
    assert hell_hooves.available is False
    assert h2.read_mercenary_bids(_read(BID_TWO))[1].slot == 1


def test_the_writer_reproduces_the_two_company_save():
    """Both bids generated from a file with no bid at all."""
    base, game = _read(NO_BID), _read(BID_TWO)
    made = h2.set_mercenary_bid(base, 0, 900, COPPER_CANYONS)
    made = h2.set_mercenary_bid(made, 1, 611, OBSIDIAN_WASTE)
    made = h2.set_gold_remaining_value(
        made, h2.gold_remaining(base) - 900 - 611)
    changed = [i for i in range(len(made)) if made[i] != game[i]]
    assert changed == [len(game) - 2, len(game) - 1]
