"""The web layer's own logic, tested without an HTTP client or a model.

Starlette's TestClient needs httpx, which this project deliberately does not
depend on, so the route functions are called directly and the SSE bridge is
driven with a fake agent. That is where the risk actually is: the bridge runs a
blocking generator on a worker thread and pumps it through an asyncio queue, and
a mistake there shows up as a page that hangs rather than an exception anywhere.
"""
import asyncio
import base64
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dom6_assistant.agent.loop import Event
from dom6_assistant.agent.llm import ModelReply, ToolCall
from dom6_assistant.agent.profiles import Profile
from dom6_assistant.agent.registry import ToolRegistry
from dom6_assistant.web.routes import agent as agent_routes
from dom6_assistant.web.routes.agent import ChatRequest, _stream


class FakeAgent:
    def __init__(self, events, raises=None):
        self._events = events
        self._raises = raises
        self.called_with = None

    def run(self, prompt, history=None):
        self.called_with = (prompt, history)
        for event in self._events:
            yield event
        if self._raises:
            raise self._raises


def drain(agent, prompt="q", history=None):
    async def go():
        return [chunk async for chunk in _stream(agent, prompt, history or [])]
    return asyncio.run(go())


def payloads(chunks):
    out = []
    for chunk in chunks:
        data = chunk["data"]
        if data == "[DONE]":
            continue
        out.append(json.loads(data))
    return out


def test_events_reach_the_client_in_order():
    events = [
        Event("tool_call", tool="get_turn_summary", args={}),
        Event("tool_result", tool="get_turn_summary",
              result={"ok": True, "result": {"turn": 23}}),
        Event("done", text="Turn 23."),
    ]
    got = payloads(drain(FakeAgent(events)))
    assert [e["kind"] for e in got] == ["tool_call", "tool_result", "done"]
    assert got[1]["result"]["result"]["turn"] == 23
    assert got[2]["text"] == "Turn 23."


def test_the_stream_is_terminated():
    chunks = drain(FakeAgent([Event("done", text="ok")]))
    assert chunks[-1]["data"] == "[DONE]"


def test_tool_results_are_sent_whole():
    """Truncating here would hide exactly the detail worth checking."""
    big = {"ok": True, "result": {"names": [f"unit {i}" for i in range(60)]}}
    got = payloads(drain(FakeAgent([Event("tool_result", tool="t", result=big)])))
    assert len(got[0]["result"]["result"]["names"]) == 60


def test_a_crash_in_the_loop_becomes_an_error_event():
    """A page that hangs is worse than one that says what went wrong."""
    agent = FakeAgent([Event("text", text="thinking")],
                      raises=RuntimeError("model exploded"))
    got = payloads(drain(agent))
    assert got[-1]["kind"] == "error" and "model exploded" in got[-1]["text"]


def test_a_crash_still_terminates_the_stream():
    agent = FakeAgent([], raises=RuntimeError("boom"))
    assert drain(agent)[-1]["data"] == "[DONE]"


def test_history_is_passed_through():
    history = [{"role": "user", "content": "earlier"}]
    agent = FakeAgent([Event("done", text="ok")])
    drain(agent, "now", history)
    assert agent.called_with == ("now", history)


def test_an_empty_run_still_completes():
    assert drain(FakeAgent([]))[-1]["data"] == "[DONE]"


@pytest.mark.parametrize("kind", ["text", "tool_call", "tool_result", "error",
                                  "cancelled", "done"])
def test_every_event_kind_serialises(kind):
    got = payloads(drain(FakeAgent([Event(kind, text="x", tool="t")])))
    assert got[0]["kind"] == kind


def test_chat_response_uses_fetch_friendly_sse_separator(monkeypatch):
    """The original page buffered CRLF events forever and showed no answer."""
    session = SimpleNamespace(
        ctx=SimpleNamespace(game_db=None, nation_id=1),
        turn=1,
        registry=ToolRegistry(),
        close=lambda: None,
    )
    monkeypatch.setattr(agent_routes, "_session", lambda game=None: session)
    monkeypatch.setattr(agent_routes, "client_from_config", lambda: object())
    monkeypatch.setattr(agent_routes.profiles, "active_profile",
                        lambda db: Profile())

    response = asyncio.run(agent_routes.chat(ChatRequest(message="hello")))
    assert response.sep == "\n"


