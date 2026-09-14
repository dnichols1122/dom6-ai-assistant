from pathlib import Path

import pytest

from dom6_assistant.agent.visibility import PlayerView
from dom6_assistant.file_reader.formats.diplomacy import (
    OutgoingDiplomacyRecord,
    SAME_GOD,
    find_controller_states,
    find_relation_matrix,
    find_outgoing_diplomacy,
    nap_state,
    set_outgoing_diplomacy,
    find_waiting_targets,
)

from ..conftest import require_corpus

SNAPSHOTS = Path("knowledge/snapshots")


def _view(label: str) -> PlayerView:
    return PlayerView(SNAPSHOTS / label, 61)


def test_relation_matrix_is_located_and_defaults_omitted_cells_to_zero():
    view = _view("t38-kratas-resolved")
    matrix = find_relation_matrix(view.data)
    assert matrix.offset == 215383
    assert matrix.code(61, 61) == SAME_GOD
    assert matrix.code(61, 85) == 0
    assert matrix.code(85, 61) == 0
    assert matrix.participant_nation_ids == (61, 74, 76, 81, 85, 87)


def test_first_border_changes_ys_from_no_contact_to_apprehensive():
    before = {row.nation_id: row for row in _view("t37-pre-kratas-attack").diplomatic_relations()}
    after = {row.nation_id: row for row in _view("t38-kratas-resolved").diplomatic_relations()}
    assert before[85].contact is False
    assert before[85].status == "no_contact"
    assert after[85].contact is True
    assert after[85].status == "apprehensive"


def test_controlled_outgoing_nap_becomes_waiting_on_the_next_turn():
    root = SNAPSHOTS / "example_game"
    before = PlayerView(root / "t14-auto", 54, "mid_ermor.trn")
    sender = PlayerView(root / "t15-auto", 54, "mid_ermor.trn")
    recipient = PlayerView(root / "t15-auto", 61, "mid_marignon.trn")
    assert find_waiting_targets(before.data, 54) == ()
    assert find_waiting_targets(sender.data, 54) == (61,)
    assert find_waiting_targets(recipient.data, 61) == ()
    sender_relation = sender.diplomatic_relations()[0]
    recipient_relation = recipient.diplomatic_relations()[0]
    assert sender_relation.status == "waiting"
    assert sender_relation.waiting_for_response is True
    assert recipient_relation.status == "apprehensive"
    assert recipient_relation.waiting_for_response is False


def test_defeated_nation_is_not_misreported_as_no_contact():
    relations = {row.nation_id: row for row in _view("t38-kratas-resolved").diplomatic_relations()}
    assert relations[81].defeated is False
    assert relations[81].status == "no_contact"
    assert relations[87].defeated is True
    assert relations[87].status == "defeated"


def test_controller_states_match_the_f4_panel():
    view = _view("t38-kratas-resolved")
    matrix = find_relation_matrix(view.data)
    assert find_controller_states(view.data, matrix.participant_nation_ids) == {
        61: "human",
        74: "AI",
        76: "AI",
        81: "AI",
        85: "AI",
        87: "defeated",
    }


def test_controller_state_uses_live_record_before_historical_copy():
    root = SNAPSHOTS / "example_game" / "t47-auto"
    for nation_id, stem in ((54, "mid_ermor"), (61, "mid_marignon")):
        view = PlayerView(root, nation_id, f"{stem}.trn")
        relation = view.diplomatic_relations()[0]
        assert relation.controller == "human"
        assert relation.status == "apprehensive"


def test_directional_nap_codes_match_f4_labels():
    assert nap_state(-13) == ("active", 3)
    assert nap_state(-3) == ("ending", 3)
    assert nap_state(-1) == ("ending", 1)
    assert nap_state(0) == (None, None)
    assert nap_state(SAME_GOD) == (None, None)


def test_empty_h2_diplomacy_list_is_anchored_at_the_final_trailer():
    data = (SNAPSHOTS / "t38-diplomacy-baseline" / "mid_marignon.2h").read_bytes()
    start, terminator, rows = find_outgoing_diplomacy(data)
    assert start == terminator == len(data) - 11
    assert rows == []


