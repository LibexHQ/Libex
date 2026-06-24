"""
Shared filtering helpers for DB-backed list endpoints.

Mirrors sorting.py: small, reusable helpers that take a select statement and
return it with extra WHERE clauses applied. Kept separate from the readers so
the same filter logic can be reused across every list endpoint.

Relationship filters (author name, series name, genre) use subqueries on
Book.asin rather than joins, so they compose with each other, with sorting,
and with pagination without producing duplicate rows or conflicting joins on
endpoints that already join those tables.
"""

# Third party
from sqlalchemy import Select, select

# Database
from app.db.models import (
    Author,
    Book,
    Genre,
    Narrator,
    Series,
    author_book,
    book_genre,
    book_series,
)


def apply_genre_filter(stmt: Select, genre: str | None) -> Select:
    """
    Filters a Book query to books tagged with a matching genre or tag.

    Matches both Genres and Tags by name (partial, case-insensitive), so
    `genre="fantasy"` catches "Fantasy", "Science Fiction & Fantasy", etc.
    Returns the statement unchanged when genre is None.
    """
    if not genre:
        return stmt

    matching_books = (
        select(book_genre.c.book_asin)
        .join(Genre, Genre.asin == book_genre.c.genre_asin)
        .where(Genre.name.ilike(f"%{genre}%"))
    )
    return stmt.where(Book.asin.in_(matching_books))


def apply_category_filter(stmt: Select, category: str | None) -> Select:
    """
    Filters a Book query to books tagged with a category by its exact id.
    Matches Genre.asin (the Audible category id from /categories), so
    `category="18580628011"` returns only books in that exact category — unlike
    the genre filter, which matches names broadly. Returns the statement
    unchanged when category is None.
    """
    if not category:
        return stmt
    matching_books = (
        select(book_genre.c.book_asin)
        .join(Genre, Genre.asin == book_genre.c.genre_asin)
        .where(Genre.asin == category)
    )
    return stmt.where(Book.asin.in_(matching_books))


def apply_book_filters(
    stmt: Select,
    *,
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
) -> Select:
    """
    Applies the full set of Book filters to a select statement.

    Every filter is optional; a None value is skipped. Relationship filters
    (author_name, series_name, genre) use Book.asin subqueries so they can be
    combined freely and used on endpoints that already join those tables.
    """
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
        matching = (
            select(author_book.c.book_asin)
            .join(Author, Author.id == author_book.c.author_id)
            .where(Author.name.ilike(f"%{author_name}%"))
        )
        stmt = stmt.where(Book.asin.in_(matching))
    if series_name is not None:
        matching = (
            select(book_series.c.book_asin)
            .join(Series, Series.asin == book_series.c.series_asin)
            .where(Series.title.ilike(f"%{series_name}%"))
        )
        stmt = stmt.where(Book.asin.in_(matching))
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
    if is_vvab is not None:
        stmt = stmt.where(Book.is_vvab == is_vvab)
    if plan_name is not None:
        stmt = stmt.where(Book.plans.contains([plan_name]))

    stmt = apply_genre_filter(stmt, genre)
    stmt = apply_category_filter(stmt, category)
    return stmt


def apply_narrator_filters(
    stmt: Select,
    *,
    gender: str | None = None,
    language: str | None = None,
    audiobooks_produced: str | None = None,
    source: str | None = None,
    cultural_heritage: str | None = None,
) -> Select:
    """
    Applies Narrator-specific filters to a select statement.

    - gender / source / cultural_heritage: partial, case-insensitive match.
    - language: matches narrators whose languages JSONB has the given key
      (stored as {"English": 5, ...}).
    - audiobooks_produced: exact match on the categorical bucket.
    """
    if gender is not None:
        stmt = stmt.where(Narrator.gender.ilike(f"%{gender}%"))
    if source is not None:
        stmt = stmt.where(Narrator.source.ilike(f"%{source}%"))
    if cultural_heritage is not None:
        stmt = stmt.where(Narrator.cultural_heritage.ilike(f"%{cultural_heritage}%"))
    if language is not None:
        stmt = stmt.where(Narrator.languages.has_key(language))  # noqa: W601
    if audiobooks_produced is not None:
        stmt = stmt.where(Narrator.audiobooks_produced == audiobooks_produced)
    return stmt