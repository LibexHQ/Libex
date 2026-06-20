"""
Shared filtering helpers for DB-backed list endpoints.

Mirrors sorting.py: small, reusable helpers that take a select statement and
return it with extra WHERE clauses applied. Kept separate from the readers so
the same filter logic can be reused across every list endpoint.
"""

# Third party
from sqlalchemy import Select, select

# Database
from app.db.models import Book, Genre, book_genre


def apply_genre_filter(stmt: Select, genre: str | None) -> Select:
    """
    Filters a Book query to books tagged with a matching genre or tag.

    Matches both Genres and Tags by name (partial, case-insensitive), so
    `genre="fantasy"` catches "Fantasy", "Science Fiction & Fantasy", etc.
    Returns the statement unchanged when genre is None.

    Uses a subquery on Book.asin rather than a join, so it composes with other
    filters and pagination without producing duplicate rows.
    """
    if not genre:
        return stmt

    matching_books = (
        select(book_genre.c.book_asin)
        .join(Genre, Genre.asin == book_genre.c.genre_asin)
        .where(Genre.name.ilike(f"%{genre}%"))
    )
    return stmt.where(Book.asin.in_(matching_books))