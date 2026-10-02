"""
libex_core.lookup against the hosted routes: the same fake Audible answers fed
to each, with the hosted route driven through the test client (cache, database
and persistence stubbed out) and the lookup called directly, must give the same
body, in the same order, in us, de and jp. Covers the plain and the filtered
and sorted form of every route that takes them, the not-found and outage
statuses, and what an incomplete author-books list reports.
"""

# Standard library
import asyncio
import json
import re

# Third party
import pytest

# Local
from libex_core import lookup as L
from libex_core.exceptions import AudibleAPIException, NotFoundException
from tests.libex_core import _lookup_support as support
from tests.libex_core._lookup_support import (
    AUTHOR,
    AUTHOR_NAME,
    BOOKS,
    CATALOGUE,
    SERIES,
    empty_get,
    fake_get,
    not_found_get,
    outage_get,
)

REGIONS = ["us", "de", "jp"]

# Section: comparing bodies


def _dump(result):
    if isinstance(result, list):
        return [_dump(item) for item in result]
    if hasattr(result, "books") and hasattr(result, "complete"):
        return _dump(result.books)
    return result.model_dump(mode="json", by_alias=True)


def _canon(value):
    """JSON with the one field that is a clock reading blanked."""
    return re.sub(r'"updatedAt": "[^"]*"', '"updatedAt": "X"', json.dumps(value, sort_keys=True))


# Section: cases

FILTERS = {"longer_than": 150, "language": "english"}
SHAPED = {"longer_than": 150, "language": "english", "sort": "lengthMinutes", "order": "desc"}
SHAPE_KW = {"filters": FILTERS, "sort": "lengthMinutes", "order": "desc"}
ALL_ASINS = list(BOOKS) + ["B0MISSING1"]

# (id, route, query, lookup call). The shaped variants carry the same filter,
# sort and order on both sides, so a route and its lookup that disagree about
# any of them fail here.
CASES = [
    ("series search", "/series/search", {"name": "q"},
     lambda get, r: L.search_series(get, "q", region=r)),
    ("author profile", f"/author/{AUTHOR}", {},
     lambda get, r: L.get_author(get, AUTHOR, region=r)),
    ("author search", "/author", {"name": "jane"},
     lambda get, r: L.search_authors(get, "jane", region=r)),
    ("author books asin", f"/author/books/{AUTHOR}", {},
     lambda get, r: L.get_author_books(get, AUTHOR, region=r)),
    ("author books asin shaped", f"/author/books/{AUTHOR}", SHAPED,
     lambda get, r: L.get_author_books(get, AUTHOR, region=r, **SHAPE_KW)),
    ("author books name", "/author/books", {"name": AUTHOR_NAME},
     lambda get, r: L.get_author_books_by_name(get, AUTHOR_NAME, region=r)),
    ("author books name shaped", "/author/books", {"name": AUTHOR_NAME, **SHAPED},
     lambda get, r: L.get_author_books_by_name(get, AUTHOR_NAME, region=r, **SHAPE_KW)),
    ("series books", f"/series/books/{SERIES}", {},
     lambda get, r: L.get_series_books(get, SERIES, region=r)),
    ("series books shaped", f"/series/books/{SERIES}", SHAPED,
     lambda get, r: L.get_series_books(get, SERIES, region=r, **SHAPE_KW)),
    ("bulk", "/book", {"asins": ",".join(ALL_ASINS)},
     lambda get, r: L.get_books(get, ALL_ASINS, region=r)),
    ("bulk shaped", "/book", {"asins": ",".join(ALL_ASINS), **SHAPED},
     lambda get, r: L.get_books(get, ALL_ASINS, region=r, **SHAPE_KW)),
    ("new releases", "/new-releases", {"days": 60},
     lambda get, r: L.new_releases(get, 60, region=r)),
    ("new releases shaped", "/new-releases", {"days": 365, **SHAPED},
     lambda get, r: L.new_releases(get, 365, region=r, **SHAPE_KW)),
    ("new releases category", "/new-releases", {"days": 365, "category": "123"},
     lambda get, r: L.new_releases(get, 365, "123", region=r)),
    ("coming soon", "/coming-soon", {"days": 30},
     lambda get, r: L.coming_soon(get, 30, region=r)),
    ("coming soon shaped", "/coming-soon", {"days": 30, "sort": "lengthMinutes", "order": "desc"},
     lambda get, r: L.coming_soon(get, 30, region=r, sort="lengthMinutes", order="desc")),
    ("categories", "/categories", {},
     lambda get, r: L.categories(get, region=r)),
    ("categories flat depth", "/categories", {"flat": "true", "depth": 2},
     lambda get, r: L.categories(get, region=r, flat=True, depth=2)),
]
CASE_IDS = [case[0] for case in CASES]


