"""The agent loop, driven by a scripted model rather than a real one.

A fake client makes the loop's own behaviour testable: that it stops, that it
hands errors back instead of swallowing them, that read-only really is
read-only. None of that can be checked against a live model, where a passing
run might just be a model that happened to behave.
"""
import shutil
import threading
from pathlib import Path

import pytest

from dom6_assistant.agent.llm import LLMError, ModelReply, ToolCall
from dom6_assistant.agent.loop import SYSTEM_PROMPT, Event, TurnAgent
from dom6_assistant.agent.profiles import Profile
from dom6_assistant.agent.session import open_session

SAVE = Path.home() / ".dominions6/savedgames/example_game"
GAME_DB = Path("knowledge/game.sqlite3")


class FakeClient:
    """Replays a scripted list of replies, recording what it was sent."""

    def __init__(self, replies, native=True):
        self.replies = list(replies)
        self.native = native
        self.seen: list[list[dict]] = []
        self.tools_offered = None

    def supports_tools(self):
        return self.native

    def complete(self, messages, tools=None, max_tokens=1200,
                 temperature=None):
        self.seen.append(list(messages))
        self.tools_offered = tools
        self.last_sampling = (max_tokens, temperature)
        if not self.replies:
            return ModelReply(text="done", finish_reason="stop")
        return self.replies.pop(0)


def call(name, args=None, cid="c1"):
    return ModelReply(tool_calls=[ToolCall(id=cid, name=name,
                                           arguments=args or {})],
                      finish_reason="tool_calls")


def text(s):
    return ModelReply(text=s, finish_reason="stop")


@pytest.fixture
def session(tmp_path):
    if not SAVE.exists() or not GAME_DB.exists():
        pytest.skip("live save or game database absent")
    save = tmp_path / "save"
    save.mkdir()
    for name in ("mid_marignon.trn", "mid_marignon.2h"):
        shutil.copy2(SAVE / name, save / name)
    db = tmp_path / "game.sqlite3"
    shutil.copy2(GAME_DB, db)
    s = open_session(game_db=db, save_dir=save)
    yield s
    s.close()


def kinds(events):
    return [e.kind for e in events]


def test_a_plain_answer_ends_the_loop(session):
    agent = TurnAgent(session, FakeClient([text("We should defend.")]))
    events = list(agent.run("what now?"))
    assert kinds(events) == ["done"]
    assert events[0].text == "We should defend."


def test_character_persona_is_layered_and_post_history_stays_near_request(session):
    client = FakeClient([text("In character.")])
    agent = TurnAgent(
        session, client,
        persona_prompt="USER-SELECTED CHARACTER PERSONA\nTemperament: bold {but careful}",
        post_history_prompt="Answer in a terse royal voice.",
    )
    list(agent.run("Choose.", history=[{"role": "user", "content": "Earlier."}]))
    sent = client.seen[0]
    assert "Temperament: bold {but careful}" in sent[0]["content"]
    assert sent[-3] == {"role": "user", "content": "Earlier."}
    assert sent[-2]["role"] == "system"
    assert "terse royal voice" in sent[-2]["content"]
    assert sent[-1] == {"role": "user", "content": "Choose."}


def test_the_answer_is_not_emitted_twice(session):
    """A reply with no tool calls must produce exactly one piece of text.

    The first live run printed every answer twice, because the loop yielded
    both a `text` and a `done` event carrying the same string.
    """
    agent = TurnAgent(session, FakeClient([text("Only once.")]))
    bodies = [e.text for e in agent.run("q") if e.text]
    assert bodies == ["Only once."]


def test_an_empty_reply_is_an_error_not_a_finished_turn(session):
    """Seen for real: a partial tool call the server could not parse.

    The model returned five tokens, no tool_calls and no content. Treating that
    as a completed turn ended the run silently with nothing recorded, which in
    a log is indistinguishable from success.
    """
    empty = ModelReply(text="", tool_calls=[], finish_reason="tool_calls")
    events = list(TurnAgent(session, FakeClient([empty])).run("q"))
    assert events[-1].kind == "error"
    assert "neither text nor a tool call" in events[-1].text