def test_turn_one_preorders_file_intentionally_has_no_final_diplomacy_section():
    data = (SNAPSHOTS / "t1-preorders" / "mid_marignon.2h").read_bytes()
    try:
        find_outgoing_diplomacy(data)
    except ValueError as exc:
        assert "no final 0x3102 diplomacy trailer" in str(exc)
    else:  # pragma: no cover - protects the fixture's defining edge case
        raise AssertionError("turn-1 pre-orders unexpectedly gained a diplomacy trailer")


def test_nap_proposal_round_trips_the_client_runtime_record():
    data = (SNAPSHOTS / "t38-diplomacy-baseline" / "mid_marignon.2h").read_bytes()
    proposal = OutgoingDiplomacyRecord(
        slot=0,
        target_nation_id=85,
        type_id=4,
        extra=0,
        text="We propose peace and a non-aggression treaty",
    )
    written = set_outgoing_diplomacy(data, [proposal])
    _start, _end, rows = find_outgoing_diplomacy(written)
    assert rows == [proposal]
    assert len(written) == len(data) + 56
    assert written[-6:] == data[-6:]


def test_nap_writer_matches_controlled_game_authored_proposal():
    """Ermor -> Marignon at first contact, with no other player action."""
    root = SNAPSHOTS / "example_game"
    before = (root / "t14-auto" / "mid_ermor.2h").read_bytes()
    after = (root / "t14-auto-2" / "mid_ermor.2h").read_bytes()
    proposal = OutgoingDiplomacyRecord(
        slot=0,
        target_nation_id=61,
        type_id=4,
        extra=0,
        text="We propose peace and a non-aggression treaty",
    )
    generated = set_outgoing_diplomacy(before, [proposal])
    assert find_outgoing_diplomacy(after)[2] == [proposal]
    # The game rewrites its non-enforced final two-byte trailer. Every byte the
    # host actually interprets is identical to our generated order.
    assert generated[:-2] == after[:-2]


def test_nap_writer_matches_controlled_game_authored_acceptance():
    """Marignon accepts Ermor's turn-15 proposal, with no other player action."""
    root = SNAPSHOTS / "example_game"
    before = (root / "t15-auto" / "mid_marignon.2h").read_bytes()
    after = (root / "t15-auto-2" / "mid_marignon.2h").read_bytes()
    acceptance = OutgoingDiplomacyRecord(
        slot=0,
        target_nation_id=54,
        type_id=5,
        extra=0,
        text="We accept the non-aggression treaty",
    )
    generated = set_outgoing_diplomacy(before, [acceptance])
    assert find_outgoing_diplomacy(after)[2] == [acceptance]
    # As with the proposal control, only the non-enforced final trailer differs.
    assert generated[:-2] == after[:-2]


def test_nap_writer_matches_controlled_game_authored_decline():
    """Marignon rejects Ermor's turn-15 proposal, with no other player action."""
    root = SNAPSHOTS / "example_game"
    before = (root / "t15-auto" / "mid_marignon.2h").read_bytes()
    after = (root / "t15-auto-3" / "mid_marignon.2h").read_bytes()
    decline = OutgoingDiplomacyRecord(
        slot=0,
        target_nation_id=54,
        type_id=6,
        extra=0,
        text="We decline the non-aggression treaty",
    )
    generated = set_outgoing_diplomacy(before, [decline])
    assert find_outgoing_diplomacy(after)[2] == [decline]
    assert generated[:-2] == after[:-2]


