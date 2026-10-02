"""
Each `libex-core db` command prints what the hosted /db route of the same name
returns for the same rows: one catalog seeded into a SQLite store the command
line reads and into a Postgres database the application's own routes read,
both asked the same question, the answers compared field for field.

Needs Docker, like the other Postgres comparisons, and runs with the
integration marker. The command line runs as a real process.

The one documented difference is the order of text sorted by title or name:
Postgres sorts by its locale's collation, SQLite by code point. Those cases
are compared as sets, over every row (no paging), and the order the command
line gives is pinned in test_cli_db_commands.py.
"""

# Standard library
import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

# Third party
import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

# Local
from app.db.session import get_session
from app.main import app
from libex_core.cli.environment import STORAGE_VARIABLE
from libex_core.storage.base import Base
from tests.libex_core._cli_support import REPO_ROOT, clean_env, run_python
from tests.libex_core._db_support import make_store
from tests.libex_core.storage._support import core_tables, seed

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Parity:
    argv: tuple[str, ...]
    route: str
    params: dict = field(default_factory=dict)
    # Compared as a set: the order depends on the database's collation.
    unordered: bool = False


CASES = (
    Parity(("db", "book", "B000000001"), "/db/book/B000000001"),
    Parity(("db", "book", "B000000002"), "/db/book/B000000002"),
    Parity(("db", "book", "B000000003"), "/db/book/B000000003"),
    Parity(("db", "book", "B000000099"), "/db/book/B000000099"),
    Parity(("db", "books", "--title", "quest"), "/db/book", {"title": "quest"}),
    Parity(("db", "books", "--region", "de"), "/db/book", {"region": "de"}),
    Parity(
        ("db", "books", "--explicit", "true", "--rating-better-than", "2.5"),
        "/db/book", {"explicit": "true", "rating_better_than": 2.5},
    ),
    Parity(("db", "books", "--genre", "fantasy"), "/db/book", {"genre": "fantasy"}),
    Parity(("db", "books", "--title", "100%"), "/db/book", {"title": "100%"}),
    Parity(("db", "books", "--title", "AC\\DC"), "/db/book", {"title": "AC\\DC"}),
    Parity(("db", "books", "--title", "émile"), "/db/book", {"title": "émile"}),
    Parity(
        ("db", "books", "--sort", "rating", "--order", "desc", "--limit", "3"),
        "/db/book", {"sort": "rating", "order": "desc", "limit": 3},
    ),
    Parity(
        ("db", "books", "--sort", "lengthMinutes", "--limit", "4", "--page", "2"),
        "/db/book", {"sort": "lengthMinutes", "limit": 4, "page": 2},
    ),
    Parity(
        ("db", "books", "--sort", "title", "--order", "desc", "--limit", "100"),
        "/db/book", {"sort": "title", "order": "desc", "limit": 100}, unordered=True,
    ),
    Parity(("db", "books"), "/db/book"),
    Parity(("db", "chapters", "B000000001"), "/db/book/B000000001/chapters"),
    Parity(("db", "chapters", "B000000002"), "/db/book/B000000002/chapters"),
    Parity(("db", "sku", "SG1"), "/db/book/sku/SG1"),
    Parity(("book", "sku", "SG1"), "/book/sku/SG1"),
    Parity(("db", "sku", "NOPE"), "/db/book/sku/NOPE"),
    Parity(("db", "author", "A000000001"), "/db/author/A000000001"),
    Parity(("db", "author", "A000000004", "--region", "fr"), "/db/author/A000000004", {"region": "fr"}),
    Parity(("db", "author", "A000000004"), "/db/author/A000000004"),
    Parity(("db", "author-books", "A000000001"), "/db/author/A000000001/books"),
    Parity(
        ("db", "author-books", "A000000001", "--book-region", "us", "--sort", "rating"),
        "/db/author/A000000001/books", {"book_region": "us", "sort": "rating"},
    ),
    Parity(("db", "series", "S000000001"), "/db/series/S000000001"),
    Parity(("db", "series-books", "S000000001"), "/db/series/S000000001/books"),
    Parity(("db", "series-books", "S000000003"), "/db/series/S000000003/books"),
    Parity(
        ("db", "series-books", "S000000001", "--sort", "title", "--order", "desc"),
        "/db/series/S000000001/books", {"sort": "title", "order": "desc"}, unordered=True,
    ),
    Parity(("db", "narrators", "nina"), "/db/narrator", {"name": "nina"}),
    Parity(
        ("db", "narrators", "e", "--sort", "name", "--order", "desc", "--limit", "100"),
        "/db/narrator", {"name": "e", "sort": "name", "order": "desc", "limit": 100}, unordered=True,
    ),
    Parity(
        ("db", "narrators", "nina", "--audiobooks-produced", "1-10"),
        "/db/narrator", {"name": "nina", "audiobooks_produced": "1 to 10"},
    ),
    Parity(("db", "narrators", "zzzz"), "/db/narrator", {"name": "zzzz"}),
    Parity(("db", "narrator-books", "Nina Voice"), "/db/narrator/books", {"name": "Nina Voice"}),
    Parity(("db", "genres"), "/db/genres"),
    Parity(("db", "genres", "--search", "fan"), "/db/genres", {"search": "fan"}),
    Parity(("db", "plans"), "/db/plans"),
    Parity(("db", "plan", "Plus"), "/db/plans/Plus"),
    Parity(
        ("db", "plan", "Plus", "--language", "english", "--sort", "rating", "--limit", "1"),
        "/db/plans/Plus", {"language": "english", "sort": "rating", "limit": 1},
    ),
    Parity(("db", "vvab"), "/db/vvab"),
    Parity(("db", "new-releases"), "/db/new-releases"),
    Parity(
        ("db", "new-releases", "--days", "365", "--sort", "title", "--limit", "100"),
        "/db/new-releases", {"days": 365, "sort": "title", "limit": 100}, unordered=True,
    ),
    Parity(("db", "coming-soon", "--days", "90"), "/db/coming-soon", {"days": 90}),
    Parity(("db", "stats"), "/db/stats"),
    Parity(("db", "stats", "--region", "us"), "/db/stats", {"region": "us"}),
    Parity(("db", "stats", "--region", "de"), "/db/stats", {"region": "de"}),
)