def test_whitespace_only_counts_as_empty(session):
    events = list(TurnAgent(session, FakeClient([text("   \n ")])).run("q"))
    assert events[-1].kind == "error"


def test_a_tool_call_is_executed_and_reported(session):
    agent = TurnAgent(session, FakeClient([
        call("get_turn_summary"), text("Turn 23.")]))
    events = list(agent.run("what turn?"))
    assert kinds(events) == ["tool_call", "tool_result", "done"]
    assert events[1].result["ok"]
    assert events[1].result["result"]["turn"] == session.turn


def test_narration_alongside_a_call_is_shown(session):
    reply = call("get_turn_summary")
    reply.text = "Let me check the turn."
    agent = TurnAgent(session, FakeClient([reply, text("Done.")]))
    events = list(agent.run("q"))
    assert kinds(events) == ["text", "tool_call", "tool_result", "done"]


def test_a_refused_call_is_handed_back_rather_than_raised(session):
    """The model must see its own mistake and get another turn."""
    agent = TurnAgent(session, FakeClient([
        call("get_province", {"province_id": 99999}), text("Ah, no such one.")]))
    events = list(agent.run("q"))
    result = next(e for e in events if e.kind == "tool_result").result
    assert result["ok"] is False and result["kind"] == "bad_call"
    assert kinds(events)[-1] == "done"


def test_a_bad_call_repeated_stops_without_a_third_completion(session):
    client = FakeClient([
        call("get_province", {}),
        call("get_province", {}),
        text("must never consume a third completion"),
    ])
    events = list(TurnAgent(session, client, max_steps=10).run("q"))
    assert len(client.seen) == 2
    # The retry used to be capped at 800 to keep it cheap. An unused ceiling
    # costs nothing, and the cap truncated models that reason before calling.
    assert client.last_sampling[0] == TurnAgent(session, client).profile.max_tokens
    assert [event.kind for event in events].count("tool_result") == 2
    assert events[-1].kind == "error"
    assert "same argument error twice" in events[-1].text


def test_the_answer_after_a_refusal_keeps_the_full_budget(session):
    client = FakeClient([
        call("get_province", {}),
        text("I should probably try something else eventually."),
    ])
    agent = TurnAgent(session, client)
    events = list(agent.run("q"))
    assert client.last_sampling[0] == agent.profile.max_tokens
    assert events[-1].kind == "done"


def test_the_refusal_text_reaches_the_model(session):
    client = FakeClient([call("get_province", {"province_id": 99999}),
                         text("ok")])
    agent = TurnAgent(session, client)
    list(agent.run("q"))
    last = client.seen[-1]
    assert any(m.get("role") == "tool" and "no province 99999" in m["content"]
               for m in last)


def test_malformed_arguments_are_reported_not_repaired(session):
    bad = ModelReply(tool_calls=[ToolCall(
        id="c1", name="get_province", arguments={},
        malformed="arguments were not valid JSON")], finish_reason="tool_calls")
    agent = TurnAgent(session, FakeClient([bad, text("retrying")]))
    events = list(agent.run("q"))
    result = next(e for e in events if e.kind == "tool_result").result
    assert result["ok"] is False
    assert "could not read the arguments" in result["error"]


def test_the_step_budget_stops_a_model_that_loops(session):
    """A local model that has lost the thread will call the same tool forever."""
    client = FakeClient([call("get_turn_summary") for _ in range(50)])
    agent = TurnAgent(session, client, max_steps=3)
    events = list(agent.run("q"))
    assert events[-1].kind == "error"
    assert "stopped after 3 steps" in events[-1].text
    assert kinds(events).count("tool_call") == 3


def test_a_repeated_read_is_answered_from_cache_with_a_note(session):
    """The first live run called list_units twice on the same province.

    Each step costs tens of seconds on a local model, so a repeat gets the
    same answer plus a note saying it was a repeat — silently returning the
    cached value would leave the model no wiser about why it is not making
    progress.
    """
    client = FakeClient([call("get_turn_summary"), call("get_turn_summary"),
                         text("done")])
    events = list(TurnAgent(session, client).run("q"))
    results = [e.result for e in events if e.kind == "tool_result"]
    assert len(results) == 2
    assert "note" not in results[0]
    assert "already called this" in results[1]["note"]
    assert results[1]["result"] == results[0]["result"]


