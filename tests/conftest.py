"""Make a fresh clone run green before any game data has been built.

Most of this suite is written against real artefacts: the reference database,
the snapshot corpus, a live save. None of those ship with the source, because
they are either the player's own games or Illwinter's data to distribute. A
newcomer running ``pytest`` therefore used to see ninety-odd failures with
FileNotFoundError, which says "this project is broken" when it means "you have
not run the build step yet".

The conversion is deliberately narrow. A missing-data error becomes a skip
**only when that data is genuinely absent**; on a machine where the reference
database exists, the same exception still fails the test, because there it
means something is actually wrong. Nothing is skipped by guessing from a
module's name or its imports -- the test has to really fail on a really
missing file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DB = ROOT / "knowledge" / "reference" / "reference.sqlite3"
SNAPSHOTS = ROOT / "knowledge" / "snapshots"

#: Substrings of exceptions raised when a build step has not been run. Kept
#: specific: "unable to open database file" is sqlite's message for a missing
#: parent directory, which is exactly the un-built case.
_MISSING_DATA = (
    "reference database not found",
    "game database not found",
    "not found at",
    "unable to open database file",
    "no such file or directory",
    "does not exist",
    # PlayerView's message when the .trn is there but its .2h is not. A
    # snapshot directory that is absent entirely reports it the same way, so a
    # fresh clone hits this before it hits a FileNotFoundError.
    "no .2h alongside the .trn",
)

_BUILD_HINT = (
    "build it with: python -m dom6_assistant.reference.build_db --refresh"
)


def _absent() -> list[str]:
    """Which build artefacts are missing right now."""
    missing = []
    if not REFERENCE_DB.exists():
        missing.append("the reference database")
    if not SNAPSHOTS.is_dir() or not any(SNAPSHOTS.rglob("*.trn")):
        missing.append("the snapshot corpus")
    if not (ROOT / "knowledge" / "game.sqlite3").exists():
        missing.append("an ingested game")
    return missing


def _looks_like_missing_data(error: BaseException) -> bool:
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return True
    text = f"{type(error).__name__}: {error}".lower()
    if not any(marker in text for marker in _MISSING_DATA):
        return False
    # sqlite says "unable to open database file" and names no path, so the
    # check below cannot apply to it. _absent() has already established that
    # the data really is missing, which is guard enough.
    if "unable to open database file" in text:
        return True
    # Only ours. A missing file somewhere else in the system is a real failure.
    return bool(re.search(r"knowledge|\.trn\b|\.2h\b|ftherlnd|reference", text))


def _maybe_skip(outcome) -> None:
    error = outcome.excinfo[1] if outcome.excinfo else None
    if error is None or isinstance(error, pytest.skip.Exception):
        return
    absent = _absent()
    if not absent or not _looks_like_missing_data(error):
        return
    outcome.force_exception(pytest.skip.Exception(
        f"needs {' and '.join(absent)}, which is not built here. {_BUILD_HINT}",
        _use_item_location=True,
    ))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    outcome = yield
    _maybe_skip(outcome)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    outcome = yield
    _maybe_skip(outcome)


def pytest_report_header(config) -> str | None:
    absent = _absent()
    if not absent:
        return None
    return (f"game data not built ({', '.join(absent)}); tests needing it will "
            f"skip. {_BUILD_HINT}")


def require_corpus(seen: int, needed: int, what: str) -> None:
    """Assert a corpus-backed test really saw its samples -- or skip it.

    These tests walk the snapshot corpus, skip any save that is not present,
    and then assert a minimum number of samples, because a test that silently
    examines nothing proves nothing. On a fresh clone that minimum fails with
    ``assert 0 >= 2``, which reads as "the parser is broken" when it means "you
    have no saves here".

    The distinction is the count: zero means absent, and anything between one
    and *needed* means the corpus is present but incomplete, which is a real
    failure and still reported as one.
    """
    if seen == 0:
        pytest.skip(f"no {what} in knowledge/snapshots/, so there is nothing "
                    f"to check here. {_BUILD_HINT}")
    assert seen >= needed, (
        f"found {seen} of the {needed} {what} needed: the corpus is present "
        f"but incomplete, so this check would not prove what it claims")