def _run(call, get, region):
    return asyncio.run(call(get, region))


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("name,path,params,call", CASES, ids=CASE_IDS)
def test_lookup_body_matches_the_hosted_route(hosted, name, path, params, call, region):
    resp = hosted(fake_get, path, {**params, "region": region})
    result = _run(call, fake_get, region)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    lib_json = _dump(result)
    if name.startswith("bulk"):
        # The bulk route answers the bucketed object; the lookup's own
        # BulkBookResponse is compared whole, not just its books.
        assert _canon(lib_json["books"]) == _canon(body["books"])
        for bucket in ("notFound", "placeholderRecords", "notFetched"):
            assert getattr(result, bucket) == body[bucket], bucket
        assert result.notFound == ["B0MISSING1"]
    else:
        assert _canon(lib_json) == _canon(body)
        assert body, "a parity match on an empty body proves nothing"


@pytest.mark.parametrize("name", ["author books asin shaped", "author books name shaped",
                                  "series books shaped", "new releases shaped"])
def test_shaping_actually_changed_the_answer(hosted, name):
    """Guards the parity cases above against passing because the filter and
    sort did nothing on both sides."""
    plain = {"author books asin shaped": "author books asin",
             "author books name shaped": "author books name",
             "series books shaped": "series books",
             "new releases shaped": "new releases"}[name]
    by_id = {case[0]: case for case in CASES}
    shaped_result = _dump(_run(by_id[name][3], fake_get, "us"))
    plain_result = _dump(_run(by_id[plain][3], fake_get, "us"))
    assert len(shaped_result) < len(plain_result)
    lengths = [b["lengthMinutes"] for b in shaped_result]
    assert lengths == sorted(lengths, reverse=True)
    assert all(length >= 150 for length in lengths)


# Section: not found and outage


def _hosted_outcome(resp):
    return {200: "ok", 404: "not-found", 503: "outage"}[resp.status_code]


def _lib_outcome(call, get):
    try:
        _run(call, get, "us")
    except NotFoundException:
        return "not-found"
    except AudibleAPIException:
        return "outage"
    return "ok"


# Where the lookup deliberately differs from its hosted route: Audible saying
# 404 on the release and category scans is a 503 on the route and stays
# NotFoundException in the library, because the library has no stored copy to
# answer from and a 404 is Audible's own answer, not an outage.
DELIBERATE_404 = {"new releases", "new releases category", "coming soon",
                  "categories", "categories flat depth"}

# Each of these answers the same status as its hosted route when Audible says
# there is nothing, when it answers an empty result and when it is down.
STATUS_CASES = [c for c in CASES if "shaped" not in c[0] and c[0] != "bulk"]


@pytest.mark.parametrize("get", [not_found_get, outage_get, empty_get],
                         ids=["audible-404", "audible-down", "audible-empty"])
@pytest.mark.parametrize("name,path,params,call", STATUS_CASES,
                         ids=[c[0] for c in STATUS_CASES])
