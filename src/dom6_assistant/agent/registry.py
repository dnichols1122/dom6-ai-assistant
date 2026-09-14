"""Tool registry: declaration, validation, dispatch.

**Why validation is strict here rather than forgiving.** The model driving this
may be a 7B running locally, and such a model gets arguments wrong constantly —
a province name where an id belongs, a string "12" for an integer, a required
field simply missing. There are two ways to handle that, and the difference
decides whether the assistant is usable.

The forgiving way coerces: accept "12", look up the name, fill the default.
That works until the coercion is wrong, and then the model has issued an order
it did not intend against a province it did not name, and nothing anywhere
reports a problem. Dominions resolves the turn either way. The failure surfaces
several turns later as a position that makes no sense.

So tools refuse, and the refusal is written to be acted on. `ToolError`
messages name the parameter, say what arrived, say what was wanted, and where
there is an obvious next call, name it. A model that cannot parse prose can
still retry against "province_id must be an integer province id, got
'Marignon'; call find_province to look up an id by name".

That is the whole design: every error is a usable instruction, and no error is
ever resolved by guessing what was meant.

**Schemas serve two protocols.** `openai_schema()` emits the function-calling
format for models that support it natively; `text_protocol()` emits a compact
prose form for models that do not. Same registry, same validation, same
handlers — only the transport differs, so a model that fakes tool calls in
prose is held to exactly the same standard as one that emits JSON.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dom6_assistant.agent.visibility import PlayerView, VisibilityError


class ToolError(RuntimeError):
    """A tool call that could not be honoured, phrased so it can be retried.

    Distinct from VisibilityError, which means the question itself was not the
    player's to ask. This one means the question was fine and the arguments
    were not.
    """


@dataclass
class ToolContext:
    """Everything the tools are allowed to touch.

    Assembled once and passed to every handler, so the set of reachable
    resources is visible in one place. Notably absent: any path to `ftherlnd`.
    `view` is the only route to the save, and it refuses that file.
    """
    view: PlayerView
    game_db: sqlite3.Connection
    reference_db: sqlite3.Connection
    game_id: int
    save_dir: Path
    h2_path: Path | None = None

    @property
    def turn(self) -> int:
        return self.view.turn

    @property
    def nation_id(self) -> int:
        return self.view.nation_id


@dataclass
class Param:
    """One parameter of a tool.

    `hint` is appended to the error when validation fails. It is where a tool
    tells the model how to recover — usually by naming the call that produces a
    valid value for this parameter.
    """
    name: str
    type: str  # integer | string | boolean | object | array
    description: str
    required: bool = True
    default: Any = None
    hint: str = ""
    choices: tuple[str, ...] | None = None

    def validate(self, value: Any) -> Any:
        if self.choices and value not in self.choices:
            raise ToolError(
                f"{self.name} must be one of {list(self.choices)}, got "
                f"{value!r}.{' ' + self.hint if self.hint else ''}")
        if self.type == "integer":
            # A model that emits "12" for an integer is common and harmless to
            # accept; a model that emits "Marignon" is making a different
            # mistake entirely and must not be quietly rescued.
            if isinstance(value, bool):
                raise ToolError(f"{self.name} must be an integer, got a boolean.")
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                return int(value.strip())
            raise ToolError(
                f"{self.name} must be an integer, got {value!r}."
                f"{' ' + self.hint if self.hint else ''}")
        if self.type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"
            raise ToolError(f"{self.name} must be true or false, got {value!r}.")
        if self.type == "string":
            if isinstance(value, str):
                return value
            raise ToolError(f"{self.name} must be a string, got {value!r}.")
        if self.type == "object":
            if isinstance(value, dict):
                return value
            # Backward compatibility for calls recorded before structured
            # parameters were exposed. New schemas advertise a real object so
            # the model never has to quote JSON inside JSON.
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise ToolError(
                        f"{self.name} must be a JSON object, got an invalid "
                        f"legacy JSON string: {exc}.") from exc
                if isinstance(decoded, dict):
                    return decoded
            raise ToolError(f"{self.name} must be a JSON object, got {value!r}.")
        if self.type == "array":
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise ToolError(
                        f"{self.name} must be a JSON array, got an invalid "
                        f"legacy JSON string: {exc}.") from exc
                if isinstance(decoded, list):
                    return decoded
            raise ToolError(f"{self.name} must be a JSON array, got {value!r}.")
        raise ToolError(f"{self.name} has unknown type {self.type!r}")

    def json_schema(self) -> dict[str, Any]:
        s: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.type == "object":
            s["additionalProperties"] = {"type": "integer"}
        elif self.type == "array":
            s["items"] = {"type": "integer"}
        if self.choices:
            s["enum"] = list(self.choices)
        return s


@dataclass
class Tool:
    name: str
    description: str
    params: list[Param] = field(default_factory=list)
    handler: Callable[..., Any] | None = None
    writes: bool = False          # does this change stored state?

    def validate_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """Check arguments and return the cleaned set.

        Unknown arguments are an error rather than being dropped. A model that
        invented a parameter has misunderstood the tool, and silently ignoring
        the invention hides that from it — it will keep sending the argument
        and keep believing it did something.
        """
        if not isinstance(args, dict):
            raise ToolError(
                f"{self.name} takes a JSON object of arguments, got "
                f"{type(args).__name__}.")
        known = {p.name: p for p in self.params}
        unknown = set(args) - set(known)
        if unknown:
            raise ToolError(
                f"{self.name} has no parameter(s) {sorted(unknown)}. "
                f"Valid: {sorted(known) or 'none'}.")
        out: dict[str, Any] = {}
        for p in self.params:
            if p.name in args and args[p.name] is not None:
                out[p.name] = p.validate(args[p.name])
            elif p.required:
                raise ToolError(
                    f"{self.name} requires {p.name} ({p.description})."
                    f"{' ' + p.hint if p.hint else ''}")
            elif p.default is not None:
                out[p.name] = p.default
        return out

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {p.name: p.json_schema() for p in self.params},
                    "required": [p.name for p in self.params if p.required],
                },
            },
        }

    def signature(self) -> str:
        """One-line form for the text protocol."""
        args = ", ".join(
            f"{p.name}: {p.type}" + ("" if p.required else "?")
            for p in self.params)
        return f"{self.name}({args})"


class ToolRegistry:
    """The set of callable tools, and the only way to invoke one."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name!r}")
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: str, description: str, *params: Param,
             writes: bool = False) -> Callable:
        """Decorator form: @registry.tool("name", "desc", Param(...))"""
        def deco(fn: Callable) -> Callable:
            self.register(Tool(name=name, description=description,
                               params=list(params), handler=fn, writes=writes))
            return fn
        return deco

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(
                f"no tool named {name!r}. Available: {sorted(self._tools)}.")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, ctx: ToolContext, name: str,
             args: dict[str, Any] | None = None) -> Any:
        """Validate and dispatch. Errors are returned to the caller, not raised.

        Both ToolError and VisibilityError become ordinary results here, marked
        with `ok: False`, because both are things the model should see and
        respond to rather than crashes. A refusal is information: it tells the
        model the question was wrong, and it can ask a better one.
        """
        try:
            tool = self.get(name)
            clean = tool.validate_args(args or {})
            if tool.handler is None:
                raise ToolError(f"{name} has no handler")
            result = tool.handler(ctx, **clean)
            return {"ok": True, "tool": name, "result": result}
        except VisibilityError as exc:
            return {"ok": False, "tool": name, "error": str(exc),
                    "kind": "not_visible"}
        except ToolError as exc:
            return {"ok": False, "tool": name, "error": str(exc),
                    "kind": "bad_call"}
        except Exception as exc:                      # noqa: BLE001
            # Anything unexpected is still returned rather than raised: a
            # crashed tool should not end the turn, and the model can be told
            # the tool broke without the process dying.
            return {"ok": False, "tool": name,
                    "error": f"{type(exc).__name__}: {exc}", "kind": "failed"}

    # -- protocol rendering ----------------------------------------------

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self]

    def text_protocol(self) -> str:
        """Compact tool listing for models without native function calling.

        Deliberately terse. A local model's context is the scarce resource, and
        a verbose schema dump crowds out the game state it needs to reason
        about. Parameter detail lives in the error it gets back if it guesses
        wrong, which costs nothing until it is needed.
        """
        lines = ["Available tools:"]
        for t in sorted(self, key=lambda x: x.name):
            mark = "!" if t.writes else " "
            lines.append(f" {mark}{t.signature()} — {t.description}")
        lines.append("")
        lines.append(
            'To call one, emit exactly one JSON object on its own line:\n'
            '{"tool": "<name>", "args": {...}}\n'
            "Tools marked ! change stored state. Wait for the result before "
            "calling another.")
        return "\n".join(lines)


