"""
The relational schema for Libex's stored catalog.

Backend-neutral: the same table definitions create on Postgres, where they are
the hosted schema exactly, and on SQLite. Where a column type needs a different
spelling per backend it is written once with a variant (see `types`), so the
Postgres DDL is untouched. The hosted app's alembic chain owns the Postgres
schema; this module must keep describing it byte for byte.
"""

# Standard library
from datetime import datetime, timezone

# Third party
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Double,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Local
from libex_core.storage.base import Base
from libex_core.storage.types import JSONDocument, UTCDateTime


REGION_ENUM = Enum(
    "us", "ca", "uk", "au", "fr", "de", "jp", "it", "in", "es", "br",
    name="region_enum",
)


# ============================================================
# BOOKS
# ============================================================

class Book(Base):
    __tablename__ = "books"

    # A book is identified by (asin, region): the same ASIN can be a different
    # marketplace's record, and each region keeps its own row.
    asin: Mapped[str] = mapped_column(String(12), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str] = mapped_column(REGION_ENUM, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    publisher: Mapped[str | None] = mapped_column(Text, nullable=True)
    copyright: Mapped[str | None] = mapped_column(Text, nullable=True)
    isbn: Mapped[str | None] = mapped_column(String(16), nullable=True)
    language: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rating: Mapped[float | None] = mapped_column(Double, nullable=True)
    release_date: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
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
    is_vvab: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    plans: Mapped[list | None] = mapped_column(JSONDocument, nullable=True)
    num_ratings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_reviews: Mapped[int | None] = mapped_column(Integer, nullable=True)
    publication_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_datetime: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    extended_product_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Text rather than a Postgres ENUM or a varchar(n), and the difference is
    # what happens on a value nobody anticipated. An enum needs ALTER TYPE ADD
    # VALUE before it will accept one, so until that migration ships the whole
    # book write fails; varchar(n) raises 22001 and fails the row the same way.
    # Text and varchar(n) store identically here, so a cap would buy nothing
    # but the failure mode. Three values seen so far -- AVAILABLE,
    # AVAILABLE_FOR_PREORDER, NOT_AVAILABLE_FOR_PURCHASE -- and Audible is
    # free to invent a fourth without telling anyone.
    product_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Three states, and all three are meaningful. NULL means no response has
    # written this column since the migration added it, '{}' means every
    # response that has written it sent nothing beyond the fields reproduced
    # above it, and a populated object holds Audible's own keys verbatim,
    # under Audible's own names. The first of those doubles as the progress
    # signal an operator reads as the corpus fills in over a refresh pass --
    # nothing selects on it automatically -- which is why the column has no
    # default: any default at all, DEFAULT '{}'::jsonb included, would stamp
    # every existing row as "asked and answered empty" before anything had
    # asked.
    #
    # A populated blob is an accumulation rather than one response's
    # remainder. Each write unions its keys in, so the blob spans every
    # response that has written the row. Keys are never removed either. So a
    # key sitting here was not necessarily sent by the most recent response,
    # and the blob is not a snapshot of what Audible would say now. libex_core.storage.merge.extras_union carries why it is merged that way,
    # and the limits of that merge.
    #
    # The blob is large enough to be toasted, so reading it is a detoast per
    # row -- about 61 microseconds, which is the intrinsic cost of carrying
    # the field rather than anything a query can avoid. It is invisible on a
    # single book and decisive across the table: an aggregate over this
    # column at 1.8M rows measures around 110 seconds, well past the 30s
    # statement_timeout every request connection carries. Anything that has
    # to walk this column walks it in cursor-sized chunks.
    audible_extras: Mapped[dict | None] = mapped_column(JSONDocument, nullable=True)
    # What has been left out of audible_extras, in Libex's own vocabulary
    # rather than Audible's. Kept out of the blob itself so it cannot collide
    # with an upstream key of the same name.
    #
    # One entry per kind of withholding, each holding what the most recent
    # response to hit that kind left behind -- unless that response's account
    # was already contained in the stored one, which stands instead, so a
    # fuller entry is not traded for a thinner later one. Unioned across every
    # response that has withheld anything from this row. Never one response's
    # account, and never cleared by a response that withheld nothing. It also merges
    # on its own terms rather than in step with the blob beside it: a
    # response whose extras were dropped whole records the drop here while
    # the stored blob stands untouched, so a record does not imply the blob
    # changed on the same write. Nor is it an inventory of what is missing
    # from the blob as it stands -- an entry outlives a later response
    # supplying the key it names. Read it as evidence that something was
    # dropped at some point. The extras_withheld merge in
    # libex_core.storage.write.statements sets out why the column has that
    # shape.
    extras_withheld: Mapped[dict | None] = mapped_column(JSONDocument, nullable=True)
    chapters_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # True on the one row of an ASIN that a reader asked for no region answers
    # with: the first stored. Set when the row is inserted and never changed
    # after, so every ASIN stored before regions were part of the key (all of
    # them, one row each) is primary, and only a row inserted for an ASIN that
    # another region already holds is not. The writer serializes the insert
    # per ASIN, which is what keeps two regions from both claiming it.
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    # When Audible last confirmed this record, and its chapter listing (a
    # stored listing or a legitimate empty answer alike). NULL means never
    # confirmed.
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    chapters_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
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
        # Covering index for region-scoped stats: booksWithChapters joins
        # tracks to books on asin under a region filter, and a bare
        # books(region) still forces a heap fetch for that join. Leading
        # with region and including asin makes both the region count and
        # the join index-only -- but only while the visibility map marks
        # the scanned pages all-visible. Postgres will not run an
        # index-only scan it cannot trust, so with the map stale it stops
        # choosing this index and reads the whole heap instead.
        #
        # That was this table's steady state, not its cold start, which is
        # the part worth knowing here. Only VACUUM sets visibility-map
        # bits -- ANALYZE never does, at any frequency -- and books takes
        # both insert and update traffic: the writer upserts with
        # on_conflict_do_update, so every re-seen ASIN leaves a dead
        # tuple, and the chapters backfill stamps chapters_checked_at one
        # book at a time. Neither autovacuum rule came near its default
        # threshold between passes.
        #
        # That those defaults are why the map went stale is an inference,
        # not a measurement: the statistics that would have evidenced it
        # were discarded on a postmaster restart. What was measured is the
        # cost, on a 1.8M-row rebuild of this table with the production
        # 30s statement_timeout -- one count(*) took 31.9s and was killed
        # by the timeout with the map stale, and 0.8s with it current.
        # c7a4e9f13b02 lowers autovacuum's vacuum scale factors here, and
        # on tracks, to keep the map current; it is what makes this index
        # worth having.
        Index("books_region_asin_index", "region", "asin"),
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
    last_seeded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # When Audible last confirmed this record. NULL means never confirmed.
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
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

    series: Mapped[list["Series"]] = relationship(
        "Series", secondary="series_author", back_populates="authors"
    )

    __table_args__ = (
        UniqueConstraint("asin", "region", "name", name="authors_asin_region_name_unique"),
        Index("authors_asin_region_name_index", "asin", "region", "name"),
        Index("authors_region_name_index", "region", "name"),
        # Partial unique index: at most one null-asin row per (name, region).
        # Postgres treats NULLs as distinct in a plain unique constraint, so
        # this can't be a UniqueConstraint — it has to be a filtered index.
        # SQLite needs its own sqlite_where: without it the index compiles as a
        # plain unique over (name, region) and widens the rule to every row.
        Index(
            "uq_authors_name_region_null_asin",
            "name",
            "region",
            unique=True,
            postgresql_where=text("asin IS NULL"),
            sqlite_where=text("asin IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Author id={self.id} name={self.name}>"


# ============================================================
# SERIES
# ============================================================

class Series(Base):
    __tablename__ = "series"

    # Identified by (asin, region), like a book: a series ASIN seen in two
    # marketplaces is two records.
    asin: Mapped[str] = mapped_column(String(12), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str] = mapped_column(REGION_ENUM, primary_key=True)
    fetched_description: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seeded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # When Audible last confirmed this record. NULL means never confirmed.
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # The row an ASIN is answered with when no region is asked for; see
    # Book.is_primary.
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    # The series product's keys beyond asin, title and publisher_summary, and
    # the record of anything left out of them. Same meaning, NULL semantics and
    # merge as the books columns of the same names; libex_core.storage.merge.extras_union
    # carries why. NULL means no profile fetch has written this row since the
    # columns landed.
    audible_extras: Mapped[dict | None] = mapped_column(JSONDocument, nullable=True)
    extras_withheld: Mapped[dict | None] = mapped_column(JSONDocument, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    books: Mapped[list["Book"]] = relationship(
        "Book", secondary="book_series", back_populates="series"
    )

    authors: Mapped[list["Author"]] = relationship(
        "Author", secondary="series_author", back_populates="series"
    )

    __table_args__ = (
        # Region-scoped series count. The primary key leads with asin, so a
        # region-only filter needs its own index.
        Index("series_region_index", "region"),
    )

    def __repr__(self) -> str:
        return f"<Series asin={self.asin} title={self.title}>"


# ============================================================
# NARRATORS
# ============================================================

class Narrator(Base):
    __tablename__ = "narrators"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    wikipedia_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    languages: Mapped[dict | None] = mapped_column(JSONDocument, nullable=True)
    accents: Mapped[dict | None] = mapped_column(JSONDocument, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    genres_narrated: Mapped[list | None] = mapped_column(JSONDocument, nullable=True)
    audiobooks_produced: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cultural_heritage: Mapped[str | None] = mapped_column(Text, nullable=True)
    publishers: Mapped[list | None] = mapped_column(JSONDocument, nullable=True)
    social_links: Mapped[dict | None] = mapped_column(JSONDocument, nullable=True)
    audio_samples: Mapped[list | None] = mapped_column(JSONDocument, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    fetched_description: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seeded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
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
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
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

    # tracks_pkey on (asin, region) is what lets the booksWithChapters join
    # run index-only, and as on books that holds only while the visibility map
    # marks the scanned pages all-visible. c7a4e9f13b02 lowers this table's
    # autovacuum vacuum scale factors for that reason; the reasoning lives
    # on Book.__table_args__. The pair is also the foreign key to the book:
    # a chapter listing belongs to one marketplace's record.
    asin: Mapped[str] = mapped_column(String(12), primary_key=True)
    region: Mapped[str] = mapped_column(REGION_ENUM, primary_key=True)
    chapters: Mapped[dict] = mapped_column(JSONDocument, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    book: Mapped["Book"] = relationship("Book", back_populates="track")

    __table_args__ = (
        ForeignKeyConstraint(
            ["asin", "region"], ["books.asin", "books.region"], ondelete="CASCADE"
        ),
    )

    def __repr__(self) -> str:
        return f"<Track asin={self.asin}>"

# ============================================================
# PIVOT TABLES
# ============================================================

author_book = Table(
    "author_book",
    Base.metadata,
    Column("author_id", Integer, ForeignKey("authors.id", ondelete="CASCADE"), nullable=False),
    Column("book_asin", String(12), nullable=False),
    Column("book_region", REGION_ENUM, nullable=False),
    ForeignKeyConstraint(
        ["book_asin", "book_region"], ["books.asin", "books.region"], ondelete="CASCADE"
    ),
    Index("book_author_index", "book_asin", "author_id"),
    UniqueConstraint("author_id", "book_asin", "book_region", name="uq_author_book"),
)

book_narrator = Table(
    "book_narrator",
    Base.metadata,
    Column("narrator_name", Text, ForeignKey("narrators.name", ondelete="CASCADE"), nullable=False),
    Column("book_asin", String(12), nullable=False),
    Column("book_region", REGION_ENUM, nullable=False),
    ForeignKeyConstraint(
        ["book_asin", "book_region"], ["books.asin", "books.region"], ondelete="CASCADE"
    ),
    UniqueConstraint("book_asin", "book_region", "narrator_name", name="uq_book_narrator"),
)

book_series = Table(
    "book_series",
    Base.metadata,
    Column("book_asin", String(12), nullable=False),
    Column("book_region", REGION_ENUM, nullable=False),
    Column("series_asin", String(12), nullable=False),
    Column("series_region", REGION_ENUM, nullable=False),
    Column("position", String(100), nullable=True),
    ForeignKeyConstraint(
        ["book_asin", "book_region"], ["books.asin", "books.region"], ondelete="CASCADE"
    ),
    ForeignKeyConstraint(
        ["series_asin", "series_region"], ["series.asin", "series.region"], ondelete="CASCADE"
    ),
    UniqueConstraint(
        "book_asin", "book_region", "series_asin", "series_region", name="uq_book_series"
    ),
)

book_genre = Table(
    "book_genre",
    Base.metadata,
    Column("book_asin", String(12), nullable=False),
    Column("book_region", REGION_ENUM, nullable=False),
    Column("genre_asin", String(12), ForeignKey("genres.asin", ondelete="CASCADE"), nullable=False),
    ForeignKeyConstraint(
        ["book_asin", "book_region"], ["books.asin", "books.region"], ondelete="CASCADE"
    ),
    # Carries book_region because the genre and category filters read the
    # (asin, region) of every linked book: without it each probe has to visit
    # the table for the region, and past a few thousand links the planner
    # prefers reading the whole of it.
    Index("genre_book_index", "genre_asin", "book_asin", "book_region"),
    UniqueConstraint("book_asin", "book_region", "genre_asin", name="uq_book_genre"),
)

author_genre = Table(
    "author_genre",
    Base.metadata,
    Column("author_id", Integer, ForeignKey("authors.id", ondelete="CASCADE"), nullable=False),
    Column("genre_asin", String(12), ForeignKey("genres.asin", ondelete="CASCADE"), nullable=False),
    Index("author_genre_index", "genre_asin", "author_id"),
    Index("genre_author_index", "author_id", "genre_asin"),
    UniqueConstraint("author_id", "genre_asin", name="uq_author_genre"),
)

series_author = Table(
    "series_author",
    Base.metadata,
    Column("series_asin", String(12), nullable=False),
    Column("series_region", REGION_ENUM, nullable=False),
    Column("author_id", Integer, ForeignKey("authors.id", ondelete="CASCADE"), nullable=False),
    ForeignKeyConstraint(
        ["series_asin", "series_region"], ["series.asin", "series.region"], ondelete="CASCADE"
    ),
    UniqueConstraint("series_asin", "series_region", "author_id", name="uq_series_author"),
    Index("author_series_index", "author_id", "series_asin"),
)


# ============================================================
# WALK RESULTS
# ============================================================

class WalkResult(Base):
    """What one author or series walk returned, kept apart from the catalog.

    An author's catalog is per marketplace and the author ASIN is not, so the
    key carries the region as well as the ASIN. There is no foreign key: a
    walk keys by the author's or series' own ASIN, which may name a record
    this store does not hold.
    """

    __tablename__ = "walk_results"

    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    asin: Mapped[str] = mapped_column(String(12), primary_key=True)
    region: Mapped[str] = mapped_column(REGION_ENUM, primary_key=True)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    incomplete_reasons: Mapped[list] = mapped_column(JSONDocument, nullable=False)
    book_asins: Mapped[list] = mapped_column(JSONDocument, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('author_books', 'series_books')", name="ck_walk_results_kind"
        ),
    )

    def __repr__(self) -> str:
        return f"<WalkResult kind={self.kind} asin={self.asin} region={self.region}>"


# The tables this module defines, which are the ones the package's own
# migrations create. The hosted app registers more on the same metadata, so
# the metadata alone cannot say which tables are the core's.
CORE_TABLES = frozenset({
    "books", "authors", "series", "narrators", "genres", "tracks",
    "author_book", "book_narrator", "book_series", "book_genre",
    "author_genre", "series_author", "walk_results",
})
