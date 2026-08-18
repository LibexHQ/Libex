"""
Cache operation unit tests.
Tests get, set, invalidate, and purge with mocked database sessions.
"""

# Standard library
import operator
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from app.db.models import Cache
from app.services.cache.manager import get, get_many, set, invalidate, purge_expired


# ============================================================
# CACHE GET TESTS
# ============================================================

@pytest.mark.asyncio
async def test_cache_get_returns_none_on_miss():
    """Cache get returns None when key not found."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    result = await get(session, "book:us:B08G9PRS1K")
    assert result is None


@pytest.mark.asyncio
async def test_cache_get_returns_value_on_hit():
    """Cache get returns cached value when key found."""
    session = AsyncMock()
    mock_entry = MagicMock()
    mock_entry.value = {"asin": "B08G9PRS1K", "title": "Dune"}
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_entry
    session.execute = AsyncMock(return_value=mock_result)

    result = await get(session, "book:us:B08G9PRS1K")
    assert result == {"asin": "B08G9PRS1K", "title": "Dune"}


@pytest.mark.asyncio
async def test_cache_get_calls_execute():
    """Cache get executes a database query."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    await get(session, "book:us:B08G9PRS1K")
    session.execute.assert_called_once()


# ============================================================
# CACHE GET_MANY TESTS
# ============================================================
# get_many answers a whole key list from one query, so what a mocked
# session can show is the mapping it builds from the rows it gets back
# and the query it sends. Whether Postgres actually withholds an expired
# row is not visible here -- the expiry filter is evaluated in SQL, and a
# mocked session hands back whatever rows the test invented regardless of
# the predicate. That property is pinned for real against a live database
# in tests/integration/test_cache_get_many.py; the last two tests in this
# section are structural proxies for it and for the deduplication, useful
# because they run in the main (non-integration) job.


def _cache_row(key, value):
    """A stand-in for one row of get_many's (key, value) result set."""
    row = MagicMock()
    row.key = key
    row.value = value
    return row


def _rows_result(*rows):
    """Mock execute() return value that iterates as the given rows."""
    result = MagicMock()
    result.__iter__.return_value = iter(rows)
    return result


def _executed_where(session):
    """The WHERE clauses of the statement the session was asked to execute.
    A single-condition WHERE is one expression rather than a clause list, so
    unwrap defensively -- a test looking for the key list should report a
    missing expiry clause as the expiry test failing, not as an attribute
    error over in the deduplication test."""
    where = session.execute.call_args.args[0].whereclause
    return list(getattr(where, "clauses", [where]))


def _executed_key_chunks(session):
    """The key list bound into each statement the session was asked to
    execute, one entry per call, so a chunked call reads back as its
    chunks and an unchunked one as a single oversized list."""
    chunks = []
    for call in session.execute.call_args_list:
        where = call.args[0].whereclause
        clauses = list(getattr(where, "clauses", [where]))
        in_clause = next(c for c in clauses if c.operator.__name__ == "in_op")
        chunks.append(in_clause.right.value)
    return chunks


@pytest.mark.asyncio
async def test_cache_get_many_returns_empty_dict_for_no_keys():
    """get_many returns {} when handed no keys."""
    session = AsyncMock()
    session.execute = AsyncMock()

    result = await get_many(session, [])
    assert result == {}


@pytest.mark.asyncio
async def test_cache_get_many_fires_no_query_for_no_keys():
    """An empty key list short-circuits before the database round trip --
    an IN () query would cost a round trip to answer nothing."""
    session = AsyncMock()
    session.execute = AsyncMock()

    await get_many(session, [])
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_cache_get_many_maps_each_key_to_its_own_value():
    """Each key maps to ITS value, not merely to some value -- values
    swapped between two keys is a shape-correct result and a wrong one."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_rows_result(
        _cache_row("book:us:B08G9PRS1K", {"asin": "B08G9PRS1K", "title": "Dune"}),
        _cache_row("book:us:B000000002", {"asin": "B000000002", "title": "Elantris"}),
    ))

    result = await get_many(session, ["book:us:B08G9PRS1K", "book:us:B000000002"])
    assert result == {
        "book:us:B08G9PRS1K": {"asin": "B08G9PRS1K", "title": "Dune"},
        "book:us:B000000002": {"asin": "B000000002", "title": "Elantris"},
    }


@pytest.mark.asyncio
async def test_cache_get_many_omits_keys_that_did_not_hit():
    """Only hits appear in the returned dict, so result.get(key) is None
    for exactly the keys get() would have returned None for -- callers
    branch on that None, and a key present with a None value would read
    as a hit holding nothing."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_rows_result(
        _cache_row("book:us:B08G9PRS1K", {"title": "Dune"}),
    ))

    keys = ["book:us:B08G9PRS1K", "book:us:BMISSING01", "book:us:BMISSING02"]
    result = await get_many(session, keys)

    # .keys() rather than set(): this module imports the cache manager's
    # own set(), which shadows the builtin.
    assert result.keys() == {"book:us:B08G9PRS1K"}
    assert result.get("book:us:BMISSING01") is None
    assert result.get("book:us:BMISSING02") is None


