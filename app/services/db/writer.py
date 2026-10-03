"""
Database writer service.
Persists Audible API responses to relational tables.

Called after every successful Audible fetch to keep the DB in sync.
Writes are upserts — existing non-null values are never overwritten with null.
The DB is used as a fallback when Audible is unavailable.

The statements and the merge rules live in libex_core.storage.write, shared
with the embedded library, and raise on failure. This module is the hosted
face of them: it fixes the dialect to Postgres, names the driver's own
unique-violation error so a lost author race is recognised however it
surfaces, and owns what only the hosted app does — the logging, the
commit-and-swallow of the single-entity entry points, and the response cache
and catalog genre tree, which are not part of the shared schema.
write_books, the batched path, owns no transaction of its own — when it runs,
how many run at once, and whose transaction it shares all belong to
persist_queue, which imports this module and is never imported by it.
upsert_book, upsert_track, upsert_author_profile and upsert_series_profile are
the exception: each is a single-entity entry point that commits and swallows
its own failure, so a caller must not wrap one in a transaction of its own.
"""

# Standard library
from datetime import timedelta

# Third party
from asyncpg.exceptions import UniqueViolationError as AsyncpgUniqueViolation
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import delete, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.models import Cache, CatalogGenre

# Core
from app.core.config import get_settings
from app.core.logging import get_logger
from libex_core.storage import merge
from libex_core.storage.write import books as _books
from libex_core.storage.write import entities as _entities
from libex_core.storage.write.params import book_params as _book_params  # noqa: F401
from libex_core.storage.write.params import series_params as _series_params  # noqa: F401
from libex_core.storage.write.statements import statements_for
from libex_core.storage.write.support import asserted_bool as _asserted_bool  # noqa: F401
from libex_core.storage.write.support import utc_now as _now

logger = get_logger()
settings = get_settings()

_DIALECT = "postgresql"
_CONFLICT_ERRORS = (AsyncpgUniqueViolation,)

# The merge builders, under the names this module has always exported.
_BLANK_CHARS = merge.BLANK_CHARS
_coalesce = merge.coalesce
_answered = merge.answered
_longer_wins = merge.longer_wins
_chapter_count = merge.chapter_count
_chaptered_wins = merge.chaptered_wins
_extras_union = merge.extras_union

_STATEMENTS = statements_for(_DIALECT)
_BOOK_UPSERT = _STATEMENTS.book_upsert
_SERIES_UPSERT = _STATEMENTS.series_upsert


def _failure_fields(exc: BaseException) -> dict:
    """
    The only thing a failed write may say about itself: what kind it was,
    which SQLSTATE the server returned, and which schema object it was
    against — never the exception itself.

    Postgres puts the offending row into its own message text — a not-null
    violation reports "Failing row contains (...)" with every column in it —
    so str(exc), repr(exc) and exc_info all publish book data into the logs
    whatever hide_parameters is set to. schema/table/column/constraint name
    are schema metadata, not row content, and safe to carry alongside it.
    exc.code is deliberately absent: that is SQLAlchemy's documentation slug,
    not the pgcode, and reading it as one is how the wrong thing ends up in a
    dashboard.

    Lives here, not in persist_queue, so writer's own upsert functions can use
    it too without persist_queue importing back into writer — writer is
    stateless and never imports the module that imports it.
    """
    orig = getattr(exc, "orig", None)
    return {
        "error_type": type(exc).__name__,
        "sqlstate": getattr(orig, "sqlstate", None),
        "schema_name": getattr(orig, "schema_name", None),
        "table_name": getattr(orig, "table_name", None),
        "column_name": getattr(orig, "column_name", None),
        "constraint_name": getattr(orig, "constraint_name", None),
    }


# ============================================================
# ENTITY WRITERS
# ============================================================

async def upsert_genre(session: AsyncSession, genre: dict) -> str | None:
    """Upserts a single genre. Returns asin if successful."""
    return await _entities.upsert_genre(session, genre, dialect=_DIALECT)


async def upsert_narrator(session: AsyncSession, narrator: dict) -> str | None:
    """Upserts a single narrator. Returns name if successful."""
    return await _entities.upsert_narrator(session, narrator, dialect=_DIALECT)


async def upsert_series(session: AsyncSession, series: dict) -> str | None:
    """Upserts a series record. Returns asin if successful."""
    return await _entities.upsert_series(session, series, dialect=_DIALECT)


async def upsert_author(session: AsyncSession, author: dict) -> int | None:
    """
    Upserts an author record. Returns the author's DB id if successful.
    See libex_core.storage.write.entities.upsert_author for the lookup order
    and the race handling.
    """
    return await _entities.upsert_author(
        session, author, dialect=_DIALECT, conflict_errors=_CONFLICT_ERRORS
    )


# ============================================================
# BOOK WRITER
# ============================================================

async def _resolve_author_ids(
    session: AsyncSession, books: list[dict]
) -> dict[tuple[str, str | None], list[int]]:
    """Resolves every book's authors to DB ids, once per distinct author,
    keyed by the book's (asin, region)."""
    return await _books.resolve_author_ids(
        session, books, dialect=_DIALECT, conflict_errors=_CONFLICT_ERRORS
    )


