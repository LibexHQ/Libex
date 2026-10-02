"""
libex_core.lookup.get_series_books and its BookList: whether the list is whole,
with the reasons in the hosted routes' own words, judged before any filtering;
the same against the hosted /series/books route's headers; a null where Audible
sends an object or list being an outage and never an empty answer, on the
library and on the hosted route (which must not store the empty list over a
cached one); and the budget and concurrency the author-books lookup hydrates
with. Nothing touches a network.
"""

# Standard library
import asyncio
import time

# Third party
import pytest

# Local
from libex_core import lookup as L
from libex_core.exceptions import AudibleAPIException
from libex_core.lookup import BookList, get_series_books
from libex_core.lookup import author_books as author_books_module
from tests.libex_core import _lookup_support as support
from tests.libex_core._lookup_support import (
    AUTHOR,
    BOOKS,
    SERIES,
    asins,
    fake_get,
)
from tests.libex_core.test_lookup_authors import WALK, _hydrating_get

REGIONS = ["us", "de", "jp"]


def _series_members(members, **hydration):
    """A get serving a series' member list and then its books, as
    _hydrating_get serves the hydration alone."""
    books = _hydrating_get(**hydration)

    async def get(region, path, params=None, extra_headers=None):
        if "asins" in (params or {}):
            return await books(region, path, params, extra_headers)
        return {
            "response_groups": ["a", "b"],
            "product": {"relationships": [
                {"asin": a, "sort": str(i + 1)} for i, a in enumerate(members)
            ]},
        }

    return get


# Section: completeness


async def test_a_whole_series_is_complete_with_no_reasons():
    result = await get_series_books(_series_members(asins(3)), SERIES)
    assert isinstance(result, BookList)
    assert len(result.books) == 3
    assert result.complete is True and result.incomplete_reasons == ()


async def test_a_member_audible_has_no_record_of_is_hydration_not_found():
    found = asins(3)
    result = await get_series_books(_series_members(found, stubs={found[1]}), SERIES)
    assert (result.complete, result.incomplete_reasons) == (False, ("hydration-not-found",))
    assert len(result.books) == 2


async def test_a_placeholder_member_is_hydration_not_found():
    found = asins(3)
    result = await get_series_books(
        _series_members(found, placeholders={found[0]}), SERIES
    )
    assert result.incomplete_reasons == ("hydration-not-found",)


async def test_a_failed_request_is_hydration_failed_and_the_rest_is_served():
    found = asins(120)
    result = await get_series_books(_series_members(found, fail={found[60]}), SERIES)
    assert (result.complete, result.incomplete_reasons) == (False, ("hydration-failed",))
    assert len(result.books) == 70


async def test_both_reasons_come_in_the_published_order():
    found = asins(120)
    get = _series_members(found, fail={found[60]}, stubs={found[3]})
    result = await get_series_books(get, SERIES)
    assert result.incomplete_reasons == ("hydration-failed", "hydration-not-found")
    assert len(result.books) == 69


async def test_discovery_and_deadline_reasons_cannot_arise_for_a_series():
    """The member list is one request and nothing here has a deadline."""
    found = asins(120)
    get = _series_members(found, fail={found[60]}, stubs={found[3]})
    result = await get_series_books(get, SERIES)
    assert not {"discovery-incomplete", "hydration-deadline"} & set(result.incomplete_reasons)


async def test_a_filter_that_leaves_nothing_does_not_make_the_series_incomplete():
    result = await get_series_books(
        _series_members(asins(3)), SERIES, filters={"language": "klingon"}
    )
    assert result.books == [] and result.complete is True


async def test_a_filter_does_not_hide_a_real_shortfall():
    found = asins(3)
    result = await get_series_books(
        _series_members(found, stubs={found[0]}), SERIES, filters={"language": "klingon"}
    )
    assert result.books == []
    assert result.incomplete_reasons == ("hydration-not-found",)


# Section: against the hosted route's headers


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("path", [f"/series/books/{SERIES}", f"/series/{SERIES}/books"],
                         ids=["primary", "legacy"])
def test_a_whole_series_says_so_the_same_way_as_the_route(hosted, path, region):
    resp = hosted(fake_get, path, {"region": region})
    result = asyncio.run(L.get_series_books(fake_get, SERIES, region=region))
    assert resp.status_code == 200
    assert result.complete is True
    assert resp.headers["x-libex-complete"] == "true"
    assert "x-libex-incomplete-reason" not in resp.headers


