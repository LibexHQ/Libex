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
"""

# Standard library
from datetime import datetime, timezone

# Third party
import pytest
from sqlalchemy import select, text

# Local
from app.db.models import Book, Cache
from app.services.cache.manager import book_key, get_many
from app.services.cache.manager import set as cache_set
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
