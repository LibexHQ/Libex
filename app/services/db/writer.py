"""
Database writer service.
Persists Audible API responses to relational tables.

Called after every successful Audible fetch to keep the DB in sync.
Writes are upserts — existing non-null values are never overwritten with null.
The DB is used as a fallback when Audible is unavailable.
"""

# Standard library
import asyncio
from datetime import datetime, timedelta, timezone

# Third party
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from asyncpg.exceptions import UniqueViolationError as AsyncpgUniqueViolation
from sqlalchemy.dialects.postgresql import insert, JSONB
from sqlalchemy import select, func, update, case, cast, delete, tuple_

# Database
from app.db.models import (
    Book,
    Author,
    Cache,
    Genre,
    Narrator,
    Series,
    Track,
    CatalogGenre,
    author_book,
    author_genre,
    book_genre,
    book_narrator,
    book_series,
    series_author,
)
from app.db.session import engine

# Core
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger()
settings = get_settings()

_BackgroundSession = async_sessionmaker(engine, expire_on_commit=False)

# Limits concurrent background writes so the seeder doesn't starve the
# API's DB connection pool. At most 2 background persist tasks write at once.
_bg_write_semaphore = asyncio.Semaphore(2)

# Books written per transaction by the batched background persist. Matched to
# the size of the 50-ASIN chunk the books service already fetches Audible in.
# Small enough that the row locks a chunk holds are released promptly for the
# seeder and for concurrent requests touching the same authors, genres and
# narrators, and that replaying a chunk after a lost transaction is cheap;
# large enough that persisting a prolific author's 1000-book catalog costs 20
# commits instead of 2000.
#
# Two things bound it from above, and neither shows in the tradeoff above.
# Subtransactions: upsert_author opens a SAVEPOINT for each asin-less author it
# inserts and each null-asin row it upgrades, so one chunk's transaction holds
# up to _PERSIST_CHUNK_SIZE x new-authors-per-book of them. Two such authors a
# book fill Postgres's 64-entry per-backend subxid cache at 32 books and pass it
# at 33, after which snapshots taken while the transaction is open carry the
# suboverflowed flag and other backends resolve subtransaction visibility
# through the pg_subtrans SLRU rather than the cache. Bind parameters:
# _cache_set_many puts four per entry into one INSERT against asyncpg's 32,767,
# so a chunk of 8192 raises before the commit — nothing is lost, since
# _persist_book_chunk replays the chunk book by book, but it discards the batch
# it just wrote and lands on the per-book path the batching exists to avoid.
# The two sit differently at 50: the INSERT binds 200 parameters and is two
# orders of magnitude clear, while a cold chunk already passes the subxid cache
# and pays the SLRU lookups — a throughput cost, not a limit, and only on a cold
# catalog, since re-writing the same books finds the authors stored and opens no
# savepoints. Only the bind ceiling is a hard stop on raising the constant.
_PERSIST_CHUNK_SIZE = 50


# ============================================================
# HELPERS
# ============================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_release_date_for_db(iso_str: str | None) -> datetime | None:
    """Converts an ISO 8601 string back to a datetime for DB storage."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None


def _coalesce(new_value, existing_col):
    """Returns new_value if not null, otherwise keeps the existing column value."""
    return func.coalesce(new_value, existing_col)


def _longer_wins(new_value, existing_col):
    """
    Keeps whichever value carries more text, so a later, richer Audible
    response replaces a thinner stored one and never the reverse.

    Both lengths are floored to a sentinel rather than compared directly,
    because length(NULL) is NULL and a comparison against NULL is NULL, not
    false. A bare length(new) > length(existing) therefore falls to the ELSE
    branch whenever the stored value is NULL, and pins that NULL permanently:
    no incoming description, however long, could fill a column that was first
    written empty.

    An incoming value that is empty or entirely whitespace measures as absent,
    so it cannot displace a stored NULL. It carries no more information than
    NULL does, and writing it would make a column Audible has never answered
    indistinguishable from one it answered blank. Only the measurement is
    trimmed — the value written is the value received, verbatim.
    """
    absent = -1
    new_length = func.coalesce(func.length(func.nullif(func.btrim(new_value), "")), absent)
    existing_length = func.coalesce(func.length(existing_col), absent)
    return case(
        (new_length > existing_length, new_value),
        else_=existing_col,
    )


def _to_bool(value, default: bool = False) -> bool:
    """Converts string or bool to bool safely."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == 'true'
    return default


# ============================================================
# GENRE WRITER
# ============================================================

