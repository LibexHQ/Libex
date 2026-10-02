"""
libex_core.audible.authors: what each public fetch promises before anything
is sent (region validated, an ASIN checked where one reaches a path, no message
repeating what a caller typed), and that an author name never reaches a log
record or an exception.

Each fetch is handed `get` rather than owning a client, so every test here
passes a stand-in and asserts on whether it was called. Nothing touches a
network.
"""

# Standard library
import ast
import inspect
import logging
from pathlib import Path
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.audible.authors import by_name as by_name_module
from libex_core.audible.authors import catalog as catalog_module
from libex_core.audible.authors import profile as profile_module
from libex_core.audible.authors import screens as screens_module
from libex_core.audible.authors.by_name import NameWalkOutcome, walk_author_books_by_name
from libex_core.audible.authors.catalog import fetch_author_books_by_catalog
from libex_core.audible.authors.profile import fetch_author_profile, fetch_author_suggestion_asins
from libex_core.audible.authors.screens import (
    SCREENS_REASON_COMPLETED,
    fetch_author_books_by_screen,
)
from libex_core.exceptions import AudibleAPIException, RegionException

BAD_REGIONS = ["xx", "", "usa", "mars"]
GOOD_ASIN = "B000APZR4U"
BAD_ASINS = ["", "short", "B000APZR4U1", "B000-PZR4U", "../etc/passwd", "B000APZR4U/../x"]
PLANTED_NAME = "Zq9 Distinctive Authorname"
PLANTED_ASIN_LIKE = "../Zq9planted/asin"

_MODULES = [by_name_module, catalog_module, profile_module, screens_module]

# Every public fetch, as a call that supplies a valid ASIN and the planted
# name, so a rejected region is the only thing wrong with it.
_FETCH_CALLS = {
    "fetch_author_profile": lambda get, region: fetch_author_profile(get, GOOD_ASIN, region),
    "fetch_author_suggestion_asins": lambda get, region: fetch_author_suggestion_asins(
        get, PLANTED_NAME, region
    ),
    "walk_author_books_by_name": lambda get, region: walk_author_books_by_name(
        get, PLANTED_NAME, region
    ),
    "fetch_author_books_by_screen": lambda get, region: fetch_author_books_by_screen(
        get, GOOD_ASIN, region
    ),
    "fetch_author_books_by_catalog": lambda get, region: fetch_author_books_by_catalog(
        get, GOOD_ASIN, PLANTED_NAME, region
    ),
}

_FETCH_FUNCTIONS = [
    fetch_author_profile,
    fetch_author_suggestion_asins,
    walk_author_books_by_name,
    fetch_author_books_by_screen,
    fetch_author_books_by_catalog,
]


def _everything_logged(caplog) -> str:
    """Every record's message, args, extras and traceback text, as one string."""
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.__dict__))
        if record.exc_info:
            parts.append(repr(record.exc_info[1]))
    return "\n".join(parts)


# ============================================================
# `get` is the required first parameter
# ============================================================

@pytest.mark.parametrize("fn", _FETCH_FUNCTIONS, ids=lambda f: f.__name__)
def test_every_author_fetch_takes_get_as_a_required_first_argument(fn):
    parameters = inspect.signature(fn).parameters
    assert list(parameters)[0] == "get"
    assert parameters["get"].default is inspect.Parameter.empty


async def test_a_fetch_called_without_get_is_a_type_error():
    with pytest.raises(TypeError):
        await fetch_author_profile(GOOD_ASIN, "us")


# ============================================================
# Region is rejected before any request
# ============================================================

@pytest.mark.parametrize("region", BAD_REGIONS)
@pytest.mark.parametrize("name", list(_FETCH_CALLS))
async def test_a_bad_region_is_rejected_before_any_request(name, region):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await _FETCH_CALLS[name](get, region)
    get.assert_not_awaited()


