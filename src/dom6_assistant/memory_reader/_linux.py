"""Linux memory reader using /proc/<pid>/mem.

Provides:
- MemoryRegion   — parsed entry from /proc/<pid>/maps
- LinuxMemoryReader — read/scan the process address space
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, Optional

import psutil

from dom6_assistant.models import GemStockpile, NationEconomicState


@dataclass
class MemoryRegion:
    start: int
    end: int
    perms: str          # e.g. "rw-p"
    offset: int
    dev: str
    inode: int
    pathname: str       # mapped file path or "" for anonymous

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def readable(self) -> bool:
        return "r" in self.perms

    @property
    def writable(self) -> bool:
        return "w" in self.perms

    @property
    def is_anonymous(self) -> bool:
        return self.pathname == "" or self.pathname.startswith("[")

    @property
    def is_heap(self) -> bool:
        return self.pathname == "[heap]"

    @property
    def is_stack(self) -> bool:
        return self.pathname == "[stack]"

    def __repr__(self) -> str:
        label = self.pathname or "<anon>"
        return f"<MemoryRegion {self.start:#x}–{self.end:#x} {self.perms} {label}>"


def _parse_maps(pid: int) -> list[MemoryRegion]:
    regions: list[MemoryRegion] = []
    with open(f"/proc/{pid}/maps") as f:
        for line in f:
            parts = line.split()
            addr_range = parts[0].split("-")
            start = int(addr_range[0], 16)
            end = int(addr_range[1], 16)
            perms = parts[1]
            offset = int(parts[2], 16)
            dev = parts[3]
            inode = int(parts[4])
            pathname = parts[5] if len(parts) > 5 else ""
            regions.append(MemoryRegion(start, end, perms, offset, dev, inode, pathname))
    return regions


_CHUNK = 4 * 1024 * 1024  # 4 MiB read chunks during scanning


class LinuxMemoryReader:
    """Reads and scans the memory of a running dom6 process on Linux."""

    def __init__(self, process: psutil.Process) -> None:
        self._pid = process.pid
        self._mem_path = f"/proc/{self._pid}/mem"
        self._maps_path = f"/proc/{self._pid}/maps"

    # ------------------------------------------------------------------
    # Region enumeration
    # ------------------------------------------------------------------

    def maps(self) -> list[MemoryRegion]:
        """Return all memory regions from /proc/<pid>/maps."""
        return _parse_maps(self._pid)

    def readable_regions(self) -> list[MemoryRegion]:
        """Return only readable, non-special regions (heap + anonymous rw)."""
        return [r for r in self.maps() if r.readable and not r.perms.endswith("s")]

    def heap_regions(self) -> list[MemoryRegion]:
        return [r for r in self.maps() if r.is_heap or (r.is_anonymous and r.readable)]

    # ------------------------------------------------------------------
    # Raw reads
    # ------------------------------------------------------------------

    def read_bytes(self, address: int, size: int) -> bytes:
        with open(self._mem_path, "rb") as f:
            f.seek(address)
            return f.read(size)

    def read_uint8(self, address: int) -> int:
        return struct.unpack("B", self.read_bytes(address, 1))[0]

    def read_uint16(self, address: int) -> int:
        return struct.unpack("<H", self.read_bytes(address, 2))[0]

    def read_int16(self, address: int) -> int:
        return struct.unpack("<h", self.read_bytes(address, 2))[0]

    def read_uint32(self, address: int) -> int:
        return struct.unpack("<I", self.read_bytes(address, 4))[0]

    def read_int32(self, address: int) -> int:
        return struct.unpack("<i", self.read_bytes(address, 4))[0]

    def read_uint64(self, address: int) -> int:
        return struct.unpack("<Q", self.read_bytes(address, 8))[0]

    def read_int64(self, address: int) -> int:
        return struct.unpack("<q", self.read_bytes(address, 8))[0]

    def read_cstring(self, address: int, max_len: int = 256) -> str:
        """Read a null-terminated C string."""
        raw = self.read_bytes(address, max_len)
        end = raw.find(b"\x00")
        return raw[:end].decode("latin-1") if end != -1 else raw.decode("latin-1")

    # ------------------------------------------------------------------
    # Value scanning (Cheat Engine style)
    # ------------------------------------------------------------------

    def _iter_region_chunks(
        self,
        regions: list[MemoryRegion],
        progress_cb: Optional[object] = None,
    ) -> Iterator[tuple[int, bytes]]:
        """Yield (base_address, chunk_bytes) for all given regions.

        Args:
            regions: Regions to read.
            progress_cb: Optional callable(scanned_bytes, total_bytes) called
                         after each chunk, for progress reporting.
        """
        total = sum(r.size for r in regions)
        scanned = 0
        with open(self._mem_path, "rb") as f:
            for region in regions:
                offset = 0
                while offset < region.size:
                    chunk_size = min(_CHUNK, region.size - offset)
                    addr = region.start + offset
                    try:
                        f.seek(addr)
                        data = f.read(chunk_size)
                        if data:
                            yield addr, data
                    except OSError:
                        pass  # region may have disappeared
                    offset += chunk_size
                    scanned += chunk_size
                    if progress_cb is not None:
                        progress_cb(scanned, total)

    def _scan(
        self,
        needle: bytes,
        regions: list[MemoryRegion],
        alignment: int,
        progress_cb: Optional[object] = None,
    ) -> list[int]:
        """Core scan using bytes.find() — C-speed substring search.

        bytes.find() is orders of magnitude faster than a Python loop because
        it runs in C. For a 2 GiB region this drops scan time from minutes
        to a few seconds.
        """
        results: list[int] = []
        for base, chunk in self._iter_region_chunks(regions, progress_cb):
            search_from = 0
            while True:
                idx = chunk.find(needle, search_from)
                if idx == -1:
                    break
                addr = base + idx
                if addr % alignment == 0:
                    results.append(addr)
                search_from = idx + 1   # advance by 1 to catch every aligned hit
        return results

    def scan_int32(
        self,
        value: int,
        regions: Optional[list[MemoryRegion]] = None,
        alignment: int = 4,
        progress_cb: Optional[object] = None,
    ) -> list[int]:
        """Find all addresses in readable memory that hold the given int32 value.

        Args:
            value: The 32-bit signed integer to search for.
            regions: Regions to scan; defaults to all heap + anonymous rw regions.
            alignment: Only check addresses aligned to this boundary (default 4).
            progress_cb: Optional callable(scanned_bytes, total_bytes).

        Returns:
            List of matching addresses.
        """
        return self._scan(
            struct.pack("<i", value),
            regions or self.heap_regions(),
            alignment,
            progress_cb,
        )

    def scan_uint32(
        self,
        value: int,
        regions: Optional[list[MemoryRegion]] = None,
        alignment: int = 4,
        progress_cb: Optional[object] = None,
    ) -> list[int]:
        """Find all addresses in readable memory that hold the given uint32 value."""
        return self._scan(
            struct.pack("<I", value),
            regions or self.heap_regions(),
            alignment,
            progress_cb,
        )

    def scan_int16(
        self,
        value: int,
        regions: Optional[list[MemoryRegion]] = None,
        alignment: int = 2,
        progress_cb: Optional[object] = None,
    ) -> list[int]:
        """Find all addresses holding the given int16 value."""
        return self._scan(
            struct.pack("<h", value),
            regions or self.heap_regions(),
            alignment,
            progress_cb,
        )

    def narrow(self, addresses: list[int], value: int, fmt: str = "<i") -> list[int]:
        """From a previous scan result, keep only addresses that now hold *value*.

        Args:
            addresses: Candidate addresses from a previous scan.
            value: Expected current value.
            fmt: struct format string for the value type (default '<i' = int32).

        Returns:
            Filtered list of addresses still matching.
        """
        size = struct.calcsize(fmt)
        needle = struct.pack(fmt, value)
        matches: list[int] = []
        with open(self._mem_path, "rb") as f:
            for addr in addresses:
                try:
                    f.seek(addr)
                    if f.read(size) == needle:
                        matches.append(addr)
                except OSError:
                    pass
        return matches

    # ------------------------------------------------------------------
    # High-level game state reading
    # ------------------------------------------------------------------

    # Offsets within the nation struct, relative to the gold field.
    # Confirmed by memory scanning across 5 turns of gameplay.
    _OFF_NATION_ID  = 0x00C   # int16
    # All nine were proved simultaneously with distinct values in the debug
    # game.  Earlier partial correlations mislabeled five of these slots.
    _OFF_GEM_FIRE    = 0xF04
    _OFF_GEM_AIR     = 0xF08
    _OFF_GEM_WATER   = 0xF0C
    _OFF_GEM_EARTH   = 0xF10
    _OFF_GEM_ASTRAL  = 0xF14
    _OFF_GEM_DEATH   = 0xF18
    _OFF_GEM_NATURE  = 0xF1C
    _OFF_GEM_GLAMOUR = 0xF20
    _OFF_GEM_BLOOD   = 0xF24

    def read_nation_state(self, gold_address: int) -> NationEconomicState:
        """Read live nation economic state from a known gold field address."""
        gold      = self.read_int32(gold_address)
        nation_id = self.read_int16(gold_address + self._OFF_NATION_ID)
        gems = GemStockpile(
            fire=self.read_int32(gold_address + self._OFF_GEM_FIRE),
            air=self.read_int32(gold_address + self._OFF_GEM_AIR),
            water=self.read_int32(gold_address + self._OFF_GEM_WATER),
            earth=self.read_int32(gold_address + self._OFF_GEM_EARTH),
            astral=self.read_int32(gold_address + self._OFF_GEM_ASTRAL),
            death=self.read_int32(gold_address + self._OFF_GEM_DEATH),
            nature=self.read_int32(gold_address + self._OFF_GEM_NATURE),
            glamour=self.read_int32(gold_address + self._OFF_GEM_GLAMOUR),
            blood=self.read_int32(gold_address + self._OFF_GEM_BLOOD),
        )
        return NationEconomicState(gold=gold, nation_id=nation_id, gems=gems)

    def hexdump(self, address: int, size: int = 256, width: int = 16) -> str:
        """Return a hex + ASCII dump of memory at *address* for *size* bytes."""
        data = self.read_bytes(address, size)
        lines: list[str] = []
        for i in range(0, len(data), width):
            row = data[i : i + width]
            hex_part = " ".join(f"{b:02x}" for b in row)
            asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            lines.append(f"{address + i:#018x}  {hex_part:<{width * 3}}  {asc_part}")
        return "\n".join(lines)
