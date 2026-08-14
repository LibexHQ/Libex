"""
Integration tests for the /db/stats caching ordering: on a live-query
failure, the all-zeros fallback must never be written into the cache.

Verifies the real, persisted state of the `cache` table against real
Postgres rather than asserting a call was or wasn't made — a mocked session
can only ever show that `cache.set` was (or wasn't) invoked, and a session
that raises for every call makes any fall-through swallow itself the same
way whether or not the guard exists, which is exactly what let this gap
through the full mocked suite untouched (measured: 913 passed against a
folded early-return).
"""

# Standard library

# Third party
import pytest
from sqlalchemy import select

# Local
from app.db.models import Cache
from app.services.cache import manager as cache_module
from app.services.db.reader import get_db_stats


class _FailOnceSession:
    """
    Wraps a real, live-Postgres AsyncSession and raises a plain Python
    exception on the Nth execute() call, without ever sending that
    statement to Postgres — modelling a transient failure that happens
    before the query reaches the wire (a driver-side timeout, a dropped
    connection acquired fresh for the next statement) rather than a
    mid-transaction Postgres error. That distinction matters here: a
    genuine mid-transaction Postgres abort would also poison the
    subsequent cache.set on the same session, which is exactly the shape
    the existing raise-on-every-call mock already covers and cannot fail
    under this mutation. This wrapper leaves the real connection healthy,
    so a `cache.set` reached after the failure is a real write against
    real Postgres, and the resulting row (or its absence) is the actual
    property under test.
    """

    def __init__(self, session, fail_on_call: int, error: Exception):
        self._session = session
        self._fail_on_call = fail_on_call
        self._error = error
        self._calls = 0

    async def execute(self, *args, **kwargs):
        self._calls += 1
        if self._calls == self._fail_on_call:
            raise self._error
        return await self._session.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_query_failure_leaves_no_stats_row_in_cache(db_session):
    """
    A live-count failure (the second session.execute call, right after the
    cache-miss read) must return the all-zeros fallback WITHOUT the zeros
    ever landing in the real `cache` table — the row for "db_stats" must
    not exist afterward, since a written row would serve stale zeros to
    every request for the full TTL, including the public README badges.
    """
    wrapped = _FailOnceSession(
        db_session, fail_on_call=2, error=Exception("simulated transient DB read failure")
    )

    result = await get_db_stats(wrapped)

    assert result == {
        "books": 0,
        "authors": 0,
        "narrators": 0,
        "series": 0,
        "booksWithChapters": 0,
    }

    row = await db_session.execute(
        select(Cache).where(Cache.key == cache_module.stats_key())
    )
    assert row.scalar_one_or_none() is None
