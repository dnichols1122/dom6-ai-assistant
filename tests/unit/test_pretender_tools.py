from __future__ import annotations

from pathlib import Path

from dom6_assistant.agent.pretender_tools import open_pretender_session
from dom6_assistant.file_reader.formats.pretender import parse


REFERENCE_DB = Path(__file__).resolve().parents[2] / "knowledge/reference/reference.sqlite3"
WIKI_DB = Path(__file__).resolve().parents[2] / "knowledge/illwiki/illwiki.sqlite3"


def test_pretender_session_lists_evaluates_and_writes(tmp_path):
    session = open_pretender_session(
        61, reference_db=REFERENCE_DB, output_dir=tmp_path,
    )
    try:
        chassis = session.call("list_pretender_chassis", {"awakening": "dormant"})
        assert chassis["ok"]
        by_id = {
            row["chassis_id"]: row
            for row in chassis["result"]["chassis"]
        }
        assert by_id[3894]["selected_awakening_cost"] == 170
        assert 179 not in by_id

        args = {
            "chassis_id": 3894,
            "awakening": "dormant",
            "dominion": 6,
            "paths": '{"fire": 6, "air": 6}',
            "scales": '{"order": 2, "productivity": 1}',
            "blessing_ids": "[7, 6, 3, 3]",
        }
        evaluated = session.call("evaluate_pretender", args)
        assert evaluated["ok"]
        assert evaluated["result"]["design_points"]["remaining"] == 0
        assert evaluated["result"]["bless_points"]["remaining"] == 0
        assert all(row["effect"] for row in evaluated["result"]["blessings"])

        created = session.call("create_pretender", {"name": "TOOL SUGAAR", **args})
        assert created["ok"]
        destination = Path(created["result"]["saved_file"])
        assert destination.parent == tmp_path
        saved = parse(destination)
        assert saved.name == "TOOL SUGAAR"
        assert saved.nation_id == 61
        assert saved.chassis_id == 3894
    finally:
        session.close()


def test_selected_blessing_resolves_exact_linked_mechanic_definition(tmp_path):
    session = open_pretender_session(
        23, reference_db=REFERENCE_DB, output_dir=tmp_path, wiki_db=WIKI_DB,
    )
    try:
        listed = session.call("list_pretender_blessings", {
            "paths": {"fire": 5, "earth": 3, "death": 5},
            "scales": {},
        })
        mending = next(
            row for row in listed["result"]["eligible"]
            if row["name"] == "Mending Bones"
        )
        assert mending["effect"] == "Innate recuperation for undead"

        evaluated = session.call("evaluate_pretender", {
            "chassis_id": 2203,
            "awakening": "awake",
            "dominion": 5,
            "paths": '{"fire": 5, "earth": 3, "death": 5}',
            "scales": "{}",
            "blessing_ids": "[53, 54, 56, 57]",
        })
        assert evaluated["ok"]
        detail = next(
            row for row in evaluated["result"]["blessings"]
            if row["name"] == "Mending Bones"
        )
        recuperation = next(
            row for row in detail["linked_mechanics"]
            if row["term"] == "Recuperation"
        )
        assert "outside of battle" in recuperation["definition"]
        assert "not Regeneration" in recuperation["definition"]
        assert "doesn't restore lost Hit Points" in recuperation["definition"]
        assert "exact game terms" in evaluated["result"]["mechanics_policy"]
    finally:
        session.close()


def test_pretender_structured_values_are_native_objects_with_legacy_support(
        tmp_path):
    session = open_pretender_session(
        23, reference_db=REFERENCE_DB, output_dir=tmp_path,
    )
    try:
        schema = session.registry.get("list_pretender_blessings").openai_schema()
        properties = schema["function"]["parameters"]["properties"]
        assert properties["paths"]["type"] == "object"
        assert properties["scales"]["type"] == "object"
        rules = session.call("get_pretender_rules")["result"]
        assert rules["tool_argument_shapes"]["paths"] == {"fire": 6, "air": 4}
        assert "never as quoted" in rules["tool_argument_shapes"]["instruction"]

        native = session.call("list_pretender_blessings", {
            "paths": {"death": 5}, "scales": {},
        })
        legacy = session.call("list_pretender_blessings", {
            "paths": '{"death": 5}', "scales": "{}",
        })
        assert native["ok"] and legacy["ok"]
        assert native["result"] == legacy["result"]

        evaluate = session.registry.get("evaluate_pretender").openai_schema()
        evaluated = evaluate["function"]["parameters"]["properties"]
        assert evaluated["paths"]["type"] == "object"
        assert evaluated["scales"]["type"] == "object"
        assert evaluated["blessing_ids"]["type"] == "array"
    finally:
        session.close()


