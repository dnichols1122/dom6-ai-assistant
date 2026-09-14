"""Timestamped transcripts of strategy videos, searchable offline.

Same rule as the manual and the wiki mirror: a transcript is the creator's
work, so it is fetched onto the user's own machine and indexed there.
`knowledge/videos/` is ignored by git and nothing is redistributed.

Why transcripts rather than frames. A Dominions strategy video is someone
talking over a mostly static interface, so nearly all the information is in the
speech. Text also works on every backend the project supports, including a
local model with no vision at all, and it costs a fraction of what sampling
images would.

Two ways in, deliberately:

* prepared ahead of time, like the wiki and the manual, with `video-add`
* by URL during a conversation, when the player wants to discuss a video they
  are looking at right now

The second means a model can cause a network fetch mid-turn, so the address is
validated to a YouTube id before anything is requested. That bound is the
safety story: a tool that took arbitrary URLs would be a general-purpose
fetcher wearing a video costume.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_DIR = Path("knowledge") / "videos"
DEFAULT_INDEX_DB = DEFAULT_DIR / "videos.sqlite3"

#: Seconds of speech per indexed chunk. Long enough to hold a complete thought,
#: short enough that the timestamp lands near what was actually said.
CHUNK_SECONDS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id         TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    title      TEXT,
    channel    TEXT,
    duration   INTEGER,
    subtitles  TEXT,          -- 'authored' or 'automatic'
    language   TEXT,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY,
    video_id      TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds   INTEGER NOT NULL,
    text          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_video ON chunks(video_id, start_seconds);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(text, content='chunks', content_rowid='id', tokenize='porter');
"""


class VideoError(RuntimeError):
    """The video cannot be identified, fetched, or read."""


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATTERNS = (
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/watch\?(?:.*&)?v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})"),
)


def parse_video_id(url_or_id: str) -> str:
    """Reduce a YouTube address to its video id, or refuse.

    This is the whole boundary on the runtime path. Anything that is not
    recognisably a YouTube video is rejected before a request is made, so the
    tool cannot be talked into fetching an arbitrary address.
    """
    candidate = (url_or_id or "").strip()
    if not candidate:
        raise VideoError("no video address given")
    if _VIDEO_ID.match(candidate):
        return candidate
    for pattern in _PATTERNS:
        found = pattern.search(candidate)
        if found:
            return found.group(1)
    raise VideoError(
        f"{candidate!r} is not a YouTube video address. Give a youtube.com "
        "or youtu.be link, or the 11-character video id. Only YouTube is "
        "supported, deliberately: this tool is not a general web fetcher.")


def timestamp_url(video_id: str, seconds: int) -> str:
    """A link that opens the video at the moment being cited."""
    return f"https://www.youtube.com/watch?v={video_id}&t={int(seconds)}s"


def clock(seconds: int) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cue:
    start: float
    text: str


def _download(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "dom6-assistant (transcript fetch)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data: bytes = response.read()
    return data


def parse_json3(raw: bytes) -> list[Cue]:
    """YouTube's json3 caption format: events carrying start time and spans."""
    data = json.loads(raw.decode("utf-8", errors="replace"))
    cues: list[Cue] = []
    for event in data.get("events", []):
        segments = event.get("segs")
        if not segments:
            continue
        text = "".join(seg.get("utf8", "") for seg in segments).strip()
        if text:
            cues.append(Cue(start=event.get("tStartMs", 0) / 1000.0, text=text))
    return cues


# The end timestamp and any cue settings (align, position) have to be consumed
# too, not just matched up to the arrow -- otherwise they stay in the caption
# text and every indexed chunk carries a stray timecode.
_VTT_TIME = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*"
    r"(?:\d+:)?\d{2}:\d{2}[.,]\d{3}[^\n]*\n?", re.M)


def parse_vtt(raw: bytes) -> list[Cue]:
    """WebVTT, the other format yt-dlp commonly hands back."""
    text = raw.decode("utf-8", errors="replace")
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        match = _VTT_TIME.search(block)
        if not match:
            continue
        hours = int(match.group(1) or 0)
        start = hours * 3600 + int(match.group(2)) * 60 + int(match.group(3))
        body = block[match.end():]
        body = re.sub(r"<[^>]+>", "", body)
        body = " ".join(line.strip() for line in body.splitlines() if line.strip())
        if body:
            cues.append(Cue(start=float(start), text=body))
    return cues


