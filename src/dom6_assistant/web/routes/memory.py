"""Memory explorer API routes.

GET  /api/memory/maps          — list memory regions
POST /api/memory/scan          — scan for a value (slow, runs in thread pool)
POST /api/memory/narrow        — narrow previous scan results
GET  /api/memory/hexdump       — hex dump at an address
GET  /api/memory/scan/state    — return current scan state
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from dom6_assistant.memory_reader import attach
from dom6_assistant.web import state

router = APIRouter(prefix="/memory", tags=["memory"])

_MAX_ADDRS_RESPONSE = 200   # cap addresses sent to frontend
_MAX_HEXDUMP_SIZE   = 4096


def _get_reader():
    try:
        return attach()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ---------------------------------------------------------------------------
# Memory maps
# ---------------------------------------------------------------------------

class RegionOut(BaseModel):
    start: str
    end: str
    size_bytes: int
    perms: str
    pathname: str


class MapsResponse(BaseModel):
    regions: list[RegionOut]
    count: int


@router.get("/maps", response_model=MapsResponse)
def get_maps(
    filter: str = Query("", description="Only show regions whose pathname contains this"),
    rw_only: bool = Query(False, description="Only show read-write regions"),
) -> MapsResponse:
    reader = _get_reader()
    regions = reader.maps()
    if rw_only:
        regions = [r for r in regions if "rw" in r.perms]
    if filter:
        regions = [r for r in regions if filter in r.pathname]
    out = [
        RegionOut(
            start=hex(r.start),
            end=hex(r.end),
            size_bytes=r.size,
            perms=r.perms,
            pathname=r.pathname,
        )
        for r in regions
    ]
    return MapsResponse(regions=out, count=len(out))


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    value: int
    val_type: Literal["int32", "uint32", "int16"] = "int32"


class ScanResponse(BaseModel):
    count: int
    addresses: list[str]   # hex strings
    truncated: bool
    val_type: str
    value: int


class ScanProgressResponse(BaseModel):
    running: bool
    scanned_bytes: int
    total_bytes: int
    percent: float


@router.get("/scan/progress", response_model=ScanProgressResponse)
def get_scan_progress() -> ScanProgressResponse:
    p = state.get_progress()
    total = p["total_bytes"]
    scanned = p["scanned_bytes"]
    pct = round(scanned / total * 100, 1) if total > 0 else 0.0
    return ScanProgressResponse(
        running=p["running"],
        scanned_bytes=scanned,
        total_bytes=total,
        percent=pct,
    )


@router.post("/scan", response_model=ScanResponse)
async def scan(body: ScanRequest) -> ScanResponse:
    """Scan process memory for a value. Runs in a thread pool (can take seconds)."""
    reader = _get_reader()

    state.set_progress(running=True, scanned=0, total=0)
    try:
        if body.val_type == "int32":
            results = await run_in_threadpool(
                reader.scan_int32, body.value, None, 4, state.update_progress
            )
        elif body.val_type == "uint32":
            results = await run_in_threadpool(
                reader.scan_uint32, body.value, None, 4, state.update_progress
            )
        else:
            results = await run_in_threadpool(
                reader.scan_int16, body.value, None, 2, state.update_progress
            )
    finally:
        state.set_progress(running=False)

    state.update_scan(results, body.val_type, body.value)

    truncated = len(results) > _MAX_ADDRS_RESPONSE
    return ScanResponse(
        count=len(results),
        addresses=[hex(a) for a in results[:_MAX_ADDRS_RESPONSE]],
        truncated=truncated,
        val_type=body.val_type,
        value=body.value,
    )


class NarrowRequest(BaseModel):
    value: int


@router.post("/narrow", response_model=ScanResponse)
async def narrow(body: NarrowRequest) -> ScanResponse:
    """Filter the previous scan result to addresses now holding the new value."""
    current = state.get_scan()
    if not current["addresses"]:
        raise HTTPException(status_code=400, detail="No previous scan to narrow. Run /scan first.")

    reader = _get_reader()
    fmt_map = {"int32": "<i", "uint32": "<I", "int16": "<h"}
    fmt = fmt_map.get(current["val_type"], "<i")

    remaining = await run_in_threadpool(reader.narrow, current["addresses"], body.value, fmt)
    state.update_scan(remaining, current["val_type"], body.value)

    truncated = len(remaining) > _MAX_ADDRS_RESPONSE
    return ScanResponse(
        count=len(remaining),
        addresses=[hex(a) for a in remaining[:_MAX_ADDRS_RESPONSE]],
        truncated=truncated,
        val_type=current["val_type"],
        value=body.value,
    )


class ScanStateResponse(BaseModel):
    addresses: list[str]
    val_type: str
    value: int | None
    count: int


@router.get("/scan/state", response_model=ScanStateResponse)
def get_scan_state() -> ScanStateResponse:
    s = state.get_scan()
    return ScanStateResponse(
        addresses=[hex(a) for a in s["addresses"][:_MAX_ADDRS_RESPONSE]],
        val_type=s["val_type"],
        value=s["value"],
        count=len(s["addresses"]),
    )


# ---------------------------------------------------------------------------
# Hex dump
# ---------------------------------------------------------------------------

class HexDumpResponse(BaseModel):
    address: str
    size: int
    dump: str   # pre-formatted text, display in <pre>


@router.get("/hexdump", response_model=HexDumpResponse)
def hexdump(
    address: str = Query(..., description="Address in hex (e.g. 0x55a3f098b000)"),
    size: int = Query(256, ge=1, le=_MAX_HEXDUMP_SIZE),
) -> HexDumpResponse:
    try:
        addr_int = int(address, 0)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid address: {address!r}")

    reader = _get_reader()
    try:
        dump = reader.hexdump(addr_int, size)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Memory read failed: {e}")

    return HexDumpResponse(address=hex(addr_int), size=size, dump=dump)