def _norm(value):
    """Lists inside a book come back in each backend's scan order."""
    if isinstance(value, list):
        items = [_norm(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


def _same_rows_any_order(a, b):
    """Rows in any order, each compared whole and (inside) in any order."""
    return isinstance(a, list) and isinstance(b, list) and _norm(a) == _norm(b)


@pytest_asyncio.fixture
async def hosted_http(postgres_url):
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=core_tables())
        await conn.run_sync(Base.metadata.create_all, tables=core_tables())
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await seed(session, odd_plans=False)

    async def override():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = override
    nothing_cached = [
        patch("app.services.cache.manager.get", AsyncMock(return_value=None)),
        patch("app.services.cache.manager.get_many", AsyncMock(return_value={})),
        patch("app.services.cache.manager.get_entry", AsyncMock(return_value=None)),
        patch("app.services.cache.manager.set", AsyncMock()),
    ]
    for p in nothing_cached:
        p.start()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://hosted") as http:
            yield http
    finally:
        for p in nothing_cached:
            p.stop()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


def _cli(argv, store):
    return run_python(
        ["-m", "libex_core", *argv],
        env=clean_env(**{STORAGE_VARIABLE: store}),
        cwd=REPO_ROOT,
        timeout=60,
    )


@pytest.fixture(scope="module")
def sqlite_file(tmp_path_factory):
    return make_store(tmp_path_factory.mktemp("parity") / "s.db")


def _ids(case):
    return " ".join(case.argv)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_ids)
async def test_the_command_prints_what_the_hosted_route_returns(hosted_http, sqlite_file, case):
    hosted = await hosted_http.get(case.route, params=case.params)
    done = _cli(case.argv, sqlite_file)
    err = done.stderr.decode()
    if hosted.status_code == 200:
        assert done.returncode == 0, err
        printed = json.loads(done.stdout)
        body = hosted.json()
        assert printed, "an empty answer would equal an empty answer"
        if case.unordered:
            assert _same_rows_any_order(printed, body)
        else:
            assert _norm(printed) == _norm(body)
        return
    # An answer the hosted route refuses is refused here too, with the same
    # code, and prints nothing.
    code = hosted.json()["code"]
    assert hosted.status_code in (404, 422), hosted.text
    assert done.stdout == b""
    assert done.returncode == {"not_in_libex": 3, "invalid_request": 2}[code], err
    assert f"(code: {code})" in err


@pytest.mark.asyncio
async def test_the_seeded_catalog_has_the_rows_the_cases_above_rely_on(hosted_http):
    stats = (await hosted_http.get("/db/stats")).json()
    assert (stats["books"], stats["authors"], stats["narrators"], stats["series"]) == (15, 4, 4, 4)