@pytest.mark.asyncio
async def test_cache_get_many_uses_one_query_for_the_whole_key_list():
    """One query for a whole key list rather than one per key -- the whole
    reason get_many exists. The bulk book route's 1000-ASIN cap is not the
    bound on that list: the author routes hand it a stored catalogue with
    no limit of their own, thousands of keys, and a per-key query there
    would put the N+1 straight back. Past 5000 keys the list is chunked,
    which the two tests below pin."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_rows_result())

    await get_many(session, [f"book:us:B{i:09d}" for i in range(25)])
    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_cache_get_many_binds_each_repeated_key_once():
    """Repeated keys are collapsed before the query. Structural: reads the
    IN list off the statement, since a duplicated bind produces the same
    answer and only shows up as a wider query."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_rows_result())

    await get_many(session, ["book:us:B0DUPE0001", "book:us:B0OTHER001", "book:us:B0DUPE0001"])

    in_clause = next(c for c in _executed_where(session) if c.operator.__name__ == "in_op")
    assert in_clause.right.value == ["book:us:B0DUPE0001", "book:us:B0OTHER001"]


@pytest.mark.asyncio
async def test_cache_get_many_query_is_bounded_by_the_expiry_predicate():
    """The query filters on expires_at, so an expired row cannot come back
    as a hit. Structural proxy only -- it asserts the predicate is in the
    statement, not that Postgres honours it; the behavioural pin is the
    live-database test in tests/integration/test_cache_get_many.py. It
    earns its place in this suite because serving an expired entry is a
    silent staleness bug, and this is the one form of alarm for it that
    runs without Docker."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_rows_result())

    await get_many(session, ["book:us:B08G9PRS1K"])

    expiry = next(
        c for c in _executed_where(session)
        if getattr(c.left, "key", None) == "expires_at"
    )
    assert expiry.operator is operator.gt
    assert expiry.left.table is Cache.__table__
    assert expiry.right.value.tzinfo is not None


@pytest.mark.asyncio
async def test_cache_get_many_sends_one_statement_at_the_chunk_ceiling():
    """5000 keys is the chunk size, so they still travel as one statement.
    The ceiling exists because asyncpg refuses more than 32767 bind
    parameters in a statement, and get_many's busiest caller is the outage
    fallback in get_books_by_asins -- an unchunked IN list there raises from
    inside the handler whose whole job is to degrade gracefully, turning an
    Audible outage into a 500."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=lambda _stmt: _rows_result())

    await get_many(session, [f"book:us:B{i:09d}" for i in range(5000)])

    assert [len(chunk) for chunk in _executed_key_chunks(session)] == [5000]


@pytest.mark.asyncio
async def test_cache_get_many_splits_the_key_list_one_past_the_ceiling():
    """One key past the ceiling is two statements, split 5000 then 1. The
    boundary is the assertion: a test at some huge key count asserting
    merely 'more than one statement' passes against a chunk size of 2 and
    against one of 30000, and the second of those still raises. The
    concatenation check is what proves chunking splits the list rather than
    losing part of it -- a slice that drops keys returns fewer hits and
    reads to the caller as a cache miss."""
    keys = [f"book:us:B{i:09d}" for i in range(5001)]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=lambda _stmt: _rows_result())

    await get_many(session, keys)

    chunks = _executed_key_chunks(session)
    assert [len(chunk) for chunk in chunks] == [5000, 1]
    assert [key for chunk in chunks for key in chunk] == keys


# ============================================================
# CACHE SET TESTS
# ============================================================

@pytest.mark.asyncio
async def test_cache_set_calls_execute():
    """Cache set executes a database upsert."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await set(session, "book:us:B08G9PRS1K", {"title": "Dune"})
    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_cache_set_calls_commit():
    """Cache set commits the transaction."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await set(session, "book:us:B08G9PRS1K", {"title": "Dune"})
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_cache_set_uses_default_ttl():
    """Cache set uses default TTL from settings when not specified."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    with patch("app.services.cache.manager.settings") as mock_settings:
        mock_settings.cache_ttl = 86400
        await set(session, "book:us:B08G9PRS1K", {"title": "Dune"})
        session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_cache_set_accepts_custom_ttl():
    """Cache set accepts custom TTL override."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await set(session, "book:us:B08G9PRS1K", {"title": "Dune"}, ttl_seconds=3600)
    session.execute.assert_called_once()


