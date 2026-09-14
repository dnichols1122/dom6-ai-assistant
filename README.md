# Dominions 6 AI Assistant

An LLM harness that reads Dominions 6 save files and plays a turn through a
tool surface deliberately limited to **what a human player can actually see**.

The interesting part is not that a model is attached to a game. It is the
discipline around it: every field the assistant reads was established by
controlled diff — change exactly one thing in the running game, save, and the
changed bytes are the field — and anything that cannot be proven is reported as
unknown rather than guessed. Dominions resolves a turn either way, so a wrong
number that looks right cannot be spotted afterwards.

## Quick start

One script does the whole thing — dependencies, game data, the manual, the wiki
mirror and the video fetcher. Everything, by default, and safe to re-run.

Budget about 35 minutes: the wiki mirror is the slow part. `--minimal` skips
all three libraries and finishes in a couple of minutes; `--ask` lets you pick.

```bash
# Linux and macOS
./scripts/install.sh

# Windows, in PowerShell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

`-ExecutionPolicy Bypass` applies to that one process only; Windows blocks
downloaded scripts by default and this does not change any system setting.

| | |
|---|---|
| *(no flags)* | everything — assistant and all three libraries |
| `--minimal` / `-Minimal` | just the assistant, ~2 minutes |
| `--ask` / `-Ask` | choose each library |
| `--start` / `-Start` | start the server when finished |

<details>
<summary>Or do it by hand</summary>

```bash
uv sync --extra web --extra dev
uv run python -m dom6_assistant.reference.build_db --refresh
uv run uvicorn dom6_assistant.ui.app:app --port 8001
```

Open <http://127.0.0.1:8001/> and point the **Model endpoint** box at your
model — local or hosted — with a **Test connection** button that tells you
whether it worked. No config file to edit.

**`--extra web` is not optional** — FastAPI and uvicorn live there, and a plain
`uv sync` installs a package that cannot start a server. Every command needs
`uv run`, which is what puts the project on the path.

That is a complete, working assistant: it reads your saves and knows the game's
units, spells, events and nations. The three reference libraries are separate,
optional, and each fetched onto your own machine:

```bash
# Illwinter's manual, searchable and cited by page.
uv sync --extra manual && uv run dom6-assistant manual-fetch && uv run dom6-assistant manual-build

# The community strategy wiki. Polite and resumable, around 30 minutes.
uv run dom6-assistant wiki-scrape && uv run dom6-assistant wiki-build

# A strategy video, searchable by timestamp.
uv sync --extra videos && uv run dom6-assistant video-add <youtube url>
```

Skip any of them and the assistant simply says that source was unavailable
rather than inventing what it would have said.

</details>

[RUNNING.md](RUNNING.md) covers this properly, including Windows, pointing it at
a model, and what each page is for.

## What it does

- **Parses `.trn`, `.2h` and `.map`** by anchored signature search rather than
  fixed offsets, because the files are rewritten every turn and nothing stays
  put.
- **Enforces a visibility boundary.** `agent/visibility.PlayerView` is the only
  path to game data. `ftherlnd` — the host's full-information file — is blocked
  by filename. Rival treasuries are parsed and deliberately not exposed, because
  no screen shows them to a player.
- **Ingests turns by itself.** While the server runs it watches your save
  folder, so finishing a turn in Dominions is the whole workflow.
- **Designs pretenders** pre-game, with exact chassis, path, scale and blessing
  arithmetic transcribed from the game's own routines.
- **Talks about the game** with no save loaded, for comparing nations before
  committing to one.
- **Cites the manual.** Illwinter's own documentation, indexed locally and
  searchable by rule, answering with the page number you can turn to.
- **Watches strategy videos, sort of.** Transcripts of videos you add, searched
  by timestamp, answering with a link to the moment. Ranked below the manual on
  purpose: a guide is one opinion, usually about an older edition.

Three workspaces — Live turn, Pretender design, Open chat — each with its own
tool registry, chat library, and character bindings.

## Requirements

- **Dominions 6**, your own copy, from [Illwinter](https://www.illwinter.com/dom6/)
- **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/)
- **An OpenAI-compatible model endpoint** — llama.cpp, KoboldCpp, LM Studio,
  Ollama, vLLM, or a hosted API. That covers nearly everything: "OpenAI" there
  names the protocol, not the company.

Developed on Linux. Save locations are resolved per platform, so Windows and
macOS are handled; the optional live-memory tooling is Linux-only and nothing
else depends on it.

## Game data is not included

The reference data is Illwinter's, not mine to redistribute, so the build step
above fetches and assembles it locally. The same goes for the strategy wiki
mirror, for Illwinter's freely published manual, and for any video transcripts
you add -- all optional, and each downloaded onto your machine and indexed
there, never shipped here. Transcripts belong to the people who recorded them. Tests that
need data you have not built will say so and skip.

## Licence

**GNU General Public License v3.0** — see [LICENSE](LICENSE).

Use it, study it, change it, share it, including commercially. Derivative works
stay under the GPL and must credit the original. No warranty.

GPL-3.0 partly for compatibility: the reference pipeline builds on
[dom6inspector](https://github.com/larzm42/dom6inspector), which is GPL-3.0.

This project is unaffiliated with Illwinter Game Design and is not endorsed by
them.
