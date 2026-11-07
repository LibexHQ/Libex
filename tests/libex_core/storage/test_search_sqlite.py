"""
The ranked search needs Postgres: its folding function and its trigram
operators do not exist on SQLite. A SQLite session is refused with a clear
error before any statement is run, never answered with something unranked.
"""

# Third party
import pytest

# Local
from app.services.db import stored
from libex_core.exceptions import ErrorCode, LibexException
from libex_core.storage.read import search


@pytest.mark.parametrize(
    "call",
    [
        lambda s: search.search_books_ranked(s, title="first quest"),
        lambda s: search.search_authors_ranked(s, "Ann Author"),
        lambda s: search.search_series_ranked(s, "Series"),
    ],
    ids=["books", "authors", "series"],
)
async def test_sqlite_is_refused_with_not_implemented(sqlite_session, call):
    with pytest.raises(NotImplementedError, match="PostgreSQL"):
        await call(sqlite_session)


async def test_nothing_is_executed_on_sqlite_before_the_refusal(sqlite_session):
    statements = []
    sqlite_session.execute = _record(sqlite_session.execute, statements)
    with pytest.raises(NotImplementedError):
        await search.search_books_ranked(sqlite_session, title="first quest")
    assert statements == []


def _record(execute, seen):
    async def wrapper(statement, *args, **kwargs):
        seen.append(statement)
        return await execute(statement, *args, **kwargs)

    return wrapper


@pytest.mark.parametrize(
    "call",
    [
        lambda s: stored.search_books_ranked_from_db(s, title="first quest"),
        lambda s: stored.search_authors_ranked_from_db(s, "Ann Author"),
        lambda s: stored.search_series_ranked_from_db(s, "Series"),
    ],
    ids=["books", "authors", "series"],
)
async def test_the_hosted_wrappers_turn_the_refusal_into_a_503(sqlite_session, call):
    with pytest.raises(LibexException) as raised:
        await call(sqlite_session)
    assert raised.value.status_code == 503
    assert raised.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert "PostgreSQL" not in str(raised.value.message)
