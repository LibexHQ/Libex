"""
Database reader service.
Reads from relational tables and reconstructs full response dicts.

Used as fallback when Audible is unavailable.
Returns the same dict format as the Audible services.
"""

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third party
from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# Database
from app.db.models import Book, Author, Narrator, Series, Track, Genre, CatalogGenre, author_book, book_narrator, book_series

# Services
from app.services.audible.client import REGION_MAP
from app.services.sorting import apply_sort, BOOK_SORT_FIELDS, NARRATOR_SORT_FIELDS
from app.services.db.filtering import apply_book_filters, apply_narrator_filters

# Core
from app.core.logging import get_logger

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
        "isVvab": book.is_vvab,
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
    is_vvab: bool | None = None,
    plan_name: str | None = None,
    genre: str | None = None,
    sort: str | None = None,
    order: str | None = None,
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

        stmt = apply_book_filters(
            stmt,
            title=title,
            subtitle=subtitle,
            region=region,
            description=description,
            summary=summary,
            publisher=publisher,
            copyright=copyright,
            isbn=isbn,
            author_name=author_name,
            series_name=series_name,
            language=language,
            rating_better_than=rating_better_than,
            rating_worse_than=rating_worse_than,
            longer_than=longer_than,
            shorter_than=shorter_than,
            explicit=explicit,
            whisper_sync=whisper_sync,
            has_pdf=has_pdf,
            book_format=book_format,
            content_type=content_type,
            content_delivery_type=content_delivery_type,
            is_listenable=is_listenable,
            is_buyable=is_buyable,
            is_vvab=is_vvab,
            plan_name=plan_name,
            genre=genre,
        )

        stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)

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


async def get_distinct_plans_from_db(session: AsyncSession) -> list[str]:
    """Returns a sorted list of all distinct plan names across stored books."""
    try:
        result = await session.execute(
            select(
                func.jsonb_array_elements_text(Book.plans).label("plan_name")
            )
            .where(Book.plans.isnot(None))
            .distinct()
        )
        plans = sorted([row[0] for row in result.fetchall()])
        return plans
    except Exception as e:
        logger.warning(f"DB read failed for distinct plans: {e}")
        return []


async def get_distinct_genres_from_db(
    session: AsyncSession,
    search: str | None = None,
) -> list[str]:
    """Returns a sorted list of all distinct genre and tag names.

    Optional `search` filters the list by partial, case-insensitive match —
    useful for finding the exact category name to feed the genre filter.
    """
    try:
        stmt = select(Genre.name).distinct()
        if search:
            stmt = stmt.where(Genre.name.ilike(f"%{search}%"))
        result = await session.execute(stmt)
        names = sorted({row[0] for row in result.fetchall()})
        return names
    except Exception as e:
        logger.warning(f"DB read failed for distinct genres: {e}")
        return []