def test_chat_session_and_tool_calls_share_the_stream_worker(monkeypatch,
                                                             tmp_path):
    """Regression: request-thread connections made every agent tool fail."""
    database = tmp_path / "game.sqlite3"
    setup = sqlite3.connect(database)
    setup.execute("CREATE TABLE state (value INTEGER NOT NULL)")
    setup.execute("INSERT INTO state VALUES (30)")
    setup.commit()
    setup.close()

    registry = ToolRegistry()

    @registry.tool("read_state", "Read a value through the game database.")
    def read_state(ctx):
        return {"value": ctx.game_db.execute(
            "SELECT value FROM state").fetchone()[0]}

    closed = []

    def open_worker_session(game=None):
        conn = sqlite3.connect(database)
        ctx = SimpleNamespace(game_db=conn, nation_id=1)
        return SimpleNamespace(
            ctx=ctx,
            turn=30,
            registry=registry,
            close=lambda: (conn.close(), closed.append(True)),
        )

    class ToolClient:
        def __init__(self):
            self.replies = [
                ModelReply(
                    tool_calls=[ToolCall(
                        id="call-1", name="read_state", arguments={})],
                    finish_reason="tool_calls",
                ),
                ModelReply(text="Turn 30.", finish_reason="stop"),
            ]

        def supports_tools(self):
            return True

        def complete(self, messages, tools=None, max_tokens=1200,
                     temperature=None):
            return self.replies.pop(0)

    monkeypatch.setattr(agent_routes, "_session", open_worker_session)
    monkeypatch.setattr(agent_routes.profiles, "active_profile",
                        lambda db: Profile())
    agent = agent_routes._SessionBoundAgent(
        ChatRequest(message="read it"), ToolClient())

    got = payloads(drain(agent, "read it"))
    tool_result = next(item for item in got if item["kind"] == "tool_result")
    assert tool_result["result"]["ok"]
    assert tool_result["result"]["result"] == {"value": 30}
    assert got[-1]["kind"] == "done"
    assert got[-1]["text"] == "Turn 30."
    assert closed == [True]


def test_legacy_think_aloud_request_does_not_force_text_tools(monkeypatch):
    """Old saved browser state must not override the model profile protocol."""
    captured = {}
    closed = []
    context = SimpleNamespace(
        player_persona="", lore_before="", persona_prompt="",
        lore_after="", post_history_prompt="",
    )
    connection = sqlite3.connect(":memory:")
    session = SimpleNamespace(
        ctx=SimpleNamespace(
            game_db=connection, nation_id=43,
            game_id="legacy-control-test",
        ),
        close=lambda: closed.append(True),
    )

    class CapturingAgent:
        def __init__(self, session_arg, client_arg, **kwargs):
            del session_arg, client_arg
            captured.update(kwargs)

        def run(self, prompt, history=None):
            del prompt, history
            yield Event("done", text="ok")

    monkeypatch.setattr(agent_routes, "_session", lambda game=None: session)
    monkeypatch.setattr(agent_routes, "TurnAgent", CapturingAgent)
    monkeypatch.setattr(agent_routes.profiles, "active_profile",
                        lambda db: Profile(tool_mode="native"))
    monkeypatch.setattr(agent_routes.character_cards, "active_context",
                        lambda *args, **kwargs: context)

    body = ChatRequest(message="q", think_aloud=True)
    events = list(agent_routes._SessionBoundAgent(body, object()).run("q"))

    assert "force_text_protocol" not in captured
    assert captured["profile"].tool_mode == "native"
    assert events[-1].text == "ok"
    assert closed == [True]
    connection.close()


def test_agent_page_persists_and_restores_chat():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "localStorage.setItem(chatKey()" in page
    assert "localStorage.getItem(chatKey()" in page
    assert "dom6-pretender-chat-v1-" in page
    assert "dom6-agent-design-nation" in page
    assert "restoreChat();" in page
    assert "replace(/\\r\\n/g, '\\n')" in page
    # Each named chat caches under its own key, so switching does not overwrite
    # the chat that was open a moment ago.
    assert "workspaceChatKey()+'-conversation-'+activeConversationId" in page


def test_agent_page_remembers_controls_per_conversation_workspace():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    # Write permission belongs to the workspace, not to one chat inside it:
    # opening a second chat about the same nation must not silently re-arm it.
    assert "function controlKey(){ return workspaceChatKey()+'-controls'; }" in page
    assert "allow_writes:$('#w').checked" in page
    assert "autonomous:$('#auto').checked" in page
    assert "think_aloud:$('#th').checked" in page
    assert "max_steps:budget" in page
    assert 'id="max-steps" type="number" min="1" max="500"' in page
    assert "max_steps:Math.max(1,Math.min(500,parseInt(maxSteps.value,10)||24))" in page
    assert "function restoreControls()" in page
    assert "restoreControls(); loadState(); loadOrders();" in page
    assert "document.body.classList.toggle('hide-thinking', !$('#th').checked)" in page
    assert "$('#th').onchange = () => { updateThinkingVisibility(); saveControls(); };" in page
    # Think aloud is display-only; it must never alter the agent request.
    request_start = page.index("const request = {message:text")
    request_end = page.index("if(!isPretender()", request_start)
    assert "think_aloud" not in page[request_start:request_end]
    assert "maxSteps.onchange = saveControls" in page


