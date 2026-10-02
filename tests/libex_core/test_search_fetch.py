"""
libex_core.audible.search: what each function promises before anything is
sent (region validated, limit and page bounded, no message repeating what a
caller typed), the request it then makes, and how it reads Audible's answer.

Each fetch is handed `get` rather than owning a client, so every test here
passes a stand-in and asserts on whether it was called. Nothing touches a
network.
"""

# Standard library
import ast
import inspect
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
import libex_core
from libex_core.audible import search as search_module
from libex_core.audible.books import BOOK_RESPONSE_GROUPS, IMAGE_SIZES
from libex_core.audible.search import (
    MAX_SEARCH_RESULTS,
    SEARCH_PATH,
    SEARCH_SUGGESTIONS_PATH,
    _generate_session_id,
    build_search_params,
    fetch_search_products,
    fetch_suggestion_asins,
)
from libex_core.exceptions import AudibleAPIException, NotFoundException, RegionException
from libex_core.models import AbsBookResponse, AbsSeriesRef, to_abs_book

BAD_REGIONS = ["xx", "", "usa", "mars"]
PLANTED = "Zq9-distinctive-title-text"


# ============================================================
# build_search_params: bounds and parameters
# ============================================================

@pytest.mark.parametrize("limit", [0, -1, 51, 1000])
def test_build_search_params_rejects_a_limit_outside_1_to_50(limit):
    with pytest.raises(ValueError):
        build_search_params(title=PLANTED, limit=limit)


@pytest.mark.parametrize("page", [-1, -50])
def test_build_search_params_rejects_a_negative_page(page):
    with pytest.raises(ValueError):
        build_search_params(title=PLANTED, page=page)


@pytest.mark.parametrize("limit", [1, 10, 50])
def test_build_search_params_accepts_the_limit_edges(limit):
    assert build_search_params(limit=limit)["num_results"] == limit


def test_build_search_params_accepts_page_zero_and_high_pages():
    assert build_search_params(page=0)["page"] == 0
    assert build_search_params(page=9)["page"] == 9


def test_max_search_results_is_fifty():
    assert MAX_SEARCH_RESULTS == 50


@pytest.mark.parametrize("kwargs", [
    {"limit": 51},
    {"limit": 0},
    {"page": -1},
])
def test_build_search_params_message_never_echoes_the_text(kwargs):
    with pytest.raises(ValueError) as exc:
        build_search_params(
            title=PLANTED, author=PLANTED, keywords=PLANTED,
            narrator=PLANTED, publisher=PLANTED, **kwargs,
        )
    assert PLANTED not in str(exc.value)
    assert "distinctive" not in str(exc.value)


def test_build_search_params_message_names_the_bounds_only():
    with pytest.raises(ValueError) as exc:
        build_search_params(limit=51)
    assert str(exc.value) == "limit must be between 1 and 50"
    with pytest.raises(ValueError) as exc:
        build_search_params(page=-1)
    assert str(exc.value) == "page must not be negative"


def test_build_search_params_defaults_are_ten_and_page_zero_with_no_filters():
    assert build_search_params() == {"num_results": 10, "page": 0}


def test_build_search_params_maps_every_filter_to_its_exact_key():
    params = build_search_params(
        title="T", author="A", keywords="K", narrator="N",
        publisher="P", products_sort_by="Relevance", limit=7, page=3,
    )
    assert params == {
        "num_results": 7,
        "page": 3,
        "title": "T",
        "author": "A",
        "keywords": "K",
        "narrator": "N",
        "publisher": "P",
        "products_sort_by": "Relevance",
    }


@pytest.mark.parametrize("field", [
    "title", "author", "keywords", "narrator", "publisher", "products_sort_by",
])
def test_build_search_params_includes_only_the_filter_given(field):
    assert build_search_params(**{field: "x"}) == {"num_results": 10, "page": 0, field: "x"}


