"""CLI entry point for the Dominions 6 AI Assistant.

Commands:
  status          — check if dom6 is running
  maps            — print memory regions of the running process
  scan            — scan process memory for a known integer value
  narrow          — filter a previous scan by a new value
  hexdump         — hex dump memory at a given address
  ask             — send a freeform question to the configured AI backend
  backends        — list available / configured backends

  trn-snapshot    — copy a .trn file into a diff session with known field values
  snapshot-watch  — automatically archive coherent .trn/.2h/ftherlnd turn sets
  ingest-watch    — ingest stable .trn changes from selected live saves
  trn-diff        — show byte-level differences between two .trn files
  trn-analyze     — correlate field changes to byte offsets across snapshots
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
def main() -> None:
    """Dominions 6 AI Assistant — read game state, get AI advice."""


# ---------------------------------------------------------------------------
# Process / memory commands
# ---------------------------------------------------------------------------

@main.command()
def status() -> None:
    """Check if Dominions 6 is running."""
    from dom6_assistant.memory_reader import find_process
    proc = find_process()
    if proc:
        console.print(f"[bold green]dom6 running[/bold green]  PID={proc.pid}  name={proc.name()}")
    else:
        console.print("[yellow]Dominions 6 is not running.[/yellow]")


@main.command()
@click.option("--filter", "filter_", default="", help="Only show regions whose path contains this string.")
@click.option("--rw-only", is_flag=True, default=False, help="Only show read-write regions.")
def maps(filter_: str, rw_only: bool) -> None:
    """Print the memory map of the running dom6 process."""
    from dom6_assistant.memory_reader import attach
    try:
        reader = attach()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    regions = reader.maps()
    if rw_only:
        regions = [r for r in regions if "rw" in r.perms]
    if filter_:
        regions = [r for r in regions if filter_ in r.pathname]

    table = Table(title="dom6 memory map", show_lines=False)
    table.add_column("Start", style="cyan", no_wrap=True)
    table.add_column("End", style="cyan", no_wrap=True)
    table.add_column("Size", justify="right")
    table.add_column("Perms")
    table.add_column("Path/Label")

    for r in regions:
        size_str = f"{r.size // 1024:,} KiB" if r.size >= 1024 else f"{r.size} B"
        table.add_row(f"{r.start:#x}", f"{r.end:#x}", size_str, r.perms, r.pathname or "")

    console.print(table)
    console.print(f"[dim]{len(regions)} regions shown.[/dim]")


@main.command()
@click.argument("value", type=int)
@click.option("--type", "val_type", default="int32",
              type=click.Choice(["int32", "uint32", "int16"]),
              help="Value type to scan for.")
@click.option("--out", default="", help="Save result addresses to this JSON file for use with narrow.")
def scan(value: int, val_type: str, out: str) -> None:
    """Scan dom6 memory for VALUE.

    Example: find all locations storing gold amount 350
        dom6-assistant scan 350 --out /tmp/scan.json
    """
    from dom6_assistant.memory_reader import attach
    try:
        reader = attach()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    console.print(f"Scanning for [bold]{val_type}[/bold] = [bold]{value}[/bold] …")
    with console.status("Scanning…"):
        if val_type == "int32":
            results = reader.scan_int32(value)
        elif val_type == "uint32":
            results = reader.scan_uint32(value)
        else:
            results = reader.scan_int16(value)

    console.print(f"Found [bold green]{len(results)}[/bold green] matches.")
    for addr in results[:20]:
        console.print(f"  {addr:#018x}")
    if len(results) > 20:
        console.print(f"  … and {len(results) - 20} more.")

    if out:
        with open(out, "w") as f:
            json.dump({"value": value, "type": val_type, "addresses": results}, f)
        console.print(f"[dim]Saved to {out}[/dim]")


@main.command()
@click.argument("value", type=int)
@click.argument("scan_file", type=click.Path(exists=True))
@click.option("--out", default="", help="Save narrowed results to this JSON file.")
def narrow(value: int, scan_file: str, out: str) -> None:
    """Narrow a previous scan result to addresses now holding VALUE.

    Example workflow:
        dom6-assistant scan 10 --out /tmp/s.json     # scan for gold=10
        # spend some gold in-game so it becomes 8
        dom6-assistant narrow 8 /tmp/s.json --out /tmp/s.json
        # repeat until 1 address remains
    """
    from dom6_assistant.memory_reader import attach

    with open(scan_file) as f:
        data = json.load(f)
    addresses: list[int] = data["addresses"]
    prev_type: str = data.get("type", "int32")

    fmt_map = {"int32": "<i", "uint32": "<I", "int16": "<h"}
    fmt = fmt_map.get(prev_type, "<i")

    try:
        reader = attach()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    with console.status("Narrowing…"):
        remaining = reader.narrow(addresses, value, fmt)

    console.print(
        f"[bold green]{len(remaining)}[/bold green] addresses remain "
        f"(was {len(addresses)})."
    )
    for addr in remaining[:20]:
        console.print(f"  {addr:#018x}")

    if out:
        with open(out, "w") as f:
            json.dump({"value": value, "type": prev_type, "addresses": remaining}, f)
        console.print(f"[dim]Saved to {out}[/dim]")


@main.command()
@click.argument("address", type=lambda x: int(x, 0))  # supports 0x prefix
@click.option("--size", default=256, help="Number of bytes to dump.")
def hexdump(address: int, size: int) -> None:
    """Hex dump SIZE bytes at ADDRESS (supports 0x prefix).

    Example: dom6-assistant hexdump 0x55a3f098b000
    """
    from dom6_assistant.memory_reader import attach
    try:
        reader = attach()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    dump = reader.hexdump(address, size)
    console.print(dump)


# ---------------------------------------------------------------------------
# AI commands
# ---------------------------------------------------------------------------

@main.command()
@click.argument("question")
@click.option("--backend", default=None,
              type=click.Choice(["ollama", "anthropic", "openai", "gemini"]),
              help="Override the configured backend.")
@click.option("--stream/--no-stream", default=True, help="Stream the response.")
def ask(question: str, backend: str | None, stream: bool) -> None:
    """Ask the AI a question about your current game.

    Example:
        dom6-assistant ask "My capital is being attacked by Ermor. What should I do?"
    """
    from dom6_assistant.ai import get_backend as _get_backend

    try:
        b = _get_backend(name=backend)
    except (ImportError, EnvironmentError, ValueError) as e:
        console.print(f"[red]Backend error:[/red] {e}")
        sys.exit(1)

    console.print(f"[dim]Using {b.name}[/dim]\n")

    system = (
        "You are an expert Dominions 6 strategist. "
        "Give concise, practical tactical and strategic advice. "
        "When uncertain, explain your reasoning."
    )
    messages = [{"role": "user", "content": question}]

    if stream:
        for chunk in b.stream(system, messages):
            console.print(chunk, end="")
        console.print()
    else:
        response = b.chat(system, messages)
        console.print(response)


@main.command()
def backends() -> None:
    """Show configured AI backends."""
    from dom6_assistant import config as cfg_mod

    conf = cfg_mod.load()
    active = cfg_mod.active_backend(conf)

    table = Table(title="Configured backends")
    table.add_column("Backend")
    table.add_column("Active", justify="center")
    table.add_column("Model")
    table.add_column("Notes")

    for name in ("ollama", "anthropic", "openai", "gemini"):
        bcfg = cfg_mod.backend_config(conf, name)
        model = bcfg.get("model", "—")
        is_active = "✓" if name == active else ""

        if name == "ollama":
            notes = bcfg.get("base_url", "http://localhost:11434")
        else:
            env = bcfg.get("api_key_env", "")
            import os
            key_set = "key set" if os.environ.get(env) else f"{env} not set"
            notes = key_set

        table.add_row(name, is_active, model, notes)

    console.print(table)


# ---------------------------------------------------------------------------
# Offline Illwiki corpus
# ---------------------------------------------------------------------------

@main.command("wiki-scrape")
@click.option(
    "--out", "output_dir", type=click.Path(file_okay=False, path_type=Path),
    default=Path("knowledge/illwiki"), show_default=True,
    help="Mirror directory for manifest.json and raw XHTML.",
)
@click.option("--page", "page_ids", multiple=True, help="Exact page id; repeat as needed.")
@click.option("--limit", type=click.IntRange(min=1), help="Crawl only the first N discovered pages.")
@click.option("--refresh", is_flag=True, help="Re-fetch pages already mirrored successfully.")
@click.option(
    "--delay", default=5.0, show_default=True, type=click.FloatRange(min=0.0),
    help="Requested seconds between requests; robots.txt may force a larger value.",
)
@click.option("--timeout", default=30.0, show_default=True, type=click.FloatRange(min=1.0))
def wiki_scrape(
    output_dir: Path,
    page_ids: tuple[str, ...],
    limit: int | None,
    refresh: bool,
    delay: float,
    timeout: float,
) -> None:
    """Mirror Illwiki's Dominions 6 namespace using its clean XHTML export.

    The crawl is serial, resumable, and honors robots.txt Crawl-delay. Existing
    successful files are skipped unless --refresh is supplied.
    """
    from dom6_assistant.wiki.mirror import mirror_site

    try:
        result = mirror_site(
            output_dir,
            page_ids=page_ids or None,
            limit=limit,
            refresh=refresh,
            requested_delay=delay,
            timeout=timeout,
            progress=console.print,
        )
    except (OSError, ValueError, PermissionError) as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(
        f"[bold green]Mirror complete:[/bold green] {result.downloaded} downloaded, "
        f"{result.skipped} already present, {result.failed} failed; "
        f"manifest {result.manifest_path}"
    )
    if result.failed:
        raise click.ClickException(
            "one or more pages failed; rerun the same command to retry them"
        )


@main.command("wiki-build")
@click.option(
    "--mirror", "mirror_dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("knowledge/illwiki"), show_default=True,
)
@click.option(
    "--out", "database_path", type=click.Path(dir_okay=False, path_type=Path),
    default=Path("knowledge/illwiki/illwiki.sqlite3"), show_default=True,
)
def wiki_build(mirror_dir: Path, database_path: Path) -> None:
    """Build an atomic section-level SQLite FTS index from mirrored XHTML."""
    from dom6_assistant.wiki.index import build_index

    try:
        result = build_index(mirror_dir, database_path)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(
        f"[bold green]Index built:[/bold green] {result.pages} pages, "
        f"{result.sections} sections, {result.words:,} words → {result.database_path}"
    )


@main.command("agent-games")
def agent_games() -> None:
    """List exact campaign/faction profiles available to the agent."""
    from dom6_assistant.agent.autonomy import list_bound_players

    players = list_bound_players()
    table = Table(title="Live assistant player profiles")
    table.add_column("Profile", style="cyan")
    table.add_column("Save")
    table.add_column("Nation id", justify="right")
    table.add_column("Player file")
    for player in players:
        table.add_row(
            player.profile, player.save_name, str(player.nation_id),
            f"{player.nation_slug}.trn/.2h")
    console.print(table)
    if not players:
        console.print(
            "[yellow]No live profiles. Ingest the faction's .trn first.[/yellow]")


@main.command("auto-turn")
@click.option(
    "--game", "profile", required=True,
    help="Exact player profile from `dom6-assistant agent-games`.",
)
@click.option(
    "--out", "snapshot_root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("knowledge/snapshots/autonomous"), show_default=True,
    help="Root for coherent evidence snapshots.",
)
@click.option("--poll", default=0.5, show_default=True,
              type=click.FloatRange(min=0.05))
@click.option("--settle", default=1.0, show_default=True,
              type=click.FloatRange(min=0.0))
@click.option("--max-attempts", default=3, show_default=True,
              type=click.IntRange(min=1, max=10))
@click.option("--max-steps", default=96, show_default=True,
              type=click.IntRange(min=1, max=500))
@click.option(
    "--skip-current", is_flag=True,
    help="Do not play the coherent state present at startup; wait for a change.",
)
@click.option("--once", is_flag=True,
              help="Exit after the first complete or exhausted turn run.")
def auto_turn(
    profile: str,
    snapshot_root: Path,
    poll: float,
    settle: float,
    max_attempts: int,
    max_steps: int,
    skip_current: bool,
    once: bool,
) -> None:
    """Watch and autonomously prepare each new turn for one faction.

    This writes and marks the selected faction's local `.2h` submitted; it does
    not host the turn or upload it to a multiplayer server. A turn succeeds
    only after `complete_turn` validates every decision and `submit_turn`
    writes the two decoded End Turn flags.
    """
    from dom6_assistant.agent.autonomy import (
        resolve_bound_player,
        watch_autonomous_turns,
    )
    from dom6_assistant.file_reader.turn_snapshot import SnapshotNotReady

    try:
        bound = resolve_bound_player(profile)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    evidence = snapshot_root / bound.save_name / bound.nation_slug
    console.print(
        f"[bold green]Autonomous player bound[/bold green] {bound.profile}\n"
        f"  nation id {bound.nation_id} · {bound.nation_slug}.trn/.2h\n"
        f"  watching {bound.save_dir}\n"
        f"  evidence {evidence}"
    )
    console.print(
        "[yellow]This writes and locally submits the .2h; it does not host or "
        "upload the turn.[/yellow]\n[dim]Press Ctrl-C to stop.[/dim]"
    )

    def snapshot_notice(snapshot) -> None:
        console.print(
            f"\n[bold]Settled turn {snapshot.turn}[/bold] → {snapshot.path}")

    def show_event(attempt: int, event) -> None:
        prefix = f"[dim]attempt {attempt}[/dim] "
        console.print(prefix + event.render(full=False))

    try:
        for result in watch_autonomous_turns(
            bound, snapshot_root=evidence, poll_seconds=poll,
            settle_seconds=settle, skip_current=skip_current, once=once,
            max_attempts=max_attempts, max_steps=max_steps,
            on_snapshot=snapshot_notice, on_event=show_event,
        ):
            if result.complete:
                console.print(
                    f"[bold green]Turn {result.turn} complete[/bold green] "
                    f"after {result.attempts} invocation(s): {result.reason}")
            else:
                console.print(
                    f"[bold red]Turn {result.turn} incomplete[/bold red] after "
                    f"{result.attempts} invocation(s): {result.reason}\n"
                    "[yellow]Automatic retries are stopped for this turn. "
                    "Inspect the logs and restart explicitly to retry.[/yellow]")
    except (KeyboardInterrupt, RuntimeError, SnapshotNotReady) as exc:
        if isinstance(exc, KeyboardInterrupt):
            console.print("\n[dim]Autonomous supervisor stopped.[/dim]")
        else:
            raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address.")
@click.option("--port", default=8765, show_default=True, help="Port to listen on.")
@click.option("--reload", is_flag=True, default=False, help="Auto-reload on code changes (dev only).")
def serve(host: str, port: int, reload: bool) -> None:
    """Start the web GUI server, then open http://HOST:PORT in your browser."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]uvicorn not installed.[/red] Run: pip install 'dom6-assistant[web]'")
        sys.exit(1)

    from dom6_assistant.web import create_app

    console.print(f"[bold green]Dom6 Assistant web UI[/bold green]  →  http://{host}:{port}")
    app = create_app()
    uvicorn.run(
        app if not reload else "dom6_assistant.web:create_app",
        host=host,
        port=port,
        reload=reload,
        factory=reload,
    )