@pytest.mark.parametrize("region", REGIONS)
def test_a_series_with_a_missing_book_reports_the_same_reasons_as_the_route(
    hosted, region, monkeypatch
):
    gone = "B0SCR00002"
    monkeypatch.setattr(
        support, "CATALOGUE", {k: v for k, v in support.CATALOGUE.items() if k != gone}
    )
    resp = hosted(fake_get, f"/series/books/{SERIES}", {"region": region})
    result = asyncio.run(L.get_series_books(fake_get, SERIES, region=region))
    assert result.incomplete_reasons == ("hydration-not-found",)
    assert resp.headers["x-libex-complete"] == "false"
    assert resp.headers["x-libex-incomplete-reason"] == ", ".join(result.incomplete_reasons)
    assert len(resp.json()) == len(result.books) == len(BOOKS) - 1


@pytest.mark.parametrize("region", REGIONS)
def test_a_filter_does_not_change_what_the_route_or_the_lookup_says(
    hosted, region, monkeypatch
):
    monkeypatch.setattr(
        support, "CATALOGUE", {k: v for k, v in support.CATALOGUE.items() if k != "B0SCR00002"}
    )
    query = {"region": region, "language": "klingon"}
    resp = hosted(fake_get, f"/series/books/{SERIES}", query)
    result = asyncio.run(
        L.get_series_books(fake_get, SERIES, region=region, filters={"language": "klingon"})
    )
    assert resp.json() == [] and result.books == []
    assert resp.headers["x-libex-complete"] == "false"
    assert result.complete is False


# Section: a null is a malformed answer, an outage and never an absence


async def _null_get(region, path, params=None, extra_headers=None):
    if "contributors/" in path:
        return {"contributor": None}
    return {"response_groups": ["a", "b"], "product": None, "products": None}


async def _null_relationships(region, path, params=None, extra_headers=None):
    return {"response_groups": ["a", "b"], "product": {"relationships": None}}


@pytest.mark.parametrize("call", [
    lambda: L.get_author(_null_get, AUTHOR),
    lambda: L.get_author_books(_null_get, AUTHOR),
    lambda: L.get_series_books(_null_get, SERIES),
    lambda: L.get_series_books(_null_relationships, SERIES),
], ids=["null contributor", "author books null contributor",
        "series null product", "series null relationships"])
async def test_a_null_where_audible_sends_an_object_is_an_outage(call):
    with pytest.raises(AudibleAPIException):
        await call()


@pytest.mark.parametrize("path", [f"/series/books/{SERIES}", f"/series/{SERIES}/books"],
                         ids=["primary", "legacy"])
@pytest.mark.parametrize("get", [_null_get, _null_relationships], ids=["product", "relationships"])
def test_the_route_answers_503_and_stores_no_empty_list_for_a_null_member_list(
    hosted, path, get
):
    resp = hosted(get, path, {"region": "us"})
    assert resp.status_code == 503
    assert hosted.hooks["app.services.audible.series.persist_cache_background"].call_count == 0


# Section: one outage message


@pytest.mark.parametrize("call", [
    lambda g: L.get_author(g, AUTHOR),
    lambda g: L.get_series(g, SERIES),
    lambda g: L.get_series_books(g, SERIES),
    lambda g: L.get_author_books(g, AUTHOR),
    lambda g: L.get_books(g, ["B0LOOK0001"]),
    lambda g: L.get_book(g, "B0LOOK0001"),
    lambda g: L.get_chapters(g, "B0LOOK0001"),
], ids=["author", "series", "series books", "author books", "bulk", "book", "chapters"])
async def test_every_outage_carries_the_same_message(call):
    with pytest.raises(AudibleAPIException) as caught:
        await call(support.outage_get)
    assert str(caught.value) == "Audible unavailable"


# Section: the author-books budget and hydration


def test_the_author_books_budget_is_twenty_five_seconds():
    assert author_books_module.AUTHOR_BOOKS_TIME_BUDGET_SECONDS == 25.0


async def test_the_walk_and_hydration_share_one_twenty_five_second_deadline(monkeypatch):
    seen = {}

    async def walk(get, asin, region, deadline):
        seen["walk"] = deadline
        return asins(2), True

    real = author_books_module.hydrate_books

    async def spied(get, found, region, **kwargs):
        seen["hydrate"] = kwargs
        return await real(get, found, region, **kwargs)

    monkeypatch.setattr(WALK, walk)
    monkeypatch.setattr(author_books_module, "hydrate_books", spied)
    before = time.monotonic()
    await L.get_author_books(_hydrating_get(), AUTHOR)
    assert 24.0 < seen["walk"] - before <= 25.5
    assert seen["hydrate"]["deadline"] == seen["walk"]
    assert seen["hydrate"]["high_concurrency"] is True
