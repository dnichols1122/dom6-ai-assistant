# Installing and using the assistant

## Requirements

- **Dominions 6**, your own copy, from [Illwinter](https://www.illwinter.com/dom6/)
- **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/)
- **An OpenAI-compatible model endpoint** — llama.cpp, KoboldCpp, vLLM, LM
  Studio, or a hosted API. Tool calling is probed at session open, so a server
  that does not support it falls back to a prompt-based protocol automatically.

Developed on Linux, but save locations are resolved per platform:

| | Saved games |
|---|---|
| Windows | `%APPDATA%\Dominions6\savedgames` |
| Linux, macOS | `~/.dominions6/savedgames` |

Set `DOM6_SAVE_ROOT` to override, which is what a Steam Proton prefix or an
unusual install needs. The optional live-memory tooling is Linux-only and
nothing else depends on it.

## Install

```bash
git clone <this repository>
cd dominions6-ai-assistant
uv sync --extra web --extra dev
```

**Do not leave off `--extra web`.** FastAPI and uvicorn are declared there, and
a plain `uv sync` gives you a package with no web server — `uvicorn` will not be
found. `--extra dev` adds pytest; drop it if you do not want to run tests.

Every command below starts with `uv run`. That is what puts the project and its
dependencies on the path; without it your system Python is used and nothing is
importable.

### Build the reference data

None of the game's data ships with this repository — it belongs to Illwinter.
This fetches and assembles it, and takes a couple of minutes:

```bash
uv run python -m dom6_assistant.reference.build_db --refresh
```

That is the only build step you need. Two optional extras:

```bash
# Spells, events and nations read from your own installed game binary.
uv run python -m dom6_assistant.reference.build_index

# A local mirror of the community strategy wiki. Polite, resumable, ~30 min.
uv run dom6-assistant wiki-scrape
uv run dom6-assistant wiki-build
```

### Point it at a model

Start the server (next section), open the assistant, and use the **Model
endpoint** box in the left-hand panel: server address, model, key, then **Test
connection**. It says whether it reached the server and what that server is
serving, so a wrong port or a missing `/v1` is one click to find rather than a
stack trace mid-chat. **Save endpoint** writes it to your config file and
applies to the next message — no restart.

Three things catch everyone out, in the interface or in a file:

- **The `/v1` on the end of the address is part of the address.** Leaving it off
  is the most common reason nothing connects. KoboldCpp is
  `http://localhost:5001/v1`, llama.cpp `:8080`, LM Studio `:1234`,
  Ollama `:11434`.
- **`auto` as the model** asks the server what it is serving, so you can swap
  models without touching this again. Hosted providers need the exact name;
  Ollama does too.
- **The key can be left blank** for a local server. Nothing is sent at all,
  which is what local servers expect.

<details>
<summary>Configuring it by hand instead</summary>

`cp dom6-assistant.toml.example dom6-assistant.toml` and edit one block. The
example file is commented with a ready-made block for each server.

```toml
[model]
backend = "openai"          # the name of the *protocol*, not the company:
                            # llama.cpp, KoboldCpp, LM Studio, Ollama, vLLM
[model.openai]              # and OpenRouter all speak it
base_url = "http://localhost:5001/v1"
model    = "auto"
api_key  = ""               # "sk-..." for a hosted provider
```

Config is read from, in increasing precedence:

| | |
|---|---|
| Linux, macOS | `~/.config/dom6-assistant/config.toml` |
| Windows | `%APPDATA%\dom6-assistant\config.toml` |
| either | `dom6-assistant.toml` in the project directory |

Saving from the interface writes to whichever of these already exists,
preferring the project file, since that is the one that wins on load.

To keep a key out of any file, set `api_key_env = "MY_KEY"` and export that
variable in the shell you start the server from.

</details>

## Run it

```bash
uv run uvicorn dom6_assistant.ui.app:app --port 8001
```

| Page | What it is |
|---|---|
| <http://127.0.0.1:8001/> | The assistant. Start here |
| <http://127.0.0.1:8001/turns> | Turn overview, built from ingested saves |
| <http://127.0.0.1:8001/verify> | Model-free audit of everything the assistant can see |

**Start with `/verify`.** It invokes the real read tools against your live
save and shows their unmodified output without contacting any model. It is the
fastest way to confirm the parser agrees with your game before you let a model
reason from it.

## The three workspaces

Pick one from the selector at the top of `/agent`. Each has its own tools, its
own saved chats, and its own character binding.

**Open chat** — no game loaded. Reference lookups, nation comparisons and the
wiki. Use it to decide what to play.

