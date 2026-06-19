"""
Integration test for series book ordering.

Verifies the fix for the string-sorted position bug: positions stored as
strings ("1", "2", "10") must sort numerically (1, 2, 10), not lexically
(1, 10, 2). Non-numeric positions ("1-3") and nulls sort last.

Runs against a real PostgreSQL container because the fix relies on Postgres
regex matching and CAST in ORDER BY, which mocked sessions cannot exercise.
"""

# Standard library
from datetime import datetime, timezone

# Third party
import pytest
from sqlalchemy import insert

# Local
from app.db.models import Book, Series, book_series
from app.services.db.reader import get_series_books_from_db

SERIES_ASIN = "B00TESTSER1"

# (asin, position) pairs, inserted in deliberately scrambled order.
# Expected numeric order: 1, 1.5, 2, 3, 10 — then non-numeric/null last.
BOOK_POSITIONS = [
    ("B00TESTBK10", "10"),
    ("B00TESTBK02", "2"),
    ("B00TESTBK01", "1"),
    ("B00TESTBK03", "3"),
    ("B00TESTBK15", "1.5"),
    ("B00TESTBK13", "1-3"),
    ("B00TESTBKNL", None),
]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_series_books_sorted_numerically(db_session):
    """Series books sort by numeric position, not string order."""
    now = datetime.now(timezone.utc)

    # Insert the series.
    await db_session.execute(
        insert(Series).values(
            asin=SERIES_ASIN,
            title="Test Series",
            region="us",
            created_at=now,
            updated_at=now,
        )
    )

    # Insert the books.
    for asin, _ in BOOK_POSITIONS:
        await db_session.execute(
            insert(Book).values(
                asin=asin,
                title=f"Book {asin}",
                region="us",
                created_at=now,
                updated_at=now,
            )
        )

    # Link them to the series with scrambled positions.
    for asin, position in BOOK_POSITIONS:
        await db_session.execute(
            insert(book_series).values(
                book_asin=asin,
                series_asin=SERIES_ASIN,
                position=position,
            )
        )

    await db_session.commit()

    books = await get_series_books_from_db(db_session, SERIES_ASIN)
    returned_asins = [b["asin"] for b in books]

    # The five numeric positions must come first, in numeric order.
    numeric_order = [
        "B00TESTBK01",  # 1
        "B00TESTBK15",  # 1.5
        "B00TESTBK02",  # 2
        "B00TESTBK03",  # 3
        "B00TESTBK10",  # 10
    ]
    assert returned_asins[:5] == numeric_order

    # The non-numeric ("1-3") and null positions come last (order among
    # themselves is not asserted, only that they trail the numeric ones).
    assert set(returned_asins[5:]) == {"B00TESTBK13", "B00TESTBKNL"}

    # The specific regression guard: "10" must not sort before "2".
    assert returned_asins.index("B00TESTBK10") > returned_asins.index("B00TESTBK02")