async def upsert_genre(session: AsyncSession, genre: dict) -> str | None:
    """Upserts a single genre. Returns asin if successful."""
    asin = genre.get("asin")
    name = genre.get("name")
    genre_type = genre.get("type", "Tags")

    if not asin or not name:
        return None

    stmt = insert(Genre).values(
        asin=asin,
        name=name,
        type=genre_type,
        created_at=_now(),
        updated_at=_now(),
    ).on_conflict_do_update(
        index_elements=["asin"],
        set_={
            "name": _coalesce(name, Genre.name),
            "type": Genre.type,
            "updated_at": _now(),
        },
    )
    await session.execute(stmt)
    return asin


# ============================================================
# NARRATOR WRITER
# ============================================================

async def upsert_narrator(session: AsyncSession, narrator: dict) -> str | None:
    """Upserts a single narrator. Returns name if successful."""
    name = narrator.get("name", "").strip()
    if not name:
        return None

    stmt = insert(Narrator).values(
        name=name,
        created_at=_now(),
        updated_at=_now(),
    ).on_conflict_do_nothing()
    await session.execute(stmt)
    return name


# ============================================================
# SERIES WRITER
# ============================================================

async def upsert_series(session: AsyncSession, series: dict) -> str | None:
    """Upserts a series record. Returns asin if successful."""
    asin = series.get("asin")
    name = series.get("name") or series.get("title")
    if not asin or not name:
        return None

    description = series.get("description")

    stmt = insert(Series).values(
        asin=asin,
        title=name,
        description=description,
        region=series.get("region"),
        fetched_description=bool(description),
        created_at=_now(),
        updated_at=_now(),
    ).on_conflict_do_update(
        index_elements=["asin"],
        set_={
            "title": _coalesce(name, Series.title),
            "description": _longer_wins(description, Series.description),
            "region": Series.region,
            "fetched_description": Series.fetched_description | bool(description),
            "updated_at": _now(),
        },
    )
    await session.execute(stmt)
    return asin


# ============================================================
# AUTHOR WRITER
# ============================================================

async def upsert_author(session: AsyncSession, author: dict) -> int | None:
    """
    Upserts an author record. Returns the author's DB id if successful.

    When asin is null: match on (name, region, asin IS NULL) to avoid duplicates.
    When asin is not null:
      1. Check if a fully-upgraded row (asin, region, name) already exists —
         return its id immediately if so. This short-circuits concurrent requests
         that would otherwise race to upgrade the same null-asin row.
      2. If not, look for a null-asin row to upgrade in place, since PostgreSQL
         does not treat NULL = NULL in unique indexes.
      3. Fall through to standard INSERT ... ON CONFLICT if neither exists.
    """
    a_asin = author.get("asin")
    a_name = author.get("name", "").strip()
    a_region = author.get("region")

    if not a_name or not a_region:
        return None

    if a_asin:
        # Step 1: check if the fully-upgraded row already exists.
        # This is the common case after the first request upgrades the row.
        existing_result = await session.execute(
            select(Author.id).where(
                Author.asin == a_asin,
                Author.region == a_region,
                Author.name == a_name,
            )
        )
        existing_id = existing_result.scalar_one_or_none()
        if existing_id:
            return existing_id

        # Step 2: look for a null-asin row to upgrade. The unique constraint
        # doesn't cover null asins (Postgres treats NULLs as distinct), so a
        # concurrent-write race can leave more than one null-asin row for the
        # same (name, region) — order by id and take the oldest so every writer
        # converges on the same row instead of raising MultipleResultsFound.
        null_result = await session.execute(
            select(Author.id).where(
                Author.name == a_name,
                Author.region == a_region,
                Author.asin.is_(None),
            )
            .order_by(Author.id)
            .limit(1)
        )
        null_id = null_result.scalar_one_or_none()

        if null_id:
            # The UPDATE runs inside a SAVEPOINT so that losing the race undoes
            # only this statement. session.rollback() discards the whole
            # transaction, which is harmless when this function is the only
            # writer in it and silently destructive when it is not: a batched
            # persist writes many books per transaction, and a bare rollback
            # here would throw away every book already written alongside this
            # one, without raising anything for the caller to notice.
            nested = await session.begin_nested()
            try:
                await session.execute(
                    update(Author)
                    .where(Author.id == null_id)
                    .values(
                        asin=a_asin,
                        image=_coalesce(author.get("image"), Author.image),
                        description=_longer_wins(author.get("description"), Author.description),
                        updated_at=_now(),
                    )
                )
                await nested.commit()
            except (IntegrityError, AsyncpgUniqueViolation):
                # A concurrent request upgraded between our SELECT and UPDATE.
                # The data is correct — undo the failed statement and return
                # the existing id.
                await nested.rollback()
            return null_id

        # No null-asin row — standard upsert on the unique constraint.
        stmt = insert(Author).values(
            asin=a_asin,
            name=a_name,
            region=a_region,
            description=author.get("description"),
            image=author.get("image"),
            fetched_description=bool(author.get("description")),
            created_at=_now(),
            updated_at=_now(),
        ).on_conflict_do_update(
            constraint="authors_asin_region_name_unique",
            set_={
                "image": _coalesce(author.get("image"), Author.image),
                "description": _longer_wins(author.get("description"), Author.description),
                "updated_at": _now(),
            },
        ).returning(Author.id)

    else:
        # Same duplicate tolerance as the upgrade lookup above: take the
        # oldest null-asin row if the race ever left more than one.
        existing = await session.execute(
            select(Author.id).where(
                Author.name == a_name,
                Author.region == a_region,
                Author.asin.is_(None),
            )
            .order_by(Author.id)
            .limit(1)
        )
        existing_id = existing.scalar_one_or_none()
        if existing_id:
            return existing_id

        # Insert a fresh null-asin row. A partial unique index on
        # (name, region) WHERE asin IS NULL means a concurrent insert of the
        # same author now conflicts instead of quietly duplicating — catch it,
        # undo just the INSERT, and return the row the winner inserted. Same
        # SAVEPOINT reasoning as the upgrade path above: the loser of the race
        # must not take the caller's other work down with it.
        nested = await session.begin_nested()
        try:
            result = await session.execute(
                insert(Author).values(
                    asin=None,
                    name=a_name,
                    region=a_region,
                    description=author.get("description"),
                    image=author.get("image"),
                    fetched_description=False,
                    created_at=_now(),
                    updated_at=_now(),
                ).returning(Author.id)
            )
            row = result.fetchone()
            await nested.commit()
            return row[0] if row else None
        except (IntegrityError, AsyncpgUniqueViolation):
            await nested.rollback()
            winner = await session.execute(
                select(Author.id).where(
                    Author.name == a_name,
                    Author.region == a_region,
                    Author.asin.is_(None),
                )
                .order_by(Author.id)
                .limit(1)
            )
            return winner.scalar_one_or_none()

    result = await session.execute(stmt)
    row = result.fetchone()
    return row[0] if row else None


