"""
Integration tests for chapter re-admission, against real Postgres.

chapters_checked_at is written unconditionally and nothing ever clears it, so
for a book that was already out when it was asked about, the stamp is the end
of the matter. For one asked ahead of its release date it is not: Audible
answers a chapter request for audio that does not exist yet with a 404, and
that 404 said nothing about the finished title, so a book whose stamp predates
its release_date becomes eligible again once that date has passed.

The rule exists twice — as the seeder's SQL WHERE clause and as the backfill's
Python filter in _select_work — because the two walk the corpus in different
ways for different reasons. Two expressions of one rule in two languages is a
drift surface, and a null-propagating comparison in SQL against an explicit
None check in Python is exactly the kind of pair that can quietly stop
agreeing. So the case table below is run through both and asserted to agree,
case for case, rather than each being tested against its own idea of the rule.

Postgres is not incidental here. Every comparison in the rule involves a column
that may be null, and SQL three-valued logic is the thing under test as much as
the operators are: `checked < release_date` where either side is null is null,
not false, and it is that propagation the seeder relies on to leave dateless
books settled.
"""

# Standard library
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Third party
import pytest
from sqlalchemy import insert, select

# Local
from app.core.exceptions import NotFoundException
from app.db.models import Book
from app.services import seeder
from app.services.audible.books import _mark_chapters_checked
from scripts.backfill_chapters import _mark_checked, _select_work

# A fixed instant both engines are pointed at, so "now" means the same thing
# on either side of the comparison and the boundary cases are exact.
NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
OUT = NOW - timedelta(days=30)          # released a month ago
AHEAD = NOW + timedelta(days=30)        # releases in a month
LONG_AGO = NOW - timedelta(days=400)
RECENT = NOW - timedelta(days=1)

# Both writers take (session, asin) and stamp the same column; the id is what
# names the failing path in the report.
WRITERS = [_mark_chapters_checked, _mark_checked]
WRITER_IDS = ["service", "backfill"]


class _Reusable:
    """Hands the test's own session to every `async with SessionFactory()` the
    seeder opens, so its real query runs against the container and the
    fixture's truncation still owns the cleanup."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _seed(session, asin, checked, release_date, *, region="us"):
    await session.execute(
        insert(Book).values(
            asin=asin,
            title=f"Book {asin}",
            region=region,
            release_date=release_date,
            chapters_checked_at=checked,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await session.commit()


async def _stamp(session, asin):
    result = await session.execute(
        select(Book.chapters_checked_at).where(Book.asin == asin)
    )
    return result.scalar_one()


async def _seeder_admits(session, asins, now):
    """The ASINs the seeder's own WHERE clause admits, as the ASINs it goes on
    to spend a chapter call on. fetch_and_store_chapters is captured rather
    than run, so what is measured is the query's answer and nothing else."""
    fetched = []

    async def _capture(asin, region, _session):
        fetched.append(asin)
        return "stored"

    with patch.object(seeder, "SessionFactory", lambda: _Reusable(session)), \
         patch.object(seeder, "fetch_and_store_chapters", _capture), \
         patch.object(seeder, "_now", return_value=now):
        await seeder._gather_chapters(asins, "us", 0)

    return fetched


# ============================================================
# The stamp is unconditional
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mark", WRITERS, ids=WRITER_IDS)
async def test_a_book_that_has_not_released_is_still_stamped(db_session, mark):
    """Nothing about the release date is tested at write time. That is what
    makes re-admission possible at all: the stamp is the record of when the
    question was put, so witholding it for an upcoming title would leave
    nothing to compare against later and the book would be re-fetched on
    every single pass instead of once after release."""
    await _seed(db_session, "B00UNRELEAS", checked=None, release_date=AHEAD)
    await mark(db_session, "B00UNRELEAS")
    assert await _stamp(db_session, "B00UNRELEAS") is not None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mark", WRITERS, ids=WRITER_IDS)
async def test_a_released_book_is_stamped(db_session, mark):
    """The ordinary case, unchanged, and settled by this write for good."""
    await _seed(db_session, "B00RELEASED", checked=None, release_date=OUT)
    await mark(db_session, "B00RELEASED")
    assert await _stamp(db_session, "B00RELEASED") is not None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mark", WRITERS, ids=WRITER_IDS)
async def test_a_book_with_no_release_date_is_stamped(db_session, mark):
    """A missing release date never triggers special treatment in either
    direction — not at the write, and not at selection, where it leaves the
    book settled by this first check."""
    await _seed(db_session, "B00NULLDATE", checked=None, release_date=None)
    await mark(db_session, "B00NULLDATE")
    assert await _stamp(db_session, "B00NULLDATE") is not None


