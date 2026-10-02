"""
Book readers: one book, many books, searches, and the filtered lists built on
the same book query (by plan, VVAB, new releases, coming soon), plus chapters.
"""

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third party
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from libex_core.storage.filtering import apply_book_filters
from libex_core.storage.models import Book, Genre, Track
from libex_core.storage.read._compat import ILike, JsonListContains, dialect_name
from libex_core.storage.read.shapes import (
    BOOK_RELATIONS,
    book_to_dict,
    hydrate_books,
    series_positions,
)
from libex_core.storage.sorting import BOOK_SORT_FIELDS, apply_sort


async def get_book(session: AsyncSession, asin: str) -> dict[str, Any] | None:
    """Fetches a single book from the DB with all relationships."""
    result = await session.execute(
        select(Book)
        .where(Book.asin == asin)
        .options(*BOOK_RELATIONS)
    )
    book = result.scalar_one_or_none()
    if not book:
        return None

    positions = await series_positions(session, asin)
    return book_to_dict(book, positions)


async def get_books(session: AsyncSession, asins: list[str]) -> list[dict[str, Any]]:
    """Fetches multiple books from the DB with all relationships."""
    result = await session.execute(
        select(Book)
        .where(Book.asin.in_(asins))
        .options(*BOOK_RELATIONS)
    )
    books = result.scalars().all()
    return await hydrate_books(session, books)


async def search_books(
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
    category: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Searches books in the DB by filter parameters with pagination."""
    stmt = (
        select(Book)
        .options(*BOOK_RELATIONS)
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
        category=category,
    )

    stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)

    stmt = stmt.limit(limit).offset((page - 1) * limit)

    result = await session.execute(stmt)
    books = result.scalars().all()

    return await hydrate_books(session, books)


async def get_books_by_sku(session: AsyncSession, sku_group: str) -> list[dict[str, Any]]:
    """Fetches all books with a matching sku_group from the DB."""
    result = await session.execute(
        select(Book)
        .where(Book.sku_group == sku_group)
        .options(*BOOK_RELATIONS)
    )
    books = result.scalars().all()
    return await hydrate_books(session, books)


# SQLite spelling of jsonb_array_elements_text: one row per element of each
# stored plans array, rendered as text the way Postgres renders it. Nested
# arrays and objects, and real numbers, come out as SQLite's own JSON text,
# which can differ from Postgres in spacing; plans only ever hold strings.
_SQLITE_PLAN_NAMES = text(
    "SELECT DISTINCT CASE _je.type"
    " WHEN 'text' THEN _je.value"
    " WHEN 'true' THEN 'true'"
    " WHEN 'false' THEN 'false'"
    " ELSE CAST(_je.value AS TEXT) END AS plan_name"
    " FROM books, json_each(books.plans) AS _je"
    " WHERE books.plans IS NOT NULL AND json_type(books.plans) = 'array'"
)


async def distinct_plans(session: AsyncSession) -> list[str]:
    """Returns a sorted list of all distinct plan names across stored books."""
    if dialect_name(session) == "sqlite":
        stmt = _SQLITE_PLAN_NAMES
    else:
        stmt = (
            select(
                func.jsonb_array_elements_text(Book.plans).label("plan_name")
            )
            .where(Book.plans.isnot(None))
            .distinct()
        )
    result = await session.execute(stmt)
    plans = sorted([row[0] for row in result.fetchall()])
    return plans


async def distinct_genres(
    session: AsyncSession,
    search: str | None = None,
) -> list[str]:
    """Returns a sorted list of all distinct genre and tag names.

    Optional `search` filters the list by partial, case-insensitive match —
    useful for finding the exact category name to feed the genre filter.
    """
    stmt = select(Genre.name).distinct()
    if search:
        stmt = stmt.where(ILike(Genre.name, f"%{search}%"))
    result = await session.execute(stmt)
    names = sorted({row[0] for row in result.fetchall()})
    return names


async def get_books_by_plan(
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
    category: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Fetches all books containing a specific plan name."""
    stmt = (
        select(Book)
        .where(JsonListContains(Book.plans, plan_name))
        .options(*BOOK_RELATIONS)
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
        category=category,
    )
    stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
    stmt = stmt.limit(limit).offset((page - 1) * limit)
    result = await session.execute(stmt)
    books = result.scalars().all()
    return await hydrate_books(session, books)


async def get_vvab_books(
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
    category: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Fetches all virtual voice audiobooks (AI-narrated) from the local DB."""
    stmt = (
        select(Book)
        .where(Book.is_vvab.is_(True))
        .options(*BOOK_RELATIONS)
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
        category=category,
    )
    stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
    stmt = stmt.limit(limit).offset((page - 1) * limit)
    result = await session.execute(stmt)
    books = result.scalars().all()
    return await hydrate_books(session, books)


async def get_new_releases(
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
    category: str | None = None,
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
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    stmt = (
        select(Book)
        .where(
            Book.release_date.isnot(None),
            Book.release_date >= window_start,
            Book.release_date <= now,
        )
        .options(*BOOK_RELATIONS)
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
        category=category,
    )
    if sort:
        stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
    else:
        stmt = stmt.order_by(Book.release_date.desc().nulls_last())
    stmt = stmt.limit(limit).offset((page - 1) * limit)
    result = await session.execute(stmt)
    books = result.scalars().all()
    return await hydrate_books(session, books)


async def get_coming_soon(
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
    category: str | None = None,
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
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=days)
    stmt = (
        select(Book)
        .where(
            Book.release_date.isnot(None),
            Book.release_date > now,
            Book.release_date <= window_end,
        )
        .options(*BOOK_RELATIONS)
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
        category=category,
    )
    if sort:
        stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
    else:
        stmt = stmt.order_by(Book.release_date.asc().nulls_last())
    stmt = stmt.limit(limit).offset((page - 1) * limit)
    result = await session.execute(stmt)
    books = result.scalars().all()
    return await hydrate_books(session, books)


async def get_track(session: AsyncSession, asin: str) -> dict[str, Any] | None:
    """Fetches chapter data for a book from the DB."""
    result = await session.execute(
        select(Track).where(Track.asin == asin)
    )
    track = result.scalar_one_or_none()
    if not track:
        return None
    return track.chapters