def test_a_repeated_write_is_not_cached(session):
    """Re-recording an order is how a decision gets changed."""
    agent = TurnAgent(session, FakeClient([
        call("record_order", {"commander_id": 308, "order": "move",
                              "destination": 86, "rationale": "first"}),
        call("record_order", {"commander_id": 308, "order": "move",
                              "destination": 98, "rationale": "changed mind"}),
        text("done")]), allow_writes=True)
    events = list(agent.run("q"))
    results = [e.result for e in events if e.kind == "tool_result"]
    assert all(r["ok"] for r in results)
    assert all("note" not in r for r in results)
    orders = session.call("get_orders")["result"]
    row = next(o for o in orders if o["commander_id"] == 308)
    assert row["destination"] == 98


def test_a_failed_read_is_not_cached(session):
    """A refusal may be fixed by the state changing; do not pin it."""
    client = FakeClient([call("get_province", {"province_id": 99999}),
                         call("get_province", {"province_id": 99999}),
                         text("done")])
    events = list(TurnAgent(session, client).run("q"))
    results = [e.result for e in events if e.kind == "tool_result"]
    assert all(not r["ok"] for r in results)
    assert all("note" not in r for r in results)


def test_read_only_mode_removes_the_write_tools(session):
    agent = TurnAgent(session, FakeClient([text("ok")]), allow_writes=False)
    assert "record_order" not in agent.registry
    assert "get_turn_summary" in agent.registry


def test_read_only_mode_refuses_a_write_even_if_the_model_tries(session):
    """Removal, not instruction — an instruction is a suggestion to a 4B model."""
    agent = TurnAgent(session, FakeClient([
        call("record_order", {"commander_id": 308, "order": "defend",
                              "rationale": "x"}),
        text("blocked")]), allow_writes=False)
    events = list(agent.run("q"))
    result = next(e for e in events if e.kind == "tool_result").result
    assert result["ok"] is False and "no tool named" in result["error"]


def test_writes_are_available_when_allowed(session):
    agent = TurnAgent(session, FakeClient([text("ok")]), allow_writes=True)
    assert "record_order" in agent.registry


def test_an_unreachable_model_is_an_event_not_a_crash(session):
    class Dead(FakeClient):
        def complete(self, *a, **k):
            raise LLMError("cannot reach endpoint")

    agent = TurnAgent(session, Dead([]))
    events = list(agent.run("q"))
    assert events[-1].kind == "error" and "cannot reach" in events[-1].text


def test_a_cancelled_run_never_contacts_the_model(session):
    cancelled = threading.Event()
    cancelled.set()
    client = FakeClient([text("must not be used")])
    events = list(TurnAgent(
        session, client, cancel_event=cancelled).run("q"))
    assert kinds(events) == ["cancelled"]
    assert client.seen == []


def test_cancellation_after_completion_discards_its_tool_call(session):
    cancelled = threading.Event()

    class CancellingClient(FakeClient):
        def complete(self, *args, **kwargs):
            reply = super().complete(*args, **kwargs)
            cancelled.set()
            return reply

    client = CancellingClient([call("get_turn_summary")])
    events = list(TurnAgent(
        session, client, cancel_event=cancelled).run("q"))
    assert kinds(events) == ["cancelled"]
    assert not any(event.kind == "tool_call" for event in events)


def test_tool_schemas_are_offered_when_calling_natively(session):
    client = FakeClient([text("ok")])
    list(TurnAgent(session, client).run("q"))
    names = {t["function"]["name"] for t in client.tools_offered}
    assert "get_turn_summary" in names and "record_order" in names


def test_text_protocol_is_used_when_native_is_unavailable(session):
    """No schemas offered; the tool list goes into the system prompt instead."""
    client = FakeClient([text('{"tool": "get_turn_summary", "args": {}}'),
                         text("Turn 23.")], native=False)
    agent = TurnAgent(session, client)
    events = list(agent.run("q"))
    assert client.tools_offered is None
    assert "Available tools:" in client.seen[0][0]["content"]
    assert kinds(events) == ["tool_call", "tool_result", "done"]


