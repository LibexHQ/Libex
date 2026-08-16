"""
Integration tests for the length-based merge the writer uses on its free-text
columns, against real Postgres.

The rule these defend is that Libex never accepts less data than it holds: a
later, richer Audible response replaces a thinner stored value, and a thinner
response never replaces a richer stored one. The merge is expressed in SQL, and
SQL's three-valued logic is where that rule is easiest to lose -- length(NULL)
is NULL and a comparison against NULL is NULL rather than false, so a bare
length-vs-length test resolves to its ELSE branch whenever the stored value is
NULL and pins that NULL permanently. A book first written without a summary
could then never be given one, however long a later response's is.

That is invisible to a mocked session, which can only show that an UPDATE was
issued, not what Postgres resolved it to. So the whole truth table runs here
against a real database through the public writer functions, in the same
sequence production writes them: one write to establish the stored value, a
second to try to change it.
"""

# Third party
import pytest
from sqlalchemy import text

# Local
from app.services.db.writer import upsert_author_profile, upsert_book, upsert_series

LONG = "The full stored description, which is by a wide margin the longest value."
SHORT = "short"

# (stored value, incoming value, what must be held afterwards)
TRUTH_TABLE = [
    pytest.param(None, LONG, LONG, id="stored-null-incoming-long-fills"),
    pytest.param(LONG, None, LONG, id="stored-long-incoming-null-keeps"),
    pytest.param(None, None, None, id="both-null-stays-null"),
    pytest.param(None, "", None, id="stored-null-incoming-empty-stays-null"),
    pytest.param(None, "   ", None, id="stored-null-incoming-whitespace-stays-null"),
    pytest.param("", LONG, LONG, id="stored-empty-incoming-long-fills"),
    pytest.param(LONG, SHORT, LONG, id="stored-long-incoming-short-keeps"),
    pytest.param(SHORT, LONG, LONG, id="stored-short-incoming-long-takes"),
    pytest.param(LONG, "", LONG, id="stored-long-incoming-empty-keeps"),
    pytest.param("aaaa", "bbbb", "aaaa", id="equal-lengths-incumbent-keeps"),
    pytest.param(None, "  padded  ", "  padded  ", id="incoming-text-stored-verbatim"),
]

BOOK_ASIN = "B0LONGERWIN"
SERIES_ASIN = "B0SERIESLW"
AUTHOR_ASIN = "B0AUTHORLW"


async def _stored(session, column, table, asin):
    result = await session.execute(
        text(f"SELECT {column} FROM {table} WHERE asin = :asin"), {"asin": asin}
    )
    return result.scalar()


async def _merge_book(session, **fields):
    await upsert_book(session, {
        "asin": BOOK_ASIN, "title": "Longer Wins", "region": "us", **fields
    })
    await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("stored, incoming, expected", TRUTH_TABLE)
async def test_book_summary_merge(db_session, stored, incoming, expected):
    """books.summary holds the richer of the two values, in both directions."""
    await _merge_book(db_session, summary=stored)
    await _merge_book(db_session, summary=incoming)

    assert await _stored(db_session, "summary", "books", BOOK_ASIN) == expected


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("stored, incoming, expected", TRUTH_TABLE)
async def test_book_description_merge(db_session, stored, incoming, expected):
    """books.description is merged by the same helper and must behave alike --
    the two book columns are separate call sites, and a guard applied to one
    and missed on the other leaves half the rule unenforced."""
    await _merge_book(db_session, description=stored)
    await _merge_book(db_session, description=incoming)

    assert await _stored(db_session, "description", "books", BOOK_ASIN) == expected


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("stored, incoming, expected", TRUTH_TABLE)
async def test_series_description_merge(db_session, stored, incoming, expected):
    """series.description, written through upsert_series."""
    for description in (stored, incoming):
        await upsert_series(db_session, {
            "asin": SERIES_ASIN, "name": "A Series", "region": "us",
            "description": description,
        })
        await db_session.commit()

    assert await _stored(db_session, "description", "series", SERIES_ASIN) == expected


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("stored, incoming, expected", TRUTH_TABLE)
async def test_author_description_merge(db_session, stored, incoming, expected):
    """authors.description, written through upsert_author_profile -- the path
    the contributors endpoint takes, and the only one that refreshes an
    author's description once the row exists."""
    for description in (stored, incoming):
        await upsert_author_profile(db_session, {
            "asin": AUTHOR_ASIN, "name": "An Author", "region": "us",
            "description": description,
        })

    assert await _stored(db_session, "description", "authors", AUTHOR_ASIN) == expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_book_summary_fills_after_many_null_writes(db_session):
    """The stored NULL is not merely fillable on the second write -- it stays
    fillable. A book can sit through many refreshes with no summary before one
    finally arrives, and every one of those writes has to leave it fillable."""
    for _ in range(5):
        await _merge_book(db_session, summary=None)

    await _merge_book(db_session, summary=LONG)

    assert await _stored(db_session, "summary", "books", BOOK_ASIN) == LONG


@pytest.mark.integration
@pytest.mark.asyncio
async def test_book_summary_never_degrades_across_a_run_of_writes(db_session):
    """Once held, the richest value survives every later thinner one, in any
    order -- the half of the rule that matters most, since making NULLs
    fillable while letting short values overwrite long ones would trade a
    silent loss for a louder one."""
    await _merge_book(db_session, summary=LONG)

    for thinner in (None, "", "   ", SHORT, "a bit longer than short"):
        await _merge_book(db_session, summary=thinner)
        assert await _stored(db_session, "summary", "books", BOOK_ASIN) == LONG
