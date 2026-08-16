"""
Integration tests for the batched cache write, against real Postgres.

_cache_set_many is the write half of the batched cache path, and the claim
that matters about it is symmetry: a value it stores must come back from
get_many byte-for-byte, under the same key, live for the same window. That
claim spans two separately built SQL statements against the same table, and
a mocked session cannot see them disagree -- it hands back whatever the test
invented, so a batched write that stored its values under a mangled key, or
with a naive expires_at, would pass a mocked round trip while every read of
those rows missed in production. The statements are only ever reconciled by
a database, so the round trip is asserted against one.

Two of its properties are Postgres semantics rather than Python logic and
exist nowhere else to be tested: ON CONFLICT DO UPDATE raises when one
INSERT touches the same row twice, which is why duplicate keys are collapsed
before the statement is built; and expires_at is read back tz-aware from a
timestamptz column, which is what makes it comparable to the aware `now` the
read side filters on.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
import pytest
from sqlalchemy import select

# Local
from app.db.models import Cache
from app.services.cache.manager import book_key, get, get_many
from app.services.db.writer import _cache_set_many


ASIN_A = "B08G9PRS1K"
ASIN_B = "B0CACHED02"

KEY_A = book_key(ASIN_A, "us")
KEY_B = book_key(ASIN_B, "us")

# A value with the nesting, unicode, empty containers and null the real book
# DTOs carry: a round trip that only ever moves flat strings proves nothing
# about JSONB, which is what the column actually stores.
VALUE_A = {
    "asin": ASIN_A,
    "title": "Dune",
    "authors": [{"asin": "B000APF21M", "name": "Frank Herbert"}],
    "rating": 4.6,
    "lengthMinutes": 1264,
    "explicit": False,
    "subtitle": None,
    "genres": [],
    "publisher": "ハヤカワ",
    "series": {"asin": "B08G9PRS1K", "position": "1"},
}
VALUE_B = {"asin": ASIN_B, "title": "Children of Dune", "authors": []}


async def _row(session, key):
    """The stored Cache row itself, for the columns the public read side
    never exposes."""
    result = await session.execute(select(Cache).where(Cache.key == key))
    return result.scalar_one_or_none()


# ============================================================
# ROUND TRIP — WRITE BATCHED, READ BATCHED
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_batched_write_reads_back_identically_through_get_many(db_session):
    """The symmetry claim itself: every entry written in one batch comes back
    from one batched read, under its own key, equal to what went in. Asserting
    the whole mapping in one comparison also catches values that survived but
    were attached to the wrong keys."""
    await _cache_set_many(db_session, [(KEY_A, VALUE_A), (KEY_B, VALUE_B)])
    await db_session.commit()

    assert await get_many(db_session, [KEY_A, KEY_B]) == {KEY_A: VALUE_A, KEY_B: VALUE_B}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_batched_write_reads_back_identically_through_get(db_session):
    """The batched write is also symmetric with the per-key read, which is
    what the fallback paths that call `get` on a single key still use. The
    two reads must not be able to disagree about a row the batch wrote."""
    await _cache_set_many(db_session, [(KEY_A, VALUE_A), (KEY_B, VALUE_B)])
    await db_session.commit()

    assert await get(db_session, KEY_A) == VALUE_A
    assert await get(db_session, KEY_B) == VALUE_B


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_batched_write_refreshes_a_key_an_earlier_write_stored(db_session):
    """The upsert half: a key already in the table takes the new value rather
    than raising or being skipped, so a refreshed book overwrites the copy a
    previous persist stored."""
    await _cache_set_many(db_session, [(KEY_A, {"asin": ASIN_A, "title": "Stale"})])
    await db_session.commit()

    await _cache_set_many(db_session, [(KEY_A, VALUE_A)])
    await db_session.commit()

    assert await get_many(db_session, [KEY_A]) == {KEY_A: VALUE_A}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_batched_write_leaves_the_commit_to_its_caller(db_session):
    """The write does not commit: the chunk transaction that wrapped it owns
    that decision, and a rolled-back chunk must leave no cache row behind. A
    write that committed on its own would strand cache entries for books the
    same transaction failed to store."""
    await _cache_set_many(db_session, [(KEY_A, VALUE_A)])
    await db_session.rollback()

    assert await get_many(db_session, [KEY_A]) == {}


# ============================================================
# DUPLICATE KEYS — LAST WINS
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_key_repeated_in_one_batch_stores_the_last_value(db_session):
    """Postgres refuses an ON CONFLICT DO UPDATE whose INSERT would touch one
    row twice, so duplicates are collapsed before the statement is built. The
    survivor is the last of them, which is what a per-key loop over the same
    entries would have left stored."""
    first = {"asin": ASIN_A, "title": "First"}
    last = {"asin": ASIN_A, "title": "Last"}

    await _cache_set_many(db_session, [(KEY_A, first), (KEY_B, VALUE_B), (KEY_A, last)])
    await db_session.commit()

    assert await get_many(db_session, [KEY_A, KEY_B]) == {KEY_A: last, KEY_B: VALUE_B}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_batch_of_nothing_but_one_repeated_key_does_not_raise(db_session):
    """The degenerate shape a chunk of duplicate ASINs produces reaches the
    database as a single-row insert rather than an error. Left uncollapsed
    this raises CardinalityViolation, which on the chunk path would abort the
    whole transaction and discard every book in it."""
    await _cache_set_many(db_session, [(KEY_A, {"n": 1}), (KEY_A, {"n": 2}), (KEY_A, {"n": 3})])
    await db_session.commit()

    assert await get_many(db_session, [KEY_A]) == {KEY_A: {"n": 3}}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_repeated_key_leaves_exactly_one_row(db_session):
    """Collapsing the duplicates writes one row, not one row per occurrence --
    the key is the primary key, and a second row under it could not be read
    back deterministically."""
    await _cache_set_many(db_session, [(KEY_A, {"n": 1}), (KEY_A, {"n": 2})])
    await db_session.commit()

    result = await db_session.execute(select(Cache.key).where(Cache.key == KEY_A))
    assert len(result.fetchall()) == 1


# ============================================================
# TTL AND EXPIRY
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_default_ttl_is_the_configured_one(db_session):
    """With no ttl_seconds the batch takes settings.cache_ttl, the same
    default cache.set applies -- a batch primitive that quietly used a
    different window would give batched writes a different lifetime from
    identical single ones."""
    from app.services.db.writer import settings

    before = datetime.now(timezone.utc)
    await _cache_set_many(db_session, [(KEY_A, VALUE_A)])
    await db_session.commit()
    after = datetime.now(timezone.utc)

    row = await _row(db_session, KEY_A)
    assert before + timedelta(seconds=settings.cache_ttl) <= row.expires_at
    assert row.expires_at <= after + timedelta(seconds=settings.cache_ttl)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_explicit_ttl_is_the_one_stored(db_session):
    """ttl_seconds is honoured rather than ignored in favour of the default.
    The values that carry their own TTL are the ones where the default is
    wrong in the unsafe direction -- a degraded author catalogue held for a
    full day instead of its own short window is served as though whole."""
    before = datetime.now(timezone.utc)
    await _cache_set_many(db_session, [(KEY_A, VALUE_A)], ttl_seconds=60)
    await db_session.commit()
    after = datetime.now(timezone.utc)

    row = await _row(db_session, KEY_A)
    assert before + timedelta(seconds=60) <= row.expires_at <= after + timedelta(seconds=60)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expires_at_reads_back_timezone_aware(db_session):
    """The stored expiry is aware, not naive. The read side compares it to an
    aware `now`, and Postgres compares an aware value against the instant it
    names rather than against whatever local wall clock a naive one implies."""
    await _cache_set_many(db_session, [(KEY_A, VALUE_A)])
    await db_session.commit()

    row = await _row(db_session, KEY_A)
    assert row.expires_at.tzinfo is not None
    assert row.expires_at.utcoffset() is not None
    assert row.created_at.tzinfo is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_batch_expires_at_a_single_instant(db_session):
    """Every entry in a batch shares one `now`, so a chunk of books expires
    together instead of drifting key by key across the write. A batch that
    took its own clock per row would let the same chunk answer live for one
    book and expired for another."""
    await _cache_set_many(db_session, [(KEY_A, VALUE_A), (KEY_B, VALUE_B)])
    await db_session.commit()

    row_a = await _row(db_session, KEY_A)
    row_b = await _row(db_session, KEY_B)
    assert row_a.expires_at == row_b.expires_at
    assert row_a.created_at == row_b.created_at


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_already_expired_batch_entry_is_withheld_by_the_read(db_session):
    """The expiry the batch writes is the one the batched read enforces: a
    negative TTL is written and then withheld. This is the round trip closed
    on the expiry column rather than on the value."""
    await _cache_set_many(db_session, [(KEY_A, VALUE_A)], ttl_seconds=-3600)
    await _cache_set_many(db_session, [(KEY_B, VALUE_B)], ttl_seconds=3600)
    await db_session.commit()

    assert await get_many(db_session, [KEY_A, KEY_B]) == {KEY_B: VALUE_B}


# ============================================================
# EMPTY BATCH
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_empty_batch_writes_nothing_and_does_not_raise(db_session):
    """An empty entry list returns before building a statement. INSERT with
    no VALUES rows is a syntax error, so the guard is load-bearing rather
    than an optimisation -- an empty chunk would abort its transaction."""
    await _cache_set_many(db_session, [])
    await db_session.commit()

    result = await db_session.execute(select(Cache.key))
    assert result.fetchall() == []
