"""A polite, resumable mirror of Illwiki's Dominions 6 namespace.

Illwiki is a DokuWiki installation.  Its XHTML export is ideal for an offline
text corpus: it contains the article, metadata and links without downloading
images, scripts, stylesheets, history pages, or user pages.  Discovery happens
once through DokuWiki's own namespace index; the crawler never follows article
links recursively.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

ORIGIN = "https://illwiki.com"
WIKI_ROOT = f"{ORIGIN}/dom5"
ROBOTS_URL = f"{ORIGIN}/robots.txt"
INDEX_URL = f"{WIKI_ROOT}/dom6/start?do=index"
EXPORT_ROOT = f"{WIKI_ROOT}/_export/xhtml"
DEFAULT_MIRROR_DIR = Path("knowledge/illwiki")
DEFAULT_USER_AGENT = "dom6-assistant-illwiki-mirror/0.1 (personal offline reference index)"
LICENSE = "CC BY-NC-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_PAGE_ID = re.compile(r"^dom6(?::[A-Za-z0-9._-]+)+$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page_ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        page_id = values.get("title") or ""
        href = values.get("href") or ""
        if (
            _PAGE_ID.fullmatch(page_id)
            and "/dom5/dom6/" in href
            and "idx=" not in href
            and "do=" not in href
        ):
            self.page_ids.add(page_id)


def discover_page_ids(index_html: str) -> list[str]:
    """Extract real Dom6 article ids from DokuWiki's namespace index."""
    parser = _IndexParser()
    parser.feed(index_html)
    parser.close()
    return sorted(parser.page_ids)


def export_url(page_id: str) -> str:
    if not _PAGE_ID.fullmatch(page_id):
        raise ValueError(f"unsafe or invalid Illwiki page id {page_id!r}")
    return f"{EXPORT_ROOT}/{'/'.join(page_id.split(':'))}"


def canonical_url(page_id: str) -> str:
    if not _PAGE_ID.fullmatch(page_id):
        raise ValueError(f"unsafe or invalid Illwiki page id {page_id!r}")
    return f"{WIKI_ROOT}/{'/'.join(page_id.split(':'))}"


def raw_path(root: Path, page_id: str) -> Path:
    if not _PAGE_ID.fullmatch(page_id):
        raise ValueError(f"unsafe or invalid Illwiki page id {page_id!r}")
    parts = page_id.split(":")
    return root / "raw" / Path(*parts[:-1]) / f"{parts[-1]}.html"