@pytest.mark.parametrize("field", [
    "title", "author", "keywords", "narrator", "publisher", "products_sort_by",
])
@pytest.mark.parametrize("empty", ["", None])
def test_build_search_params_omits_an_empty_or_missing_filter(field, empty):
    assert build_search_params(**{field: empty}) == {"num_results": 10, "page": 0}


def test_build_search_params_does_not_reject_empty_keywords():
    assert build_search_params(keywords="") == {"num_results": 10, "page": 0}


def test_build_search_params_does_not_inspect_the_text():
    odd = "  ../etc\n<script>  "
    assert build_search_params(title=odd)["title"] == odd


def test_build_search_params_is_keyword_only():
    with pytest.raises(TypeError):
        build_search_params("a title")


# ============================================================
# fetch_search_products
# ============================================================

def test_fetch_search_products_takes_get_as_a_required_first_argument():
    parameters = inspect.signature(fetch_search_products).parameters
    assert list(parameters)[0] == "get"
    assert parameters["get"].default is inspect.Parameter.empty


async def test_fetch_search_products_without_get_is_a_type_error():
    with pytest.raises(TypeError):
        await fetch_search_products("us", {})


@pytest.mark.parametrize("region", BAD_REGIONS)
async def test_fetch_search_products_rejects_an_unknown_region_before_any_request(region):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await fetch_search_products(get, region, {"num_results": 10, "page": 0})
    get.assert_not_awaited()


async def test_fetch_search_products_sends_the_params_with_groups_and_sizes():
    get = AsyncMock(return_value={"products": [{"asin": "B0SEARCH01"}]})
    params = {"num_results": 5, "page": 2, "title": "T"}

    result = await fetch_search_products(get, "us", params)

    get.assert_awaited_once_with("us", SEARCH_PATH, {
        "num_results": 5,
        "page": 2,
        "title": "T",
        "response_groups": BOOK_RESPONSE_GROUPS,
        "image_sizes": IMAGE_SIZES,
    })
    assert result == [{"asin": "B0SEARCH01"}]


async def test_fetch_search_products_does_not_mutate_the_callers_params():
    get = AsyncMock(return_value={"products": []})
    params = {"num_results": 5, "page": 0}
    await fetch_search_products(get, "us", params)
    assert params == {"num_results": 5, "page": 0}


async def test_fetch_search_products_missing_products_key_is_an_empty_list():
    get = AsyncMock(return_value={})
    assert await fetch_search_products(get, "de", {}) == []


async def test_fetch_search_products_passes_the_region_through():
    get = AsyncMock(return_value={"products": []})
    await fetch_search_products(get, "jp", {})
    assert get.await_args.args[0] == "jp"


@pytest.mark.parametrize("exc", [NotFoundException("gone"), AudibleAPIException("down")])
async def test_fetch_search_products_propagates_failures_from_get(exc):
    get = AsyncMock(side_effect=exc)
    with pytest.raises(type(exc)) as raised:
        await fetch_search_products(get, "us", {})
    assert raised.value is exc


def test_search_path_is_exact_and_keeps_its_trailing_slash():
    assert SEARCH_PATH == "/1.0/catalog/products/"


def test_search_suggestions_path_is_exact():
    assert SEARCH_SUGGESTIONS_PATH == "/1.0/searchsuggestions"


# ============================================================
# fetch_suggestion_asins
# ============================================================

def _row(asin, template="AsinRow"):
    return {"view": {"template": template}, "model": {"product_metadata": {"asin": asin}}}


def test_fetch_suggestion_asins_takes_get_as_a_required_first_argument():
    parameters = inspect.signature(fetch_suggestion_asins).parameters
    assert list(parameters)[0] == "get"
    assert parameters["get"].default is inspect.Parameter.empty


async def test_fetch_suggestion_asins_without_get_is_a_type_error():
    with pytest.raises(TypeError):
        await fetch_suggestion_asins("dune", "us")