def test_text_protocol_does_not_echo_the_call_as_narration(session):
    """The whole reply is the JSON object; showing it is showing the plumbing."""
    client = FakeClient([text('{"tool": "get_turn_summary", "args": {}}'),
                         text("ok")], native=False)
    events = list(TurnAgent(session, client).run("q"))
    assert "text" not in kinds(events)


def test_text_protocol_keeps_real_prose_around_a_call(session):
    client = FakeClient([
        text('Let me check first.\n{"tool": "get_turn_summary", "args": {}}'),
        text("ok")], native=False)
    events = list(TurnAgent(session, client).run("q"))
    narration = [e.text for e in events if e.kind == "text"]
    assert narration == ["Let me check first."]


def test_structured_server_call_is_honoured_even_when_text_mode_was_forced(session):
    """Koboldcpp may parse Gemma's native marker despite omitted schemas."""
    reply = call("get_turn_summary")
    reply.text = (
        "I will inspect the turn.`<|tool_call>"
        "call:get_turn_summary{}<|tool_call|>`"
    )
    client = FakeClient([reply, text("Now I can answer.")], native=False)
    events = list(TurnAgent(
        session, client, force_text_protocol=True,
    ).run("q"))

    assert kinds(events) == ["text", "tool_call", "tool_result", "done"]
    assert events[1].tool == "get_turn_summary"
    assert events[2].result["ok"] is True
    assert events[-1].text == "Now I can answer."
    assert any(
        message["role"] == "user" and "Result of get_turn_summary" in message["content"]
        for message in client.seen[-1]
    )


def test_forced_text_history_records_every_structured_server_call(session):
    """A server may recognize several calls even with schemas omitted."""
    reply = ModelReply(tool_calls=[
        ToolCall(id="summary", name="get_turn_summary", arguments={}),
        ToolCall(id="messages", name="get_turn_messages", arguments={}),
    ], finish_reason="tool_calls")
    client = FakeClient([reply, text("Done.")], native=False)

    events = list(TurnAgent(
        session, client, force_text_protocol=True,
    ).run("q"))

    assert [event.tool for event in events if event.kind == "tool_call"] == [
        "get_turn_summary", "get_turn_messages",
    ]
    assistant = next(
        message for message in client.seen[-1]
        if message["role"] == "assistant"
    )
    assert '"tool": "get_turn_summary"' in assistant["content"]
    assert '"tool": "get_turn_messages"' in assistant["content"]


def test_leaked_native_marker_is_not_mistaken_for_a_final_answer(session):
    """Without structured tool_calls, ask for valid JSON and keep going."""
    leaked = text(
        "I will inspect the turn.`<|tool_call>"
        "call:get_turn_summary{}<|tool_call|>`"
    )
    client = FakeClient([
        leaked,
        text('{"tool": "get_turn_summary", "args": {}}'),
        text("Now I can answer."),
    ], native=False)
    events = list(TurnAgent(
        session, client, force_text_protocol=True,
    ).run("q"))

    results = [event for event in events if event.kind == "tool_result"]
    assert results[0].tool == "(unparsed)"
    assert results[0].result["ok"] is False
    assert any(event.tool == "get_turn_summary" for event in events)
    assert events[-1].kind == "done"
    assert events[-1].text == "Now I can answer."


def test_a_malformed_call_is_reported_and_the_loop_continues(session):
    """One missing colon must not end the turn as though it were an answer.

    Observed live: {"tool": "list_commanders", "args {}} — the model was
    mid-thought and waiting for a result, and the run stopped there having
    done nothing.
    """
    broken = 'I will check.\n{"tool": "list_commanders", "args {}}'
    client = FakeClient([text(broken),
                         text('{"tool": "get_turn_summary", "args": {}}'),
                         text("done")], native=False)
    events = list(TurnAgent(session, client).run("q"))
    kinds_seen = kinds(events)
    assert "tool_result" in kinds_seen
    first = next(e for e in events if e.kind == "tool_result").result
    assert first["ok"] is False and "not valid JSON" in first["error"]
    # ...and it went on to make a real call rather than stopping.
    assert any(e.kind == "tool_call" for e in events)
    assert kinds_seen[-1] == "done"