async def write_books(session: AsyncSession, books: list[dict]) -> None:
    """
    Issues every statement for a list of books — their rows plus their genre,
    narrator, series and author relationships — and nothing else.

    Owns no transaction: it neither commits nor rolls back, so the caller
    decides whether one book or fifty share a transaction. Every statement is
    an idempotent upsert, which is what lets a caller whose transaction was
    lost replay the same books without double-counting anything. See
    libex_core.storage.write.books.write_books for the shape of the batch.

    Existing non-null values are never overwritten with null. Pivot
    relationships (genres, narrators, authors) are additive — never shrink.
    Series position is kept current via upsert.
    """
    await _books.write_books(
        session, books, dialect=_DIALECT, conflict_errors=_CONFLICT_ERRORS
    )


async def _write_book(session: AsyncSession, data: dict) -> None:
    """
    Issues every statement for one book, as a batch of one.

    Kept as its own name because the single-book callers read better for it,
    and routed through write_books so the one-book and fifty-book paths cannot
    drift apart in what they write or how they merge it.
    """
    await write_books(session, [data])


async def upsert_book(session: AsyncSession, data: dict) -> None:
    """
    Upserts a book and all its relationships to the relational DB, in a
    transaction of its own.

    The single-book entry point, and the per-book replay path for a chunk whose
    shared transaction was lost: it wraps _write_book in a commit of its own and
    keeps an ordinary bad book's failure to itself.

    That is most of "one bad book costs only itself" and not all of it, which
    is worth stating exactly, because the missing part used to read here as
    settled. The rollback below is unguarded — nothing catches it — so a
    connection that has died under the statement raises there instead, and the
    failure leaves this function after all. The property holds because the
    caller carries the rest of it: _replay_book_chunk guards this call and
    clears the session before it reaches the next book. Neither half is
    sufficient alone.

    The batched persist calls write_books directly on its normal path.

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
        logger.warning(
            "DB write failed for book",
            extra={"asin": asin, **_failure_fields(e)},
        )
        await session.rollback()


# ============================================================
# TRACK WRITER
# ============================================================

async def upsert_track(
    session: AsyncSession, asin: str, chapters_data: dict, *, region: str
) -> None:
    """
    Upserts chapter data for a book, keeping the richer of the two payloads.
    The listing belongs to the book's record in `region`; there is no default,
    because a listing filed under the wrong marketplace is a silent error.

    The merge is decided in the SET clause of one statement, against the row
    as postgresql has it locked — see libex_core.storage.write.entities.
    write_track. The stored count comes back so a suppressed overwrite can be
    logged: a write that silently declines is no easier to diagnose than the
    silent overwrite it replaces, and no one is watching this path.

    The insert is conditional on the book's row for `region`. When it is not
    stored nothing is written, and that is logged at info, not as a failure:
    a chapters request for a region the book was never stored under is
    ordinary.
    """
    try:
        stored_count = await _entities.write_track(
            session, asin, chapters_data, region=region, dialect=_DIALECT
        )
        await session.commit()

        if stored_count is None:
            logger.info(
                "Chapters not stored: the book is not stored for the region",
                extra={"asin": asin, "region": region},
            )
            return

        offered = chapters_data.get("chapters") if isinstance(chapters_data, dict) else None
        offered_count = len(offered) if isinstance(offered, list) else 0

        if offered_count == 0 and stored_count > 0:
            logger.warning(
                "Kept stored chapters over an empty response",
                extra={"asin": asin, "stored_chapters": stored_count},
            )
        else:
            logger.info(f"DB write: track {asin} ({region})")

    except Exception as e:
        logger.warning(
            "DB write failed for track",
            extra={"asin": asin, **_failure_fields(e)},
        )
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
        await _entities.write_author_profile(session, data, dialect=_DIALECT)
        await session.commit()
        logger.info(f"DB write: author {asin} ({name})")

    except Exception as e:
        logger.warning(
            "DB write failed for author",
            extra={"asin": asin, **_failure_fields(e)},
        )
        await session.rollback()


# ============================================================
# SERIES PROFILE WRITER
# ============================================================

async def upsert_series_profile(session: AsyncSession, data: dict) -> str | None:
    """
    Upserts a full series profile fetched from the series endpoint.
    Updates description which isn't always available from book relationship data.

    Writes through the same statement the book path writes series with. All
    this adds is a transaction of its own and a stricter guard: a profile
    fetch that answered without a name has failed, where a book's series
    relationship may legitimately carry the title under either key.

    Returns the series asin once it is written, and None when nothing was: the
    profile names no asin, no name or no region (a series row is keyed by its
    region, so one without it cannot be stored), or the write failed.
    """
    asin = data.get("asin")
    name = data.get("name")
    if not asin or not name:
        return None

    try:
        written = await _entities.write_series_profile(session, data, dialect=_DIALECT)
        if not written:
            logger.info(
                "Series not stored: the profile names no region",
                extra={"asin": asin},
            )
            return None
        await session.commit()
        logger.info(f"DB write: series {asin} ({name})")
        return written

    except Exception as e:
        logger.warning(
            "DB write failed for series",
            extra={"asin": asin, **_failure_fields(e)},
        )
        await session.rollback()
        return None


# ============================================================
# CACHE WRITER
# ============================================================

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
    does not bound is what puts a chunk loop here. It is the only multi-row
    VALUES the persist path issues — every other statement in it binds one row
    per execution and so cannot reach the cap at any chunk size.
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


# ============================================================
# CATALOG GENRE WRITER
# ============================================================

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