@pytest.mark.parametrize("region", BAD_REGIONS)
async def test_fetch_suggestion_asins_rejects_an_unknown_region_before_any_request(region):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await fetch_suggestion_asins(get, PLANTED, region)
    get.assert_not_awaited()


async def test_fetch_suggestion_asins_region_error_never_echoes_the_keywords():
    get = AsyncMock()
    with pytest.raises(RegionException) as exc:
        await fetch_suggestion_asins(get, PLANTED, "xx")
    assert PLANTED not in str(exc.value)


async def test_fetch_suggestion_asins_sends_the_exact_params():
    get = AsyncMock(return_value={})
    await fetch_suggestion_asins(get, "dune", "uk")

    get.assert_awaited_once()
    region, path, params = get.await_args.args
    assert region == "uk"
    assert path == SEARCH_SUGGESTIONS_PATH
    assert set(params) == {
        "keywords", "key_strokes", "site_variant", "session_id", "local_time", "surface",
    }
    assert params["keywords"] == "dune"
    assert params["key_strokes"] == "dune"
    assert params["site_variant"] == "desktop"
    assert params["surface"] == "Android"


async def test_fetch_suggestion_asins_local_time_is_naive_utc_iso():
    get = AsyncMock(return_value={})
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await fetch_suggestion_asins(get, "dune", "us")
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    stamp = get.await_args.args[2]["local_time"]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is None
    assert "+" not in stamp and not stamp.endswith("Z")
    assert before <= parsed <= after


async def test_fetch_suggestion_asins_session_id_has_the_expected_format():
    get = AsyncMock(return_value={})
    await fetch_suggestion_asins(get, "dune", "us")
    assert re.fullmatch(r"000-\d{7}-\d{7}", get.await_args.args[2]["session_id"])


def test_generate_session_id_format_and_variation():
    ids = {_generate_session_id() for _ in range(50)}
    assert all(re.fullmatch(r"000-\d{7}-\d{7}", i) for i in ids)
    assert len(ids) > 1


def test_generate_session_id_zero_pads_each_group(monkeypatch):
    monkeypatch.setattr(search_module.random, "randint", lambda a, b: 42)
    assert _generate_session_id() == "000-0000042-0000042"


async def test_fetch_suggestion_asins_returns_asin_rows_in_order():
    get = AsyncMock(return_value={"model": {"items": [_row("B0AAAAAAA1"), _row("B0AAAAAAA2")]}})
    assert await fetch_suggestion_asins(get, "x", "us") == ["B0AAAAAAA1", "B0AAAAAAA2"]


async def test_fetch_suggestion_asins_skips_other_templates():
    items = [_row("B0AAAAAAA1", "AuthorRow"), _row("B0AAAAAAA2"), _row("B0AAAAAAA3", "SeriesRow")]
    get = AsyncMock(return_value={"model": {"items": items}})
    assert await fetch_suggestion_asins(get, "x", "us") == ["B0AAAAAAA2"]


async def test_fetch_suggestion_asins_does_not_validate_the_asins():
    get = AsyncMock(return_value={"model": {"items": [_row("not an asin!"), _row("0008433844")]}})
    assert await fetch_suggestion_asins(get, "x", "us") == ["not an asin!", "0008433844"]


@pytest.mark.parametrize("data", [
    {},
    {"model": {}},
    {"model": {"items": []}},
])
async def test_fetch_suggestion_asins_empty_shapes_give_an_empty_list(data):
    get = AsyncMock(return_value=data)
    assert await fetch_suggestion_asins(get, "x", "us") == []


@pytest.mark.parametrize("item", [
    {},
    {"view": {}},
    {"view": {"template": "AsinRow"}},
    {"view": {"template": "AsinRow"}, "model": {}},
    {"view": {"template": "AsinRow"}, "model": {"product_metadata": {}}},
    _row(None),
    _row(""),
])
async def test_fetch_suggestion_asins_skips_rows_without_an_asin(item):
    get = AsyncMock(return_value={"model": {"items": [item, _row("B0AAAAAAA1")]}})
    assert await fetch_suggestion_asins(get, "x", "us") == ["B0AAAAAAA1"]