def test_plain_prose_still_ends_the_turn(session):
    """The detector must not mistake an answer for a broken call."""
    client = FakeClient([text("We should defend everywhere.")], native=False)
    events = list(TurnAgent(session, client).run("q"))
    assert kinds(events) == ["done"]


def test_invented_results_after_a_call_are_not_kept(session):
    """A live run fabricated a province table before calling any tool.

    "Hestia", "Ithaca", "Juno", "Kalliope" with invented production and income,
    when the real provinces are Marignon, Copper Canyons and The Obsidian
    Waste. Everything after a tool call is the model guessing at a result it
    has not been given; storing it puts fiction beside fact in the context it
    reasons from next.
    """
    fabricated = (
        'I will check the turn.\n'
        '{"tool": "get_turn_summary", "args": {}}\n'
        'Result: {"provinces": [{"name": "Hestia", "income": 6}, '
        '{"name": "Ithaca", "income": 8}]}\n'
        'So we hold Hestia and Ithaca.')
    client = FakeClient([text(fabricated), text("done")], native=False)
    list(TurnAgent(session, client).run("q"))
    stored = "".join(m["content"] for m in client.seen[-1]
                     if m["role"] == "assistant")
    assert "Hestia" not in stored and "Ithaca" not in stored
    assert "I will check the turn." in stored


def test_the_real_result_is_still_delivered(session):
    """Truncating the model's guess must not drop the actual answer."""
    client = FakeClient([
        text('checking\n{"tool": "get_turn_summary", "args": {}}\nfake result'),
        text("done")], native=False)
    list(TurnAgent(session, client).run("q"))
    assert any(m["role"] == "user" and "Result of get_turn_summary" in m["content"]
               for m in client.seen[-1])


def test_text_protocol_results_come_back_as_user_messages(session):
    """There is no `tool` role to use when the endpoint has no tool support."""
    client = FakeClient([text('{"tool": "get_turn_summary", "args": {}}'),
                         text("ok")], native=False)
    list(TurnAgent(session, client).run("q"))
    assert any(m["role"] == "user" and "Result of get_turn_summary" in m["content"]
               for m in client.seen[-1])


def test_event_render_truncates_unless_asked_not_to():
    big = {"ok": True, "tool": "t", "result": {"x": "y" * 500}}
    event = Event("tool_result", tool="t", result=big)
    assert "…" in event.render()
    assert "…" not in event.render(full=True)
def test_a_retry_gets_the_whole_configured_budget(session):
    """max_tokens is a ceiling, not an allowance.

    The loop used to cap a retry at 800 on the theory that re-sending a
    corrected call is cheap. It saves nothing -- an unused ceiling costs no
    tokens -- and it caused the very failure it was meant to make cheap.
    Live koboldcpp: `Generated:3009/5000` reached the call, the call failed,
    and the retry ran `Generated:800/800` and was cut off. The profile's
    number is the number, whether the last reply was long or short.
    """
    for used in (3009, 120):
        failed = ModelReply(
            text="", finish_reason="tool_calls", completion_tokens=used,
            tool_calls=[ToolCall(id="c1", name="get_province", arguments={})])
        client = FakeClient(
            [failed, ModelReply(text="ok", finish_reason="stop")])
        agent = TurnAgent(session, client)
        list(agent.run("q"))

        budget, _ = client.last_sampling
        assert budget == agent.profile.max_tokens, (
            f"after a reply of {used} tokens the retry got {budget}, "
            f"not the configured {agent.profile.max_tokens}")


def test_a_failed_call_records_the_replys_shape_for_diagnosis(session):
    """Intermittent failures are only diagnosable from what the reply looked
    like -- how it finished, how much it generated, what actually arrived."""
    bad = ModelReply(
        text="", finish_reason="tool_calls", completion_tokens=3009,
        tool_calls=[ToolCall(id="c1", name="get_province", arguments={})])
    agent = TurnAgent(session, FakeClient([bad]))
    failure = next(e for e in agent.run("q") if e.kind == "tool_result").result

    assert failure["finish_reason"] == "tool_calls"
    assert failure["completion_tokens"] == 3009
    assert failure["arguments_received"] == {}