async def get_books_by_plan_from_db(
    session: AsyncSession,
    plan_name: str,
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
    is_vvab: bool | None = None,
    genre: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Fetches all books containing a specific plan name."""
    try:
        stmt = (
            select(Book)
            .where(Book.plans.contains([plan_name]))
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
        )
        stmt = apply_book_filters(
            stmt,
            title=title,
            subtitle=subtitle,
            region=region,
            description=description,
            summary=summary,
            publisher=publisher,
            copyright=copyright,
            isbn=isbn,
            author_name=author_name,
            series_name=series_name,
            language=language,
            rating_better_than=rating_better_than,
            rating_worse_than=rating_worse_than,
            longer_than=longer_than,
            shorter_than=shorter_than,
            explicit=explicit,
            whisper_sync=whisper_sync,
            has_pdf=has_pdf,
            book_format=book_format,
            content_type=content_type,
            content_delivery_type=content_delivery_type,
            is_listenable=is_listenable,
            is_buyable=is_buyable,
            is_vvab=is_vvab,
            genre=genre,
        )
        stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
        stmt = stmt.limit(limit).offset((page - 1) * limit)
        result = await session.execute(stmt)
        books = result.scalars().all()
        results = []
        for book in books:
            positions = await _get_series_positions(session, book.asin)
            results.append(_book_to_dict(book, positions))
        return results
    except Exception as e:
        logger.warning(f"DB read failed for plan '{plan_name}': {e}")
        return []


async def get_vvab_books_from_db(
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
    plan_name: str | None = None,
    genre: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Fetches all virtual voice audiobooks (AI-narrated) from the local DB."""
    try:
        stmt = (
            select(Book)
            .where(Book.is_vvab.is_(True))
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
        )
        stmt = apply_book_filters(
            stmt,
            title=title,
            subtitle=subtitle,
            region=region,
            description=description,
            summary=summary,
            publisher=publisher,
            copyright=copyright,
            isbn=isbn,
            author_name=author_name,
            series_name=series_name,
            language=language,
            rating_better_than=rating_better_than,
            rating_worse_than=rating_worse_than,
            longer_than=longer_than,
            shorter_than=shorter_than,
            explicit=explicit,
            whisper_sync=whisper_sync,
            has_pdf=has_pdf,
            book_format=book_format,
            content_type=content_type,
            content_delivery_type=content_delivery_type,
            is_listenable=is_listenable,
            is_buyable=is_buyable,
            plan_name=plan_name,
            genre=genre,
        )
        stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
        stmt = stmt.limit(limit).offset((page - 1) * limit)
        result = await session.execute(stmt)
        books = result.scalars().all()
        results = []
        for book in books:
            positions = await _get_series_positions(session, book.asin)
            results.append(_book_to_dict(book, positions))
        return results
    except Exception as e:
        logger.warning(f"DB read failed for VVAB books: {e}")
        return []


async def get_new_releases_from_db(
    session: AsyncSession,
    days: int = 30,
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
    is_vvab: bool | None = None,
    plan_name: str | None = None,
    genre: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """
    Fetches books released within the last `days`, newest first by default.

    The window is release_date between (now - days) and now — already-released
    books only, so far-future pre-orders are excluded. Defaults to releaseDate
    descending; passing an explicit sort field overrides that.
    """
    try:
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=days)
        stmt = (
            select(Book)
            .where(
                Book.release_date.isnot(None),
                Book.release_date >= window_start,
                Book.release_date <= now,
            )
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
        )
        stmt = apply_book_filters(
            stmt,
            title=title,
            subtitle=subtitle,
            region=region,
            description=description,
            summary=summary,
            publisher=publisher,
            copyright=copyright,
            isbn=isbn,
            author_name=author_name,
            series_name=series_name,
            language=language,
            rating_better_than=rating_better_than,
            rating_worse_than=rating_worse_than,
            longer_than=longer_than,
            shorter_than=shorter_than,
            explicit=explicit,
            whisper_sync=whisper_sync,
            has_pdf=has_pdf,
            book_format=book_format,
            content_type=content_type,
            content_delivery_type=content_delivery_type,
            is_listenable=is_listenable,
            is_buyable=is_buyable,
            is_vvab=is_vvab,
            plan_name=plan_name,
            genre=genre,
        )
        if sort:
            stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
        else:
            stmt = stmt.order_by(Book.release_date.desc().nulls_last())
        stmt = stmt.limit(limit).offset((page - 1) * limit)
        result = await session.execute(stmt)
        books = result.scalars().all()
        results = []
        for book in books:
            positions = await _get_series_positions(session, book.asin)
            results.append(_book_to_dict(book, positions))
        return results
    except Exception as e:
        logger.warning(f"DB read failed for new releases: {e}")
        return []


async def get_coming_soon_from_db(
    session: AsyncSession,
    days: int = 30,
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
    is_vvab: bool | None = None,
    plan_name: str | None = None,
    genre: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """
    Fetches upcoming books releasing within the next `days`, soonest first.

    The window is release_date between now and (now + days) — future releases
    only. The upper bound also excludes Audible's "no date yet" placeholder
    (year 2200) and other far-future junk, since nothing that distant falls
    inside a real window. Defaults to releaseDate ascending; passing an
    explicit sort field overrides that.
    """
    try:
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(days=days)
        stmt = (
            select(Book)
            .where(
                Book.release_date.isnot(None),
                Book.release_date > now,
                Book.release_date <= window_end,
            )
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
        )
        stmt = apply_book_filters(
            stmt,
            title=title,
            subtitle=subtitle,
            region=region,
            description=description,
            summary=summary,
            publisher=publisher,
            copyright=copyright,
            isbn=isbn,
            author_name=author_name,
            series_name=series_name,
            language=language,
            rating_better_than=rating_better_than,
            rating_worse_than=rating_worse_than,
            longer_than=longer_than,
            shorter_than=shorter_than,
            explicit=explicit,
            whisper_sync=whisper_sync,
            has_pdf=has_pdf,
            book_format=book_format,
            content_type=content_type,
            content_delivery_type=content_delivery_type,
            is_listenable=is_listenable,
            is_buyable=is_buyable,
            is_vvab=is_vvab,
            plan_name=plan_name,
            genre=genre,
        )
        if sort:
            stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
        else:
            stmt = stmt.order_by(Book.release_date.asc().nulls_last())
        stmt = stmt.limit(limit).offset((page - 1) * limit)
        result = await session.execute(stmt)
        books = result.scalars().all()
        results = []
        for book in books:
            positions = await _get_series_positions(session, book.asin)
            results.append(_book_to_dict(book, positions))
        return results
    except Exception as e:
        logger.warning(f"DB read failed for coming soon: {e}")
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
    session: AsyncSession,
    author_asin: str,
    region: str,
    title: str | None = None,
    subtitle: str | None = None,
    book_region: str | None = None,
    description: str | None = None,
    summary: str | None = None,
    publisher: str | None = None,
    copyright: str | None = None,
    isbn: str | None = None,
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
    is_vvab: bool | None = None,
    plan_name: str | None = None,
    genre: str | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> list[dict[str, Any]]:
    """Fetches all books for an author from the DB."""
    try:
        stmt = (
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
        stmt = apply_book_filters(
            stmt,
            title=title,
            subtitle=subtitle,
            region=book_region,
            description=description,
            summary=summary,
            publisher=publisher,
            copyright=copyright,
            isbn=isbn,
            series_name=series_name,
            language=language,
            rating_better_than=rating_better_than,
            rating_worse_than=rating_worse_than,
            longer_than=longer_than,
            shorter_than=shorter_than,
            explicit=explicit,
            whisper_sync=whisper_sync,
            has_pdf=has_pdf,
            book_format=book_format,
            content_type=content_type,
            content_delivery_type=content_delivery_type,
            is_listenable=is_listenable,
            is_buyable=is_buyable,
            is_vvab=is_vvab,
            plan_name=plan_name,
            genre=genre,
        )
        stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
        result = await session.execute(stmt)
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
# NARRATOR READER
# ============================================================

def _narrator_to_dict(n) -> dict[str, Any]:
    """Converts a Narrator model to a response dict with attribution."""
    result = {
        "name": n.name,
        "description": n.description,
        "image": n.image,
        "website": n.website,
        "wikipediaUrl": n.wikipedia_url,
        "languages": n.languages,
        "accents": n.accents,
        "gender": n.gender,
        "genresNarrated": n.genres_narrated,
        "audiobooksProduced": n.audiobooks_produced,
        "culturalHeritage": n.cultural_heritage,
        "publishers": n.publishers,
        "socialLinks": n.social_links,
        "audioSamples": n.audio_samples,
        "source": n.source,
        "sourceUrl": n.source_url,
        "sourceUpdatedAt": n.source_updated_at.isoformat() if n.source_updated_at else None,
        "attribution": None,
        "updatedAt": n.updated_at.isoformat() if n.updated_at else None,
    }
    if n.source and n.source_updated_at:
        date_str = n.source_updated_at.strftime("%B %Y")
        result["attribution"] = f"Profile data provided by {n.source}, retrieved {date_str}"
    return result

async def search_narrators_from_db(
    session: AsyncSession,
    name: str,
    gender: str | None = None,
    language: str | None = None,
    audiobooks_produced: str | None = None,
    source: str | None = None,
    cultural_heritage: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Searches narrators by name (case-insensitive partial match)."""
    try:
        stmt = select(Narrator).where(Narrator.name.ilike(f"%{name}%"))
        stmt = apply_narrator_filters(
            stmt,
            gender=gender,
            language=language,
            audiobooks_produced=audiobooks_produced,
            source=source,
            cultural_heritage=cultural_heritage,
        )
        stmt = apply_sort(stmt, sort, order, NARRATOR_SORT_FIELDS)
        stmt = stmt.limit(limit).offset((page - 1) * limit)
        result = await session.execute(stmt)
        narrators = result.scalars().all()
        return [_narrator_to_dict(n) for n in narrators]
    except Exception as e:
        logger.warning(f"DB read failed for narrator search '{name}': {e}")
        return []


async def get_narrator_books_from_db(
    session: AsyncSession,
    name: str,
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
    is_vvab: bool | None = None,
    plan_name: str | None = None,
    genre: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Fetches all books by a narrator name from the local DB."""
    try:
        stmt = (
            select(Book)
            .join(book_narrator, Book.asin == book_narrator.c.book_asin)
            .where(book_narrator.c.narrator_name == name)
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
        )
        stmt = apply_book_filters(
            stmt,
            title=title,
            subtitle=subtitle,
            region=region,
            description=description,
            summary=summary,
            publisher=publisher,
            copyright=copyright,
            isbn=isbn,
            author_name=author_name,
            series_name=series_name,
            language=language,
            rating_better_than=rating_better_than,
            rating_worse_than=rating_worse_than,
            longer_than=longer_than,
            shorter_than=shorter_than,
            explicit=explicit,
            whisper_sync=whisper_sync,
            has_pdf=has_pdf,
            book_format=book_format,
            content_type=content_type,
            content_delivery_type=content_delivery_type,
            is_listenable=is_listenable,
            is_buyable=is_buyable,
            is_vvab=is_vvab,
            plan_name=plan_name,
            genre=genre,
        )
        stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
        stmt = stmt.limit(limit).offset((page - 1) * limit)
        result = await session.execute(stmt)
        books = result.scalars().all()
        results = []
        for book in books:
            positions = await _get_series_positions(session, book.asin)
            results.append(_book_to_dict(book, positions))
        return results
    except Exception as e:
        logger.warning(f"DB read failed for narrator books '{name}': {e}")
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
    session: AsyncSession,
    series_asin: str,
    title: str | None = None,
    subtitle: str | None = None,
    region: str | None = None,
    description: str | None = None,
    summary: str | None = None,
    publisher: str | None = None,
    copyright: str | None = None,
    isbn: str | None = None,
    author_name: str | None = None,
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
    is_vvab: bool | None = None,
    plan_name: str | None = None,
    genre: str | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> list[dict[str, Any]]:
    """Fetches all books in a series from the DB.

    Defaults to series position order. Position is a String column but commonly
    holds numeric values ("1", "2", "10", "1.5"). Plain string ordering sorts
    "10" before "2", so numeric positions are cast to Float for ordering.
    Non-numeric positions ("1-3", "Book 1", null) fall to the end in stable
    string order.

    Passing an explicit sort field overrides the position ordering.
    """
    try:
        stmt = (
            select(Book)
            .join(book_series, book_series.c.book_asin == Book.asin)
            .where(book_series.c.series_asin == series_asin)
            .options(
                selectinload(Book.authors),
                selectinload(Book.narrators),
                selectinload(Book.genres),
                selectinload(Book.series),
            )
        )
        stmt = apply_book_filters(
            stmt,
            title=title,
            subtitle=subtitle,
            region=region,
            description=description,
            summary=summary,
            publisher=publisher,
            copyright=copyright,
            isbn=isbn,
            author_name=author_name,
            language=language,
            rating_better_than=rating_better_than,
            rating_worse_than=rating_worse_than,
            longer_than=longer_than,
            shorter_than=shorter_than,
            explicit=explicit,
            whisper_sync=whisper_sync,
            has_pdf=has_pdf,
            book_format=book_format,
            content_type=content_type,
            content_delivery_type=content_delivery_type,
            is_listenable=is_listenable,
            is_buyable=is_buyable,
            is_vvab=is_vvab,
            plan_name=plan_name,
            genre=genre,
        )
        if sort:
            stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
        else:
            stmt = stmt.order_by(
                case(
                    (
                        book_series.c.position.op("~")(r"^\d+(\.\d+)?$"),
                        cast(book_series.c.position, Float),
                    ),
                    else_=None,
                ).asc().nulls_last(),
                book_series.c.position.asc(),
            )
        result = await session.execute(stmt)
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


# ============================================================
# STATS READER
# ============================================================

async def get_db_stats(session: AsyncSession) -> dict[str, int]:
    """Returns counts of books, authors, narrators, and series in the local DB."""
    try:
        books = await session.execute(select(func.count()).select_from(Book))
        authors = await session.execute(select(func.count()).select_from(Author))
        narrators = await session.execute(select(func.count()).select_from(Narrator))
        series = await session.execute(select(func.count()).select_from(Series))

        return {
            "books": books.scalar_one(),
            "authors": authors.scalar_one(),
            "narrators": narrators.scalar_one(),
            "series": series.scalar_one(),
        }
    except Exception as e:
        logger.warning(f"DB read failed for stats: {e}")
        return {"books": 0, "authors": 0, "narrators": 0, "series": 0}


async def get_stored_genres(
    session: AsyncSession, region: str
) -> tuple[list[dict[str, str]], datetime | None]:
    """
    Returns stored catalog genres for a region and the oldest last_checked
    timestamp among them, so callers can decide whether the stored set is stale
    and needs refreshing. Returns ([], None) when no genres are stored yet.
    """
    try:
        result = await session.execute(
            select(CatalogGenre.genre_id, CatalogGenre.name, CatalogGenre.last_checked)
            .where(CatalogGenre.region == region)
            .order_by(CatalogGenre.name.asc())
        )
        rows = result.fetchall()
        if not rows:
            return [], None
        genres = [{"genre_id": r[0], "name": r[1]} for r in rows]
        oldest_checked = min(r[2] for r in rows)
        return genres, oldest_checked
    except Exception as e:
        logger.warning(f"DB read failed for catalog_genres '{region}': {e}")
        return [], None