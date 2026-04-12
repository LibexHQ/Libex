"""
Database models for Libex.
"""

# Standard library
from datetime import datetime, timezone

# Third party
from sqlalchemy import (
    Boolean,
    DateTime,
    Double,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Local
from app.db.base import Base


REGION_ENUM = Enum(
    "us", "ca", "uk", "au", "fr", "de", "jp", "it", "in", "es", "br",
    name="region_enum",
)


# ============================================================
# CACHE
# ============================================================

class Cache(Base):
    """
    Cache table for Audible API responses.
    Key format: {type}:{region}:{identifier}
    Examples:
        book:us:B08G9PRS1K
        author:uk:B000APF21M
        series:us:B08G9PRS1K
        search:us:dune+frank+herbert
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
# BOOKS
# ============================================================

class Book(Base):
    __tablename__ = "books"

    asin: Mapped[str] = mapped_column(String(12), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str] = mapped_column(REGION_ENUM, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    publisher: Mapped[str | None] = mapped_column(Text, nullable=True)
    copyright: Mapped[str | None] = mapped_column(Text, nullable=True)
    isbn: Mapped[str | None] = mapped_column(String(16), nullable=True)
    language: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rating: Mapped[float | None] = mapped_column(Double, nullable=True)
    release_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    length_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    explicit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    whisper_sync: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_pdf: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)
    book_format: Mapped[str | None] = mapped_column(String(50), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content_delivery_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    episode_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    episode_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sku_group: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_listenable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_buyable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    authors: Mapped[list["Author"]] = relationship(
        "Author", secondary="author_book", back_populates="books"
    )
    narrators: Mapped[list["Narrator"]] = relationship(
        "Narrator", secondary="book_narrator", back_populates="books"
    )
    genres: Mapped[list["Genre"]] = relationship(
        "Genre", secondary="book_genre", back_populates="books"
    )
    series: Mapped[list["Series"]] = relationship(
        "Series", secondary="book_series", back_populates="books"
    )
    track: Mapped["Track | None"] = relationship("Track", back_populates="book", uselist=False)

    __table_args__ = (
        Index("books_asin_index", "asin"),
    )

    def __repr__(self) -> str:
        return f"<Book asin={self.asin} title={self.title}>"


# ============================================================
# AUTHORS
# ============================================================

class Author(Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(REGION_ENUM, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_description: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    books: Mapped[list["Book"]] = relationship(
        "Book", secondary="author_book", back_populates="authors"
    )
    genres: Mapped[list["Genre"]] = relationship(
        "Genre", secondary="author_genre", back_populates="authors"
    )

    __table_args__ = (
        UniqueConstraint("asin", "region", "name", name="authors_asin_region_name_unique"),
        Index("authors_asin_region_name_index", "asin", "region", "name"),
        Index("authors_region_name_index", "region", "name"),
    )

    def __repr__(self) -> str:
        return f"<Author id={self.id} name={self.name}>"


# ============================================================
# SERIES
# ============================================================

class Series(Base):
    __tablename__ = "series"

    asin: Mapped[str] = mapped_column(String(12), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(REGION_ENUM, nullable=True)
    fetched_description: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    books: Mapped[list["Book"]] = relationship(
        "Book", secondary="book_series", back_populates="series"
    )

    __table_args__ = (
        Index("series_asin_index", "asin"),
    )

    def __repr__(self) -> str:
        return f"<Series asin={self.asin} title={self.title}>"


# ============================================================
# NARRATORS
# ============================================================

class Narrator(Base):
    __tablename__ = "narrators"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    books: Mapped[list["Book"]] = relationship(
        "Book", secondary="book_narrator", back_populates="narrators"
    )

    def __repr__(self) -> str:
        return f"<Narrator name={self.name}>"


# ============================================================
# GENRES
# ============================================================

class Genre(Base):
    __tablename__ = "genres"

    asin: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Enum("Genres", "Tags", name="genre_type_enum"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    books: Mapped[list["Book"]] = relationship(
        "Book", secondary="book_genre", back_populates="genres"
    )
    authors: Mapped[list["Author"]] = relationship(
        "Author", secondary="author_genre", back_populates="genres"
    )

    def __repr__(self) -> str:
        return f"<Genre asin={self.asin} name={self.name}>"


# ============================================================
# TRACKS (CHAPTERS)
# ============================================================

class Track(Base):
    __tablename__ = "tracks"

    asin: Mapped[str] = mapped_column(
        String(12), ForeignKey("books.asin", ondelete="CASCADE"), primary_key=True
    )
    chapters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    book: Mapped["Book"] = relationship("Book", back_populates="track")

    def __repr__(self) -> str:
        return f"<Track asin={self.asin}>"


# ============================================================
# PIVOT TABLES
# ============================================================

from sqlalchemy import Table, Column  # noqa: E402

author_book = Table(
    "author_book",
    Base.metadata,
    Column("author_id", Integer, ForeignKey("authors.id", ondelete="CASCADE"), nullable=False),
    Column("book_asin", String(12), ForeignKey("books.asin", ondelete="CASCADE"), nullable=False),
    Index("book_author_index", "book_asin", "author_id"),
)

book_narrator = Table(
    "book_narrator",
    Base.metadata,
    Column("narrator_name", Text, ForeignKey("narrators.name", ondelete="CASCADE"), nullable=False),
    Column("book_asin", String(12), ForeignKey("books.asin", ondelete="CASCADE"), nullable=False),
    Index("book_narrator_index", "book_asin", "narrator_name"),
)

book_series = Table(
    "book_series",
    Base.metadata,
    Column("book_asin", String(12), ForeignKey("books.asin", ondelete="CASCADE"), nullable=False),
    Column("series_asin", String(12), ForeignKey("series.asin", ondelete="CASCADE"), nullable=False),
    Column("position", String(20), nullable=True),
    Index("book_series_index", "book_asin", "series_asin"),
)

book_genre = Table(
    "book_genre",
    Base.metadata,
    Column("book_asin", String(12), ForeignKey("books.asin", ondelete="CASCADE"), nullable=False),
    Column("genre_asin", String(12), ForeignKey("genres.asin", ondelete="CASCADE"), nullable=False),
    Index("book_genre_index", "book_asin", "genre_asin"),
    Index("genre_book_index", "genre_asin", "book_asin"),
)

author_genre = Table(
    "author_genre",
    Base.metadata,
    Column("author_id", Integer, ForeignKey("authors.id", ondelete="CASCADE"), nullable=False),
    Column("genre_asin", String(12), ForeignKey("genres.asin", ondelete="CASCADE"), nullable=False),
    Index("author_genre_index", "genre_asin", "author_id"),
    Index("genre_author_index", "author_id", "genre_asin"),
)