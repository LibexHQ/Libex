"""
libex_core.lookup: each endpoint function against a stand-in `get`, checking
the published shape it returns, the not-found / outage split, the bulk
bookkeeping (notFound, notFetched, placeholderRecords), and that nothing a
caller typed is logged or repeated in an exception message. Nothing touches a
network.
"""

# Standard library
import asyncio
import logging
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.audible.books import UNRELEASED_PLACEHOLDER
from libex_core.exceptions import (
    AudibleAPIException,
    ErrorCode,
    NotFoundException,
    RegionException,
)
from libex_core.lookup import (
    abs_quick_search,
    abs_search,
    get_book,
    get_books,
    get_chapters,
    get_series,
    get_series_books,
    narrator_books,
    quick_search,
    search,
)
from libex_core.models import (
    AbsSearchResponse,
    BookResponse,
    BulkBookResponse,
    ChapterResponse,
    SeriesResponse,
)

PLANTED = "Zq9-distinctive-typed-text"


def _product(asin: str, **extra) -> dict:
    return {
        "asin": asin,
        "title": f"Title {asin}",
        "publication_datetime": "2020-01-01T00:00:00Z",
        "some_new_audible_key": {"kept": True},
        **extra,
    }


def _stub(asin: str) -> dict:
    return {"asin": asin}


def _placeholder(asin: str) -> dict:
    return _product(asin, publication_datetime=UNRELEASED_PLACEHOLDER)


def _asins(count: int) -> list[str]:
    return [f"B0LOOK{i:04d}" for i in range(count)]


def _batch_get(known=(), stubs=(), placeholders=(), fail=()):
    """A get answering batch requests: known ASINs as products, `stubs` as
    hollow stubs, `placeholders` as placeholder records; a request containing
    any ASIN in `fail` raises AudibleAPIException."""
    async def get(region, path, params=None, extra_headers=None):
        wanted = params["asins"].split(",") if "asins" in params else [path.rsplit("/", 1)[1]]
        if any(a in fail for a in wanted):
            raise AudibleAPIException("boom", upstream_status=503)
        products = []
        for a in wanted:
            if a in placeholders:
                products.append(_placeholder(a))
            elif a in known:
                products.append(_product(a))
            else:
                products.append(_stub(a))
        if "asins" in params:
            return {"products": products}
        if products[0].get("title"):
            return {"product": products[0]}
        raise NotFoundException("nope")
    return AsyncMock(side_effect=get)


# ============================================================
# get_book
# ============================================================

async def test_get_book_returns_the_published_model_with_every_audible_key_kept():
    get = _batch_get(known={"B0LOOK0001"})
    book = await get_book(get, "b0look0001", region="de")
    assert isinstance(book, BookResponse)
    assert book.asin == "B0LOOK0001"
    assert book.region == "de"
    assert book.audibleExtras["some_new_audible_key"] == {"kept": True}
    assert get.await_args.args[0] == "de"
    assert get.await_args.args[1].endswith("/B0LOOK0001")


async def test_get_book_defaults_to_us():
    get = _batch_get(known={"B0LOOK0001"})
    await get_book(get, "B0LOOK0001")
    assert get.await_args.args[0] == "us"


async def test_get_book_settles_the_tri_state_flags():
    book = await get_book(_batch_get(known={"B0LOOK0001"}), "B0LOOK0001")
    assert book.explicit is False
    assert book.isBuyable is True
    assert book.plans == []


async def test_get_book_not_found_is_terminal_not_an_outage():
    get = AsyncMock(side_effect=NotFoundException("gone"))
    with pytest.raises(NotFoundException) as info:
        await get_book(get, "B0LOOK0001")
    assert info.value.code == ErrorCode.NOT_ON_AUDIBLE
    assert get.await_count == 1


async def test_get_book_placeholder_is_withheld():
    get = AsyncMock(return_value={"product": _placeholder("B0LOOK0001")})
    with pytest.raises(NotFoundException) as info:
        await get_book(get, "B0LOOK0001")
    assert info.value.code == ErrorCode.WITHHELD


async def test_get_book_outage_is_audible_api_exception_with_upstream_status():
    get = AsyncMock(side_effect=AudibleAPIException("boom", upstream_status=503))
    with pytest.raises(AudibleAPIException) as info:
        await get_book(get, "B0LOOK0001")
    assert info.value.upstream_status == 503
    assert "boom" not in info.value.message


@pytest.mark.parametrize("bad", ["short", "B0LOOK00!!", "", None])
async def test_get_book_rejects_a_malformed_asin_without_a_request_or_echo(bad):
    get = AsyncMock()
    with pytest.raises(NotFoundException) as info:
        await get_book(get, bad)
    assert info.value.code == ErrorCode.INVALID_REQUEST
    get.assert_not_called()