def test_a_tool_call_cut_off_at_the_token_limit_is_named_as_truncation(session):
    """A reply that runs out of tokens mid-arguments must not be reported to
    the model as bad arguments. Told only "requires page_id", a model that
    believes it sent page_id sends the identical call again."""
    truncated = ModelReply(
        text="", finish_reason="length",
        tool_calls=[ToolCall(id="c1", name="get_province", arguments={})])
    agent = TurnAgent(session, FakeClient([truncated]))
    results = [e for e in agent.run("q") if e.kind == "tool_result"]

    assert results, "the truncated call produced no result at all"
    assert "cut off" in results[0].result["error"]


def test_a_cut_off_reply_never_performs_a_write(session):
    """A reply that stopped mid-sentence cannot express a complete intent."""
    write_tool = next((t for t in session.registry if t.writes), None)
    if write_tool is None:
        pytest.skip("no write tool registered")
    truncated = ModelReply(
        text="", finish_reason="length",
        tool_calls=[ToolCall(id="c1", name=write_tool.name, arguments={})])
    agent = TurnAgent(session, FakeClient([truncated]), allow_writes=True)
    results = [e for e in agent.run("q") if e.kind == "tool_result"]

    assert results and not results[0].result["ok"]
    assert "refused" in results[0].result["error"]


def test_an_ordinary_argument_error_is_still_reported_as_one(session):
    """Truncation handling must not blur a genuine mistake."""
    bad = ModelReply(
        text="", finish_reason="tool_calls",
        tool_calls=[ToolCall(id="c1", name="get_province", arguments={})])
    agent = TurnAgent(session, FakeClient([bad]))
    results = [e for e in agent.run("q") if e.kind == "tool_result"]

    error = results[0].result["error"]
    assert "requires province_id" in error and "cut off" not in error


def test_a_cut_off_answer_is_not_presented_as_a_whole_one(session):
    truncated = ModelReply(text="The best move is to", finish_reason="length")
    agent = TurnAgent(session, FakeClient([truncated]))
    events = list(agent.run("q"))

    assert "error" in [e.kind for e in events] and events[-1].kind == "done"
    notice = next(e for e in events if e.kind == "error")
    assert "cut off" in notice.text and "max_tokens" in notice.text
    assert events[-1].text == "The best move is to"


class StreamingClient(FakeClient):
    """A client whose completions arrive in pieces, like a real one."""

    def stream_complete(self, messages, tools=None, max_tokens=1200,
                        temperature=None):
        self.seen.append(list(messages))
        self.last_sampling = (max_tokens, temperature)
        reply = (self.replies.pop(0) if self.replies
                 else ModelReply(text="done", finish_reason="stop"))
        for piece in (reply.reasoning or ""):
            yield "reasoning", piece
        for piece in (reply.text or ""):
            yield "text", piece
        return reply


def test_streaming_shows_the_reply_as_it_arrives(session):
    reply = ModelReply(text="Turn 23.", reasoning="hm", finish_reason="stop")
    agent = TurnAgent(session, StreamingClient([reply]), stream=True)
    events = list(agent.run("q"))

    deltas = [e for e in events if e.kind == "delta"]
    assert "".join(e.text for e in deltas if e.channel == "reasoning") == "hm"
    assert "".join(e.text for e in deltas if e.channel == "text") == "Turn 23."
    # The whole answer still arrives once, so the transcript keeps a message
    # rather than a pile of fragments.
    assert events[-1].kind == "done" and events[-1].text == "Turn 23."
    # ...and the reasoning is not repeated wholesale after being streamed.
    assert not [e for e in events if e.kind == "reasoning"]