# ============================================================
# BOOK WRITER
# ============================================================

async def _write_book(session: AsyncSession, data: dict) -> None:
    """
    Issues every statement for one book — the book row plus its genre,
    narrator, series and author relationships — and nothing else.

    Owns no transaction: it neither commits nor rolls back, so the caller
    decides whether one book or fifty share a transaction. Every statement is
    an idempotent upsert, which is what lets a caller whose transaction was
    lost replay the same books without double-counting anything.

    Existing non-null values are never overwritten with null. Pivot
    relationships (genres, narrators, authors) are additive — never shrink.
    Series position is kept current via upsert.
    """
    asin = data["asin"]
    release_date = _parse_release_date_for_db(data.get("releaseDate"))

    stmt = insert(Book).values(
        asin=asin,
        title=data.get("title", ""),
        subtitle=data.get("subtitle"),
        region=data.get("region"),
        description=data.get("description"),
        summary=data.get("summary"),
        publisher=data.get("publisher"),
        copyright=data.get("copyright"),
        isbn=data.get("isbn"),
        language=data.get("language"),
        rating=data.get("rating"),
        release_date=release_date,
        length_minutes=data.get("lengthMinutes"),
        explicit=_to_bool(data.get("explicit")),
        whisper_sync=_to_bool(data.get("whisperSync")),
        has_pdf=_to_bool(data.get("hasPdf")),
        image=data.get("imageUrl"),
        book_format=data.get("bookFormat"),
        content_type=data.get("contentType"),
        content_delivery_type=data.get("contentDeliveryType"),
        episode_number=data.get("episodeNumber"),
        episode_type=data.get("episodeType"),
        sku=data.get("sku"),
        sku_group=data.get("skuGroup"),
        is_listenable=_to_bool(data.get("isListenable"), True),
        is_buyable=_to_bool(data.get("isBuyable"), True),
        plans=cast(data.get("plans"), JSONB),
        created_at=_now(),
        updated_at=_now(),
    ).on_conflict_do_update(
        index_elements=["asin"],
        set_={
            "title": _coalesce(data.get("title"), Book.title),
            "subtitle": _coalesce(data.get("subtitle"), Book.subtitle),
            "region": Book.region,
            "description": _longer_wins(data.get("description"), Book.description),
            "summary": _longer_wins(data.get("summary"), Book.summary),
            "publisher": _coalesce(data.get("publisher"), Book.publisher),
            "copyright": _coalesce(data.get("copyright"), Book.copyright),
            "isbn": _coalesce(data.get("isbn"), Book.isbn),
            "language": _coalesce(data.get("language"), Book.language),
            "rating": _coalesce(data.get("rating"), Book.rating),
            "release_date": _coalesce(release_date, Book.release_date),
            "length_minutes": _coalesce(data.get("lengthMinutes"), Book.length_minutes),
            "explicit": _to_bool(data.get("explicit", False)),
            "whisper_sync": _to_bool(data.get("whisperSync", False)),
            "has_pdf": _to_bool(data.get("hasPdf", False)),
            "image": _coalesce(data.get("imageUrl"), Book.image),
            "book_format": _coalesce(data.get("bookFormat"), Book.book_format),
            "content_type": _coalesce(data.get("contentType"), Book.content_type),
            "content_delivery_type": _coalesce(data.get("contentDeliveryType"), Book.content_delivery_type),
            "episode_number": _coalesce(data.get("episodeNumber"), Book.episode_number),
            "episode_type": _coalesce(data.get("episodeType"), Book.episode_type),
            "sku": _coalesce(data.get("sku"), Book.sku),
            "sku_group": _coalesce(data.get("skuGroup"), Book.sku_group),
            "is_listenable": _to_bool(data.get("isListenable", True)),
            "is_buyable": _to_bool(data.get("isBuyable", True)),
            "plans": _coalesce(cast(data.get("plans"), JSONB), Book.plans),
            "updated_at": _now(),
        },
    )
    await session.execute(stmt)

    # Genres — batch upsert entities, then batch insert pivots
    genre_asins = []
    for genre in data.get("genres", []):
        g_asin = genre.get("asin")
        g_name = genre.get("name")
        if g_asin and g_name:
            genre_asins.append(g_asin)
    if genre_asins:
        genre_values = [
            {"asin": g["asin"], "name": g["name"], "type": g.get("type", "Tags"), "created_at": _now(), "updated_at": _now()}
            for g in data["genres"] if g.get("asin") and g.get("name")
        ]
        await session.execute(
            insert(Genre).values(genre_values).on_conflict_do_nothing()
        )
        await session.execute(
            insert(book_genre).values([
                {"book_asin": asin, "genre_asin": ga} for ga in genre_asins
            ]).on_conflict_do_nothing()
        )

    # Narrators — batch upsert entities, then batch insert pivots
    narrator_names = [n.get("name", "").strip() for n in data.get("narrators", []) if n.get("name", "").strip()]
    if narrator_names:
        await session.execute(
            insert(Narrator).values([
                {"name": name, "created_at": _now(), "updated_at": _now()}
                for name in narrator_names
            ]).on_conflict_do_nothing()
        )
        await session.execute(
            insert(book_narrator).values([
                {"book_asin": asin, "narrator_name": name} for name in narrator_names
            ]).on_conflict_do_nothing()
        )

    # Series — position kept current via upsert (usually 1-2, kept individual)
    for s in data.get("series", []):
        s_asin = await upsert_series(session, s)
        if s_asin:
            await session.execute(
                insert(book_series).values(
                    book_asin=asin,
                    series_asin=s_asin,
                    position=s.get("position"),
                ).on_conflict_do_update(
                    index_elements=["book_asin", "series_asin"],
                    set_={"position": _coalesce(s.get("position"), book_series.c.position)},
                )
            )

    # Authors — kept individual (complex null-asin upgrade logic)
    author_ids = []
    for author_data in data.get("authors", []):
        author_id = await upsert_author(session, author_data)
        if author_id:
            author_ids.append(author_id)
    if author_ids:
        await session.execute(
            insert(author_book).values([
                {"author_id": aid, "book_asin": asin} for aid in author_ids
            ]).on_conflict_do_nothing()
        )

    # Derived series authors — batch the cross product
    series_author_values = []
    for s in data.get("series", []):
        s_asin = s.get("asin")
        if s_asin:
            for author_id in author_ids:
                series_author_values.append({"series_asin": s_asin, "author_id": author_id})
    if series_author_values:
        await session.execute(
            insert(series_author).values(series_author_values).on_conflict_do_nothing()
        )


