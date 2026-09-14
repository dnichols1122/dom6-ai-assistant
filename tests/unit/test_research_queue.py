"""Nation research-queue decoding and surgical `.2h` writes."""
from pathlib import Path

import pytest

from dom6_assistant.file_reader.formats import h2


SNAPSHOTS = Path("knowledge/snapshots")
LEVELS = [3, 0, 0, 1, 0, 1, 0]
PROGRESS = [9, 0, 0, 0, 0, 0, 0]


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ("t25-research-before", []),
        ("t25-research-alteration1", [1]),
        ("t25-research-alteration1-2", [1, 1]),
        ("t25-research-alteration1-2-evocation1", [1, 1, 2]),
        ("t25-research-full9", [1, 1, 2, 0, 3, 4, 5, 6, 0]),
        ("t25-research-conj4-8", [0, 0, 0, 0, 0]),
        ("t25-research-conj4-8-tartarian", [0, 0, 0, 0, 0, 1080]),
        ("t25-research-conj4-8-tartarian-ghostriders",
         [0, 0, 0, 0, 0, 1080, 1078]),
    ],
)
def test_reads_each_observed_research_queue(snapshot, expected):
    data = (SNAPSHOTS / snapshot / "mid_marignon.2h").read_bytes()
    assert h2.read_research_queue(data, LEVELS, PROGRESS) == expected


def test_writer_changes_only_the_queue_and_first_terminator():
    path = SNAPSHOTS / "t25-research-before" / "mid_marignon.2h"
    original = path.read_bytes()
    queue = [1, 1, 2, 0, 3, 4, 5, 6, 0]
    written = h2.set_research_queue(original, queue, LEVELS, PROGRESS)
    start = h2.research_queue_offset(original, LEVELS, PROGRESS)

    assert h2.read_research_queue(written, LEVELS, PROGRESS) == queue
    changed = {i for i, (old, new) in enumerate(zip(original, written))
               if old != new}
    assert changed
    assert changed <= set(range(start, start + 2 * (len(queue) + 1)))


def test_shorter_queue_is_terminated_before_stale_entries():
    path = SNAPSHOTS / "t25-research-full9" / "mid_marignon.2h"
    original = path.read_bytes()
    written = h2.set_research_queue(original, [4], LEVELS, PROGRESS)
    assert h2.read_research_queue(written, LEVELS, PROGRESS) == [4]


def test_research_writer_refuses_capacity_and_invalid_targets():
    data = (SNAPSHOTS / "t25-research-before" / "mid_marignon.2h").read_bytes()
    with pytest.raises(ValueError, match="at most 9"):
        h2.set_research_queue(data, [0] * 10, LEVELS, PROGRESS)
    with pytest.raises(ValueError, match="range 0-5000"):
        h2.set_research_queue(data, [5001], LEVELS, PROGRESS)


def test_stale_research_state_refuses_instead_of_guessing_an_offset():
    data = (SNAPSHOTS / "t25-research-before" / "mid_marignon.2h").read_bytes()
    stale_levels = [2, 0, 0, 1, 0, 1, 0]
    with pytest.raises(h2.ResearchQueueNotLocated, match="occurs 0 times"):
        h2.read_research_queue(data, stale_levels, PROGRESS)