def test_suing_for_peace_ends_the_war_the_moment_the_pact_forms():
    """War back to peace, the only route there, in three controlled turns.

    The proposal record is identical to one sent from `apprehensive` -- the
    originating relation state does not change the outgoing bytes.  Acceptance
    then takes both rows straight from `5` to `-13`; there is no intervening
    turn of `0` while the war formally lapses, so the F4 label never leads the
    real relation.
    """
    root = SNAPSHOTS / "example_game"
    if not (root / "t22-auto" / "mid_ermor.trn").exists():
        pytest.skip("suing-for-peace snapshots absent")

    proposal = find_outgoing_diplomacy(
        (root / "t20-auto-2" / "mid_ermor.2h").read_bytes()
    )[2]
    assert proposal == [
        OutgoingDiplomacyRecord(
            slot=0,
            target_nation_id=61,
            type_id=4,
            extra=0,
            text="We propose peace and a non-aggression treaty",
        )
    ]

    # The war is untouched while the herald is away: both rows still 5.
    for stem, us in (("ermor", 54), ("marignon", 61)):
        view = PlayerView(root / "t21-auto", us, f"mid_{stem}.trn")
        assert view.diplomatic_relations()[0].relation_code == 5

    # 5 -> -13 directly. No apprehensive step in between.
    for stem, us, them in (("ermor", 54, 61), ("marignon", 61, 54)):
        relation = PlayerView(
            root / "t22-auto", us, f"mid_{stem}.trn"
        ).diplomatic_relations()[0]
        assert relation.status == "NAP-3"
        assert relation.relation_code == -13
        assert relation.reciprocal_code == -13
        assert relation.waiting_for_response is False

    # The acceptance report has no war-specific variant.
    accept = next(
        row
        for row in PlayerView(root / "t22-auto", 54, "mid_ermor.trn").turn_messages()
        if row.diplomatic_action == "accept_nap"
    )
    assert accept.kind == "incoming_nap_accepted"
    assert accept.body_and_effects[1] == ("Non-aggression pact with Marignon",)


def test_waiting_masks_war_in_the_client_too():
    """Confirmed against the client, not inferred from our own chain.

    Our status chain tests the `waiting` overlay before the war cell, so a
    proposal sent during a war hides `war` behind `waiting`.  That ordering was
    only ever established against a zero relation, and masking a live war would
    be a serious misreport -- so the player checked the panel directly on turn
    21.  Ermor's row read `waiting`; Marignon's, with no outgoing proposal of
    its own, still read `war`.  Both match this decoder exactly.
    """
    root = SNAPSHOTS / "example_game"
    if not (root / "t21-auto" / "mid_ermor.trn").exists():
        pytest.skip("suing-for-peace snapshots absent")
    ermor = PlayerView(root / "t21-auto", 54, "mid_ermor.trn").diplomatic_relations()[0]
    assert ermor.status == "waiting"
    assert ermor.waiting_for_response is True
    # The war is still there underneath, and still readable.
    assert ermor.relation_code == 5
    assert ermor.reciprocal_code == 5

    marignon = PlayerView(
        root / "t21-auto", 61, "mid_marignon.trn"
    ).diplomatic_relations()[0]
    assert marignon.status == "war"
    assert marignon.waiting_for_response is False


def test_dispel_targets_a_global_by_chain_index_in_the_province_field():
    """The selector is positional, and it hides in the target-province field.

    Three globals are active in chain order [17, 10, 29].  Dispelling Burden of
    Time -- effect id 29, chain index 2 -- writes 2 into `+124`, so it is the
    index rather than the id, and it is not in `+132` as predicted.

    The single-global case is included because it is why this looked absent:
    that dispel wrote `+124 = 0`, which is both the only enchantment's index
    and the untargeted-province sentinel.
    """
    root = SNAPSHOTS / "example_game"
    if not (root / "t45-auto-3" / "mid_marignon.2h").exists():
        pytest.skip("targeted-dispel snapshot absent")
    import struct

    from dom6_assistant.file_reader.formats import trn as T

    order = (root / "t45-auto-3" / "mid_marignon.2h").read_bytes()
    base = order.find(struct.pack("<i", 1180)) - 116
    fields = {
        offset: struct.unpack_from("<i", order, base + offset)[0]
        for offset in (116, 120, 124, 128, 132)
    }
    assert fields == {116: 1180, 120: 30, 124: 2, 128: -1, 132: -1}

    chain = T.read_global_effects(
        (root / "t45-auto-3" / "mid_marignon.trn").read_bytes()
    )
    assert [e.effect_id for e in chain] == [17, 10, 29]
    assert chain[fields[124]].effect_id == 29, "index selects Burden of Time"
    assert fields[124] != 29, "and it is the index, not the effect id"

    # The one-global dispel: index 0, indistinguishable from an empty field.
    single = (root / "t28-auto-3" / "mid_marignon.2h").read_bytes()
    single_base = single.find(struct.pack("<i", 1180)) - 116
    assert struct.unpack_from("<i", single, single_base + 124)[0] == 0


