"""
Database reader service.
Reads from relational tables and reconstructs full response dicts.

Used as fallback when Audible is unavailable.
Returns the same dict format as the Audible services.
"""

# Standard library
from typing import Any

# Third party
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# Database
from app.db.models import Book, Author, Series, Track, author_book, book_series

# Core
from app.core.logging import get_logger
from app.services.audible.client import REGION_MAP

logger = get_logger()


# ============================================================
# HELPERS
# ============================================================

def _audible_link(asin: str, region: str) -> str:
    tld = REGION_MAP.get(region, ".com")
    return f"https://audible{tld}/pd/{asin}"


async def _get_series_positions(session: AsyncSession, book_asin: str) -> dict[str, str | None]:
    """Returns {series_asin: position} for a book."""
    result = await session.execute(
        select(book_series.c.series_asin, book_series.c.position)
        .where(book_series.c.book_asin == book_asin)
    )
    return {row[0]: row[1] for row in result.fetchall()}


def _book_to_dict(book: Book, series_positions: dict[str, str | None]) -> dict[str, Any]:
    """Converts a Book ORM object to the same dict format as _normalize_product."""
    release_date = None
    if book.release_date:
        try:
            release_date = book.release_date.isoformat()
        except Exception:
            pass

    authors = [
        {
            "id": a.id,
            "asin": a.asin,
            "name": a.name,
            "region": a.region,
            "regions": [a.region],
            "image": a.image,
            "updatedAt": a.updated_at.isoformat() if a.updated_at else None,
        }
        for a in (book.authors or [])
    ]

    narrators = [
        {
            "name": n.name,
            "updatedAt": n.updated_at.isoformat() if n.updated_at else None,
        }
        for n in (book.narrators or [])
    ]

    genres = [
        {
            "asin": g.asin,
            "name": g.name,
            "type": g.type,
            "betterType": g.type.lower().rstrip("s"),
            "updatedAt": g.updated_at.isoformat() if g.updated_at else None,
        }
        for g in (book.genres or [])
    ]

    series = [
        {
            "asin": s.asin,
            "name": s.title,
            "position": series_positions.get(s.asin),
            "region": s.region,
            "updatedAt": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in (book.series or [])
    ]

    content_type = book.content_type
    is_podcast = content_type and content_type.lower() == "podcast"

    return {
        "asin": book.asin,
        "title": book.title,
        "subtitle": book.subtitle,
        "description": book.description,
        "summary": book.summary,
        "region": book.region,
        "regions": [book.region],
        "publisher": book.publisher,
        "copyright": book.copyright,
        "isbn": book.isbn,
        "language": book.language,
        "rating": book.rating,
        "bookFormat": book.book_format,
        "releaseDate": release_date,
        "explicit": book.explicit,
        "hasPdf": book.has_pdf,
        "whisperSync": book.whisper_sync,
        "imageUrl": book.image,
        "lengthMinutes": book.length_minutes,
        "link": _audible_link(book.asin, book.region),
        "contentType": content_type,
        "contentDeliveryType": book.content_delivery_type,
        "episodeNumber": book.episode_number if is_podcast else None,
        "episodeType": book.episode_type if is_podcast else None,
        "sku": book.sku,
        "skuGroup": book.sku_group,
        "isListenable": book.is_listenable,
        "isAvailable": book.is_buyable,
        "isBuyable": book.is_buyable,
        "plans": book.plans,
        "updatedAt": book.updated_at.isoformat() if book.updated_at else None,
        "authors": authors,
        "narrators": narrators,
        "genres": genres,
        "series": series,
    }


# ============================================================
# BOOK READERS
# ============================================================

async def get_book_from_db(session: AsyncSession, asin: str) -> dict[str, Any] | None:
    """Fetches a single book from the DB with all relationships."""
    try:
        result = await session.execute(
            select(Book)
            .where(Book.asin == asin)
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
        )
        book = result.scalar_one_or_none()
        if not book:
            return None

        positions = await _get_series_positions(session, asin)
        return _book_to_dict(book, positions)
    except Exception as e:
        logger.warning(f"DB read failed for book {asin}: {e}")
        return None


async def get_books_from_db(session: AsyncSession, asins: list[str]) -> list[dict[str, Any]]:
    """Fetches multiple books from the DB with all relationships."""
    try:
        result = await session.execute(
            select(Book)
            .where(Book.asin.in_(asins))
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
        )
        books = result.scalars().all()
        results = []
        for book in books:
            positions = await _get_series_positions(session, book.asin)
            results.append(_book_to_dict(book, positions))
        return results
    except Exception as e:
        logger.warning(f"DB read failed for books {asins}: {e}")
        return []

async def search_books_from_db(
    session: AsyncSession,
    title: str | None = None,
    subtitle: str | None = None,
    region: str | None = None,
    description: str | None = None,
    summary: str | None = None,
    publisher: str | None = None,
    copyright: str | None = None,
    isbn: str | None = None,
    author_name: str | None = None,
    series_name: str | None = None,
    language: str | None = None,
    rating_better_than: float | None = None,
    rating_worse_than: float | None = None,
    longer_than: int | None = None,
    shorter_than: int | None = None,
    explicit: bool | None = None,
    whisper_sync: bool | None = None,
    has_pdf: bool | None = None,
    book_format: str | None = None,
    content_type: str | None = None,
    content_delivery_type: str | None = None,
    is_listenable: bool | None = None,
    is_buyable: bool | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Searches books in the DB by filter parameters with pagination."""
    try:
        stmt = (
            select(Book)
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
        )

        if title is not None:
            stmt = stmt.where(Book.title.ilike(f"%{title}%"))
        if subtitle is not None:
            stmt = stmt.where(Book.subtitle.ilike(f"%{subtitle}%"))
        if region is not None:
            stmt = stmt.where(Book.region == region)
        if description is not None:
            stmt = stmt.where(Book.description.ilike(f"%{description}%"))
        if summary is not None:
            stmt = stmt.where(Book.summary.ilike(f"%{summary}%"))
        if publisher is not None:
            stmt = stmt.where(Book.publisher.ilike(f"%{publisher}%"))
        if copyright is not None:
            stmt = stmt.where(Book.copyright.ilike(f"%{copyright}%"))
        if isbn is not None:
            stmt = stmt.where(Book.isbn.ilike(f"%{isbn}%"))
        if author_name is not None:
            stmt = (
                stmt
                .join(author_book, author_book.c.book_asin == Book.asin)
                .join(Author, Author.id == author_book.c.author_id)
                .where(Author.name.ilike(f"%{author_name}%"))
                .distinct()
            )
        if series_name is not None:
            stmt = (
                stmt
                .join(book_series, book_series.c.book_asin == Book.asin)
                .join(Series, Series.asin == book_series.c.series_asin)
                .where(Series.title.ilike(f"%{series_name}%"))
                .distinct()
            )
        if language is not None:
            stmt = stmt.where(Book.language == language)
        if rating_better_than is not None:
            stmt = stmt.where(Book.rating >= rating_better_than)
        if rating_worse_than is not None:
            stmt = stmt.where(Book.rating <= rating_worse_than)
        if longer_than is not None:
            stmt = stmt.where(Book.length_minutes >= longer_than)
        if shorter_than is not None:
            stmt = stmt.where(Book.length_minutes <= shorter_than)
        if explicit is not None:
            stmt = stmt.where(Book.explicit == explicit)
        if whisper_sync is not None:
            stmt = stmt.where(Book.whisper_sync == whisper_sync)
        if has_pdf is not None:
            stmt = stmt.where(Book.has_pdf == has_pdf)
        if book_format is not None:
            stmt = stmt.where(Book.book_format == book_format)
        if content_type is not None:
            stmt = stmt.where(Book.content_type == content_type)
        if content_delivery_type is not None:
            stmt = stmt.where(Book.content_delivery_type == content_delivery_type)
        if is_listenable is not None:
            stmt = stmt.where(Book.is_listenable == is_listenable)
        if is_buyable is not None:
            stmt = stmt.where(Book.is_buyable == is_buyable)

        stmt = stmt.limit(limit).offset((page - 1) * limit)

        result = await session.execute(stmt)
        books = result.scalars().all()

        results = []
        for book in books:
            positions = await _get_series_positions(session, book.asin)
            results.append(_book_to_dict(book, positions))
        return results
    except Exception as e:
        logger.warning(f"DB search failed for books: {e}")
        return []

async def get_books_by_sku_from_db(session: AsyncSession, sku_group: str) -> list[dict[str, Any]]:
    """Fetches all books with a matching sku_group from the DB."""
    try:
        result = await session.execute(
            select(Book)
            .where(Book.sku_group == sku_group)
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
        )
        books = result.scalars().all()
        results = []
        for book in books:
            positions = await _get_series_positions(session, book.asin)
            results.append(_book_to_dict(book, positions))
        return results
    except Exception as e:
        logger.warning(f"DB read failed for sku_group {sku_group}: {e}")
        return []

# ============================================================
# AUTHOR READER
# ============================================================

async def get_author_from_db(session: AsyncSession, asin: str, region: str) -> dict[str, Any] | None:
    """Fetches an author from the DB with genres."""
    try:
        result = await session.execute(
            select(Author)
            .where(Author.asin == asin, Author.region == region)
            .options(selectinload(Author.genres))
        )
        author = result.scalar_one_or_none()
        if not author:
            return None

        genres = [
            {
                "asin": g.asin,
                "name": g.name,
                "type": g.type,
                "betterType": g.type.lower().rstrip("s"),
                "updatedAt": g.updated_at.isoformat() if g.updated_at else None,
            }
            for g in (author.genres or [])
        ]

        return {
            "id": author.id,
            "asin": author.asin,
            "name": author.name,
            "description": author.description,
            "image": author.image,
            "region": author.region,
            "regions": [author.region],
            "genres": genres,
            "updatedAt": author.updated_at.isoformat() if author.updated_at else None,
        }
    except Exception as e:
        logger.warning(f"DB read failed for author {asin}: {e}")
        return None


async def get_author_books_from_db(
    session: AsyncSession, author_asin: str, region: str
) -> list[dict[str, Any]]:
    """Fetches all books for an author from the DB."""
    try:
        result = await session.execute(
            select(Book)
            .join(author_book, author_book.c.book_asin == Book.asin)
            .join(Author, Author.id == author_book.c.author_id)
            .where(Author.asin == author_asin, Author.region == region)
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
            .distinct()
        )
        books = result.scalars().all()
        results = []
        for book in books:
            positions = await _get_series_positions(session, book.asin)
            results.append(_book_to_dict(book, positions))
        return results
    except Exception as e:
        logger.warning(f"DB read failed for author books {author_asin}: {e}")
        return []


# ============================================================
# SERIES READER
# ============================================================

async def get_series_from_db(session: AsyncSession, asin: str) -> dict[str, Any] | None:
    """Fetches a series from the DB."""
    try:
        result = await session.execute(
            select(Series).where(Series.asin == asin)
        )
        series = result.scalar_one_or_none()
        if not series:
            return None

        return {
            "asin": series.asin,
            "name": series.title,
            "description": series.description,
            "region": series.region,
            "position": None,
            "updatedAt": series.updated_at.isoformat() if series.updated_at else None,
        }
    except Exception as e:
        logger.warning(f"DB read failed for series {asin}: {e}")
        return None


async def search_series_from_db(session: AsyncSession, name: str) -> list[dict[str, Any]]:
    """Searches for series by name in the DB."""
    try:
        result = await session.execute(
            select(Series)
            .where(Series.title.ilike(f"%{name}%"))
            .limit(10)
        )
        series_list = result.scalars().all()
        return [
            {
                "asin": s.asin,
                "name": s.title,
                "description": s.description,
                "region": s.region,
                "position": None,
                "updatedAt": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in series_list
        ]
    except Exception as e:
        logger.warning(f"DB search failed for series '{name}': {e}")
        return []


async def get_series_books_from_db(
    session: AsyncSession, series_asin: str
) -> list[dict[str, Any]]:
    """Fetches all books in a series from the DB, sorted by position."""
    try:
        result = await session.execute(
            select(Book)
            .join(book_series, book_series.c.book_asin == Book.asin)
            .where(book_series.c.series_asin == series_asin)
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
            .order_by(book_series.c.position)
        )
        books = result.scalars().all()
        results = []
        for book in books:
            positions = await _get_series_positions(session, book.asin)
            results.append(_book_to_dict(book, positions))
        return results
    except Exception as e:
        logger.warning(f"DB read failed for series books {series_asin}: {e}")
        return []


# ============================================================
# TRACK READER
# ============================================================

async def get_track_from_db(session: AsyncSession, asin: str) -> dict[str, Any] | None:
    """Fetches chapter data for a book from the DB."""
    try:
        result = await session.execute(
            select(Track).where(Track.asin == asin)
        )
        track = result.scalar_one_or_none()
        if not track:
            return None
        return track.chapters
    except Exception as e:
        logger.warning(f"DB read failed for track {asin}: {e}")
        return None