def test_lookup_status_matches_the_hosted_route(hosted, name, path, params, call, get):
    resp = hosted(get, path, {**params, "region": "us"})
    if get is not_found_get and name in DELIBERATE_404:
        assert _hosted_outcome(resp) == "outage", name
        assert _lib_outcome(call, get) == "not-found", name
        return
    assert _lib_outcome(call, get) == _hosted_outcome(resp), (name, resp.status_code)


def _only_the_search_answers(fails_on):
    """A get that answers the search that finds candidates and fails every
    request made for a candidate itself."""
    async def get(region, path, params=None, extra_headers=None):
        if fails_on(path):
            raise AudibleAPIException("boom", upstream_status=503)
        return await fake_get(region, path, params, extra_headers)
    return get


@pytest.mark.parametrize("name,path,params,call,fails_on", [
    ("series search", "/series/search", {"name": "q"},
     lambda get, r: L.search_series(get, "q", region=r),
     lambda p: "/series/" in p or p.rsplit("/", 1)[-1].startswith("B0SERIES")),
    ("author search", "/author", {"name": "jane"},
     lambda get, r: L.search_authors(get, "jane", region=r),
     lambda p: "contributors/" in p),
], ids=["series search", "author search"])
def test_a_search_whose_every_candidate_failed_is_an_outage_in_the_library(
        hosted, name, path, params, call, fails_on):
    """Pinned deviation: with candidates found and none fetchable, the library
    raises AudibleAPIException where the route answers 404, which passes an
    outage off as a confirmed absence."""
    get = _only_the_search_answers(fails_on)
    resp = hosted(get, path, {**params, "region": "us"})
    assert resp.status_code == 404
    with pytest.raises(AudibleAPIException):
        _run(call, get, "us")


# Section: author books completeness against the hosted headers


@pytest.mark.parametrize("region", REGIONS)
def test_by_name_reasons_match_the_hosted_header(hosted, region, monkeypatch):
    """A book Audible answers with nothing makes the list incomplete with the
    same reason, in the same words, as X-Libex-Incomplete-Reason."""
    gone = "B0SCR00002"
    monkeypatch.setattr(
        support, "CATALOGUE", {k: v for k, v in CATALOGUE.items() if k != gone}
    )
    resp = hosted(fake_get, "/author/books", {"name": AUTHOR_NAME, "region": region})
    result = asyncio.run(L.get_author_books_by_name(fake_get, AUTHOR_NAME, region=region))

    assert result.complete is False
    assert result.incomplete_reasons == ("hydration-not-found",)
    assert resp.headers["x-libex-complete"] == "false"
    assert resp.headers["x-libex-incomplete-reason"] == ", ".join(result.incomplete_reasons)
    assert len(resp.json()) == len(result.books) == 3


@pytest.mark.parametrize("region", REGIONS)
def test_a_whole_by_name_list_is_complete_on_both_sides(hosted, region):
    resp = hosted(fake_get, "/author/books", {"region": region, "name": AUTHOR_NAME})
    result = asyncio.run(L.get_author_books_by_name(fake_get, AUTHOR_NAME, region=region))
    assert result.complete is True
    assert result.incomplete_reasons == ()
    assert resp.headers["x-libex-complete"] == "true"
    assert "x-libex-incomplete-reason" not in resp.headers


@pytest.mark.parametrize("region", REGIONS)
def test_an_unfinished_screens_walk_is_incomplete_on_both_sides(hosted, region):
    """The fake's author-detail screen never confirms an end, so discovery is
    unfinished: hosted says so in its header and the lookup in its result."""
    resp = hosted(fake_get, f"/author/books/{AUTHOR}", {"region": region})
    result = asyncio.run(L.get_author_books(fake_get, AUTHOR, region=region))
    assert result.complete is False
    assert result.incomplete_reasons == ("discovery-incomplete",)
    assert resp.headers["x-libex-complete"] == "false"
    assert len(resp.json()) == len(result.books) == len(BOOKS)
