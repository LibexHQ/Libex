"""
One product Audible sends that cannot be normalized (a null authors list
raises on purpose, so a bad answer never overwrites a stored book) costs only
its own ASIN: the others are served, the bad one is reported not fetched or
answered from the store, and it is never reported as absent.
"""

# Third party
import pytest
import pytest_asyncio

# Local
from libex_core.exceptions import AudibleAPIException
from libex_core.lookup import get_books, search
from libex_core.lookup.books import hydrate_books
from libex_core.storage.store import LocalStore
from tests.libex_core._lookup_support import product
from tests.libex_core.test_lookup_store import batch_get

GOOD = "B0GOODONE1"
BAD = "B0BADONE01"


@pytest_asyncio.fixture
async def store(tmp_path):
    local = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    await local.upgrade()
    await local.open()
    yield local
    await local.close()


def _answer(**bad_extra):
    return batch_get(**{GOOD: product(GOOD, isbn=None), BAD: product(BAD, authors=None, **bad_extra)})


async def test_the_good_book_is_served_and_the_bad_one_is_not_fetched():
    hydration = await hydrate_books(_answer(), [GOOD, BAD], "us")
    assert [b["asin"] for b in hydration.books] == [GOOD]
    assert hydration.not_fetched == [BAD]
    assert hydration.not_found == []
    assert hydration.explicit_nulls == {GOOD: ("isbn",)}


async def test_get_books_serves_the_good_book():
    result = await get_books(_answer(), [GOOD, BAD])
    assert [b.asin for b in result.books] == [GOOD]


async def test_a_stored_copy_answers_for_the_bad_product(store):
    await hydrate_books(batch_get(**{BAD: product(BAD)}), [BAD], "us", store=store)
    hydration = await hydrate_books(_answer(), [GOOD, BAD], "us", store=store)
    assert sorted(b["asin"] for b in hydration.books) == sorted([GOOD, BAD])
    assert hydration.from_store == [BAD]
    assert hydration.not_fetched == []
    assert hydration.not_found == []


async def test_every_product_bad_with_nothing_stored_is_an_outage():
    with pytest.raises(AudibleAPIException):
        await hydrate_books(batch_get(**{BAD: product(BAD, authors=None)}), [BAD], "us")


async def test_the_bad_product_does_not_stop_the_rest_of_a_large_batch():
    asins = [f"B0BLK{i:05d}" for i in range(120)]
    members = {a: product(a) for a in asins}
    members[asins[60]] = product(asins[60], authors=None)
    hydration = await hydrate_books(batch_get(**members), asins, "us")
    assert len(hydration.books) == 119
    assert hydration.not_fetched == [asins[60]]


async def _search_get(*products):
    async def get(region, path, params=None, extra_headers=None):
        return {"products": list(products)}
    return get


async def test_search_serves_the_rest_when_one_result_is_bad():
    get = await _search_get(product(GOOD), product(BAD, authors=None))
    assert [b.asin for b in await search(get, title="x")] == [GOOD]


async def test_search_is_an_outage_when_every_result_is_bad():
    get = await _search_get(product(BAD, authors=None))
    with pytest.raises(AudibleAPIException):
        await search(get, title="x")
