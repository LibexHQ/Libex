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
from datetime import datetime
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

# What a failed read returns where None already means a book that is not stored.
_READ_FAILED = object()


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
    store: "LocalStore", books: list[dict[str, Any]], region: str, *, confirm: bool = False
) -> tuple[set[str], bool]:
    """
    Writes normalized books, unsettled (the writer needs the tri-state flags as
    normalization produced them), and returns the ASINs that were committed and
    whether any chunk failed to be.

    A book is keyed by its ASIN and its marketplace, so the same ASIN written
    for two regions is two rows and neither write touches the other. Every
    book in the list is written for region; the writer links its series,
    authors, narrators and genres to that region's record.

    confirm stamps the books as confirmed by Audible, and is for a product
    fetch of those books only, never a listing or a search; the series and
    authors they name are never stamped.
    """
    from libex_core.storage import write

    persistable = [b for b in books if b.get("asin")]
    written: set[str] = set()
    failed = False
    for start in range(0, len(persistable), WRITE_CHUNK_SIZE):
        chunk = persistable[start:start + WRITE_CHUNK_SIZE]
        try:
            async with store.write() as session:
                await write.write_books(session, chunk, confirm=confirm)
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
    store: "LocalStore",
    what: str,
    write_one: Callable[[Any], Awaitable[Any]],
    *,
    refusal: str | None = None,
    **fields: Any,
) -> bool:
    """Runs one write in the store's write session. With `refusal`, a writer
    that answers None has declined the data rather than failed: nothing was
    written, so that is logged as such and reported as False."""
    try:
        async with store.write() as session:
            result = await write_one(session)
    except Exception as exc:
        _log_write_failure(what, exc, **fields)
        return False
    if refusal is not None and result is None:
        logger.info("Not written to the store", extra={"what": what, "reason": refusal, **fields})
        return False
    logger.info("Wrote to the store", extra={"what": what, **fields})
    return True


async def persist_series(
    store: "LocalStore", data: dict[str, Any], region: str, *, confirm: bool = False
) -> bool:
    """Writes a series profile, which carries its own region. False when the
    write failed or the profile names no region and so could not be keyed.
    confirm stamps the series as confirmed, for a series profile fetch."""
    from libex_core.storage import write

    return await _persist_one(
        store,
        "series",
        lambda s: write.write_series_profile(s, data, confirm=confirm),
        refusal="the series has no asin, name or region",
        region=region,
    )


async def persist_author(
    store: "LocalStore", data: dict[str, Any], region: str, *, confirm: bool = False
) -> bool:
    """Writes an author profile. False when the write failed. confirm stamps
    the author as confirmed, for an author profile fetch."""
    from libex_core.storage import write

    return await _persist_one(
        store,
        "author",
        lambda s: write.write_author_profile(s, data, confirm=confirm),
        region=region,
    )


async def persist_track(
    store: "LocalStore",
    asin: str,
    chapters: dict[str, Any],
    region: str,
    *,
    confirm: bool = False,
) -> bool:
    """Writes a book's chapters under the book's record for region, keeping the
    richer of the stored and offered listing. False when that record is not in
    the store, the read that looked for it failed, or the write failed; each is
    logged as its own case. confirm also stamps the book's chapters as
    confirmed, for a listing Audible just answered with."""
    from libex_core.storage import write

    # A chapter listing hangs off its book's row, so one for a book the store
    # does not hold for this region cannot be written; the hosted writer
    # meets the same limit inside its insert, which then writes nothing and
    # logs an info line instead of failing. A read that fails is not the
    # same as the book being absent: it is logged as a failed read and nothing
    # is written.
    stored = await _read(
        store, "chapter book", lambda s: _book_stored(s, asin, region), _READ_FAILED
    )
    if stored is _READ_FAILED:
        return False
    if not stored:
        logger.info("Chapters not stored: the book is not in the store", extra={
            "asin": asin,
            "region": region,
        })
        return False
    return await _persist_one(
        store,
        "chapters",
        lambda s: write.write_track(s, asin, chapters, region=region, confirm=confirm),
        refusal="the book is not stored for the region",
        region=region,
    )


async def persist_chapters_confirmed_absent(
    store: "LocalStore", asin: str, region: str
) -> bool:
    """
    Records that Audible answered for a book's chapters with nothing to list (a
    404, or a response with no listing). That is an answer, and it is what
    tells a book without chapters from one nobody has asked about. Nothing else
    about the store changes, and a book the store does not hold for the region
    has nothing to record it on. False when the write failed.
    """
    from libex_core.storage import write

    return await _persist_one(
        store,
        "chapters confirmation",
        lambda s: write.confirm_chapters(s, asin, region=region),
        region=region,
    )


async def persist_walk(
    store: "LocalStore",
    kind: str,
    asin: str,
    region: str,
    book_asins: list[str],
    *,
    complete: bool,
    incomplete_reasons: tuple[str, ...],
    at: datetime,
) -> bool:
    """Records a finished walk as the snapshot for (kind, asin, region). False
    when the write failed. A newer snapshot already held is kept by the writer
    and is not a failure. Logged with counts only, never the ASINs."""
    from libex_core.storage import write

    return await _persist_one(
        store,
        "walk",
        lambda s: write.write_walk_result(
            s,
            kind=kind,
            asin=asin,
            region=region,
            book_asins=book_asins,
            complete=complete,
            incomplete_reasons=list(incomplete_reasons),
            at=at,
        ),
        kind=kind,
        region=region,
        book_num=len(book_asins),
    )


