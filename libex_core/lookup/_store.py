"""
Write-through to a LocalStore, shared by the lookups that accept one.

With a store, a lookup keeps what Audible answered and answers from what it
keeps, the way the hosted service does without its cache: a successful fetch is
written under the same merge rules (an existing value is never replaced by less,
and the relationship rows only ever grow), the row the store then holds is what
is served, and when Audible cannot be reached the store is what answers. A
confirmed absence on Audible is never answered from the store.

Nothing here imports SQLAlchemy at import time, so libex_core.lookup stays
importable without the storage extra: the storage package is reached only
when a store was actually passed.

Failures are never silent and never raised for a persistence problem alone,
as on the hosted service: a write that fails is logged at warning (the error
type and counts only, no value a caller supplied) and the live answer is still
returned, the failure being reported to the caller through the lookups that
return a result object. A store that is closed or was never opened is the
caller's mistake and is raised up front by check, since carrying on would
quietly run without storage.
"""

# Standard library
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

# Core
from libex_core.audible.books import settle_flags_list

if TYPE_CHECKING:
    from libex_core.storage.store import LocalStore

logger = logging.getLogger("libex")

# Books are written in transactions of this many, as the hosted service does,
# so a long list is not one long transaction and what committed survives a
# later chunk failing.
WRITE_CHUNK_SIZE = 50

# Stored books are read this many ASINs to a query, well under the variable
# limit SQLite binds in one statement.
READ_CHUNK_SIZE = 500


async def check(store: "LocalStore") -> None:
    """
    Raises StoreClosed when the store is closed or was never opened. Called
    first by every lookup that was given a store, so that a store that cannot
    be used is the caller's error, raised before anything is fetched, and is
    never taken for a persistence failure to log and carry on past.
    """
    async with store.session():
        pass


def log_served_from_store(what: str, region: str, **fields: Any) -> None:
    """One line, always the same message, whenever the store answers in place
    of Audible. what names the kind of record; fields are counts or an ASIN the
    lookup already logs, never text a caller supplied."""
    logger.warning(
        "Answered from the store while Audible was unavailable",
        extra={"what": what, "region": region, **fields},
    )


def _log_write_failure(what: str, exc: Exception, **fields: Any) -> None:
    logger.warning(
        "Store write failed, serving the live answer",
        extra={"what": what, "error_type": type(exc).__name__, **fields},
    )


# ============================================================
# WRITING
# ============================================================

async def persist_books(
    store: "LocalStore", books: list[dict[str, Any]], region: str
) -> tuple[set[str], bool]:
    """
    Writes normalized books, unsettled (the writer needs the tri-state flags as
    normalization produced them), and returns the ASINs that were committed and
    whether any chunk failed to be.
    """
    from libex_core.storage import write

    persistable = [b for b in books if b.get("asin")]
    written: set[str] = set()
    failed = False
    for start in range(0, len(persistable), WRITE_CHUNK_SIZE):
        chunk = persistable[start:start + WRITE_CHUNK_SIZE]
        try:
            async with store.write() as session:
                await write.write_books(session, chunk)
        except Exception as exc:
            failed = True
            _log_write_failure("books", exc, books=len(chunk), region=region)
            continue
        written.update(b["asin"] for b in chunk)
    if written:
        logger.info("Wrote books to the store", extra={
            "books": len(written),
            "region": region,
        })
    return written, failed


async def _persist_one(
    store: "LocalStore", what: str, write_one: Callable[[Any], Awaitable[Any]], **fields: Any
) -> bool:
    try:
        async with store.write() as session:
            await write_one(session)
    except Exception as exc:
        _log_write_failure(what, exc, **fields)
        return False
    logger.info("Wrote to the store", extra={"what": what, **fields})
    return True


async def persist_series(store: "LocalStore", data: dict[str, Any], region: str) -> bool:
    """Writes a series profile. False when the write failed."""
    from libex_core.storage import write

    return await _persist_one(
        store, "series", lambda s: write.write_series_profile(s, data), region=region
    )


async def persist_author(store: "LocalStore", data: dict[str, Any], region: str) -> bool:
    """Writes an author profile. False when the write failed."""
    from libex_core.storage import write

    return await _persist_one(
        store, "author", lambda s: write.write_author_profile(s, data), region=region
    )


async def persist_track(
    store: "LocalStore", asin: str, chapters: dict[str, Any], region: str
) -> bool:
    """Writes a book's chapters, keeping the richer of the stored and offered
    listing. False when the book is not in the store or the write failed."""
    from libex_core.storage import write

    # A chapter listing hangs off its book's row, so one for a book the store
    # does not hold cannot be written; the hosted service meets the same limit
    # and only logs the failure. A read that fails is not the same as the book
    # being absent: it is logged as a failed read and nothing is written.
    held = await _read(store, "chapter book", lambda s: _book_held(s, asin), None)
    if held is None:
        return False
    if not held:
        logger.info("Chapters not stored: the book is not in the store", extra={
            "asin": asin,
            "region": region,
        })
        return False
    return await _persist_one(
        store, "chapters", lambda s: write.write_track(s, asin, chapters), region=region
    )


# ============================================================
# READING
# ============================================================