**Pretender design** — choose a nation; no save file needed. Exact chassis,
path, scale and blessing arithmetic. With **allow writes** enabled, the final
`create_pretender` writes a new god into `~/.dominions6/savedgames/newlords`
without overwriting an existing design. Select it in Dominions when creating
the game.

**Live turn** — reads your current `.trn` and `.2h`.

**Turns ingest themselves.** While the server is running it watches
`~/.dominions6/savedgames`, and every `.trn` is read once its bytes stop
changing — so finishing a turn in Dominions is all that is required. Games
started after the server was launched are picked up too. The header shows how
many games are being watched and when the last turn landed.

To ingest by hand anyway, or to backfill before the server existed:

```bash
uv run python -m dom6_assistant.gamestate.ingest --scan
```

To stop the server touching your saves at all — a read-only session:

```bash
DOM6_AUTO_INGEST=0 uv run uvicorn dom6_assistant.ui.app:app --port 8001
```

Or in `dom6-assistant.toml`:

```toml
[ingest]
auto = false                                  # default is true
# save_root = "~/.dominions6/savedgames"      # if yours lives elsewhere
```

## Chats, characters and personas

Each workspace keeps a **library of named chats** — New, Rename, Delete in the
header — so several lines of enquiry about the same nation can run side by
side. They are stored in `knowledge/game.sqlite3`, not in browser storage, so
long tool transcripts survive.

The **Character** tab imports SillyTavern V1/V2/V3 cards, or writes one from
scratch. Set **your persona** there too: a name and a description of yourself.
The name fills every `{{user}}` in a card; the description is given to the
character as information about you, never as an instruction.

## The model profile

The **Model** tab edits sampling and protocol settings, saved as revisions.

Two settings are worth understanding:

- **request timeout** — per model completion, not per agent run. Slow local
  inference on a long context can exceed the default; `0` disables the deadline.
- **prefill** — puts words in the model's mouth to force a thinking block.
  **Leave it blank if your server already returns reasoning in its own field**
  (llama.cpp and vLLM send `reasoning_content` beside the answer). The trailing
  turn a prefill adds suppresses that channel, and the reasoning disappears
  instead of arriving separately. This fails silently, with no error.

**stream** shows the reply as it is written. Tool calls are still only executed
once complete — their arguments arrive split across chunks, so acting on a
fragment would call the tool with nonsense.

## Reading the confidence markers

Parsed values carry a marker saying how far to trust them:

| Marker | Meaning |
|---|---|
| green — confirmed | cross-checked against a known value |
| amber — derived | consistent with the reference data, but not independently proven |
| red — guess | inferred from range or behaviour, unverified |

The project's standing rule is **prefer absent over plausible**: a value that
cannot be established is reported as unknown rather than estimated. Dominions
resolves a turn either way, so a wrong number that looks right cannot be caught
afterwards.

## When you and the assistant both edit a turn

Order materialisation rebuilds the `.2h` from a pristine base captured before
the first write, so the file is a pure function of recorded intent. That base
is re-taken automatically when the turn advances, and when the live file
matches neither the base nor the last thing the assistant wrote — which is what
you saving in-game looks like.

Detection cannot see every case. A file restored from a backup carries nothing
in its bytes that says so. Declare it authoritative:

```bash
uv run python -m dom6_assistant.agent fresh-orders
```

## Databases

| Path | Contents | Rebuildable |
|---|---|---|
| `knowledge/reference/reference.sqlite3` | Static game data | yes — `build_db --refresh` |
| `knowledge/illwiki/illwiki.sqlite3` | Wiki mirror and search index | yes — `wiki-scrape`, `wiki-build` |
| `knowledge/game.sqlite3` | Your turns, orders, chats and characters | **no — accumulates history** |

The first two are disposable. The third is not: it holds everything the
assistant has recorded about your games. Back it up.

## Tests

```bash
uv run pytest
```

Much of this suite is written against real artefacts rather than fixtures —
that is the point of a reverse-engineering project — so what you see depends on
what you have built.

**Before building anything**, roughly 285 tests pass, 900 skip, and about seven
fail. The header tells you why, and the skips name what is missing. The seven
are the exception to an otherwise clean run: they assert against the author's
own controlled-diff snapshot corpus, which is not published, and they assert on
its contents rather than failing to open it, so nothing can distinguish them
from a genuine regression automatically.

**After `build_db --refresh`**, the reference-dependent tests run and only the
snapshot ones remain skipped or failing.

The skip behaviour is deliberately narrow: a missing-data error becomes a skip
only when that data is genuinely absent. On a machine where the reference
database exists, the same exception still fails the test, because there it
means something is really wrong.