def test_agent_progress_is_rendered_in_chat_not_only_activity():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "bubble('progress', ev.text, true, exchange)" in page
    assert "activityNote(ev.text, true, exchange)" not in page
    assert "'user','bot','think','progress','err','cancelled'" in page


def test_web_step_budgets_are_bounded_and_customizable():
    assert agent_routes.ChatRequest(message="q").max_steps == 24
    assert agent_routes.ChatRequest(message="q", max_steps=120).max_steps == 120
    assert agent_routes.OpenChatRequest(message="q").max_steps == 16
    assert agent_routes.PretenderChatRequest(message="q", nation_id=43).max_steps == 24
    with pytest.raises(ValueError):
        agent_routes.ChatRequest(message="q", max_steps=0)
    with pytest.raises(ValueError):
        agent_routes.ChatRequest(message="q", max_steps=501)


def test_agent_page_retains_tool_evidence_and_separates_activity():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert 'id="p-activity"' in page
    assert "activity.appendChild(d)" in page
    assert "kind:'tool', name, args, result:null" in page
    assert "entry.result = result" in page
    assert "transcript:transcript.slice(-250)" in page
    assert "<summary><span>Used " in page
    assert "card.open = false" in page
    # Evidence persists in Activity, not in assistant-role model history.
    retain = page.partition("function retainRunForNextMessage()")[2].partition(
        "function retireStreamedStep()")[0]
    assert "Tool result from " not in retain
    assert "content:finalText" in retain
    assert 'id="auto"' in page
    assert "request.autonomous = $('#auto').checked" in page
    assert "always inject" in page
    assert "every model completion" in page
    assert "textarea.field{resize:vertical;min-height:72px;max-height:none}" in page


def test_agent_page_can_edit_delete_and_retry_messages_safely():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert 'id="retry-last"' in page
    assert "function normalizeExchangeIds()" in page
    assert "function editAndRetry(exchange, bubbleElement)" in page
    assert "function editResponse(exchange, bubbleElement)" in page
    assert "function deleteExchange(exchange)" in page
    assert "function deleteResponse(exchange)" in page
    assert "function retryExchange(exchange)" in page
    assert "role:message.role, content:message.content" in page
    assert "_exchange:exchange" in page
    assert "Tool result delivered to the assistant" in page


def test_active_run_can_be_cancelled_and_backend_abort_is_requested():
    backend_called = threading.Event()

    class Client:
        def abort(self):
            backend_called.set()
            return True

    run_id = "test-cancel-run"
    event = agent_routes._register_run(run_id, Client())
    try:
        result = agent_routes.cancel_run(
            agent_routes.CancelRunBody(run_id=run_id))
        assert result == {"run_id": run_id, "cancelled": True}
        assert event.is_set()
        assert backend_called.wait(1)
    finally:
        agent_routes._release_run(run_id, event)


def test_agent_page_has_stop_control_and_run_abort_plumbing():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert 'id="stop"' in page
    assert "new AbortController()" in page
    assert "'/api/agent/cancel'" in page
    assert "run_id:runId" in page
    assert "No later tool calls from it will be executed" in page


def test_model_editor_draft_survives_tab_switches():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "async function loadModel(force=false)" in page
    assert "if(!force && pane.dataset.loaded === 'true') return" in page
    assert "loadModel(true); loadState();" in page
    assert "if(r.ok) loadModel(true);" in page
    assert 'id="pf-timeout"' in page
    assert 'id="pf-preserve-tool-reasoning"' in page
    assert "preserve_tool_reasoning:$('#pf-preserve-tool-reasoning').checked" in page
    assert "Use 0 for no deadline" in page


def test_profile_timeout_is_applied_to_model_client():
    client = SimpleNamespace(timeout=300)
    agent_routes._apply_request_timeout(
        client, Profile(request_timeout=1800),
    )
    assert client.timeout == 1800
    agent_routes._apply_request_timeout(
        client, Profile(request_timeout=0),
    )
    assert client.timeout is None


