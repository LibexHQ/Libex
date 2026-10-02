"""
libex_core.audible.releases: what each function promises before anything is
sent (region validated, days bounded, no message repeating what a caller
passed), the requests the catalog walk then makes, how the window gates and
sort keys read a release date, and how the genre taxonomy is flattened and
shaped.

Each fetch is handed `get` rather than owning a client, so every test here
passes a stand-in and asserts on whether it was called. Nothing touches a
network, and every window is computed from an injected `now`.
"""

# Standard library
import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
import libex_core
from libex_core.audible import releases as releases_module
from libex_core.audible.books import BOOK_RESPONSE_GROUPS, IMAGE_SIZES, normalize_product
from libex_core.audible.releases import (
    CATEGORIES_PATH,
    GENRE_TAXONOMY_LEVELS,
    RELEASE_PAGE_SIZE,
    build_category_tree,
    coming_soon_gates,
    coming_soon_sort_key,
    fetch_catalog_genres,
    fetch_coming_soon,
    fetch_new_releases,
    flatten_genre_nodes,
    new_releases_gates,
    new_releases_sort_key,
    release_datetime,
    walk_catalog,
)
from libex_core.audible.search import SEARCH_PATH
from libex_core.exceptions import AudibleAPIException, NotFoundException, RegionException
from libex_core.models import CategoryAncestor, CategoryNode, FlatCategoryNode

BAD_REGIONS = ["xx", "", "usa", "mars"]
PLANTED = "Zq9-distinctive-category-text"
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def _product(asin, day):
    """A minimal catalog product released on `day` (YYYY-MM-DD), midnight UTC."""
    return {"asin": asin, "title": f"Book {asin}", "release_date": day}


def _page(*products):
    return {"products": list(products)}


def _serving(*pages):
    """A `get` that answers each successive call with the next page."""
    return AsyncMock(side_effect=list(pages))


def _always_in(_dt):
    return True


def _never(_dt):
    return False


# ============================================================
# Region and days are checked before any request
# ============================================================

@pytest.mark.parametrize("region", BAD_REGIONS)
async def test_fetch_new_releases_rejects_an_unknown_region_before_any_request(region):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await fetch_new_releases(get, region, now=NOW)
    get.assert_not_awaited()


@pytest.mark.parametrize("region", BAD_REGIONS)
async def test_fetch_coming_soon_rejects_an_unknown_region_before_any_request(region):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await fetch_coming_soon(get, region, now=NOW)
    get.assert_not_awaited()


@pytest.mark.parametrize("region", BAD_REGIONS)
async def test_walk_catalog_rejects_an_unknown_region_before_any_request(region):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await walk_catalog(get, region, None, _always_in, _never)
    get.assert_not_awaited()


@pytest.mark.parametrize("region", BAD_REGIONS)
async def test_fetch_catalog_genres_rejects_an_unknown_region_before_any_request(region):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await fetch_catalog_genres(get, region)
    get.assert_not_awaited()


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
@pytest.mark.parametrize("days", [0, -1, -4242])
async def test_days_below_one_raise_before_any_request(fetch, days):
    get = AsyncMock()
    with pytest.raises(ValueError):
        await fetch(get, "us", days, now=NOW)
    get.assert_not_awaited()


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
async def test_days_message_never_echoes_the_input(fetch):
    with pytest.raises(ValueError) as exc:
        await fetch(AsyncMock(), "us", -4242, PLANTED, now=NOW)
    assert str(exc.value) == "days must be at least 1"
    assert "4242" not in str(exc.value)
    assert PLANTED not in str(exc.value)


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
async def test_days_of_one_is_accepted(fetch):
    get = AsyncMock(return_value=_page())
    assert await fetch(get, "us", 1, now=NOW) == []
    get.assert_awaited_once()


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
async def test_region_is_judged_before_days(fetch):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await fetch(get, "xx", 0, now=NOW)
    get.assert_not_awaited()


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
async def test_region_error_never_echoes_the_category(fetch):
    with pytest.raises(RegionException) as exc:
        await fetch(AsyncMock(), "xx", 30, PLANTED, now=NOW)
    assert PLANTED not in str(exc.value)


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
async def test_region_is_normalised_before_it_reaches_get(fetch):
    get = AsyncMock(return_value=_page())
    await fetch(get, " JP ", now=NOW)
    assert get.await_args.args[0] == "jp"


