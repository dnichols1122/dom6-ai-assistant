"""Build an authoritative reference index straight from the game binary.

Why this exists: an assistant recalling Dominions facts from memory gets them
subtly and confidently wrong — recommending "Magma Bolt" when the spell is
"Magma Bolts", or citing items that do not exist. Every name in this index came
out of the shipped binary, so it can be checked instead of remembered.

Built from three undocumented switches. None appear in `dom6_amd64 --help`;
they were found by scanning the binary for "--" strings:

    --listspells    1473 spells, "<id> <name>"
    --listevents    3302 event texts, "<id>  <text>"
    --listnations    103 nations, grouped under "----- Era N -----"

Deliberately NOT covered here:

  * spell paths, levels, gem costs, research levels
  * unit and item stats
    -> use dom6inspector (https://larzm42.github.io/dom6inspector/), which
       extracts these properly. This index only answers "does X exist, and what
       is it called exactly", which is the failure mode that bites hardest.
  * blesses — `--listbless` exists but produces no output, under a display or
    without one. Still an open gap.

Usage:
    python -m dom6_assistant.reference.build_index [--game-dir DIR] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_GAME_DIR = Path(
    "<your Steam library>/steamapps/common/Dominions6")
DEFAULT_OUT = Path(__file__).resolve().parents[3] / "knowledge" / "reference"


def _run_list(game_dir: Path, flag: str, timeout: int = 90) -> str:
    """Run one --list switch headlessly and return stdout.

    --nosteam keeps it out of the Steam runtime; no display is needed because
    these switches print and exit before any SDL window is created.
    """
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{env.get('LD_LIBRARY_PATH','')}:{game_dir / 'linux64'}"
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    try:
        res = subprocess.run(
            [str(game_dir / "dom6_amd64"), f"--{flag}", "--nosteam"],
            cwd=str(game_dir), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return ""
    return res.stdout


def parse_spells(text: str) -> list[dict]:
    """`--listspells` emits "   1 Minor Area Shock" — id then name."""
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+(.+?)\s*$", line)
        if m:
            out.append({"id": int(m.group(1)), "name": m.group(2)})
    return out


def parse_events(text: str) -> list[dict]:
    """`--listevents` emits "0  <text>" — ids repeat, text may be truncated."""
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+(.+?)\s*$", line)
        if m:
            out.append({"id": int(m.group(1)), "text": m.group(2)})
    return out


def parse_nations(text: str) -> list[dict]:
    """`--listnations` groups "  5  Arcoscephale, Golden Era" under era headers."""
    out, era = [], None
    for line in text.splitlines():
        m = re.match(r"^-+ Era (\d+) -+", line.strip())
        if m:
            era = int(m.group(1))
            continue
        m = re.match(r"^\s*(\d+)\s+(.+?)(?:,\s*(.+))?$", line.rstrip())
        if m and era:
            out.append({"id": int(m.group(1)), "era": era,
                        "name": m.group(2).strip(),
                        "title": (m.group(3) or "").strip()})
    return out


def build(game_dir: Path, out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for flag, parser, name in (
        ("listspells", parse_spells, "spells"),
        ("listevents", parse_events, "events"),
        ("listnations", parse_nations, "nations"),
    ):
        rows = parser(_run_list(game_dir, flag))
        (out_dir / f"{name}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        counts[name] = len(rows)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    if not (args.game_dir / "dom6_amd64").exists():
        print(f"No dom6_amd64 under {args.game_dir}", file=sys.stderr)
        return 1

    counts = build(args.game_dir, args.out)
    for name, n in counts.items():
        print(f"  {name:<8} {n:>5} entries -> {args.out / (name + '.json')}")
    return 0 if all(counts.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
