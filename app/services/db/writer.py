"""
Database writer service.
Persists Audible API responses to relational tables.

Called after every successful Audible fetch to keep the DB in sync.
Writes are upserts — existing non-null values are never overwritten with null.
The DB is used as a fallback when Audible is unavailable.
"""

# Standard library
import asyncio
from datetime import datetime, timezone

# Third party
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from asyncpg.exceptions import UniqueViolationError as AsyncpgUniqueViolation
from sqlalchemy.dialects.postgresql import insert, JSONB
from sqlalchemy import select, func, update, case, cast

# Database
from app.db.models import (
    Book,
    Author,
    Genre,
    Narrator,
    Series,
    Track,
    author_book,
    author_genre,
    book_genre,
    book_narrator,
    book_series,
    series_author,
)
from app.db.session import engine

# Core
from app.core.logging import get_logger

logger = get_logger()

_BackgroundSession = async_sessionmaker(engine, expire_on_commit=False)


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
    """Returns new_value if it is longer than the existing value, otherwise keeps existing."""
    return case(
        (func.length(new_value) > func.length(existing_col), new_value),
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

        # Step 2: look for a null-asin row to upgrade.
        null_result = await session.execute(
            select(Author.id).where(
                Author.name == a_name,
                Author.region == a_region,
                Author.asin.is_(None),
            )
        )
        null_id = null_result.scalar_one_or_none()

        if null_id:
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
            except (IntegrityError, AsyncpgUniqueViolation):
                # A concurrent request upgraded between our SELECT and UPDATE.
                # The data is correct — roll back the failed statement and
                # return the existing id.
                await session.rollback()
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
        existing = await session.execute(
            select(Author.id).where(
                Author.name == a_name,
                Author.region == a_region,
                Author.asin.is_(None),
            )
        )
        existing_id = existing.scalar_one_or_none()
        if existing_id:
            return existing_id

        stmt = insert(Author).values(
            asin=None,
            name=a_name,
            region=a_region,
            description=author.get("description"),
            image=author.get("image"),
            fetched_description=False,
            created_at=_now(),
            updated_at=_now(),
        ).returning(Author.id)

    result = await session.execute(stmt)
    row = result.fetchone()
    return row[0] if row else None


# ============================================================
# BOOK WRITER
# ============================================================

async def upsert_book(session: AsyncSession, data: dict) -> None:
    """
    Upserts a book and all its relationships to the relational DB.
    Called after every successful Audible fetch.
    Existing non-null values are never overwritten with null.
    Pivot relationships (genres, narrators, authors) are additive — never shrink.
    Series position is kept current via upsert.
    """
    asin = data.get("asin")
    if not asin:
        return

    try:
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
        try:
            async with _BackgroundSession() as session:
                await upsert_book(session, book)
                if book.get("asin"):
                    await cache.set(session, book_key(book["asin"], region), book)
        except Exception as e:
            logger.warning(f"Background persist failed for book {book.get('asin')}: {e}")

    asyncio.create_task(_persist())


def persist_books_background(books: list[dict], region: str) -> None:
    """Fires a single background task to write multiple books to DB and cache."""
    from app.services.cache import manager as cache
    from app.services.cache.manager import book_key

    async def _persist():
        try:
            async with _BackgroundSession() as session:
                for book in books:
                    if book.get("asin"):
                        await upsert_book(session, book)
                        await cache.set(session, book_key(book["asin"], region), book)
        except Exception as e:
            logger.warning(f"Background persist failed for book batch: {e}")

    asyncio.create_task(_persist())


def persist_author_background(data: dict, region: str) -> None:
    """Fires a background task to write an author profile to DB and cache."""
    from app.services.cache import manager as cache
    from app.services.cache.manager import author_key

    async def _persist():
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
        try:
            async with _BackgroundSession() as session:
                await cache.set(session, key, value)
        except Exception as e:
            logger.warning(f"Background cache persist failed for {key}: {e}")

    asyncio.create_task(_persist())