async def upsert_book(session: AsyncSession, data: dict) -> None:
    """
    Upserts a book and all its relationships to the relational DB, in a
    transaction of its own.

    The single-book entry point, and the per-book replay path for a chunk whose
    shared transaction was lost: it wraps _write_book in a commit of its own and
    swallows the failure, so one bad book costs only itself. The batched persist
    calls _write_book directly on its normal path.

    Existing non-null values are never overwritten with null.
    Pivot relationships (genres, narrators, authors) are additive — never shrink.
    Series position is kept current via upsert.
    """
    asin = data.get("asin")
    if not asin:
        return

    try:
        await _write_book(session, data)
        await session.commit()
        logger.info(f"DB write: book {asin}")

    except Exception as e:
        logger.warning(f"DB write failed for book {asin}: {e}")
        await session.rollback()


# ============================================================
# TRACK WRITER
# ============================================================

async def upsert_track(session: AsyncSession, asin: str, chapters_data: dict) -> None:
    """Upserts chapter data for a book."""
    try:
        stmt = insert(Track).values(
            asin=asin,
            chapters=chapters_data,
            created_at=_now(),
            updated_at=_now(),
        ).on_conflict_do_update(
            index_elements=["asin"],
            set_={"chapters": chapters_data, "updated_at": _now()},
        )
        await session.execute(stmt)
        await session.commit()
        logger.info(f"DB write: track {asin}")

    except Exception as e:
        logger.warning(f"DB write failed for track {asin}: {e}")
        await session.rollback()


