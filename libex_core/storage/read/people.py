"""
Author and narrator readers.
"""

# Standard library
import logging
from typing import Any

# Third party
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# Local
from libex_core.storage.filtering import apply_book_filters, apply_narrator_filters
from libex_core.storage.models import Author, Book, Narrator, author_book, book_narrator
from libex_core.storage.read._compat import ILike
from libex_core.storage.read._regions import only_first_stored
from libex_core.storage.read.shapes import (
    BOOK_RELATIONS,
    book_to_dict,
    hydrate_books,
    narrator_to_dict,
    series_positions_by_book,
)
from libex_core.storage.sorting import BOOK_SORT_FIELDS, NARRATOR_SORT_FIELDS, apply_sort

logger = logging.getLogger("libex")


async def get_author(session: AsyncSession, asin: str, region: str) -> dict[str, Any] | None:
    """Fetches an author from the DB with genres.

    The unique constraint on the authors table covers (asin, region, name), not
    just (asin, region) — a fresh spelling of an existing author's name inserts
    a second row instead of updating the first, so more than one row can match
    here. Every matching row is fetched, ordered by id both in the query and
    again defensively on the fetched rows (so the merge below is deterministic
    regardless of what order rows come back in), then merged. Identity and
    content are selected independently of each other:

    - id, name and region come from the oldest row. This is a stability choice,
      not a content comparison — oldest-id is also the convention
      libex_core.storage.write.entities.upsert_author uses to converge concurrent writers, but only for its own,
      narrower case: same-name rows still missing an asin, racing to claim
      one. That path never runs for what this function merges — a non-null
      asin under a *different* name spelling — which upsert_author's exact
      three-column match misses, falling through to a plain insert of a new
      row instead of converging on the old one. That gap is exactly why these
      duplicates exist; the oldest-id choice here is this function's own
      answer to it, not a convergence the writer already provides.
    - description is the longest trimmed value across every candidate, in the
      spirit of libex_core.storage.merge.longer_wins — a whitespace-only value
      measures as absent — independently of which row supplies identity above.
      Not a literal match: longer_wins trims only the incoming side, and its
      trim strips the Unicode White_Space set (BLANK_CHARS) while Python's
      .strip() strips its own whitespace definition, and this trims every
      candidate. Neither difference can make the result poorer, only
      occasionally more willing to treat a candidate as absent.
    - image is the first candidate, in id order, with a real, non-blank value
      — using the same absent test as description (a whitespace-only or empty
      image is exactly as absent as a whitespace-only description) but not
      its length ranking: two real URLs are not compared against each other,
      the earlier one simply wins. Falls back to the base row's own (possibly
      absent) value if no candidate has one, which is also what keeps a
      single-row read byte-identical to returning that row's raw field.
    - updatedAt is the max across every candidate, not the base row's own —
      description or image can come from a newer sibling, and a caller doing
      incremental sync on updatedAt must still see that the record changed.
    - genres are unioned across every candidate row, deduplicated by asin.

    What this guarantees: description, image and genres are never poorer than
    any single stored row — description is always the longest available,
    genres are always a superset (equal when every row carries the same
    genres, or there is only one row), and image is never null when any row
    holds one. What it does not guarantee: that the surfaced id/name was
    drawn from whichever row happened to supply the winning description or
    image — identity and content are chosen on separate criteria.
    """
    result = await session.execute(
        select(Author)
        .where(Author.asin == asin, Author.region == region)
        .options(selectinload(Author.genres))
        .order_by(Author.id)
    )
    authors = sorted(result.scalars().all(), key=lambda a: a.id)
    if not authors:
        return None

    if len(authors) > 1:
        logger.warning(
            "Multiple author rows found for asin/region, merging into one read",
            extra={
                "asin": asin,
                "region": region,
                "row_count": len(authors),
            },
        )

    base = authors[0]

    def _measured_length(value: str | None) -> int:
        """Trimmed length, floored to -1 for absent — same idea as
        merge.longer_wins' absent-sentinel, applied to whichever text is
        being ranked (description or image)."""
        stripped = value.strip() if value else ""
        return len(stripped) if stripped else -1

    description = base.description
    best_length = _measured_length(description)
    for a in authors[1:]:
        length = _measured_length(a.description)
        if length > best_length:
            description = a.description
            best_length = length

    image = next(
        (a.image for a in authors if _measured_length(a.image) >= 0),
        base.image,
    )

    # Defensive against a stored null slipping through despite the column
    # being NOT NULL on every write path today: the `if a.updated_at`
    # filter drops None candidates before max() ever sees them, so an
    # all-None input leaves the generator empty rather than raising
    # TypeError on a bad comparison — but max() on an empty iterable
    # raises ValueError unless a default is given. `default=None` is
    # what closes that: without it a null here would fail the whole
    # read for a record that is otherwise readable.
    updated_at = max((a.updated_at for a in authors if a.updated_at), default=None)

    genres_by_asin: dict[str, Any] = {}
    for a in authors:
        for g in (a.genres or []):
            genres_by_asin.setdefault(g.asin, g)

    genres = [
        {
            "asin": g.asin,
            "name": g.name,
            "type": g.type,
            "betterType": g.type.lower().rstrip("s"),
            "updatedAt": g.updated_at.isoformat() if g.updated_at else None,
        }
        for g in genres_by_asin.values()
    ]

    return {
        "id": base.id,
        "asin": base.asin,
        "name": base.name,
        "description": description,
        "image": image,
        "region": base.region,
        "regions": [base.region],
        "genres": genres,
        "updatedAt": updated_at.isoformat() if updated_at else None,
    }