def fetch_transcript(video_id: str) -> tuple[dict[str, Any], list[Cue]]:
    """Metadata and caption cues for one video, via yt-dlp.

    Author-written captions are preferred over automatic ones and the choice is
    recorded, because automatic captions mangle exactly the vocabulary that
    matters here -- nation names, "thug", "bless", "SC" -- and a reader should
    be able to discount a hit accordingly.
    """
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-untyped]
    except ImportError as exc:
        raise VideoError(
            "yt-dlp is needed to fetch transcripts. Install it with:\n"
            "  uv sync --extra videos") from exc

    options = {"skip_download": True, "quiet": True, "no_warnings": True}
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception as exc:  # yt-dlp raises a family of its own errors
        raise VideoError(f"could not read video {video_id}: {exc}") from exc

    authored = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    for source, tracks in (("authored", authored), ("automatic", automatic)):
        for language, formats in tracks.items():
            if not language.startswith("en"):
                continue
            best = _preferred_format(formats)
            if not best:
                continue
            raw = _download(best["url"])
            cues = (parse_json3(raw) if best["ext"] == "json3"
                    else parse_vtt(raw))
            if cues:
                meta = {
                    "id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "title": info.get("title"),
                    "channel": info.get("uploader") or info.get("channel"),
                    "duration": info.get("duration"),
                    "subtitles": source,
                    "language": language,
                }
                return meta, cues

    raise VideoError(
        f"video {video_id} has no English captions, authored or automatic, so "
        "there is nothing to index. Nothing was stored.")