# ============================================================
# AUTHOR PROFILE WRITER
# ============================================================

async def upsert_author_profile(session: AsyncSession, data: dict) -> None:
    """
    Upserts a full author profile fetched from the contributors endpoint.
    Updates description and image which aren't available from book data alone.
    Also writes author genres to author_genre pivot.
    Author genres are additive — never delete.
    """
    asin = data.get("asin")
    name = data.get("name", "").strip()
    region = data.get("region")

    if not name or not region:
        return

    try:
        if asin:
            stmt = insert(Author).values(
                asin=asin,
                name=name,
                region=region,
                description=data.get("description"),
                image=data.get("image"),
                fetched_description=True,
                created_at=_now(),
                updated_at=_now(),
            ).on_conflict_do_update(
                constraint="authors_asin_region_name_unique",
                set_={
                    "description": _longer_wins(data.get("description"), Author.description),
                    "image": _coalesce(data.get("image"), Author.image),
                    "fetched_description": True,
                    "updated_at": _now(),
                },
            ).returning(Author.id)
            result = await session.execute(stmt)
            row = result.fetchone()
            author_id = row[0] if row else None

            # Author genres — additive, never delete
            if author_id and data.get("genres"):
                for genre in data["genres"]:
                    g_asin = await upsert_genre(session, genre)
                    if g_asin:
                        await session.execute(
                            insert(author_genre).values(author_id=author_id, genre_asin=g_asin)
                            .on_conflict_do_nothing()
                        )

        await session.commit()
        logger.info(f"DB write: author {asin} ({name})")

    except Exception as e:
        logger.warning(f"DB write failed for author {asin}: {e}")
        await session.rollback()


# ============================================================
# SERIES PROFILE WRITER
# ============================================================

async def upsert_series_profile(session: AsyncSession, data: dict) -> None:
    """
    Upserts a full series profile fetched from the series endpoint.
    Updates description which isn't always available from book relationship data.
    """
    asin = data.get("asin")
    name = data.get("name")
    if not asin or not name:
        return

    description = data.get("description")

    try:
        stmt = insert(Series).values(
            asin=asin,
            title=name,
            description=description,
            region=data.get("region"),
            fetched_description=bool(description),
            created_at=_now(),
            updated_at=_now(),
        ).on_conflict_do_update(
            index_elements=["asin"],
            set_={
                "title": _coalesce(name, Series.title),
                "description": _longer_wins(description, Series.description),
                "region": Series.region,
                "fetched_description": Series.fetched_description | bool(description),
                "updated_at": _now(),
            },
        )
        await session.execute(stmt)
        await session.commit()
        logger.info(f"DB write: series {asin} ({name})")

    except Exception as e:
        logger.warning(f"DB write failed for series {asin}: {e}")
        await session.rollback()


# ============================================================
# BACKGROUND PERSISTENCE
# ============================================================

def persist_book_background(book: dict, region: str) -> None:
    """Fires a background task to write a book to DB and cache."""
    from app.services.cache import manager as cache
    from app.services.cache.manager import book_key

    async def _persist():
        async with _bg_write_semaphore:
            try:
                async with _BackgroundSession() as session:
                    await upsert_book(session, book)
                    if book.get("asin"):
                        await cache.set(session, book_key(book["asin"], region), book)
            except Exception as e:
                logger.warning(f"Background persist failed for book {book.get('asin')}: {e}")

    asyncio.create_task(_persist())


