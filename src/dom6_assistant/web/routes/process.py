"""GET /api/process — dom6 process status."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from dom6_assistant.memory_reader import find_process

router = APIRouter(prefix="/process", tags=["process"])


class MemoryInfo(BaseModel):
    rss_bytes: int
    vms_bytes: int


class ProcessStatus(BaseModel):
    running: bool
    pid: int | None
    name: str | None
    memory: MemoryInfo | None


@router.get("/status", response_model=ProcessStatus)
def get_status() -> ProcessStatus:
    proc = find_process()
    if proc is None:
        return ProcessStatus(running=False, pid=None, name=None, memory=None)
    try:
        mi = proc.memory_info()
        mem = MemoryInfo(rss_bytes=mi.rss, vms_bytes=mi.vms)
    except Exception:
        mem = None
    return ProcessStatus(running=True, pid=proc.pid, name=proc.name(), memory=mem)
