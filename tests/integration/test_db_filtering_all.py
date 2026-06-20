"""
Integration tests for broader filtering across DB endpoints.

Covers the full book-filter set on the scoped book endpoints (vvab, plans,
narrator books, author books, series books), the author endpoint's dual-region
handling (author region vs book_region filter), and the narrator filters
(gender, language JSONB key-exists, audiobooksProduced bucket, heritage).
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


async def _book(session, asin, title, *, region="us", rating=None, length=None, vvab=False, plans=None, explicit=False):
    await session.execute(
        insert(Book).values(
            asin=asin,
            title=title,
            region=region,
            rating=rating,
            length_minutes=length,
            is_vvab=vvab,
            plans=plans,
            explicit=explicit,
            created_at=NOW,
            updated_at=NOW,
        )
    )


# ============================================================
# Book filters on scoped endpoints
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_vvab_filter_by_rating(db_session):
    await _book(db_session, "B00VV001", "Low", vvab=True, rating=2.0)
    await _book(db_session, "B00VV002", "High", vvab=True, rating=4.5)
    await db_session.commit()
    books = await get_vvab_books_from_db(db_session, rating_better_than=4.0)
    assert [b["asin"] for b in books] == ["B00VV002"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plan_filter_by_length(db_session):
    await _book(db_session, "B00PL001", "Short", plans=["US Minerva"], length=100)
    await _book(db_session, "B00PL002", "Long", plans=["US Minerva"], length=900)
    await db_session.commit()
    books = await get_books_by_plan_from_db(db_session, "US Minerva", longer_than=500)
    assert [b["asin"] for b in books] == ["B00PL002"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_narrator_books_filter_by_explicit(db_session):
    await db_session.execute(insert(Narrator).values(name="Nar One", created_at=NOW, updated_at=NOW))
    await _book(db_session, "B00NB001", "Clean", explicit=False)
    await _book(db_session, "B00NB002", "Explicit", explicit=True)
    await db_session.execute(insert(book_narrator).values(book_asin="B00NB001", narrator_name="Nar One"))
    await db_session.execute(insert(book_narrator).values(book_asin="B00NB002", narrator_name="Nar One"))
    await db_session.commit()
    books = await get_narrator_books_from_db(db_session, "Nar One", explicit=True)
    assert [b["asin"] for b in books] == ["B00NB002"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_series_filter_by_rating_keeps_position_order(db_session):
    await db_session.execute(insert(Series).values(asin="B00SR001", title="S", region="us", created_at=NOW, updated_at=NOW))
    await _book(db_session, "B00SB001", "First", rating=4.5)
    await _book(db_session, "B00SB002", "Second", rating=2.0)
    await _book(db_session, "B00SB003", "Third", rating=4.8)
    await db_session.execute(insert(book_series).values(book_asin="B00SB001", series_asin="B00SR001", position="1"))
    await db_session.execute(insert(book_series).values(book_asin="B00SB002", series_asin="B00SR001", position="2"))
    await db_session.execute(insert(book_series).values(book_asin="B00SB003", series_asin="B00SR001", position="3"))
    await db_session.commit()
    # Filter to highly-rated, default position order preserved among matches
    books = await get_series_books_from_db(db_session, "B00SR001", rating_better_than=4.0)
    assert [b["asin"] for b in books] == ["B00SB001", "B00SB003"]


# ============================================================
# Author dual-region
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_author_books_filter_by_book_region(db_session):
    await db_session.execute(
        insert(Author).values(asin="B00AUTH001", name="A", region="us", created_at=NOW, updated_at=NOW)
    )
    await _book(db_session, "B00AB001", "US Book", region="us")
    await _book(db_session, "B00AB002", "UK Book", region="uk")
    aid = (await db_session.execute(select(Author.id).where(Author.asin == "B00AUTH001"))).scalar_one()
    await db_session.execute(insert(author_book).values(author_id=aid, book_asin="B00AB001"))
    await db_session.execute(insert(author_book).values(author_id=aid, book_asin="B00AB002"))
    await db_session.commit()
    # Author looked up in us region; filter their books to uk-region editions
    books = await get_author_books_from_db(db_session, "B00AUTH001", "us", book_region="uk")
    assert [b["asin"] for b in books] == ["B00AB002"]


# ============================================================
# Narrator filters
# ============================================================

async def _narrator(session, name, *, gender=None, languages=None, audiobooks_produced=None, cultural_heritage=None):
    await session.execute(
        insert(Narrator).values(
            name=name,
            gender=gender,
            languages=languages,
            audiobooks_produced=audiobooks_produced,
            cultural_heritage=cultural_heritage,
            created_at=NOW,
            updated_at=NOW,
        )
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_narrator_filter_by_gender(db_session):
    await _narrator(db_session, "Filter Alpha", gender="Female")
    await _narrator(db_session, "Filter Beta", gender="Male")
    await _narrator(db_session, "Filter Gamma", gender="Other")
    await db_session.commit()
    results = await search_narrators_from_db(db_session, "Filter", gender="Other")
    assert [n["name"] for n in results] == ["Filter Gamma"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_narrator_filter_by_language_jsonb_key(db_session):
    await _narrator(db_session, "Lang Alpha", languages={"English": 5})
    await _narrator(db_session, "Lang Beta", languages={"Spanish": 5, "English": 3})
    await _narrator(db_session, "Lang Gamma", languages={"French": 5})
    await db_session.commit()
    results = await search_narrators_from_db(db_session, "Lang", language="English")
    names = sorted(n["name"] for n in results)
    assert names == ["Lang Alpha", "Lang Beta"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_narrator_filter_by_audiobooks_produced_bucket(db_session):
    await _narrator(db_session, "Prod Alpha", audiobooks_produced="1 to 10")
    await _narrator(db_session, "Prod Beta", audiobooks_produced="More than 100")
    await db_session.commit()
    results = await search_narrators_from_db(db_session, "Prod", audiobooks_produced="More than 100")
    assert [n["name"] for n in results] == ["Prod Beta"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_narrator_filter_by_heritage(db_session):
    await _narrator(db_session, "Her Alpha", cultural_heritage="Asian")
    await _narrator(db_session, "Her Beta", cultural_heritage="Hispanic or Latino")
    await db_session.commit()
    results = await search_narrators_from_db(db_session, "Her", cultural_heritage="hispanic")
    assert [n["name"] for n in results] == ["Her Beta"]