async def _cache_set_many(
    session: AsyncSession,
    entries: list[tuple[str, dict]],
    ttl_seconds: int | None = None,
) -> None:
    """
    Writes many cache entries in one statement and does not commit — the
    caller's transaction owns that.

    Same row shape, same TTL rule, and the same last-write-wins upsert as
    cache.set. What it does not do is spend a commit per key, which is the
    only reason it exists: the batched book persist would otherwise pay one
    transaction per cached book on top of one per written book. It takes a
    single `now` for the whole batch rather than letting it drift key by key,
    matching cache.get_many's single point in time across a batch.

    ttl_seconds carries cache.set's signature rather than fixing the default,
    because TTL in Libex is a property of the value and not of the key: the
    date-derived scans expire at UTC midnight, the stats key has its own
    constant, and an incomplete author catalogue is deliberately stored for
    less time than a complete one. A batch primitive that could only write the
    default would silently promote any of those to the full TTL the first time
    someone batched them, which for the degraded-catalogue case means serving
    known-incomplete data as though it were whole.

    Duplicate keys are collapsed last-wins before the statement is built:
    Postgres rejects an ON CONFLICT DO UPDATE that would touch the same row
    twice within one INSERT, and last-wins is exactly what a per-key loop over
    those same duplicates would have left stored.

    Unchunked, where cache.get_many is chunked: the row shape binds four
    parameters per entry into a single INSERT, so 8192 entries reach asyncpg's
    32,767 cap. The one caller is the batched book persist, bounded by
    _PERSIST_CHUNK_SIZE at 50 entries and 200 binds. A caller passing a list it
    does not bound is what puts a chunk loop here.
    """
    if not entries:
        return

    ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl
    now = _now()
    expires_at = now + timedelta(seconds=ttl)
    deduped = dict(entries)

    stmt = insert(Cache).values([
        {"key": key, "value": value, "created_at": now, "expires_at": expires_at}
        for key, value in deduped.items()
    ])
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "value": stmt.excluded.value,
                "created_at": stmt.excluded.created_at,
                "expires_at": stmt.excluded.expires_at,
            },
        )
    )
    logger.info("Cache set batch", extra={
        "entries": len(deduped),
        "ttl": ttl,
    })


async def _persist_book_chunk(session: AsyncSession, chunk: list[dict], region: str) -> None:
    """
    Writes one chunk of books and their cache entries in a single transaction.

    If anything takes that transaction down — a deadlock against the seeder, a
    dropped connection, one book carrying data the schema rejects — the chunk
    is replayed a book at a time down the per-book-commit path, so no book that
    a per-book write would have stored is lost by having been batched with a
    book that failed. Replay is safe because every statement involved is an
    idempotent upsert: books already written before the abort are simply
    written again to the same values, and _coalesce and _longer_wins reach the
    same result applied twice as applied once.

    A book whose own write fails still gets its cache entry on the replay
    path — the two stores fail independently.
    """
    from app.services.cache import manager as cache
    from app.services.cache.manager import book_key

    try:
        for book in chunk:
            await _write_book(session, book)
        await _cache_set_many(session, [(book_key(b["asin"], region), b) for b in chunk])
        await session.commit()
        logger.info(f"DB write: {len(chunk)} books")
        return
    except Exception as e:
        logger.warning(
            f"DB write failed for a chunk of {len(chunk)} books, replaying individually: {e}"
        )
        await session.rollback()

    for book in chunk:
        await upsert_book(session, book)
        await cache.set(session, book_key(book["asin"], region), book)


def persist_books_background(books: list[dict], region: str) -> None:
    """
    Fires a single background task to write multiple books to DB and cache.

    Writes in transactions of _PERSIST_CHUNK_SIZE books rather than one
    transaction per book. A prolific author's catalog runs past a thousand
    books, and committing per book — twice per book, once for the row and
    again for its cache entry — costs thousands of separate transactions for
    it; the commits, not the statements, are the cost. For what partial
    progress survives a failure, see _persist_book_chunk.
    """
    async def _persist():
        async with _bg_write_semaphore:
            try:
                async with _BackgroundSession() as session:
                    persistable = [b for b in books if b.get("asin")]
                    for start in range(0, len(persistable), _PERSIST_CHUNK_SIZE):
                        await _persist_book_chunk(
                            session,
                            persistable[start:start + _PERSIST_CHUNK_SIZE],
                            region,
                        )
            except Exception as e:
                logger.warning(f"Background persist failed for book batch: {e}")

    asyncio.create_task(_persist())


