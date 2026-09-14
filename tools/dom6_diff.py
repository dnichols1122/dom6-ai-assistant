#!/usr/bin/env python3
"""Diff two .trn snapshots by RECORD IDENTITY, not byte position.

Byte diffing these files is useless once anything changes size: a turn with a
battle is 245KB against 150KB the turn after, everything realigns, and a naive
comparison reports hundreds of thousands of differing bytes. Chunk hashing fails
for the same reason. Both were tried.

This locates each record independently in both files and compares only fields
within a record, so a shift anywhere else in the file cannot produce a false
difference.

Usage:
    dom6_diff.py <before.trn> <after.trn>              # summary of every province
    dom6_diff.py <before.trn> <after.trn> --prov 85    # one province, every byte
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dom6_assistant.file_reader.formats import trn as T  # noqa: E402


def province_bodies(path: Path) -> dict[int, tuple[object, int, bytes]]:
    """{province id: (parsed, body offset, file bytes)} for every province."""
    data = path.read_bytes()
    out: dict[int, tuple[object, int, bytes]] = {}
    for rec_off, _pid in T._find_province_records(data):
        try:
            prov, body = T._parse_province(data, rec_off)
        except Exception:
            continue
        if prov:
            out[prov.province_id] = (prov, body, data)
    return out


FIELDS = ("owner_nation_id", "population", "unrest", "province_defense",
          "dominion_owner", "dominion_strength", "fort_type", "has_temple",
          "has_laboratory", "commanders_queued", "troops_queued",
          "wall_integrity", "under_construction")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 1
    before, after = province_bodies(Path(argv[1])), province_bodies(Path(argv[2]))

    if "--prov" in argv:
        pid = int(argv[argv.index("--prov") + 1])
        if pid not in before or pid not in after:
            print(f"province {pid} missing from one side", file=sys.stderr)
            return 2
        _pa, ba, da = before[pid]
        _pb, bb, db = after[pid]
        # Every byte of the record body, so an unknown field cannot hide.
        diffs = [(o, da[ba + o], db[bb + o]) for o in range(0, 200)
                 if ba + o < len(da) and bb + o < len(db)
                 and da[ba + o] != db[bb + o]]
        print(f"province {pid}: {len(diffs)} body bytes changed")
        for o, x, y in diffs:
            print(f"  body+{o:<4} {x:>4} -> {y:<4}   (delta {y - x:+d})")
        return 0

    for pid in sorted(set(before) & set(after)):
        pa, _ba, _da = before[pid]
        pb, _bb, _db = after[pid]
        changed = [f"{f} {getattr(pa, f)}->{getattr(pb, f)}"
                   for f in FIELDS
                   if getattr(pa, f, None) != getattr(pb, f, None)]
        if changed:
            print(f"{pid:>4} {(pa.name or '')[:18]:<19} {'; '.join(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