def test_the_index_selector_hits_the_intended_global_end_to_end():
    """Queued index 2, and the host attacked Burden of Time.

    The `.2h` proves which index was written; this proves what the host did
    with it.  With three globals up, the victim's report names the one that was
    attacked, so the selector's semantics are confirmed by the game rather than
    inferred from the byte alone.

    Note the asymmetry: the victim learns *which* of its globals was hit, while
    the dispeller's own report never names its target.  An agent must therefore
    keep its own record of what it aimed at.
    """
    root = SNAPSHOTS / "example_game"
    if not (root / "t46-auto" / "mid_ermor.trn").exists():
        pytest.skip("hosted targeted-dispel snapshot absent")
    from dom6_assistant.file_reader.formats import trn as T

    ermor = PlayerView(root / "t46-auto", 54, "mid_ermor.trn")
    attacked = next(
        row for row in ermor.turn_messages()
        if row.kind == "global_dispel_failed"
    )
    assert "disturbance in the Burden of Time" in attacked.text
    assert "Foul Air" not in attacked.text

    chain = T.read_global_effects(ermor.data)
    assert [e.effect_id for e in chain] == [17, 10, 29]
    # All three survive, and the failed dispel left the target's overcast alone.
    burden = chain[2]
    assert burden.effect_id == 29 and burden.value1 == 20

    dispeller = next(
        row for row in PlayerView(root / "t46-auto", 61, "mid_marignon.trn").turn_messages()
        if row.kind == "own_ritual_cast"
    )
    assert "overpowered by the global enchantment" in dispeller.text
    assert "Burden of Time" not in dispeller.text


def test_the_dispel_selector_is_the_slot_not_the_ordinal_position():
    """The one case where the two readings disagree.

    After Foul Air was dispelled out of slot 1, Burden of Time sits at slot 2
    but ordinal position 1.  Ermor's Dispel against it writes 2, so the
    selector is the stable slot rather than the position in the list.

    That matters for a writer: an unrelated dispel elsewhere in the chain does
    not retarget a queued order.  It also shows self-targeting is allowed --
    Burden of Time is Ermor's own global.
    """
    root = SNAPSHOTS / "example_game"
    if not (root / "t46-auto-4" / "mid_ermor.2h").exists():
        pytest.skip("self-dispel snapshot absent")
    import struct

    from dom6_assistant.file_reader.formats import trn as T

    order = (root / "t46-auto-4" / "mid_ermor.2h").read_bytes()
    # The spell id appears elsewhere in the file, so find the occurrence whose
    # neighbouring fields actually look like a ritual block.
    needle = struct.pack("<i", 1180)
    offset = order.find(needle)
    while offset >= 0:
        cost = struct.unpack_from("<i", order, offset + 4)[0]
        if 0 < cost < 1000:
            break
        offset = order.find(needle, offset + 1)
    assert offset >= 0, "no ritual block found for Dispel"
    base = offset - 116
    assert struct.unpack_from("<i", order, base + 120)[0] == 110
    selector = struct.unpack_from("<i", order, base + 124)[0]

    chain = T.read_global_effects((root / "t46-auto-4" / "mid_ermor.trn").read_bytes())
    burden = next(e for e in chain if e.effect_id == 29)
    assert burden.slot == 2
    assert chain.index(burden) == 1, "ordinal and slot must differ for this test"
    assert selector == burden.slot == 2
    assert selector != chain.index(burden)
    # Ermor's own enchantment, so the client permits self-targeting.
    assert burden.caster_nation_id == 54


def test_a_one_sided_war_row_is_reported_unknown_rather_than_peaceful():
    """Guards the assumption we chose not to test.

    Attacking with no pact and no declaration flips the relation to war
    automatically.  That transition is derived from the declared-war case
    rather than observed, and the one shape it could plausibly take that the
    others did not is a one-sided write.  Because the F4 rule keys on the
    *reciprocal* cell, such a row would otherwise read `apprehensive` -- a
    silent claim of peace during a war.  It reports `unknown` instead.
    """
    class _OneSided:
        """A matrix where only our row records the war."""

        def __init__(self, real):
            self._real = real
            self.offset = real.offset
            self.entries = real.entries
            self.participant_nation_ids = real.participant_nation_ids

        def code(self, row, column):
            if (row, column) == (54, 61):
                return 5
            if (row, column) == (61, 54):
                return 0
            return self._real.code(row, column)

    root = SNAPSHOTS / "example_game"
    if not (root / "t20-auto" / "mid_ermor.trn").exists():
        pytest.skip("war snapshot absent")
    view = PlayerView(root / "t20-auto", 54, "mid_ermor.trn")
    assert view.diplomatic_relations()[0].status == "war"

    import dom6_assistant.agent.visibility as V

    real = V.D.find_relation_matrix
    V.D.find_relation_matrix = lambda data: _OneSided(real(data))  # type: ignore[assignment]
    try:
        relation = view.diplomatic_relations()[0]
    finally:
        V.D.find_relation_matrix = real  # type: ignore[assignment]
    assert relation.relation_code == 5
    assert relation.reciprocal_code == 0
    assert relation.status == "unknown"