async def test_get_book_unknown_region_is_a_region_exception_not_an_outage():
    get = AsyncMock()
    with pytest.raises(RegionException):
        await get_book(get, "B0LOOK0001", region="xx")
    get.assert_not_called()


# ============================================================
# get_books
# ============================================================

async def test_get_books_buckets_every_asin_in_exactly_one_place():
    ids = ["B0LOOK0001", "B0LOOK0002", "B0LOOK0003"]
    get = _batch_get(known={ids[0]}, stubs={ids[1]}, placeholders={ids[2]})
    result = await get_books(get, ids)
    assert isinstance(result, BulkBookResponse)
    assert [b.asin for b in result.books] == [ids[0]]
    assert result.notFound == [ids[1]]
    assert result.placeholderRecords == [ids[2]]
    assert result.notFetched == []


async def test_get_books_reports_the_callers_own_strings_and_repeats():
    get = _batch_get(known=set())
    result = await get_books(get, ["b0look0001, b0look0001,b0look0002"])
    assert result.notFound == ["b0look0001", "b0look0001", "b0look0002"]
    assert get.await_count == 1
    assert get.await_args.args[2]["asins"] == "B0LOOK0001,B0LOOK0002"


async def test_get_books_sends_the_uppercase_form_once():
    get = _batch_get(known=set())
    await get_books(get, ["b0look0001", "B0LOOK0001", "b0look0002"])
    assert get.await_args.args[2]["asins"] == "B0LOOK0001,B0LOOK0002"


async def test_get_books_a_failed_chunk_goes_to_not_fetched_and_the_rest_is_served():
    ids = _asins(60)
    get = _batch_get(known=set(ids), fail={ids[55]})
    result = await get_books(get, ids)
    assert [b.asin for b in result.books] == ids[:50]
    assert result.notFetched == ids[50:]
    assert result.notFound == []


async def test_get_books_every_chunk_failing_raises_an_outage():
    ids = _asins(60)
    get = _batch_get(fail=set(ids))
    with pytest.raises(AudibleAPIException) as info:
        await get_books(get, ids)
    assert info.value.upstream_status == 503


async def test_get_books_a_failed_chunk_with_no_book_served_raises_even_beside_a_confirmed_absence():
    ids = _asins(60)
    get = _batch_get(known=set(), fail={ids[55]})
    with pytest.raises(AudibleAPIException):
        await get_books(get, ids)


async def test_get_books_chunks_in_fifties():
    ids = _asins(120)
    get = _batch_get(known=set(ids))
    result = await get_books(get, ids)
    assert get.await_count == 3
    assert len(result.books) == 120


async def test_get_books_accepts_exactly_a_thousand_and_rejects_more():
    ids = _asins(1000)
    result = await get_books(_batch_get(known=set(ids)), ids)
    assert len(result.books) == 1000
    get = AsyncMock()
    with pytest.raises(NotFoundException) as info:
        await get_books(get, _asins(1001))
    assert info.value.code == ErrorCode.INVALID_REQUEST
    get.assert_not_called()


async def test_get_books_one_malformed_identifier_rejects_the_request_without_echo():
    get = AsyncMock()
    with pytest.raises(NotFoundException) as info:
        await get_books(get, ["B0LOOK0001", PLANTED])
    assert info.value.code == ErrorCode.INVALID_REQUEST
    assert PLANTED not in info.value.message
    get.assert_not_called()


@pytest.mark.parametrize("empty", [[], [""], [" , "]])
async def test_get_books_nothing_to_look_up_is_invalid(empty):
    with pytest.raises(NotFoundException) as info:
        await get_books(AsyncMock(), empty)
    assert info.value.code == ErrorCode.INVALID_REQUEST


async def test_get_books_single_asin_404_is_a_not_found_entry():
    get = AsyncMock(side_effect=NotFoundException("gone"))
    result = await get_books(get, ["B0LOOK0001"])
    assert result.notFound == ["B0LOOK0001"]
    assert result.notFetched == []


async def test_get_books_found_wins_over_every_other_bucket():
    # Audible returns the same ASIN as a servable book and as a placeholder.
    async def get(region, path, params=None, extra_headers=None):
        return {"products": [_product("B0LOOK0001"), _placeholder("B0LOOK0001"),
                             _product("B0LOOK0002")]}
    result = await get_books(AsyncMock(side_effect=get), ["B0LOOK0001", "B0LOOK0002"])
    assert result.placeholderRecords == []
    assert result.notFound == []


