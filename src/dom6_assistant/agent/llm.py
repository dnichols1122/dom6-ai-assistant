"""Talking to an OpenAI-compatible endpoint, with or without tool calling.

Uses `urllib` from the standard library rather than `httpx` or the `openai`
package. This project is offline-first and the transport is one JSON POST, so a
dependency would buy nothing and cost the ability to run on a machine that has
never seen an index.

**Two protocols, one interface.** The endpoint may or may not implement
function calling. Probed against the local koboldcpp running
gemma-4-26B-A4B, native calling works fully: it returns proper `tool_calls`
with `finish_reason: "tool_calls"`, accepts a `role: "tool"` message carrying
the result, and emits correctly typed arguments on the next step.

Where it is unavailable the same registry is rendered as prose and the model is
asked to emit a JSON object; `parse_text_call` reads it back. The fallback is
strictly worse — it costs context and a model can wander off protocol — so it
is used only when native calling is absent, and never silently: `mode` records
which path was taken.

**Nothing is repaired.** Malformed arguments come back to the model as an
error, in both protocols, for the reason set out in `registry`: the model that
sent `{province_id: 93}` needs to learn that, and a parser that fixes it
teaches nothing while occasionally guessing wrong.
"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Generator, Iterator

DEFAULT_BASE_URL = "http://localhost:5001/v1"
DEFAULT_TIMEOUT = 300
PRE_RESPONSE_RETRIES = 2


class LLMError(RuntimeError):
    """The endpoint could not be reached or returned something unusable."""


def _open_model_response(request: urllib.request.Request,
                         timeout: int | None):
    """Open one model response, retrying only pre-response disconnects.

    Every retry creates a fresh HTTP connection.  Nothing from the model has
    been received at this point, so the agent cannot have interpreted a reply
    or executed a tool call.  Once a response object exists, failures are left
    to the caller: replaying a partially received stream could duplicate work
    or splice two different completions together.
    """
    for attempt in range(PRE_RESPONSE_RETRIES + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except (http.client.RemoteDisconnected, ConnectionResetError,
                BrokenPipeError) as exc:
            if attempt == PRE_RESPONSE_RETRIES:
                raise LLMError(
                    "model connection closed before a response after "
                    f"{PRE_RESPONSE_RETRIES} retries: {exc}"
                ) from exc
    raise AssertionError("unreachable")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    #: Set when the model emitted arguments that were not valid JSON. Carried
    #: rather than raised so the loop can hand the model its own mistake.
    malformed: str | None = None


@dataclass
class ModelReply:
    text: str = ""
    #: Chain-of-thought, when the endpoint exposes it separately. Reasoning
    #: models served over the OpenAI-compatible API return it as
    #: `reasoning_content` (DeepSeek, vLLM, koboldcpp) or `reasoning`, beside
    #: `content` rather than inside it. Captured because with native tool
    #: calling `content` is usually empty — the model's entire output is the
    #: call — so without this there is nothing to show a watching human at all.
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    #: Tokens the endpoint reports it generated. Used to size a retry: a model
    #: that needed 3000 tokens to reach its call cannot repeat it in 800.
    completion_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _post(url: str, payload: dict, api_key: str | None,
          timeout: int | None) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})})
    try:
        with _open_model_response(req, timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise LLMError(f"{exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(
            f"cannot reach {url}: {exc.reason}. Is the model server running?"
        ) from exc
    except TimeoutError as exc:
        deadline = f" after {timeout} seconds" if timeout is not None else ""
        raise LLMError(
            f"model request timed out{deadline}. Increase request timeout in "
            "the active Model profile, or set it to 0 for no deadline."
        ) from exc


class OpenAICompatClient:
    """An OpenAI-compatible chat endpoint: koboldcpp, vLLM, LM Studio, or the API."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 model: str | None = None,
                 api_key: str | None = None,
                 timeout: int | None = DEFAULT_TIMEOUT,
                 temperature: float = 0.3) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self._model = model
        self._supports_tools: bool | None = None

    # -- discovery -------------------------------------------------------

    def models(self, timeout: int = 15) -> list[str]:
        """Model ids the endpoint offers.

        `timeout` is short by default and shorter still from the web layer,
        because a local server answers this while it is generating — it
        serialises requests — and a page that asks which model is loaded must
        not hang for as long as the model happens to be busy.
        """
        req = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"}
            if self.api_key else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise LLMError(
                f"cannot list models at {self.base_url}: {exc}") from exc
        return [m["id"] for m in data.get("data", [])]

    def resolve_model(self, timeout: int = 5) -> str | None:
        """The model name without raising, for display.

        Returns None when the endpoint cannot be reached quickly. The caller
        wants something to put in a header, not a guarantee.
        """
        if self._model and self._model != "auto":
            return self._model
        try:
            found = self.models(timeout=timeout)
        except LLMError:
            return None
        if found:
            self._model = found[0]
        return self._model if self._model != "auto" else None

    @property
    def model(self) -> str:
        """The model to request, asking the endpoint when set to "auto".

        Auto-detection matters for local servers: the loaded GGUF changes and
        the config would otherwise have to change with it. It is also a guard
        against a quiet mismatch — koboldcpp accepts any model name and serves
        whatever it has loaded, so a stale config name is answered by a
        different model with no error anywhere.
        """
        if not self._model or self._model == "auto":
            found = self.models()
            if not found:
                raise LLMError(f"{self.base_url} lists no models")
            self._model = found[0]
        return self._model

    def supports_tools(self) -> bool:
        """Ask the endpoint for a tool call and see whether it makes one.

        A live probe rather than a version check, because the answer depends on
        the server build, the model, and the chat template together — and the
        one thing that reliably predicts whether tool calling will work is
        whether tool calling just worked.
        """
        if self._supports_tools is not None:
            return self._supports_tools
        probe = {
            "type": "function",
            "function": {
                "name": "_probe",
                "description": "Return the number 1.",
                "parameters": {"type": "object",
                               "properties": {"n": {"type": "integer",
                                                    "description": "the number 1"}},
                               "required": ["n"]},
            },
        }
        try:
            reply = self.complete(
                [{"role": "user", "content": "Call the _probe tool with n=1."}],
                tools=[probe], max_tokens=100)
            self._supports_tools = bool(reply.tool_calls)
        except LLMError:
            self._supports_tools = False
        return self._supports_tools

    # -- completion ------------------------------------------------------

    def abort(self) -> bool:
        """Ask a local KoboldCpp backend to stop its active generation.

        There is no standard OpenAI-compatible cancellation endpoint.  Do not
        probe arbitrary remote providers with a Kobold-specific request; the
        harness's cooperative cancellation still prevents any returned call
        from being executed there. KoboldCpp's local `/api/extra/abort` makes
        retrying immediately practical instead of waiting for discarded
        inference to finish.
        """
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return False
        root_path = parsed.path.rstrip("/")
        if root_path.endswith("/v1"):
            root_path = root_path[:-3]
        url = urllib.parse.urlunsplit((
            parsed.scheme, parsed.netloc,
            root_path + "/api/extra/abort", "", ""))
        request = urllib.request.Request(
            url, data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                response.read()
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict] | None = None,
                 max_tokens: int = 1200,
                 temperature: float | None = None) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": (self.temperature if temperature is None
                            else temperature),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = _post(f"{self.base_url}/chat/completions", payload,
                     self.api_key, self.timeout)
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"no choices in response: {str(data)[:300]}") from exc
        message = choice.get("message", {})
        calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                bad = None
                if not isinstance(args, dict):
                    args, bad = {}, f"arguments were {type(args).__name__}, not an object"
            except json.JSONDecodeError as exc:
                args, bad = {}, f"arguments were not valid JSON: {exc}"
            calls.append(ToolCall(id=tc.get("id", f"call_{len(calls)}"),
                                  name=fn.get("name", ""), arguments=args,
                                  malformed=bad))
        usage = data.get("usage") or {}
        return ModelReply(text=message.get("content") or "",
                          reasoning=(message.get("reasoning_content")
                                     or message.get("reasoning") or ""),
                          tool_calls=calls,
                          finish_reason=choice.get("finish_reason", ""),
                          completion_tokens=int(
                              usage.get("completion_tokens") or 0),
                          raw=data)

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1200,
        temperature: float | None = None,
    ) -> Generator[tuple[str, str], None, ModelReply]:
        """Yield ``(channel, text)`` deltas, returning the assembled reply.

        The same completion as :meth:`complete`, watched as it arrives. Callers
        take the deltas for display and the finished ``ModelReply`` from the
        generator's return value::

            stream = client.stream_complete(messages, tools)
            while True:
                try:
                    channel, text = next(stream)
                except StopIteration as done:
                    reply = done.value
                    break

        ``channel`` is ``"text"`` or ``"reasoning"``. Tool calls are **not**
        yielded: a call is only meaningful once whole, and its ``arguments``
        arrive as a string split across chunks at arbitrary points. They are
        accumulated by index and handed back with the final reply, so a stream
        that dies mid-call produces no call at all rather than half of one.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": (self.temperature if temperature is None
                            else temperature),
            "stream": True,
            # Needed for the token count the loop records on a failed call.
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        # index -> {"id":…, "name":…, "arguments": [fragments]}
        partial: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        completion_tokens = 0

        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"}
                        if self.api_key else {})})
        try:
            with _open_model_response(request, self.timeout) as resp:
                for raw in resp:
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        parsed = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    usage = parsed.get("usage") or {}
                    if usage.get("completion_tokens"):
                        completion_tokens = int(usage["completion_tokens"])
                    choices = parsed.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}

                    piece = delta.get("content")
                    if piece:
                        text_parts.append(piece)
                        yield "text", piece
                    thought = (delta.get("reasoning_content")
                               or delta.get("reasoning"))
                    if thought:
                        reasoning_parts.append(thought)
                        yield "reasoning", thought

                    for call in delta.get("tool_calls") or []:
                        index = int(call.get("index", 0))
                        slot = partial.setdefault(
                            index, {"id": "", "name": "", "arguments": []})
                        if call.get("id"):
                            slot["id"] = call["id"]
                        fn = call.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"].append(fn["arguments"])
        except urllib.error.URLError as exc:
            raise LLMError(f"stream failed: {exc}") from exc

        calls: list[ToolCall] = []
        for index in sorted(partial):
            slot = partial[index]
            raw_args = "".join(slot["arguments"]) or "{}"
            bad: str | None = None
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args, bad = {}, (f"arguments were {type(args).__name__}, "
                                     "not an object")
            except json.JSONDecodeError as exc:
                # A stream cut mid-arguments lands here. Handing back the
                # fragment rather than repairing it keeps the same contract as
                # the non-streaming path: the model is told, not corrected.
                args, bad = {}, f"arguments were not valid JSON: {exc}"
            calls.append(ToolCall(id=slot["id"] or f"call_{index}",
                                  name=slot["name"], arguments=args,
                                  malformed=bad))

        return ModelReply(
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=calls,
            finish_reason=finish_reason,
            completion_tokens=completion_tokens,
        )

    def stream(self, messages: list[dict[str, Any]],
               max_tokens: int = 1200) -> Iterator[str]:
        """Token deltas, for the chat UI. Tools are not streamed.

        Tool calls arrive as a unit and are useless half-arrived, so the loop
        uses `complete` for any step that may call one and streams only the
        final prose answer.
        """
        payload = {"model": self.model, "messages": messages,
                   "max_tokens": max_tokens, "temperature": self.temperature,
                   "stream": True}
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"}
                        if self.api_key else {})})
        try:
            with _open_model_response(req, self.timeout) as resp:
                for raw in resp:
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        return
                    try:
                        delta = (json.loads(chunk)["choices"][0]
                                 .get("delta", {}).get("content"))
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        yield delta
        except urllib.error.URLError as exc:
            raise LLMError(f"stream failed: {exc}") from exc


def client_from_config(conf: dict[str, Any] | None = None) -> OpenAICompatClient:
    """Build a client from the project config's `[model.openai]` section."""
    from dom6_assistant import config as cfg
    conf = conf or cfg.load()
    bcfg = cfg.backend_config(conf, "openai")
    configured_timeout = int(bcfg.get("timeout_seconds", DEFAULT_TIMEOUT))
    return OpenAICompatClient(
        base_url=bcfg.get("base_url", DEFAULT_BASE_URL),
        model=bcfg.get("model"),
        api_key=cfg.resolve_api_key(bcfg),
        timeout=None if configured_timeout == 0 else configured_timeout,
    )
