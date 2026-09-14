from __future__ import annotations

import http.client

import pytest

from dom6_assistant.agent import llm


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return b'{"success":true}'


def test_local_koboldcpp_abort_uses_its_documented_endpoint(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_method(), timeout))
        return _Response()

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    client = llm.OpenAICompatClient(base_url="http://127.0.0.1:5001/v1")
    assert client.abort() is True
    assert seen == [("http://127.0.0.1:5001/api/extra/abort", "POST", 3)]


def test_remote_openai_compatible_endpoint_is_not_probed_with_kobold_api(
        monkeypatch):
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("called")),
    )
    client = llm.OpenAICompatClient(base_url="https://example.test/v1")
    assert client.abort() is False



class _SSEResponse:
    """An SSE body, iterated line by line the way urlopen yields it."""

    def __init__(self, lines):
        self._lines = [line.encode() for line in lines]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def __iter__(self):
        return iter(self._lines)


def _sse(*objects):
    import json as _json
    return [f"data: {_json.dumps(o)}\n" for o in objects] + ["data: [DONE]\n"]


def _stream_client(monkeypatch, lines):
    client = llm.OpenAICompatClient(base_url="http://x/v1", model="m")
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda request, timeout=None: _SSEResponse(lines))
    return client


def _drain(stream):
    """Deltas plus the generator's return value."""
    deltas = []
    while True:
        try:
            deltas.append(next(stream))
        except StopIteration as done:
            return deltas, done.value


def test_stream_open_retries_twice_before_succeeding(monkeypatch):
    attempts = 0
    lines = _sse(
        {"choices": [{"delta": {"content": "recovered"},
                      "finish_reason": "stop"}]},
    )

    def urlopen(request, timeout=None):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise http.client.RemoteDisconnected(
                "remote closed before sending response headers")
        return _SSEResponse(lines)

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    client = llm.OpenAICompatClient(
        base_url="http://x/v1", model="m", timeout=600)

    _, reply = _drain(client.stream_complete(
        [{"role": "user", "content": "x"}]))

    assert attempts == 3
    assert reply.text == "recovered"


@pytest.mark.parametrize("failure", [
    ConnectionResetError("reset"),
    BrokenPipeError("broken pipe"),
])
def test_other_pre_response_connection_failures_are_retried(
        monkeypatch, failure):
    attempts = 0
    lines = _sse(
        {"choices": [{"delta": {"content": "ok"},
                      "finish_reason": "stop"}]},
    )

    def urlopen(request, timeout=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise failure
        return _SSEResponse(lines)

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    client = llm.OpenAICompatClient(base_url="http://x/v1", model="m")

    _, reply = _drain(client.stream_complete(
        [{"role": "user", "content": "x"}]))

    assert attempts == 2
    assert reply.text == "ok"


def test_pre_response_retry_exhaustion_has_a_clear_error(monkeypatch):
    attempts = 0

    def urlopen(request, timeout=None):
        nonlocal attempts
        attempts += 1
        raise http.client.RemoteDisconnected("gone")

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    client = llm.OpenAICompatClient(base_url="http://x/v1", model="m")

    with pytest.raises(llm.LLMError, match="before a response after 2 retries"):
        _drain(client.stream_complete([{"role": "user", "content": "x"}]))
    assert attempts == 3


def test_streamed_tool_arguments_are_reassembled_before_the_call_is_made(
        monkeypatch):
    """Observed live from llama.cpp: arguments arrive as '{', '"n', '":', '1',
    '}'. Acting on each fragment would call the tool five times with rubbish.
    """
    lines = _sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "_probe",
                                                  "arguments": "{"}}]}}]},
        *[{"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": part}}]}}]}
          for part in ('"n', '":', "1", "}")],
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"completion_tokens": 53}},
    )
    client = _stream_client(monkeypatch, lines)
    deltas, reply = _drain(client.stream_complete([{"role": "user", "content": "x"}]))

    assert deltas == [], "a tool call must not be yielded as a delta"
    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert call.name == "_probe" and call.arguments == {"n": 1}
    assert call.malformed is None
    assert reply.finish_reason == "tool_calls"
    assert reply.completion_tokens == 53


def test_streamed_text_and_reasoning_arrive_on_separate_channels(monkeypatch):
    lines = _sse(
        {"choices": [{"delta": {"reasoning_content": "let me "}}]},
        {"choices": [{"delta": {"reasoning_content": "think"}}]},
        {"choices": [{"delta": {"content": "Turn "}}]},
        {"choices": [{"delta": {"content": "23."}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    client = _stream_client(monkeypatch, lines)
    deltas, reply = _drain(client.stream_complete([{"role": "user", "content": "x"}]))

    assert deltas == [("reasoning", "let me "), ("reasoning", "think"),
                      ("text", "Turn "), ("text", "23.")]
    assert reply.text == "Turn 23."
    assert reply.reasoning == "let me think"
    assert reply.tool_calls == []


def test_a_stream_cut_mid_arguments_yields_no_usable_call(monkeypatch):
    """Half a call is not a call. The fragment comes back marked, exactly as
    the non-streaming path hands back arguments it could not read."""
    lines = _sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1",
             "function": {"name": "get_province", "arguments": '{"province_'}}]}}]},
    )
    client = _stream_client(monkeypatch, lines)
    _, reply = _drain(client.stream_complete([{"role": "user", "content": "x"}]))

    call = reply.tool_calls[0]
    assert call.arguments == {}
    assert call.malformed and "not valid JSON" in call.malformed


def test_parallel_streamed_calls_stay_separate(monkeypatch):
    lines = _sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "one", "arguments": '{"x":'}},
            {"index": 1, "id": "b", "function": {"name": "two", "arguments": '{"y":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "1}"}},
            {"index": 1, "function": {"arguments": "2}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    client = _stream_client(monkeypatch, lines)
    _, reply = _drain(client.stream_complete([{"role": "user", "content": "x"}]))

    assert [(c.name, c.arguments) for c in reply.tool_calls] == [
        ("one", {"x": 1}), ("two", {"y": 2})]
