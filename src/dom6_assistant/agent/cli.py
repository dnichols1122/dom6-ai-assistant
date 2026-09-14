"""Command-line harness for the tool surface.

Exists to exercise the loop without the web UI in the way. Three modes:

    python -m dom6_assistant.agent tools            list the tool surface
    python -m dom6_assistant.agent call NAME [k=v]  invoke one tool directly
    python -m dom6_assistant.agent run "PROMPT"     drive the model through it

`call` is the one that earns its place. When something misbehaves, being able
to run a single tool against the live save with no model involved separates a
broken tool from a confused model — and those look identical from the outside.

Writes are off unless `--write` is passed, and the sandbox flag copies the save
and database first, so the default way to try anything cannot touch a real game.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from dom6_assistant.agent.llm import LLMError, OpenAICompatClient
from dom6_assistant.agent.loop import TurnAgent
from dom6_assistant.agent.session import SessionError, open_session


def _sandbox(save_dir: Path, game_db: Path, into: Path) -> tuple[Path, Path]:
    """Copy the save and database so a run cannot alter a real game."""
    into.mkdir(parents=True, exist_ok=True)
    save_copy = into / "save"
    if save_copy.exists():
        shutil.rmtree(save_copy)
    save_copy.mkdir()
    for pattern in ("*.trn", "*.2h"):
        for path in save_dir.glob(pattern):
            shutil.copy2(path, save_copy / path.name)
    db_copy = into / "game.sqlite3"
    shutil.copy2(game_db, db_copy)
    return save_copy, db_copy


def _parse_kv(pairs: list[str]) -> dict:
    """k=v arguments, with JSON values where they parse as JSON."""
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"argument {pair!r} is not k=v")
        key, _, value = pair.partition("=")
        try:
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dom6-agent", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("tools", "call", "run", "probe",
                                        "fresh-orders"))
    parser.add_argument("args", nargs="*")
    parser.add_argument("--game", help="game name; default is the first one")
    parser.add_argument("--game-db", type=Path,
                        default=Path("knowledge/game.sqlite3"))
    parser.add_argument("--save-dir", type=Path,
                        help="override the save directory")
    parser.add_argument("--write", action="store_true",
                        help="allow tools that change state")
    parser.add_argument("--sandbox", type=Path, metavar="DIR",
                        help="copy save and database into DIR and work there")
    parser.add_argument("--base-url", default=None,
                        help="model endpoint (default: config, else koboldcpp)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output for `call`")
    parser.add_argument("--full", action="store_true",
                        help="show tool results in full rather than truncated")
    parser.add_argument("--profile", metavar="NAME",
                        help="model profile to activate for this run "
                             "(e.g. gemma-koboldcpp, deepseek-r1)")
    parser.add_argument("--think", action="store_true",
                        help="force the text protocol, so the model must write "
                             "its reasoning as prose; slower but visible")
    opts = parser.parse_args(argv)

    if opts.mode == "probe":
        return _probe(opts)

    try:
        save_dir = opts.save_dir
        game_db = opts.game_db
        if opts.sandbox:
            probe = open_session(opts.game, game_db=game_db, save_dir=save_dir)
            save_dir, game_db = _sandbox(probe.ctx.save_dir, opts.game_db,
                                         opts.sandbox)
            probe.close()
            print(f"sandbox: {save_dir.parent}", file=sys.stderr)
        session = open_session(opts.game, game_db=game_db, save_dir=save_dir)
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if opts.mode == "fresh-orders":
            # Declare the current .2h authoritative. Detection handles the
            # ordinary cases; this covers a file restored from a backup or
            # copied in from elsewhere, where nothing in the bytes says so.
            from dom6_assistant.orders import materialize as _M
            h2_path = session.ctx.h2_path
            if h2_path is None or not h2_path.exists():
                print("error: no .2h file for this game", file=sys.stderr)
                return 2
            reason = _M.base_is_stale(h2_path)
            _M.reset_base(h2_path)
            print(f"{h2_path.name} is now the base for rebuilds.")
            print(f"  it was already considered stale: {reason}" if reason
                  else "  it had been considered current; the base is re-taken "
                       "from it anyway")
            return 0
        if opts.mode == "tools":
            print(session.registry.text_protocol())
            return 0
        if opts.mode == "call":
            if not opts.args:
                print("usage: call NAME [k=v ...]", file=sys.stderr)
                return 1
            result = session.call(opts.args[0], _parse_kv(opts.args[1:]))
            if opts.json:
                print(json.dumps(result, default=str))
            else:
                payload = result.get("result", result.get("error"))
                print(json.dumps(payload, indent=2, default=str))
            return 0 if result["ok"] else 1
        return _run(opts, session)
    finally:
        session.close()


def _client(opts) -> OpenAICompatClient:
    from dom6_assistant.agent.llm import client_from_config
    client = client_from_config()
    if opts.base_url:
        client.base_url = opts.base_url.rstrip("/")
    if opts.model:
        client._model = opts.model
    return client


def _probe(opts) -> int:
    client = _client(opts)
    try:
        models = client.models()
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"endpoint : {client.base_url}")
    print(f"models   : {', '.join(models) or 'none'}")
    print(f"using    : {client.model}")
    native = client.supports_tools()
    print(f"tool call: {'native' if native else 'not supported — text protocol'}")
    return 0


def _run(opts, session) -> int:
    if not opts.args:
        print("usage: run \"PROMPT\"", file=sys.stderr)
        return 1
    client = _client(opts)
    from dom6_assistant.agent import profiles
    if opts.profile:
        profiles.activate(session.ctx.game_db, opts.profile)
    profile = profiles.active_profile(session.ctx.game_db)
    agent = TurnAgent(session, client, max_steps=opts.max_steps,
                      allow_writes=opts.write,
                      force_text_protocol=opts.think, profile=profile)
    print(f"(profile: {profile.name}, tools: "
          f"{'text' if agent.force_text_protocol else profile.tool_mode})",
          file=sys.stderr)
    if not opts.write:
        print("(read-only: write tools are not loaded; pass --write to enable)",
              file=sys.stderr)
    failed = False
    for event in agent.run(" ".join(opts.args)):
        if event.kind == "error":
            print(f"error: {event.text}", file=sys.stderr, flush=True)
            failed = True
        else:
            # Flushed per event: a local model takes tens of seconds per step,
            # and buffered output makes a working run indistinguishable from a
            # hung one.
            print(event.render(full=opts.full), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
