"""A local, searchable index of Illwinter's Dominions 6 manual.

The manual is Illwinter's copyrighted work, so it is **fetched on the user's
machine and never redistributed** -- the same rule the reference data and the
wiki mirror follow. Nothing here ships with the source; `knowledge/manual/` is
ignored by git.

Structure comes from the document rather than from guesswork. The manual opens
with a five-page table of contents whose entries are "title .... page", and
every content page ends with its printed page number. Parsing those two things
gives 263 real sections and a page mapping that was measured, not assumed:
every printed page number is exactly one more than its index in the PDF, so a
citation the assistant returns matches the page a human has open.

Font-size heading detection was tried first and abandoned. `pdftotext -layout`
discards the size information, and the numeric lines that survive are battle
timings and item tables rather than headings -- 52 false candidates, no real
ones.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

#: Illwinter publishes the manual free; this is the canonical location.
MANUAL_URL = "https://www.illwinter.com/dom6/dom6manual.pdf"

DEFAULT_DIR = Path("knowledge") / "manual"
DEFAULT_PDF = DEFAULT_DIR / "dom6manual.pdf"
DEFAULT_INDEX_DB = DEFAULT_DIR / "manual.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sections (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    start_page INTEGER NOT NULL,
    end_page   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
    page       INTEGER PRIMARY KEY,
    section_id INTEGER REFERENCES sections(id),
    text       TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts
    USING fts5(text, content='pages', content_rowid='page', tokenize='porter');
"""


class ManualError(RuntimeError):
    """The manual is not fetched, not built, or not readable."""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(destination: Path = DEFAULT_PDF, url: str = MANUAL_URL,
          force: bool = False) -> dict[str, Any]:
    """Download the manual to *destination*, unless it is already there.

    Returns the size and digest so a build can record which revision it read.
    Illwinter revises the manual in place, so the digest is the only way to
    tell two downloads apart.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        raw = destination.read_bytes()
        return {"path": str(destination), "bytes": len(raw), "downloaded": False,
                "sha256": hashlib.sha256(raw).hexdigest()}

    request = urllib.request.Request(
        url, headers={"User-Agent": "dom6-assistant (manual fetch)"})
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
    if not raw.startswith(b"%PDF"):
        raise ManualError(
            f"{url} did not return a PDF. Has the address changed?")
    destination.write_bytes(raw)
    return {"path": str(destination), "bytes": len(raw), "downloaded": True,
            "sha256": hashlib.sha256(raw).hexdigest()}


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _pages_via_pypdf(pdf: Path) -> list[str] | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    reader = PdfReader(str(pdf))
    return [(page.extract_text() or "") for page in reader.pages]


def _pages_via_pdftotext(pdf: Path) -> list[str] | None:
    """Poppler, if it happens to be installed. Not required."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            capture_output=True, check=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.decode("utf-8", errors="replace").split("\f")


def extract_pages(pdf: Path) -> list[str]:
    """Per-page text, by whichever extractor is available.

    pypdf is preferred because it is pure Python and therefore works on the
    platform most Dominions players are on; poppler's pdftotext is a fallback
    for anyone who has it and would rather not add a dependency.
    """
    if not pdf.exists():
        raise ManualError(
            f"no manual at {pdf}. Fetch it first: "
            "python -m dom6_assistant.manual.index --fetch")
    for extractor in (_pages_via_pypdf, _pages_via_pdftotext):
        pages = extractor(pdf)
        if pages and any(p.strip() for p in pages):
            return pages
    raise ManualError(
        "could not read the PDF. Install the reader with:\n"
        "  uv sync --extra manual\n"
        "or install poppler-utils for pdftotext.")


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Section:
    title: str
    start_page: int
    end_page: int


# The two extractors lay the contents out differently: pdftotext keeps the
# aligned columns, pypdf emits "Title 6" with a single space. Both end the
# line with the page number, so anchor on that rather than on the leader.
_TOC_LINE = re.compile(r"^\s*(.+?)[\s.]+(\d{1,3})\s*$")
#: Lines on the contents pages that are cover text rather than entries.
_TOC_NOISE = re.compile(r"^(manual dominions|illwinter game design)", re.I)


