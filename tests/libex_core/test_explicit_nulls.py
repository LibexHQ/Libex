"""
Explicit nulls on libex_core lookup results: which published fields Audible
sent as a JSON null rather than omitting. The signal only reports. These tests
pin that a null differs from an absent key, that a container null stays an
outage and is never reported as a clear, that no served value moves, that a
book answered from the store carries no entry, and that audibleExtras agrees.
"""

# Standard library
import copy

# Third party
import pytest
import pytest_asyncio

# Local
from libex_core.audible.books import explicit_null_fields, normalize_product
from libex_core.exceptions import AudibleAPIException
from libex_core.lookup import get_book, get_books, get_series_books
from libex_core.lookup.books import get_book_with_nulls, get_books_with_nulls, hydrate_books
from libex_core.storage.store import LocalStore
from tests.libex_core._lookup_support import BOOKS, SERIES, outage_get, product
from tests.libex_core.test_lookup_store import batch_get

ASIN = "B0SCR00000"
OTHER = "B0SCR00001"


@pytest_asyncio.fixture
async def store(tmp_path):
    local = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    await local.upgrade()
    await local.open()
    yield local
    await local.close()


# Section: the detector

def test_a_present_null_is_reported_and_an_absent_key_is_not():
    raw = {"asin": ASIN, "title": "T", "subtitle": None, "isbn": None}
    assert explicit_null_fields(raw) == ("subtitle", "isbn")
    assert explicit_null_fields({"asin": ASIN, "title": "T"}) == ()


def test_a_response_group_that_was_not_sent_gives_no_false_nulls():
    # No rating, relationships or product_plans keys at all: the groups were
    # absent, which is silence and not a null.
    raw = {"asin": ASIN, "title": "T", "publisher_name": "P"}
    assert explicit_null_fields(raw) == ()


def test_one_upstream_key_names_every_field_it_feeds():
    assert explicit_null_fields({"is_buyable": None}) == ("isAvailable", "isBuyable")
    assert explicit_null_fields({"sku_lite": None}) == ("skuGroup",)


def test_the_rating_scalars_are_reported_when_the_rating_object_is_there():
    raw = {"rating": {"overall_distribution": {"average_rating": None, "num_ratings": None},
                      "num_reviews": None}}
    assert explicit_null_fields(raw) == ("rating", "numRatings", "numReviews")
    assert explicit_null_fields({"rating": {"overall_distribution": {}}}) == ()


@pytest.mark.parametrize("key", [
    "authors", "narrators", "category_ladders", "relationships", "plans",
    "product_images", "rating",
])
def test_a_container_null_is_never_reported_as_a_clear(key):
    assert explicit_null_fields({"asin": ASIN, key: None}) == ()


def test_an_overall_distribution_null_is_not_reported():
    assert explicit_null_fields({"rating": {"overall_distribution": None, "num_reviews": 3}}) == ()


def test_episode_fields_count_only_for_a_podcast():
    nulls = {"episode_number": None, "episode_type": None}
    assert explicit_null_fields({"content_type": "Podcast", **nulls}) == (
        "episodeNumber", "episodeType")
    assert explicit_null_fields({"content_type": "Product", **nulls}) == ()


# Section: values stay exactly as they are

def test_a_null_publishes_the_same_values_as_an_omitted_key():
    keys = ["subtitle", "publisher_name", "isbn", "language", "sku", "is_listenable",
            "is_buyable", "runtime_length_min", "product_state"]
    full = {"asin": ASIN, "title": "T", **{k: None for k in keys}}
    bare = {"asin": ASIN, "title": "T"}
    assert normalize_product(full, "us") == normalize_product(bare, "us")
    assert len(explicit_null_fields(full)) > len(keys)


def test_the_detector_does_not_mutate_its_input():
    raw = {"asin": ASIN, "title": "T", "subtitle": None, "rating": {"num_reviews": None}}
    before = copy.deepcopy(raw)
    explicit_null_fields(raw)
    assert raw == before


async def test_get_book_is_unchanged_by_the_signal():
    get = batch_get(**{ASIN: product(ASIN, subtitle=None, publisher_name=None)})
    plain = await get_book(get, ASIN)
    with_nulls = await get_book_with_nulls(get, ASIN)
    assert with_nulls.book == plain
    assert with_nulls.explicit_nulls == ("subtitle", "publisher")


# Section: the result carriers

