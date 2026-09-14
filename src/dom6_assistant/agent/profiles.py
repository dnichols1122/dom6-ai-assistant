"""Model profiles: system prompt, reasoning markers, and how to call tools.

Every model wraps its thinking differently and there is no standard, so the
wrapping is configuration rather than code. Built from what the endpoints
actually do:

* **DeepSeek R1, Qwen, QwQ** — literal `<think>` … `</think>` inside `content`.
* **Harmony (gpt-oss)** — `<|channel|>analysis<|message|>` … `<|end|>`.
* **Gemma via koboldcpp** — `<|channel>thought` with *no closing marker*: the
  block runs until the next channel marker or the end of the text. Measured on
  the local server, which does not lift it into `reasoning_content`.
* **Anthropic, OpenAI o-series** — a separate field, no markers at all.

Hence `reasoning_end` is optional: an empty end means "to the next start marker
or the end of the text", which is the only way to read Gemma's output.

**Why the system prompt is stored here too.** It is the thing that will be
tuned most, game after game, and tuning it by editing a Python constant means
losing the previous version and having no record of which wording produced
which turn. A profile is editable, nameable and copyable, so a prompt that
plays well can be kept while another is tried.

`tool_mode` matters more than it looks. On this local model, native tool
calling returns `content: ""` beside the call — the thinking is discarded by
the server's tool-call parser — so a run is completely silent. Forcing `text`
puts the tool list in the prompt and makes the model write both its reasoning
and the call as prose. Slower, more fragile, and the only way to watch it
think while it uses tools.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

#: Shipped presets. `system_prompt` empty means "use the built-in default",
#: so a preset does not silently pin a prompt that later improves.
PRESETS: dict[str, dict[str, Any]] = {
    "default": {
        "reasoning_start": "", "reasoning_end": "", "prefill": "",
        "preserve_tool_reasoning": True,
        "tool_mode": "auto", "temperature": 0.3, "max_tokens": 1600,
        "notes": "No reasoning markers. Works with any model that returns a "
                 "plain answer, or one whose server exposes reasoning_content.",
    },
    "gemma-koboldcpp": {
        "reasoning_start": "", "reasoning_end": "",
        "prefill": "<|channel>thought\n",
        "preserve_tool_reasoning": True,
        "tool_mode": "text", "temperature": 0.3, "max_tokens": 2400,
        "notes": "Gemma served by koboldcpp with thinking on. Markers are "
                 "deliberately EMPTY: the output format is not consistent. The "
                 "same model emitted a raw '<|channel>thought' token on one "
                 "reply and plain '**Thinking:** / **Answer:**' markdown on the "
                 "next, so any fixed marker is wrong about half the time and "
                 "would hide the answer. Showing everything is correct here. "
                 "tool_mode=text is the important part: with native tool "
                 "calling this server returns content:'' beside the call and "
                 "the whole run is silent. If the raw channel token starts "
                 "leaking consistently, set start='<|channel>thought' and "
                 "end='<channel|>'.",
    },
    "gemma-koboldcpp-channels": {
        "reasoning_start": "<|channel>thought", "reasoning_end": "<channel|>",
        "prefill": "<|channel>thought\n",
        "preserve_tool_reasoning": True,
        "tool_mode": "text", "temperature": 0.3, "max_tokens": 2400,
        "notes": "Gemma 4's official thinking format: the asymmetric "
                 "<|channel>thought opener and <channel|> closer.",
    },
    "deepseek-r1": {
        "reasoning_start": "<think>", "reasoning_end": "</think>",
        "prefill": "<think>\n",
        "preserve_tool_reasoning": True,
        "tool_mode": "auto", "temperature": 0.6, "max_tokens": 2400,
        "notes": "Also matches Qwen and QwQ distills.",
    },
    "harmony-gpt-oss": {
        "reasoning_start": "<|channel|>analysis<|message|>",
        "reasoning_end": "<|end|>", "prefill": "",
        "preserve_tool_reasoning": True,
        "tool_mode": "auto", "temperature": 0.3, "max_tokens": 2400,
        "notes": "OpenAI harmony format.",
    },
    "anthropic": {
        "reasoning_start": "", "reasoning_end": "", "prefill": "",
        "preserve_tool_reasoning": True,
        "tool_mode": "native", "temperature": 0.3, "max_tokens": 4000,
        "notes": "Reasoning arrives in its own field, not in the text, so no "
                 "markers are needed. Native tool calling is reliable.",
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_profile (
    name            TEXT PRIMARY KEY,
    system_prompt   TEXT,            -- empty = use the built-in default
    reasoning_start TEXT NOT NULL DEFAULT '',
    reasoning_end   TEXT NOT NULL DEFAULT '',
    prefill         TEXT NOT NULL DEFAULT '',
    preserve_tool_reasoning INTEGER NOT NULL DEFAULT 1,
    tool_mode       TEXT NOT NULL DEFAULT 'auto',  -- auto | native | text
    temperature     REAL NOT NULL DEFAULT 0.3,
    max_tokens      INTEGER NOT NULL DEFAULT 1600,
    request_timeout INTEGER NOT NULL DEFAULT 300,
    notes           TEXT,
    is_active       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS agent_profile_revision (
    id              INTEGER PRIMARY KEY,
    profile_name    TEXT NOT NULL,
    system_prompt   TEXT NOT NULL DEFAULT '',
    reasoning_start TEXT NOT NULL DEFAULT '',
    reasoning_end   TEXT NOT NULL DEFAULT '',
    prefill         TEXT NOT NULL DEFAULT '',
    preserve_tool_reasoning INTEGER NOT NULL DEFAULT 1,
    tool_mode       TEXT NOT NULL,
    temperature     REAL NOT NULL,
    max_tokens      INTEGER NOT NULL,
    request_timeout INTEGER NOT NULL DEFAULT 300,
    notes           TEXT,
    saved_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_profile_revision
    ON agent_profile_revision(profile_name, id);
"""


