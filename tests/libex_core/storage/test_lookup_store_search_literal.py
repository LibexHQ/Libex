"""
The stored fallbacks behind search_series and quick_search read what the caller
typed as plain text: a % or _ is not a wildcard and a backslash is not an
escape, on SQLite and on Postgres (the latter needs Docker). The hosted /db
search that shares the readers keeps its wildcards, which test_read_sqlite pins.
"""

# Standard library
import uuid

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.exceptions import NotFoundException
from libex_core.lookup import _store, get_book, search_series
from libex_core.storage import write
from libex_core.storage.store import LocalStore
from tests.libex_core._lookup_support import AUTHOR_NAME, empty_get, product
from tests.libex_core.test_lookup_store import batch_get


@pytest_asyncio.fixture(
    params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)]
)
async def store(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'library.db'}"
    else:
        base = request.getfixturevalue("postgres_url")
        name = "t_" + uuid.uuid4().hex[:12]
        engine = create_async_engine(base, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
        await engine.dispose()
        url = make_url(base).set(database=name).render_as_string(hide_password=False)
    local = LocalStore(url)
    await local.upgrade()
    await local.open()
    yield local
    await local.close()


async def _store_series(store, *titles):
    async with store.write() as session:
        for i, title in enumerate(titles):
            await write.write_series_profile(
                session, {"asin": f"B0SRCH000{i}", "name": title, "region": "us"}
            )


async def _store_books(store, **titles):
    get = batch_get(**{a: product(a, title=t) for a, t in titles.items()})
    for asin in titles:
        await get_book(get, asin, store=store)


async def _series_names(store, name):
    return [s["name"] for s in await _store.search_stored_series(store, name, "us")]


async def _book_asins(store, title, author=AUTHOR_NAME):
    return [b["asin"] for b in await _store.search_stored_books(store, title, author, "us")]


async def test_a_wildcard_character_does_not_match_a_stored_series(store):
    await _store_series(store, "The Stormlight Archive")
    assert await _series_names(store, "_") == []
    assert await _series_names(store, "%") == []
    assert await _series_names(store, "Storm_ight") == []


async def test_a_percent_series_name_matches_only_itself(store):
    await _store_series(store, "100% Wolf", "100 Big Wolf", "The Stormlight Archive")
    assert await _series_names(store, "100% Wolf") == ["100% Wolf"]


async def test_a_backslash_in_a_series_name_is_literal(store):
    await _store_series(store, "AC\\DC Saga", "ACDC Saga")
    assert await _series_names(store, "AC\\DC") == ["AC\\DC Saga"]


async def test_a_plain_series_name_still_matches_as_a_substring_ignoring_case(store):
    await _store_series(store, "The Stormlight Archive", "Mistborn")
    assert await _series_names(store, "stormlight") == ["The Stormlight Archive"]


async def test_search_series_with_a_wildcard_and_nothing_from_audible_is_not_found(store):
    await _store_series(store, "The Stormlight Archive")
    for term in ("_", "%"):
        with pytest.raises(NotFoundException):
            await search_series(empty_get, term, store=store)
    assert [s.name for s in await search_series(empty_get, "Stormlight", store=store)] \
        == ["The Stormlight Archive"]


async def test_a_wildcard_character_does_not_match_stored_books(store):
    await _store_books(store, B0BOOK0001="100% Wolf", B0BOOK0002="100 Big Wolf")
    assert await _book_asins(store, "_") == []
    assert await _book_asins(store, "%") == ["B0BOOK0001"]
    assert await _book_asins(store, "100% Wolf") == ["B0BOOK0001"]
    assert await _book_asins(store, "wolf") == ["B0BOOK0001", "B0BOOK0002"]


async def test_a_wildcard_character_in_the_author_does_not_match(store):
    await _store_books(store, B0BOOK0001="Plain")
    assert await _book_asins(store, "Plain", "J_ne Test") == []
    assert await _book_asins(store, "Plain", "%") == []
    assert await _book_asins(store, "Plain", "jane") == ["B0BOOK0001"]
