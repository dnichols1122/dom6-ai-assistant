"""Dominions 6.36 Wish parser and canonical result builder.

The game never stores the text entered in Wish's prompt.  The client parses it
immediately and writes a result family at ritual +124 and one signed payload at
+128.  This module is a port of that parser/result builder, kept separate from
the order writer so every caller uses the same typed contract.

Raw prose is accepted only by :func:`parse_client_text`, which exists for
parity tests and importing a player-authored choice.  Agent-facing code should
call :func:`build_typed`; it exposes stable result names and typed item, unit,
nation, and random selectors instead of relying on spelling tricks.
"""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


WISH_SPELL_ID = 915


@dataclass(frozen=True)
class WishSpec:
    code: int
    payload_kind: str = "none"
    fixed_payload: int = -1
    outcome: str = ""
    visibility: str = "private cast report only"


# Quantities are from the 6.36 resolver, not the older community table.  In
# particular the current client grants 300 slaves, 500 militia, 30 of each gem
# type, and 5000/3000 gold for the two directly requested wealth results.
SPECS: Mapping[str, WishSpec] = {
    "nothing": WishSpec(10000, outcome="no effect"),
    "magic_item": WishSpec(
        10001, "item",
        outcome=("the named item is created, or an existing unique item is "
                 "teleported to the caster; displaced equipment goes to the lab")),
    "unit": WishSpec(
        10002, "unit",
        outcome=("the named unit is created or transferred; unique foreign "
                 "commanders can cause a defence battle at the caster's province")),
    "blood_slaves": WishSpec(10003, outcome="300 blood slaves"),
    "gold": WishSpec(10004, "amount", 5000, "5000 gold"),
    "lesser_gold": WishSpec(10004, "amount", 3000, "3000 gold"),
    # This concrete result is reachable through the random 'something' branch.
    "abundant_gold": WishSpec(10004, "amount", 7500, "7500 gold"),
    # The resolver's loop deliberately/accidentally skips stockpile index 4
    # (Astral) and stops before index 8 (Blood): this is not the old wiki's
    # "all types except slaves" result.
    "gems": WishSpec(
        10005,
        outcome="30 Fire, Air, Water, Earth, Death, Nature, and Glamour gems; no Astral pearls or Blood slaves"),
    "magic_power": WishSpec(
        10006,
        outcome="+1 to every intrinsic magic path on the caster, capped at 10"),
    "physical_power": WishSpec(
        10007,
        outcome=("+20 Strength, +50 HP, and +5 Attack/Defence/Precision on the "
                 "first application; later applications add half")),
    "armageddon": WishSpec(
        10008,
        outcome=("every province loses 20% population and every unit has a 20% "
                 "chance to be struck"),
        visibility="worldwide effects and reports"),
    "divine_power": WishSpec(
        10009,
        outcome=("up to 20 additional dominion candles, gained at the expense "
                 "of other pretender gods"),
        visibility="private cast report plus worldwide proclamation"),
    "troops": WishSpec(10010, "fixed", 500, "500 Militia"),
    "food": WishSpec(
        10011, "fixed", 1,
        "3 Enormous Cauldrons of Broth, 2 Cornucopias, and 5 Endless Bags of Wine"),
    "population": WishSpec(
        10012, "fixed", 1,
        outcome=("one eligible foreign province's population and occupants are "
                 "moved to a random owned province; enemy occupants fight there")),
    "death": WishSpec(
        10013,
        outcome="the caster is attacked by Wish's pretender-killing effects"),
    "kill_pretender": WishSpec(
        10014, "nation",
        outcome="the selected nation's pretender is attacked by Wish's killing effects"),
    "provinces": WishSpec(
        10015,
        outcome=("up to 20 enemy provinces with no occupants and province defence "
                 "below 10; the result can transfer nothing"),
        visibility="private cast report plus worldwide proclamation"),
    "experience": WishSpec(10017, "fixed", 500, "500 experience for the caster"),
    "horror": WishSpec(
        10018, "optional_unit",
        outcome=("a named Horror, or a random Doom/greater Horror, answers; it may "
                 "join the caster or attack in an assassination battle")),
    "remove_curse": WishSpec(10019, outcome="remove the caster's curse"),
    "commanders": WishSpec(10020, "fixed", 1, "100 Vanara Captains"),
}

