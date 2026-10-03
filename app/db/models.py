"""
Database models for Libex.

The catalog tables (books, authors, series, narrators, genres, tracks and their
pivots) live in `libex_core.storage.models`, shared with the embedded library,
and are re-exported here under the same names. The tables below are the hosted
app's own: the response cache and the catalog genre tree.
"""

# Standard library
from datetime import datetime, timezone

# Third party
from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Local
from app.db.base import Base
from libex_core.storage.models import (
    REGION_ENUM,
    Author,
    Book,
    Genre,
    Narrator,
    Series,
    Track,
    WalkResult,
    author_book,
    author_genre,
    book_genre,
    book_narrator,
    book_series,
    series_author,
)

__all__ = [
    "REGION_ENUM",
    "Author",
    "Book",
    "Cache",
    "CatalogGenre",
    "Genre",
    "Narrator",
    "Series",
    "Track",
    "WalkResult",
    "author_book",
    "author_genre",
    "book_genre",
    "book_narrator",
    "book_series",
    "series_author",
]


# ============================================================
# CACHE
# ============================================================

class Cache(Base):
    """
    Cache table for Audible API responses and Libex-internal derived values.
    Region-varying data keys as {type}:{region}:{identifier} — region is not
    optional there, since the same identifier can hold a different value per
    region:
        book:us:B08G9PRS1K
        author:uk:B000APF21M
        series:us:B08G9PRS1K
        search:us:dune+frank+herbert

    A value with nothing that varies by region or identifier keys on its
    bare type name instead. The global DB stats snapshot is the example —
    but stats also has a region-scoped form, keyed like a region-varying
    value above it rather than like its own bare key:
        db_stats
        db_stats:us
    """

    __tablename__ = "cache"

    key: Mapped[str] = mapped_column(String(500), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Cache key={self.key} expires={self.expires_at}>"


# ============================================================
# CATALOG GENRES
# ============================================================

class CatalogGenre(Base):
    __tablename__ = "catalog_genres"

    region: Mapped[str] = mapped_column(String(2), primary_key=True)
    genre_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    # Parent category id, or "" for a top-level parent. Part of the PK so a leaf
    # that appears under two parents is stored once per parent.
    parent_id: Mapped[str] = mapped_column(String(20), primary_key=True, default="")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    last_checked: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<CatalogGenre region={self.region} genre_id={self.genre_id} parent_id={self.parent_id} name={self.name}>"