@pytest.mark.parametrize("name", list(_FETCH_CALLS))
async def test_a_region_error_never_echoes_the_name(name):
    get = AsyncMock()
    with pytest.raises(RegionException) as exc:
        await _FETCH_CALLS[name](get, "xx")
    assert PLANTED_NAME not in str(exc.value)
    assert "Distinctive" not in str(exc.value)


# ============================================================
# ASIN validation
# ============================================================

@pytest.mark.parametrize("asin", BAD_ASINS)
async def test_fetch_author_profile_rejects_a_non_asin_before_any_request(asin):
    get = AsyncMock()
    with pytest.raises(ValueError) as exc:
        await fetch_author_profile(get, asin, "us")
    get.assert_not_awaited()
    assert str(exc.value) == "not a valid ASIN"


async def test_fetch_author_profile_message_never_echoes_the_input():
    get = AsyncMock()
    with pytest.raises(ValueError) as exc:
        await fetch_author_profile(get, PLANTED_ASIN_LIKE, "us")
    assert "Zq9planted" not in str(exc.value)
    get.assert_not_awaited()


async def test_fetch_author_profile_sends_the_uppercased_asin_in_the_path():
    get = AsyncMock(return_value={"contributor": {}})
    await fetch_author_profile(get, GOOD_ASIN.lower(), "de")
    region, path, params = get.await_args.args
    assert region == "de"
    assert path == f"/1.0/catalog/contributors/{GOOD_ASIN}"
    assert params == {"locale": "de-DE"}


@pytest.mark.parametrize("asin", BAD_ASINS)
async def test_fetch_author_books_by_screen_answers_a_non_asin_empty_and_clean_without_a_request(asin):
    get = AsyncMock()
    result = await fetch_author_books_by_screen(get, asin, "us")
    get.assert_not_awaited()
    assert result.asins == []
    assert result.pages_fetched == 0
    assert result.termination_reason == SCREENS_REASON_COMPLETED


async def test_fetch_author_books_by_catalog_never_sends_the_author_asin():
    """author_asin only attributes what comes back; whatever it holds stays
    out of every request, so it needs no validation of its own."""
    get = AsyncMock(return_value={"total_results": 0, "products": []})
    await fetch_author_books_by_catalog(get, PLANTED_ASIN_LIKE, PLANTED_NAME, "us")
    assert get.await_count > 0
    for call in get.await_args_list:
        assert "Zq9planted" not in repr(call)


# ============================================================
# The author name never reaches a log record or an exception
# ============================================================

def _product(asin):
    return {"asin": asin, "authors": [{"name": PLANTED_NAME}], "language": "english"}


def _page(prefix, count=50):
    return {"products": [_product(f"{prefix}{i:05d}") for i in range(count)]}


async def test_by_name_first_page_failure_logs_and_raises_without_the_name(caplog):
    get = AsyncMock(side_effect=RuntimeError("upstream exploded"))
    with caplog.at_level(logging.DEBUG, logger="libex"):
        with pytest.raises(AudibleAPIException) as exc:
            await walk_author_books_by_name(get, PLANTED_NAME, "us")
    assert caplog.records, "the failure path logged nothing, so the check below proves nothing"
    assert "Distinctive" not in _everything_logged(caplog)
    assert "Distinctive" not in str(exc.value)
    assert PLANTED_NAME not in repr(exc.value.__cause__)


async def test_by_name_later_page_failure_logs_without_the_name(caplog):
    get = AsyncMock(side_effect=[_page("B0FULL"), RuntimeError("upstream 500")])
    with caplog.at_level(logging.DEBUG, logger="libex"):
        asins, pages, completed = await walk_author_books_by_name(get, PLANTED_NAME, "us")
    assert completed is False
    assert len(asins) == 50
    assert caplog.records
    assert "Distinctive" not in _everything_logged(caplog)


