"""
libex_core.lookup.search_series: the first ten catalog products titled with the
name, the series they name fetched once each in the order found, with an
unknown series left out and a failed one left out with a warning. Pins the
deviation that every candidate failing is an outage, not a 404. Nothing
touches a network.
"""

# Standard library
import logging

# Third party
import pytest

# Local
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import search_series
from libex_core.models import SeriesResponse
from tests.libex_core._lookup_support import SERIES, empty_get, fake_get, outage_get


async def test_search_series_returns_the_published_model():
    result = await search_series(fake_get, "q")
    assert [type(s) for s in result] == [SeriesResponse]
    assert result[0].asin == SERIES


async def test_search_series_asks_for_ten_products_titled_with_the_name():
    seen = []

    async def get(region, path, params=None, extra_headers=None):
        seen.append((path, params))
        return await fake_get(region, path, params, extra_headers)

    await search_series(get, "the name")
    path, params = seen[0]
    assert path == "/1.0/catalog/products"
    assert params["title"] == "the name"
    assert params["num_results"] == 10


async def test_search_series_names_each_series_once_in_the_order_found():
    fetched = []

    async def get(region, path, params=None, extra_headers=None):
        if "title" in params:
            return {"products": [
                {"asin": "B0SRCH0001", "relationships": [
                    {"relationship_type": "series", "asin": "B0SERIESBB"},
                    {"relationship_type": "series", "asin": "B0SERIESAA"},
                    {"relationship_type": "season", "asin": "B0NOTSERIE"},
                ]},
                {"asin": "B0SRCH0002", "relationships": [
                    {"relationship_type": "series", "asin": "B0SERIESBB"},
                ]},
            ]}
        fetched.append(path.rsplit("/", 1)[-1])
        return await fake_get(region, "/series/x", params, extra_headers)

    await search_series(get, "q")
    assert fetched == ["B0SERIESBB", "B0SERIESAA"]


async def test_search_series_leaves_out_a_series_audible_has_no_record_of():
    result = await search_series(fake_get, "q")
    assert [s.asin for s in result] == [SERIES], "B0SERIES02 is a 404 and is skipped"


async def test_search_series_leaves_out_a_failed_series_and_warns(caplog):
    async def get(region, path, params=None, extra_headers=None):
        if path.endswith("B0SERIES02"):
            raise AudibleAPIException("boom", upstream_status=503)
        return await fake_get(region, path, params, extra_headers)

    with caplog.at_level(logging.WARNING, logger="libex"):
        result = await search_series(get, "q")
    assert [s.asin for s in result] == [SERIES]
    assert any("skipping" in r.getMessage() for r in caplog.records)


async def test_search_series_no_series_named_is_not_found():
    with pytest.raises(NotFoundException):
        await search_series(empty_get, "q")


async def test_search_series_every_candidate_failing_is_an_outage_not_an_absence():
    async def get(region, path, params=None, extra_headers=None):
        if "title" in params:
            return await fake_get(region, path, params, extra_headers)
        raise AudibleAPIException("boom", upstream_status=503)

    with pytest.raises(AudibleAPIException):
        await search_series(get, "q")


async def test_search_series_every_candidate_unknown_is_not_found_not_an_outage():
    async def get(region, path, params=None, extra_headers=None):
        if "title" in params:
            return await fake_get(region, path, params, extra_headers)
        raise NotFoundException("gone")

    with pytest.raises(NotFoundException):
        await search_series(get, "q")


async def test_search_series_the_search_failing_is_an_outage():
    with pytest.raises(AudibleAPIException) as caught:
        await search_series(outage_get, "q")
    assert caught.value.upstream_status == 503
