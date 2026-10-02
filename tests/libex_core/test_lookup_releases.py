"""
libex_core.lookup releases: new_releases, coming_soon and categories. Checks
the windows and category ids refused before any request, the date ordering of
each window, the category tree in nested, flat and depth-limited form, and the
pinned deviation that Audible's 404 stays NotFoundException where the hosted
routes answer 503. Nothing touches a network.
"""

# Standard library
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import RELEASE_WINDOWS, categories, coming_soon, new_releases
from libex_core.models import BookResponse, CategoryNode, FlatCategoryNode
from tests.libex_core._lookup_support import (
    BOOKS,
    FUTURE,
    empty_get,
    fake_get,
    not_found_get,
    outage_get,
)

# Section: new releases


async def test_new_releases_are_the_released_books_newest_first_and_skip_pre_orders():
    result = await new_releases(fake_get, 60)
    assert all(isinstance(b, BookResponse) for b in result)
    assert [b.asin for b in result] == list(BOOKS)
    assert not set(FUTURE) & {b.asin for b in result}
    dates = [b.releaseDate for b in result]
    assert dates == sorted(dates, reverse=True)


async def test_new_releases_window_edge_is_respected():
    # The four books are 10, 17, 24 and 31 days old.
    assert len(await new_releases(fake_get, 30)) == 3
    assert len(await new_releases(fake_get, 60)) == 4


async def test_new_releases_category_is_sent_to_audible():
    seen = []

    async def get(region, path, params=None, extra_headers=None):
        seen.append(params)
        return await fake_get(region, path, params, extra_headers)

    await new_releases(get, 60, "123")
    assert seen and all(p.get("category_id") == "123" for p in seen)
    seen.clear()
    await new_releases(get, 60)
    assert seen and all("category_id" not in p for p in seen)


async def test_new_releases_default_is_thirty_days_newest_first_with_no_sort_applied():
    result = await new_releases(fake_get)
    assert len(result) == 3


async def test_new_releases_shaping_comes_after_the_scan():
    result = await new_releases(
        fake_get, 365, filters={"longer_than": 150}, sort="lengthMinutes", order="asc"
    )
    assert [b.lengthMinutes for b in result] == [200, 300, 400]


async def test_new_releases_a_filter_that_leaves_none_is_not_found():
    with pytest.raises(NotFoundException):
        await new_releases(fake_get, 365, filters={"language": "klingon"})


async def test_new_releases_an_empty_window_is_not_found():
    with pytest.raises(NotFoundException):
        await new_releases(empty_get, 30)


# Section: coming soon


async def test_coming_soon_are_the_pre_orders_soonest_first():
    result = await coming_soon(fake_get, 30)
    assert [b.asin for b in result] == list(FUTURE)


async def test_coming_soon_nothing_upcoming_is_not_found():
    with pytest.raises(NotFoundException):
        await coming_soon(empty_get, 30)


async def test_coming_soon_shaping_applies():
    with pytest.raises(NotFoundException):
        await coming_soon(fake_get, 30, filters={"language": "klingon"})


# Section: Audible's answers


@pytest.mark.parametrize("fn", [new_releases, coming_soon])
async def test_a_404_from_audible_stays_not_found_and_is_not_an_outage(fn):
    """Pinned deviation: the hosted routes answer 503 here, but the library
    has no stored copy to answer from, and a 404 is Audible's own answer."""
    with pytest.raises(NotFoundException):
        await fn(not_found_get, 30)


async def test_a_404_on_the_taxonomy_stays_not_found():
    with pytest.raises(NotFoundException):
        await categories(not_found_get)


@pytest.mark.parametrize("call", [
    lambda: new_releases(outage_get, 30),
    lambda: coming_soon(outage_get, 30),
    lambda: categories(outage_get),
], ids=["new", "soon", "categories"])
async def test_an_outage_is_never_an_empty_window(call):
    with pytest.raises(AudibleAPIException) as caught:
        await call()
    assert caught.value.upstream_status == 503


# Section: windows and category ids


@pytest.mark.parametrize("days", [0, 1, 29, 31, 366, -30, True, "30", None, 30.0 + 0.5])
async def test_a_window_outside_the_offered_ones_is_refused_before_any_request(days):
    get = AsyncMock()
    for fn in (new_releases, coming_soon):
        with pytest.raises(ValueError):
            await fn(get, days)
    get.assert_not_called()


@pytest.mark.parametrize("days", RELEASE_WINDOWS)
async def test_every_offered_window_is_accepted(days):
    await new_releases(fake_get, days)


def test_the_offered_windows_are_the_hosted_ones():
    assert RELEASE_WINDOWS == (30, 60, 90, 120, 240, 365)


@pytest.mark.parametrize("category", ["", "abc", "12a", "-1", "1 2", "1234567890123", "1;2", "123\n", "\n123", 5, ["1"]])
async def test_a_category_that_is_not_a_numeric_id_is_refused_before_any_request(category):
    get = AsyncMock()
    for fn in (new_releases, coming_soon):
        with pytest.raises(ValueError):
            await fn(get, 30, category)
    get.assert_not_called()


async def test_a_twelve_digit_category_is_accepted():
    await new_releases(fake_get, 30, "123456789012")


# Section: categories


async def test_categories_default_is_the_nested_tree_ordered_by_name():
    result = await categories(fake_get)
    assert all(isinstance(n, CategoryNode) for n in result)
    assert [n.name for n in result] == ["Alpha", "Zed"]
    assert [c.id for c in result[1].children] == ["3"]
    assert result[0].children == []


async def test_categories_flat_lists_every_node_with_its_ancestors_root_first():
    result = await categories(fake_get, flat=True)
    assert all(isinstance(n, FlatCategoryNode) for n in result)
    by_id = {n.id: n for n in result}
    assert set(by_id) == {"1", "2", "3"}
    assert [a.id for a in by_id["3"].ancestors] == ["1"]
    assert by_id["1"].ancestors == []


async def test_categories_depth_one_is_the_top_level_only():
    nested = await categories(fake_get, depth=1)
    assert [n.id for n in nested] == ["2", "1"]
    assert nested[1].children == []
    flat = await categories(fake_get, flat=True, depth=1)
    assert {n.id for n in flat} == {"1", "2"}


async def test_categories_depth_two_reaches_the_children():
    flat = await categories(fake_get, flat=True, depth=2)
    assert {n.id for n in flat} == {"1", "2", "3"}


@pytest.mark.parametrize("depth", [0, -1, True, "2", 1.5])
async def test_categories_a_bad_depth_is_refused_before_any_request(depth):
    get = AsyncMock()
    with pytest.raises(ValueError):
        await categories(get, depth=depth)
    get.assert_not_called()


async def test_categories_an_empty_taxonomy_is_not_found():
    with pytest.raises(NotFoundException):
        await categories(empty_get)