async def test_hydration_maps_each_book_to_its_own_nulls():
    get = batch_get(**{ASIN: product(ASIN, isbn=None), OTHER: product(OTHER)})
    hydration = await hydrate_books(get, [ASIN, OTHER], "us")
    assert hydration.explicit_nulls == {ASIN: ("isbn",), OTHER: ()}


async def test_get_books_with_nulls_reports_only_the_books_it_returns():
    get = batch_get(**{ASIN: product(ASIN, isbn=None, length=500), OTHER: product(OTHER, subtitle=None, length=100)})
    both = await get_books_with_nulls(get, [ASIN, OTHER])
    assert both.explicit_nulls == {ASIN: ("isbn",), OTHER: ("subtitle",)}
    assert both.response == await get_books(get, [ASIN, OTHER])
    filtered = await get_books_with_nulls(get, [ASIN, OTHER], filters={"longer_than": 300})
    assert [b.asin for b in filtered.response.books] == [ASIN]
    assert filtered.explicit_nulls == {ASIN: ("isbn",)}


async def test_a_book_list_carries_the_nulls():
    members = {a: product(a, isbn=None) for a in BOOKS}
    result = await get_series_books(batch_get(**members), SERIES)
    assert result.explicit_nulls == {a: ("isbn",) for a in BOOKS}


async def test_a_book_list_reports_only_the_books_that_survive_the_filter():
    short, long_ = list(BOOKS)[:2]
    members = {
        short: product(short, isbn=None, length=10),
        long_: product(long_, subtitle=None, length=900),
    }
    result = await get_series_books(
        batch_get(**{**{a: product(a) for a in BOOKS}, **members}), SERIES,
        filters={"longer_than": 500},
    )
    assert [b.asin for b in result.books] == [long_]
    assert result.explicit_nulls == {long_: ("subtitle",)}


# Section: a container null is served empty, and is not reported as a clear

@pytest.mark.parametrize("key", ["authors", "narrators", "category_ladders", "relationships"])
async def test_a_container_null_serves_the_book_and_is_not_reported(key):
    get = batch_get(**{ASIN: product(ASIN, **{key: None})})
    lookup = await get_book_with_nulls(get, ASIN)
    assert lookup.book.asin == ASIN
    assert lookup.explicit_nulls == ()


async def test_a_null_rating_object_stays_an_outage():
    get = batch_get(**{ASIN: {**product(ASIN), "rating": None}})
    with pytest.raises(AudibleAPIException):
        await get_book_with_nulls(get, ASIN)


# Section: the store

async def test_a_book_answered_from_the_store_has_no_entry(store):
    await get_book(batch_get(**{ASIN: product(ASIN, isbn=None)}), ASIN, store=store)
    served = await get_book_with_nulls(outage_get, ASIN, store=store)
    assert served.explicit_nulls is None
    hydration = await hydrate_books(outage_get, [ASIN], "us", store=store)
    assert hydration.from_store == [ASIN]
    assert ASIN not in hydration.explicit_nulls


async def test_a_live_book_served_through_the_store_still_reports_what_audible_said(store):
    live = await get_book_with_nulls(batch_get(**{ASIN: product(ASIN, isbn=None)}), ASIN, store=store)
    assert live.explicit_nulls == ("isbn",)


async def test_a_null_does_not_clear_what_the_store_holds(store):
    await get_book(batch_get(**{ASIN: product(ASIN, publisher_name="Kept House")}), ASIN, store=store)
    again = await get_book_with_nulls(
        batch_get(**{ASIN: product(ASIN, publisher_name=None)}), ASIN, store=store
    )
    assert again.explicit_nulls == ("publisher",)
    assert again.book.publisher == "Kept House"


# Section: audibleExtras

async def test_audible_extras_keeps_a_null_verbatim_where_a_key_is_not_reproduced():
    get = batch_get(**{ASIN: product(ASIN, publication_datetime=None, product_images=None)})
    result = await get_book_with_nulls(get, ASIN)
    extras = result.book.audibleExtras
    assert "publication_datetime" in extras and extras["publication_datetime"] is None
    assert "publicationDatetime" in result.explicit_nulls
    # A container null is verbatim in the blob and absent from the report.
    assert "product_images" in extras and extras["product_images"] is None
    assert "imageUrl" not in result.explicit_nulls


async def test_a_reproduced_key_is_in_the_report_and_not_the_blob():
    result = await get_book_with_nulls(batch_get(**{ASIN: product(ASIN, subtitle=None)}), ASIN)
    assert "subtitle" not in result.book.audibleExtras
    assert "subtitle" in result.explicit_nulls
