"""Empowerment: order shape, price chart, and the gems it reserves.

The order was recovered from existing turn-15 snapshots rather than a new
experiment. Bruise went from no paths to Fire 1, and the cost was established
by accounting for every other gem-reserving order in the same save.
"""
import shutil
import sqlite3
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.orders import materialize as M
from dom6_assistant.orders.orders_2h import (
    OrdersEditor, find_order_blocks, read_empowerment_path,
)
from dom6_assistant.reference import empowerment_cost as EC

SNAPSHOTS = Path("knowledge/snapshots")
BEFORE = SNAPSHOTS / "t15-orders" / "mid_marignon.2h"
AFTER = SNAPSHOTS / "t15-misc" / "mid_marignon.2h"
BRUISE, TURGIS = 308, 307


def ok(result):
    assert result["ok"], result.get("error")
    return result["result"]


def _require(*paths):
    for path in paths:
        if not path.exists():
            pytest.skip(f"{path} absent")


def test_the_published_chart_is_reproduced():
    """50 for the first level, then 15 per level. Level 1 is the exception."""
    assert EC.empowerment_cost(1) == 50
    for level, gems in ((2, 30), (3, 45), (4, 60), (5, 75),
                        (6, 90), (7, 105), (8, 120), (9, 135)):
        assert EC.empowerment_cost(level) == gems
    assert EC.empowerment_cost(11) == 165          # 15 * level, past the chart


def test_only_the_first_level_price_has_been_measured():
    """The rest is the published chart, and the tool says so.

    Level 1 is the one we watched the game charge: 50 fire gems, recovered by
    accounting for a 5-gem Fire Sword forge against a pool that fell 60 -> 5.
    """
    assert EC.cost_is_confirmed(1)
    assert not EC.cost_is_confirmed(2)


def test_the_measured_cost_matches_the_chart():
    """The only cross-check available, and it agrees exactly."""
    _require(BEFORE, AFTER)
    before = H2.gem_remaining(BEFORE.read_bytes())[0]
    after = H2.gem_remaining(AFTER.read_bytes())[0]
    forge_in_the_same_save = 5
    assert before - after - forge_in_the_same_save == EC.empowerment_cost(1)


def test_the_order_stores_a_path_and_no_cost():
    """Unlike a forge or ritual, +120 stays zero while the pool still pays."""
    _require(AFTER)
    data = AFTER.read_bytes()
    block = find_order_blocks(data)[BRUISE]
    assert block.order_code == 5
    assert read_empowerment_path(data, block.name_end) == 0      # fire
    assert int.from_bytes(
        data[block.name_end + 120:block.name_end + 124], "little") == 0


def test_the_editor_reproduces_the_order(tmp_path):
    _require(BEFORE, AFTER)
    path = tmp_path / "mid_marignon.2h"
    shutil.copy2(BEFORE, path)
    editor = OrdersEditor(path)
    editor.set_empowerment(BRUISE, 0)
    made = bytes(editor.data)
    game = AFTER.read_bytes()
    end = find_order_blocks(made)[BRUISE].name_end

    assert len(made) == len(BEFORE.read_bytes())
    assert made[end + 164] == game[end + 164] == 5
    assert made[end + 116] == game[end + 116] == 0
    assert made[end + 120:end + 136] == game[end + 120:end + 136]


def test_an_edit_may_never_change_the_file_length(tmp_path):
    """A mis-sized slice assignment resizes a bytearray and shifts the file.

    That happened once — 22 zeros written over an 18-byte span — and produced
    a file that still loaded while being wrong everywhere after the edit.
    """
    _require(BEFORE)
    path = tmp_path / "mid_marignon.2h"
    shutil.copy2(BEFORE, path)
    editor = OrdersEditor(path)
    editor.set_empowerment(BRUISE, 0)
    editor.data.extend(b"\x00\x00")          # simulate the mistake
    with pytest.raises(ValueError, match="changed the file length"):
        editor.save()


@pytest.fixture
def wired(tmp_path):
    _require(BEFORE, SNAPSHOTS / "t15" / "mid_marignon.trn")
    shutil.copy2(BEFORE, tmp_path / "mid_marignon.2h")
    shutil.copy2(SNAPSHOTS / "t15" / "mid_marignon.trn",
                 tmp_path / "mid_marignon.trn")
    db = sqlite3.connect(":memory:")
    db.executescript(
        Path("src/dom6_assistant/gamestate/schema.sql").read_text())
    db.execute("INSERT INTO games(id, name) VALUES(1, 'test')")
    db.commit()
    ref = sqlite3.connect("knowledge/reference/reference.sqlite3")
    ref.row_factory = sqlite3.Row
    return tmp_path / "mid_marignon.2h", db, ref