def test_pretender_session_never_overwrites_existing_design(tmp_path):
    session = open_pretender_session(
        61, reference_db=REFERENCE_DB, output_dir=tmp_path,
    )
    args = {
        "name": "FIRST",
        "chassis_id": 3894,
        "awakening": "dormant",
        "dominion": 6,
        "paths": '{"fire": 6, "air": 6}',
        "scales": '{"order": 2, "productivity": 1}',
        "blessing_ids": "[7, 6, 3, 3]",
    }
    try:
        first = Path(session.call("create_pretender", args)["result"]["saved_file"])
        second = Path(session.call("create_pretender", args)["result"]["saved_file"])
        assert first != second
        assert first.exists() and second.exists()
    finally:
        session.close()


def test_costs_tool_prices_every_choice_and_reconciles_with_evaluation(tmp_path):
    """The designer could previously only learn a price by spending it.

    evaluate_pretender returns four totals, so working out what one more path
    level or one more Dominion point costs meant guess-and-check against a
    whole candidate design. This prices each choice up front.
    """
    session = open_pretender_session(
        61, reference_db=REFERENCE_DB, output_dir=tmp_path,
    )
    try:
        priced = session.call(
            "get_pretender_costs", {"chassis_id": 3894, "awakening": "dormant"})
        assert priced["ok"]
        costs = priced["result"]
        assert costs["point_pool"] == 600
        assert costs["chassis"]["dormant"] == 170

        # Dominion is the triangular sum, not a flat rate.
        start = costs["dominion"]["starting_value"]
        assert costs["dominion"]["by_value"][start] == 0
        steps = [costs["dominion"]["by_value"][start + n] for n in range(1, 5)]
        assert steps == [7, 21, 42, 70]

        # Paths escalate, and a path the chassis lacks costs its opening price.
        fire = costs["paths"]["fire"]
        assert fire["by_level"][fire["starting_level"]]["total"] == 0
        levels = [
            fire["by_level"][n]["cost_from_previous_level"]
            for n in range(2, 7)
        ]
        assert levels == sorted(levels) and levels[0] < levels[-1]

        # Bless points are a separate currency and are stated as such.
        assert costs["bless_points"]["national_flat_bonus"] == 3

        # The published prices must add up to what evaluation actually charges.
        args = {
            "chassis_id": 3894, "awakening": "dormant", "dominion": 6,
            "paths": {"fire": 6, "air": 6},
            "scales": {"order": 2, "productivity": 1},
            "blessing_ids": [],
        }
        actual = session.call("evaluate_pretender", args)["result"]["design_points"]
        predicted_scales = (
            costs["scales"]["all_neutral_cost"]
            + costs["scales"]["delta_by_value"]["order"][2]
            + costs["scales"]["delta_by_value"]["productivity"][1]
        )
        assert predicted_scales == actual["scales"]
        assert costs["dominion"]["by_value"][6] == actual["dominion"]
        assert costs["chassis"]["dormant"] == actual["chassis"]
        assert (costs["paths"]["fire"]["by_level"][6]["total"]
                + costs["paths"]["air"]["by_level"][6]["total"]) == actual["paths"]
    finally:
        session.close()


def test_costs_tool_states_the_national_scale_position_a_formula_would_miss():
    """Neutral is not free for every nation, and that is not derivable."""
    caelum = open_pretender_session(71, reference_db=REFERENCE_DB)
    ermor = open_pretender_session(54, reference_db=REFERENCE_DB)
    try:
        first = lambda s: s.call(
            "list_pretender_chassis", {"awakening": "awake"}
        )["result"]["chassis"][0]["chassis_id"]

        # MA Caelum prefers Cold 3, so an all-neutral set already refunds 120
        # and returning to its preference gives that back.
        cold = session_costs(caelum, first(caelum))
        assert cold["scales"]["all_neutral_cost"] == -120
        assert cold["scales"]["delta_by_value"]["heat"][-3] == 120
        # Deviating past neutral earns nothing further. Cold limit +1 shifts
        # Caelum's whole temperature window, so Heat stops at 1.
        assert cold["scales"]["delta_by_value"]["heat"][1] == 0
        assert 2 not in cold["scales"]["delta_by_value"]["heat"]
        assert cold["scales"]["legal_range"]["heat"]["minimum"] == -3
        assert cold["scales"]["legal_range"]["heat"]["maximum"] == 1

        # Ermor starts at Growth -3, so neutral growth costs it 120.
        death = session_costs(ermor, first(ermor))
        assert death["scales"]["all_neutral_cost"] == 120
        assert death["scales"]["delta_by_value"]["growth"][-3] == -120
    finally:
        caelum.close()
        ermor.close()