def test_declaration_writer_matches_the_controlled_game_authored_file():
    """The last writer that had never been byte-compared against the client.

    Propose, accept and decline were each verified this way.  The declaration
    writer was built from one observation and, until the missive turned out to
    be nation-flavoured, was building the text itself.  Now that the caller
    supplies the text, the record it produces can be checked against the file
    the game wrote from the same root.
    """
    root = SNAPSHOTS / "example_game"
    before = root / "t16-auto-2" / "mid_ermor.2h"
    after = root / "t16-auto-3" / "mid_ermor.2h"
    if not after.exists():
        pytest.skip("war-under-NAP snapshot absent")
    declaration = OutgoingDiplomacyRecord(
        slot=0,
        target_nation_id=61,
        type_id=3,
        extra=0,
        text=(
            "A message from Ermor\n\n"
            "Your nation will be destroyed and your people shall join our "
            "legion of the dead!"
        ),
    )
    generated = set_outgoing_diplomacy(before.read_bytes(), [declaration])
    written = after.read_bytes()
    assert find_outgoing_diplomacy(written)[2] == [declaration]
    # As with every other controlled writer check, only the game's non-enforced
    # final two-byte trailer differs.
    assert generated[:-2] == written[:-2]


def test_breaking_a_pact_reuses_the_ordinary_war_declaration_type():
    """Ermor declares war on Marignon while NAP-3 is active, nothing else changed.

    Breaking a pact is a separate F4 choice, so it could have carried a fifth
    action type.  It does not: the record is an ordinary type 3.
    """
    root = SNAPSHOTS / "example_game"
    before = root / "t16-auto-2" / "mid_ermor.2h"
    after = root / "t16-auto-3" / "mid_ermor.2h"
    if not after.exists():
        pytest.skip("war-under-NAP snapshot absent")
    assert find_outgoing_diplomacy(before.read_bytes())[2] == []
    records = find_outgoing_diplomacy(after.read_bytes())[2]
    assert len(records) == 1
    declaration = records[0]
    assert declaration.slot == 0
    assert declaration.target_nation_id == 61
    assert declaration.type_id == 3
    assert declaration.action == "declare_war"
    assert declaration.extra == 0


def test_the_matrix_also_carries_a_nation_zero_scalar_that_is_not_a_relation():
    """Row/column 0 is not a relation and must never be read as one.

    The two-human game has exactly two participants and no third party, yet
    both nations still carry a growing value against id 0.  So the 0x1e1f
    section is not purely a relation matrix: id 0 holds some per-nation figure.
    It tracks the empire loosely -- correlated with owned provinces but equal
    to neither that nor dominion spread -- and is left undecoded rather than
    guessed at.  Nothing exposes it, because participants are identified by the
    -99 diagonal and id 0 has none.
    """
    root = SNAPSHOTS / "example_game"
    matrix = find_relation_matrix((root / "t19-auto" / "mid_ermor.trn").read_bytes())
    assert matrix.participant_nation_ids == (54, 61)
    assert matrix.code(54, 0) > 0 and matrix.code(61, 0) > 0
    # Symmetric, unlike the genuine directional nation-vs-nation cells.
    assert matrix.code(0, 54) == matrix.code(54, 0)
    assert 0 not in matrix.participant_nation_ids


