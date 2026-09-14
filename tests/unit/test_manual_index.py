"""The offline manual index.

Fixtures are synthetic pages rather than the real manual: the PDF is Illwinter's
and is not in the repository, so a test that needed it would fail everywhere
except on a machine that had already fetched it.

The two things worth protecting are the structure parse and the page mapping.
Both were established by measurement -- the contents pages give the sections,
and the printed page number in each footer gives the offset -- and both have a
failure mode that is silent rather than loud, which is the dangerous kind: an
index that builds cleanly and then finds nothing looks exactly like a manual
that does not cover the topic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dom6_assistant.manual import index as manual

original_parse = manual.parse_contents


def make_pages() -> list[str]:
    """A contents page, then numbered content pages.

    The footer convention matches the real document: the last line of each
    content page is its printed number, and the printed number is one more
    than the page's index in the file.
    """
    contents = "\n".join([
        "Manual Dominions 6",
        "Illwinter Game Design  (revision 2)",
        "Beginnings 2",
        "  Provinces 3",
        "  Supplies 4",
        "Endings 5",
    ])
    body = []
    for printed in range(2, 7):
        body.append(f"text for page {printed} about widgets\n{printed}")
    return [contents] + body


def test_contents_become_page_bounded_sections():
    sections = manual.parse_contents(make_pages(), scan=1, minimum=3)

    titles = [s.title for s in sections]
    assert titles == ["Beginnings", "Provinces", "Supplies", "Endings"]
    beginnings = sections[0]
    assert (beginnings.start_page, beginnings.end_page) == (2, 2)
    assert sections[-1].start_page == 5


def test_cover_lines_are_not_sections():
    """"Manual Dominions 6" parses as a title plus a page number otherwise."""
    titles = [s.title for s in manual.parse_contents(make_pages(), scan=1, minimum=3)]
    assert not any("Illwinter Game Design" in t for t in titles)
    assert not any(t.startswith("Manual Dominions") for t in titles)


def test_a_layout_it_cannot_read_fails_loudly():
    """A silent empty index is the failure mode this guards against."""
    with pytest.raises(manual.ManualError, match="contents entries"):
        manual.parse_contents(["nothing here resembles a contents page"], scan=1)


def test_the_page_offset_is_measured_not_assumed():
    assert manual.page_number_offset(make_pages()) == -1


def test_an_unnumbered_document_does_not_crash():
    assert manual.page_number_offset(["no footer", "still none"]) == 0


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

@pytest.fixture()
def built(tmp_path: Path, monkeypatch) -> Path:
    """Build an index from the synthetic pages, with no PDF involved."""
    database = tmp_path / "manual.sqlite3"
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    monkeypatch.setattr(manual, "extract_pages", lambda _: make_pages())
    monkeypatch.setattr(manual, "parse_contents",
                        lambda pages, **kw: original_parse(pages, scan=1, minimum=3))
    manual.build(pdf=pdf, database_path=database)
    return database


def test_search_returns_a_page_a_player_can_turn_to(built):
    result = manual.search("widgets", limit=5, database_path=built)

    assert result["results"], "the corpus contains the term"
    first = result["results"][0]
    assert isinstance(first["page"], int)
    assert first["section"] in {"Beginnings", "Provinces", "Supplies", "Endings"}
    assert "Illwinter" in result["source"], "excerpts must stay attributed"


def test_punctuation_does_not_break_the_query(built):
    """FTS5 reads bare punctuation as syntax; a user query is not syntax."""
    result = manual.search('widgets "and" (things)', database_path=built)
    assert "results" in result


def test_reading_a_page_names_its_section(built):
    page = manual.read_page(3, database_path=built)
    assert page["page"] == 3
    assert page["section"] == "Provinces"


def test_an_out_of_range_page_says_the_range(built):
    with pytest.raises(KeyError, match="manual runs"):
        manual.read_page(999, database_path=built)


def test_an_unbuilt_index_explains_how_to_build_it(tmp_path):
    with pytest.raises(manual.ManualError, match="--fetch --build"):
        manual.search("anything", database_path=tmp_path / "absent.sqlite3")


def test_status_reports_not_built_without_raising(tmp_path):
    state = manual.status(database_path=tmp_path / "absent.sqlite3",
                          pdf=tmp_path / "absent.pdf")
    assert state["built"] is False
    assert "--fetch" in state["hint"]


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

def test_every_workspace_can_reach_the_manual():
    """Rules questions come up in all three, not only during a live turn."""
    from dom6_assistant.agent import open_tools, pretender_tools, read_tools
    from dom6_assistant.agent.registry import ToolRegistry

    def names(reg):
        for attr in ("names", "tool_names"):
            if hasattr(reg, attr):
                return set(getattr(reg, attr)())
        return set(getattr(reg, "_tools", {}))

    wanted = {"search_manual", "read_manual_page", "list_manual_sections"}
    for registry in (read_tools.register(ToolRegistry()),
                     open_tools.register(),
                     pretender_tools.register()):
        assert wanted <= names(registry)


def test_the_tool_explains_itself_when_nothing_is_built(tmp_path):
    from dom6_assistant.agent.registry import ToolRegistry
    from dom6_assistant.agent import manual_tools

    reg = manual_tools.register(ToolRegistry(),
                                database_path=tmp_path / "absent.sqlite3")

    # The registry turns a ToolError into a result rather than a crash, on
    # purpose: a refusal is something the model should read and act on.
    result = reg.call(None, "search_manual", {"query": "supplies"})

    assert result["ok"] is False
    # The model has to be told not to pretend it consulted the manual.
    assert "--fetch --build" in result["error"]
    assert "rather than implying you consulted it" in result["error"]