def session_costs(session, chassis_id):
    result = session.call(
        "get_pretender_costs", {"chassis_id": chassis_id, "awakening": "awake"})
    assert result["ok"], result
    return result["result"]


def test_chassis_listing_says_what_each_chassis_is_for():
    """Paths and costs alone cannot separate a battle god from a lab god."""
    session = open_pretender_session(23, reference_db=REFERENCE_DB)
    try:
        rows = {
            row["chassis_id"]: row
            for row in session.call(
                "list_pretender_chassis", {"awakening": "awake"}
            )["result"]["chassis"]
        }

        # Master Lich and Demilich have identical paths and near-identical
        # costs; only the body distinguishes them.
        lich, demilich = rows[179], rows[180]
        assert lich["starting_paths"] == demilich["starting_paths"] == {"death": 3}
        assert lich["stats"]["hp"] == 30 and demilich["stats"]["hp"] == 3
        assert "immobile" not in lich["traits"]
        assert demilich["traits"]["immobile"] == 1
        assert lich["traits"]["immortal"] == demilich["traits"]["immortal"] == 1

        # A chassis with nothing notable says so rather than omitting the key.
        assert "traits" in rows[244]
    finally:
        session.close()


def test_chassis_detail_reports_the_exact_unit_record():
    session = open_pretender_session(23, reference_db=REFERENCE_DB)
    try:
        detail = session.call(
            "describe_pretender_chassis", {"chassis_id": 179})
        assert detail["ok"]
        lich = detail["result"]
        assert lich["name"] == "Master Lich"
        assert [w["name"] for w in lich["weapons"]] == ["Magic Sceptre"]
        assert [a["name"] for a in lich["armor"]] == ["Crown"]
        assert lich["resistances"]["fireres"] == -10
        assert lich["resistances"]["poisonres"] == 25
        assert lich["abilities"]["immortal"] == 1
        assert lich["abilities"]["invulnerable"] == 25
        assert lich["leadership_reference_values"]["undeadleader"] == 100
        assert lich["equipment_slots"]["misc"] == 2
        # The card text the game itself shows, taken from the 6.36 binary.
        assert lich["description"].startswith("A Master Lich is the dried husk")
        assert "dried husk of an Arch Mage" in lich["description"]

        missing = session.call("describe_pretender_chassis", {"chassis_id": 999999})
        assert not missing["ok"]
    finally:
        session.close()


def test_chassis_detail_refuses_a_chassis_this_nation_cannot_take():
    """The tool must not become a way to inspect another nation's options."""
    session = open_pretender_session(61, reference_db=REFERENCE_DB)
    try:
        # Master Lich is in MA Marignon's realm superset but client-rejected.
        refused = session.call("describe_pretender_chassis", {"chassis_id": 179})
        assert not refused["ok"]
    finally:
        session.close()


def test_every_listed_chassis_carries_its_in_game_description():
    """The description was missing entirely: dom6inspector keeps it in
    gamedata/unitdescr/, a directory of .txt files that the CSV-only refresh
    filter had always skipped."""
    for nation in (23, 61):
        session = open_pretender_session(nation, reference_db=REFERENCE_DB)
        try:
            rows = session.call(
                "list_pretender_chassis", {"awakening": "awake"}
            )["result"]["chassis"]
            assert rows
            missing = [r["name"] for r in rows if not r.get("description")]
            assert not missing, f"nation {nation} chassis without text: {missing}"
            assert all(len(r["description"]) > 40 for r in rows)
        finally:
            session.close()