async def get_author_book_asins(
    session: AsyncSession, author_asin: str, region: str
) -> list[str]:
    """
    Fetches only the book ASINs for an author from the DB — a single-column
    projection with no relationship loading, for callers that only need the
    ASIN list (e.g. as a merge input) and would otherwise pay for the fully
    hydrated rows from get_author_books and discard everything but
    the asin. ASINs are returned exactly as stored, uppercase or not; the
    caller is responsible for canonicalizing case at its merge site.

    An empty list means the author genuinely has no stored books. A failed
    read raises, so a caller feeding this into an authoritative write cannot
    mistake a failure for an empty catalogue.
    """
    result = await session.execute(
        select(Book.asin)
        .join(
            author_book,
            (author_book.c.book_asin == Book.asin) & (author_book.c.book_region == Book.region),
        )
        .join(Author, Author.id == author_book.c.author_id)
        .where(Author.asin == author_asin, Author.region == region)
        .distinct()
    )
    return [row[0] for row in result.fetchall()]


async def get_author_books(
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
    category: str | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> list[dict[str, Any]]:
    """Fetches all books for an author from the DB.

    Every stored book linked to the author is returned, whichever region's
    record it is; book_region narrows the list to one marketplace's records.
    """
    stmt = (
        select(Book)
        .join(
            author_book,
            (author_book.c.book_asin == Book.asin) & (author_book.c.book_region == Book.region),
        )
        .join(Author, Author.id == author_book.c.author_id)
        .where(Author.asin == author_asin, Author.region == region)
        .options(*BOOK_RELATIONS)
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
        category=category,
    )
    stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
    result = await session.execute(stmt)
    books = result.scalars().all()
    # This read has no LIMIT — the result is the author's entire stored
    # catalogue, which for a prolific author is thousands of rows. Series
    # positions are fetched for all of them in one statement rather than
    # one per book; the ordering apply_sort established above is untouched.
    positions_by_book = await series_positions_by_book(session, books)
    results = []
    for book in books:
        positions = positions_by_book.get((book.asin, book.region), {})
        results.append(book_to_dict(book, positions))
    return results


async def search_narrators(
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
    stmt = select(Narrator).where(ILike(Narrator.name, f"%{name}%"))
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
    return [narrator_to_dict(n) for n in narrators]


async def get_narrator_books(
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
    category: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Fetches all books by a narrator name from the local DB."""
    stmt = (
        select(Book)
        .join(
            book_narrator,
            (Book.asin == book_narrator.c.book_asin) & (Book.region == book_narrator.c.book_region),
        )
        .where(book_narrator.c.narrator_name == name)
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
    if region is None:
        stmt = only_first_stored(stmt)
    stmt = stmt.options(*BOOK_RELATIONS)
    stmt = apply_sort(stmt, sort, order, BOOK_SORT_FIELDS)
    stmt = stmt.limit(limit).offset((page - 1) * limit)
    result = await session.execute(stmt)
    books = result.scalars().all()
    return await hydrate_books(session, books)
