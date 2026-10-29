"""
Audible listing probe: it classifies Audible's own answer, reads and writes
nothing local, and turns anything that is not an answer into an error so an
outage is never read as a delisting.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.services.audible import listing
from app.services.audible.listing import probe_listing
from libex_core.audible.books import UNRELEASED_PLACEHOLDER
from libex_core.exceptions import AudibleAPIException, NotFoundException, RegionException

ASIN = "B0MANUAL01"


def _answer(product):
    return AsyncMock(return_value={"product": product})


# ============================================================
# CLASSIFICATION
# ============================================================


@pytest.mark.parametrize(
    "product,expected",
    [
        ({"asin": ASIN, "title": "Listed"}, "live"),
        ({"asin": ASIN, "title": "Listed", "publication_datetime": "2020-01-01T00:00:00Z"}, "live"),
        ({"asin": ASIN, "title": "Unreleased", "publication_datetime": UNRELEASED_PLACEHOLDER}, "placeholder"),
        ({"asin": ASIN}, "absent"),
        ({"asin": ASIN, "title": ""}, "absent"),
        # a titleless stub is a stub whatever date it carries
        ({"asin": ASIN, "publication_datetime": UNRELEASED_PLACEHOLDER}, "absent"),
    ],
    ids=["live", "live-dated", "placeholder", "stub", "blank-title", "titleless-placeholder-date"],
)
async def test_an_answer_is_classified(product, expected):
    with patch.object(listing, "audible_get", _answer(product)):
        assert await probe_listing(ASIN, "us") == expected


async def test_an_empty_single_answer_is_absent():
    with patch.object(listing, "audible_get", AsyncMock(return_value={})):
        assert await probe_listing(ASIN, "us") == "absent"


async def test_a_404_is_absent_and_is_asked_once():
    get = AsyncMock(side_effect=NotFoundException("gone"))
    with patch.object(listing, "audible_get", get):
        assert await probe_listing(ASIN, "uk") == "absent"
    assert get.await_count == 1


async def test_the_region_and_asin_reach_audible():
    get = _answer({"asin": ASIN, "title": "Listed"})
    with patch.object(listing, "audible_get", get):
        await probe_listing(ASIN, "jp")
    assert get.await_args.args[0] == "jp"
    assert get.await_args.args[1].endswith(f"/{ASIN}")


# ============================================================
# AN OUTAGE IS NEVER AN ANSWER
# ============================================================


@pytest.mark.parametrize(
    "failure",
    [
        AudibleAPIException("503", upstream_status=503),
        AudibleAPIException("429", upstream_status=429),
        AudibleAPIException("timeout"),
        TimeoutError("slow"),
        ConnectionError("reset"),
        RuntimeError("anything else"),
    ],
    ids=["5xx", "429", "audible-timeout", "timeout", "transport", "unexpected"],
)
async def test_every_outage_class_raises(failure):
    with patch.object(listing, "audible_get", AsyncMock(side_effect=failure)):
        with pytest.raises(AudibleAPIException):
            await probe_listing(ASIN, "us")


async def test_a_null_product_is_an_outage_not_an_absence():
    with patch.object(listing, "audible_get", _answer(None)):
        with pytest.raises(AudibleAPIException):
            await probe_listing(ASIN, "us")


async def test_an_outage_message_does_not_carry_the_underlying_text():
    with patch.object(listing, "audible_get", AsyncMock(side_effect=RuntimeError("secret-detail"))):
        with pytest.raises(AudibleAPIException) as caught:
            await probe_listing(ASIN, "us")
    assert "secret-detail" not in str(caught.value)


@pytest.mark.parametrize("asin", ["not an asin", "", "B0'; DROP"])
async def test_a_malformed_asin_is_refused_before_anything_is_sent(asin):
    get = AsyncMock()
    with patch.object(listing, "audible_get", get):
        with pytest.raises(ValueError):
            await probe_listing(asin, "us")
    get.assert_not_called()


async def test_an_unknown_region_is_refused_before_anything_is_sent():
    get = AsyncMock()
    with patch.object(listing, "audible_get", get):
        with pytest.raises(RegionException):
            await probe_listing(ASIN, "xx")
    get.assert_not_called()


# ============================================================
# NOTHING LOCAL IS TOUCHED
# ============================================================


async def test_it_reads_no_database_or_cache_and_persists_nothing():
    trap = AsyncMock(side_effect=AssertionError("local state touched"))
    with patch.object(listing, "audible_get", _answer({"asin": ASIN})), \
         patch("app.services.audible.books.get_book_by_asin", trap), \
         patch("app.services.audible.books.get_books_by_asins", trap), \
         patch("app.services.cache.manager.get", trap), \
         patch("app.services.cache.manager.set", trap), \
         patch("app.db.session.AsyncSessionFactory", trap):
        assert await probe_listing(ASIN, "us") == "absent"
    trap.assert_not_called()


def test_the_module_imports_no_database_cache_or_persist_layer():
    names = set(vars(listing))
    assert not {"AsyncSessionFactory", "get_session", "cache", "persist_queue"} & names
