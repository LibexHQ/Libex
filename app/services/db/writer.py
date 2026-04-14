"""
Database writer service.
Persists Audible API responses to relational tables.

Called after every successful Audible fetch to keep the DB in sync.
Writes are upserts — always update with the latest data from Audible.
The DB is used as a fallback when Audible is unavailable.
"""

# Standard library
from datetime import datetime, timezone
from typing import Any

# Third party
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select, delete

# Database
from app.db.models import (
    Book,
    Author,
    Genre,
    Narrator,
    Series,
    Track,
    author_book,
    book_genre,
    book_narrator,
    book_series,
)

# Core
from app.core.logging import get_logger

logger = get_logger()


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
        set_={"name": name, "type": genre_type, "updated_at": _now()},
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

    stmt = insert(Series).values(
        asin=asin,
        title=name,
        description=series.get("description"),
        region=series.get("region"),
        fetched_description=bool(series.get("description")),
        created_at=_now(),
        updated_at=_now(),
    ).on_conflict_do_update(
        index_elements=["asin"],
        set_={
            "title": name,
            "region": series.get("region"),
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
    Authors are uniquely identified by (asin, region, name).
    """
    a_asin = author.get("asin")
    a_name = author.get("name", "").strip()
    a_region = author.get("region")

    if not a_name or not a_region:
        return None

    if a_asin:
        # Upsert on unique constraint when ASIN is present
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
                "image": author.get("image"),
                "updated_at": _now(),
            },
        ).returning(Author.id)
    else:
        # No ASIN — insert only if not already present by name/region
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
    """
    asin = data.get("asin")
    if not asin:
        return

    try:
        release_date = _parse_release_date_for_db(data.get("releaseDate"))

        # Upsert the book record
        book_stmt = insert(Book).values(
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
            explicit=data.get("explicit", False),
            whisper_sync=data.get("whisperSync", False),
            has_pdf=data.get("hasPdf", False),
            image=data.get("imageUrl"),
            book_format=data.get("bookFormat"),
            content_type=data.get("contentType"),
            content_delivery_type=data.get("contentDeliveryType"),
            episode_number=data.get("episodeNumber"),
            episode_type=data.get("episodeType"),
            sku=data.get("sku"),
            sku_group=data.get("skuGroup"),
            is_listenable=data.get("isListenable", True),
            is_buyable=data.get("isBuyable", True),
            created_at=_now(),
            updated_at=_now(),
        ).on_conflict_do_update(
            index_elements=["asin"],
            set_={
                "title": data.get("title", ""),
                "subtitle": data.get("subtitle"),
                "region": data.get("region"),
                "description": data.get("description"),
                "summary": data.get("summary"),
                "publisher": data.get("publisher"),
                "copyright": data.get("copyright"),
                "isbn": data.get("isbn"),
                "language": data.get("language"),
                "rating": data.get("rating"),
                "release_date": release_date,
                "length_minutes": data.get("lengthMinutes"),
                "explicit": data.get("explicit", False),
                "whisper_sync": data.get("whisperSync", False),
                "has_pdf": data.get("hasPdf", False),
                "image": data.get("imageUrl"),
                "book_format": data.get("bookFormat"),
                "content_type": data.get("contentType"),
                "content_delivery_type": data.get("contentDeliveryType"),
                "episode_number": data.get("episodeNumber"),
                "episode_type": data.get("episodeType"),
                "sku": data.get("sku"),
                "sku_group": data.get("skuGroup"),
                "is_listenable": data.get("isListenable", True),
                "is_buyable": data.get("isBuyable", True),
                "updated_at": _now(),
            },
        )
        await session.execute(book_stmt)

        # Genres
        genre_asins = []
        for genre in data.get("genres", []):
            g_asin = await upsert_genre(session, genre)
            if g_asin:
                genre_asins.append(g_asin)

        if genre_asins:
            await session.execute(delete(book_genre).where(book_genre.c.book_asin == asin))
            for g_asin in genre_asins:
                await session.execute(
                    insert(book_genre).values(book_asin=asin, genre_asin=g_asin)
                    .on_conflict_do_nothing()
                )

        # Narrators
        narrator_names = []
        for narrator in data.get("narrators", []):
            n_name = await upsert_narrator(session, narrator)
            if n_name:
                narrator_names.append(n_name)

        if narrator_names:
            await session.execute(delete(book_narrator).where(book_narrator.c.book_asin == asin))
            for n_name in narrator_names:
                await session.execute(
                    insert(book_narrator).values(book_asin=asin, narrator_name=n_name)
                    .on_conflict_do_nothing()
                )

        # Series
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
                        set_={"position": s.get("position")},
                    )
                )

        # Authors
        for author_data in data.get("authors", []):
            author_id = await upsert_author(session, author_data)
            if author_id:
                await session.execute(
                    insert(author_book).values(author_id=author_id, book_asin=asin)
                    .on_conflict_do_nothing()
                )

        await session.commit()

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
                    "description": data.get("description"),
                    "image": data.get("image"),
                    "fetched_description": True,
                    "updated_at": _now(),
                },
            )
            await session.execute(stmt)
        await session.commit()
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

    try:
        stmt = insert(Series).values(
            asin=asin,
            title=name,
            description=data.get("description"),
            region=data.get("region"),
            fetched_description=bool(data.get("description")),
            created_at=_now(),
            updated_at=_now(),
        ).on_conflict_do_update(
            index_elements=["asin"],
            set_={
                "title": name,
                "description": data.get("description"),
                "region": data.get("region"),
                "fetched_description": bool(data.get("description")),
                "updated_at": _now(),
            },
        )
        await session.execute(stmt)
        await session.commit()
    except Exception as e:
        logger.warning(f"DB write failed for series {asin}: {e}")
        await session.rollback()