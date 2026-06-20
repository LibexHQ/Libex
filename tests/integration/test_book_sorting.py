"""
Integration test for /db/book sorting.

Verifies the sort helper applies correct ORDER BY against real Postgres:
sortable fields work asc/desc, no-sort preserves current behaviour, and an
unknown field is ignored rather than crashing.
"""

# Standard library
from datetime import datetime, timezone

# Third party
import pytest
from sqlalchemy import insert

# Local
from app.db.models import Book
from app.services.db.reader import search_books_from_db

# (asin, title, rating, length_minutes, release_year)
BOOKS = [
    ("B00SORTBK01", "Charlie", 3.5, 600, 2020),
    ("B00SORTBK02", "Alpha", 4.8, 200, 2024),
    ("B00SORTBK03", "Bravo", 2.1, 900, 2018),
    ("B00SORTBK04", "Delta", 4.8, 450, 2022),
]


async def _seed(db_session):
    now = datetime.now(timezone.utc)
    for asin, title, rating, length, year in BOOKS:
        await db_session.execute(
            insert(Book).values(
                asin=asin,
                title=title,
                region="us",
                rating=rating,
                length_minutes=length,
                release_date=datetime(year, 1, 1, tzinfo=timezone.utc),
                created_at=now,
                updated_at=now,
            )
        )
    await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sort_by_release_date_desc(db_session):
    """releaseDate desc returns newest first."""
    await _seed(db_session)
    books = await search_books_from_db(
        db_session, region="us", sort="releaseDate", order="desc"
    )
    years = [b["releaseDate"][:4] for b in books]
    assert years == ["2024", "2022", "2020", "2018"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sort_by_release_date_asc(db_session):
    """releaseDate asc returns oldest first."""
    await _seed(db_session)
    books = await search_books_from_db(
        db_session, region="us", sort="releaseDate", order="asc"
    )
    years = [b["releaseDate"][:4] for b in books]
    assert years == ["2018", "2020", "2022", "2024"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sort_by_title_asc(db_session):
    """title asc returns alphabetical order."""
    await _seed(db_session)
    books = await search_books_from_db(
        db_session, region="us", sort="title", order="asc"
    )
    titles = [b["title"] for b in books]
    assert titles == ["Alpha", "Bravo", "Charlie", "Delta"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sort_by_rating_desc(db_session):
    """rating desc returns highest rated first."""
    await _seed(db_session)
    books = await search_books_from_db(
        db_session, region="us", sort="rating", order="desc"
    )
    ratings = [b["rating"] for b in books]
    assert ratings[0] == 4.8
    assert ratings[-1] == 2.1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sort_by_length_asc(db_session):
    """lengthMinutes asc returns shortest first."""
    await _seed(db_session)
    books = await search_books_from_db(
        db_session, region="us", sort="lengthMinutes", order="asc"
    )
    lengths = [b["lengthMinutes"] for b in books]
    assert lengths == [200, 450, 600, 900]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_sort_returns_results(db_session):
    """No sort param still returns all matching books (current behaviour)."""
    await _seed(db_session)
    books = await search_books_from_db(db_session, region="us")
    assert len(books) == 4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_sort_field_ignored(db_session):
    """An unknown sort field is ignored, not an error."""
    await _seed(db_session)
    books = await search_books_from_db(
        db_session, region="us", sort="description", order="asc"
    )
    assert len(books) == 4