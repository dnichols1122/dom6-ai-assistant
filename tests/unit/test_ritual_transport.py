"""Gem transport rituals: the recipient field and the carried-gem payload.

Decoded across turns 33-37 with Teleport Gems (spell 1288, effect 160 — the
same gem-transport family as Carrier Birds). Three casts, each read out of
the files rather than inferred.
"""
import shutil
import struct
from pathlib import Path

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.file_reader.formats import h2 as H2
from dom6_assistant.orders.orders_2h import find_order_blocks, read_carried_gems

SNAPSHOTS = Path("knowledge/snapshots")
TELEPORT_GEMS, AUGURY = 1288, 1283
BRETAIGNE, URRACA, TURGIS, FLOREDEE, SUGAAR = 126, 9, 307, 87, 297


def _read(snapshot, name="mid_marignon.2h"):
    path = SNAPSHOTS / snapshot / name
    if not path.exists():
        pytest.skip(f"{path} absent")
    return path.read_bytes()


def test_the_recipient_is_a_runtime_handle():
    """+128 was recorded as a sentinel; it names the receiving commander.

    The value is the same handle used to join a commander's `.2h` block to
    their `.trn` stat record, so no new identity mechanism is needed.
    """
    for snapshot, recipient in (("t33-teleport-gems", TURGIS),
                                ("t34-teleport-second-cast", URRACA),
                                ("t35-teleport-five-gems", FLOREDEE)):
        data = _read(snapshot)
        blocks = find_order_blocks(data)
        caster = blocks[BRETAIGNE]
        target = struct.unpack_from("<I", data, caster.name_end + 128)[0]
        handle = struct.unpack_from("<I", data, blocks[recipient].name_end)[0]
        assert target == handle, snapshot
        assert data[caster.name_end + 164] == 9              # cast_ritual once
        assert struct.unpack_from(
            "<H", data, caster.name_end + 116)[0] == TELEPORT_GEMS


def test_the_second_trailing_field_is_still_unused():
    data = _read("t35-teleport-five-gems")
    end = find_order_blocks(data)[BRETAIGNE].name_end
    assert struct.unpack_from("<i", data, end + 132)[0] == -1


@pytest.mark.parametrize(
    ("before", "after", "sender", "recipient", "sent"),
    [("t34-teleport-second-cast", "t35-teleport-confirmed",
      BRETAIGNE, URRACA, 10),
     ("t35-teleport-five-gems", "t37-teleport-five-confirmed",
      BRETAIGNE, FLOREDEE, 5)],
)
def test_the_payload_is_the_casters_carried_gems(before, after, sender,
                                                 recipient, sent):
    """min(carried, capacity), moved between commanders and nowhere else."""
    a, b = _read(before), _read(after)
    blocks_a, blocks_b = find_order_blocks(a), find_order_blocks(b)

    assert read_carried_gems(a, blocks_a[sender].name_end)["fire"] == sent
    assert read_carried_gems(b, blocks_b[sender].name_end).get("fire", 0) == 0
    assert read_carried_gems(b, blocks_b[recipient].name_end)["fire"] == sent


def test_gems_are_conserved_across_the_transfer():
    """The national pool pays the ritual's cost and nothing else.

    Totals are what caught the first cast's arithmetic: the caster appeared to
    gain gems, and only conservation showed the loaded amount had been
    miscounted rather than a mechanic topping him up.
    """
    def total_fire(data):
        pool = dict(zip(H2.GEM_PATHS, H2.gem_remaining(data)))["fire"]
        carried = sum(read_carried_gems(data, b.name_end).get("fire", 0)
                      for b in find_order_blocks(data).values())
        return pool + carried

    before = total_fire(_read("t34-teleport-second-cast"))
    after = total_fire(_read("t35-teleport-confirmed"))
    assert after - before == 4          # one turn of fire income, nothing else


@pytest.fixture
def session(tmp_path):
    snapshot = SNAPSHOTS / "t37-teleport-five-confirmed"
    if not (snapshot / "mid_marignon.2h").exists():
        pytest.skip("snapshot absent")
    for name in ("mid_marignon.2h", "mid_marignon.trn"):
        shutil.copy2(snapshot / name, tmp_path / name)
    game_db = tmp_path / "game.sqlite3"
    shutil.copy2("knowledge/game.sqlite3", game_db)
    s = open_session(save_dir=tmp_path, game_db=game_db)
    yield s
    s.close()


def test_a_transport_ritual_requires_a_recipient(session):
    result = session.call("cast_ritual", {
        "commander_id": BRETAIGNE, "spell_id": TELEPORT_GEMS,
        "target_province": 98, "rationale": "no recipient"})
    assert not result["ok"]
    assert "target_commander is required" in result["error"]


def test_an_ordinary_ritual_refuses_a_recipient(session):
    result = session.call("cast_ritual", {
        "commander_id": SUGAAR, "spell_id": AUGURY, "target_province": 86,
        "target_commander": FLOREDEE, "rationale": "augury takes no commander"})
    assert not result["ok"]
    assert "does not deliver to a commander" in result["error"]


def test_an_empty_caster_is_refused(session):
    """Casting with nothing loaded spends the ritual's gems for no effect."""
    result = session.call("cast_ritual", {
        "commander_id": BRETAIGNE, "spell_id": TELEPORT_GEMS,
        "target_province": 98, "target_commander": TURGIS,
        "rationale": "carries nothing"})
    assert not result["ok"]
    assert "carries no gems" in result["error"]
    assert "set_carried_gems" in result["error"]


def test_a_mixed_stock_within_capacity_is_allowed(session):
    """Refusing every mixed payload was too strict.

    Ambiguity comes from a SUBSET having to be chosen, not from variety: five
    fire, three water and two air is ten gems and travels whole. The earlier
    rule refused it and cost capability without buying safety.
    """
    from dom6_assistant.orders.orders_2h import find_order_blocks
    from dom6_assistant.orders import orders_2h as O

    h2_path = session.ctx.h2_path
    data = bytearray(h2_path.read_bytes())
    end = find_order_blocks(bytes(data))[BRETAIGNE].name_end
    for path, count in (("fire", 5), ("water", 3), ("air", 2)):
        data[end + O.OFF_CARRIED_GEMS + O.CARRIED_GEM_PATHS.index(path)] = count
    h2_path.write_bytes(bytes(data))

    result = session.call("cast_ritual", {
        "commander_id": BRETAIGNE, "spell_id": TELEPORT_GEMS,
        "target_province": 98, "target_commander": TURGIS,
        "rationale": "ten gems across three paths, all of them travel"})
    assert result["ok"], result.get("error")


def test_an_overfull_caster_is_refused(session):
    """Over capacity a subset travels, and nothing records which."""
    from dom6_assistant.orders.orders_2h import find_order_blocks
    from dom6_assistant.orders import orders_2h as O

    h2_path = session.ctx.h2_path
    data = bytearray(h2_path.read_bytes())
    end = find_order_blocks(bytes(data))[BRETAIGNE].name_end
    for path, count in (("fire", 9), ("water", 9)):
        data[end + O.OFF_CARRIED_GEMS + O.CARRIED_GEM_PATHS.index(path)] = count
    h2_path.write_bytes(bytes(data))

    result = session.call("cast_ritual", {
        "commander_id": BRETAIGNE, "spell_id": TELEPORT_GEMS,
        "target_province": 98, "target_commander": TURGIS,
        "rationale": "eighteen gems, only ten travel"})
    assert not result["ok"]
    assert "at most 10" in result["error"]
