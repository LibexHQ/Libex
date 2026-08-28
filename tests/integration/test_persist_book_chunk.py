"""
Integration tests for the chunked background book persist, against real
Postgres.

This drives _persist_book_chunk_background, the function persist_books_background
actually calls for every chunk -- not _persist_book_chunk, which no production
caller reaches. The two differ in exactly the place that matters for a real
database: _persist_book_chunk_background builds a fresh session per attempt
from the app's own engine, where the retired caller-owned-session version took
whatever transaction the caller handed it. A mocked session cannot show which
session survived which failure or whether a fresh one is genuinely usable
after an aborted one, so both are asserted here against a real container --
the whole safety of the batch-then-replay trade rests on the replay: when a
chunk's transaction is lost, every book in it is written again down the
per-book path, so batching never costs a book that writing one at a time
would have stored. That guarantee is the aborted-transaction semantics of a
real database -- a failed statement poisons the transaction, the rollback
discards work that already succeeded within it, and the replay's upserts have
to be genuinely idempotent against rows a previous attempt may or may not
have left behind.

The failing book carries an ASIN longer than the column accepts, which is
the schema rejecting one book's data mid-chunk: the failure lands in the
middle of the loop with books written before it and books not yet attempted
after it, which is the shape that distinguishes a real replay from a chunk
that merely happened to commit. It is also not retryable (_is_retryable
returns False for a schema rejection), so the chunk falls straight to the
per-book replay after its first attempt rather than spending its retries.

The last section goes further and fails a book inside the replay itself,
which the schema cannot arrange: a book Postgres rejects fails the batched
attempt, so it is never the book the replay is midway through. Those failures
are injected, and only those -- everything they then leave behind is measured
against the same container as the rest of the file.
"""

# Standard library
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from app.db.models import Book, Cache
from app.services.cache import manager as cache_manager
from app.services.cache.manager import book_key, get_many
from app.services.cache.manager import set as cache_set
from app.services.db import persist_queue
from app.services.db.persist_queue import _persist_book_chunk_background
from app.services.db.writer import upsert_book


REGION = "us"

# Appended to a 5-character prefix this makes a 17-character ASIN against a
# 12-character column: Postgres raises rather than truncating, so exactly one
# book in the chunk fails and the rest are sound.
POISON_SUFFIX = "POISON000ONE"


def _book(prefix, index):
    """One book carrying an author as well as its own row, so the replay is
    measured over the pivot writes too and not just the books table."""
    return {
        "asin": f"{prefix}{index:04d}",
        "title": f"Book {index}",
        "region": REGION,
        "description": f"Description {index}",
        "authors": [{"asin": "B000APF21M", "name": "Frank Herbert", "region": REGION}],
    }


def _poison(prefix):
    book = _book(prefix, 9)
    book["asin"] = f"{prefix}{POISON_SUFFIX}"
    return book


def _chunk(prefix):
    """A chunk whose failing book sits in the middle: two books precede it
    and two follow it."""
    return [_book(prefix, 0), _book(prefix, 1), _poison(prefix), _book(prefix, 2), _book(prefix, 3)]


async def _stored_books(session, prefix):
    """{asin suffix: title} for the books actually in the table, keyed so two
    prefixes are comparable to each other."""
    result = await session.execute(
        select(Book.asin, Book.title).where(Book.asin.like(f"{prefix}%"))
    )
    return {row.asin[len(prefix):]: row.title for row in result}


async def _cached_books(session, prefix):
    """{asin suffix: cached value} for the cache rows written for a prefix."""
    result = await session.execute(
        select(Cache.key, Cache.value).where(Cache.key.like(f"book:{REGION}:{prefix}%"))
    )
    return {row.key[len(f"book:{REGION}:{prefix}"):]: row.value for row in result}


async def _author_links(session, prefix):
    """The asin suffixes that ended up linked to an author."""
    result = await session.execute(
        text(
            "SELECT book_asin FROM author_book WHERE book_asin LIKE :pattern"
        ),
        {"pattern": f"{prefix}%"},
    )
    return {row[0][len(prefix):] for row in result}


async def _run_per_book_path(session, chunk):
    """The unbatched path exactly as it was before chunking: one transaction
    per book row, one per cache entry."""
    for book in chunk:
        await upsert_book(session, book)
        await cache_set(session, book_key(book["asin"], REGION), book)