RESULT_CODES = {name: spec.code for name, spec in SPECS.items()}

# Codes are unique except for the three semantically distinct gold quantities.
_DEFAULT_NAME_BY_CODE = {
    spec.code: name for name, spec in SPECS.items()
    if name not in {"lesser_gold", "abundant_gold"}
}

FIXED_RESULTS = tuple(
    name for name, spec in SPECS.items()
    if spec.payload_kind in {"none", "fixed", "amount"}
    and name != "abundant_gold"
)
RANDOM_REQUESTS = ("item", "artifact", "something", "horror")


@dataclass(frozen=True)
class WishSelection:
    """One concrete value that can be serialized in a `.2h`."""
    result: str
    code: int
    payload: int = -1
    requested: str | None = None

    @property
    def spec(self) -> WishSpec:
        return SPECS[self.result]

    def as_dict(self) -> dict:
        out = {
            "result": self.result,
            "result_code": self.code,
            "payload_kind": self.spec.payload_kind,
            "outcome": self.spec.outcome,
            "visibility": self.spec.visibility,
        }
        if self.payload >= 0:
            out[{
                "item": "item_id", "unit": "unit_type_id",
                "optional_unit": "unit_type_id", "nation": "nation_id",
                "amount": "amount", "fixed": "client_parameter",
            }.get(self.spec.payload_kind, "payload")] = self.payload
        if self.requested is not None:
            out["requested"] = self.requested
        return out


def selection_from_wire(code: int, payload: int) -> WishSelection:
    """Interpret the exact code/payload pair stored by the client."""
    if code == 10004:
        name = {3000: "lesser_gold", 5000: "gold", 7500: "abundant_gold"}.get(
            payload, "gold")
    else:
        name = _DEFAULT_NAME_BY_CODE.get(code, f"unknown_{code}")
    if name.startswith("unknown_"):
        return WishSelection(name, code, payload)
    return WishSelection(name, code, payload)


def _row(conn: sqlite3.Connection, table: str, object_id: int):
    return conn.execute(f"SELECT * FROM {table} WHERE id=?", (object_id,)).fetchone()


def _available_random_items(
    conn: sqlite3.Connection,
    constlevel: int,
    item_states: Sequence[int] | None,
) -> list[int]:
    """Port the client's tier-filtered, uniform random-item candidate list."""
    rows = conn.execute(
        "SELECT id FROM items WHERE constlevel=? ORDER BY id", (constlevel,)
    ).fetchall()
    if constlevel >= 9 and item_states is None:
        raise ValueError(
            "current artifact availability is not decoded; random artifact Wish refused")
    result: list[int] = []
    for row in rows:
        item_id = int(row["id"])
        # The client excludes an already-created artifact (effect 278).  The
        # extracted DB's odd construction tiers 9/11 are precisely the two
        # random unique/special pools used here.
        if constlevel >= 9:
            assert item_states is not None
            if item_id >= len(item_states):
                raise ValueError(
                    f"artifact state for item {item_id} is outside the decoded table")
            if int(item_states[item_id]) > 0:
                continue
        result.append(item_id)
    return result


def _random_item(
    conn: sqlite3.Connection,
    constlevel: int,
    item_states: Sequence[int] | None,
    randbelow: Callable[[int], int],
) -> int:
    candidates = _available_random_items(conn, constlevel, item_states)
    if not candidates:
        raise ValueError(
            f"Wish has no available random item in internal Construction tier {constlevel}")
    return candidates[randbelow(len(candidates))]