def test_positive_relations_exist_only_in_rows_we_never_expose():
    """`reciprocal > 0 means war` has never been checked against a shown row.

    Restricted to genuine nation-vs-nation cells -- id 0 excluded, since it is
    not a nation -- the only positive relations in the whole corpus are eight
    cells among four AI pairs in the single-human game.  `PlayerView` withholds
    those and no UI ever confirmed them.  Our own row has never held a positive
    value in any save.

    They also rise *and fall*, so the value is a fluctuating scalar rather than
    a war-duration counter or a flag.  That is compatible with the client
    branching on `> 0` to print "war" while the magnitude means something else,
    but it is read from the client's logic, not watched happening.
    """
    ours: list[int] = []
    foreign: dict[tuple[int, int], list[int]] = {}
    scanned = 0
    for path in sorted(SNAPSHOTS.rglob("*.trn")):
        if "example_game" in str(path):
            continue
        scanned += 1
        for row, column, value in find_relation_matrix(path.read_bytes()).entries:
            if row == column or row < 5 or column < 5:
                continue
            if row == 61:
                ours.append(value)
            else:
                foreign.setdefault((row, column), []).append(value)
    # "eight cells in the whole corpus" is an exact claim, so the guard is on
    # having a corpus at all rather than on the count.
    require_corpus(scanned, 1, "saves")
    assert ours == [], f"our own row held {sorted(set(ours))}; the war rule is now testable"
    assert len(foreign) == 8
    assert all(value > 0 for values in foreign.values() for value in values)
    assert any(
        b < a for values in foreign.values() for a, b in zip(values, values[1:])
    ), "a war-duration counter would never decrease"


def test_declaring_under_a_pact_moves_the_active_row_into_the_ending_range():
    """-13 becomes -3 on both rows: NAP-3 active becomes NAP-3* ending.

    The ending encoding is the active one without its -10 bias, which is what
    the relation table predicted before any pact had ever been broken.  Both
    rows move together, as they did when the pact was formed.
    """
    root = SNAPSHOTS / "example_game"
    if not (root / "t17-auto" / "mid_ermor.trn").exists():
        pytest.skip("hosted war-under-NAP snapshot absent")
    for stem, us, them in (("ermor", 54, 61), ("marignon", 61, 54)):
        before = PlayerView(root / "t16-auto-2", us, f"mid_{stem}.trn")
        after = PlayerView(root / "t17-auto", us, f"mid_{stem}.trn")
        was = before.diplomatic_relations()[0]
        now = after.diplomatic_relations()[0]
        assert (was.relation_code, was.status) == (-13, "NAP-3")
        assert (now.relation_code, now.status) == (-3, "NAP-3*")
        assert now.nap_phase == "ending"
        assert now.nap_notice_turns == 3
        # Symmetric: the reciprocal cell holds the same value, so neither side
        # is left believing a different pact state from the other.
        assert now.reciprocal_code == -3
        assert find_relation_matrix(after.data).code(them, us) == -3


def test_the_ending_notice_counts_down_one_per_hosted_turn_and_says_nothing():
    """The pact expires on a silent timer.

    Only the declaration turn produces a message.  Every later tick changes the
    relation byte and announces nothing, on either side, so the countdown is
    readable only from the relation row.  An agent that waits to be told a war
    is coming will be told once and then surprised.
    """
    root = SNAPSHOTS / "example_game"
    countdown = [("t17-auto", -3), ("t18-auto", -2), ("t19-auto", -1)]
    available = [step for step in countdown if (root / step[0] / "mid_ermor.trn").exists()]
    if not available:
        pytest.skip("hosted countdown snapshots absent")
    for snapshot, expected in available:
        for stem, us in (("ermor", 54), ("marignon", 61)):
            view = PlayerView(root / snapshot, us, f"mid_{stem}.trn")
            relation = view.diplomatic_relations()[0]
            assert relation.relation_code == expected, snapshot
            assert relation.reciprocal_code == expected, snapshot
            assert relation.nap_phase == "ending"
            assert relation.nap_notice_turns == abs(expected)
            assert relation.status == f"NAP-{abs(expected)}*"
            # Silent after the declaration turn itself.
            if snapshot != "t17-auto":
                assert all(
                    row.diplomatic_action is None for row in view.turn_messages()
                ), f"{snapshot}/{stem} announced something"


