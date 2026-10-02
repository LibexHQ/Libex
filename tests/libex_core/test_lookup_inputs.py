"""
What every new libex_core.lookup function does with its inputs: an unknown
region is refused before any request, each of the eleven regions is accepted
and is the one sent to Audible, and nothing a caller typed (a name, an ASIN, a
category, a filter) comes back in an exception message or a log record.
Nothing touches a network.
"""

# Standard library
import logging
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.exceptions import (
    AudibleAPIException,
    NotFoundException,
    RegionException,
)
from libex_core.lookup import (
    categories,
    coming_soon,
    get_author,
    get_author_books,
    get_author_books_by_name,
    get_books,
    get_series_books,
    new_releases,
    search_authors,
    search_series,
)
from tests.libex_core._lookup_support import (
    AUTHOR,
    AUTHOR_NAME,
    PLANTED,
    SERIES,
    asins,
    fake_get,
    product,
)

REGIONS = ["us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"]

CALLS = {
    "search_series": lambda get, region: search_series(get, "q", region=region),
    "get_author": lambda get, region: get_author(get, AUTHOR, region=region),
    "search_authors": lambda get, region: search_authors(get, "jane", region=region),
    "get_author_books": lambda get, region: get_author_books(get, AUTHOR, region=region),
    "get_author_books_by_name": lambda get, region: get_author_books_by_name(
        get, AUTHOR_NAME, region=region),
    "get_series_books": lambda get, region: get_series_books(get, SERIES, region=region),
    "get_books": lambda get, region: get_books(get, asins(3), region=region),
    "new_releases": lambda get, region: new_releases(get, 60, region=region),
    "coming_soon": lambda get, region: coming_soon(get, 30, region=region),
    "categories": lambda get, region: categories(get, region=region),
}

# Section: region


@pytest.mark.parametrize("bad", ["xx", "", "usa", "us ;", PLANTED])
@pytest.mark.parametrize("fn", CALLS)
async def test_an_unknown_region_is_refused_before_any_request(fn, bad):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await CALLS[fn](get, bad)
    get.assert_not_called()


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("fn", CALLS)
async def test_each_of_the_eleven_regions_is_accepted_and_is_the_one_asked(fn, region):
    seen = set()

    async def get(r, path, params=None, extra_headers=None):
        seen.add(r)
        return await fake_get(r, path, params, extra_headers)

    try:
        await CALLS[fn](get, region)
    except (NotFoundException, AudibleAPIException):
        pass  # the fake's answer may be thin; the region it was asked is the point
    assert seen == {region}


@pytest.mark.parametrize("fn", CALLS)
async def test_region_is_normalised_not_rejected_for_case_and_padding(fn):
    seen = set()

    async def get(r, path, params=None, extra_headers=None):
        seen.add(r)
        return await fake_get(r, path, params, extra_headers)

    try:
        await CALLS[fn](get, " DE ")
    except (NotFoundException, AudibleAPIException):
        pass
    assert seen == {"de"}


# Section: no caller input in messages or logs


def _everything_logged(caplog):
    return " ".join(
        f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records
    )


async def _planted_name_get(region, path, params=None, extra_headers=None):
    """Answers a search on the planted name with a book credited to it, so the
    success path runs with the planted text in play."""
    if "author" in params or "title" in params:
        return {"total_results": 1, "products": [
            product("B0PLANT001", authors=[{"asin": AUTHOR, "name": PLANTED}])
        ]}
    if "asins" in params:
        return {"products": [
            product(a, authors=[{"asin": AUTHOR, "name": PLANTED}])
            for a in params["asins"].split(",")
        ]}
    return await fake_get(region, path, params, extra_headers)


async def _failing_get(region, path, params=None, extra_headers=None):
    raise AudibleAPIException("boom", upstream_status=503)


async def _missing_get(region, path, params=None, extra_headers=None):
    raise NotFoundException("nothing")


NAME_CALLS = {
    "search_series": lambda get: search_series(get, PLANTED),
    "search_authors": lambda get: search_authors(get, PLANTED),
    "get_author_books_by_name": lambda get: get_author_books_by_name(get, PLANTED),
}


@pytest.mark.parametrize("get", [_planted_name_get, _failing_get, _missing_get, fake_get],
                         ids=["answers", "outage", "404", "unrelated"])
@pytest.mark.parametrize("fn", NAME_CALLS)
async def test_a_typed_name_is_in_no_message_and_no_log_record(fn, get, caplog):
    with caplog.at_level(logging.DEBUG, logger="libex"):
        try:
            await NAME_CALLS[fn](get)
        except (NotFoundException, AudibleAPIException) as exc:
            assert PLANTED not in str(exc)
            assert PLANTED not in repr(exc.__dict__)
            cause = exc.__cause__
            while cause is not None:
                assert PLANTED not in str(cause)
                cause = cause.__cause__
    assert PLANTED not in _everything_logged(caplog)


@pytest.mark.parametrize("call", [
    lambda: get_author(AsyncMock(), PLANTED),
    lambda: get_author_books(AsyncMock(), PLANTED),
    lambda: get_series_books(AsyncMock(), PLANTED),
    lambda: get_books(AsyncMock(), [PLANTED]),
    lambda: new_releases(AsyncMock(), 30, PLANTED),
    lambda: coming_soon(AsyncMock(), 30, PLANTED),
    lambda: new_releases(AsyncMock(), PLANTED),
    lambda: categories(AsyncMock(), depth=PLANTED),
], ids=["author", "author books", "series books", "bulk", "new category",
        "soon category", "new days", "depth"])
async def test_a_refused_value_is_not_repeated(call, caplog):
    with caplog.at_level(logging.DEBUG, logger="libex"):
        with pytest.raises((NotFoundException, ValueError)) as caught:
            await call()
    assert PLANTED not in str(caught.value)
    assert PLANTED not in repr(caught.value.args)
    assert PLANTED not in _everything_logged(caplog)


@pytest.mark.parametrize("fn", ["get_author", "get_author_books", "get_series_books"])
async def test_an_outage_log_names_the_validated_asin_not_the_raw_input(fn, caplog):
    raw = AUTHOR.lower() if fn != "get_series_books" else SERIES.lower()
    call = {
        "get_author": lambda: get_author(_failing_get, raw),
        "get_author_books": lambda: get_author_books(_failing_get, raw),
        "get_series_books": lambda: get_series_books(_failing_get, raw),
    }[fn]
    with caplog.at_level(logging.DEBUG, logger="libex"):
        with pytest.raises(AudibleAPIException):
            await call()
    assert raw not in _everything_logged(caplog)
