"""
libex_core.audible.chapters and libex_core.audible.series: what each fetch
promises before anything is sent (region and ASIN validated, the rejected
value never echoed), the request it then makes, and how it reads Audible's
answer.

Each fetch is handed `get` rather than owning a client, so every test here
passes a stand-in and asserts on whether it was called. Nothing touches a
network.
"""

# Standard library
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.audible.chapters import (
    CHAPTERS_QUALITY,
    CHAPTERS_RESPONSE_GROUPS,
    fetch_chapter_metadata,
)
from libex_core.audible.series import (
    SERIES_BOOKS_RESPONSE_GROUPS,
    SERIES_RESPONSE_GROUPS,
    fetch_series,
    fetch_series_book_asins,
)
from libex_core.exceptions import NotFoundException, RegionException

BAD_ASINS = [
    "B0SHORT",
    "B0ELEVENCHR",
    "B0FETCH00!",
    "../etc/pw",
    "B0FETCH00/",
    "",
    None,
    12345,
]
BAD_REGIONS = ["xx", "", "usa", "mars"]

FETCHES = [
    pytest.param(fetch_chapter_metadata, id="chapters"),
    pytest.param(fetch_series, id="series"),
    pytest.param(fetch_series_book_asins, id="series_books"),
]


# ============================================================
# Validated before anything is sent
# ============================================================

@pytest.mark.parametrize("fetch", FETCHES)
@pytest.mark.parametrize("region", BAD_REGIONS)
async def test_fetch_rejects_an_unknown_region_before_any_request(fetch, region):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await fetch(get, "B0FETCH001", region)
    get.assert_not_awaited()


@pytest.mark.parametrize("fetch", FETCHES)
@pytest.mark.parametrize("bad", BAD_ASINS)
async def test_fetch_rejects_a_non_asin_before_any_request(fetch, bad):
    get = AsyncMock()
    with pytest.raises(ValueError):
        await fetch(get, bad, "us")
    get.assert_not_awaited()


@pytest.mark.parametrize("fetch", FETCHES)
async def test_fetch_error_message_never_echoes_the_rejected_value(fetch):
    get = AsyncMock()
    with pytest.raises(ValueError) as exc:
        await fetch(get, "inject\nline", "us")
    assert "inject" not in str(exc.value)


@pytest.mark.parametrize("fetch", FETCHES)
async def test_fetch_takes_get_as_a_required_first_argument(fetch):
    with pytest.raises(TypeError):
        await fetch("B0FETCH001", "us")


@pytest.mark.parametrize("fetch", FETCHES)
async def test_fetch_normalises_the_region_it_passes_to_get(fetch):
    get = AsyncMock(return_value={})
    try:
        await fetch(get, "B0FETCH001", " US ")
    except NotFoundException:
        pass
    assert get.await_args.args[0] == "us"


# ============================================================
# fetch_chapter_metadata: request and answer
# ============================================================

async def test_fetch_chapter_metadata_requests_the_content_metadata_path_with_pinned_params():
    get = AsyncMock(return_value={"content_metadata": {"chapter_info": {"chapters": []}}})
    await fetch_chapter_metadata(get, "b0fetch001", "uk")
    get.assert_awaited_once_with(
        "uk",
        "/1.0/content/B0FETCH001/metadata",
        {
            "response_groups": CHAPTERS_RESPONSE_GROUPS,
            "quality": CHAPTERS_QUALITY,
        },
    )
    assert CHAPTERS_RESPONSE_GROUPS == "chapter_info, always-returned, content_reference, content_url"
    assert CHAPTERS_QUALITY == "High"


async def test_fetch_chapter_metadata_returns_the_response_as_audible_sent_it():
    payload = {"content_metadata": {"chapter_info": {"chapters": [1]}}, "extra": "kept"}
    get = AsyncMock(return_value=payload)
    assert await fetch_chapter_metadata(get, "B0FETCH001", "us") is payload


async def test_fetch_chapter_metadata_returns_a_response_without_chapter_info_unraised():
    get = AsyncMock(return_value={"content_metadata": {}})
    assert await fetch_chapter_metadata(get, "B0FETCH001", "us") == {"content_metadata": {}}