class PoliteFetcher:
    """Serial HTTP client that enforces robots.txt and Crawl-delay."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        requested_delay: float = 5.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._sleep = sleep
        self._last_request: float | None = None
        self.delay = max(float(requested_delay), 0.0)
        robots_text = self._request(ROBOTS_URL).decode("utf-8", errors="replace")
        self.robots = urllib.robotparser.RobotFileParser(ROBOTS_URL)
        self.robots.parse(robots_text.splitlines())
        robots_delay = self.robots.crawl_delay(user_agent)
        if robots_delay is None:
            robots_delay = self.robots.crawl_delay("*")
        self.delay = max(self.delay, float(robots_delay or 0))

    def _request(self, url: str) -> bytes:
        if self._last_request is not None:
            remaining = self.delay - (time.monotonic() - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "text/html,*/*;q=0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError(f"response exceeds {MAX_RESPONSE_BYTES:,} bytes: {url}")
                return payload
        finally:
            self._last_request = time.monotonic()

    def fetch(self, url: str) -> bytes:
        if not self.robots.can_fetch(self.user_agent, url):
            raise PermissionError(f"robots.txt disallows {url}")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self._request(url)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                retry_after = exc.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else self.delay
                self._sleep(max(self.delay, wait))
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == 2:
                    raise
                self._sleep(self.delay)
        assert last_error is not None
        raise last_error


@dataclass(frozen=True)
class MirrorResult:
    discovered: int
    selected: int
    downloaded: int
    skipped: int
    failed: int
    manifest_path: Path


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"format": 1, "pages": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"cannot read mirror manifest {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("pages"), dict):
        raise ValueError(f"mirror manifest {path} has an unsupported shape")
    return value


def _write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def mirror_site(
    output_dir: Path = DEFAULT_MIRROR_DIR,
    *,
    page_ids: Iterable[str] | None = None,
    limit: int | None = None,
    refresh: bool = False,
    requested_delay: float = 5.0,
    timeout: float = 30.0,
    user_agent: str = DEFAULT_USER_AGENT,
    progress: Callable[[str], None] | None = None,
    fetcher: PoliteFetcher | None = None,
) -> MirrorResult:
    """Mirror selected pages, persisting progress after every response.

    Existing successful pages are skipped unless ``refresh`` is true.  This
    makes an interrupted first crawl resumable; a later refresh deliberately
    rechecks every selected page for wiki changes.
    """
    explicit_ids: list[str] | None = None
    if page_ids is not None:
        explicit_ids = sorted(set(page_ids))
        for page_id in explicit_ids:
            export_url(page_id)
    client = fetcher or PoliteFetcher(
        user_agent=user_agent, requested_delay=requested_delay, timeout=timeout
    )
    if explicit_ids is None:
        index_html = client.fetch(INDEX_URL).decode("utf-8", errors="replace")
        discovered_ids = discover_page_ids(index_html)
        selected_ids = discovered_ids[:limit] if limit is not None else discovered_ids
    else:
        selected_ids = explicit_ids
        if limit is not None:
            selected_ids = selected_ids[:limit]
        discovered_ids = selected_ids

    manifest_path = output_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)
    manifest.update(
        {
            "format": 1,
            "source": WIKI_ROOT,
            "namespace": "dom6",
            "license": LICENSE,
            "license_url": LICENSE_URL,
            "crawl_delay_seconds": client.delay,
            "discovered_pages": len(discovered_ids),
            "updated_at": utc_now(),
        }
    )
    pages: dict[str, dict] = manifest["pages"]
    if explicit_ids is None and limit is None:
        discovered_set = set(discovered_ids)
        for page_id, record in pages.items():
            if page_id.startswith("dom6:") and page_id not in discovered_set:
                record["status"] = "absent_from_latest_sitemap"
                record["last_checked_at"] = utc_now()
    downloaded = skipped = failed = 0
    for index, page_id in enumerate(selected_ids, 1):
        destination = raw_path(output_dir, page_id)
        old = pages.get(page_id, {})
        if not refresh and old.get("status") == "ok" and destination.exists():
            skipped += 1
            if progress:
                progress(f"[{index}/{len(selected_ids)}] skip {page_id}")
            continue
        if progress:
            progress(f"[{index}/{len(selected_ids)}] fetch {page_id}")
        try:
            payload = client.fetch(export_url(page_id))
            if b'div class="dokuwiki export"' not in payload:
                raise ValueError("response is not a DokuWiki XHTML article export")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".html.tmp")
            temporary.write_bytes(payload)
            temporary.replace(destination)
            pages[page_id] = {
                "status": "ok",
                "url": canonical_url(page_id),
                "export_url": export_url(page_id),
                "path": str(destination.relative_to(output_dir)),
                "fetched_at": utc_now(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
            downloaded += 1
        except (OSError, ValueError, PermissionError, urllib.error.URLError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if old.get("status") == "ok" and destination.exists():
                # A transient refresh failure must not discard the last good
                # offline copy.  It is still usable and the failed refresh is
                # explicit in the manifest for the next refresh to retry.
                pages[page_id] = {
                    **old,
                    "last_refresh_error": error,
                    "last_refresh_attempted_at": utc_now(),
                }
            else:
                pages[page_id] = {
                    **old,
                    "status": "error",
                    "url": canonical_url(page_id),
                    "export_url": export_url(page_id),
                    "error": error,
                    "attempted_at": utc_now(),
                }
            failed += 1
            if progress:
                progress(f"  error: {exc}")
        manifest["updated_at"] = utc_now()
        _write_manifest(manifest_path, manifest)
    _write_manifest(manifest_path, manifest)
    return MirrorResult(
        discovered=len(discovered_ids),
        selected=len(selected_ids),
        downloaded=downloaded,
        skipped=skipped,
        failed=failed,
        manifest_path=manifest_path,
    )