# ============================================================
# get_chapters
# ============================================================

async def test_get_chapters_returns_the_published_model():
    data = {"content_metadata": {"chapter_info": {
        "is_accurate": True,
        "runtime_length_ms": 5000,
        "chapters": [{"length_ms": 5000, "start_offset_ms": 0, "title": "One"}],
    }}}
    result = await get_chapters(AsyncMock(return_value=data), "b0look0001", region="uk")
    assert isinstance(result, ChapterResponse)
    assert result.isAccurate is True
    assert result.chapters[0].title == "One"


async def test_get_chapters_404_and_missing_listing_are_both_not_found():
    with pytest.raises(NotFoundException):
        await get_chapters(AsyncMock(side_effect=NotFoundException("x")), "B0LOOK0001")
    with pytest.raises(NotFoundException):
        await get_chapters(AsyncMock(return_value={"content_metadata": {}}), "B0LOOK0001")


async def test_get_chapters_transient_failure_is_an_outage():
    get = AsyncMock(side_effect=AudibleAPIException("boom", upstream_status=500))
    with pytest.raises(AudibleAPIException) as info:
        await get_chapters(get, "B0LOOK0001")
    assert info.value.upstream_status == 500


async def test_get_chapters_rejects_bad_asin_and_region_before_any_request():
    get = AsyncMock()
    with pytest.raises(NotFoundException):
        await get_chapters(get, "nope")
    with pytest.raises(RegionException):
        await get_chapters(get, "B0LOOK0001", region="xx")
    get.assert_not_called()


# ============================================================
# series
# ============================================================

def _series_get(members=()):
    async def get(region, path, params=None, extra_headers=None):
        if path == "/1.0/catalog/products/B0SERIES01":
            if params["response_groups"] == "relationships":
                return {"product": {"relationships": [
                    {"asin": a, "sort": str(i + 1)} for i, a in enumerate(members)
                ]}}
            return {"response_groups": ["a", "b"], "product": {
                "asin": "B0SERIES01", "title": "A Series",
                "publisher_summary": "<p>About</p>",
            }}
        if "asins" in params:
            return {"products": [_product(a) for a in params["asins"].split(",")]}
        return {"product": _product(path.rsplit("/", 1)[1])}
    return AsyncMock(side_effect=get)


async def test_get_series_returns_the_published_model():
    result = await get_series(_series_get(), "b0series01", region="fr")
    assert isinstance(result, SeriesResponse)
    assert (result.asin, result.name, result.region) == ("B0SERIES01", "A Series", "fr")
    assert result.description == "About"


async def test_get_series_unknown_series_is_not_found_and_outage_is_not():
    get = AsyncMock(return_value={})
    with pytest.raises(NotFoundException):
        await get_series(get, "B0SERIES01")
    get = AsyncMock(side_effect=AudibleAPIException("boom", upstream_status=502))
    with pytest.raises(AudibleAPIException):
        await get_series(get, "B0SERIES01")


async def test_get_series_books_hydrates_the_members_in_series_order():
    members = ["B0LOOK0002", "B0LOOK0001"]
    result = await get_series_books(_series_get(members), "B0SERIES01", region="ca")
    assert [b.asin for b in result.books] == members
    assert result.complete is True and result.incomplete_reasons == ()
    assert all(isinstance(b, BookResponse) and b.region == "ca" for b in result.books)


async def test_get_series_books_a_malformed_member_costs_only_itself():
    members = ["B0LOOK0001", "not-an-asin", "B0LOOK0002"]
    get = _series_get(members)
    result = await get_series_books(get, "B0SERIES01")
    assert [b.asin for b in result.books] == ["B0LOOK0001", "B0LOOK0002"]


async def test_get_series_books_no_members_is_not_found():
    with pytest.raises(NotFoundException):
        await get_series_books(_series_get([]), "B0SERIES01")


async def test_series_functions_reject_bad_asin_and_region_before_any_request():
    get = AsyncMock()
    for fn in (get_series, get_series_books):
        with pytest.raises(NotFoundException):
            await fn(get, "bad")
        with pytest.raises(RegionException):
            await fn(get, "B0SERIES01", region="xx")
    get.assert_not_called()


# ============================================================
# search
# ============================================================

def _search_get(products):
    return AsyncMock(return_value={"products": products})


async def test_search_returns_books_and_maps_query_to_title():
    get = _search_get([_product("B0LOOK0001"), _stub("B0LOOK0002")])
    result = await search(get, query="hobbit", limit=5, region="au")
    assert [b.asin for b in result] == ["B0LOOK0001"]
    assert all(isinstance(b, BookResponse) for b in result)
    assert get.await_args.args[0] == "au"
    assert get.await_args.args[2]["title"] == "hobbit"
    assert get.await_args.args[2]["num_results"] == 5
    assert "image_sizes" in get.await_args.args[2]


