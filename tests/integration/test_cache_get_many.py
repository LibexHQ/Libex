"""
Integration tests for the batched cache read, against real Postgres.

get_many answers a whole key list in one query, and the two properties
that matter most about it are invisible to a mocked session: whether an
expired row is genuinely withheld, and whether the batched answer agrees
with what get() would have returned key by key. Both are decided by SQL
the database evaluates -- a mocked session hands back whatever rows the
test invented no matter what the WHERE clause says, so a get_many that
dropped the expiry predicate entirely would sail through the mocked
suite while serving stale entries in production. That is a silent
correctness failure, not a performance one, which is why it is pinned
here as well as structurally in tests/services/test_cache_operations.py.
"""

# Third party
import pytest

# Local
from app.services.cache.manager import book_key, get, get_many, set as cache_set


LIVE = book_key("B08G9PRS1K", "us")
EXPIRED = book_key("B000EXPIRE", "us")
ABSENT = book_key("B000ABSENT", "us")

LIVE_VALUE = {"asin": "B08G9PRS1K", "title": "Dune"}
EXPIRED_VALUE = {"asin": "B000EXPIRE", "title": "Stale Title"}


async def _seed(session):
    """One live entry and one already-expired entry, written through the
    real cache writer so the rows carry exactly the columns production
    rows carry."""
    await cache_set(session, LIVE, LIVE_VALUE, ttl_seconds=3600)
    await cache_set(session, EXPIRED, EXPIRED_VALUE, ttl_seconds=-3600)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_many_withholds_an_expired_entry(db_session):
    """An entry whose expires_at has passed is not a hit, even when its
    key is in the batch alongside live keys. Returning it would serve a
    stale value that the caller cannot tell from a fresh one."""
    await _seed(db_session)

    hits = await get_many(db_session, [LIVE, EXPIRED, ABSENT])

    assert hits.keys() == {LIVE}
    assert hits[LIVE] == LIVE_VALUE
    assert hits.get(EXPIRED) is None
    assert hits.get(ABSENT) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_many_answers_exactly_as_get_does_key_by_key(db_session):
    """The batched read is a drop-in for a per-key loop over get: for every
    key -- live, expired, absent -- hits.get(key) equals what get() returns
    for that same key against the same rows. Asserting the whole
    mapping at once, rather than key by key, also catches a get_many that
    got each key right individually but attached the values to the wrong
    keys."""
    await _seed(db_session)

    keys = [LIVE, EXPIRED, ABSENT]
    hits = await get_many(db_session, keys)
    per_key = {key: await get(db_session, key) for key in keys}

    assert {key: hits.get(key) for key in keys} == per_key


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_many_handles_a_repeated_key(db_session):
    """A key repeated in the batch resolves to the one entry it names --
    the keys are collapsed before the query, and the caller reads values
    back by key."""
    await _seed(db_session)

    hits = await get_many(db_session, [LIVE, ABSENT, LIVE])

    assert hits == {LIVE: LIVE_VALUE}