def test_materialising_reserves_the_gems(wired):
    h2_path, db, ref = wired
    before = H2.gem_remaining(h2_path.read_bytes())[0]
    M.record_empowerment(db, 1, 15, BRUISE, 0, 1, 50,
                         commander_name="Bruise", rationale="unlock fire")
    result = M.materialize(db, 1, 15, h2_path, reference_conn=ref)

    assert not result.skipped, result.skipped
    data = h2_path.read_bytes()
    assert read_empowerment_path(data, find_order_blocks(data)[BRUISE].name_end) == 0
    assert H2.gem_remaining(data)[0] == before - 50


def test_replacing_astral_empowerment_refunds_astral_not_air(tmp_path):
    clean = SNAPSHOTS / "t33-construction3-clean"
    _require(clean / "mid_marignon.2h", clean / "mid_marignon.trn")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(clean / name, tmp_path / name)
    path = tmp_path / "mid_marignon.2h"

    editor = OrdersEditor(path)
    editor.set_empowerment(126, 4)  # Bretaigne is S3; S3 -> S4 reserves 60.
    gems = list(H2.gem_remaining(bytes(editor.data)))
    starting_air, starting_astral = gems[1], 100
    gems[4] = starting_astral - 60
    editor.data[:] = H2.set_gem_remaining(bytes(editor.data), gems)
    editor.save(backup=False)

    db = sqlite3.connect(":memory:")
    db.executescript(
        Path("src/dom6_assistant/gamestate/schema.sql").read_text())
    db.execute("INSERT INTO games(id, name) VALUES(1, 'test')")
    M.record_order(db, 1, 33, 126, "defend", commander_name="Bretaigne",
                   rationale="cancel empowerment")

    result = M.materialize(db, 1, 33, path)

    assert not result.skipped
    after = H2.gem_remaining(path.read_bytes())
    assert after[1] == starting_air
    assert after[4] == starting_astral


@pytest.fixture
def session(tmp_path):
    clean = SNAPSHOTS / "t33-construction3-clean"
    _require(clean / "mid_marignon.2h")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(clean / name, tmp_path / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2("knowledge/game.sqlite3", game_db)
    s = open_session(save_dir=tmp_path, game_db=game_db)
    yield s
    s.close()


def test_the_tool_prices_the_next_level_not_the_first(session):
    """Bruise already has Fire 1, so empowering him costs the level-2 price."""
    result = session.call("empower_commander", {
        "commander_id": BRUISE, "path": "fire",
        "rationale": "deepen an existing path"})
    assert result["ok"], result.get("error")
    out = result["result"]
    assert (out["from_level"], out["to_level"]) == (1, 2)
    assert out["gems_reserved"] == {"fire": 30}
    assert "published price chart" in out["cost_source"]


def test_holy_cannot_be_empowered(session):
    result = session.call("empower_commander", {
        "commander_id": BRUISE, "path": "holy", "rationale": "no"})
    assert not result["ok"]
    assert "Holy cannot be empowered" in result["error"]


def test_empowerment_needs_a_laboratory(session):
    """Confirmed against the game, three provinces and one variable.

    Of our provinces only Marignon has a laboratory, and it is the only one
    whose commanders are offered the order. The Obsidian Waste has a temple
    but no laboratory and refuses it, so it is the laboratory that matters
    rather than any structure.
    """
    commanders = ok(session.call("list_commanders", {}))["commanders"]
    elsewhere = next(c for c in commanders
                     if c["province_name"] == "The Obsidian Waste")
    result = session.call("empower_commander", {
        "commander_id": elsewhere["commander_id"], "path": "fire",
        "rationale": "no laboratory here"})
    assert not result["ok"]
    assert "no laboratory" in result["error"]


def test_an_unaffordable_empowerment_is_refused(session):
    result = session.call("empower_commander", {
        "commander_id": BRUISE, "path": "water", "rationale": "no gems"})
    assert not result["ok"]
    assert "costs 50 water gems" in result["error"]
