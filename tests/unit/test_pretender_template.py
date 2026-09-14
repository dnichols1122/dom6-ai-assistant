from __future__ import annotations

import base64

import pytest

from dom6_assistant.file_reader.formats.pretender import (
    Awakening,
    Pretender,
    build_template,
    calculate_checksum,
    parse_bytes,
    verify_checksum,
)


def _fixture(value: str) -> bytes:
    return base64.b64decode("".join(value.split()))


SUGAAR = _fixture("""
AQIERE9NHWICAHwCAAAAAAAAAAAAAP////89AAAAAAAAAAAAAAAhKjgjID0rPE94eAAAAAACMQAAAAAAAL0BAAAA
AAAAAAAAAAAAAAAAAAAAAAQAAAAAAAD/////Ng9zAP////8AAHwCAAAAAAAAAAAAAAAAAAAAAAAAUgc9AP////8A
AAAAAAAAAAAAAP////82DwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHDooLi49TwIAAAD/////////////////////////
/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/////////////AAAAAAAAAAYG
BgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACMf7/AAAAAE8BAAAAAAAA
AAAAAAAEAAAAAAAAAAcAAAAGAAAAAwAAAAMAAAAcOiguLj1PT0+sC3sAAAAGBgAAAAAAAAAAiVoAAAOF
""")

WEEPING_ONE = _fixture("""
AQIERE9NHWICAHwCAAAAAAAAAAAAAP////8HAAAAAAAAAAAAAAAhKjgjID0rPE94eAAAAAACMQAAAAAAAL0BAAAA
AAAAAAAAAAAAAAAAAAAAAAQAAAAAAAD/////kAIjAP////8AAHwCAAAAAAAAAAAAAAAAAAAAAAAA7QcHAP////8A
AAAAAAAAAAAAAP////+QAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGwcKbxgKCh8GAQhvAAEKTwAAAAD/////////////
/////////////wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/////////////
AAAAAAAAAAgAAAAAAAAIAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACMQMAAP3+
/k8CAAAAAAAAAAAAAAAIAAAAAAAAAFUAAABWAAAAUgAAAFIAAABSAAAAUgAAAFIAAAA/AAAAGwcKbxgKCh8GAQhv
AAEKT09PrAt7AAAAAAAAAAAACAAIAIlaAAAxLA==
""")

DRAGON = _fixture("""
AQIERE9NHWICAHwCAAAAAAAAAAAAAP////9HAAAAAAAAAAAAAAAhKjgjID0rPE94eAAAAAACMQAAAAAAAL0BAAAA
AAAAAAAAAAAAAAAAAAAAAAQAAAAAAAD/////fg59AP////8AAHwCAAAAAAAAAAAAAAAAAAAAAAAADAhHAP////8A
AAAAAAAAAAAAAP////9/DgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADS41KCY9TwAAAAD/////////////////////////
/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/////////////AAAAAAAAAAQA
CAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACMQAAAwAAAE8AAAAAAAAA
AAAAAAADAAAAAAAAAFsAAAANAAAADQAAAA0uNSgmPU9PT6wLewAAAAAIAAAAAAAAAACJWgAAAzo=
""")


def test_real_template_is_structural_and_round_trips_exactly():
    design = parse_bytes(SUGAAR, nation_slug="mid_marignon")

    assert design.name == "Sugaar"
    assert design.nation_id == 61
    assert design.chassis_id == 3894
    assert design.awakening is Awakening.DORMANT
    assert design.dominion == 6
    assert design.paths == {"fire": 6, "air": 6}
    assert design.scales["order"] == 2
    assert design.scales["productivity"] == 1
    assert design.blessing_ids == (7, 6, 3, 3)
    assert design.checksum == 0x8503
    assert design.checksum_valid
    assert design.to_bytes() == SUGAAR


def test_raw_zero_is_an_uppercase_o_not_a_string_terminator():
    design = parse_bytes(WEEPING_ONE)

    assert design.name == "THE WEEPING ONE"
    assert design.blessing_ids == (85, 86, 82, 82, 82, 82, 82, 63)
    assert design.to_bytes() == WEEPING_ONE


def test_shape_changer_has_a_distinct_serialized_form():
    dragon = parse_bytes(DRAGON)

    assert dragon.chassis_id == 3710
    assert dragon.serialized_form_id == 3711
    assert dragon.to_bytes() == DRAGON


def test_edit_relocates_variable_sections_and_rechecksums():
    original = parse_bytes(SUGAAR)
    changed = original.edited(
        name="SUGAAR ONE",
        awakening=Awakening.AWAKE,
        dominion=7,
        paths={"fire": 7, "air": 6},
        blessing_ids=(7, 6, 3),
    )

    encoded = changed.to_bytes()
    decoded = parse_bytes(encoded)
    assert decoded.name == "SUGAAR ONE"
    assert decoded.awakening is Awakening.AWAKE
    assert decoded.dominion == 7
    assert decoded.paths == {"fire": 7, "air": 6}
    assert decoded.blessing_ids == (7, 6, 3)
    assert verify_checksum(encoded)
    assert calculate_checksum(encoded[:-2]) == int.from_bytes(encoded[-2:], "little")


def test_corrupt_template_is_rejected_by_default():
    corrupted = bytearray(SUGAAR)
    corrupted[0x1A] ^= 1

    with pytest.raises(ValueError, match="checksum mismatch"):
        parse_bytes(bytes(corrupted))

    decoded = parse_bytes(bytes(corrupted), require_valid_checksum=False)
    assert decoded.checksum_valid is False


def test_editor_refuses_fake_chassis_swaps():
    original = parse_bytes(SUGAAR)

    with pytest.raises(ValueError, match="static chassis block"):
        original.edited(chassis_id=905).to_bytes()


def test_builder_constructs_arbitrary_client_serialized_chassis():
    design = Pretender(
        name="NEW SUGAAR",
        nation_id=61,
        chassis_id=3894,
        awakening=Awakening.DORMANT,
        dominion=6,
        paths={"fire": 6, "air": 6},
        scales={"order": 2, "productivity": 1},
        blessing_ids=(7, 6, 3, 3),
    )

    decoded = parse_bytes(build_template(design))
    assert decoded.name == "NEW SUGAAR"
    assert decoded.nation_id == 61
    assert decoded.chassis_id == 3894
    assert decoded.serialized_form_id == 3894
    assert decoded.dominion == 6
    assert decoded.paths == {"fire": 6, "air": 6}
    assert decoded.scales["order"] == 2
    assert decoded.scales["productivity"] == 1
    assert decoded.blessing_ids == (7, 6, 3, 3)
    assert decoded.checksum_valid


def test_builder_retains_distinct_shape_change_display_form():
    design = Pretender(
        name="STORM DRAGON",
        nation_id=71,
        chassis_id=3710,
        awakening=Awakening.AWAKE,
        dominion=4,
        paths={"air": 8},
        scales={},
    )

    decoded = parse_bytes(design.to_bytes())
    assert decoded.chassis_id == 3710
    assert decoded.serialized_form_id == 3711
