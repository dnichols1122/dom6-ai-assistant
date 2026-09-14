"""Windows memory reader using pymem."""

from __future__ import annotations

import struct

import psutil

try:
    import pymem
except ImportError as e:
    raise ImportError("pymem is required on Windows: pip install pymem") from e


class WindowsMemoryReader:
    """Reads memory from a running dom6 process on Windows via pymem."""

    def __init__(self, process: psutil.Process) -> None:
        self._pm = pymem.Pymem(process.name())

    def read_bytes(self, address: int, size: int) -> bytes:
        return self._pm.read_bytes(address, size)

    def read_uint32(self, address: int) -> int:
        return self._pm.read_uint(address)

    def read_int32(self, address: int) -> int:
        return self._pm.read_int(address)

    def read_uint16(self, address: int) -> int:
        raw = self.read_bytes(address, 2)
        return struct.unpack("<H", raw)[0]
