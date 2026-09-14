"""Read and interpret the capture files produced by trn_hook.so.

Output files produced by the hook for each .trn write:
  /tmp/dom6_trn_<nation>.bin  — raw bytes (identical to the .trn file)
  /tmp/dom6_trn_<nation>.log  — one line per fwrite call
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class WriteRecord:
    offset: int
    length: int
    caller_rva: int
    data: bytes          # up to 16 bytes (preview from log); full data in .bin


@dataclass
class HookCapture:
    nation: str
    load_base: int
    records: list[WriteRecord] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def bin_path(self) -> Path:
        return Path(f"/tmp/dom6_trn_{self.nation}.bin")

    @property
    def log_path(self) -> Path:
        return Path(f"/tmp/dom6_trn_{self.nation}.log")

    def raw_bytes(self) -> bytes:
        """Return the full captured .trn bytes."""
        return self.bin_path.read_bytes()

    # Convenience: group records by caller_rva
    def by_caller(self) -> dict[int, list[WriteRecord]]:
        out: dict[int, list[WriteRecord]] = {}
        for r in self.records:
            out.setdefault(r.caller_rva, []).append(r)
        return out

    def caller_summary(self) -> list[tuple[int, int, int]]:
        """Return (caller_rva, call_count, total_bytes) sorted by first occurrence."""
        seen: dict[int, list[int]] = {}
        for r in self.records:
            seen.setdefault(r.caller_rva, []).append(r.length)
        return [
            (rva, len(sizes), sum(sizes))
            for rva, sizes in seen.items()
        ]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_LOG_RE = re.compile(
    r"off=0x(?P<off>[0-9a-f]+)"
    r"\s+len=\s*(?P<len>\d+)"
    r"\s+caller_rva=0x(?P<rva>[0-9a-f]+)"
    r"\s+data=(?P<data>[0-9a-f ]*)"
)
_BASE_RE = re.compile(r"#\s*load_base=0x(?P<base>[0-9a-f]+)")


def parse_log(log_path: Path) -> HookCapture:
    """Parse a /tmp/dom6_trn_<nation>.log file into a HookCapture."""
    nation = log_path.stem.removeprefix("dom6_trn_")
    load_base = 0
    records: list[WriteRecord] = []

    for line in log_path.read_text().splitlines():
        m = _BASE_RE.match(line)
        if m:
            load_base = int(m.group("base"), 16)
            continue

        m = _LOG_RE.match(line)
        if not m:
            continue

        data_bytes = bytes(
            int(x, 16) for x in m.group("data").split() if x
        )
        records.append(WriteRecord(
            offset=int(m.group("off"), 16),
            length=int(m.group("len")),
            caller_rva=int(m.group("rva"), 16),
            data=data_bytes,
        ))

    total = records[-1].offset + records[-1].length if records else 0
    cap = HookCapture(nation=nation, load_base=load_base,
                      records=records, total_bytes=total)
    return cap


def load_latest(nation: str | None = None) -> list[HookCapture]:
    """Load all (or a named) hook capture from /tmp."""
    logs = sorted(Path("/tmp").glob("dom6_trn_*.log"))
    if nation:
        logs = [p for p in logs if p.stem == f"dom6_trn_{nation}"]
    return [parse_log(p) for p in logs if p.stat().st_size > 0]


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

def print_summary(cap: HookCapture) -> None:
    print(f"Nation:     {cap.nation}")
    print(f"Load base:  0x{cap.load_base:x}")
    print(f"Records:    {len(cap.records)}")
    print(f"Total bytes:{cap.total_bytes:,}")
    print()
    print(f"{'Caller RVA':<16} {'Calls':>6} {'Bytes':>10}  First offset")
    print("-" * 55)
    first_off: dict[int, int] = {}
    for r in cap.records:
        first_off.setdefault(r.caller_rva, r.offset)
    for rva, calls, nbytes in cap.caller_summary():
        print(f"0x{rva:06x}        {calls:>6} {nbytes:>10}  0x{first_off[rva]:06x}")


def find_offset(cap: HookCapture, file_offset: int) -> WriteRecord | None:
    """Find the write record that covers a given file offset."""
    for r in cap.records:
        if r.offset <= file_offset < r.offset + r.length:
            return r
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    nation = sys.argv[1] if len(sys.argv) > 1 else None
    captures = load_latest(nation)

    if not captures:
        print("No hook captures found in /tmp. Run with LD_PRELOAD=trn_hook.so first.")
        sys.exit(1)

    for cap in captures:
        print_summary(cap)
        print()

        # Cross-reference known offsets from trn_format.md knowledge base
        # (these are Pangaea Mid, debug_enabled_nosteam, turn 4)
        known = {
            0x146bf: "gem_base (fire slot)",
            0x137ce: "treasury",
            0x14333: "FF block start",
        }
        print("Known offset cross-reference:")
        for off, label in known.items():
            r = find_offset(cap, off)
            if r:
                print(f"  0x{off:06x}  {label:<30} → caller_rva=0x{r.caller_rva:06x}")
            else:
                print(f"  0x{off:06x}  {label:<30} → not found in this capture")
