"""The strategy-video transcript index.

No test here touches the network. Transcripts are fabricated, which is both
faster and the only honest option: real transcripts belong to the people who
recorded them and are not in this repository.

The boundary worth guarding hardest is `parse_video_id`. One of the two ways a
video gets indexed is a URL handed over mid-conversation, which means a model
can cause a fetch. Restricting that to YouTube ids before any request is made
is what stops the tool being a general-purpose web fetcher.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dom6_assistant.videos import index as videos


# ---------------------------------------------------------------------------
# Addresses: the safety boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("address", [
    "https://www.youtube.com/watch?v=yk4uJ81GVjU",
    "http://youtube.com/watch?v=yk4uJ81GVjU",
    "https://www.youtube.com/watch?list=PL123&v=yk4uJ81GVjU",
    "https://youtu.be/yk4uJ81GVjU?t=90",
    "https://www.youtube.com/shorts/yk4uJ81GVjU",
    "https://www.youtube.com/embed/yk4uJ81GVjU",
    "yk4uJ81GVjU",
])
def test_every_shape_of_youtube_address_resolves(address):
    assert videos.parse_video_id(address) == "yk4uJ81GVjU"


@pytest.mark.parametrize("address", [
    "https://example.com/evil.mp4",
    "file:///etc/passwd",
    "http://127.0.0.1:8001/api/settings",
    "https://youtube.com.evil.test/watch?v=yk4uJ81GVjU",
    "not a url at all",
    "",
])
def test_anything_that_is_not_a_youtube_video_is_refused(address):
    """A model must not be able to talk this into fetching an arbitrary URL."""
    with pytest.raises(videos.VideoError):
        videos.parse_video_id(address)


def test_the_refusal_explains_the_restriction():
    with pytest.raises(videos.VideoError, match="not a general web fetcher"):
        videos.parse_video_id("https://example.com/clip")


# ---------------------------------------------------------------------------
# Caption parsing
# ---------------------------------------------------------------------------

def test_json3_captions_become_timed_cues():
    raw = json.dumps({"events": [
        {"tStartMs": 0, "segs": [{"utf8": "first "}, {"utf8": "words"}]},
        {"tStartMs": 5000, "segs": [{"utf8": "later words"}]},
        {"tStartMs": 9000},                       # no segs: a positioning event
        {"tStartMs": 9500, "segs": [{"utf8": "  "}]},   # whitespace only
    ]}).encode()

    cues = videos.parse_json3(raw)

    assert [c.text for c in cues] == ["first words", "later words"]
    assert cues[1].start == 5.0


def test_vtt_captions_become_timed_cues():
    raw = (b"WEBVTT\n\n"
           b"00:00:02.000 --> 00:00:04.000\nopening line\n\n"
           b"01:00:06.500 --> 01:00:08.000\n<c>styled</c> line\n")

    cues = videos.parse_vtt(raw)

    assert cues[0].start == 2.0
    assert cues[0].text == "opening line"
    assert cues[1].start == 3606.0, "an hour field must count"
    assert "<c>" not in cues[1].text, "style tags are not speech"


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def test_cues_group_into_windows_that_keep_a_usable_timestamp():
    cues = [videos.Cue(start=float(t), text=f"word{t}") for t in range(0, 180, 10)]

    chunks = videos.chunk_cues(cues, window=60)

    assert len(chunks) == 3
    assert [c[0] for c in chunks] == [0, 60, 120]
    assert "word0" in chunks[0][2] and "word50" in chunks[0][2]


def test_an_empty_transcript_chunks_to_nothing():
    assert videos.chunk_cues([]) == []


def test_clock_formatting_crosses_an_hour():
    assert videos.clock(75) == "1:15"
    assert videos.clock(3661) == "1:01:01"


def test_a_citation_links_to_the_moment():
    assert videos.timestamp_url("yk4uJ81GVjU", 90).endswith("v=yk4uJ81GVjU&t=90s")


# ---------------------------------------------------------------------------
# Indexing and retrieval
# ---------------------------------------------------------------------------

def fake_fetcher(video_id: str):
    meta = {"id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": "A guide", "channel": "Someone", "duration": 240,
            "subtitles": "automatic", "language": "en"}
    cues = [videos.Cue(start=0.0, text="open with black knights"),
            videos.Cue(start=70.0, text="recruit smiths steadily"),
            videos.Cue(start=140.0, text="earth buffs for the infantry")]
    return meta, cues


@pytest.fixture()
def indexed(tmp_path: Path) -> Path:
    database = tmp_path / "videos.sqlite3"
    videos.add_video("aaaaaaaaaaa", database_path=database, fetcher=fake_fetcher)
    return database


def test_a_hit_carries_a_timestamp_and_a_link(indexed):
    result = videos.search("smiths", database_path=indexed)

    assert result["results"], "the fixture says this"
    hit = result["results"][0]
    assert hit["at"] == "1:10"
    assert hit["link"].endswith("&t=70s")
    assert hit["captions"] == "automatic"
    assert "older version" in result["note"], "the caveat travels with the hit"


def test_automatic_captions_are_flagged_on_every_result(indexed):
    for hit in videos.search("knights", database_path=indexed)["results"]:
        assert hit["captions"] in {"automatic", "authored"}


def test_punctuation_in_a_query_does_not_break_search(indexed):
    assert "results" in videos.search('smiths "and" (buffs)', database_path=indexed)


def test_reading_a_segment_returns_the_span(indexed):
    segment = videos.read_segment("aaaaaaaaaaa", 0, window=200,
                                  database_path=indexed)
    assert "black knights" in segment["text"]
    assert "smiths" in segment["text"], "the window covers the next chunk too"
    assert segment["channel"] == "Someone"


def test_re_adding_leaves_no_stale_search_rows(indexed):
    """An external-content FTS table does not hear about deletes by itself.

    Without a rebuild the old chunks stay searchable, so a hit points at a
    timestamp that no longer exists.
    """
    def shorter(video_id):
        meta, _ = fake_fetcher(video_id)
        return meta, [videos.Cue(start=0.0, text="completely different advice")]

    videos.add_video("aaaaaaaaaaa", database_path=indexed, fetcher=shorter)

    assert not videos.search("smiths", database_path=indexed)["results"]
    assert videos.search("different", database_path=indexed)["results"]


def test_removing_a_video_removes_its_hits(indexed):
    videos.remove_video("aaaaaaaaaaa", database_path=indexed)

    assert videos.list_videos(database_path=indexed)["count"] == 0
    assert not videos.search("smiths", database_path=indexed)["results"]


def test_listing_reports_caption_quality(indexed):
    row = videos.list_videos(database_path=indexed)["videos"][0]
    assert row["subtitles"] == "automatic"
    assert row["chunks"] == 3


def test_an_empty_library_says_how_to_fill_it(tmp_path):
    with pytest.raises(videos.VideoError, match="video-add"):
        videos.search("anything", database_path=tmp_path / "none.sqlite3")


def test_status_does_not_raise_on_an_empty_library(tmp_path):
    state = videos.status(database_path=tmp_path / "none.sqlite3")
    assert state["built"] is False and state["videos"] == 0


def test_a_video_with_no_captions_stores_nothing(tmp_path):
    """Half an entry is worse than none: it would look indexed and find zero."""
    def no_cues(video_id):
        return fake_fetcher(video_id)[0], []

    database = tmp_path / "videos.sqlite3"
    with pytest.raises(videos.VideoError):
        videos.add_video("aaaaaaaaaaa", database_path=database,
                         fetcher=no_cues)
    assert videos.status(database_path=database)["videos"] == 0


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

def test_every_workspace_can_reach_the_videos():
    from dom6_assistant.agent import open_tools, pretender_tools, read_tools
    from dom6_assistant.agent.registry import ToolRegistry

    wanted = {"search_videos", "read_video_segment", "list_videos", "add_video"}
    for registry in (read_tools.register(ToolRegistry()),
                     open_tools.register(),
                     pretender_tools.register()):
        assert wanted <= set(registry.names())


def test_the_tool_refuses_a_non_youtube_url_without_fetching(tmp_path):
    from dom6_assistant.agent import video_tools
    from dom6_assistant.agent.registry import ToolRegistry

    reg = video_tools.register(ToolRegistry(),
                               database_path=tmp_path / "videos.sqlite3")

    result = reg.call(None, "add_video", {"url": "https://example.com/x.mp4"})

    assert result["ok"] is False
    assert "not a general web fetcher" in result["error"]


def test_an_empty_search_tells_the_model_not_to_invent(tmp_path):
    from dom6_assistant.agent import video_tools
    from dom6_assistant.agent.registry import ToolRegistry

    database = tmp_path / "videos.sqlite3"
    videos.add_video("aaaaaaaaaaa", database_path=database, fetcher=fake_fetcher)
    reg = video_tools.register(ToolRegistry(), database_path=database)

    result = reg.call(None, "search_videos", {"query": "zzzzunrelated"})

    assert result["ok"] is True
    assert "Do not invent" in result["result"]["note"]