# ============================================================
# The eligibility rule, in both languages, over the same cases
# ============================================================
# (case name, chapters_checked_at, release_date, eligible)

CASES = [
    ("never checked and no release date", None, None, True),
    ("never checked and already out", None, OUT, True),
    ("never checked and not out yet", None, AHEAD, True),
    ("checked before a release that has now passed", LONG_AGO, OUT, True),
    ("checked before a release still ahead", RECENT, AHEAD, False),
    ("checked after it was already out", RECENT, LONG_AGO, False),
    ("checked at exactly the release instant", OUT, OUT, False),
    ("checked a microsecond before release", OUT - timedelta(microseconds=1), OUT, True),
    ("releasing at exactly now", LONG_AGO, NOW, True),
    ("releasing a second from now", LONG_AGO, NOW + timedelta(seconds=1), False),
    ("checked with no release date at all", LONG_AGO, None, False),
]
CASE_IDS = [case[0] for case in CASES]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("name,checked,release_date,eligible", CASES, ids=CASE_IDS)
async def test_both_forms_of_the_rule_agree_on_every_case(
    db_session, name, checked, release_date, eligible
):
    """The standing agreement check between the seeder's SQL and the
    backfill's Python, replacing a one-off probe that proved the two matched
    on the day they were written.

    Each case is asserted twice against the same expectation rather than the
    two forms being compared to each other, so a case where both drift the
    same way still fails, and the report names which side broke.

    Four of these carry the whole rule. "checked before a release that has
    now passed" is the fix itself. "checked after it was already out" is what
    stops re-admission becoming a permanent re-fetch loop. "checked at
    exactly the release instant" pins checked < release_date as strict, and
    "releasing at exactly now" pins release_date <= now as inclusive.

    "checked with no release date at all" is the one to read twice. It is
    false on both sides for different-looking reasons — SQL three-valued
    logic on one, an explicit `release_date is not None` on the other — and
    that is precisely why it is here rather than assumed."""
    asin = "B00CASE0001"
    await _seed(db_session, asin, checked=checked, release_date=release_date)

    by_sql = await _seeder_admits(db_session, [asin], NOW)
    by_python = _select_work([(asin, "us", checked, release_date)], NOW)

    assert (by_sql == [asin]) is eligible
    assert (by_python == [(asin, "us")]) is eligible


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_seeder_sorts_a_whole_mixed_batch_at_once(db_session):
    """The cases above go through the query one book at a time, which cannot
    catch a clause that is right per row and wrong across a set -- an OR that
    admits the whole batch as soon as any member qualifies, say, which is the
    shape this WHERE clause is one misplaced parenthesis away from. This
    hands the query every case together and checks that exactly the eligible
    ones come back."""
    asins = [f"B00BATCH{index:04d}" for index in range(len(CASES))]
    for asin, (_name, checked, release_date, _eligible) in zip(asins, CASES):
        await _seed(db_session, asin, checked=checked, release_date=release_date)

    expected = [asin for asin, case in zip(asins, CASES) if case[3]]
    admitted = await _seeder_admits(db_session, asins, NOW)

    assert sorted(admitted) == sorted(expected)


# ============================================================
# The whole story, end to end
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_early_404_does_not_retire_a_title_before_release(db_session):
    """The slice in one test, with nothing about it mocked but Audible.

    A title still a month out is asked about, Audible 404s because the audio
    does not exist yet, and the real fetch_and_store_chapters stamps it. The
    next cycle, still before release, correctly leaves it alone -- one wasted
    request, not a permanent loop. Then the release date passes and the same
    query hands it back, which is the whole point: before this slice that
    stamp was terminal and the title would never have picked up chapters at
    all.

    Not a theoretical recovery either: a substantial minority of unreleased
    books already hold chapter data before their release date. The share was
    measured on the live corpus while this was being written, and the number
    is deliberately not repeated here -- an undated point-in-time figure in a
    docstring ages into a falsehood that reads as fact."""
    asin = "B00PREORDER"
    await _seed(db_session, asin, checked=None, release_date=AHEAD)

    with patch("app.services.audible.books.audible_get", side_effect=NotFoundException()), \
         patch.object(seeder, "SessionFactory", lambda: _Reusable(db_session)), \
         patch.object(seeder, "_now", return_value=NOW):
        await seeder._gather_chapters([asin], "us", 0)

    stamped_at = await _stamp(db_session, asin)
    assert stamped_at is not None
    assert stamped_at < AHEAD

    still_upcoming = await _seeder_admits(db_session, [asin], NOW + timedelta(days=1))
    assert still_upcoming == []

    after_release = await _seeder_admits(db_session, [asin], AHEAD + timedelta(days=1))
    assert after_release == [asin]
