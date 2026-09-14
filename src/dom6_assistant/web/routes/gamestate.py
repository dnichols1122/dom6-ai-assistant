"""Game state API routes.

GET  /api/gamestate        — read live nation economic state from locked address
POST /api/gamestate/lock   — set the gold field address (found via memory scan)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dom6_assistant.memory_reader import attach
from dom6_assistant.web import state

router = APIRouter(prefix="/gamestate", tags=["gamestate"])


class LockRequest(BaseModel):
    address: str   # hex string, e.g. "0x55a3fed21940"


class GemStockpileOut(BaseModel):
    fire: int
    air: int
    water: int
    earth: int
    astral: int
    death: int
    nature: int
    glamour: int
    blood: int


class GameStateResponse(BaseModel):
    locked: bool
    address: str | None = None
    gold: int | None = None
    nation_id: int | None = None
    gems: GemStockpileOut | None = None


@router.post("/lock")
def lock_address(body: LockRequest) -> dict:
    """Store the gold field address so /api/gamestate can read live data."""
    try:
        addr = int(body.address, 0)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid address: {body.address!r}")
    state.set_game_state_address(addr)
    return {"address": hex(addr)}


@router.get("", response_model=GameStateResponse)
def get_game_state() -> GameStateResponse:
    """Return live nation economic state. Returns locked=False if no address locked."""
    addr = state.get_game_state_address()
    if addr is None:
        return GameStateResponse(locked=False)

    try:
        reader = attach()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        ns = reader.read_nation_state(addr)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Memory read failed: {e}")

    g = ns.gems
    return GameStateResponse(
        locked=True,
        address=hex(addr),
        gold=ns.gold,
        nation_id=ns.nation_id,
        gems=GemStockpileOut(
            fire=g.fire, air=g.air, water=g.water, earth=g.earth,
            astral=g.astral, death=g.death, nature=g.nature,
            glamour=g.glamour, blood=g.blood,
        ),
    )