async def test_search_title_wins_over_query():
    get = _search_get([_product("B0LOOK0001")])
    await search(get, title="a", query="b")
    assert get.await_args.args[2]["title"] == "a"


async def test_search_with_no_match_is_not_found():
    for products in ([], [_stub("B0LOOK0001")], [_placeholder("B0LOOK0001")]):
        with pytest.raises(NotFoundException):
            await search(_search_get(products), title="x")


async def test_search_audible_404_is_not_found_and_failure_is_an_outage():
    with pytest.raises(NotFoundException):
        await search(AsyncMock(side_effect=NotFoundException("x")), title="x")
    with pytest.raises(AudibleAPIException):
        await search(AsyncMock(side_effect=AudibleAPIException("x")), title="x")


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 51}, {"page": -1}, {"page": 10}])
async def test_search_bounds_are_value_errors_not_outages(kwargs):
    get = AsyncMock()
    with pytest.raises(ValueError):
        await search(get, title="x", **kwargs)
    get.assert_not_called()


async def test_search_unknown_region_is_a_region_exception_not_an_outage():
    with pytest.raises(RegionException):
        await search(AsyncMock(), title="x", region="xx")


async def test_narrator_books_searches_the_narrator_filter_and_never_echoes_the_name():
    get = _search_get([_product("B0LOOK0001")])
    result = await narrator_books(get, PLANTED, limit=3, page=2, region="it")
    assert [b.asin for b in result] == ["B0LOOK0001"]
    assert get.await_args.args[2]["narrator"] == PLANTED
    assert get.await_args.args[2]["page"] == 2
    with pytest.raises(NotFoundException) as info:
        await narrator_books(_search_get([]), PLANTED)
    assert PLANTED not in info.value.message


# ============================================================
# quick search
# ============================================================

def _suggestion_get(asins, known=(), catalog=None):
    async def get(region, path, params=None, extra_headers=None):
        if path == "/1.0/searchsuggestions":
            return {"model": {"items": [
                {"view": {"template": "AsinRow"},
                 "model": {"product_metadata": {"asin": a}}} for a in asins
            ]}}
        if path == "/1.0/catalog/products/":
            if isinstance(catalog, Exception):
                raise catalog
            return {"products": catalog or []}
        if "asins" in params:
            return {"products": [_product(a) for a in params["asins"].split(",")]}
        return {"product": _product(path.rsplit("/", 1)[1])}
    return AsyncMock(side_effect=get)


async def test_quick_search_hydrates_the_suggested_asins():
    result = await quick_search(
        _suggestion_get(["B0LOOK0001", "B0LOOK0002"]), "hobbit", region="es"
    )
    assert [b.asin for b in result] == ["B0LOOK0001", "B0LOOK0002"]


async def test_quick_search_no_suggestions_and_no_compound_is_not_found():
    with pytest.raises(NotFoundException):
        await quick_search(_suggestion_get([]), "hobbit")


async def test_quick_search_compound_query_falls_back_to_author_and_title():
    get = _suggestion_get([], catalog=[_product("B0LOOK0009")])
    result = await quick_search(get, "Tolkien - Middle Earth - The Hobbit")
    assert [b.asin for b in result] == ["B0LOOK0009"]
    params = get.await_args.args[2]
    assert (params["author"], params["title"]) == ("Tolkien", "The Hobbit")
    assert params["num_results"] == 10


@pytest.mark.parametrize("keywords", ["foo - ", " - foo", "foo -  - "])
async def test_quick_search_one_usable_segment_is_not_a_compound_query(keywords):
    """A trailing or doubled separator leaves one non-empty segment, and one
    segment has no author and title to search for: no catalog request is made."""
    get = _suggestion_get([], catalog=[_product("B0LOOK0009")])
    with pytest.raises(NotFoundException):
        await quick_search(get, keywords)
    assert [c.args[1] for c in get.await_args_list] == ["/1.0/searchsuggestions"]


async def test_quick_search_compound_fallback_outage_is_an_outage_not_an_absence():
    get = _suggestion_get([], catalog=AudibleAPIException("boom"))
    with pytest.raises(AudibleAPIException):
        await quick_search(get, "A - B")