def persist_author_background(data: dict, region: str) -> None:
    """Fires a background task to write an author profile to DB and cache."""
    from app.services.cache import manager as cache
    from app.services.cache.manager import author_key

    async def _persist():
        async with _bg_write_semaphore:
            try:
                async with _BackgroundSession() as session:
                    await upsert_author_profile(session, data)
                    if data.get("asin"):
                        await cache.set(session, author_key(data["asin"], region), data)
            except Exception as e:
                logger.warning(f"Background persist failed for author {data.get('asin')}: {e}")

    asyncio.create_task(_persist())


def persist_series_background(data: dict, region: str) -> None:
    """Fires a background task to write a series profile to DB and cache."""
    from app.services.cache import manager as cache
    from app.services.cache.manager import series_key

    async def _persist():
        async with _bg_write_semaphore:
            try:
                async with _BackgroundSession() as session:
                    await upsert_series_profile(session, data)
                    if data.get("asin"):
                        await cache.set(session, series_key(data["asin"], region), data)
            except Exception as e:
                logger.warning(f"Background persist failed for series {data.get('asin')}: {e}")

    asyncio.create_task(_persist())


def persist_track_background(asin: str, chapters_data: dict, region: str) -> None:
    """Fires a background task to write chapter data to DB and cache."""
    from app.services.cache import manager as cache
    from app.services.cache.manager import chapters_key

    async def _persist():
        async with _bg_write_semaphore:
            try:
                async with _BackgroundSession() as session:
                    await upsert_track(session, asin, chapters_data)
                    await cache.set(session, chapters_key(asin, region), chapters_data)
            except Exception as e:
                logger.warning(f"Background persist failed for track {asin}: {e}")

    asyncio.create_task(_persist())


def persist_cache_background(key: str, value) -> None:
    """Fires a background task to write a single cache entry."""
    from app.services.cache import manager as cache

    async def _persist():
        async with _bg_write_semaphore:
            try:
                async with _BackgroundSession() as session:
                    await cache.set(session, key, value)
            except Exception as e:
                logger.warning(f"Background cache persist failed for {key}: {e}")

    asyncio.create_task(_persist())


def persist_author_books_cache_background(
    key: str, asins: list[str], ttl_seconds: int | None = None
) -> None:
    """
    Fires a background task to write an author's book-ASIN list to cache.

    An author's ASIN list only ever grows — it never legitimately shrinks —
    so the write is the union of what's already stored and what just came
    in, never a straight replacement. The catalog sort windows this list is
    built from shift as new titles are released, so a later, equal-or-longer
    run legitimately surfaces new ASINs while its own window pushes older
    ones out; comparing the two lists for one to be a superset of the other
    would refuse that normal case and discard the very ASINs it's meant to
    protect. The union orders incoming ASINs first, then appends any
    stored-only ones, since list order is a consumer-visible contract here.

    The stored row is locked with SELECT ... FOR UPDATE before the union is
    built, and the write happens later in the same transaction that took the
    lock, so a second concurrent call for the same key blocks on the lock
    until the first call's write has committed, then unions against that
    just-written value instead of the stale one. This only protects a key
    that already has a row: two calls racing to write the very first value
    for a brand-new key still both proceed unconditionally, the same as any
    other first-write upsert in this module, since there is nothing stored
    yet to union with.

    A stored row past its expiry is still locked (so the lock keeps working),
    but is treated as not-stored for the union, matching cache.get's own
    expired-is-a-miss behavior — this guard does not protect an expired entry.

    The expiry gets the same can-only-grow discipline as the ASIN list: the
    write keeps whichever is later, the stored row's expires_at or the one
    this call is asking for, rather than letting a plain overwrite apply
    the incoming TTL regardless of what's already been earned. That is
    deliberate, not an oversight of the union guarantee stopping at content:
    the two callers who actually gate a walk behind this key's TTL —
    get_author_books' use_cache=True path, and the total-failure fallback
    inside _walk_author_books itself — only ever reach this row after
    cache.get already reported a miss, i.e. after the stored expires_at has
    already passed. Keeping the later expiry costs neither of them anything:
    there is no case where it makes an already-expired, about-to-be-refreshed
    entry look fresher than it is. What it does protect is the other,
    unthrottled caller: get_author_books' default, Audible-first path calls
    this function on every request regardless of the stored TTL, so a
    previously-earned 24h window sat behind a row that is still very much
    live can otherwise be clobbered down to the short, degraded-run TTL by
    the very next request that happens to hit one bad page out of hundreds —
    and, since nothing else ever re-lengthens a downgraded entry, that
    ratchets permanently. Content and trustworthiness window move together:
    a degraded run's result is provably never worse than what's stored (it's
    a union, never a shrink), so there is no basis for treating it as less
    trustworthy either. The cost: an explicitly-cached reader that would
    otherwise be forced to retry within the short TTL instead waits out the
    remainder of the earned window — bounded by the union guarantee to
    "missing very recent additions," never "holding data since superseded."

    ttl_seconds is passed through as the request, not the outcome: since
    this write is always a union that can only grow the stored list, never
    shrink it, a caller who knows this run's own result is incomplete can
    still write it with a shorter TTL than the default so it refreshes
    sooner, rather than withholding the write entirely — the later-wins rule
    above is what stops that shorter request from undoing a longer window
    still in force. None keeps cache.set's own default (settings.cache_ttl).
    """
    from app.services.cache import manager as cache

    async def _persist():
        async with _bg_write_semaphore:
            try:
                async with _BackgroundSession() as session:
                    locked = await session.execute(
                        select(Cache).where(Cache.key == key).with_for_update()
                    )
                    row = locked.scalar_one_or_none()
                    now = _now()
                    stored = row.value if row and row.expires_at > now else None
                    to_write = asins
                    if stored:
                        incoming_set = set(asins)
                        stored_only = [a for a in stored if a not in incoming_set]
                        if stored_only:
                            to_write = list(asins) + stored_only
                    requested_ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl
                    requested_expires_at = now + timedelta(seconds=requested_ttl)
                    if row is not None and row.expires_at > requested_expires_at:
                        final_ttl = (row.expires_at - now).total_seconds()
                    else:
                        final_ttl = requested_ttl
                    await cache.set(session, key, to_write, ttl_seconds=final_ttl)
            except Exception as e:
                logger.warning(f"Background cache persist failed for {key}: {e}")

    asyncio.create_task(_persist())