def test_streaming_still_runs_tools_only_once_complete(session):
    reply = ModelReply(
        text="", reasoning="deciding", finish_reason="tool_calls",
        tool_calls=[ToolCall(id="c1", name="get_turn_summary", arguments={})])
    client = StreamingClient([reply, ModelReply(text="ok", finish_reason="stop")])
    agent = TurnAgent(session, client, stream=True)
    events = list(agent.run("q"))

    calls = [e for e in events if e.kind == "tool_call"]
    assert len(calls) == 1 and calls[0].tool == "get_turn_summary"
    # The call is not emitted as deltas, only the prose around it.
    assert all(e.channel in ("reasoning", "text") for e in events if e.kind == "delta")


def test_streaming_tool_step_emits_authoritative_parsed_narration(session):
    reply = ModelReply(
        text="I will inspect the turn.", reasoning="deciding",
        finish_reason="tool_calls",
        tool_calls=[ToolCall(id="c1", name="get_turn_summary", arguments={})],
    )
    client = StreamingClient([reply, ModelReply(text="done", finish_reason="stop")])
    events = list(TurnAgent(session, client, stream=True).run("q"))

    # The raw delta is provisional and may contain a prose-protocol call. The
    # UI discards it at tool_call, so genuine narration must also arrive as an
    # authoritative text event for Activity.
    assert [event.text for event in events if event.kind == "text"] == [
        "I will inspect the turn."
    ]


def test_without_the_flag_nothing_streams(session):
    """The default path must be untouched."""
    reply = ModelReply(text="plain", reasoning="thought", finish_reason="stop")
    agent = TurnAgent(session, FakeClient([reply]))
    events = list(agent.run("q"))

    assert not [e for e in events if e.kind == "delta"]
    assert [e.kind for e in events if e.kind == "reasoning"] == ["reasoning"]
    assert events[-1].text == "plain"


def test_system_prompt_requires_incremental_not_repeated_thinking():
    assert SYSTEM_PROMPT.startswith("<|think|>\n")
    assert "Continue from the existing tool trace" in SYSTEM_PROMPT
    assert "at most two short sentences" in SYSTEM_PROMPT


def test_tool_call_reasoning_is_preserved_in_model_history(session):
    reply = ModelReply(
        reasoning="I need the current turn before deciding.",
        finish_reason="tool_calls",
        tool_calls=[ToolCall(id="c1", name="get_turn_summary", arguments={})],
    )
    client = FakeClient([reply, ModelReply(text="done", finish_reason="stop")])
    profile = Profile(
        reasoning_start="<|channel>thought",
        reasoning_end="<channel|>",
        preserve_tool_reasoning=True,
    )

    list(TurnAgent(session, client, profile=profile).run("q"))

    assistant = next(
        message for message in client.seen[1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant["content"] == (
        "<|channel>thought\nI need the current turn before deciding.<channel|>"
    )


def test_tool_call_reasoning_retention_can_be_disabled(session):
    reply = ModelReply(
        reasoning="Do not retain this.",
        finish_reason="tool_calls",
        tool_calls=[ToolCall(id="c1", name="get_turn_summary", arguments={})],
    )
    client = FakeClient([reply, ModelReply(text="done", finish_reason="stop")])

    list(TurnAgent(
        session, client, profile=Profile(preserve_tool_reasoning=False),
    ).run("q"))

    assistant = next(
        message for message in client.seen[1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant["content"] == ""


def test_text_protocol_also_preserves_tool_call_reasoning(session):
    reply = ModelReply(
        reasoning="Continue from the latest result.",
        finish_reason="tool_calls",
        tool_calls=[ToolCall(id="c1", name="get_turn_summary", arguments={})],
    )
    client = FakeClient(
        [reply, ModelReply(text="done", finish_reason="stop")], native=False,
    )
    profile = Profile(
        reasoning_start="<|channel>thought",
        reasoning_end="<channel|>",
        preserve_tool_reasoning=True,
    )

    list(TurnAgent(session, client, profile=profile).run("q"))

    assistant = next(
        message for message in client.seen[1]
        if message.get("role") == "assistant"
    )
    assert assistant["content"].startswith(
        "<|channel>thought\nContinue from the latest result.<channel|>\n"
    )
    assert '"tool": "get_turn_summary"' in assistant["content"]