@pytest.mark.parametrize("exc", [NotFoundException("gone"), AudibleAPIException("down")])
async def test_fetch_suggestion_asins_propagates_failures_from_get(exc):
    get = AsyncMock(side_effect=exc)
    with pytest.raises(type(exc)) as raised:
        await fetch_suggestion_asins(get, "x", "us")
    assert raised.value is exc


# ============================================================
# The module's own rules: no logger, no environment
# ============================================================

def _module_tree() -> ast.Module:
    return ast.parse(Path(search_module.__file__).read_text())


def test_search_module_has_no_logger():
    assert not hasattr(search_module, "logger")
    names = {n.id for n in ast.walk(_module_tree()) if isinstance(n, ast.Name)}
    assert "logger" not in names
    imported = {
        a.name for n in ast.walk(_module_tree()) if isinstance(n, ast.Import) for a in n.names
    } | {
        n.module for n in ast.walk(_module_tree()) if isinstance(n, ast.ImportFrom)
    }
    assert not any(m and ("logging" in m or "logger" in m) for m in imported)


def test_search_module_reads_no_environment():
    tree = _module_tree()
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not ({"environ", "getenv"} & (attrs | names))
    assert "os" not in names


def test_search_module_is_inside_the_isolation_walk():
    from tests.libex_core.test_isolation import _EXPECTED_MODULES
    assert "libex_core.audible.search" in _EXPECTED_MODULES
    assert Path(search_module.__file__).is_relative_to(Path(libex_core.__file__).parent)


# ============================================================
# to_abs_book
# ============================================================

FULL_BOOK = {
    "asin": "B08G9PRS1K",
    "title": "T",
    "subtitle": "S",
    "summary": "the summary",
    "description": "the description",
    "imageUrl": "https://example.com/c.jpg",
    "publisher": "Pub",
    "releaseDate": "2021-03-04",
    "isbn": "9780000000002",
    "language": "english",
    "lengthMinutes": 600,
    "authors": [{"name": "A1"}, {"name": ""}, {"name": "A2"}],
    "narrators": [{"name": "N1"}, {"name": "N2"}],
    "genres": [
        {"name": "Fantasy", "type": "Genres"},
        {"name": "Epic", "type": "Tags"},
        {"name": "Other", "type": "Else"},
        {"name": None, "type": "Genres"},
    ],
    "series": [{"name": "Saga", "position": "2"}],
}


def test_to_abs_book_maps_every_field():
    book = to_abs_book(FULL_BOOK)
    assert isinstance(book, AbsBookResponse)
    assert book.model_dump() == {
        "asin": "B08G9PRS1K",
        "title": "T",
        "subtitle": "S",
        "description": "the summary",
        "cover": "https://example.com/c.jpg",
        "publisher": "Pub",
        "publishedYear": "2021",
        "isbn": "9780000000002",
        "language": "english",
        "duration": "600",
        "author": "A1, A2",
        "narrator": "N1, N2",
        "tags": ["Epic"],
        "genres": ["Fantasy"],
        "series": [{"series": "Saga", "sequence": "2"}],
    }
    assert isinstance(book.series[0], AbsSeriesRef)


def test_to_abs_book_falls_back_to_description_without_summary():
    assert to_abs_book({"asin": "A", "description": "d"}).description == "d"


def test_to_abs_book_empty_book_is_all_none_with_blank_asin():
    book = to_abs_book({})
    assert book.asin == ""
    dumped = book.model_dump()
    assert {k: v for k, v in dumped.items() if k != "asin"} == {
        k: None for k in dumped if k != "asin"
    }


def test_to_abs_book_zero_length_is_no_duration():
    assert to_abs_book({"asin": "A", "lengthMinutes": 0}).duration is None