async def upsert_genres(
    session: AsyncSession, region: str, genres: list[dict[str, str]]
) -> None:
    """
    Stores the catalog genre list for a region, stamping last_checked=now on
    every row so the stored set's freshness can be tracked. Upserts by
    (region, genre_id, parent_id): new nodes are inserted, existing ones get
    their name and last_checked refreshed. Each node carries a parent_id ("" for
    a top-level parent, the parent's id for a leaf), so a leaf that appears under
    two parents is stored once per parent. No-ops on an empty list.
    """
    if not genres:
        return
    now = _now()
    for genre in genres:
        parent_id = genre.get("parent_id", "")
        stmt = insert(CatalogGenre).values(
            region=region,
            genre_id=genre["genre_id"],
            parent_id=parent_id,
            name=genre["name"],
            last_checked=now,
        ).on_conflict_do_update(
            index_elements=["region", "genre_id", "parent_id"],
            set_={"name": genre["name"], "last_checked": now},
        )
        await session.execute(stmt)


async def reconcile_genres(
    session: AsyncSession, region: str, genres: list[dict[str, str]]
) -> None:
    """
    Makes the stored taxonomy for a region mirror the given set. Upserts every
    node (insert new, refresh name and last_checked), then prunes — deletes any
    stored node for the region whose (genre_id, parent_id) is not in the given
    set.

    Unlike upsert_genres, which is additive and never deletes, this prunes — so
    it must only be called with a COMPLETE taxonomy, i.e. the single live
    /categories fetch that returns the whole tree at once. Pruning is what lets
    the tree self-heal when Audible restructures: when a category moves to a new
    parent, an additive upsert leaves the old (id, old_parent) row behind as a
    ghost (e.g. a category that's no longer top-level still showing at the root).
    Reconcile removes those stale placements so the stored tree matches Audible's
    current one. No-ops on an empty list.
    """
    if not genres:
        return
    now = _now()
    fresh_keys = [(g["genre_id"], g.get("parent_id", "")) for g in genres]
    for genre in genres:
        parent_id = genre.get("parent_id", "")
        stmt = insert(CatalogGenre).values(
            region=region,
            genre_id=genre["genre_id"],
            parent_id=parent_id,
            name=genre["name"],
            last_checked=now,
        ).on_conflict_do_update(
            index_elements=["region", "genre_id", "parent_id"],
            set_={"name": genre["name"], "last_checked": now},
        )
        await session.execute(stmt)
    # Prune stale placements — e.g. a category's old parent_id after Audible
    # moves it. Everything in the fresh (complete) fetch is kept; anything stored
    # for this region but absent from it is removed.
    await session.execute(
        delete(CatalogGenre).where(
            CatalogGenre.region == region,
            tuple_(CatalogGenre.genre_id, CatalogGenre.parent_id).notin_(fresh_keys),
        )
    )