# ---------------------------------------------------------------------------
# .trn binary diff / reverse-engineering commands
# ---------------------------------------------------------------------------

@main.command("trn-snapshot")
@click.argument("trn_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("session_dir", type=click.Path(file_okay=False))
@click.option("--label", required=True, help="Short name for this snapshot, e.g. 'benchmark' or 'after_gold_spend'.")
@click.option("--fields", default="", help="Comma-separated field=value pairs, e.g. gold=600,turn=1,fire=0,nature=30")
def trn_snapshot(trn_path: str, session_dir: str, label: str, fields: str) -> None:
    """Copy a .trn file into SESSION_DIR and record known field values.

    Example — benchmark save with gold=600 and nature=30 gems:

        dom6-assistant trn-snapshot ~/.dominions6/savedgames/mygame/mid_pangaea.trn \\
            /tmp/trn_session --label benchmark --fields gold=600,nature=30,turn=1

    Then change something in-game and snapshot again:

        dom6-assistant trn-snapshot ... --label after_ritual --fields gold=550,nature=25
    """
    from dom6_assistant.file_reader.trn_diff import add_snapshot

    parsed_fields: dict[str, float] = {}
    if fields:
        for pair in fields.split(","):
            pair = pair.strip()
            if "=" not in pair:
                console.print(f"[red]Bad field spec '{pair}' — expected key=value[/red]")
                sys.exit(1)
            k, v = pair.split("=", 1)
            try:
                parsed_fields[k.strip()] = float(v.strip())
            except ValueError:
                console.print(f"[red]Non-numeric value for field '{k}': {v!r}[/red]")
                sys.exit(1)

    snap = add_snapshot(Path(trn_path), Path(session_dir), label, parsed_fields)
    console.print(f"[bold green]Snapshot saved:[/bold green] {snap.saved_path}")
    if parsed_fields:
        field_str = "  ".join(f"{k}={v:g}" for k, v in parsed_fields.items())
        console.print(f"  Fields: {field_str}")


@main.command("snapshot-watch")
@click.argument("save_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--out", "output_root", type=click.Path(file_okay=False, path_type=Path),
    default=Path("knowledge/snapshots"), show_default=True,
    help="Parent directory for tTURN-auto snapshot folders.",
)
@click.option(
    "--nation", "nation_stems", multiple=True,
    help=("Player filename stem, e.g. mid_marignon. Repeat for multiple human "
          "players; all .trn player files are auto-detected when omitted."),
)
@click.option("--poll", default=0.5, show_default=True, type=click.FloatRange(min=0.05))
@click.option("--settle", default=1.0, show_default=True, type=click.FloatRange(min=0.0))
@click.option(
    "--skip-current", is_flag=True,
    help="Treat the current turn as already captured and wait for the next one.",
)
@click.option("--once", is_flag=True, help="Exit after the first new snapshot.")
def snapshot_watch(
    save_dir: Path,
    output_root: Path,
    nation_stems: tuple[str, ...],
    poll: float,
    settle: float,
    skip_current: bool,
    once: bool,
) -> None:
    """Archive every settled player TRN/2H pair and the shared FTHERLND.

    Dominions writes the files at different moments. This waits until all
    headers name the same turn and their full contents remain unchanged for
    --settle seconds, then atomically creates ``tTURN-auto``. Identical captures
    are deduplicated; a different state for the same turn receives a numeric
    suffix instead of overwriting evidence.
    """
    from dom6_assistant.file_reader.turn_snapshot import (
        SnapshotNotReady,
        read_turn_files,
        resolve_nation_stems,
        watch_turns,
    )

    try:
        stems = resolve_nation_stems(save_dir, list(nation_stems) or None)
        skipped = None
        if skip_current:
            skipped = read_turn_files(save_dir, stems)
    except (OSError, SnapshotNotReady) as exc:
        raise click.ClickException(str(exc)) from exc

    players = " + ".join(f"{stem}.trn/.2h" for stem in stems)
    console.print(
        f"[bold green]Watching[/bold green] {players} + ftherlnd "
        f"→ {output_root}"
    )
    if skipped is not None:
        console.print(
            f"[dim]Current turn {skipped.turn} state skipped; a later save "
            "during this turn will still be captured.[/dim]"
        )
    console.print("[dim]Press Ctrl-C to stop.[/dim]")
    try:
        for result in watch_turns(
            save_dir,
            output_root,
            nation_stem=stems,
            skip_fingerprint=(skipped.fingerprint if skipped is not None else None),
            poll_seconds=poll,
            settle_seconds=settle,
        ):
            action = "Snapshot saved" if result.created else "Already captured"
            console.print(
                f"[bold green]{action}:[/bold green] turn {result.turn} → {result.path}"
            )
            if once:
                return
    except KeyboardInterrupt:
        console.print("\n[dim]Snapshot watcher stopped.[/dim]")


@main.command("ingest-watch")
@click.argument(
    "save_dirs", nargs=-1, required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--db", "database_path", type=click.Path(dir_okay=False, path_type=Path),
    default=Path("knowledge/game.sqlite3"), show_default=True,
)
@click.option("--poll", default=0.5, show_default=True,
              type=click.FloatRange(min=0.05))
@click.option("--settle", default=1.0, show_default=True,
              type=click.FloatRange(min=0.0))
@click.option(
    "--once", is_flag=True,
    help="Ingest the first settled batch of existing files, then exit.",
)
def ingest_watch(
    save_dirs: tuple[Path, ...],
    database_path: Path,
    poll: float,
    settle: float,
    once: bool,
) -> None:
    """Continuously ingest turns from only the selected SAVE_DIRS.

    Every existing, newly-created, or changed ``.trn`` directly inside the
    supplied directories is ingested after it remains stable for ``--settle``
    seconds. Repeat SAVE_DIRS to watch multiple campaigns; all human player
    files in each campaign are handled independently.
    """
    from dom6_assistant.gamestate.ingest import connect, watch_save_dirs

    watched = tuple(dict.fromkeys(path.resolve() for path in save_dirs))
    console.print("[bold green]Watching for turns[/bold green]")
    for directory in watched:
        console.print(f"  {directory}")
    console.print(f"[dim]Ingesting into {database_path}. Press Ctrl-C to stop.[/dim]")

    conn = connect(database_path)
    try:
        for batch in watch_save_dirs(
            conn, watched, poll_seconds=poll, settle_seconds=settle,
        ):
            for event in batch:
                if event.error:
                    console.print(
                        f"[red]Failed:[/red] {event.path.name}: {event.error}")
                elif event.changed:
                    console.print(
                        f"[bold green]Ingested:[/bold green] {event.profile} "
                        f"turn {event.turn}")
                else:
                    console.print(
                        f"[dim]Unchanged: {event.profile} turn {event.turn}[/dim]")
            if once:
                return
    except KeyboardInterrupt:
        console.print("\n[dim]Ingest watcher stopped.[/dim]")
    finally:
        conn.close()


@main.command("trn-diff")
@click.argument("baseline", type=click.Path(exists=True, dir_okay=False))
@click.argument("target", type=click.Path(exists=True, dir_okay=False))
@click.option("--min-len", default=1, help="Only show regions of at least N changed bytes.")
@click.option("--max-regions", default=200, help="Cap output at this many regions.")
def trn_diff(baseline: str, target: str, min_len: int, max_regions: int) -> None:
    """Show byte-level differences between two .trn files.

    Example:

        dom6-assistant trn-diff /tmp/trn_session/benchmark.trn /tmp/trn_session/after_ritual.trn
    """
    from dom6_assistant.file_reader.trn_diff import diff_bytes

    base_data = Path(baseline).read_bytes()
    tgt_data  = Path(target).read_bytes()

    regions = diff_bytes(base_data, tgt_data)
    regions = [r for r in regions if r.length >= min_len]

    console.print(f"[bold]{Path(baseline).name}[/bold] → [bold]{Path(target).name}[/bold]")
    console.print(f"  File sizes: {len(base_data):,} → {len(tgt_data):,} bytes")
    console.print(f"  Changed regions: {len(regions)}")

    table = Table(show_lines=False, box=None)
    table.add_column("Offset",   style="cyan",   no_wrap=True)
    table.add_column("Len",      justify="right", no_wrap=True)
    table.add_column("Old (hex)", no_wrap=True)
    table.add_column("New (hex)", no_wrap=True)
    table.add_column("Old i32",  justify="right", no_wrap=True)
    table.add_column("New i32",  justify="right", no_wrap=True)
    table.add_column("Δ i32",    justify="right", no_wrap=True)

    shown = 0
    for r in regions:
        if shown >= max_regions:
            console.print(f"[dim]… {len(regions) - max_regions} more regions omitted (use --max-regions)[/dim]")
            break
        old_hex = r.old_bytes[:8].hex()
        new_hex = r.new_bytes[:8].hex()
        if len(r.old_bytes) > 8:
            old_hex += "…"
            new_hex += "…"
        old_i32, new_i32 = r.decode_int32()
        if old_i32 is not None:
            delta_str = f"{new_i32 - old_i32:+d}"
            old_i32_s = str(old_i32)
            new_i32_s = str(new_i32)
        else:
            old_i32_s = new_i32_s = delta_str = ""
        table.add_row(
            f"0x{r.offset:06x}",
            str(r.length),
            old_hex,
            new_hex,
            old_i32_s,
            new_i32_s,
            delta_str,
        )
        shown += 1

    console.print(table)


@main.command("trn-analyze")
@click.argument("session_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--top", default=5, help="Show top N candidates per field.")
def trn_analyze(session_dir: str, top: int) -> None:
    """Correlate labeled field changes to byte offsets across all snapshots.

    After taking several trn-snapshots with different known field values,
    this command shows which byte offsets changed in lock-step with each field.

    Example:

        dom6-assistant trn-analyze /tmp/trn_session/
    """
    from dom6_assistant.file_reader.trn_diff import analyze_session, load_session

    sd = Path(session_dir)
    snapshots = load_session(sd)
    console.print(f"Session: [bold]{sd}[/bold]  ({len(snapshots)} snapshots)")
    for s in snapshots:
        fstr = "  ".join(f"{k}={v:g}" for k, v in s.fields.items()) if s.fields else "(no fields)"
        console.print(f"  [cyan]{s.label}[/cyan]  {fstr}")

    if len(snapshots) < 2:
        console.print("[yellow]Need at least 2 snapshots to analyze.[/yellow]")
        return

    results = analyze_session(sd)
    if not results:
        console.print("[yellow]No correlations found — try snapshots where field values differ.[/yellow]")
        return

    console.print()
    for field_name, candidates in results.items():
        console.print(f"[bold green]{field_name}[/bold green]  ({len(candidates)} candidates)")
        table = Table(show_lines=False, box=None)
        table.add_column("Offset",       style="cyan", no_wrap=True)
        table.add_column("Len",          justify="right")
        table.add_column("Observations", justify="right")
        table.add_column("Sample (field old→new, bytes old→new)")

        for c in candidates[:top]:
            samples = []
            for f_old, f_new, b_old, b_new in c.observed[:3]:
                samples.append(f"{f_old:g}→{f_new:g} / {b_old}→{b_new}")
            table.add_row(
                f"0x{c.offset:06x}",
                str(c.length),
                str(len(c.observed)),
                "  |  ".join(samples),
            )
        console.print(table)


if __name__ == "__main__":
    main()