@dataclass
class Profile:
    name: str = "default"
    system_prompt: str = ""
    reasoning_start: str = ""
    reasoning_end: str = ""
    #: Text to put in the model's mouth as the start of its reply. Sent as a
    #: trailing assistant message, which an OpenAI-compatible server continues
    #: rather than answering.
    #:
    #: This is how a thinking block is made reliable. Left to itself Gemma
    #: opens one when it feels like it — the same prompt produced a raw
    #: `<|channel>thought` token on one reply and none at all on the next.
    #: Prefilling `<|channel>thought` starts the block for it, and the model
    #: finishes it. The player uses the same trick in SillyTavern.
    prefill: str = ""
    #: Gemma 4 removes thinking from ordinary history, but its official format
    #: explicitly retains thinking on assistant turns that call a tool. Keep
    #: this model-specific because other endpoints may expect different
    #: historical reasoning semantics.
    preserve_tool_reasoning: bool = True
    tool_mode: str = "auto"
    temperature: float = 0.3
    max_tokens: int = 1600
    #: Deadline for one model HTTP request, not the entire multi-tool run.
    #: Zero deliberately means no deadline for slow local inference.
    request_timeout: int = 300
    notes: str = ""
    is_active: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "system_prompt": self.system_prompt,
            "reasoning_start": self.reasoning_start,
            "reasoning_end": self.reasoning_end,
            "prefill": self.prefill,
            "preserve_tool_reasoning": self.preserve_tool_reasoning,
            "tool_mode": self.tool_mode, "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "request_timeout": self.request_timeout,
            "notes": self.notes,
            "is_active": self.is_active,
        }

    def split_reasoning(self, content: str) -> tuple[str, str]:
        """Split `content` into (visible answer, reasoning).

        With no start marker the content is returned unchanged, which is the
        right answer for a model that puts reasoning in its own field and for
        one that does not reason at all.
        """
        return split_reasoning(content, self.reasoning_start,
                               self.reasoning_end)