def split_text_call(text: str) -> tuple[str, dict[str, Any], str] | None:
    """Like `parse_text_call`, but also returns the prose leading up to the call.

    **Only what comes before.** Everything a model writes after its tool call is
    speculation about a result it has not received, and small models write a
    great deal of it. Observed live: asked to work a turn, the model invented a
    whole province list — "Hestia", "Ithaca", "Juno", "Kalliope" with made-up
    production and income figures — and only then called `get_turn_summary`.
    The real provinces are Marignon, Copper Canyons and The Obsidian Waste.

    Discarding the tail keeps that out of the transcript and out of the context
    the model reads on its next step. Keeping it would leave a fabricated table
    sitting next to a real one, indistinguishable to a model that will shortly
    be asked to reason over both.

    In text-protocol mode the whole reply may be the JSON object, so an empty
    prose result is normal and means there was no reasoning to show.
    """
    found = _find_call(text)
    if found is None:
        return None
    name, args, start, _end = found
    prose = _strip_control_tokens(text[:start]).strip()
    # A bare fence left behind by stripping the object is not prose.
    if prose.replace("`", "").replace("json", "").strip() == "":
        prose = ""
    return name, args, prose


#: Chat-template control tokens that leak into the text when a model is asked
#: to emit tool calls as prose — it still reaches for its native tool-call
#: markers. Observed: `<tool_call|>` around a JSON object in text-protocol mode.
#: Stripped from display only; the JSON itself is parsed as it stands.
_CONTROL_TOKEN_RE = re.compile(r"<\|?/?[a-z_]+\|?>")