def parse_contents(pages: list[str], scan: int = 6,
                   minimum: int = 20) -> list[Section]:
    """Read the table of contents into ordered, page-bounded sections.

    The contents are laid out in columns, so reading order is not page order;
    entries are sorted afterwards. Each section runs until the next one starts,
    which is what lets a search hit name the section it came from.
    """
    entries: list[tuple[str, int]] = []
    for page in pages[:scan]:
        for line in page.splitlines():
            match = _TOC_LINE.match(line.rstrip())
            if not match:
                continue
            title = match.group(1).strip(" .\t")
            if not title or title.isdigit() or _TOC_NOISE.match(title):
                continue
            entries.append((title, int(match.group(2))))

    if len(entries) < minimum:
        # A quietly empty index is worse than a failure: search would answer
        # "nothing found" for every query and look like the manual simply does
        # not cover the topic. This exact shape -- a parse that silently
        # produced one section -- is why the check is here.
        raise ManualError(
            f"only {len(entries)} contents entries found in the first {scan} "
            "pages; the manual's layout is not what this parser expects. The "
            "index would be unusable, so nothing was written.")

    ordered = sorted(set(entries), key=lambda e: (e[1], e[0]))
    sections: list[Section] = []
    for position, (title, start) in enumerate(ordered):
        following = ordered[position + 1][1] if position + 1 < len(ordered) else None
        end = (following - 1) if following and following > start else start
        sections.append(Section(title=title, start_page=start, end_page=end))
    return sections