async def test_quick_search_suggestion_outage_and_hydration_outage_raise():
    with pytest.raises(AudibleAPIException):
        await quick_search(AsyncMock(side_effect=AudibleAPIException("x")), "k")

    async def get(region, path, params=None, extra_headers=None):
        if path == "/1.0/searchsuggestions":
            return {"model": {"items": [
                {"view": {"template": "AsinRow"},
                 "model": {"product_metadata": {"asin": "B0LOOK0001"}}}]}}
        raise AudibleAPIException("boom", upstream_status=503)
    with pytest.raises(AudibleAPIException) as info:
        await quick_search(AsyncMock(side_effect=get), "k")
    assert info.value.upstream_status == 503


# ============================================================
# Audiobookshelf shapes
# ============================================================

async def test_abs_search_wraps_matches_and_asks_for_five():
    get = _search_get([_product("B0LOOK0001")])
    result = await abs_search(get, query="hobbit", region="us")
    assert isinstance(result, AbsSearchResponse)
    assert result.matches[0].asin == "B0LOOK0001"
    assert get.await_args.args[2]["num_results"] == 5
    assert get.await_args.args[2]["title"] == "hobbit"


async def test_abs_functions_answer_an_unknown_region_as_an_invalid_request():
    for call in (abs_search(AsyncMock(), title="x", region="xx"),
                 abs_quick_search(AsyncMock(), "x", region="xx")):
        with pytest.raises(NotFoundException) as info:
            await call
        assert info.value.code == ErrorCode.INVALID_REQUEST


async def test_abs_quick_search_takes_the_first_term_and_needs_one():
    get = _suggestion_get(["B0LOOK0001"])
    result = await abs_quick_search(get, query="hobbit")
    assert result.matches[0].asin == "B0LOOK0001"
    assert get.await_args_list[0].args[2]["keywords"] == "hobbit"
    with pytest.raises(NotFoundException) as info:
        await abs_quick_search(AsyncMock())
    assert info.value.code == ErrorCode.INVALID_REQUEST


async def test_abs_search_with_no_match_is_not_found():
    with pytest.raises(NotFoundException):
        await abs_search(_search_get([]), title="x")


# ============================================================
# Caller text never reaches a log or a message
# ============================================================

async def test_nothing_the_caller_typed_is_logged_or_raised(caplog):
    caplog.set_level(logging.DEBUG, logger="libex")
    get = _suggestion_get([], catalog=[_product("B0LOOK0001")])
    await quick_search(get, f"{PLANTED} - {PLANTED}")
    await search(_search_get([_product("B0LOOK0001")]), title=PLANTED, author=PLANTED)
    await narrator_books(_search_get([_product("B0LOOK0001")]), PLANTED)
    with pytest.raises(AudibleAPIException) as info:
        await search(AsyncMock(side_effect=AudibleAPIException(PLANTED)), title=PLANTED)
    assert PLANTED not in info.value.message
    text = "\n".join(
        f"{r.getMessage()} {sorted(r.__dict__.items(), key=str)}" for r in caplog.records
    )
    assert caplog.records
    assert PLANTED not in text


async def test_chunks_run_concurrently():
    in_flight = 0
    peak = 0

    async def get(region, path, params=None, extra_headers=None):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return {"products": [_product(a) for a in params["asins"].split(",")]}

    ids = _asins(150)
    await get_books(AsyncMock(side_effect=get), ids)
    assert peak == 3


async def test_series_members_are_sent_to_audible_in_uppercase():
    get = _series_get(["b0look0001", "B0LOOK0001", "b0look0002"])
    result = await get_series_books(get, "B0SERIES01")
    assert [b.asin for b in result.books] == ["B0LOOK0001", "B0LOOK0002"]
    assert get.await_args.args[2]["asins"] == "B0LOOK0001,B0LOOK0002"


# ============================================================
# Input validation matches the hosted routes
# ============================================================

async def test_only_the_abs_quick_search_route_rejects_missing_terms():
    # The hosted /search, /narrator/books, /quick-search and /{region}/search
    # routes carry no emptiness check, so the lookups send what they are given
    # and Audible's answer stands. Only /{region}/quick-search/search rejects.
    get = _search_get([_product("B0LOOK0001")])
    assert await search(get)
    assert await narrator_books(get, "")
    assert await abs_search(get)
    assert get.await_count == 3
    assert "narrator" not in get.await_args.args[2]

    quick = _suggestion_get(["B0LOOK0001"])
    assert await quick_search(quick, "")
    assert quick.await_args_list[0].args[2]["keywords"] == ""

    never = AsyncMock()
    with pytest.raises(NotFoundException) as info:
        await abs_quick_search(never, "", "", "")
    assert info.value.code == ErrorCode.INVALID_REQUEST
    never.assert_not_called()