@pytest.mark.parametrize("kind", ["plateau", "deadline", "completed"])
async def test_by_name_every_other_stop_logs_without_the_name(kind, caplog):
    kwargs = {}
    if kind == "plateau":
        get = AsyncMock(return_value=_page("B0FULL"))
    elif kind == "deadline":
        get = AsyncMock(return_value=_page("B0FULL"))
        kwargs["deadline"] = 0.0
    else:
        get = AsyncMock(return_value=_page("B0ONE", 1))
    outcome = NameWalkOutcome()
    with caplog.at_level(logging.DEBUG, logger="libex"):
        await walk_author_books_by_name(get, PLANTED_NAME, "us", outcome=outcome, **kwargs)
    assert outcome.stop == kind
    assert "Distinctive" not in _everything_logged(caplog)


async def test_by_name_does_send_the_name_to_audible():
    """The counterpart of the checks above: the name is the query, so it must
    reach `get` -- what is withheld is the log and the exception, not the request."""
    get = AsyncMock(return_value=_page("B0ONE", 1))
    await walk_author_books_by_name(get, PLANTED_NAME, "us")
    assert get.await_args.args[2]["author"] == PLANTED_NAME


async def test_suggestion_failure_propagates_without_logging_the_name(caplog):
    exc = AudibleAPIException("down")
    get = AsyncMock(side_effect=exc)
    with caplog.at_level(logging.DEBUG, logger="libex"):
        with pytest.raises(AudibleAPIException) as raised:
            await fetch_author_suggestion_asins(get, PLANTED_NAME, "us")
    assert raised.value is exc
    assert "Distinctive" not in _everything_logged(caplog)


async def test_suggestion_success_logs_nothing_with_the_name(caplog):
    get = AsyncMock(return_value={})
    with caplog.at_level(logging.DEBUG, logger="libex"):
        assert await fetch_author_suggestion_asins(get, PLANTED_NAME, "us") == []
    assert get.await_args.args[2]["keywords"] == PLANTED_NAME
    assert "Distinctive" not in _everything_logged(caplog)


async def test_suggestion_returns_author_rows_only_and_in_order():
    def row(asin, template="AuthorItemV2"):
        return {"view": {"template": template}, "model": {"person_metadata": {"asin": asin}}}

    get = AsyncMock(return_value={"model": {"items": [
        row("B0AAAAAAA1"), row("B0BBBBBBB2", "AsinRow"), row("B0CCCCCCC3"), row(""),
    ]}})
    assert await fetch_author_suggestion_asins(get, "x", "us") == ["B0AAAAAAA1", "B0CCCCCCC3"]


async def test_catalog_failure_reports_without_the_name(caplog):
    get = AsyncMock(side_effect=RuntimeError("upstream exploded"))
    with caplog.at_level(logging.DEBUG, logger="libex"):
        result = await fetch_author_books_by_catalog(get, GOOD_ASIN, PLANTED_NAME, "us")
    assert result.sort_errors, "no failure was recorded, so the check below proves nothing"
    assert "Distinctive" not in _everything_logged(caplog)
    assert "Distinctive" not in repr(result)


async def test_screens_failure_logs_without_the_name(caplog):
    """The screens walk is handed no name; this pins that it stays so by
    checking nothing it logs or returns could carry one."""
    assert "name" not in inspect.signature(fetch_author_books_by_screen).parameters
    get = AsyncMock(side_effect=RuntimeError("upstream exploded"))
    with caplog.at_level(logging.DEBUG, logger="libex"):
        result = await fetch_author_books_by_screen(get, GOOD_ASIN, "us")
    assert caplog.records
    assert "Distinctive" not in _everything_logged(caplog) + repr(result)


# ============================================================
# The modules' own rules: no environment read
# ============================================================

@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__)
def test_author_modules_read_no_environment(module):
    tree = ast.parse(Path(module.__file__).read_text())
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imported = {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    } | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not ({"environ", "getenv"} & (attrs | names))
    assert "os" not in names
    assert "os" not in imported