def _preferred_format(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_ext = {f.get("ext"): f for f in formats if f.get("url")}
    for ext in ("json3", "vtt", "srv1"):
        if ext in by_ext:
            return {"url": by_ext[ext]["url"], "ext": ext}
    return None


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_cues(cues: list[Cue],
               window: int = CHUNK_SECONDS) -> list[tuple[int, int, str]]:
    """Group cues into fixed windows so a hit carries a usable timestamp.

    Captions arrive as fragments of a few words. Indexed individually, a search
    matches a fragment too small to mean anything; indexed as one blob, the
    timestamp stops being useful. A window is the compromise.
    """
    if not cues:
        return []
    chunks: list[tuple[int, int, str]] = []
    start = int(cues[0].start)
    words: list[str] = []
    last = start
    for cue in cues:
        moment = int(cue.start)
        if moment - start >= window and words:
            chunks.append((start, last, " ".join(words)))
            start, words = moment, []
        words.append(cue.text)
        last = moment
    if words:
        chunks.append((start, last, " ".join(words)))
    return chunks


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _connect(database_path: Path, writable: bool = False) -> sqlite3.Connection:
    if not writable and not database_path.exists():
        raise VideoError(
            f"no video index at {database_path}. Add one with:\n"
            "  uv run dom6-assistant video-add <youtube url>")
    if writable:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(database_path)
        conn.executescript(SCHEMA)
    else:
        conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def add_video(url_or_id: str,
              database_path: Path = DEFAULT_INDEX_DB,
              fetcher: Callable[[str], tuple[dict[str, Any], list[Cue]]] | None = None,
              ) -> dict[str, Any]:
    """Fetch, chunk and index one video. Re-adding refreshes it."""
    video_id = parse_video_id(url_or_id)
    meta, cues = (fetcher or fetch_transcript)(video_id)
    chunks = chunk_cues(cues)
    if not chunks:
        raise VideoError(f"video {video_id} produced no transcript text")

    conn = _connect(database_path, writable=True)
    try:
        with conn:
            conn.execute("DELETE FROM chunks WHERE video_id=?", (video_id,))
            conn.execute(
                "INSERT OR REPLACE INTO videos"
                "(id,url,title,channel,duration,subtitles,language,fetched_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (video_id, meta["url"], meta.get("title"), meta.get("channel"),
                 meta.get("duration"), meta.get("subtitles"),
                 meta.get("language"),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            for start, end, text in chunks:
                conn.execute(
                    "INSERT INTO chunks(video_id,start_seconds,end_seconds,text)"
                    " VALUES(?,?,?,?)", (video_id, start, end, text))
            # An external-content FTS table does not learn about deletes from
            # its content table, so re-adding a video would leave the old
            # chunks searchable -- hits pointing at timestamps that no longer
            # exist. Rebuilding is cheap at this size and always correct.
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        return {"video_id": video_id, "title": meta.get("title"),
                "channel": meta.get("channel"), "chunks": len(chunks),
                "subtitles": meta.get("subtitles"),
                "url": meta["url"]}
    finally:
        conn.close()


def remove_video(url_or_id: str,
                 database_path: Path = DEFAULT_INDEX_DB) -> dict[str, Any]:
    video_id = parse_video_id(url_or_id)
    conn = _connect(database_path, writable=True)
    try:
        with conn:
            rows = conn.execute("DELETE FROM chunks WHERE video_id=?",
                                (video_id,)).rowcount
            conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        return {"video_id": video_id, "removed_chunks": rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _excerpt(text: str, query: str, width: int = 300) -> str:
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    lowered = text.lower()
    position = next((lowered.find(w) for w in words if lowered.find(w) >= 0), 0)
    start = max(0, position - width // 3)
    snippet = " ".join(text[start:start + width].split())
    return ("..." if start else "") + snippet + ("..." if start + width < len(text) else "")


def search(query: str, limit: int = 5,
           database_path: Path = DEFAULT_INDEX_DB) -> dict[str, Any]:
    """Find moments in indexed videos, cited by timestamp."""
    if not query.strip():
        raise ValueError("empty query")
    limit = max(1, min(int(limit), 15))
    conn = _connect(database_path)
    try:
        statement = (
            "SELECT c.video_id, c.start_seconds, c.text, "
            "v.title, v.channel, v.subtitles "
            "FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
            "JOIN videos v ON v.id = c.video_id "
            "WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?")
        try:
            rows = conn.execute(statement, (query, limit)).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                statement, ('"' + query.replace('"', " ") + '"', limit)).fetchall()
        return {
            "query": query,
            "results": [{
                "video_id": r["video_id"],
                "title": r["title"],
                "channel": r["channel"],
                "at": clock(r["start_seconds"]),
                "start_seconds": r["start_seconds"],
                "link": timestamp_url(r["video_id"], r["start_seconds"]),
                "captions": r["subtitles"],
                "excerpt": _excerpt(r["text"], query),
            } for r in rows],
            "note": ("Community video advice. Automatic captions mistake "
                     "Dominions vocabulary, and guides are often written for an "
                     "older version."),
        }
    finally:
        conn.close()


def read_segment(video_id: str, start_seconds: int, window: int = 300,
                 database_path: Path = DEFAULT_INDEX_DB) -> dict[str, Any]:
    """A span of transcript around a moment, for when an excerpt is cut off."""
    video_id = parse_video_id(video_id)
    conn = _connect(database_path)
    try:
        video = conn.execute("SELECT * FROM videos WHERE id=?",
                             (video_id,)).fetchone()
        if video is None:
            raise KeyError(f"video {video_id} is not indexed")
        rows = conn.execute(
            "SELECT start_seconds,text FROM chunks WHERE video_id=? "
            "AND start_seconds >= ? AND start_seconds < ? ORDER BY start_seconds",
            (video_id, int(start_seconds), int(start_seconds) + int(window))
        ).fetchall()
        return {
            "video_id": video_id, "title": video["title"],
            "channel": video["channel"], "captions": video["subtitles"],
            "from": clock(int(start_seconds)),
            "link": timestamp_url(video_id, int(start_seconds)),
            "text": " ".join(" ".join(r["text"].split()) for r in rows),
        }
    finally:
        conn.close()


def list_videos(database_path: Path = DEFAULT_INDEX_DB) -> dict[str, Any]:
    conn = _connect(database_path)
    try:
        rows = conn.execute(
            "SELECT v.id, v.title, v.channel, v.duration, v.subtitles, "
            "COUNT(c.id) AS chunks FROM videos v "
            "LEFT JOIN chunks c ON c.video_id = v.id GROUP BY v.id "
            "ORDER BY v.fetched_at DESC").fetchall()
        return {"videos": [dict(r) for r in rows], "count": len(rows)}
    finally:
        conn.close()


def status(database_path: Path = DEFAULT_INDEX_DB) -> dict[str, Any]:
    if not database_path.exists():
        return {"built": False, "videos": 0,
                "hint": "uv run dom6-assistant video-add <youtube url>"}
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        count = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"built": True, "videos": count, "chunks": chunks}
    finally:
        conn.close()
