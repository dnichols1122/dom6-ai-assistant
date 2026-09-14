"""The serialized item-state array and late-game yearning sentinel."""

from pathlib import Path

import pytest

from dom6_assistant.file_reader.formats import trn as T


ROOT = Path("knowledge/snapshots/example_game_3")
SAUROMATIA = "early_sauromatia.trn"
TIEN_CHI = "early_tienchi.trn"


def _states(turn: int, stem: str = SAUROMATIA) -> tuple[int, ...]:
    path = ROOT / f"t{turn}-auto" / stem
    if not path.exists():
        pytest.skip(f"yearning control absent: {path}")
    states = T.read_item_states(path.read_bytes())
    assert states is not None
    assert len(states) == T.ITEM_STATE_COUNT
    return states


def _yearning(states: tuple[int, ...]) -> set[int]:
    return {
        item_id for item_id, state in enumerate(states)
        if state == T.ITEM_STATE_YEARNING
    }


def test_yearning_appears_as_minus_98_in_successive_turns():
    assert _yearning(_states(60)) == set()
    assert _yearning(_states(61)) == {451}  # The Death Globes
    assert _yearning(_states(62)) == {439, 451}  # + Alchemist's Stone
    assert _yearning(_states(67)) == {106, 110, 128, 439, 451, 456}


def test_both_human_player_files_receive_the_same_world_item_state():
    assert _states(61, SAUROMATIA) == _states(61, TIEN_CHI)
    assert _states(67, SAUROMATIA) == _states(67, TIEN_CHI)


def test_positive_and_zero_states_are_not_yearning():
    states = _states(67)
    assert states[2] == 1       # one Ice Sword exists
    assert states[37] == 0      # a spent/destroyed Hunter's Knife
    assert states[451] == T.ITEM_STATE_YEARNING
    assert states[500] == T.ITEM_STATE_UNMADE


def test_structural_locator_fails_closed_without_both_anchors():
    data = bytearray((ROOT / "t61-auto" / SAUROMATIA).read_bytes())
    states = T.read_item_states(bytes(data))
    assert states is not None
    # Locate the unique array by its exact content and corrupt its trailer.
    start = bytes(data).find(bytes((value & 0xFF) for value in states))
    assert start >= 4
    data[start + T.ITEM_STATE_COUNT:start + T.ITEM_STATE_COUNT + 4] = b"BAD!"
    assert T.read_item_states(bytes(data)) is None
