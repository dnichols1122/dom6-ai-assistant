"""The local `.2h` state transition made by Dominions' End Turn action."""
from pathlib import Path

import pytest

from dom6_assistant.file_reader.formats import h2 as H2


CONTROLS = Path("knowledge/snapshots/example_game")


def _control(name: str) -> bytes:
    path = CONTROLS / name / "mid_ermor.2h"
    if not path.exists():
        pytest.skip(f"submission control absent: {path}")
    return path.read_bytes()


def test_mark_submitted_reproduces_the_game_authored_semantic_bytes():
    unfinished = _control("t12-auto")
    submitted = _control("t12-auto-2")
    offsets = H2.submission_flag_offsets(unfinished)

    assert offsets[0] == 30
    assert tuple(unfinished[offset] for offset in offsets) == (1, 1)
    assert tuple(submitted[offset] for offset in offsets) == (0, 0)
    assert H2.turn_is_submitted(unfinished) is False
    assert H2.turn_is_submitted(submitted) is True

    generated = H2.mark_turn_submitted(unfinished)
    assert generated[:-2] == submitted[:-2]
    assert generated[-2:] == unfinished[-2:]  # preserve the stale trailer
    assert [i for i, (a, b) in enumerate(zip(unfinished, generated)) if a != b] \
        == list(offsets)


def test_mark_submitted_is_idempotent():
    submitted = _control("t12-auto-2")
    assert H2.mark_turn_submitted(submitted) == submitted


def test_submission_refuses_a_mixed_flag_pair():
    unfinished = bytearray(_control("t12-auto"))
    _, treasury_flag = H2.submission_flag_offsets(unfinished)
    unfinished[treasury_flag] = 0
    with pytest.raises(H2.SubmissionStateError, match="flags disagree"):
        H2.mark_turn_submitted(bytes(unfinished))


def test_submission_refuses_a_first_turn_bootstrap_file():
    path = Path("knowledge/snapshots/t1-preorders/mid_marignon.2h")
    if not path.exists():
        pytest.skip("first-turn bootstrap control absent")
    with pytest.raises(H2.SubmissionStateError, match="opened and saved"):
        H2.mark_turn_submitted(path.read_bytes())
