"""
Each lookup command against the hosted route it stands for. Both are given the
same stand-in Audible answer, and the JSON the command prints must be the JSON
the route returns -- the published shape, field for field -- and a failure must
be the same failure on both: a 404 is status 3 and a 503 is status 4.
"""

# Standard library
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.db.session import get_session
from app.main import app
from libex_core.exceptions import AudibleAPIException, NotFoundException
from tests.libex_core._cli_lookup_support import (
    CASE_IDS,
    CASES,
    EGRESS,
    install_session,
    library_json,
)
from tests.libex_core.test_lookup import _asins, _batch_get

_HOSTED_MODULES = (
    "app.services.audible.books",
    "app.services.audible.search",
    "app.services.audible.series",
)
# Everything the hosted path would otherwise reach a stored copy or the cache
# through. Each answers "nothing", so what comes back is what Audible said.
_NOTHING = (
    ("app.services.audible.books.get_books_from_db", AsyncMock(return_value=[])),
    ("app.services.audible.books.get_track_from_db", AsyncMock(return_value=None)),
    ("app.services.audible.books.cache.get", AsyncMock(return_value=None)),
    ("app.services.audible.books.cache.get_many", AsyncMock(return_value={})),
    ("app.services.audible.series.get_series_from_db", AsyncMock(return_value=None)),
    ("app.services.audible.series.cache.get", AsyncMock(return_value=None)),
    ("app.services.audible.search.search_books_from_db", AsyncMock(return_value=[])),
)
_PERSISTERS = (
    "app.services.audible.books.persist_books_background",
    "app.services.audible.books.persist_track_background",
    "app.services.audible.search.persist_books_background",
    "app.services.audible.series.persist_series_background",
    "app.services.audible.series.persist_cache_background",
)


@pytest.fixture
def hosted(client):
    """Asks a hosted route with Audible answered by `get`. Returns the
    response. The `cache=false` the routes take keeps every read off the
    cache, and the rest of what the route could consult is patched to say
    nothing, so the body is only what `get` made of it."""

    app.dependency_overrides[get_session] = lambda: AsyncMock()

    def ask(get, path, params):
        with ExitStack() as stack:
            for module in _HOSTED_MODULES:
                stack.enter_context(patch(f"{module}.audible_get", new=get))
            for name in _PERSISTERS:
                stack.enter_context(patch(name))
            for name, stand_in in _NOTHING:
                stack.enter_context(patch(name, new=stand_in))
            return client.get(path, params={**params, "cache": "false"})

    try:
        yield ask
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def run(run_cli, monkeypatch):
    def go(argv, get):
        install_session(monkeypatch, get)
        return run_cli(list(argv), env=EGRESS)

    return go


# ============================================================
# THE SAME JSON AS THE HOSTED ROUTE
# ============================================================

@pytest.mark.parametrize("region", ["us", "de", "jp"])
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_the_command_prints_what_the_hosted_route_returns(run, hosted, case, region):
    cli = run([*case.argv, "--region", region], case.make_get())
    assert cli.code == 0, cli.err

    path, params = case.route(region)
    response = hosted(case.make_get(), path, params)
    assert response.status_code == 200, response.text

    printed = json.loads(cli.out)
    assert printed == response.json()
    assert printed == library_json(case, region)
    assert printed, "two empty answers would agree"


def test_the_route_this_compares_against_would_notice_a_different_answer(run, hosted):
    """The comparison is not vacuous: the same question with a different
    stand-in answer gives a different body."""
    case = next(c for c in CASES if c.name == "book get")
    path, params = case.route("us")
    first = hosted(case.make_get(), path, params).json()
    other = hosted(_batch_get(known=set()), path, params)
    assert other.status_code == 404
    assert first != other.json()


def test_a_bulk_answer_with_every_kind_of_miss_is_the_same_on_both(run, hosted):
    # 51 ASINs is two requests: the first answers for 50 of them, the second
    # is the one that fails.
    asins = _asins(51)

    def make():
        return _batch_get(
            known=set(asins[:48]), placeholders={asins[49]}, fail={asins[50]}
        )

    cli = run(["book", "bulk", *asins], make())
    response = hosted(make(), "/book", {"asins": ",".join(asins)})
    assert cli.code == 0 and response.status_code == 200
    body = json.loads(cli.out)
    assert body == response.json()
    assert body["placeholderRecords"] == [asins[49]]
    assert body["notFetched"] == [asins[50]]
    assert body["notFound"] == [asins[48]]
    assert len(body["books"]) == 48


def test_a_lower_case_bulk_asin_is_reported_as_the_caller_sent_it_on_both(run, hosted):
    def make():
        return _batch_get(known={"B0LOOK0001"})

    cli = run(["book", "bulk", "b0look0001", "b0look0002"], make())
    response = hosted(make(), "/book", {"asins": "b0look0001,b0look0002"})
    assert json.loads(cli.out) == response.json()
    assert json.loads(cli.out)["notFound"] == ["b0look0002"]


# ============================================================
# THE SAME FAILURE AS THE HOSTED ROUTE
# ============================================================

# The commands whose route answers an absence with a 404 and an outage with
# a 503. A bulk request reports its misses in the body instead.
_FAILING = [c for c in CASES if c.name != "book bulk"]


@pytest.mark.parametrize("case", _FAILING, ids=lambda c: c.name)
def test_not_found_is_a_404_on_the_route_and_status_three_on_the_command(run, hosted, case):
    def make():
        return AsyncMock(side_effect=NotFoundException("gone"))

    path, params = case.route("us")
    assert hosted(make(), path, params).status_code == 404
    assert run(case.argv, make()).code == 3


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_an_outage_is_a_503_on_the_route_and_status_four_on_the_command(run, hosted, case):
    def make():
        return AsyncMock(side_effect=AudibleAPIException("boom", upstream_status=503))

    path, params = case.route("us")
    assert hosted(make(), path, params).status_code == 503
    assert run(case.argv, make()).code == 4