async def forget_walk(
    store: "LocalStore", kind: str, asin: str, region: str, *, at: datetime
) -> bool:
    """Removes the snapshot for (kind, asin, region) after Audible confirmed
    the walk's subject has no books. False when the delete failed."""
    from libex_core.storage import write

    return await _persist_one(
        store,
        "walk removal",
        lambda s: write.delete_walk_result(s, kind=kind, asin=asin, region=region, at=at),
        kind=kind,
        region=region,
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


async def _book_stored(session: Any, asin: str, region: str) -> bool:
    """Whether the store holds this ASIN's record for exactly this region."""
    from sqlalchemy import select

    from libex_core.storage.models import Book

    result = await session.execute(
        select(Book.asin).where(Book.asin == asin, Book.region == region).limit(1)
    )
    return result.first() is not None


async def _read_books_chunked(
    session: Any, asins: list[str], region: str | None
) -> list[dict[str, Any]]:
    from libex_core.storage.read import books as read_books

    rows: list[dict[str, Any]] = []
    for start in range(0, len(asins), READ_CHUNK_SIZE):
        rows.extend(await read_books.get_books(
            session, asins[start:start + READ_CHUNK_SIZE], region=region
        ))
    return rows


async def stored_books(
    store: "LocalStore", asins: list[str], region: str | None = None
) -> list[dict[str, Any]]:
    """The stored books for these ASINs, settled, in the order asked, those
    not stored left out. A book ASIN is region-specific and a row is keyed by
    ASIN and region, so with a region only that marketplace's record is read:
    another region's is never this one's. Without a region the reader returns
    each ASIN's first-stored record. Either way the reader answers one row per
    ASIN, so the lookup by ASIN below has nothing to choose between. Read in
    chunks of READ_CHUNK_SIZE."""
    if not asins:
        return []
    rows = await _read(
        store, "books", lambda s: _read_books_chunked(s, list(asins), region), []
    )
    by_asin = {row["asin"]: row for row in rows}
    return settle_flags_list([by_asin[a] for a in asins if a in by_asin])


async def stored_walk(
    store: "LocalStore", kind: str, asin: str, region: str
) -> dict[str, Any] | None | object:
    """The raw stored snapshot row for (kind, asin, region), None when there is
    none, and _READ_FAILED when the read failed, which is not the same as
    there being none. The values are unchecked; libex_core.lookup._walks judges
    them."""
    from libex_core.storage.read import walks

    return await _read(
        store, "walk", lambda s: walks.get_walk_result(s, kind, asin, region), _READ_FAILED
    )


async def serve_merged(
    store: "LocalStore", live: list[dict[str, Any]], written: set[str], region: str
) -> list[dict[str, Any]]:
    """
    The live books, in their order, with each one that was just written replaced
    by the row the store now holds, which is the merge of what it had and what
    Audible just said. A book that was not written, or that cannot be read
    back, is served as Audible sent it. Settled.

    Only this region's record is read back, so a row another marketplace
    stored for the same ASIN is never served for this one.

    A live series entry the stored row lacks is carried over as Audible sent
    it, because an entry with no ASIN cannot be stored: the list takes
    Audible's order and the stored row's own entries after it. The rest of the
    row is the merged one.
    """
    if not written:
        return settle_flags_list(live)
    stored = {b["asin"]: b for b in await stored_books(store, sorted(written), region)}
    return settle_flags_list([
        _with_live_series(stored[b["asin"]], b) if b.get("asin") in stored else b
        for b in live
    ])


def _with_live_series(row: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """The stored row with the live book's series entries it does not hold added
    back. Entries are matched on ASIN; one the row holds is kept as stored (the
    merged value), and a live entry with no ASIN, which can never be stored,
    is always carried over. Unchanged when nothing is missing."""
    held = {e.get("asin") for e in row.get("series") or []}
    live_entries = live.get("series") or []
    if all(e.get("asin") and e["asin"] in held for e in live_entries):
        return row
    by_asin = {e.get("asin"): e for e in row.get("series") or []}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in live_entries:
        asin = entry.get("asin")
        if asin and asin in seen:
            continue
        if asin:
            seen.add(asin)
        merged.append(by_asin.get(asin, entry) if asin else entry)
    merged.extend(e for e in row.get("series") or [] if e.get("asin") not in seen)
    return {**row, "series": merged}


async def stored_series(
    store: "LocalStore", asin: str, region: str | None = None
) -> dict[str, Any] | None:
    """The stored series record: the region's when one is given, otherwise the
    first-stored record of the ASIN."""
    from libex_core.storage.read import series as read_series

    return await _read(
        store, "series", lambda s: read_series.get_series(s, asin, region=region), None
    )


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


async def search_stored_series(
    store: "LocalStore", name: str, region: str | None = None
) -> list[dict[str, Any]]:
    """Stored series matching a name, those stored for the region only when one
    is given."""
    from libex_core.storage.read import series as read_series

    return await _read(
        store,
        "series search",
        lambda s: read_series.search_series(s, name, region=region),
        [],
    )


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
    """The stored chapters. A listing belongs to one marketplace's record of the
    book: with a region, that listing; without, the first-stored one."""
    from libex_core.storage.read import books as read_books

    return await _read(
        store, "chapters", lambda s: read_books.get_track(s, asin, region=region), None
    )


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