# ============================================================
# Window gates: the edges
# ============================================================

def test_new_releases_gate_is_inclusive_at_both_edges():
    collect, _ = new_releases_gates(7, NOW)
    start = NOW - timedelta(days=7)
    assert collect(start)
    assert collect(NOW)
    assert not collect(start - timedelta(microseconds=1))
    assert not collect(NOW + timedelta(microseconds=1))


def test_new_releases_stops_only_once_past_the_old_edge():
    _, should_stop = new_releases_gates(7, NOW)
    start = NOW - timedelta(days=7)
    assert not should_stop(start)
    assert not should_stop(NOW)
    assert should_stop(start - timedelta(microseconds=1))


def test_new_releases_window_scales_with_days():
    collect, _ = new_releases_gates(30, NOW)
    assert collect(NOW - timedelta(days=30))
    assert not collect(NOW - timedelta(days=30, microseconds=1))


def test_coming_soon_gate_excludes_now_and_includes_the_far_edge():
    collect, _ = coming_soon_gates(7, NOW)
    end = NOW + timedelta(days=7)
    assert not collect(NOW)
    assert collect(NOW + timedelta(microseconds=1))
    assert collect(end)
    assert not collect(end + timedelta(microseconds=1))


def test_coming_soon_stops_at_now_and_before_but_not_after():
    _, should_stop = coming_soon_gates(7, NOW)
    assert should_stop(NOW)
    assert should_stop(NOW - timedelta(days=1))
    assert not should_stop(NOW + timedelta(microseconds=1))


# ============================================================
# release_datetime and the sort keys
# ============================================================