def _strip_control_tokens(text: str) -> str:
    return _CONTROL_TOKEN_RE.sub("", text)


def looks_like_call(text: str) -> bool:
    """Did the model try to call a tool and get the JSON wrong?

    Distinguishing a failed call from a plain answer matters: a failed call
    handed back as prose ends the turn as though the model had finished, when
    in fact it is mid-thought and waiting for a result it will never get.
    Observed live: `{"tool": "list_commanders", "args {}}` — a missing colon,
    and the run stopped there.

    Deliberately a *detector*, not a repair. What the model gets back is its
    own broken JSON and a note saying so, on the same principle that arguments
    are never coerced.
    """
    lowered = text.casefold()
    return (
        '"tool"' in text
        or "'tool'" in text
        or "<|tool_call" in lowered
        or "<tool_call" in lowered
    )


def parse_text_call(text: str) -> tuple[str, dict[str, Any]] | None:
    """Find a tool call in prose output from a model without function calling.

    Scans for a JSON object carrying a "tool" key. Models wrap these in prose,
    in ``` fences, and in explanations of what they are about to do, so the
    parse is by brace-matching rather than by expecting clean output — but it
    does NOT repair malformed JSON. A model that emitted broken JSON should be
    told so, for the same reason arguments are not coerced.

    Returns (name, args) or None if there is no call.
    """
    found = _find_call(text)
    return (found[0], found[1]) if found else None


def _find_call(text: str) -> tuple[str, dict[str, Any], int, int] | None:
    """(name, args, start, end) of the first tool call object in `text`."""
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                blob = text[start:i + 1]
                try:
                    obj = json.loads(blob)
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(obj, dict) and "tool" in obj:
                    args = obj.get("args", obj.get("arguments", {}))
                    if not isinstance(args, dict):
                        args = {}
                    return str(obj["tool"]), args, start, i + 1
                start = -1
    return None
