"""
Series readers.
"""

# Standard library
from typing import Any

# Third party
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from libex_core.storage.filtering import apply_book_filters
from libex_core.storage.models import Book, Series, book_series
from libex_core.storage.read._compat import AscNullsLast, ILike, NumericPosition
from libex_core.storage.read.shapes import BOOK_RELATIONS, hydrate_books
from libex_core.storage.sorting import BOOK_SORT_FIELDS, apply_sort


async def get_series(session: AsyncSession, asin: str) -> dict[str, Any] | None:
    """Fetches a series from the DB."""
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
        "audibleExtras": series.audible_extras,
        "extrasWithheld": series.extras_withheld,
    }


async def search_series(session: AsyncSession, name: str) -> list[dict[str, Any]]:
    """Searches for series by name in the DB."""
    result = await session.execute(
        select(Series)
        .where(ILike(Series.title, f"%{name}%"))
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
            "audibleExtras": s.audible_extras,
            "extrasWithheld": s.extras_withheld,
        }
        for s in series_list
    ]


async def get_series_books(
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
    category: str | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> list[dict[str, Any]]:
    """Fetches all books in a series from the DB.

    Defaults to series position order. Position is a String column but commonly
    holds numeric values ("1", "2", "10", "1.5"). Plain string ordering sorts
    "10" before "2", so numeric positions are cast to Float for ordering.
    Non-numeric positions ("1-3", "Book 1", null) fall to the end in stable
    string order, which is the backend's collation: locale-aware on Postgres,
    byte order on SQLite.

    Passing an explicit sort field overrides the position ordering.
    """
    stmt = (
        select(Book)
        .join(book_series, book_series.c.book_asin == Book.asin)
        .where(book_series.c.series_asin == series_asin)
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
        stmt = stmt.order_by(
            NumericPosition(book_series.c.position).asc().nulls_last(),
            AscNullsLast(book_series.c.position),
        )
    result = await session.execute(stmt)
    books = result.scalars().all()
    return await hydrate_books(session, books)
