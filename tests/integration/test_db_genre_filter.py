"""
Integration tests for the genre filter and /db/genres discovery.

Verifies partial genre matching against real Postgres: "fantasy" catches
"Science Fiction & Fantasy", matching works across both Genres and Tags, the
filter composes on the book-returning endpoints, and the discovery endpoint
(with optional search) returns distinct names.
"""

# Standard library
from datetime import datetime, timezone

# Third party
import pytest
from sqlalchemy import insert

# Local
from app.db.models import Book, Genre, book_genre
from app.services.db.reader import (
    get_distinct_genres_from_db,
    search_books_from_db,
)

NOW = datetime.now(timezone.utc)

# (asin, genre_name, genre_type)
GENRES = [
    ("G001", "Science Fiction & Fantasy", "Genres"),
    ("G002", "Mystery, Thriller & Suspense", "Genres"),
    ("G003", "Epic Fantasy", "Tags"),
    ("G004", "Cozy Mystery", "Tags"),
]


async def _seed(db_session):
    # Genres / tags
    for asin, name, gtype in GENRES:
        await db_session.execute(
            insert(Genre).values(asin=asin, name=name, type=gtype, created_at=NOW, updated_at=NOW)
        )
    # Books
    await db_session.execute(insert(Book).values(asin="B00GENRE001", title="Dragon Tale", region="us", created_at=NOW, updated_at=NOW))
    await db_session.execute(insert(Book).values(asin="B00GENRE002", title="Murder Mansion", region="us", created_at=NOW, updated_at=NOW))
    await db_session.execute(insert(Book).values(asin="B00GENRE003", title="Space Wizards", region="us", created_at=NOW, updated_at=NOW))
    # Links: book1 -> SciFi&Fantasy (genre) + Epic Fantasy (tag)
    await db_session.execute(insert(book_genre).values(book_asin="B00GENRE001", genre_asin="G001"))
    await db_session.execute(insert(book_genre).values(book_asin="B00GENRE001", genre_asin="G003"))
    # book2 -> Mystery (genre) + Cozy Mystery (tag)
    await db_session.execute(insert(book_genre).values(book_asin="B00GENRE002", genre_asin="G002"))
    await db_session.execute(insert(book_genre).values(book_asin="B00GENRE002", genre_asin="G004"))
    # book3 -> SciFi&Fantasy (genre) only
    await db_session.execute(insert(book_genre).values(book_asin="B00GENRE003", genre_asin="G001"))
    await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_genre_partial_matches_compound_name(db_session):
    """'fantasy' catches 'Science Fiction & Fantasy' and 'Epic Fantasy'."""
    await _seed(db_session)
    books = await search_books_from_db(db_session, genre="fantasy")
    asins = {b["asin"] for b in books}
    # book1 (SciFi&Fantasy genre + Epic Fantasy tag) and book3 (SciFi&Fantasy)
    assert asins == {"B00GENRE001", "B00GENRE003"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_genre_matches_across_genres_and_tags(db_session):
    """'mystery' catches both the Mystery genre and Cozy Mystery tag on book2."""
    await _seed(db_session)
    books = await search_books_from_db(db_session, genre="mystery")
    asins = {b["asin"] for b in books}
    assert asins == {"B00GENRE002"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_genre_no_duplicate_rows(db_session):
    """A book matching on multiple genres/tags returns once, not per match."""
    await _seed(db_session)
    # book1 matches "fantasy" on both its genre and its tag — must appear once
    books = await search_books_from_db(db_session, genre="fantasy")
    asins = [b["asin"] for b in books]
    assert asins.count("B00GENRE001") == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_genre_no_match_returns_empty(db_session):
    """A genre nothing is tagged with returns no books."""
    await _seed(db_session)
    books = await search_books_from_db(db_session, genre="horror")
    assert books == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_genre_composes_with_other_filters(db_session):
    """Genre filter narrows alongside another filter."""
    await _seed(db_session)
    # fantasy + title "Dragon" should match only book1, not book3
    books = await search_books_from_db(db_session, genre="fantasy", title="Dragon")
    asins = {b["asin"] for b in books}
    assert asins == {"B00GENRE001"}


# ============================================================
# /db/genres discovery
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_distinct_genres_returns_all(db_session):
    """Discovery returns every distinct genre/tag name."""
    await _seed(db_session)
    names = await get_distinct_genres_from_db(db_session)
    assert set(names) == {
        "Science Fiction & Fantasy",
        "Mystery, Thriller & Suspense",
        "Epic Fantasy",
        "Cozy Mystery",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_distinct_genres_search_filters(db_session):
    """Discovery search narrows by partial match."""
    await _seed(db_session)
    names = await get_distinct_genres_from_db(db_session, search="fantasy")
    assert set(names) == {"Science Fiction & Fantasy", "Epic Fantasy"}