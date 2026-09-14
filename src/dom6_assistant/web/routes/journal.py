"""Game journal API routes.

POST /api/journal/turns              — upsert global turn data (treasury, income, gems)
GET  /api/journal/turns              — list all recorded turns (summary)
GET  /api/journal/turns/{n}          — full data for one turn

GET  /api/journal/provinces          — list provinces with latest snapshot
POST /api/journal/provinces          — create province
POST /api/journal/provinces/{id}/snapshot — upsert province data for a turn
PUT  /api/journal/provinces/{id}/sites    — replace site list
DELETE /api/journal/provinces/{id}        — remove province
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dom6_assistant.journal.db import get_db
from dom6_assistant.web import state

router = APIRouter(prefix="/journal", tags=["journal"])


# ─────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────

class GemIn(BaseModel):
    fire:   Optional[int] = None
    water:  Optional[int] = None   # confirmed idx1
    nature: Optional[int] = None   # confirmed idx2
    earth:  Optional[int] = None   # idx 3, 4, or 7 — mapping TBD
    death:  Optional[int] = None   # idx 3, 4, or 7 — mapping TBD
    astral: Optional[int] = None
    air:    Optional[int] = None
    blood:  Optional[int] = None   # idx 3, 4, or 7 — mapping TBD


class TurnIn(BaseModel):
    turn_number:    int
    treasury_start: Optional[int] = None
    treasury_end:   Optional[int] = None
    total_income:   Optional[int] = None
    upkeep:         Optional[int] = None
    gems:           Optional[GemIn] = None


class TurnOut(BaseModel):
    turn_number:    int
    treasury_start: Optional[int]
    treasury_end:   Optional[int]
    total_income:   Optional[int]
    upkeep:         Optional[int]
    recorded_at:    Optional[str]
    gems:           Optional[GemIn]


class ProvinceIn(BaseModel):
    name:            str
    is_capital:      bool = False
    province_number: Optional[int] = None


class SnapshotIn(BaseModel):
    turn_number:          int
    terrain:              Optional[str] = None
    population:           Optional[int] = None
    income:               Optional[int] = None
    resources:            Optional[int] = None
    recruitment_points:   Optional[int] = None
    recruitment_per_turn: Optional[int] = None
    supplies:             Optional[int] = None
    supply_usage:         Optional[int] = None
    defense:              Optional[int] = None
    unrest:               Optional[int] = None
    dominion_strength:    Optional[int] = None
    scale_order:          Optional[int] = None
    scale_productivity:   Optional[int] = None
    scale_heat:           Optional[int] = None
    scale_growth:         Optional[int] = None
    scale_luck:           Optional[int] = None
    scale_magic:          Optional[int] = None


class SiteIn(BaseModel):
    name:      str
    site_type: Optional[str] = None   # 'building' | 'site' | null


class SitesIn(BaseModel):
    sites: list[SiteIn]


class SnapshotOut(SnapshotIn):
    pass


class ProvinceOut(BaseModel):
    id:              int
    name:            str
    is_capital:      bool
    province_number: Optional[int]
    latest:          Optional[SnapshotOut]
    sites:           list[SiteIn]


# ─────────────────────────────────────────────────────────────
# Turns
# ─────────────────────────────────────────────────────────────

@router.post("/turns", response_model=TurnOut)
def upsert_turn(body: TurnIn) -> TurnOut:
    con = get_db()
    with con:
        con.execute("""
            INSERT INTO turns (turn_number, treasury_start, treasury_end, total_income, upkeep)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(turn_number) DO UPDATE SET
                treasury_start = excluded.treasury_start,
                treasury_end   = excluded.treasury_end,
                total_income   = excluded.total_income,
                upkeep         = excluded.upkeep,
                recorded_at    = datetime('now')
        """, (body.turn_number, body.treasury_start, body.treasury_end,
              body.total_income, body.upkeep))

        if body.gems is not None:
            g = body.gems
            con.execute("""
                INSERT INTO gem_snapshots
                    (turn_number, fire, water, nature, earth, death, astral, air, blood)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_number) DO UPDATE SET
                    fire=excluded.fire, water=excluded.water, nature=excluded.nature,
                    earth=excluded.earth, death=excluded.death,
                    astral=excluded.astral, air=excluded.air, blood=excluded.blood
            """, (body.turn_number, g.fire, g.water, g.nature, g.earth,
                  g.death, g.astral, g.air, g.blood))

    _post_turn_hooks(con, body)
    return _fetch_turn(con, body.turn_number)


@router.get("/turns", response_model=list[TurnOut])
def list_turns() -> list[TurnOut]:
    con = get_db()
    rows = con.execute(
        "SELECT turn_number FROM turns ORDER BY turn_number DESC"
    ).fetchall()
    return [_fetch_turn(con, r["turn_number"]) for r in rows]


@router.get("/turns/{turn_number}", response_model=TurnOut)
def get_turn(turn_number: int) -> TurnOut:
    con = get_db()
    row = con.execute(
        "SELECT 1 FROM turns WHERE turn_number=?", (turn_number,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Turn {turn_number} not recorded")
    return _fetch_turn(con, turn_number)


def _fetch_turn(con, turn_number: int) -> TurnOut:
    t = con.execute(
        "SELECT * FROM turns WHERE turn_number=?", (turn_number,)
    ).fetchone()
    g = con.execute(
        "SELECT * FROM gem_snapshots WHERE turn_number=?", (turn_number,)
    ).fetchone()
    gems = GemIn(**dict(g)) if g else None
    return TurnOut(
        turn_number=t["turn_number"],
        treasury_start=t["treasury_start"],
        treasury_end=t["treasury_end"],
        total_income=t["total_income"],
        upkeep=t["upkeep"],
        recorded_at=t["recorded_at"],
        gems=gems,
    )


def _post_turn_hooks(con, body: TurnIn) -> None:
    """Side-effects triggered after a turn is saved:
    1. Capture legacy F10/F14/F20 memory samples from the locked gold address.
    2. Auto-narrow field correlation candidates for any resolved scans.
    Both are best-effort — failures are silently swallowed.
    """
    gold_addr = state.get_game_state_address()
    if gold_addr is None:
        return

    try:
        from dom6_assistant.memory_reader import attach
        reader = attach()
    except Exception:
        return

    # 1. Gem snapshot
    try:
        s3 = reader.read_int32(gold_addr + 0xF10)
        s4 = reader.read_int32(gold_addr + 0xF14)
        s7 = reader.read_int32(gold_addr + 0xF20)
        with con:
            con.execute("""
                INSERT INTO memory_snapshots (turn_number, gem_slot_3, gem_slot_4, gem_slot_7)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(turn_number) DO UPDATE SET
                    gem_slot_3 = excluded.gem_slot_3,
                    gem_slot_4 = excluded.gem_slot_4,
                    gem_slot_7 = excluded.gem_slot_7,
                    captured_at = datetime('now')
            """, (body.turn_number, s3, s4, s7))
    except Exception:
        pass

    # 2. Auto-narrow field correlations (only if candidates exist and no manual override)
    _SCANNABLE = {
        "total_income": ("total_income", "<i"),
        "upkeep":       ("upkeep",       "<i"),
    }
    for field_name, (attr, fmt) in _SCANNABLE.items():
        value = getattr(body, attr, None)
        if value is None:
            continue
        row = con.execute(
            "SELECT candidates FROM field_correlations WHERE field_name=?",
            (field_name,),
        ).fetchone()
        if row is None:
            continue
        try:
            prev = json.loads(row["candidates"])
            new_cands = reader.narrow(prev, value, fmt)
            locked = new_cands[0] if len(new_cands) == 1 else None
            with con:
                con.execute("""
                    UPDATE field_correlations
                    SET candidates=?, locked_addr=?, updated_at=datetime('now')
                    WHERE field_name=?
                """, (json.dumps(new_cands), locked, field_name))
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# Provinces
# ─────────────────────────────────────────────────────────────

@router.get("/provinces", response_model=list[ProvinceOut])
def list_provinces() -> list[ProvinceOut]:
    con = get_db()
    rows = con.execute("SELECT id FROM provinces ORDER BY is_capital DESC, name").fetchall()
    return [_fetch_province(con, r["id"]) for r in rows]


@router.post("/provinces", response_model=ProvinceOut, status_code=201)
def create_province(body: ProvinceIn) -> ProvinceOut:
    con = get_db()
    try:
        with con:
            cur = con.execute(
                "INSERT INTO provinces (name, is_capital, province_number) VALUES (?, ?, ?)",
                (body.name, int(body.is_capital), body.province_number),
            )
        return _fetch_province(con, cur.lastrowid)
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail=f"Province '{body.name}' already exists")
        raise


@router.delete("/provinces/{province_id}", status_code=204)
def delete_province(province_id: int) -> None:
    con = get_db()
    row = con.execute("SELECT 1 FROM provinces WHERE id=?", (province_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Province not found")
    with con:
        con.execute("DELETE FROM provinces WHERE id=?", (province_id,))


@router.post("/provinces/{province_id}/snapshot", response_model=SnapshotOut)
def upsert_snapshot(province_id: int, body: SnapshotIn) -> SnapshotOut:
    con = get_db()
    if con.execute("SELECT 1 FROM provinces WHERE id=?", (province_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="Province not found")

    fields = body.model_dump(exclude={"turn_number"})
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" * len(fields))
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
    vals = list(fields.values())

    with con:
        con.execute(f"""
            INSERT INTO province_snapshots (province_id, turn_number, {cols})
            VALUES (?, ?, {placeholders})
            ON CONFLICT(province_id, turn_number) DO UPDATE SET {updates}
        """, [province_id, body.turn_number] + vals)

    return body


@router.put("/provinces/{province_id}/sites", response_model=list[SiteIn])
def set_sites(province_id: int, body: SitesIn) -> list[SiteIn]:
    con = get_db()
    if con.execute("SELECT 1 FROM provinces WHERE id=?", (province_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="Province not found")
    with con:
        con.execute("DELETE FROM province_sites WHERE province_id=?", (province_id,))
        for site in body.sites:
            con.execute(
                "INSERT OR IGNORE INTO province_sites (province_id, name, site_type) VALUES (?,?,?)",
                (province_id, site.name, site.site_type),
            )
    return body.sites


def _fetch_province(con, province_id: int) -> ProvinceOut:
    p = con.execute("SELECT * FROM provinces WHERE id=?", (province_id,)).fetchone()
    snap = con.execute("""
        SELECT * FROM province_snapshots
        WHERE province_id=?
        ORDER BY turn_number DESC LIMIT 1
    """, (province_id,)).fetchone()
    sites = con.execute(
        "SELECT name, site_type FROM province_sites WHERE province_id=? ORDER BY name",
        (province_id,),
    ).fetchall()
    return ProvinceOut(
        id=p["id"],
        name=p["name"],
        is_capital=bool(p["is_capital"]),
        province_number=p["province_number"],
        latest=SnapshotOut(**dict(snap)) if snap else None,
        sites=[SiteIn(name=s["name"], site_type=s["site_type"]) for s in sites],
    )