# ============================================================
# CACHE INVALIDATE TESTS
# ============================================================

@pytest.mark.asyncio
async def test_cache_invalidate_calls_execute():
    """Cache invalidate executes a delete query."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await invalidate(session, "book:us:B08G9PRS1K")
    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_cache_invalidate_calls_commit():
    """Cache invalidate commits the transaction."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await invalidate(session, "book:us:B08G9PRS1K")
    session.commit.assert_called_once()


# ============================================================
# CACHE PURGE TESTS
# ============================================================
#
# purge_expired sweeps ctid ascending with two statements per batch: a SELECT
# of candidate ctids, then a range DELETE bounded by the cursor and the last
# of them. These mocks answer both, in order. Where the sweep STOPS is the
# property worth pinning -- it ends when the candidate SELECT comes back
# empty, not when a DELETE removes nothing, because a batch can legitimately
# delete nothing when every candidate was refreshed between the two
# statements, and stopping there would abandon the rest of the table.


def _purge_execute(batches):
    """A session.execute answering a SELECT then a DELETE per batch.

    batches is a list of (candidate_ctids, rows_deleted); the sweep ends on
    the first entry with no candidates.
    """
    calls = []

    async def _execute(stmt, params=None):
        calls.append(params or {})
        step = len(calls) - 1
        idx = step // 2
        ctids, deleted = batches[idx] if idx < len(batches) else ([], 0)
        result = MagicMock()
        if step % 2 == 0:
            result.__iter__.return_value = iter([(c,) for c in ctids])
        else:
            result.rowcount = deleted
        return result

    return AsyncMock(side_effect=_execute), calls


@pytest.mark.asyncio
async def test_cache_purge_calls_execute():
    """Cache purge issues its candidate query."""
    session = AsyncMock()
    session.execute, _ = _purge_execute([([], 0)])
    session.commit = AsyncMock()

    await purge_expired(session)
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_cache_purge_calls_commit():
    """A batch commits on its own, so a later failure keeps what already
    went."""
    session = AsyncMock()
    session.execute, _ = _purge_execute([(["(0,1)"], 1), ([], 0)])
    session.commit = AsyncMock()

    await purge_expired(session)
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_cache_purge_returns_rowcount():
    """The total sums across batches rather than reporting only the first."""
    session = AsyncMock()
    session.execute, _ = _purge_execute([(["(0,1)"], 42), (["(0,9)"], 8), ([], 0)])
    session.commit = AsyncMock()

    count = await purge_expired(session)
    assert count == 50


@pytest.mark.asyncio
async def test_cache_purge_sweeps_past_a_batch_that_deleted_nothing():
    """A batch can delete nothing because every candidate was refreshed
    between the SELECT and the DELETE. Stopping there would abandon every
    expired row further down the table."""
    session = AsyncMock()
    session.execute, _ = _purge_execute([(["(0,1)"], 0), (["(0,9)"], 7), ([], 0)])
    session.commit = AsyncMock()

    count = await purge_expired(session)
    assert count == 7


@pytest.mark.asyncio
async def test_cache_purge_carries_the_cursor_forward():
    """Each batch resumes after the last ctid the previous one SELECTED, not
    the last it deleted. Without this the sweep re-reads the prefix it has
    already cleared and a full purge is quadratic in the table size."""
    session = AsyncMock()
    session.execute, calls = _purge_execute([(["(0,1)", "(0,5)"], 2), (["(0,20)"], 1), ([], 0)])
    session.commit = AsyncMock()

    await purge_expired(session)

    selects = [c for i, c in enumerate(calls) if i % 2 == 0]
    assert [c["after"] for c in selects] == ["(0,0)", "(0,5)", "(0,20)"]


@pytest.mark.asyncio
async def test_cache_purge_deletes_only_within_the_range_it_selected():
    """The DELETE is bounded by the cursor and the last selected ctid, and
    carries the expiry predicate again so a row refreshed between the two
    statements is spared exactly as the single-statement form spared it."""
    session = AsyncMock()
    session.execute, calls = _purge_execute([(["(0,1)", "(0,5)"], 2), ([], 0)])
    session.commit = AsyncMock()

    await purge_expired(session)

    delete_params = calls[1]
    assert delete_params["after"] == "(0,0)"
    assert delete_params["last"] == "(0,5)"
    assert "now" in delete_params


@pytest.mark.asyncio
async def test_cache_purge_returns_zero_when_nothing_expired():
    """Nothing expired means one candidate query and no delete at all."""
    session = AsyncMock()
    session.execute, _ = _purge_execute([([], 0)])
    session.commit = AsyncMock()

    count = await purge_expired(session)
    assert count == 0
    session.commit.assert_not_called()