def _strip_prefix(value, prefix):
    """Rewrites a book payload's ASIN to its suffix so payloads written under
    two different prefixes compare equal when the outcome is the same."""
    return {**value, "asin": value["asin"][len(prefix):]}


# ============================================================
# REPLAY — EQUIVALENCE WITH THE PER-BOOK PATH
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_stores_exactly_the_books_the_per_book_path_stores(db_session):
    """The guarantee the chunking rests on: a chunk containing one
    unwritable book leaves the same books stored, with the same values, as
    running those books one at a time would have. Without the replay the
    rollback takes the four sound books with it and the batching has silently
    lost data the unbatched path kept."""
    await _persist_book_chunk_background(_chunk("B0CHK"), REGION)
    await _run_per_book_path(db_session, _chunk("B0PBK"))

    assert await _stored_books(db_session, "B0CHK") == await _stored_books(db_session, "B0PBK")
    assert await _stored_books(db_session, "B0CHK") == {
        "0000": "Book 0", "0001": "Book 1", "0002": "Book 2", "0003": "Book 3",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_writes_the_cache_entries_the_per_book_path_writes(db_session):
    """Cache entries survive the same way, and for every book in the chunk
    including the one whose row failed -- the two stores fail independently,
    so a book Audible answered stays servable from cache even when the schema
    refused its row."""
    await _persist_book_chunk_background(_chunk("B0CHK"), REGION)
    await _run_per_book_path(db_session, _chunk("B0PBK"))

    chunked = {k: _strip_prefix(v, "B0CHK") for k, v in (await _cached_books(db_session, "B0CHK")).items()}
    per_book = {k: _strip_prefix(v, "B0PBK") for k, v in (await _cached_books(db_session, "B0PBK")).items()}

    assert chunked == per_book
    assert set(chunked) == {"0000", "0001", "0002", "0003", POISON_SUFFIX}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_restores_the_pivot_rows_too(db_session):
    """The replay recovers the whole per-book write, not just the book row:
    the author links the aborted transaction discarded are written again, so
    a recovered book is queryable by its author rather than orphaned."""
    await _persist_book_chunk_background(_chunk("B0CHK"), REGION)
    await _run_per_book_path(db_session, _chunk("B0PBK"))

    assert await _author_links(db_session, "B0CHK") == await _author_links(db_session, "B0PBK")
    assert await _author_links(db_session, "B0CHK") == {"0000", "0001", "0002", "0003"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_chunk_commits_nothing_before_it_replays(db_session):
    """The fingerprint that separates a replay from a chunk that quietly
    succeeded: the failing book has a cache entry but no row. The batched
    path is all-or-nothing and writes the cache entries only after every
    book, so this combination is reachable only by the rollback happening and
    the per-book replay then running."""
    await _persist_book_chunk_background(_chunk("B0CHK"), REGION)

    poison_asin = f"B0CHK{POISON_SUFFIX}"
    stored = await db_session.execute(select(Book.asin).where(Book.asin.like("B0CHK%")))
    assert poison_asin not in {row.asin for row in stored}
    assert await get_many(db_session, [book_key(poison_asin, REGION)]) != {}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_books_after_the_failure_are_recovered_not_just_those_before_it(db_session):
    """The books positioned after the failing one were never attempted before
    the abort, so they can only be present because the replay ran the whole
    chunk again rather than resuming from where the loop stopped."""
    await _persist_book_chunk_background(_chunk("B0CHK"), REGION)

    stored = await _stored_books(db_session, "B0CHK")
    assert "0002" in stored and "0003" in stored


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_replayed_chunk_does_not_cost_the_next_chunk(db_session):
    """A persist runs many chunks in sequence, so a chunk that failed and
    replayed must not leave anything behind that costs the next one. Each
    attempt and the replay itself build their own session from the app's
    engine rather than sharing the caller's, so there is no aborted
    transaction to hand forward -- but the pool slot the failed attempt
    checked out must still come back, or enough failing chunks in a row
    would starve every chunk after them of a connection."""
    await _persist_book_chunk_background(_chunk("B0CHK"), REGION)
    await _persist_book_chunk_background([_book("B0NXT", 0)], REGION)

    assert await _stored_books(db_session, "B0NXT") == {"0000": "Book 0"}


# ============================================================
# THE UNBROKEN CHUNK
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_sound_chunk_stores_every_book_and_every_cache_entry(db_session):
    """The path taken almost every time: no book fails, one transaction
    carries all of them, and both stores end up complete."""
    chunk = [_book("B0CHK", i) for i in range(4)]

    await _persist_book_chunk_background(chunk, REGION)

    assert await _stored_books(db_session, "B0CHK") == {
        "0000": "Book 0", "0001": "Book 1", "0002": "Book 2", "0003": "Book 3",
    }
    assert set(await _cached_books(db_session, "B0CHK")) == {"0000", "0001", "0002", "0003"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_sound_chunk_caches_each_book_under_its_own_key(db_session):
    """The batched cache write attaches each book to its own key. A chunk
    that stored the right values under shifted keys would leave every later
    read serving the wrong book, which the row count alone cannot see."""
    chunk = [_book("B0CHK", i) for i in range(4)]

    await _persist_book_chunk_background(chunk, REGION)

    hits = await get_many(db_session, [book_key(b["asin"], REGION) for b in chunk])
    assert hits == {book_key(b["asin"], REGION): b for b in chunk}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replaying_a_chunk_that_already_wrote_is_idempotent(db_session):
    """Replay is only safe because every statement is an idempotent upsert.
    Writing the same chunk twice must leave one row per book with the same
    values, not duplicated pivots or a second attempt that fails on the rows
    the first left behind."""
    chunk = [_book("B0CHK", i) for i in range(3)]

    await _persist_book_chunk_background(chunk, REGION)
    first = await _stored_books(db_session, "B0CHK")
    await _persist_book_chunk_background(chunk, REGION)

    assert await _stored_books(db_session, "B0CHK") == first
    assert await _author_links(db_session, "B0CHK") == {"0000", "0001", "0002"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_chunk_write_carries_a_live_expiry(db_session):
    """The cache entries a sound chunk writes are live when it finishes --
    the batched write is what populates the cache for an Audible outage, and
    an entry written already expired would be invisible the moment it was
    needed."""
    chunk = [_book("B0CHK", 0)]

    await _persist_book_chunk_background(chunk, REGION)

    result = await db_session.execute(
        select(Cache.expires_at).where(Cache.key == book_key(chunk[0]["asin"], REGION))
    )
    assert result.scalar_one() > datetime.now(timezone.utc)


# ============================================================
# REPLAY — ONE BOOK'S FAILURE COSTS ONLY THAT BOOK
# ============================================================
#
# The replay's guarantee is per book, so it has to survive a book that fails
# inside the replay itself and not merely one the batched attempt rejected.
# Two failures are injected here because they are the two the loop cannot
# handle by doing nothing: cache.set commits a statement of its own, and
# upsert_book's rollback runs on a connection that may already be gone. Both
# are injected at the name the replay resolves -- cache.manager.set is looked
# up on the module every call, and upsert_book is bound into persist_queue.
# Only the second of those is narrow: rebinding upsert_book reaches nothing
# outside persist_queue, while rebinding cache.manager.set is visible to
# every caller that looks the name up the same way, and what keeps it from
# reaching one is the with block rather than the patch site. The chunk still
# carries its poison book: without it the batched attempt commits and the
# replay never runs at all.


def _cache_failing_on(asin):
    """
    cache.set with a genuine database failure on one book's key.

    The failure is a real statement error rather than a raise from the mock,
    because it is the harder case and the one production sees: the cache write
    is a statement in the session's transaction, so a failure aborts that
    transaction rather than leaving it usable. Division by zero is the
    cheapest way to make Postgres itself refuse a statement.
    """
    async def _set(session, key, value, ttl_seconds=None):
        if key == book_key(asin, REGION):
            await session.execute(text("SELECT 1 / 0"))
        return await cache_set(session, key, value, ttl_seconds)

    return _set


def _upsert_escaping_on(*asins):
    """
    upsert_book raising for the named books instead of keeping its failure to
    itself.

    Its own handler swallows the write failure, so the only way past it is the
    rollback in that handler failing too -- which is what a connection that
    died under the statement does. InterfaceError is what that surfaces as,
    carrying an orig with no SQLSTATE, exactly as a dead connection does.
    """
    async def _upsert(session, data):
        if data.get("asin") in asins:
            raise InterfaceError("ROLLBACK", None, Exception("connection was closed"))
        return await upsert_book(session, data)

    return _upsert


def _upsert_escaping_on_an_aborted_session(asin):
    """
    upsert_book escaping with the session's transaction already aborted.

    The narrower shape of the same escape, and the one a bare raise cannot
    stand in for. upsert_book's handler re-raises a failed rollback before
    SQLAlchemy has unwound to the root transaction, so a SAVEPOINT that fails
    to roll back -- upsert_author opens one per new or still-null-asin author
    -- leaves the exception on its way out and the session still carrying an
    aborted transaction. Whether the replay clears the session on that path is
    only observable from here: with a raise alone there is nothing to clear.
    """
    async def _upsert(session, data):
        if data.get("asin") == asin:
            try:
                await session.execute(text("SELECT 1 / 0"))
            except Exception as aborted:
                raise InterfaceError(
                    "ROLLBACK", None, Exception("connection was closed")
                ) from aborted
        return await upsert_book(session, data)

    return _upsert


def _rollback_always_failing():
    """
    Every rollback on every session raising, which is what a connection that
    has gone rather than merely refused a statement does.

    Patched on AsyncSession itself because the sessions this reaches are built
    inside the code under test and never handed in. It is scoped to the one
    call: the db_session fixture rolls back in its own teardown, outside the
    patch.
    """
    return AsyncMock(side_effect=InterfaceError(
        "ROLLBACK", None, Exception("connection was closed")
    ))


def _cache_failure_chunk(prefix):
    """A chunk whose first book fails its cache write, with three sound books
    behind it and the poison book last. The failure is at the front so every
    other book in the chunk is positioned after it."""
    return [_book(prefix, i) for i in range(4)] + [_poison(prefix)]


def _warnings_saying(caplog, message):
    """The warning records for one message, with their extra fields intact."""
    return [r for r in caplog.records if r.levelno >= logging.WARNING and r.getMessage() == message]


def _the_record_saying(caplog, message):
    """The single record for one message, asserting it is single: a summary
    line emitted twice, or once per book, would be a different thing from the
    one this file measures."""
    records = [r for r in caplog.records if r.getMessage() == message]
    assert len(records) == 1
    return records[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_cache_write_still_leaves_the_book_stored(db_session):
    """The row is the durable copy and it is committed before the cache entry
    is attempted, so a cache write that fails must not cost the store that
    already succeeded. Read back on a different session than the replay used,
    which is what makes it a commit and not an uncommitted row visible to its
    own transaction."""
    with patch.object(cache_manager, "set", _cache_failing_on("B0CHK0000")):
        await _persist_book_chunk_background(_cache_failure_chunk("B0CHK"), REGION)

    assert (await _stored_books(db_session, "B0CHK"))["0000"] == "Book 0"
    # The failure was real and not quietly swallowed upstream: had the cache
    # write actually run, this key would be present like every other book's.
    assert "0000" not in await _cached_books(db_session, "B0CHK")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_books_after_a_cache_failure_are_still_stored(db_session):
    """
    The cache-side twin of the DB-side test above: a book whose cache write
    fails must not decide the outcome of the books behind it in the chunk.
    Unguarded, the raise ends the loop and the whole tail of the chunk is
    never attempted at all.

    The book immediately behind the failure is the one that matters, and it is
    the reason the failure here is a real statement error rather than a raise
    from the mock. cache.set's INSERT runs inside the session's transaction,
    so a failure at the statement leaves that transaction aborted, and the
    next book's first statement dies of 25P02 with nothing wrong with the
    book -- catching the cache failure without clearing the session costs
    exactly one more book every time. The rollback in the cache handler is
    what closes that, and only this failure mode can tell: a mock raising
    before it touches the session leaves nothing to roll back and passes at
    this distance whether the rollback is there or not.

    Every sound book in the chunk is therefore expected, and the poison book
    is the only absence. What this cannot promise, and does not assert, is the
    same independence when the rollback itself fails on a connection that is
    gone rather than merely aborted -- the remaining books fail through their
    own guards then, three lines apiece: upsert_book's own record of the write
    it could not make, the replay's record of that failure escaping, and the
    clear that could not run either.
    """
    with patch.object(cache_manager, "set", _cache_failing_on("B0CHK0000")):
        await _persist_book_chunk_background(_cache_failure_chunk("B0CHK"), REGION)

    assert await _stored_books(db_session, "B0CHK") == {
        "0000": "Book 0", "0001": "Book 1", "0002": "Book 2", "0003": "Book 3",
    }
    # The cache path recovered too, rather than the loop limping on writing
    # rows and skipping every entry after the one that failed. "0000" is
    # absent by construction: its cache write is the one that failed.
    assert {"0001", "0002", "0003"} <= set(await _cached_books(db_session, "B0CHK"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_cache_failure_does_not_cost_the_chunks_after_it(db_session):
    """
    The larger half of the blast radius. A chunk's replay runs outside its
    caller's retry loop and that caller has no handler of its own, so an
    escape does not stop at this chunk -- it unwinds into the batch loop in
    persist_books_background, which abandons every chunk it had not started.
    A prolific author's catalog is twenty-odd chunks, so one book's cache
    write costs the thousand books behind it.

    Driven through persist_books_background rather than a chunk call, since
    the loop being protected is the one that slices the batch. Its background
    task is taken from the queue's own in-flight set and awaited, so the batch
    runs through the real spawn path and the assertions run after the writes
    rather than racing them.

    The chunk size is narrowed for the same reason the chunk here is only
    four books: what is being measured is that a second chunk runs at all,
    which the width of the first has no bearing on. At the real size this
    costs a fifty-book replay a book at a time -- five seconds of the thirty
    the suite's runaway tripwire allows, for no assertion the four-book
    version does not already make. That the real constant is what slices a
    real batch is covered in tests/test_db_writer.py.
    """
    batch = [_book("B0CHK", i) for i in range(3)]
    batch.append(_poison("B0CHK"))
    batch.append(_book("B0NXT", 0))

    with patch.object(persist_queue, "_PERSIST_CHUNK_SIZE", len(batch) - 1), \
         patch.object(cache_manager, "set", _cache_failing_on("B0CHK0000")):
        before = set(persist_queue._inflight)
        persist_queue.persist_books_background(batch, REGION)
        spawned = set(persist_queue._inflight) - before
        assert len(spawned) == 1
        await spawned.pop()

    assert await _stored_books(db_session, "B0NXT") == {"0000": "Book 0"}
    assert set(await _cached_books(db_session, "B0NXT")) == {"0000"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_write_failure_that_escapes_upsert_book_costs_only_that_book(db_session):
    """
    upsert_book handles its own write failure, but the rollback it takes on
    the way out runs on the same connection that just failed and can raise in
    turn -- and that raise is outside its handler. The replay has to contain
    it the same way it contains a cache failure, or the connection dying under
    one book takes the chunk and every chunk after it.

    The book that escaped gets neither a row nor a cache entry, and the
    absent cache entry is a consequence rather than a policy: the guard sends
    the loop on to the next book, and the cache write is simply the statement
    it did not reach. It is not that a row-less book should not be cached --
    the test above stores a cache entry for the poison book on purpose, since
    a book with no row is exactly the book that the DB has no answer for when
    Audible is down.
    """
    chunk = [_book("B0CHK", i) for i in range(3)] + [_poison("B0CHK")]

    with patch.object(persist_queue, "upsert_book", _upsert_escaping_on("B0CHK0000")):
        await _persist_book_chunk_background(chunk, REGION)

    stored = await _stored_books(db_session, "B0CHK")
    assert "0000" not in stored
    assert stored["0001"] == "Book 1"
    assert stored["0002"] == "Book 2"

    cached = await _cached_books(db_session, "B0CHK")
    assert "0000" not in cached
    assert {"0001", "0002"} <= set(cached)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_contained_cache_failure_names_the_book_it_lost(db_session, caplog):
    """
    Containing the failure is only half of it: a cache entry silently missing
    for one book out of a thousand is invisible, so the record has to carry
    the ASIN that failed. Without it the operator knows a book somewhere in
    the batch has no cache entry and has no way to find which.

    db_session is taken for its teardown, not to read from. It is what
    truncates the tables, it is not autouse, and this test commits rows like
    every other one here -- without it the books and cache entries below stay
    in the container for whatever runs next.
    """
    with caplog.at_level(logging.WARNING):
        with patch.object(cache_manager, "set", _cache_failing_on("B0CHK0000")):
            await _persist_book_chunk_background(_cache_failure_chunk("B0CHK"), REGION)

    records = _warnings_saying(caplog, "Background persist replay failed to cache book")
    assert [r.asin for r in records] == ["B0CHK0000"]
    assert records[0].region == REGION
    # The SQLSTATE the server actually returned, so the entry says what kind
    # of failure it was and not merely that one happened.
    assert records[0].sqlstate == "22012"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_contained_write_failure_names_the_book_it_lost(db_session, caplog):
    """The same for the write side. This is the only record of the book: it
    has no row, no cache entry, and the replay's closing line counts it
    without naming it. db_session is taken for its truncating teardown, as
    above."""
    chunk = [_book("B0CHK", i) for i in range(3)] + [_poison("B0CHK")]

    with caplog.at_level(logging.WARNING):
        with patch.object(persist_queue, "upsert_book", _upsert_escaping_on("B0CHK0000")):
            await _persist_book_chunk_background(chunk, REGION)

    records = _warnings_saying(caplog, "Background persist replay failed for book")
    assert [r.asin for r in records] == ["B0CHK0000"]
    assert records[0].region == REGION
    assert records[0].error_type == "InterfaceError"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_write_failure_that_escapes_leaves_the_session_usable(db_session):
    """
    The write side of the same collateral the cache side had. Catching the
    escape is not enough on its own when the session it escaped from is
    carrying an aborted transaction: the next book's first statement dies of
    25P02 with nothing wrong with the book, and one failure has decided
    another book's outcome after all.

    The book directly behind the failure is what says whether the handler
    clears the session, and it can only say so against a failure that left
    something to clear -- which is why this test injects an aborted
    transaction and the test above injects a bare raise. The two are the same
    escape at different depths, and only this one can tell the handlers apart.
    """
    chunk = _cache_failure_chunk("B0CHK")

    with patch.object(
        persist_queue, "upsert_book", _upsert_escaping_on_an_aborted_session("B0CHK0000")
    ):
        await _persist_book_chunk_background(chunk, REGION)

    assert await _stored_books(db_session, "B0CHK") == {
        "0001": "Book 1", "0002": "Book 2", "0003": "Book 3",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clearing_the_session_keeps_what_earlier_books_already_committed(db_session):
    """
    The direction the tests above do not look in. They fail the first book of
    the chunk, so there is never a book behind the rollback -- and a rollback
    that reached backwards would be data loss rather than a lost book, since
    the entry it discarded belongs to a book that was written successfully and
    reported as written.

    Failing the second book puts a completed book on the other side of it. The
    clear can only be safe because each book's two writes are committed by the
    time the loop leaves them: upsert_book commits its own transaction and
    cache.set commits its own statement, so the rollback the next book's
    handler takes has nothing of theirs left to discard. Were either write
    still open at that point -- one transaction spanning the replay, a cache
    write that stopped committing -- this is the assertion that would notice.

    The failure aborts the session rather than merely raising, because a
    rollback with nothing to roll back cannot show any of this.
    """
    chunk = _cache_failure_chunk("B0CHK")

    with patch.object(
        persist_queue, "upsert_book", _upsert_escaping_on_an_aborted_session("B0CHK0001")
    ):
        await _persist_book_chunk_background(chunk, REGION)

    assert await _stored_books(db_session, "B0CHK") == {
        "0000": "Book 0", "0002": "Book 2", "0003": "Book 3",
    }

    cached = await _cached_books(db_session, "B0CHK")
    # The book behind the failure keeps both halves of its write, the failed
    # book has neither, and the poison book keeps the cache entry its missing
    # row makes it need -- the DB has no answer for it, so the cache is the
    # only one there is.
    assert set(cached) == {"0000", "0002", "0003", POISON_SUFFIX}
    # The value and not merely the key: an entry left holding a partial or
    # stale payload is a worse outcome than the missing one this looks for.
    assert cached["0000"] == chunk[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rollback_that_fails_is_reported_and_contained(db_session, caplog):
    """
    The last thing on this path with nothing behind it. Every guard here ends
    in a rollback, and the rollback is itself work against the connection, so
    on a connection that is gone rather than merely unhappy it raises out of
    the very handler that exists to contain the first failure.

    Both sites are exercised at once, which is the point of driving the whole
    chunk rather than either function alone. The batched attempt runs for
    every chunk Libex writes and takes its rollback whenever one of them
    fails; the per-book handler is reached only once a chunk has fallen to the
    replay, which a non-retryable failure does after a single attempt -- the
    usual route, and this chunk's -- and a retryable one after three. Neither
    may escape, and each says which unit of work it was cleaning up after --
    the attempt knows only its region, the per-book handler names the book.

    The sound books still store: nothing rolls back a write that succeeded,
    so a broken rollback costs only the books that were failing anyway.
    """
    with caplog.at_level(logging.WARNING):
        with patch.object(AsyncSession, "rollback", _rollback_always_failing()):
            await _persist_book_chunk_background(_cache_failure_chunk("B0CHK"), REGION)

    records = _warnings_saying(caplog, "Background persist could not clear the session")

    from_the_attempt = [r for r in records if not hasattr(r, "asin")]
    assert [r.region for r in from_the_attempt] == [REGION]

    per_book = {getattr(r, "asin", None) for r in records if hasattr(r, "asin")}
    assert per_book == {f"B0CHK{POISON_SUFFIX}"}

    assert await _stored_books(db_session, "B0CHK") == {
        "0000": "Book 0", "0001": "Book 1", "0002": "Book 2", "0003": "Book 3",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_rollback_after_a_cache_failure_is_reported_and_contained(db_session, caplog):
    """
    The cache handler's own rollback, which the test above never reaches: the
    only book failing there fails on the write side, so the cache handler is
    never entered and an unguarded rollback in it would go unnoticed.

    Nothing is asserted here about what ended up stored, deliberately. Once
    the rollback is broken the aborted transaction cannot be cleared at all,
    so the books behind this one fail too -- that is the residue the replay
    documents rather than a property it promises, and pinning it would be
    asserting an independence that does not exist on a dead connection. What
    must hold is that none of it escapes and that the book is still named.
    """
    with caplog.at_level(logging.WARNING):
        with patch.object(cache_manager, "set", _cache_failing_on("B0CHK0000")), \
             patch.object(AsyncSession, "rollback", _rollback_always_failing()):
            await _persist_book_chunk_background(_cache_failure_chunk("B0CHK"), REGION)

    records = _warnings_saying(caplog, "Background persist could not clear the session")
    assert "B0CHK0000" in {getattr(r, "asin", None) for r in records}


# ============================================================
# REPLAY — WHAT THE SUMMARY LINE CLAIMS
# ============================================================
#
# The closing line is the only per-chunk record of a degraded replay, and its
# two counters count narrower things than their names suggest to a reader who
# has not read the function. Both are pinned against the same chunk shape from
# opposite sides, so the increments cannot be swapped without a failure here.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_replay_summary_counts_the_writes_that_escaped(db_session, caplog):
    """
    write_escaped counts the failures that got past upsert_book's own handler,
    which is every book here because every one of them is injected to escape.

    cache_failed is zero rather than four: the escape path never reaches the
    cache write, so a chunk that cached nothing at all reports no cache
    failures. That is the honest reading of both fields and the reason to
    assert them together.
    """
    chunk = [_book("B0CHK", i) for i in range(3)] + [_poison("B0CHK")]
    every_asin = [b["asin"] for b in chunk]

    with caplog.at_level(logging.INFO):
        with patch.object(persist_queue, "upsert_book", _upsert_escaping_on(*every_asin)):
            await _persist_book_chunk_background(chunk, REGION)

    record = _the_record_saying(caplog, "Background persist replay complete")
    assert record.levelno == logging.INFO
    assert (record.books, record.region, record.write_escaped, record.cache_failed) == (
        4, REGION, 4, 0,
    )
    assert await _stored_books(db_session, "B0CHK") == {}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_replay_summary_counts_the_cache_writes_that_failed(db_session, caplog):
    """
    The mirror, and the one that shows what write_escaped is not. The poison
    book fails to store in this chunk -- upsert_book catches its own failure
    and reports nothing back -- so write_escaped reads zero over a replay that
    lost a book. Anything downstream treating that field as a count of books
    that failed to store would read this chunk as clean.
    """
    chunk = _cache_failure_chunk("B0CHK")

    with caplog.at_level(logging.INFO):
        with patch.object(cache_manager, "set", _cache_failing_on("B0CHK0000")):
            await _persist_book_chunk_background(chunk, REGION)

    record = _the_record_saying(caplog, "Background persist replay complete")
    assert (record.books, record.region, record.write_escaped, record.cache_failed) == (
        5, REGION, 0, 1,
    )
    # The book the counters do not mention, proving the point above rather
    # than asserting it in prose alone.
    assert f"{POISON_SUFFIX}" not in await _stored_books(db_session, "B0CHK")
