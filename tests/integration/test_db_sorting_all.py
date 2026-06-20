"""
Integration tests for sort across the DB list endpoints.

Covers sort on /db/vvab, /db/plans, /db/narrator/books, /db/author books,
/db/narrator, and /db/series books (where sort overrides default position
order). Runs against a real Postgres container via the integration harness.
"""

# Standard library
from datetime import datetime, timezone

# Third party
import pytest
from sqlalchemy import insert, select

# Local
from app.db.models import (
    Author,
    Book,
    Narrator,
    Series,
    author_book,
    book_narrator,
    book_series,
)
from app.services.db.reader import (
    get_author_books_from_db,
    get_books_by_plan_from_db,
    get_narrator_books_from_db,
    get_series_books_from_db,
    get_vvab_books_from_db,
    search_narrators_from_db,
)

NOW = datetime.now(timezone.utc)


async def _book(session, asin, title, year, *, vvab=False, plans=None):
    await session.execute(
        insert(Book).values(
            asin=asin,
            title=title,
            region="us",
            release_date=datetime(year, 1, 1, tzinfo=timezone.utc),
            is_vvab=vvab,
            plans=plans,
            created_at=NOW,
            updated_at=NOW,
        )
    )


# ============================================================
# /db/vvab
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_vvab_sort_by_release_date_desc(db_session):
    await _book(db_session, "B00VVAB0001", "Older", 2019, vvab=True)
    await _book(db_session, "B00VVAB0002", "Newer", 2023, vvab=True)
    await db_session.commit()

    books = await get_vvab_books_from_db(db_session, sort="releaseDate", order="desc")
    assert [b["asin"] for b in books] == ["B00VVAB0002", "B00VVAB0001"]


# ============================================================
# /db/plans/{plan_name}
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_plans_sort_by_title_asc(db_session):
    await _book(db_session, "B00PLAN0001", "Zebra", 2020, plans=["US Minerva"])
    await _book(db_session, "B00PLAN0002", "Apple", 2021, plans=["US Minerva"])
    await db_session.commit()

    books = await get_books_by_plan_from_db(
        db_session, "US Minerva", sort="title", order="asc"
    )
    assert [b["title"] for b in books] == ["Apple", "Zebra"]


# ============================================================
# /db/narrator/books
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_narrator_books_sort_by_release_date_asc(db_session):
    await db_session.execute(insert(Narrator).values(name="Test Narrator", created_at=NOW, updated_at=NOW))
    await _book(db_session, "B00NARRBK01", "First", 2018)
    await _book(db_session, "B00NARRBK02", "Second", 2022)
    await db_session.execute(insert(book_narrator).values(book_asin="B00NARRBK01", narrator_name="Test Narrator"))
    await db_session.execute(insert(book_narrator).values(book_asin="B00NARRBK02", narrator_name="Test Narrator"))
    await db_session.commit()

    books = await get_narrator_books_from_db(
        db_session, "Test Narrator", sort="releaseDate", order="asc"
    )
    assert [b["asin"] for b in books] == ["B00NARRBK01", "B00NARRBK02"]


# ============================================================
# /db/author/{asin}/books
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_author_books_sort_by_title_desc(db_session):
    await db_session.execute(
        insert(Author).values(asin="B00AUTHOR01", name="Test Author", region="us", created_at=NOW, updated_at=NOW)
    )
    await _book(db_session, "B00AUTHBK01", "Alpha", 2020)
    await _book(db_session, "B00AUTHBK02", "Beta", 2021)
    aid = (await db_session.execute(select(Author.id).where(Author.asin == "B00AUTHOR01"))).scalar_one()
    await db_session.execute(insert(author_book).values(author_id=aid, book_asin="B00AUTHBK01"))
    await db_session.execute(insert(author_book).values(author_id=aid, book_asin="B00AUTHBK02"))
    await db_session.commit()

    books = await get_author_books_from_db(
        db_session, "B00AUTHOR01", "us", sort="title", order="desc"
    )
    assert [b["title"] for b in books] == ["Beta", "Alpha"]


# ============================================================
# /db/narrator (search)
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_narrator_search_sort_by_name_asc(db_session):
    await db_session.execute(insert(Narrator).values(name="Zoe Smith", created_at=NOW, updated_at=NOW))
    await db_session.execute(insert(Narrator).values(name="Amy Smith", created_at=NOW, updated_at=NOW))
    await db_session.commit()

    narrators = await search_narrators_from_db(
        db_session, "Smith", sort="name", order="asc"
    )
    assert [n["name"] for n in narrators] == ["Amy Smith", "Zoe Smith"]


# ============================================================
# /db/series/{asin}/books — sort overrides position
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_series_books_default_is_position(db_session):
    await db_session.execute(insert(Series).values(asin="B00SERIES01", title="A Series", region="us", created_at=NOW, updated_at=NOW))
    await _book(db_session, "B00SERBK001", "Charlie", 2020)
    await _book(db_session, "B00SERBK002", "Alpha", 2021)
    await db_session.execute(insert(book_series).values(book_asin="B00SERBK001", series_asin="B00SERIES01", position="1"))
    await db_session.execute(insert(book_series).values(book_asin="B00SERBK002", series_asin="B00SERIES01", position="2"))
    await db_session.commit()

    # No sort: position order (book 1 first, even though its title sorts later)
    books = await get_series_books_from_db(db_session, "B00SERIES01")
    assert [b["asin"] for b in books] == ["B00SERBK001", "B00SERBK002"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_series_books_sort_overrides_position(db_session):
    await db_session.execute(insert(Series).values(asin="B00SERIES02", title="B Series", region="us", created_at=NOW, updated_at=NOW))
    await _book(db_session, "B00SERBK003", "Charlie", 2020)
    await _book(db_session, "B00SERBK004", "Alpha", 2021)
    await db_session.execute(insert(book_series).values(book_asin="B00SERBK003", series_asin="B00SERIES02", position="1"))
    await db_session.execute(insert(book_series).values(book_asin="B00SERBK004", series_asin="B00SERIES02", position="2"))
    await db_session.commit()

    # Sort by title: Alpha (position 2) comes before Charlie (position 1)
    books = await get_series_books_from_db(
        db_session, "B00SERIES02", sort="title", order="asc"
    )
    assert [b["title"] for b in books] == ["Alpha", "Charlie"]