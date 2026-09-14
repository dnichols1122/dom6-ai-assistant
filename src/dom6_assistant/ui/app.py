"""Web UI for inspecting parsed game state.

Purpose is verification, not decoration: the player reads this next to the game
and confirms that what came out of the .trn matches what the game shows. That
is why every field carries a confidence marker — the point is to direct
attention at the numbers that are still guesses rather than ask for everything
to be checked equally.

FastAPI + Jinja + HTMX, deliberately: the data changes once per turn and is
dense and tabular, so a SPA would add a build step and a client state layer to
render what is essentially static between turns. HTMX gives partial swaps with
no bundler.

    uvicorn dom6_assistant.ui.app:app --reload
"""
from __future__ import annotations

import sqlite3
import html
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..file_reader.formats.mapfile import BORDER_TYPES

ROOT = Path(__file__).resolve().parents[3]
GAME_DB = ROOT / "knowledge" / "game.sqlite3"
REF_DB = ROOT / "knowledge" / "reference" / "reference.sqlite3"
HERE = Path(__file__).parent

from ..gamestate import service as ingest_service

# Turns are ingested for as long as this server runs. Doing it by hand was a
# step easy to forget, and forgetting it is silent: the assistant answers
# confidently from the previous turn.
app = FastAPI(title="Dominions 6 Assistant", lifespan=ingest_service.lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

# How much each decoded field is actually trusted. Rendered as a marker next to
# the value so the player knows what is worth checking against the game.
#   confirmed  cross-checked against a known value
#   derived    matches reference data, but every sample shared one value
#   guess      inferred from range or behaviour, not verified
CONFIDENCE = {
    "type_id": "confirmed", "nation_id": "confirmed", "instance_id": "confirmed",
    "unit_name": "confirmed",
    # Costs are observed from the game's own .2h, never computed.
    "gold": "confirmed",
    "size": "derived",

    # Checked against the live game on 2026-08-12, turn 1 of
    # example_game, by reading the province screen side by side.
    "age": "confirmed",           # per-unit ages on the unit sheets matched
    "population": "confirmed",    # 39650
    "unrest": "confirmed",        # 0
    "scales": "confirmed",        # Order 2, Productivity 1
    "dominion": "confirmed",      # strength 1
    "gems": "confirmed",          # 4 fire, 1 astral
    "hp": "confirmed",            # checked on the unit sheets
    "sites": "confirmed",         # House of Fiery Justice + Royal Academy
    "gem_income": "confirmed",    # 4 fire + 1 astral, from the site's own yield
    "treasury": "confirmed",      # 600 gold
    "terrain": "confirmed",       # Plains; and all 99 provinces against the .map
    "neighbours": "confirmed",    # 83, 86, 91, 98 against the .map
    "province_name": "confirmed", "owner": "confirmed",

    # 25 in the one province we own and 0 in the other 165. Right value, but a
    # single observation — settled once we hold two provinces with different PD.
    "province_defense": "derived",
}

# Values the game shows that the .trn does not contain. Each was searched for
# across the whole file as u8/u16/i16/u32 and is simply absent, so the client
# computes them at display time from population, scales, terrain, forts and
# sites. Listed explicitly because a missing row invites the assumption that
# nobody looked.
NOT_STORED = [
    ("Income", 560), ("Resources", 144), ("Recruitment points", 477),
    ("Supplies", 1280), ("Supply usage", 37),
]

# body+41 and body+42 are Laboratory and Temple; which is which is unresolved.
BUILDING_LABEL = "Laboratory / Temple (not yet separated)"


def terrain_names(conn: sqlite3.Connection, flags: int) -> str:
    """Decode a terrain bitmask using the game's own table.

    Values above the documented bit range mean the record's tail was read at
    the wrong offset — about 1.3% of provinces across the sample, in saves with
    no .map file to resolve the shift against. Say so rather than print the
    number as though it meant something.
    """
    if flags > (1 << 27) - 1:
        return "not decoded"
    if flags == 0:
        return "Plains"
    rows = conn.execute(
        "SELECT bit_value, bit_name FROM ref.map_terrain_types ORDER BY bit_value"
    ).fetchall()
    out = [r["bit_name"] for r in rows
           if r["bit_value"] and (flags & r["bit_value"])
           and not r["bit_name"].startswith("<")]
    return ", ".join(out) or f"unknown ({flags})"


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{GAME_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE 'file:{REF_DB}?mode=ro' AS ref")
    return conn


def _current(conn: sqlite3.Connection, game_id: int | None, turn: int | None):
    """Resolve (game row, turn row), defaulting to the newest turn of the newest game."""
    games = conn.execute("SELECT * FROM games ORDER BY id").fetchall()
    if not games:
        return None, None, []
    game = next((g for g in games if g["id"] == game_id), games[-1])
    turns = conn.execute(
        "SELECT * FROM turns WHERE game_id=? ORDER BY turn", (game["id"],)).fetchall()
    if not turns:
        return game, None, games
    t = next((x for x in turns if x["turn"] == turn), turns[-1])
    return game, t, games


def _context(request: Request, game_id: int | None, turn: int | None) -> dict:
    conn = db()
    try:
        game, t, games = _current(conn, game_id, turn)
        if not game or not t:
            return {"request": request, "games": games, "game": game,
                    "turn": None, "turns": [], "conf": CONFIDENCE}

        turns = conn.execute("SELECT turn FROM turns WHERE game_id=? ORDER BY turn",
                             (game["id"],)).fetchall()
        nation = conn.execute(
            "SELECT name, epithet, era FROM ref.nations WHERE id=?",
            (game["nation_id"],)).fetchone()

        army = conn.execute("""
            SELECT u.type_id, COUNT(*) AS n, ru.name AS unit_name,
                   uc.gold, MIN(u.hp) AS hp_min, MAX(u.hp) AS hp_max,
                   MIN(u.age) AS age_min, MAX(u.age) AS age_max, ru.size
            FROM units u
            LEFT JOIN ref.units ru ON ru.id = u.type_id
            LEFT JOIN observed_unit_costs uc ON uc.unit_type_id = u.type_id
            WHERE u.turn_id = ?
            GROUP BY u.type_id ORDER BY n DESC, ru.name
        """, (t["id"],)).fetchall()

        provinces = conn.execute("""
            SELECT * FROM provinces WHERE turn_id=? AND owner_nation_id=?
            ORDER BY is_capital DESC, province_id
        """, (t["id"], game["nation_id"])).fetchall()

        # Provinces we can see but do not own — the scouting picture.
        visible = conn.execute("""
            SELECT COUNT(*) AS n FROM provinces
            WHERE turn_id=? AND population > 0 AND COALESCE(owner_nation_id,0) != ?
        """, (t["id"], game["nation_id"])).fetchone()["n"]

        # Who is in the game. Read from the per-nation records rather than
        # inferred from capital names, which only yields nations whose capital
        # we can see and cannot name their nation id at all.
        roster = conn.execute("""
            SELECT r.nation_id, r.gold, rn.name, rn.epithet, rn.era
            FROM nation_roster r
            LEFT JOIN ref.nations rn ON rn.id = r.nation_id
            WHERE r.turn_id = ? AND r.nation_id > 4
            ORDER BY r.nation_id
        """, (t["id"],)).fetchall()

        # Other nations' capitals are legitimately visible information.
        rivals = conn.execute("""
            SELECT p.name, p.owner_nation_id, rn.name AS nation, rn.epithet, rn.era
            FROM provinces p
            LEFT JOIN ref.nations rn ON rn.id = p.owner_nation_id
            WHERE p.turn_id=? AND p.is_capital=1
              AND COALESCE(p.owner_nation_id,0) != ?
            ORDER BY rn.name
        """, (t["id"], game["nation_id"])).fetchall()

        gems = conn.execute("SELECT * FROM nation_state WHERE turn_id=?",
                            (t["id"],)).fetchone()

        # Fields decoded but never surfaced. Showing them is the point: three
        # decodes were corrected this session only because the player could
        # compare a number on screen against the game.
        from ..file_reader.formats import commanders as _cmd
        from ..file_reader.formats import trn as _trn
        live = None
        if game["save_dir"]:
            p = Path(game["save_dir"]) / f"{game['nation_slug']}.trn"
            if p.exists():
                raw = p.read_bytes()
                names = _cmd.read_commander_names(raw)
                parsed = _trn.parse(p)
                live = {
                    "research_points": parsed.research_points,
                    "schools": list(zip(_trn.RESEARCH_SCHOOLS,
                                        parsed.research_levels or [],
                                        parsed.research_progress or [])),
                    "titles": _trn.read_pretender_titles(raw, names),
                    "mercenaries": _trn.read_mercenaries(raw),
                    "hall_of_fame": [(i, names.get(i, "?"))
                                     for i in _trn.read_hall_of_fame(raw)[:8]],
                }

        # Gem income: the sum of every site in a province we own. This is the
        # first figure the .trn does not store that can be produced without
        # guessing, because the site ids come from the file and the per-site
        # yield comes from the game's own data.
        income = conn.execute("""
            SELECT SUM(ms.F) f, SUM(ms.A) a, SUM(ms.W) w, SUM(ms.E) e,
                   SUM(ms.S) s, SUM(ms.D) d, SUM(ms.N) n, SUM(ms.G) g,
                   SUM(ms.B) b
            FROM province_sites ps
            JOIN provinces p ON p.turn_id = ps.turn_id
                            AND p.province_id = ps.province_id
            JOIN ref.magic_sites ms ON ms.id = ps.site_id
            WHERE ps.turn_id = ? AND p.owner_nation_id = ?
        """, (t["id"], game["nation_id"])).fetchone()

        provinces = [dict(r) for r in provinces]
        for p in provinces:
            p["terrain"] = terrain_names(conn, p["terrain_flags"] or 0)
            links = conn.execute("""
                SELECT l.neighbour_id AS id, pr.name,
                       COALESCE(b.border_flags, 0) AS border
                FROM province_links l
                LEFT JOIN provinces pr
                       ON pr.turn_id = l.turn_id AND pr.province_id = l.neighbour_id
                LEFT JOIN map_borders b
                       ON b.game_id = ?
                      AND b.province_id = MIN(l.province_id, l.neighbour_id)
                      AND b.neighbour_id = MAX(l.province_id, l.neighbour_id)
                WHERE l.turn_id = ? AND l.province_id = ?
                ORDER BY l.neighbour_id
            """, (game["id"], t["id"], p["province_id"])).fetchall()
            p["links"] = [dict(r) for r in links]
            p["sites"] = [dict(r) for r in conn.execute("""
                SELECT ms.name, ms.path, ms.level, ms.F, ms.A, ms.W, ms.E,
                       ms.S, ms.D, ms.N, ms.G, ms.B
                FROM province_sites ps
                JOIN ref.magic_sites ms ON ms.id = ps.site_id
                WHERE ps.turn_id = ? AND ps.province_id = ?
                ORDER BY ps.slot
            """, (t["id"], p["province_id"])).fetchall()]

        return {
            "request": request, "games": games, "game": game, "nation": nation,
            "turn": t, "turns": [r["turn"] for r in turns],
            "army": army, "provinces": provinces, "rivals": rivals,
            "gems": gems, "visible_count": visible, "conf": CONFIDENCE, "roster": roster,
            "not_stored": NOT_STORED, "borders": BORDER_TYPES, "gem_income": income,
            "live": live,
            "army_total": sum((r["gold"] or 0) * r["n"] for r in army),
            "army_size": sum(r["n"] for r in army),
        }
    finally:
        conn.close()


# The assistant, mounted here rather than only on the other app. There are two
# FastAPI apps in this repo — this one and `dom6_assistant.web.app` — and the
# assistant was first built into the other, where nobody was looking. Serving it
# from both is cheaper than choosing, since the routes are the same object.
from dom6_assistant.web.routes.agent import router as _agent_router  # noqa: E402
from dom6_assistant.web.routes.settings import router as _settings_router  # noqa: E402

app.include_router(_agent_router, prefix="/api")
app.include_router(ingest_service.router, prefix="/api")
# The endpoint editor in the assistant page needs this. It was only
# mounted on the other app, so the page silently got a 404 and showed
# "could not read the current settings".
app.include_router(_settings_router, prefix="/api")

_AGENT_PAGE = (Path(__file__).resolve().parents[1] / "web" / "static"
               / "agent.html")
_VERIFY_PAGE = (Path(__file__).resolve().parents[1] / "web" / "static"
                / "verify.html")


@app.get("/agent", response_class=HTMLResponse)
def agent_page() -> HTMLResponse:
    """Chat with tools, plus orders, notes, lessons, gaps and model profiles.

    Reads the live `.trn` through the tool surface rather than the ingested
    database, so it is current even when the turn history is not.
    """
    return HTMLResponse(_AGENT_PAGE.read_text())


@app.get("/verify", response_class=HTMLResponse)
def verification_page() -> HTMLResponse:
    """Exact live agent-visible state, without contacting a model."""
    return HTMLResponse(_VERIFY_PAGE.read_text())


#: Shown instead of a traceback when no turn has ever been ingested. A 500 on
#: the landing page is the worst possible first impression, and "no games yet"
#: is not an error -- it is what a new installation looks like.
_NOTHING_INGESTED = """<!doctype html><meta charset="utf-8">
<title>Dominions 6 Assistant</title>
<style>body{background:#0d0f13;color:#dfe4ee;font:15px/1.6 system-ui,sans-serif;
max-width:44rem;margin:12vh auto;padding:0 1.5rem}h1{color:#d9a441;font-size:20px}
code{background:#1b202b;padding:2px 6px;border-radius:4px;color:#d9a441}
a{color:#6ba3d6}li{margin:.4rem 0}</style>
<h1>No turns ingested yet</h1>
<p>This page shows a turn once one has been read. Nothing has been yet, which
is what a new installation looks like.</p>
<ul>
<li>Turns are ingested <b>automatically</b> while this server is running.
Play a turn in Dominions and it will appear here &mdash; watching
<code>__SAVE_ROOT__</code>.</li>
<li>Already have saves? Read them all in one go:<br>
<code>uv run python -m dom6_assistant.gamestate.ingest --scan</code></li>
<li>No game yet? Design a pretender first &mdash;
<a href="/agent">open the assistant</a> and choose <b>Pretender design</b>.
That needs no save file.</li>
</ul>
<p><a href="/agent">Assistant</a> &middot; <a href="/verify">Verify</a></p>
"""


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """The front door is the assistant.

    This used to be the Jinja turn-overview page, which is the older of the
    two interfaces and shows nothing at all until a turn has been ingested.
    It is still there, at /turns.
    """
    return RedirectResponse("/agent", status_code=307)


@app.get("/turns", response_class=HTMLResponse)
def index(request: Request, game_id: int | None = None, turn: int | None = None):
    try:
        return templates.TemplateResponse(request, "index.html",
                                          _context(request, game_id, turn))
    except sqlite3.OperationalError:
        # No database at all, i.e. nothing has ever been ingested here.
        from ..paths import default_save_root
        # A plain replace, not .format(): the page carries CSS, and every
        # `{` in it would be read as a format field.
        return HTMLResponse(_NOTHING_INGESTED.replace(
            "__SAVE_ROOT__", html.escape(str(default_save_root()))))


@app.get("/panel", response_class=HTMLResponse)
def panel(request: Request, game_id: int | None = None, turn: int | None = None):
    """HTMX partial: just the state panel, for switching game or turn."""
    return templates.TemplateResponse(request, "_state.html",
                                  _context(request, game_id, turn))


@app.get("/api/state")
def api_state(game_id: int | None = None, turn: int | None = None):
    """JSON for the eventual chat wrapper — same data, no presentation."""
    conn = db()
    try:
        game, t, _ = _current(conn, game_id, turn)
        if not game or not t:
            return {"error": "no game state ingested yet"}
        rows = conn.execute("""
            SELECT u.type_id, COUNT(*) n, ru.name, uc.gold
            FROM units u LEFT JOIN ref.units ru ON ru.id=u.type_id
            LEFT JOIN observed_unit_costs uc ON uc.unit_type_id=u.type_id
            WHERE u.turn_id=? GROUP BY u.type_id
        """, (t["id"],)).fetchall()
        provs = conn.execute(
            "SELECT * FROM provinces WHERE turn_id=? AND owner_nation_id=?",
            (t["id"], game["nation_id"])).fetchall()
        return {
            "game": game["name"], "turn": t["turn"], "nation_id": game["nation_id"],
            "army": [dict(r) for r in rows],
            "provinces": [dict(r) for r in provs],
        }
    finally:
        conn.close()
