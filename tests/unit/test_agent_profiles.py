"""Model profiles: reasoning markers, tool mode, and prompt overrides.

The marker splitting carries the weight. Getting it wrong is not visible from
the outside — the run shows the whole chain of thought as if it were the
answer, or shows nothing at all — so the cases here are the real formats,
measured off endpoints rather than imagined.
"""
import sqlite3

import pytest

from dom6_assistant.agent import profiles as P

# Real output shapes. Gemma 4 uses the asymmetric <|channel> opener and
# <channel|> closer documented by Google.
DEEPSEEK = "<think>Weigh the options.</think>The answer is 42."
HARMONY = ("<|channel|>analysis<|message|>Consider it.<|end|>"
           "The answer is 42.")
GEMMA = "<|channel>thought\n\nWeigh the options.\n<channel|>The answer is 42."


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    P.seed_presets(conn)
    return conn


# -- splitting -------------------------------------------------------------

def test_no_start_marker_shows_everything():
    """A model that does not mark its reasoning must not lose its answer."""
    assert P.split_reasoning("Plain answer.", "", "") == ("Plain answer.", "")


def test_a_start_marker_that_never_appears_changes_nothing():
    assert P.split_reasoning("Plain answer.", "<think>", "</think>") == (
        "Plain answer.", "")


def test_deepseek_style_markers():
    visible, thinking = P.split_reasoning(DEEPSEEK, "<think>", "</think>")
    assert visible == "The answer is 42."
    assert thinking == "Weigh the options."


def test_harmony_style_markers():
    visible, thinking = P.split_reasoning(
        HARMONY, "<|channel|>analysis<|message|>", "<|end|>")
    assert visible == "The answer is 42."
    assert thinking == "Consider it."


def test_gemma_4_asymmetric_channel_markers():
    visible, thinking = P.split_reasoning(GEMMA, "<|channel>thought",
                                          "<channel|>")
    assert thinking == "Weigh the options."
    assert "The answer is 42." in visible


def test_an_open_ended_block_with_no_following_marker_takes_the_rest():
    visible, thinking = P.split_reasoning(
        "<|channel>thought\nStill thinking", "<|channel>thought", "")
    assert visible == "" and thinking == "Still thinking"


def test_text_before_the_first_marker_is_kept():
    visible, thinking = P.split_reasoning(
        "Preamble.<think>hmm</think>Answer.", "<think>", "</think>")
    assert "Preamble." in visible and "Answer." in visible
    assert thinking == "hmm"


def test_repeated_blocks_are_all_collected():
    text = "<think>one</think>A<think>two</think>B"
    visible, thinking = P.split_reasoning(text, "<think>", "</think>")
    assert thinking == "one\ntwo"
    assert visible == "A\nB"


# -- storage ---------------------------------------------------------------

def test_presets_are_seeded_and_one_is_active(db):
    names = {p.name for p in P.list_profiles(db)}
    assert {"default", "gemma-koboldcpp", "deepseek-r1", "anthropic"} <= names
    assert P.active_profile(db).name == "default"


def test_saving_then_activating_a_profile(db):
    P.save_profile(db, P.Profile(name="mine", reasoning_start="<r>",
                                 reasoning_end="</r>", tool_mode="text",
                                 temperature=0.8, max_tokens=999,
                                 request_timeout=1800,
                                 preserve_tool_reasoning=False))
    active = P.activate(db, "mine")
    assert active.name == "mine" and active.tool_mode == "text"
    assert active.temperature == 0.8 and active.max_tokens == 999
    assert active.request_timeout == 1800
    assert active.preserve_tool_reasoning is False


def test_only_one_profile_is_active_at_a_time(db):
    P.save_profile(db, P.Profile(name="mine"))
    P.activate(db, "mine")
    actives = [p for p in P.list_profiles(db) if p.is_active]
    assert len(actives) == 1 and actives[0].name == "mine"


def test_reseeding_does_not_revert_an_edited_preset(db):
    """A preset the user has tuned is theirs."""
    P.save_profile(db, P.Profile(name="deepseek-r1", temperature=0.15,
                                 reasoning_start="<think>",
                                 reasoning_end="</think>"))
    P.seed_presets(db)
    edited = next(p for p in P.list_profiles(db) if p.name == "deepseek-r1")
    assert edited.temperature == 0.15


def test_an_invalid_tool_mode_is_refused(db):
    with pytest.raises(ValueError, match="tool_mode"):
        P.save_profile(db, P.Profile(name="bad", tool_mode="telepathy"))


def test_negative_request_timeout_is_refused(db):
    with pytest.raises(ValueError, match="request_timeout"):
        P.save_profile(db, P.Profile(name="bad", request_timeout=-1))


def test_shipped_presets_cannot_be_deleted(db):
    with pytest.raises(ValueError, match="preset"):
        P.delete_profile(db, "default")


def test_deleting_the_active_profile_falls_back_to_default(db):
    P.save_profile(db, P.Profile(name="temp"))
    P.activate(db, "temp")
    P.delete_profile(db, "temp")
    assert P.active_profile(db).name == "default"


def test_activating_something_that_does_not_exist_is_refused(db):
    with pytest.raises(ValueError, match="no profile"):
        P.activate(db, "nonexistent")


def test_an_empty_system_prompt_means_use_the_built_in(db):
    """So the shipped prompt keeps improving instead of being frozen in a row."""
    assert all(p.system_prompt == "" for p in P.list_profiles(db))


def test_prompt_saves_are_revisioned_and_restorable(db):
    P.save_profile(db, P.Profile(
        name="mine", system_prompt="first", request_timeout=1200,
        preserve_tool_reasoning=False,
    ))
    first = P.profile_revisions(db, "mine")[0]
    P.save_profile(db, P.Profile(name="mine", system_prompt="second"))

    restored = P.restore_revision(db, "mine", first["id"])

    assert restored.system_prompt == "first"
    assert restored.request_timeout == 1200
    assert restored.preserve_tool_reasoning is False
    assert [row["system_prompt"] for row in P.profile_revisions(db, "mine")] == [
        "first", "second", "first"]
