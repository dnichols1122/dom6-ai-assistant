"""Smoke tests for core data models."""

from dom6_assistant.models import Era, GameState, Nation, Province
from dom6_assistant.memory_reader._linux import LinuxMemoryReader


def test_game_state_empty():
    gs = GameState(turn=1, era=Era.EARLY)
    assert gs.turn == 1
    assert gs.era == Era.EARLY
    assert gs.nations == {}
    assert gs.provinces == {}


def test_nation_creation():
    n = Nation(nation_id=5, name="Ulm", epithet="Forges of Ulm", era=Era.EARLY)
    assert n.nation_id == 5
    assert n.era == Era.EARLY


def test_province_no_owner():
    p = Province(province_id=10, name="Ermor", owner_nation_id=None, capital=False)
    assert p.owner_nation_id is None
    assert p.units == []


def test_live_gem_slots_use_the_confirmed_nine_path_order():
    """Every slot is distinct so a swapped label cannot accidentally pass."""
    base = 0x100000
    values = {
        0x000: 7000,
        0xF04: 97, 0xF08: 91, 0xF0C: 79,
        0xF10: 105, 0xF14: 84, 0xF18: 101,
        0xF1C: 89, 0xF20: 87, 0xF24: 99,
    }

    class FakeReader(LinuxMemoryReader):
        def __init__(self):
            pass

        def read_int32(self, address):
            return values[address - base]

        def read_int16(self, address):
            assert address == base + 0x00C
            return 80

    state = FakeReader().read_nation_state(base)
    assert state.gold == 7000 and state.nation_id == 80
    assert vars(state.gems) == {
        "fire": 97, "air": 91, "water": 79, "earth": 105,
        "astral": 84, "death": 101, "nature": 89,
        "glamour": 87, "blood": 99,
    }
