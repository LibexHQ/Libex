"""
Row counts over the stored catalog.
"""

# Third party
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from libex_core.storage.models import Author, Book, Narrator, Series, Track


async def count_stored(session: AsyncSession, region: str | None = None) -> dict[str, int]:
    """
    Counts of books, authors, narrators, series, and books with stored chapter
    data. booksWithChapters counts rows in the tracks table (one per book that
    actually has chapters stored), not books that have merely been checked:
    checked includes ISBN-keyed records and bundle ASINs that will never have
    chapters, which would overstate what is held.

    `region=None` returns the global counts. Passing a region scopes books,
    authors, booksWithChapters and series to it. One count cannot follow:
    narrators has no region column at all (the name is the primary key, and a
    narrator is not owned by any one marketplace), so a region-scoped call
    still returns the global narrator count.

    books counts stored records, one per (asin, region), so the per-region
    counts sum to it. distinctBookAsins counts the ASINs among them, which is
    smaller whenever a book is stored under more than one region; scoped to a
    region the two are equal. Likewise series counts (asin, region) records.

    A region-scoped result also carries seriesRegionUnknown, kept so the shape
    does not change under a caller. It is always 0: a series row has a region
    by construction now (the column is part of its key), so no series falls
    out of the per-region totals.

    booksWithChapters is scoped by joining tracks to books on the key, asin
    and region together.
    """
    books_stmt = select(func.count()).select_from(Book)
    # One primary row per ASIN, so this is the number of distinct ASINs and
    # needs no sort or hash of 1.8M values. Within one region an ASIN has one
    # row, so the scoped count is the plain record count.
    distinct_stmt = select(func.count()).select_from(Book).where(Book.is_primary.is_(True))
    authors_stmt = select(func.count()).select_from(Author)
    series_stmt = select(func.count()).select_from(Series)
    chapters_stmt = select(func.count()).select_from(Track)

    if region is not None:
        books_stmt = books_stmt.where(Book.region == region)
        distinct_stmt = select(func.count()).select_from(Book).where(Book.region == region)
        authors_stmt = authors_stmt.where(Author.region == region)
        series_stmt = series_stmt.where(Series.region == region)
        chapters_stmt = (
            chapters_stmt
            .join(Book, (Book.asin == Track.asin) & (Book.region == Track.region))
            .where(Book.region == region)
        )

    books = await session.execute(books_stmt)
    authors = await session.execute(authors_stmt)
    narrators = await session.execute(select(func.count()).select_from(Narrator))
    series = await session.execute(series_stmt)
    books_with_chapters = await session.execute(chapters_stmt)
    distinct_books = await session.execute(distinct_stmt)

    stats = {
        "books": books.scalar_one(),
        "distinctBookAsins": distinct_books.scalar_one(),
        "authors": authors.scalar_one(),
        "narrators": narrators.scalar_one(),
        "series": series.scalar_one(),
        "booksWithChapters": books_with_chapters.scalar_one(),
    }

    if region is not None:
        stats["seriesRegionUnknown"] = 0

    return stats
