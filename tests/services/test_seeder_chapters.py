"""
Service tests for the seeder's chapter pass (_gather_chapters) and which books
it will spend an Audible call on.

Two ways a book is admitted: its chapters have never been checked, or it was
checked before its own release date and that date has since passed. The second
is the point of the rule. Audible answers a chapter request for audio that does
not exist yet with a 404, and fetch_and_store_chapters marks that answer like
any other, so a title asked about while it was still upcoming would be retired
before it ever had chapters to find. Comparing the stamp against the release
date brings exactly those back, once, on their own.

The rule lives in this function's SQL WHERE clause, which is what these tests
can and cannot reach. A mocked session answers with whatever rows the test
hands it no matter what the clause says, so nothing here can show which books
the query would ADMIT — only what the statement asks for. The behaviour, and
that this clause agrees case for case with the backfill's Python form of the
same rule, is pinned against real Postgres in
tests/integration/test_chapters_release_gate.py. What is left for this file is
the part a real database cannot see: which clock the comparison is made
against.
"""

# Standard library
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Services
from app.services import seeder

NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)


class _FakeSessionCM:
    """Minimal async context manager standing in for SessionFactory()'s
    `async with` usage. Mirrors tests/services/test_seeder_shed_awareness.py's
    own helper of the same name."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _statement_for(asins, *, now=NOW):
    """Runs _gather_chapters far enough to capture the select it issues,
    against a frozen clock and a session that answers with no rows — so the
    fetch loop below never runs and no Audible call is even contemplated."""
    result = MagicMock()
    result.fetchall.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    with patch.object(seeder, "SessionFactory", lambda: _FakeSessionCM(session)), \
         patch.object(seeder, "_now", return_value=now):
        await seeder._gather_chapters(asins, "us", 0)

    return session.execute.call_args_list[0].args[0]


# ============================================================
# _gather_chapters — THE ELIGIBILITY QUERY
# ============================================================

@pytest.mark.asyncio
async def test_gather_chapters_asks_for_both_ways_a_book_becomes_eligible():
    """Both arms are in the statement: never checked at all, or checked
    before a release date that has since passed. A shape check, not a
    behaviour one — see the module docstring — kept because it runs in the
    suite that needs no Docker, and because losing either arm silently is
    the whole failure this slice is about."""
    stmt = await _statement_for(["B00SEEDER01"])
    sql = str(stmt.compile())

    assert "books.chapters_checked_at IS NULL" in sql
    # Asserted as one joined string rather than three separate substrings,
    # because the shape of the join is the part that can go wrong quietly:
    # SQL binds AND tighter than OR, so this renders without parentheses and
    # reads correctly. Written as a flat three-argument or_(), it would render
    # the same three comparisons with the same operators and mean something
    # else entirely -- every released book in the batch admitted forever,
    # re-fetched every cycle, with no substring check on any one term able to
    # tell the difference.
    assert (
        "books.chapters_checked_at IS NULL "
        "OR books.chapters_checked_at < books.release_date "
        "AND books.release_date <= " in sql
    )


@pytest.mark.asyncio
async def test_gather_chapters_compares_against_its_own_clock():
    """The one thing a database test cannot see: the instant the release date
    is compared against is the seeder's own _now(), read for this batch, not
    a value captured when the module was imported. A stale bound constant
    would make the query silently stop admitting anything as the process
    aged, and every row it returned would still look correct."""
    instant = datetime(2026, 8, 30, 9, 30, 0, tzinfo=timezone.utc)
    stmt = await _statement_for(["B00SEEDER01"], now=instant)

    bound = [v for v in stmt.compile().params.values() if isinstance(v, datetime)]
    assert bound == [instant]


@pytest.mark.asyncio
async def test_gather_chapters_still_scopes_the_query_to_the_batch():
    """Eligibility narrows the batch it was handed; it never widens the pass
    into a corpus-wide sweep. The seeder is paced for the live IP, one
    Audible call per book, and this function has no limit of its own."""
    stmt = await _statement_for(["B00SEEDER01", "B00SEEDER02"])
    sql = str(stmt.compile())

    assert "books.asin IN " in sql
    assert stmt.compile().params["asin_1"] == ["B00SEEDER01", "B00SEEDER02"]


@pytest.mark.asyncio
async def test_gather_chapters_asks_nothing_at_all_for_an_empty_batch():
    """No batch, no query — the early return keeps a phase that discovered
    nothing from opening a session at all."""
    session = AsyncMock()
    with patch.object(seeder, "SessionFactory", lambda: _FakeSessionCM(session)):
        await seeder._gather_chapters([], "us", 0)
    session.execute.assert_not_awaited()