async def _read(
    store: "LocalStore", what: str, read_one: Callable[[Any], Awaitable[Any]], default: Any
) -> Any:
    """Runs one stored read. A failed read is the same as nothing stored, and
    is logged as such, so a broken store never turns an outage into something
    else."""
    try:
        async with store.session() as session:
            return await read_one(session)
    except Exception as exc:
        logger.warning("Store read failed", extra={
            "what": what,
            "error_type": type(exc).__name__,
        })
        return default


async def _book_held(session: Any, asin: str) -> bool:
    from sqlalchemy import select

    from libex_core.storage.models import Book

    result = await session.execute(select(Book.asin).where(Book.asin == asin))
    return result.first() is not None


async def _book_region(session: Any, asin: str) -> str | None:
    from sqlalchemy import select

    from libex_core.storage.models import Book

    result = await session.execute(select(Book.region).where(Book.asin == asin))
    return result.scalar_one_or_none()


async def _read_books_chunked(session: Any, asins: list[str]) -> list[dict[str, Any]]:
    from libex_core.storage.read import books as read_books

    rows: list[dict[str, Any]] = []
    for start in range(0, len(asins), READ_CHUNK_SIZE):
        rows.extend(await read_books.get_books(session, asins[start:start + READ_CHUNK_SIZE]))
    return rows


async def stored_books(
    store: "LocalStore", asins: list[str], region: str | None = None
) -> list[dict[str, Any]]:
    """The stored books for these ASINs, settled, in the order asked, those
    not stored left out. With a region, only books stored for that marketplace
    count: a book ASIN is region-specific, so a row another marketplace stored
    is not this one's. Read in chunks of READ_CHUNK_SIZE."""
    if not asins:
        return []
    rows = await _read(store, "books", lambda s: _read_books_chunked(s, list(asins)), [])
    by_asin = {
        row["asin"]: row for row in rows
        if region is None or row.get("region") == region
    }
    return settle_flags_list([by_asin[a] for a in asins if a in by_asin])


async def serve_merged(
    store: "LocalStore", live: list[dict[str, Any]], written: set[str]
) -> list[dict[str, Any]]:
    """
    The live books, in their order, with each one that was just written replaced
    by the row the store now holds, which is the merge of what it had and what
    Audible just said. A book that was not written, or that cannot be read
    back, is served as Audible sent it. Settled.
    """
    if not written:
        return settle_flags_list(live)
    stored = {b["asin"]: b for b in await stored_books(store, sorted(written))}
    return settle_flags_list([
        stored[b["asin"]] if b.get("asin") in stored else b for b in live
    ])


async def stored_series(
    store: "LocalStore", asin: str, region: str | None = None
) -> dict[str, Any] | None:
    """The stored series record. With a region, a record stored for another
    marketplace is not returned; one stored with no region is, since nothing
    ties it to a different one."""
    from libex_core.storage.read import series as read_series

    row = await _read(store, "series", lambda s: read_series.get_series(s, asin), None)
    if row and region is not None and row.get("region") not in (None, region):
        return None
    return row


async def stored_series_books(
    store: "LocalStore", asin: str, region: str | None = None
) -> list[dict[str, Any]]:
    """The stored books of a series in series order, settled, those stored for
    the region only when one is given."""
    from libex_core.storage.read import series as read_series

    rows = await _read(
        store,
        "series books",
        lambda s: read_series.get_series_books(s, asin, region=region),
        [],
    )
    return settle_flags_list(rows)


async def search_stored_series(store: "LocalStore", name: str) -> list[dict[str, Any]]:
    from libex_core.storage.read import series as read_series

    return await _read(store, "series search", lambda s: read_series.search_series(s, name), [])


async def stored_author(
    store: "LocalStore", asin: str, region: str
) -> dict[str, Any] | None:
    from libex_core.storage.read import people

    return await _read(store, "author", lambda s: people.get_author(s, asin, region), None)


async def stored_author_book_asins(
    store: "LocalStore", asin: str, region: str
) -> list[str] | None:
    """The ASINs of the author's stored books, or None when the read failed,
    which is not the same as the author having none."""
    from libex_core.storage.read import people

    return await _read(
        store, "author books", lambda s: people.get_author_book_asins(s, asin, region), None
    )


async def stored_track(
    store: "LocalStore", asin: str, region: str | None = None
) -> dict[str, Any] | None:
    """The stored chapters. A listing belongs to its book, so with a region it
    is returned only when the book is stored for that marketplace."""
    from libex_core.storage.read import books as read_books

    async def read_one(session: Any) -> dict[str, Any] | None:
        if region is not None and await _book_region(session, asin) != region:
            return None
        return await read_books.get_track(session, asin)

    return await _read(store, "chapters", read_one, None)


async def search_stored_books(
    store: "LocalStore", title: str, author: str, region: str | None = None
) -> list[dict[str, Any]]:
    """Stored books matching a title and an author name, settled, those stored
    for the region only when one is given."""
    from libex_core.storage.read import books as read_books

    rows = await _read(
        store,
        "book search",
        lambda s: read_books.search_books(
            s, title=title, author_name=author, region=region, limit=10
        ),
        [],
    )
    return settle_flags_list(rows)