def test_release_datetime_parses_a_normalised_date():
    assert release_datetime({"releaseDate": "2021-03-02T00:00:00+00:00"}) == datetime(
        2021, 3, 2, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("book", [{}, {"releaseDate": None}, {"releaseDate": ""}, {"releaseDate": "soon"}])
def test_release_datetime_is_none_when_there_is_no_parseable_date(book):
    assert release_datetime(book) is None


def test_new_releases_sort_key_puts_undated_books_last_when_reversed():
    early = {"asin": "A", "releaseDate": "2026-01-01T00:00:00+00:00"}
    late = {"asin": "B", "releaseDate": "2026-03-01T00:00:00+00:00"}
    undated = {"asin": "C"}
    ordered = sorted([undated, early, late], key=new_releases_sort_key, reverse=True)
    assert [b["asin"] for b in ordered] == ["B", "A", "C"]


def test_coming_soon_sort_key_puts_undated_books_last():
    early = {"asin": "A", "releaseDate": "2026-01-01T00:00:00+00:00"}
    late = {"asin": "B", "releaseDate": "2026-03-01T00:00:00+00:00"}
    undated = {"asin": "C"}
    ordered = sorted([undated, late, early], key=coming_soon_sort_key)
    assert [b["asin"] for b in ordered] == ["A", "B", "C"]


def test_sort_keys_return_timezone_aware_datetimes_for_undated_books():
    assert new_releases_sort_key({}).tzinfo is not None
    assert coming_soon_sort_key({}).tzinfo is not None


# ============================================================
# walk_catalog: the request, the paging, the stops
# ============================================================

async def test_walk_sends_the_catalog_query_without_a_category_by_default():
    get = _serving(_page())
    await walk_catalog(get, "us", None, _always_in, _never)
    get.assert_awaited_once_with("us", SEARCH_PATH, {
        "num_results": RELEASE_PAGE_SIZE,
        "page": 0,
        "response_groups": BOOK_RESPONSE_GROUPS,
        "image_sizes": IMAGE_SIZES,
        "products_sort_by": "-ReleaseDate",
    })


async def test_walk_scopes_the_query_to_a_category_when_given_one():
    get = _serving(_page())
    await walk_catalog(get, "us", "CAT42", _always_in, _never)
    assert get.await_args.args[2]["category_id"] == "CAT42"


@pytest.mark.parametrize("category", [None, ""])
async def test_walk_omits_category_id_for_none_and_empty(category):
    get = _serving(_page())
    await walk_catalog(get, "us", category, _always_in, _never)
    assert "category_id" not in get.await_args.args[2]


def test_release_page_size_is_audibles_cap_of_fifty():
    assert RELEASE_PAGE_SIZE == 50


async def test_walk_requests_the_next_page_after_a_full_one_and_stops_on_a_short_one():
    full = _page(*[_product(f"B0FULL{i:04d}", "2026-06-10") for i in range(RELEASE_PAGE_SIZE)])
    short = _page(_product("B0SHORT0001", "2026-06-09"))
    get = _serving(full, short)

    result = await walk_catalog(get, "us", None, _always_in, _never)

    assert [c.args[2]["page"] for c in get.await_args_list] == [0, 1]
    assert len(result) == RELEASE_PAGE_SIZE + 1


async def test_walk_stops_on_an_empty_page():
    full = _page(*[_product(f"B0FULL{i:04d}", "2026-06-10") for i in range(RELEASE_PAGE_SIZE)])
    get = _serving(full, _page())

    result = await walk_catalog(get, "us", None, _always_in, _never)

    assert get.await_count == 2
    assert len(result) == RELEASE_PAGE_SIZE


async def test_walk_stops_when_a_page_repeats_the_one_before_it():
    full = _page(*[_product(f"B0FULL{i:04d}", "2026-06-10") for i in range(RELEASE_PAGE_SIZE)])
    get = AsyncMock(return_value=full)

    result = await walk_catalog(get, "us", None, _always_in, _never)

    assert get.await_count == 2
    assert len(result) == RELEASE_PAGE_SIZE


async def test_walk_stops_at_the_first_book_that_satisfies_should_stop_even_on_a_full_page():
    products = [_product(f"B0FULL{i:04d}", "2026-06-10") for i in range(RELEASE_PAGE_SIZE)]
    products[3] = _product("B0STOPHERE", "2026-01-01")
    get = _serving(_page(*products), _page(_product("B0NEVERSEEN", "2026-06-10")))
    stop_before = datetime(2026, 3, 1, tzinfo=timezone.utc)

    result = await walk_catalog(get, "us", None, _always_in, lambda dt: dt < stop_before)

    get.assert_awaited_once()
    assert [b["asin"] for b in result] == ["B0FULL0000", "B0FULL0001", "B0FULL0002"]


async def test_walk_collects_only_what_collect_accepts_and_keeps_walking_past_the_rest():
    get = _serving(_page(
        _product("B0IN00001", "2026-06-10"),
        _product("B0OUT0001", "2026-07-10"),
        _product("B0IN00002", "2026-06-09"),
    ))
    cutoff = datetime(2026, 7, 1, tzinfo=timezone.utc)

    result = await walk_catalog(get, "us", None, lambda dt: dt < cutoff, _never)

    assert [b["asin"] for b in result] == ["B0IN00001", "B0IN00002"]


async def test_walk_skips_books_with_no_parseable_date():
    get = _serving(_page(
        _product("B0DATED001", "2026-06-10"),
        {"asin": "B0NODATE01", "title": "No date"},
        _product("B0BADDATE1", "not-a-date"),
    ))

    result = await walk_catalog(get, "us", None, _always_in, _never)

    assert [b["asin"] for b in result] == ["B0DATED001"]


async def test_walk_dedupes_by_asin_across_pages():
    page0 = _page(*[_product(f"B0FULL{i:04d}", "2026-06-10") for i in range(RELEASE_PAGE_SIZE)])
    repeat_one = [_product("B0FULL0000", "2026-06-10")]
    get = _serving(page0, _page(*repeat_one))

    result = await walk_catalog(get, "us", None, _always_in, _never)

    assert len(result) == RELEASE_PAGE_SIZE
    assert [b["asin"] for b in result].count("B0FULL0000") == 1


async def test_walk_returns_books_exactly_as_normalize_product_made_them():
    product = _product("B0EXACT001", "2026-06-10")
    get = _serving(_page(product))

    result = await walk_catalog(get, "de", None, _always_in, _never)

    assert result == [normalize_product(product, "de")]


async def test_walk_with_a_missing_products_key_is_empty():
    assert await walk_catalog(AsyncMock(return_value={}), "us", None, _always_in, _never) == []


@pytest.mark.parametrize("exc", [NotFoundException("gone"), AudibleAPIException("down")])
async def test_walk_propagates_failures_from_get(exc):
    get = AsyncMock(side_effect=exc)
    with pytest.raises(type(exc)) as raised:
        await walk_catalog(get, "us", None, _always_in, _never)
    assert raised.value is exc


# ============================================================
# fetch_new_releases and fetch_coming_soon: window and order
# ============================================================

async def test_new_releases_keeps_the_window_skips_preorders_and_sorts_newest_first():
    # The in-window books arrive oldest first here; the sort is what puts the
    # newest ahead, whatever order the walk met them in.
    get = _serving(_page(
        _product("B0PREORDER", "2026-06-20"),
        _product("B0OLDER001", "2026-06-10"),
        _product("B0NEWER001", "2026-06-15"),
        _product("B0TOOOLD01", "2026-06-05"),
        _product("B0NEVERSEE", "2026-06-12"),
    ))

    result = await fetch_new_releases(get, "us", 7, now=NOW)

    assert [b["asin"] for b in result] == ["B0NEWER001", "B0OLDER001"]


async def test_new_releases_stops_walking_once_past_the_window():
    get = _serving(
        _page(*[_product(f"B0FULL{i:04d}", "2026-06-10") for i in range(RELEASE_PAGE_SIZE - 1)],
              _product("B0TOOOLD01", "2026-01-01")),
        _page(_product("B0NEVERSEE", "2026-06-10")),
    )

    result = await fetch_new_releases(get, "us", 7, now=NOW)

    get.assert_awaited_once()
    assert len(result) == RELEASE_PAGE_SIZE - 1


async def test_new_releases_window_follows_days():
    products = [_product("B0TENDAYS1", "2026-06-05"), _product("B0TWODAYS1", "2026-06-13")]
    narrow = await fetch_new_releases(_serving(_page(*products[::-1])), "us", 3, now=NOW)
    wide = await fetch_new_releases(_serving(_page(*products[::-1])), "us", 30, now=NOW)

    assert [b["asin"] for b in narrow] == ["B0TWODAYS1"]
    assert [b["asin"] for b in wide] == ["B0TWODAYS1", "B0TENDAYS1"]


async def test_coming_soon_keeps_the_window_skips_beyond_and_released_and_sorts_soonest_first():
    get = _serving(_page(
        _product("B0FARAWAY1", "2026-07-01"),
        _product("B0THIRD001", "2026-06-21"),
        _product("B0FIRST001", "2026-06-16"),
        _product("B0SECOND01", "2026-06-18"),
        _product("B0ALREADY1", "2026-06-15"),
        _product("B0NEVERSEE", "2026-06-17"),
    ))

    result = await fetch_coming_soon(get, "us", 7, now=NOW)

    assert [b["asin"] for b in result] == ["B0FIRST001", "B0SECOND01", "B0THIRD001"]


async def test_coming_soon_stops_walking_once_a_title_is_already_out():
    get = _serving(
        _page(*[_product(f"B0FULL{i:04d}", "2026-06-20") for i in range(RELEASE_PAGE_SIZE - 1)],
              _product("B0ALREADY1", "2026-06-01")),
        _page(_product("B0NEVERSEE", "2026-06-20")),
    )

    result = await fetch_coming_soon(get, "us", 30, now=NOW)

    get.assert_awaited_once()
    assert len(result) == RELEASE_PAGE_SIZE - 1


def _freeze_default_now(monkeypatch, instant):
    """Pins the clock the module reads for a default `now`; returns the calls made to it."""
    calls = []

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(tz)
            return instant

    monkeypatch.setattr(releases_module, "datetime", _Frozen)
    return calls


async def test_new_releases_default_now_is_the_current_utc_time(monkeypatch):
    calls = _freeze_default_now(monkeypatch, NOW)
    get = _serving(_page(
        _product("B0RECENT01", "2026-06-13"),
        _product("B0OLD00001", "2026-03-17"),
    ))

    result = await fetch_new_releases(get, "us")

    assert calls == [timezone.utc]
    assert [b["asin"] for b in result] == ["B0RECENT01"]


async def test_coming_soon_default_now_is_the_current_utc_time(monkeypatch):
    calls = _freeze_default_now(monkeypatch, NOW)
    get = _serving(_page(
        _product("B0SOON0001", "2026-06-18"),
        _product("B0OUT00001", "2026-06-12"),
    ))

    result = await fetch_coming_soon(get, "us")

    assert calls == [timezone.utc]
    assert [b["asin"] for b in result] == ["B0SOON0001"]


async def test_an_injected_now_never_reads_the_clock(monkeypatch):
    calls = _freeze_default_now(monkeypatch, NOW + timedelta(days=400))
    await fetch_new_releases(_serving(_page()), "us", now=NOW)
    await fetch_coming_soon(_serving(_page()), "us", now=NOW)
    assert calls == []


async def test_the_default_window_is_thirty_days():
    inside = _product("B0INSIDE01", "2026-05-17")
    outside = _product("B0OUTSIDE1", "2026-05-14")
    result = await fetch_new_releases(_serving(_page(inside, outside)), "us", now=NOW)
    assert [b["asin"] for b in result] == ["B0INSIDE01"]

    inside = _product("B0INSIDE02", "2026-07-14")
    outside = _product("B0OUTSIDE2", "2026-07-16")
    result = await fetch_coming_soon(_serving(_page(outside, inside)), "us", now=NOW)
    assert [b["asin"] for b in result] == ["B0INSIDE02"]


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
async def test_category_is_passed_through_to_the_walk(fetch):
    get = _serving(_page())
    await fetch(get, "us", 7, "CAT42", now=NOW)
    assert get.await_args.args[2]["category_id"] == "CAT42"


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
async def test_no_category_means_no_category_id(fetch):
    get = _serving(_page())
    await fetch(get, "us", 7, now=NOW)
    assert "category_id" not in get.await_args.args[2]


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
async def test_region_is_passed_through_to_get(fetch):
    get = _serving(_page())
    await fetch(get, "jp", 7, now=NOW)
    assert get.await_args.args[0] == "jp"


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
@pytest.mark.parametrize("exc", [NotFoundException("gone"), AudibleAPIException("down")])
async def test_fetches_propagate_failures_from_get(fetch, exc):
    get = AsyncMock(side_effect=exc)
    with pytest.raises(type(exc)) as raised:
        await fetch(get, "us", 7, now=NOW)
    assert raised.value is exc


# ============================================================
# Taxonomy: fetch and flatten
# ============================================================

def _tax(*specs):
    """Builds a /categories response from (id, name[, [children]]) specs."""
    def node(spec):
        if len(spec) == 2:
            return {"id": spec[0], "name": spec[1], "children": []}
        return {"id": spec[0], "name": spec[1], "children": [node(c) for c in spec[2]]}
    return {"categories": [node(s) for s in specs]}


def test_taxonomy_constants():
    assert CATEGORIES_PATH == "/1.0/catalog/categories"
    assert GENRE_TAXONOMY_LEVELS == 5


async def test_fetch_catalog_genres_asks_for_the_whole_genre_tree():
    get = AsyncMock(return_value=_tax())
    await fetch_catalog_genres(get, "de")
    get.assert_awaited_once_with("de", CATEGORIES_PATH, {"root": "Genres", "categories_num_levels": 5})


async def test_fetch_catalog_genres_returns_the_flattened_rows():
    get = AsyncMock(return_value=_tax(("P1", "History", [("L1", "Ancient")]), ("P2", "Sci-Fi")))

    nodes = await fetch_catalog_genres(get, "us")

    assert nodes == [
        {"genre_id": "P1", "name": "History", "parent_id": ""},
        {"genre_id": "L1", "name": "Ancient", "parent_id": "P1"},
        {"genre_id": "P2", "name": "Sci-Fi", "parent_id": ""},
    ]


@pytest.mark.parametrize("exc", [NotFoundException("gone"), AudibleAPIException("down")])
async def test_fetch_catalog_genres_propagates_failures_from_get(exc):
    get = AsyncMock(side_effect=exc)
    with pytest.raises(type(exc)) as raised:
        await fetch_catalog_genres(get, "us")
    assert raised.value is exc


def test_flatten_recurses_a_ragged_tree_to_every_depth():
    nodes = flatten_genre_nodes(_tax(
        ("P1", "Arts", [
            ("C1", "Performing", [("G1", "Film", [("GG1", "Direction", [("GGG1", "Deep")])])]),
            ("C2", "Architecture"),
        ]),
        ("P2", "History"),
    ))
    assert {(n["genre_id"], n["parent_id"]) for n in nodes} == {
        ("P1", ""), ("C1", "P1"), ("G1", "C1"), ("GG1", "G1"), ("GGG1", "GG1"),
        ("C2", "P1"), ("P2", ""),
    }


def test_flatten_keeps_a_node_once_per_parent():
    nodes = flatten_genre_nodes(_tax(
        ("P1", "History", [("LX", "Shared")]),
        ("P2", "Society", [("LX", "Shared")]),
    ))
    assert sorted((n["genre_id"], n["parent_id"]) for n in nodes if n["genre_id"] == "LX") == [
        ("LX", "P1"), ("LX", "P2"),
    ]


def test_flatten_dedupes_the_same_node_under_the_same_parent():
    nodes = flatten_genre_nodes(_tax(("P1", "History", [("L1", "A"), ("L1", "A")])))
    assert [n for n in nodes if n["genre_id"] == "L1"] == [
        {"genre_id": "L1", "name": "A", "parent_id": "P1"}
    ]


def test_flatten_skips_a_node_without_an_id_or_a_name_but_still_descends_into_an_unnamed_one():
    data = {"categories": [
        {"name": "No id", "children": [{"id": "X1", "name": "Orphaned"}]},
        {"id": "NONAME", "children": [{"id": "C1", "name": "Child"}]},
    ]}
    nodes = flatten_genre_nodes(data)
    assert {(n["genre_id"], n["parent_id"]) for n in nodes} == {("C1", "NONAME")}


@pytest.mark.parametrize("data", [{}, {"categories": []}])
def test_flatten_of_nothing_is_empty(data):
    assert flatten_genre_nodes(data) == []


# ============================================================
# Taxonomy: build_category_tree
# ============================================================

def _rows():
    # Ids are chosen so that id order and name order disagree at every level.
    #  Fiction (G) -> Zombies (Y), Adventure (Z) -> Pirates (P), Shared (S)
    #  Biography (H) -> Shared (S)
    def row(gid, name, parent):
        return {"genre_id": gid, "name": name, "parent_id": parent}
    return [
        row("G", "Fiction", ""),
        row("H", "Biography", ""),
        row("Y", "Zombies", "G"),
        row("Z", "Adventure", "G"),
        row("P", "Pirates", "Z"),
        row("S", "Shared", "G"),
        row("S", "Shared", "H"),
    ]


def test_tree_is_nested_and_sorted_by_name_at_every_level():
    tree = build_category_tree(_rows())

    assert all(isinstance(n, CategoryNode) for n in tree)
    assert [n.name for n in tree] == ["Biography", "Fiction"]
    fiction = tree[1]
    assert [c.name for c in fiction.children] == ["Adventure", "Shared", "Zombies"]
    assert [g.name for g in fiction.children[0].children] == ["Pirates"]
    assert [c.name for c in tree[0].children] == ["Shared"]


@pytest.mark.parametrize("flat", [False, True])
def test_empty_rows_build_an_empty_result(flat):
    assert build_category_tree([], flat=flat) == []


def test_tree_depth_one_is_the_top_level_with_no_children():
    tree = build_category_tree(_rows(), depth=1)
    assert [n.name for n in tree] == ["Biography", "Fiction"]
    assert all(n.children == [] for n in tree)


def test_tree_depth_two_stops_below_the_second_level():
    tree = build_category_tree(_rows(), depth=2)
    fiction = next(n for n in tree if n.id == "G")
    assert [c.name for c in fiction.children] == ["Adventure", "Shared", "Zombies"]
    assert all(c.children == [] for c in fiction.children)


def test_tree_depth_beyond_the_taxonomy_is_the_whole_tree():
    assert build_category_tree(_rows(), depth=9) == build_category_tree(_rows())


def test_flat_lists_every_node_at_every_level_once_per_parent_with_root_first_ancestors():
    flat = build_category_tree(_rows(), flat=True)

    assert all(isinstance(n, FlatCategoryNode) for n in flat)
    assert [(n.name, [a.name for a in n.ancestors]) for n in flat] == [
        ("Biography", []),
        ("Shared", ["Biography"]),
        ("Fiction", []),
        ("Adventure", ["Fiction"]),
        ("Pirates", ["Fiction", "Adventure"]),
        ("Shared", ["Fiction"]),
        ("Zombies", ["Fiction"]),
    ]
    assert all(isinstance(a, CategoryAncestor) for n in flat for a in n.ancestors)


def test_flat_ancestors_carry_ids_as_well_as_names():
    pirates = next(n for n in build_category_tree(_rows(), flat=True) if n.id == "P")
    assert [(a.id, a.name) for a in pirates.ancestors] == [("G", "Fiction"), ("Z", "Adventure")]


def test_flat_depth_one_is_only_the_top_level():
    flat = build_category_tree(_rows(), flat=True, depth=1)
    assert [n.name for n in flat] == ["Biography", "Fiction"]
    assert all(n.ancestors == [] for n in flat)


def test_flat_depth_two_includes_the_second_level_and_nothing_below():
    flat = build_category_tree(_rows(), flat=True, depth=2)
    assert [n.name for n in flat] == [
        "Biography", "Shared", "Fiction", "Adventure", "Shared", "Zombies",
    ]
    assert "Pirates" not in [n.name for n in flat]


@pytest.mark.parametrize("flat", [False, True])
@pytest.mark.parametrize("depth", [0, -1])
def test_depth_below_one_is_rejected(flat, depth):
    with pytest.raises(ValueError) as exc:
        build_category_tree(_rows(), flat=flat, depth=depth)
    assert str(exc.value) == "depth must be at least 1"


def test_flat_and_depth_are_keyword_only():
    parameters = inspect.signature(build_category_tree).parameters
    assert parameters["flat"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["depth"].kind is inspect.Parameter.KEYWORD_ONLY


def test_tree_does_not_mutate_the_rows():
    rows = _rows()
    snapshot = [dict(r) for r in rows]
    build_category_tree(rows)
    build_category_tree(rows, flat=True, depth=2)
    assert rows == snapshot


# ============================================================
# Signatures
# ============================================================

@pytest.mark.parametrize("fn", [fetch_new_releases, fetch_coming_soon, walk_catalog, fetch_catalog_genres])
def test_get_is_a_required_first_argument(fn):
    parameters = inspect.signature(fn).parameters
    assert next(iter(parameters)) == "get"
    assert parameters["get"].default is inspect.Parameter.empty


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
async def test_fetches_without_get_are_a_type_error(fetch):
    with pytest.raises(TypeError):
        await fetch("us")


@pytest.mark.parametrize("fetch", [fetch_new_releases, fetch_coming_soon])
def test_now_is_keyword_only_and_days_defaults_to_thirty(fetch):
    parameters = inspect.signature(fetch).parameters
    assert parameters["now"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["now"].default is None
    assert parameters["days"].default == 30
    assert parameters["category"].default is None


# ============================================================
# The module's own rules: no logger, no environment, no app
# ============================================================

def _module_tree() -> ast.Module:
    return ast.parse(Path(releases_module.__file__).read_text())


def test_releases_module_has_no_logger():
    assert not hasattr(releases_module, "logger")
    names = {n.id for n in ast.walk(_module_tree()) if isinstance(n, ast.Name)}
    assert "logger" not in names
    imported = {
        a.name for n in ast.walk(_module_tree()) if isinstance(n, ast.Import) for a in n.names
    } | {
        n.module for n in ast.walk(_module_tree()) if isinstance(n, ast.ImportFrom)
    }
    assert not any(m and ("logging" in m or "logger" in m) for m in imported)


def test_releases_module_reads_no_environment():
    tree = _module_tree()
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not ({"environ", "getenv"} & (attrs | names))
    assert "os" not in names


def test_releases_module_imports_nothing_from_the_hosted_app():
    imported = {
        a.name for n in ast.walk(_module_tree()) if isinstance(n, ast.Import) for a in n.names
    } | {
        n.module for n in ast.walk(_module_tree()) if isinstance(n, ast.ImportFrom)
    }
    assert not any(m and m.split(".")[0] in {"app", "sqlalchemy"} for m in imported)


def test_releases_module_is_inside_the_isolation_walk():
    from tests.libex_core.test_isolation import _EXPECTED_MODULES
    assert "libex_core.audible.releases" in _EXPECTED_MODULES
    assert Path(releases_module.__file__).is_relative_to(Path(libex_core.__file__).parent)