async def test_fetch_chapter_metadata_lets_a_404_from_get_through():
    get = AsyncMock(side_effect=NotFoundException("gone"))
    with pytest.raises(NotFoundException):
        await fetch_chapter_metadata(get, "B0FETCH001", "us")


# ============================================================
# fetch_series: request and not-found handling
# ============================================================

async def test_fetch_series_requests_the_product_path_with_pinned_params():
    product = {"asin": "B0SERIES01", "title": "Dune"}
    get = AsyncMock(return_value={"response_groups": ["a", "b"], "product": product})
    await fetch_series(get, "b0series01", "de")
    get.assert_awaited_once_with(
        "de",
        "/1.0/catalog/products/B0SERIES01",
        {"response_groups": SERIES_RESPONSE_GROUPS},
    )
    assert SERIES_RESPONSE_GROUPS == "product_attrs, product_desc, product_extended_attrs"


async def test_fetch_series_returns_the_product_raw():
    product = {"asin": "B0SERIES01", "title": "Dune", "unmodelled": {"k": 1}}
    get = AsyncMock(return_value={"response_groups": ["a", "b"], "product": product})
    assert await fetch_series(get, "B0SERIES01", "us") is product


@pytest.mark.parametrize("data", [
    None,
    {},
    {"product": {"asin": "B0SERIES01"}},
    {"response_groups": [], "product": {"asin": "B0SERIES01"}},
    {"response_groups": ["only_one"], "product": {"asin": "B0SERIES01"}},
    {"response_groups": ["a", "b"]},
    {"response_groups": ["a", "b"], "product": {}},
    {"response_groups": ["a", "b"], "product": None},
])
async def test_fetch_series_not_found_when_audible_answers_for_no_series(data):
    get = AsyncMock(return_value=data)
    with pytest.raises(NotFoundException) as exc:
        await fetch_series(get, "B0SERIES01", "us")
    assert "B0SERIES01" in exc.value.message


async def test_fetch_series_lets_a_failure_from_get_through():
    boom = RuntimeError("transient")
    get = AsyncMock(side_effect=boom)
    with pytest.raises(RuntimeError) as exc:
        await fetch_series(get, "B0SERIES01", "us")
    assert exc.value is boom


# ============================================================
# fetch_series_book_asins: request, ordering and not-found handling
# ============================================================

async def test_fetch_series_book_asins_requests_the_relationships_group():
    get = AsyncMock(return_value={"product": {"relationships": [{"asin": "B0BOOK0001", "sort": "1"}]}})
    await fetch_series_book_asins(get, "b0series01", "fr")
    get.assert_awaited_once_with(
        "fr",
        "/1.0/catalog/products/B0SERIES01",
        {"response_groups": SERIES_BOOKS_RESPONSE_GROUPS},
    )
    assert SERIES_BOOKS_RESPONSE_GROUPS == "relationships"


async def test_fetch_series_book_asins_sorts_numerically_and_drops_entries_missing_either_field():
    get = AsyncMock(return_value={"product": {"relationships": [
        {"asin": "B0BOOK0010", "sort": "10"},
        {"asin": "B0BOOK0002", "sort": "2"},
        {"asin": "B0BOOK0015", "sort": "1.5"},
        {"asin": "B0NOSORT01"},
        {"sort": "3"},
        {"asin": "B0EMPTYSRT", "sort": ""},
    ]}})
    assert await fetch_series_book_asins(get, "B0SERIES01", "us") == [
        "B0BOOK0015", "B0BOOK0002", "B0BOOK0010",
    ]


@pytest.mark.parametrize("data", [
    {},
    {"product": {}},
    {"product": {"relationships": []}},
    {"product": {"relationships": [{"asin": "B0NOSORT01"}, {"sort": "1"}]}},
])
async def test_fetch_series_book_asins_not_found_when_no_member_qualifies(data):
    get = AsyncMock(return_value=data)
    with pytest.raises(NotFoundException) as exc:
        await fetch_series_book_asins(get, "B0SERIES01", "us")
    assert "B0SERIES01" in exc.value.message
