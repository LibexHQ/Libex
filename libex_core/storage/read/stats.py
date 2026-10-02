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

    `region=None` returns the global counts under five keys. Passing a region
    scopes books, authors, booksWithChapters and series to it. Two counts
    cannot follow:

    - narrators has no region column at all (the name is the primary key, and
      a narrator is not owned by any one marketplace), so a region-scoped call
      still returns the global narrator count.
    - series.region is nullable, so a per-region series count is a subset of
      the global one: rows with no recorded region fall out of every
      per-region total. A region-scoped result therefore carries a sixth key,
      seriesRegionUnknown, the count of series rows with no region, so the
      gap is visible instead of passing for a complete total.

    booksWithChapters is scoped by joining tracks to books on asin, since
    tracks carries no region column.
    """
    books_stmt = select(func.count()).select_from(Book)
    authors_stmt = select(func.count()).select_from(Author)
    series_stmt = select(func.count()).select_from(Series)
    chapters_stmt = select(func.count()).select_from(Track)

    if region is not None:
        books_stmt = books_stmt.where(Book.region == region)
        authors_stmt = authors_stmt.where(Author.region == region)
        series_stmt = series_stmt.where(Series.region == region)
        chapters_stmt = (
            chapters_stmt
            .join(Book, Book.asin == Track.asin)
            .where(Book.region == region)
        )

    books = await session.execute(books_stmt)
    authors = await session.execute(authors_stmt)
    narrators = await session.execute(select(func.count()).select_from(Narrator))
    series = await session.execute(series_stmt)
    books_with_chapters = await session.execute(chapters_stmt)

    stats = {
        "books": books.scalar_one(),
        "authors": authors.scalar_one(),
        "narrators": narrators.scalar_one(),
        "series": series.scalar_one(),
        "booksWithChapters": books_with_chapters.scalar_one(),
    }

    if region is not None:
        series_region_unknown = await session.execute(
            select(func.count()).select_from(Series).where(Series.region.is_(None))
        )
        stats["seriesRegionUnknown"] = series_region_unknown.scalar_one()

    return stats