def split_reasoning(content: str, start: str, end: str) -> tuple[str, str]:
    """Extract marker-delimited thinking from a completion.

    An empty `end` means the block runs to the next occurrence of `start` or to
    the end of the text. Some local templates omit a closing marker, and
    treating that as "no reasoning" would show the user the whole chain of
    thought as if it were the answer.
    """
    if not start or start not in content:
        return content, ""
    visible: list[str] = []
    thinking: list[str] = []
    rest = content
    while start in rest:
        before, _, after = rest.partition(start)
        if before.strip():
            visible.append(before)
        if end and end in after:
            thought, _, remainder = after.partition(end)
            thinking.append(thought)
            rest = remainder
        else:
            # No closing marker: the block runs to the next opening one.
            if start in after:
                thought, _, remainder = after.partition(start)
                thinking.append(thought)
                rest = start + remainder
            else:
                thinking.append(after)
                rest = ""
    if rest.strip():
        visible.append(rest)
    return ("\n".join(v.strip() for v in visible if v.strip()),
            "\n".join(t.strip() for t in thinking if t.strip()))


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the table, and migrate one that predates a column.

    Migration rather than a rebuild: a database from before `prefill` already
    holds tuned profiles, and dropping the table to add a column would throw
    away the tuning the table exists to preserve.

    Backfilling the preset prefills happens here, at the moment the column is
    added, and only then. That is the one instant when an empty value is
    unambiguously the migration default rather than a deliberate choice — after
    it, a preset row with no prefill means the user cleared it, and re-seeding
    must not put it back.
    """
    conn.executescript(SCHEMA)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(agent_profile)")}
    if "prefill" not in columns:
        conn.execute("ALTER TABLE agent_profile ADD COLUMN prefill TEXT "
                     "NOT NULL DEFAULT ''")
        for name, values in PRESETS.items():
            if values.get("prefill"):
                conn.execute(
                    "UPDATE agent_profile SET prefill=? WHERE name=?",
                    (values["prefill"], name))
    if "request_timeout" not in columns:
        conn.execute("ALTER TABLE agent_profile ADD COLUMN request_timeout "
                     "INTEGER NOT NULL DEFAULT 300")
    if "preserve_tool_reasoning" not in columns:
        conn.execute("ALTER TABLE agent_profile ADD COLUMN "
                     "preserve_tool_reasoning INTEGER NOT NULL DEFAULT 1")
    revision_columns = {
        r[1] for r in conn.execute("PRAGMA table_info(agent_profile_revision)")
    }
    if "request_timeout" not in revision_columns:
        conn.execute("ALTER TABLE agent_profile_revision ADD COLUMN "
                     "request_timeout INTEGER NOT NULL DEFAULT 300")
    if "preserve_tool_reasoning" not in revision_columns:
        conn.execute("ALTER TABLE agent_profile_revision ADD COLUMN "
                     "preserve_tool_reasoning INTEGER NOT NULL DEFAULT 1")
    conn.commit()


def seed_presets(conn: sqlite3.Connection) -> None:
    """Insert shipped presets that are not already present.

    INSERT OR IGNORE rather than REPLACE: a preset the user has edited is
    theirs, and re-seeding must not quietly revert their tuning.
    """
    ensure_schema(conn)
    for name, values in PRESETS.items():
        conn.execute(
            "INSERT OR IGNORE INTO agent_profile(name, system_prompt, "
            "reasoning_start, reasoning_end, prefill, preserve_tool_reasoning, "
            "tool_mode, temperature, max_tokens, request_timeout, notes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (name, "", values["reasoning_start"], values["reasoning_end"],
             values.get("prefill", ""),
             int(values.get("preserve_tool_reasoning", True)),
             values["tool_mode"],
             values["temperature"], values["max_tokens"],
             values.get("request_timeout", 300), values["notes"]))
    if not conn.execute(
            "SELECT 1 FROM agent_profile WHERE is_active=1").fetchone():
        conn.execute("UPDATE agent_profile SET is_active=1 WHERE name='default'")
    conn.commit()


def _row_to_profile(row: sqlite3.Row) -> Profile:
    return Profile(
        name=row["name"], system_prompt=row["system_prompt"] or "",
        reasoning_start=row["reasoning_start"],
        reasoning_end=row["reasoning_end"],
        prefill=row["prefill"] if "prefill" in row.keys() else "",
        preserve_tool_reasoning=(
            bool(row["preserve_tool_reasoning"])
            if "preserve_tool_reasoning" in row.keys() else True),
        tool_mode=row["tool_mode"],
        temperature=row["temperature"], max_tokens=row["max_tokens"],
        request_timeout=row["request_timeout"],
        notes=row["notes"] or "", is_active=bool(row["is_active"]))


def list_profiles(conn: sqlite3.Connection) -> list[Profile]:
    seed_presets(conn)
    return [_row_to_profile(r) for r in
            conn.execute("SELECT * FROM agent_profile ORDER BY name")]


def active_profile(conn: sqlite3.Connection) -> Profile:
    seed_presets(conn)
    row = conn.execute(
        "SELECT * FROM agent_profile WHERE is_active=1 LIMIT 1").fetchone()
    return _row_to_profile(row) if row else Profile()


def save_profile(conn: sqlite3.Connection, profile: Profile) -> Profile:
    ensure_schema(conn)
    if profile.tool_mode not in ("auto", "native", "text"):
        raise ValueError(
            f"tool_mode must be auto, native or text; got {profile.tool_mode!r}")
    if profile.request_timeout < 0:
        raise ValueError(
            "request_timeout must be zero or a positive number of seconds")
    conn.execute(
        "INSERT INTO agent_profile(name, system_prompt, reasoning_start, "
        "reasoning_end, prefill, preserve_tool_reasoning, tool_mode, "
        "temperature, max_tokens, request_timeout, notes, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(name) DO UPDATE SET system_prompt=excluded.system_prompt, "
        "reasoning_start=excluded.reasoning_start, "
        "reasoning_end=excluded.reasoning_end, prefill=excluded.prefill, "
        "preserve_tool_reasoning=excluded.preserve_tool_reasoning, "
        "tool_mode=excluded.tool_mode, "
        "temperature=excluded.temperature, max_tokens=excluded.max_tokens, "
        "request_timeout=excluded.request_timeout, "
        "notes=excluded.notes, updated_at=datetime('now')",
        (profile.name, profile.system_prompt, profile.reasoning_start,
         profile.reasoning_end, profile.prefill,
         int(profile.preserve_tool_reasoning), profile.tool_mode,
         profile.temperature, profile.max_tokens, profile.request_timeout,
         profile.notes))
    conn.execute(
        "INSERT INTO agent_profile_revision(profile_name,system_prompt,"
        "reasoning_start,reasoning_end,prefill,preserve_tool_reasoning,"
        "tool_mode,temperature,max_tokens,request_timeout,notes) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (profile.name, profile.system_prompt, profile.reasoning_start,
         profile.reasoning_end, profile.prefill,
         int(profile.preserve_tool_reasoning), profile.tool_mode,
         profile.temperature, profile.max_tokens, profile.request_timeout,
         profile.notes),
    )
    conn.commit()
    return profile


def profile_revisions(
    conn: sqlite3.Connection, name: str, limit: int = 30
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    return [dict(row) for row in conn.execute(
        "SELECT * FROM agent_profile_revision WHERE profile_name=? "
        "ORDER BY id DESC LIMIT ?", (name, max(1, min(limit, 200))))]


def restore_revision(
    conn: sqlite3.Connection, name: str, revision_id: int
) -> Profile:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM agent_profile_revision WHERE id=? AND profile_name=?",
        (revision_id, name),
    ).fetchone()
    if row is None:
        raise ValueError(f"no revision {revision_id} for profile {name!r}")
    return save_profile(conn, Profile(
        name=name,
        system_prompt=row["system_prompt"],
        reasoning_start=row["reasoning_start"],
        reasoning_end=row["reasoning_end"],
        prefill=row["prefill"],
        preserve_tool_reasoning=bool(row["preserve_tool_reasoning"]),
        tool_mode=row["tool_mode"],
        temperature=row["temperature"],
        max_tokens=row["max_tokens"],
        request_timeout=row["request_timeout"],
        notes=row["notes"] or "",
    ))


def activate(conn: sqlite3.Connection, name: str) -> Profile:
    ensure_schema(conn)
    if not conn.execute("SELECT 1 FROM agent_profile WHERE name=?",
                        (name,)).fetchone():
        raise ValueError(f"no profile named {name!r}")
    conn.execute("UPDATE agent_profile SET is_active=0")
    conn.execute("UPDATE agent_profile SET is_active=1 WHERE name=?", (name,))
    conn.commit()
    return active_profile(conn)


def delete_profile(conn: sqlite3.Connection, name: str) -> None:
    if name in PRESETS:
        raise ValueError(f"{name!r} is a shipped preset and cannot be deleted; "
                         "copy it under another name instead")
    conn.execute("DELETE FROM agent_profile WHERE name=?", (name,))
    conn.commit()
    if not conn.execute(
            "SELECT 1 FROM agent_profile WHERE is_active=1").fetchone():
        activate(conn, "default")