def test_autonomous_pretender_continues_until_creation(monkeypatch):
    registry = ToolRegistry()
    created = []

    @registry.tool("inspect_candidate", "Inspect a candidate.")
    def inspect_candidate(ctx):
        del ctx
        return {"candidate": "checked"}

    @registry.tool("create_pretender", "Save the final design.", writes=True)
    def create_pretender(ctx):
        del ctx
        created.append(True)
        return {"saved_file": "/tmp/yomi_0.2h"}

    closed = []
    session = SimpleNamespace(
        ctx=SimpleNamespace(nation_id=23),
        turn=0,
        registry=registry,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(agent_routes, "open_pretender_session", lambda nation: session)
    monkeypatch.setattr(agent_routes.profiles, "active_profile",
                        lambda db: Profile(tool_mode="native"))

    class Client:
        def __init__(self):
            self.replies = [
                ModelReply(text="I should inspect a candidate.", finish_reason="stop"),
                ModelReply(tool_calls=[ToolCall(
                    id="inspect-1", name="inspect_candidate", arguments={},
                )], finish_reason="tool_calls"),
                ModelReply(tool_calls=[ToolCall(
                    id="create-1", name="create_pretender", arguments={},
                )], finish_reason="tool_calls"),
                ModelReply(text="The design is saved.", finish_reason="stop"),
            ]

        def complete(self, messages, tools=None, max_tokens=1200,
                     temperature=None):
            del messages, tools, max_tokens, temperature
            return self.replies.pop(0)

    body = agent_routes.PretenderChatRequest(
        message="Design and save a Yomi pretender.",
        nation_id=23,
        allow_writes=True,
        autonomous=True,
        max_steps=8,
    )
    events = list(agent_routes._SessionBoundPretenderAgent(body, Client()).run(
        body.message,
    ))

    assert created == [True]
    assert closed == [True]
    assert events[0].kind == "text"
    assert events[0].text == "I should inspect a candidate."
    assert any(event.tool == "inspect_candidate" for event in events)
    assert any(event.tool == "create_pretender" for event in events)
    assert events[-1].kind == "done"
    assert events[-1].text == "The design is saved."


def test_autonomous_pretender_requires_write_permission(monkeypatch):
    closed = []
    session = SimpleNamespace(
        ctx=SimpleNamespace(nation_id=23), turn=0, registry=ToolRegistry(),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(agent_routes, "open_pretender_session", lambda nation: session)
    monkeypatch.setattr(agent_routes.profiles, "active_profile", lambda db: Profile())
    body = agent_routes.PretenderChatRequest(
        message="Design it.", nation_id=23, allow_writes=False, autonomous=True,
    )
    events = list(agent_routes._SessionBoundPretenderAgent(body, object()).run(
        body.message,
    ))
    assert events[-1].kind == "error"
    assert "requires Allow writes" in events[-1].text
    assert closed == [True]


def test_pretender_workspace_exposes_nations_and_separate_tools():
    nations = agent_routes.pretender_nations()
    marignon = next(row for row in nations if row["id"] == 61)
    assert marignon["name"] == "Marignon"
    assert marignon["age"] == "MA"
    assert marignon["age_name"] == "Middle Age"
    assert {row["era"] for row in nations} == {1, 2, 3}
    assert not any(row["name"].startswith("nation_") for row in nations)

    state = agent_routes.pretender_state(61)
    assert state["nation"]["id"] == 61
    assert {tool["name"] for tool in state["tools"]} == {
        "get_pretender_rules",
        "list_pretender_chassis",
        "describe_pretender_chassis",
        "get_pretender_costs",
        "list_pretender_blessings",
        "evaluate_pretender",
        "create_pretender",
        "search_illwiki",
        "read_illwiki_page",
    }


def test_pretender_workspace_labels_nations_with_age_and_epithet():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "n.age + ' · ' + n.id + ' · ' + n.name" in page
    assert "n.epithet ? ' — ' + n.epithet" in page


def test_character_library_import_binding_and_preview(monkeypatch, tmp_path):
    database = tmp_path / "characters.sqlite3"
    monkeypatch.setattr(agent_routes, "DEFAULT_GAME_DB", database)
    raw = {
        "spec": "chara_card_v2", "spec_version": "2.0",
        "data": {
            "name": "Cautious Queen",
            "personality": "Conservative with irreplaceable commanders.",
            "creator_notes": "shown, not prompted",
            "character_book": {"entries": [{
                "keys": ["pretender"], "content": "Prefer resilient gods.",
                "comment": "pretender preference",
            }]},
        },
    }
    imported = agent_routes.import_character(agent_routes.CharacterImportBody(
        filename="queen.json",
        content_base64=base64.b64encode(json.dumps(raw).encode()).decode(),
    ))["card"]
    assert imported["name"] == "Cautious Queen"
    assert "source_blob" not in imported and "raw_json" not in imported

    binding = agent_routes.put_character_binding(
        agent_routes.CharacterBindingBody(
            scope_key="pretender:23", card_id=imported["id"],
            influence_mode="strategy_and_voice",
        ))["binding"]
    assert binding["revision_id"] == imported["revision_id"]

    preview = agent_routes.character_preview(
        "pretender:23", "Design a pretender", "[]")
    assert "Conservative with irreplaceable" in preview["context"]["persona_prompt"]
    assert "Prefer resilient gods" in preview["context"]["lore_after"]
    assert "shown, not prompted" not in preview["context"]["persona_prompt"]


def test_character_panel_is_present_and_accepts_json_and_png():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert 'data-p="character"' in page
    assert 'accept=".json,.png,application/json,image/png"' in page
    assert "Exact next-invocation preview" in page


def test_conversation_routes_keep_several_chats_per_workspace(monkeypatch,
                                                              tmp_path):
    """One chat per nation was the limit; a workspace now holds a library."""
    monkeypatch.setattr(agent_routes, "DEFAULT_GAME_DB",
                        tmp_path / "conversations.sqlite3")
    assert agent_routes.list_conversations("pretender:23")["conversations"] == []

    first = agent_routes.create_conversation(agent_routes.ConversationCreateBody(
        scope_key="pretender:23", title="Awake Oni"))["conversation"]
    second = agent_routes.create_conversation(agent_routes.ConversationCreateBody(
        scope_key="pretender:23", title="Dormant Bishamon",
        state={"history": [{"role": "user", "content": "compare these"}],
               "transcript": [{"kind": "user", "text": "compare these"}]},
    ))["conversation"]
    assert first["id"] != second["id"]
    assert {row["title"] for row
            in agent_routes.list_conversations("pretender:23")["conversations"]
            } == {"Awake Oni", "Dormant Bishamon"}

    # A second nation is a separate workspace, not more of the same list.
    agent_routes.create_conversation(agent_routes.ConversationCreateBody(
        scope_key="pretender:61", title="Ulm"))
    assert len(agent_routes.list_conversations(
        "pretender:23")["conversations"]) == 2

    reopened = agent_routes.get_conversation(
        second["id"], "pretender:23")["conversation"]
    assert reopened["state"]["history"][0]["content"] == "compare these"


def test_a_conversation_is_renamed_and_saved_without_touching_its_neighbour(
        monkeypatch, tmp_path):
    monkeypatch.setattr(agent_routes, "DEFAULT_GAME_DB",
                        tmp_path / "conversations.sqlite3")
    keep = agent_routes.create_conversation(agent_routes.ConversationCreateBody(
        scope_key="turn:g1", title="Keep me"))["conversation"]
    edit = agent_routes.create_conversation(agent_routes.ConversationCreateBody(
        scope_key="turn:g1", title="New chat"))["conversation"]

    renamed = agent_routes.save_conversation(edit["id"],
        agent_routes.ConversationSaveBody(
            scope_key="turn:g1", title="Turn 37 assault",
            state={"history": [{"role": "user", "content": "assault plan"}],
                   "transcript": []}))["conversation"]
    assert renamed["title"] == "Turn 37 assault"
    assert renamed["state"]["history"][0]["content"] == "assault plan"
    assert agent_routes.get_conversation(
        keep["id"], "turn:g1")["conversation"]["title"] == "Keep me"


def test_deleting_a_conversation_archives_it_rather_than_destroying_it(
        monkeypatch, tmp_path):
    """A long transcript is evidence; Delete removes it from the list only."""
    database = tmp_path / "conversations.sqlite3"
    monkeypatch.setattr(agent_routes, "DEFAULT_GAME_DB", database)
    made = agent_routes.create_conversation(agent_routes.ConversationCreateBody(
        scope_key="turn:g1", title="Mistaken",
        state={"history": [{"role": "user", "content": "worth keeping"}],
               "transcript": []}))["conversation"]

    assert agent_routes.delete_conversation(
        made["id"], "turn:g1") == {"archived": made["id"]}
    assert agent_routes.list_conversations("turn:g1")["conversations"] == []
    with pytest.raises(HTTPException) as gone:
        agent_routes.get_conversation(made["id"], "turn:g1")
    assert gone.value.status_code == 404

    db = sqlite3.connect(database)
    stored = db.execute(
        "SELECT archived, state_json FROM assistant_conversation WHERE id=?",
        (made["id"],)).fetchone()
    db.close()
    assert stored[0] == 1 and "worth keeping" in stored[1]


def test_a_conversation_cannot_be_reached_from_another_workspace(monkeypatch,
                                                                 tmp_path):
    monkeypatch.setattr(agent_routes, "DEFAULT_GAME_DB",
                        tmp_path / "conversations.sqlite3")
    made = agent_routes.create_conversation(agent_routes.ConversationCreateBody(
        scope_key="pretender:23", title="Yomi"))["conversation"]
    for call in (
        lambda: agent_routes.get_conversation(made["id"], "pretender:61"),
        lambda: agent_routes.save_conversation(made["id"],
            agent_routes.ConversationSaveBody(
                scope_key="pretender:61",
                state={"history": [], "transcript": []})),
        lambda: agent_routes.delete_conversation(made["id"], "pretender:61"),
    ):
        with pytest.raises(HTTPException) as refused:
            call()
        assert refused.value.status_code == 404
    with pytest.raises(HTTPException) as bad_scope:
        agent_routes.list_conversations("not-a-scope")
    assert bad_scope.value.status_code == 400


def test_agent_page_wires_the_conversation_library_controls():
    """The controls existed but did nothing: New and Rename had no handler."""
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "newChat.onclick=()=>{ void newConversation(); };" in page
    assert "renameChat.onclick=()=>{ void renameConversation(); };" in page
    assert "deleteChat.onclick=()=>{ void deleteConversation(); };" in page
    assert "chatSelect.onchange=async()=>{" in page
    assert "await openConversationLibrary(generation);" in page
    # The server copy is the archive and gets the whole conversation; only the
    # browser cache is trimmed to fit a per-origin quota.
    assert "function browserState(){" in page
    assert "return {history:history.slice(-40),transcript:transcript.slice(-250)};" in page
    assert "scheduleConversationSave(conversationState());" in page
    assert "'-chat-library-migrated'" in page


def test_agent_page_will_not_write_a_partial_copy_over_the_archive():
    """Autosave stops when the server copy could not be read back."""
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "if(!activeConversationId||!conversationLibraryReady) return;" in page
    assert "function flushConversationSave()" in page
    assert "await flushConversationSave();" in page
    assert "setSaveState('not saved')" in page


def test_agent_page_edits_a_message_in_place_rather_than_in_a_dialog():
    """prompt() could not be resized or scrolled, showed none of the
    surrounding conversation, and lost the text if dismissed by accident."""
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "function editInline(bubbleElement, current, label, onSave)" in page
    assert "class=\"msg-editor\"" in page or "'msg-editor'" in page
    assert "Ctrl+Enter saves" in page
    assert "event.key === 'Escape'" in page
    # The editor is seeded with the source text, so markdown stays editable as
    # markdown rather than as its rendering.
    assert "editor.value = current" in page
    # No message edit may go back to a browser dialog.
    edit_section = page[page.index("function editAndRetry"):
                        page.index("function deleteExchange")]
    assert "window.prompt" not in edit_section


def test_agent_page_renders_assistant_markdown_without_trusting_it():
    """The assistant writes **bold**, lists and tables and the transcript
    showed the punctuation. Rendering it must not hand untrusted wiki text a
    way to contribute markup."""
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "function renderMarkdown(raw)" in page
    assert "const MARKDOWN_KINDS = new Set(['bot', 'think'])" in page
    assert "body.innerHTML = renderMarkdown(text)" in page
    # Escaping happens before any markup is applied; that ordering is the
    # whole safety argument.
    assert "const lines = esc(String(raw == null ? '' : raw)).split('\\n')" in page
    # Links are restricted to http(s), so a javascript: URL never becomes one.
    assert "(https?:\\/\\/[^\\s)]+)" in page
    assert 'rel="noopener noreferrer"' in page
    # The user's own text is left exactly as typed.
    assert "body.textContent = text" in page


def test_open_chat_reaches_reference_data_and_nothing_else():
    """The third workspace has no game, so there is no visibility line to
    police -- the context simply carries nothing that could cross it."""
    from dom6_assistant.agent.open_tools import open_chat_session

    session = open_chat_session()
    try:
        fields = set(session.ctx.__dataclass_fields__)
        assert fields == {"reference_db", "wiki_db"}
        for forbidden in ("game_db", "save_dir", "view", "nation_id"):
            assert not hasattr(session.ctx, forbidden), forbidden
        assert not any(tool.writes for tool in session.registry)
        names = {tool.name for tool in session.registry}
        assert {"list_nations", "describe_nation", "lookup_unit"} <= names
        # No tool may take a nation/game escape hatch the way a turn tool would.
        for tool in session.registry:
            params = {p.name for p in tool.params}
            assert "game" not in params
    finally:
        session.close()


def test_open_chat_names_the_scale_limit_as_the_game_does():
    """EA Yomi's own nation screen reads "Turmoil limit +1"; a raw -1 on order
    would be the same fact in a form the model cannot quote back."""
    from dom6_assistant.agent.open_tools import open_chat_session

    session = open_chat_session()
    try:
        yomi = session.call("describe_nation", {"nation_id": 23})["result"]
        assert yomi["nation"]["name"] == "Yomi"
        assert yomi["scale_limit_modifiers"]["order"]["reads_as"] == "Turmoil limit +1"
        caelum = session.call("describe_nation", {"nation_id": 71})["result"]
        assert caelum["scale_limit_modifiers"]["heat"]["reads_as"] == "Cold limit +1"
    finally:
        session.close()


def test_the_open_workspace_is_offered_and_routed_in_the_page():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert '<option value="open">Open chat</option>' in page
    assert "function isOpenChat(){ return mode.value === 'open'; }" in page
    assert "'/api/agent/open/chat'" in page
    assert "'open:general'" in page
    # Nothing there writes, so the permission is hidden rather than offered.
    assert "$('#write-control').style.display = isOpenChat() ? 'none' : '';" in page


def test_the_page_streams_deltas_into_one_growing_bubble():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert 'id="stream"' in page
    assert "stream:$('#stream').checked" in page
    assert "ev.kind === 'delta'" in page
    # One bubble per channel, grown in place, not one per token.
    assert "const streaming = {think:null, bot:null};" in page
    assert "streaming[cls].dataset.raw" in page
    # The authoritative whole text still replaces the streamed answer.
    assert "streaming.bot.remove()" in page
    # Provisional text is retired at tool/error boundaries, so a run that ends
    # on a bad call cannot leave raw calls mixed into the answer pane.
    assert "function retireStreamedStep()" in page
    assert "else if(ev.kind === 'tool_call')       {\n      retireStreamedStep();" in page
    assert "else if(ev.kind === 'tool_result')     {\n      retireStreamedStep();" in page
    assert "else if(ev.kind === 'error')           {\n      retireStreamedStep();" in page


def test_tool_evidence_is_not_flattened_into_assistant_history():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    retain = page.partition("function retainRunForNextMessage()")[2].partition(
        "function retireStreamedStep()")[0]
    assert "runTrace" not in retain
    assert "Tool-assisted work retained" not in retain
    assert "content:finalText" in retain
    # Existing stored chats are migrated when restored/sent, not left poisoned.
    assert "saved.history.map(cleanHistoryMessage).filter(Boolean)" in page


def test_a_character_can_be_written_not_only_imported(monkeypatch, tmp_path):
    """SillyTavern lets you author a card; importing was the only way in here."""
    monkeypatch.setattr(agent_routes, "DEFAULT_GAME_DB",
                        tmp_path / "characters.sqlite3")
    made = agent_routes.create_character(agent_routes.AuthoredCardBody(
        name="Iron Prophet",
        description="Believes fortifications win wars.",
        personality="Patient, unromantic.",
        first_mes="Where are your walls?",
        tags=["defensive"],
        lore=[{"keys": ["siege"], "content": "Always values castle defence."}],
    ))["card"]
    assert made["spec"] == "chara_card_v3"
    assert made["lore_entries"] == 1
    # Source payloads are never handed back, authored or imported.
    assert "source_blob" not in made and "raw_json" not in made

    fields = agent_routes.character_fields(made["id"])["fields"]
    assert fields["name"] == "Iron Prophet"
    assert fields["lore"][0]["keys"] == ["siege"]

    fields["personality"] = "Patient. Now grim."
    updated = agent_routes.update_character(
        made["id"], agent_routes.AuthoredCardBody(**{
            k: v for k, v in fields.items()
            if k in agent_routes.AuthoredCardBody.model_fields}))["card"]
    assert updated["revision_id"]
    assert len(agent_routes.characters()["cards"]) == 1

    # An authored card binds and injects exactly as an imported one does.
    agent_routes.put_character_binding(agent_routes.CharacterBindingBody(
        scope_key="open:general", card_id=made["id"],
        influence_mode="strategy_and_voice"))
    preview = agent_routes.character_preview("open:general", "surviving a siege", "[]")
    assert "castle defence" in (preview["context"]["lore_after"] or "")


def test_a_missing_card_is_refused_rather_than_created(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_routes, "DEFAULT_GAME_DB",
                        tmp_path / "characters.sqlite3")
    with pytest.raises(HTTPException) as gone:
        agent_routes.character_fields(9999)
    assert gone.value.status_code == 404
    with pytest.raises(HTTPException) as refused:
        agent_routes.update_character(9999, agent_routes.AuthoredCardBody(name="x"))
    assert refused.value.status_code == 404


def test_the_character_editor_is_present_in_the_page():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "Write a character" in page
    assert "id=\"char-new\"" in page
    assert "'/api/agent/characters/create'" in page
    assert "function openEditor(cardId, fields)" in page
    assert "function collectLore()" in page
    # Editing an existing card saves a revision, and the button says so.
    assert "Save as new revision" in page


def test_the_persona_reaches_the_assembled_prompt(monkeypatch, tmp_path):
    """A persona nobody assembles into the prompt is decoration."""
    monkeypatch.setattr(agent_routes, "DEFAULT_GAME_DB",
                        tmp_path / "persona.sqlite3")
    card = agent_routes.create_character(agent_routes.AuthoredCardBody(
        name="Iron Prophet", personality="Addresses {{user}} bluntly."))["card"]
    agent_routes.put_character_binding(agent_routes.CharacterBindingBody(
        scope_key="open:general", card_id=card["id"],
        influence_mode="strategy_and_voice"))
    agent_routes.put_persona(agent_routes.PersonaBody(
        name="Tester", description="A cautious veteran of Early Age nations."))

    assert agent_routes.get_persona()["persona"]["name"] == "Tester"
    preview = agent_routes.character_preview("open:general", "siege advice", "[]")
    assert "Tester" in preview["context"]["persona_prompt"]
    assert "cautious veteran" in preview["context"]["player_persona"]

    # Every prompt assembly in the routes must carry it, not just one.
    source = Path(agent_routes.__file__).read_text()
    assemblies = source.count("persona_prompt,")
    assert source.count("player_persona,") == assemblies, (
        "a persona assembled in some code paths and not others would apply "
        "in one workspace and vanish in another")


def test_the_persona_editor_is_present_in_the_page():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "Your persona" in page
    assert 'id="me-name"' in page and 'id="me-desc"' in page
    assert "'/api/agent/persona'" in page
    assert "{{user}}" in page


def test_a_freshly_written_character_can_be_edited_and_archived(monkeypatch,
                                                                tmp_path):
    """The editor first gated Edit and Archive behind the *bound* card, so a
    character you had just written could be neither changed nor removed until
    you bound it to something."""
    monkeypatch.setattr(agent_routes, "DEFAULT_GAME_DB",
                        tmp_path / "unbound.sqlite3")
    made = agent_routes.create_character(
        agent_routes.AuthoredCardBody(name="Scratch", personality="draft"))["card"]
    keeper = agent_routes.create_character(
        agent_routes.AuthoredCardBody(name="Keeper"))["card"]

    # Nothing is bound anywhere.
    assert agent_routes.characters(
        scope_key="open:general")["binding"]["card_id"] is None

    fields = agent_routes.character_fields(made["id"])["fields"]
    fields["personality"] = "revised while unbound"
    agent_routes.update_character(made["id"], agent_routes.AuthoredCardBody(**{
        k: v for k, v in fields.items()
        if k in agent_routes.AuthoredCardBody.model_fields}))
    assert agent_routes.character_fields(
        made["id"])["fields"]["personality"] == "revised while unbound"

    agent_routes.archive_character(made["id"])
    remaining = [c["name"] for c in agent_routes.characters()["cards"]]
    assert remaining == ["Keeper"]
    assert keeper["id"]


def test_edit_and_archive_target_the_picker_not_the_binding():
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert 'id="char-edit"' in page and 'id="char-archive"' in page
    assert "const pickedCard=()=>{" in page
    # Both must read the picker at click time rather than closing over the
    # bound card, or an unbound character is unreachable again.
    assert "$('#char-edit').onclick=async()=>{\n      const card=pickedCard();" in page
    assert "$('#char-archive').onclick=async()=>{\n      const card=pickedCard();" in page
    # Exactly one archive handler; the bound-card-only one is gone.
    assert page.count("char-archive').onclick") == 1


def test_literal_character_paths_are_declared_before_the_parameterised_one():
    """A {card_id} route declared above /characters/binding captures the word
    "binding" and fails to parse it as an integer -- a 422 that looks like a
    bad request body rather than a routing mistake."""
    from dom6_assistant.web.routes.agent import router

    order = [(getattr(route, "path", ""), set(getattr(route, "methods", ())))
             for route in router.routes]

    def first(path_suffix, method):
        for index, (path, methods) in enumerate(order):
            if path.endswith(path_suffix) and method in methods:
                return index
        raise AssertionError(f"no {method} route ending {path_suffix}")

    # PUT specifically: the DELETE on the same template is declared later and
    # is not what swallowed the binding request.
    parameterised = first("/characters/{card_id}", "PUT")
    for literal, method in (("/characters/binding", "PUT"),
                            ("/characters/preview", "GET"),
                            ("/characters/import", "POST"),
                            ("/characters/create", "POST")):
        assert first(literal, method) < parameterised, (
            f"{literal} must be declared before PUT /characters/{{card_id}}")


def test_validation_errors_are_rendered_readably_in_the_page():
    """FastAPI reports a 422 as a list of objects; interpolating one gave
    "[object Object],[object Object]", which named neither field nor reason."""
    page = (Path(agent_routes.__file__).parent.parent / "static" /
            "agent.html").read_text()
    assert "function problemText(payload, fallback)" in page
    assert "Array.isArray(detail)" in page
    # No alert may fall back to interpolating a raw detail again.
    assert "result.detail||'Could not save binding'" not in page
    assert "result.detail||'Import failed'" not in page