def test_the_pact_expires_into_war_at_relation_five_and_says_nothing():
    """The last unobserved region of the relation table, and it arrives silent.

    -1 becomes +5 on both rows and F4 reads `war`.  This is the first time the
    `reciprocal > 0` branch has ever run against a row the client shows us; it
    had been inferred from the client's logic alone.  Five also sits at the
    bottom of the range the AI-vs-AI cells occupy mid-war, which fits reading
    the magnitude as an attitude scalar that starts low and drifts.

    Nothing announces it.  Neither nation receives any diplomatic message on
    the turn its war begins; what they do receive is ordinary unrelated news --
    two province events for Marignon and a worldwide event both of them see.
    So the war is not merely unannounced, it is buried in a normal turn.
    """
    root = SNAPSHOTS / "example_game"
    if not (root / "t20-auto" / "mid_ermor.trn").exists():
        pytest.skip("hosted war-expiry snapshot absent")
    for stem, us, them in (("ermor", 54, 61), ("marignon", 61, 54)):
        before = PlayerView(root / "t19-auto", us, f"mid_{stem}.trn")
        view = PlayerView(root / "t20-auto", us, f"mid_{stem}.trn")
        assert before.diplomatic_relations()[0].status == "NAP-1*"
        relation = view.diplomatic_relations()[0]
        assert relation.nation_id == them
        assert relation.status == "war"
        assert relation.relation_code == 5
        assert relation.reciprocal_code == 5
        assert relation.nap_phase is None
        assert relation.nap_notice_turns is None
        assert relation.contact is True
        assert find_relation_matrix(view.data).code(them, us) == 5
        assert all(row.diplomatic_action is None for row in view.turn_messages())
        assert all(row.type_id not in (20, 21) for row in view.turn_messages())
    # The turn is not empty -- it just says nothing about the war.
    marignon = PlayerView(root / "t20-auto", 61, "mid_marignon.trn")
    assert [row.is_worldwide for row in marignon.turn_messages()] == [False, False, True]


def test_war_declaration_missive_is_delivered_verbatim_unlike_a_nap_proposal():
    """The two outgoing missives are treated completely differently.

    A declaration's stored text is embedded in the target's message with only a
    prefix added.  A NAP proposal's stored text is never delivered at all; the
    host writes its own herald prose naming our god.  So the declaration
    missive is real content and must not be invented, while the proposal's is
    effectively a label.
    """
    root = SNAPSHOTS / "example_game"
    if not (root / "t17-auto" / "mid_marignon.trn").exists():
        pytest.skip("hosted war-under-NAP snapshot absent")
    stored = find_outgoing_diplomacy(
        (root / "t16-auto-3" / "mid_ermor.2h").read_bytes()
    )[2][0].text
    delivered = next(
        row for row in PlayerView(root / "t17-auto", 61, "mid_marignon.trn").turn_messages()
        if row.type_id == 21
    )
    assert delivered.kind == "incoming_war_declaration"
    assert delivered.diplomatic_action == "declare_war"
    assert delivered.source_nation_id == 54
    assert delivered.recipient_nation_id == 61
    assert delivered.fields == (0,) * 8
    assert delivered.text == f"A messenger has arrived from Ermor.\n\n{stored}"

    proposed = find_outgoing_diplomacy(
        (root / "t14-auto-2" / "mid_ermor.2h").read_bytes()
    )[2][0].text
    herald = next(
        row for row in PlayerView(root / "t15-auto", 61, "mid_marignon.trn").turn_messages()
        if row.type_id == 20
    )
    assert proposed not in herald.text
    assert "Mambo, God of Ermor" in herald.text


def test_war_declaration_missive_is_nation_flavoured_not_a_name_template():
    """The declaration text is not a fixed sentence with a name substituted.

    Ermor's declaration is written in Ermor's own voice.  The only other
    declaration text ever recorded -- "your unholy nation" -- reads as
    Marignon's crusader voice, so the sentence itself varies by nation and
    cannot be reconstructed from a template.  It is not present in either
    reference database, so it lives in the game binary's string table.
    """
    root = SNAPSHOTS / "example_game"
    after = root / "t16-auto-3" / "mid_ermor.2h"
    if not after.exists():
        pytest.skip("war-under-NAP snapshot absent")
    text = find_outgoing_diplomacy(after.read_bytes())[2][0].text
    assert text == (
        "A message from Ermor\n\n"
        "Your nation will be destroyed and your people shall join our legion "
        "of the dead!"
    )
    # The header names the nation, not the pretender. Ermor's pretender is
    # Mambo, and the god's name appears in message bodies rather than here.
    assert "Mambo" not in text
    assert "unholy nation" not in text
