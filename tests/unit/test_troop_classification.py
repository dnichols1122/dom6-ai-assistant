"""Which units the squad writers will move, and which they refuse.

The old rule asked "is this type nationally recruitable?", which is a proxy
rather than the hazard. It refused four whole classes that are ordinary
soldiers for squad purposes — summons, mercenary troops, units recruited in
non-home provinces (generic Heavy Cavalry, Barbarians and the like), and event
units — while catching commanders only incidentally.
"""
import json

import pytest

from dom6_assistant.agent.session import open_session
from dom6_assistant.agent.write_tools import _is_leader_type, _troop_selection


@pytest.fixture(scope="module")
def ctx():
    session = open_session()
    yield session.ctx
    session.close()


def test_a_mercenary_troop_type_is_not_a_commander(ctx):
    """Longdead stood in squads under their own commander for three turns."""
    assert not _is_leader_type(ctx, 195)


def test_a_generic_province_troop_is_not_a_commander(ctx):
    """Heavy Cavalry is recruitable outside the capital and off-roster."""
    assert not _is_leader_type(ctx, 292)


@pytest.mark.parametrize("type_id", [149, 224, 440])
def test_roster_commanders_are_recognised(ctx, type_id):
    """Inquisitor, Witch Hunter and Paladin, via the nation leader tables."""
    assert _is_leader_type(ctx, type_id)


def test_the_sentinel_instance_is_refused(ctx):
    """65535 resolves to a scanner phantom, not a unit.

    One in the live save reads instance 65535, warband 0 and a runtime index
    of 556 million. Its sibling is a mount, so the uniqueness check does not
    catch it: the mount filter leaves exactly one match.
    """
    with pytest.raises(Exception, match="no-instance sentinel"):
        _troop_selection(ctx, json.dumps([0xFFFF]))


def test_every_commander_we_hold_is_refused(ctx):
    """Nothing with an order block or a leader-table entry may be reassigned.

    A commander misclassified as a troop has its warband token rewritten,
    which corrupts both the commander and the squad it was leading.
    """
    import struct
    from dom6_assistant.orders import orders_2h as O

    data = ctx.h2_path.read_bytes()
    handles = {struct.unpack_from("<I", data, b.name_end)[0]
               for b in O.find_order_blocks(data).values()}
    commanders = [u for u in O.read_h2_units(data, ctx.nation_id)
                  if not u.is_mount and u.instance_id != 0xFFFF
                  and (u.runtime_index in handles
                       or _is_leader_type(ctx, u.type_id))]
    assert commanders, "the live save has commanders to check"
    for unit in commanders:
        with pytest.raises(Exception, match="is a commander"):
            _troop_selection(ctx, json.dumps([unit.instance_id]))


def test_ordinary_troops_are_still_accepted(ctx):
    """The point of the change: real soldiers remain assignable."""
    from dom6_assistant.orders import orders_2h as O
    import struct

    data = ctx.h2_path.read_bytes()
    handles = {struct.unpack_from("<I", data, b.name_end)[0]
               for b in O.find_order_blocks(data).values()}
    troops = [u for u in O.read_h2_units(data, ctx.nation_id)
              if not u.is_mount and u.instance_id != 0xFFFF
              and u.runtime_index not in handles
              and not _is_leader_type(ctx, u.type_id)]
    assert troops, "the live save has ordinary troops"
    requested, chosen, records = _troop_selection(
        ctx, json.dumps([troops[0].instance_id]))
    assert chosen and chosen[0][0] == troops[0].instance_id