def test_chassis_reports_the_resolved_scale_range_not_just_its_own_modifier():
    """EA Yomi's nation screen reads "Turmoil limit +1" and Oni Kunshu adds
    another +1 to the same direction. They do not stack: Turmoil 3, Order 1."""
    session = open_pretender_session(23, reference_db=REFERENCE_DB)
    try:
        listed = session.call(
            "list_pretender_chassis", {"awakening": "awake"})["result"]
        assert "shifts that whole five-step window" in listed["scale_limit_rule"]

        oni = next(r for r in listed["chassis"] if r["chassis_id"] == 2203)
        assert oni["scale_limit_modifiers"] == {"order": -1}
        assert oni["scale_legal_range"]["order"] == [-3, 1]
        assert oni["scale_legal_range"]["magic"] == [-2, 2]

        detail = session.call(
            "describe_pretender_chassis", {"chassis_id": 2203})["result"]
        order = detail["scale_legal_range"]["order"]
        assert order["nation_modifier"] == -1
        assert order["chassis_modifier"] == -1
        assert (order["minimum"], order["maximum"]) == (-3, 1)

        refused = session.call("evaluate_pretender", {
            "chassis_id": 2203, "awakening": "awake", "dominion": 3,
            "paths": {"fire": 1, "earth": 1, "death": 1},
            "scales": {"order": 3}, "blessing_ids": []})
        assert not refused["ok"]
        assert "outside the legal range" in refused["error"]
    finally:
        session.close()


def test_dagon_cost_tool_rejects_productivity_two_and_reports_45_remaining():
    session = open_pretender_session(43, reference_db=REFERENCE_DB)
    try:
        costs = session_costs(session, 109)
        productivity = costs["scales"]["delta_by_value"]["productivity"]
        assert sorted(productivity) == [-3, -2, -1, 0, 1]

        args = {
            "chassis_id": 109, "awakening": "dormant", "dominion": 4,
            "paths": {"earth": 5, "water": 5},
            "scales": {
                "order": 0, "productivity": 1, "heat": 0,
                "growth": 0, "luck": 0, "magic": 2,
            },
            "blessing_ids": [37, 27, 27, 25],
        }
        evaluated = session.call("evaluate_pretender", args)
        assert evaluated["ok"], evaluated
        assert evaluated["result"]["design_points"]["remaining"] == 45

        impossible = session.call("evaluate_pretender", {
            **args, "scales": {**args["scales"], "productivity": 2},
        })
        assert not impossible["ok"]
        assert "legal range -3..1" in impossible["error"]
    finally:
        session.close()


def test_dagon_path_table_names_both_sides_of_each_level_unambiguously():
    session = open_pretender_session(43, reference_db=REFERENCE_DB)
    try:
        costs = session_costs(session, 109)
        earth5 = costs["paths"]["earth"]["by_level"][5]
        water5 = costs["paths"]["water"]["by_level"][5]

        # Dagon starts at E2 W1. The selected level is the same, but the
        # distance from the chassis base is not.
        assert costs["paths"]["earth"]["starting_level"] == 2
        assert costs["paths"]["water"]["starting_level"] == 1
        assert earth5 == {
            "total": 48,
            "cost_from_previous_level": 24,
            "cost_to_next_level": 32,
        }
        assert water5 == {
            "total": 80,
            "cost_from_previous_level": 32,
            "cost_to_next_level": 40,
        }
        assert "5->6 costs 32" in session.call(
            "get_pretender_rules", {})["result"]["cost_model"]["paths"]
    finally:
        session.close()


def test_descriptions_can_be_omitted_when_context_is_tight():
    """65 chassis with their card text is ~78 KB, which a small local model
    cannot absorb. The text is on by default because it is what makes a
    chassis legible; detail=costs is the escape hatch."""
    session = open_pretender_session(61, reference_db=REFERENCE_DB)
    try:
        full = session.call(
            "list_pretender_chassis", {"awakening": "awake"})["result"]
        lean = session.call(
            "list_pretender_chassis",
            {"awakening": "awake", "detail": "costs"})["result"]

        assert full["detail"] == "full" and lean["detail"] == "costs"
        assert all(row["description"] for row in full["chassis"])
        assert all(row["description"] is None for row in lean["chassis"])
        # Everything used to price and triage survives either way.
        assert [r["chassis_id"] for r in full["chassis"]] == [
            r["chassis_id"] for r in lean["chassis"]]
        assert all(r["stats"] for r in lean["chassis"])
        assert "detail=full" in lean["note"]
    finally:
        session.close()