def page_number_offset(pages: list[str]) -> int:
    """How far the printed page number sits from the index in the file.

    Measured rather than assumed: every page whose last line is a bare number
    votes, and the most common difference wins. A manual with front matter
    numbered differently would still resolve to the offset that fits most
    pages.
    """
    votes: dict[int, int] = {}
    for index, page in enumerate(pages):
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        if lines and re.fullmatch(r"\d{1,3}", lines[-1]):
            delta = index - int(lines[-1])
            votes[delta] = votes.get(delta, 0) + 1
    if not votes:
        return 0
    return max(votes.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(pdf: Path = DEFAULT_PDF,
          database_path: Path = DEFAULT_INDEX_DB) -> dict[str, Any]:
    """Extract, structure and index the manual. Safe to re-run."""
    pages = extract_pages(pdf)
    sections = parse_contents(pages)
    offset = page_number_offset(pages)

    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()
    conn = sqlite3.connect(database_path)
    try:
        conn.executescript(SCHEMA)
        section_ids: dict[int, int] = {}
        for number, section in enumerate(sections, start=1):
            conn.execute(
                "INSERT INTO sections(id,title,start_page,end_page) VALUES(?,?,?,?)",
                (number, section.title, section.start_page, section.end_page))
            for printed in range(section.start_page, section.end_page + 1):
                section_ids.setdefault(printed, number)

        indexed = 0
        for index, text in enumerate(pages):
            printed = index - offset
            if printed < 1 or not text.strip():
                continue
            conn.execute(
                "INSERT OR REPLACE INTO pages(page,section_id,text) VALUES(?,?,?)",
                (printed, section_ids.get(printed), text))
            indexed += 1
        conn.execute(
            "INSERT INTO pages_fts(rowid,text) SELECT page,text FROM pages")

        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        for key, value in (("source_url", MANUAL_URL), ("sha256", digest),
                           ("pages", str(indexed)),
                           ("sections", str(len(sections))),
                           ("page_offset", str(offset))):
            conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                         (key, value))
        conn.commit()
        return {"pages": indexed, "sections": len(sections),
                "page_offset": offset, "database": str(database_path),
                "sha256": digest}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _connect(database_path: Path) -> sqlite3.Connection:
    if not database_path.exists():
        raise ManualError(
            f"no manual index at {database_path}. Build it with:\n"
            "  uv run python -m dom6_assistant.manual.index --fetch --build")
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _excerpt(text: str, query: str, width: int = 320) -> str:
    """The part of the page that actually matched, not its first line."""
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    lowered = text.lower()
    position = -1
    for word in words:
        position = lowered.find(word)
        if position >= 0:
            break
    if position < 0:
        position = 0
    start = max(0, position - width // 3)
    snippet = " ".join(text[start:start + width].split())
    return ("..." if start else "") + snippet + ("..." if start + width < len(text) else "")


def search(query: str, limit: int = 5,
           database_path: Path = DEFAULT_INDEX_DB) -> dict[str, Any]:
    """Full-text search, answering with page numbers you can turn to."""
    if not query.strip():
        raise ValueError("empty query")
    limit = max(1, min(int(limit), 15))
    conn = _connect(database_path)
    try:
        try:
            rows = conn.execute(
                "SELECT p.page, p.text, s.title AS section "
                "FROM pages_fts f JOIN pages p ON p.page = f.rowid "
                "LEFT JOIN sections s ON s.id = p.section_id "
                "WHERE pages_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit)).fetchall()
        except sqlite3.OperationalError:
            # FTS5 treats punctuation as syntax; a plain phrase always works.
            quoted = '"' + query.replace('"', " ") + '"'
            rows = conn.execute(
                "SELECT p.page, p.text, s.title AS section "
                "FROM pages_fts f JOIN pages p ON p.page = f.rowid "
                "LEFT JOIN sections s ON s.id = p.section_id "
                "WHERE pages_fts MATCH ? ORDER BY rank LIMIT ?",
                (quoted, limit)).fetchall()
        return {
            "query": query,
            "results": [{"page": r["page"], "section": r["section"],
                         "excerpt": _excerpt(r["text"], query)} for r in rows],
            "source": "Dominions 6 manual, Illwinter Game Design",
        }
    finally:
        conn.close()


def read_page(page: int, max_chars: int = 6000,
              database_path: Path = DEFAULT_INDEX_DB) -> dict[str, Any]:
    """One page, by its printed number."""
    conn = _connect(database_path)
    try:
        row = conn.execute(
            "SELECT p.page, p.text, s.title AS section FROM pages p "
            "LEFT JOIN sections s ON s.id = p.section_id WHERE p.page=?",
            (int(page),)).fetchone()
        if row is None:
            span = conn.execute(
                "SELECT MIN(page) AS lo, MAX(page) AS hi FROM pages").fetchone()
            raise KeyError(
                f"no page {page}; the manual runs {span['lo']}-{span['hi']}")
        text = " ".join(row["text"].split())[:max(500, int(max_chars))]
        return {"page": row["page"], "section": row["section"], "text": text,
                "source": "Dominions 6 manual, Illwinter Game Design"}
    finally:
        conn.close()


def list_sections(database_path: Path = DEFAULT_INDEX_DB) -> dict[str, Any]:
    conn = _connect(database_path)
    try:
        rows = conn.execute(
            "SELECT title, start_page, end_page FROM sections ORDER BY start_page"
        ).fetchall()
        return {"sections": [dict(r) for r in rows], "count": len(rows)}
    finally:
        conn.close()


def status(database_path: Path = DEFAULT_INDEX_DB,
           pdf: Path = DEFAULT_PDF) -> dict[str, Any]:
    """Whether the manual is available, for the UI and for error messages."""
    if not database_path.exists():
        return {"built": False, "pdf_present": pdf.exists(),
                "hint": "uv run python -m dom6_assistant.manual.index --fetch --build"}
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        meta = {k: v for k, v in conn.execute("SELECT key,value FROM metadata")}
        return {"built": True, "pdf_present": pdf.exists(), **meta}
    finally:
        conn.close()


def _main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Fetch and index Illwinter's Dominions 6 manual locally. "
                    "The PDF is never redistributed; it is downloaded here.")
    parser.add_argument("--fetch", action="store_true", help="download the PDF")
    parser.add_argument("--build", action="store_true", help="index it")
    parser.add_argument("--force", action="store_true", help="re-download")
    parser.add_argument("--search", metavar="QUERY", help="try a search")
    args = parser.parse_args(argv)

    if not any((args.fetch, args.build, args.search)):
        parser.print_help()
        return 0
    if args.fetch:
        result = fetch(force=args.force)
        size = result["bytes"] / 1_048_576
        print(f"  {'downloaded' if result['downloaded'] else 'already present'}: "
              f"{result['path']} ({size:.1f} MiB)")
    if args.build:
        result = build()
        print(f"  {result['pages']} pages, {result['sections']} sections "
              f"-> {result['database']}")
    if args.search:
        for hit in search(args.search)["results"]:
            print(f"  p{hit['page']:>3}  {hit['section'] or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