def build_typed(
    conn: sqlite3.Connection,
    *,
    result: str | None = None,
    item_id: int | None = None,
    unit_id: int | None = None,
    nation_id: int | None = None,
    random_request: str | None = None,
    item_states: Sequence[int] | None = None,
    caster_is_cursed: bool | None = None,
    randbelow: Callable[[int], int] = secrets.randbelow,
) -> WishSelection:
    """Build the same concrete code/payload pair as the 6.36 client.

    Exactly one top-level selector is required. Named Horrors are classified
    from unit data automatically. Random `item`, `artifact`, and `something`
    are resolved now because the client resolves them before saving. A generic
    Horror remains payload -1 because that random choice occurs on the host
    during turn resolution.
    """
    selectors = sum(value is not None for value in (
        result, item_id, unit_id, nation_id, random_request))
    if selectors != 1:
        raise ValueError(
            "Wish requires exactly one typed result, item, unit, nation, or random request")

    if item_id is not None:
        row = _row(conn, "items", item_id)
        if row is None:
            raise ValueError(f"magic item {item_id} is absent from the reference data")
        # Named artifacts are intentionally allowed even when they already
        # exist: the resolver teleports/steals the existing item.
        return WishSelection("magic_item", 10001, int(item_id), "named_item")

    if unit_id is not None:
        row = _row(conn, "units", unit_id)
        if row is None:
            raise ValueError(f"unit {unit_id} is absent from the reference data")
        if int(row["nowish"] or 0):
            raise ValueError(f"{row['name']} has No Wish and cannot be wished for")
        name = "horror" if int(row["horror"] or 0) > 2 else "unit"
        return WishSelection(name, SPECS[name].code, int(unit_id), "named_unit")

    if nation_id is not None:
        row = _row(conn, "nations", nation_id)
        if row is None:
            raise ValueError(f"nation {nation_id} is absent from the reference data")
        return WishSelection("kill_pretender", 10014, int(nation_id), "nation")

    if random_request is not None:
        if random_request not in RANDOM_REQUESTS:
            raise ValueError(
                f"unsupported Wish random request {random_request!r}; choose one of {RANDOM_REQUESTS}")
        if random_request == "horror":
            return WishSelection("horror", 10018, -1, "random_horror")
        if random_request == "artifact":
            item = _random_item(conn, 9, item_states, randbelow)
            return WishSelection("magic_item", 10001, item, "random_artifact")
        if random_request == "item":
            # Client chooses the tier uniformly, then the item uniformly within
            # it; this is not uniform across all Construction 1/3/5 items.
            tier = (1, 3, 5)[randbelow(3)]
            item = _random_item(conn, tier, item_states, randbelow)
            return WishSelection("magic_item", 10001, item, "random_item")
        # `something`: <=32 on rand(100) is the special-item branch. The two
        # remaining branches use a second independent roll, exactly as client.
        if randbelow(100) <= 32:
            item = _random_item(conn, 11, item_states, randbelow)
            return WishSelection("magic_item", 10001, item, "random_something")
        if randbelow(100) <= 49:
            return WishSelection("abundant_gold", 10004, 7500, "random_something")
        return WishSelection("death", 10013, -1, "random_something")

    assert result is not None
    if result not in SPECS:
        raise ValueError(f"unsupported Wish result {result!r}; supported: {sorted(SPECS)}")
    spec = SPECS[result]
    if spec.payload_kind in {"item", "unit", "optional_unit", "nation"}:
        raise ValueError(
            f"the {result} Wish result requires its typed object selector")
    if result == "remove_curse" and caster_is_cursed is False:
        raise ValueError("the client accepts remove_curse only when the caster is cursed")
    return WishSelection(result, spec.code, spec.fixed_payload, "typed_result")


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def parse_client_text(
    conn: sqlite3.Connection,
    text: str,
    *,
    item_states: Sequence[int] | None = None,
    caster_is_cursed: bool = False,
    randbelow: Callable[[int], int] = secrets.randbelow,
) -> WishSelection | None:
    """Port the 6.36 free-text parser, in its original precedence order.

    This is deliberately not the agent API. It is useful for parity tests,
    decoding UI behavior, and converting a human-entered prompt to the same
    canonical typed result the assistant exposes.
    """
    value = text.casefold()
    exact = lambda *choices: value in choices

    if _contains(value, "nothing", "exit") or exact("cancel"):
        return build_typed(conn, result="nothing")
    if exact("death", "to die", "my death", "a swift death"):
        return build_typed(conn, result="death")

    item = conn.execute(
        "SELECT id FROM items WHERE lower(name)=lower(?) ORDER BY id LIMIT 1", (text,)
    ).fetchone()
    if item is not None:
        return build_typed(conn, item_id=int(item["id"]), item_states=item_states)
    unit = conn.execute(
        "SELECT id FROM units WHERE lower(name)=lower(?) ORDER BY id LIMIT 1", (text,)
    ).fetchone()
    if unit is not None:
        try:
            return build_typed(conn, unit_id=int(unit["id"]))
        except ValueError:
            return None

    if _contains(value, "dominion", "divine power", "divine authority"):
        return build_typed(conn, result="divine_power")
    if (_contains(value, "strength", "physic", "hitpoint", "hit point",
                  "defense", "defence", "attack", "stronger") or exact("power")):
        return build_typed(conn, result="physical_power")
    if _contains(value, "magical power", "magic power", "magic skill",
                 "magic master", "magic might", "ultimate power"):
        return build_typed(conn, result="magic_power")
    if (exact("armageddon", "genocide") or
            (_contains(value, "death", "end") and
             _contains(value, "world", "everyone", "everything"))):
        return build_typed(conn, result="armageddon")
    if _contains(value, "blood slave") or exact("slaves", "blood"):
        return build_typed(conn, result="blood_slaves")
    if (_contains(value, "uncurse") or
            (_contains(value, "remove", "removed") and "curse" in value)):
        if not caster_is_cursed:
            return None
        return build_typed(conn, result="remove_curse", caster_is_cursed=True)
    if exact("horror", "scary monster"):
        return build_typed(conn, random_request="horror")
    if _contains(value, "troops", "military", "units") or exact("army", "militia"):
        return build_typed(conn, result="troops")
    if _contains(value, "gems", "vis", "magic resources", "diamonds"):
        return build_typed(conn, result="gems")
    if (_contains(value, "provinces", "lands") or
            (_contains(value, "large", "huge") and "nation" in value)):
        return build_typed(conn, result="provinces")
    if (_contains(value, "food", "supply", "supplies", "broth", "wine") or
            (_contains(value, "no", "stop", "end") and "starv" in value)):
        return build_typed(conn, result="food")
    if _contains(value, "population", "people", "populace", "enemies",
                 "friends", "peasants", "commoners"):
        return build_typed(conn, result="population")

    if (_contains(value, "death", "end", "kill", "die") and
            _contains(value, "pretender", "god", "leader")):
        for row in conn.execute("SELECT id, name FROM nations ORDER BY id"):
            if row["name"].casefold() in value:
                return build_typed(conn, nation_id=int(row["id"]))

    if _contains(value, "fame", "experience", "combat skill", "fighting skill"):
        return build_typed(conn, result="experience")
    if _contains(value, "artefact", "artifact"):
        return build_typed(conn, random_request="artifact",
                           item_states=item_states, randbelow=randbelow)
    if "sword" in value:
        row = conn.execute("SELECT id FROM items WHERE name='Enchanted Sword'").fetchone()
        return build_typed(conn, item_id=int(row["id"])) if row else None
    if "staff" in value:
        row = conn.execute("SELECT id FROM items WHERE name='Staff of Storms'").fetchone()
        return build_typed(conn, item_id=int(row["id"])) if row else None
    if "weapon" in value:
        row = conn.execute("SELECT id FROM items WHERE name='Sword of Sharpness'").fetchone()
        return build_typed(conn, item_id=int(row["id"])) if row else None
    if _contains(value, "gold", "money", "wealth", "riches"):
        return build_typed(conn, result="gold")
    if _contains(value, "silver", "copper", "dough"):
        return build_typed(conn, result="lesser_gold")
    if "item" in value:
        return build_typed(conn, random_request="item", item_states=item_states,
                           randbelow=randbelow)
    if "commanders" in value:
        return build_typed(conn, result="commanders")
    if _contains(value, "something", "anything", "whatever"):
        return build_typed(conn, random_request="something",
                           item_states=item_states, randbelow=randbelow)
    return None
