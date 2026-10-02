"""
The libex-core author and releases commands, the series search command and the
filter and sort flags that every command listing books takes: that each flag
reaches the library as the keyword it stands for, that a value the tool
refuses is refused before anything is asked and without being repeated, the
warning an author's books print when the list may not be whole, and how each
command's exit status compares with the hosted route it stands for.
"""

# Standard library
import json
import re
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core import lookup
from libex_core.cli.commands import releases as releases_command
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import INCOMPLETE_REASONS, RELEASE_WINDOWS, BookList
from libex_core.models import BulkBookResponse
from libex_core.shaping import BOOK_FILTER_SPECS, BOOK_SORT_FIELDS
from tests.libex_core import _lookup_support as support
from tests.libex_core._cli_lookup_support import (
    BY_NAME_NAMES,
    CASES,
    EGRESS,
    ROUTE_503_ON_AUDIBLE_404,
    install_session,
)
from tests.libex_core._lookup_support import (
    AUTHOR,
    AUTHOR_NAME,
    BOOKS,
    PLANTED,
    SERIES,
    fake_get,
)


@pytest.fixture
def run(run_cli, monkeypatch):
    def go(argv, get=None, **env):
        install_session(monkeypatch, get if get is not None else AsyncMock(side_effect=fake_get))
        return run_cli(list(argv), env={**EGRESS, **env})

    return go


@pytest.fixture
def spy(monkeypatch):
    """Replaces a lookup function where the commands import it from with one
    that records the arguments it was called with and answers with an empty
    result of the right kind, so a test about what the command passed is not
    also about what the stand-in Audible made of it."""
    seen = {}
    empty = {
        "get_books": BulkBookResponse(
            books=[], notFound=[], placeholderRecords=[], notFetched=[]
        ),
        "get_series_books": BookList(),
        "get_author_books": BookList(),
        "get_author_books_by_name": BookList(),
    }

    def wrap(name):
        async def recorded(*args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs
            return empty.get(name, [])

        monkeypatch.setattr(lookup, name, recorded)
        return seen

    return wrap


# The commands that list books and take the filter and sort flags, with the
# library function each one calls and the order its hosted route applies when
# none is given.
SHAPED = [
    pytest.param(("series", "books", SERIES), "get_series_books", "asc", id="series books"),
    pytest.param(("book", "bulk", *BOOKS), "get_books", "asc", id="book bulk"),
    pytest.param(("author", "books", AUTHOR), "get_author_books", "asc", id="author books"),
    pytest.param(
        ("author", "books-by-name", AUTHOR_NAME), "get_author_books_by_name", "asc",
        id="author books-by-name",
    ),
    pytest.param(("releases", "new"), "new_releases", "desc", id="releases new"),
    pytest.param(("releases", "coming-soon"), "coming_soon", "asc", id="releases coming-soon"),
]

# One accepted value for each filter, typed as the flag takes it and as the
# library gets it.
_VALUES = {
    str: ("an-example", "an-example"),
    bool: ("true", True),
    float: ("4.5", 4.5),
    int: ("120", 120),
}


def _flag(spec):
    return "--" + spec.name.replace("_", "-")


# ============================================================
# EACH FLAG REACHES THE LIBRARY AS THE KEYWORD IT STANDS FOR
# ============================================================

@pytest.mark.parametrize("base, fn, default_order", SHAPED)
def test_with_no_flag_nothing_is_filtered_or_sorted_and_the_routes_order_is_kept(
    run, spy, base, fn, default_order
):
    seen = spy(fn)
    result = run(base)
    assert result.code == 0, result.err
    assert seen["kwargs"]["filters"] is None
    assert seen["kwargs"]["sort"] is None
    assert seen["kwargs"]["order"] == default_order


@pytest.mark.parametrize("base, fn, default_order", SHAPED)
@pytest.mark.parametrize("spec", BOOK_FILTER_SPECS, ids=lambda s: s.name)
def test_each_filter_flag_is_the_filter_of_the_same_name(run, spy, base, fn, default_order, spec):
    seen = spy(fn)
    text, value = _VALUES[spec.type]
    result = run([*base, _flag(spec), text])
    assert result.code == 0, result.err
    assert seen["kwargs"]["filters"] == {spec.name: value}
    assert type(seen["kwargs"]["filters"][spec.name]) is type(value)


@pytest.mark.parametrize("base, fn, default_order", SHAPED)
def test_every_filter_at_once_arrives_together(run, spy, base, fn, default_order):
    seen = spy(fn)
    argv = [*base]
    expected = {}
    for spec in BOOK_FILTER_SPECS:
        text, value = _VALUES[spec.type]
        argv += [_flag(spec), text]
        expected[spec.name] = value
    assert run(argv).code == 0
    assert seen["kwargs"]["filters"] == expected


@pytest.mark.parametrize("text, value", [("true", True), ("false", False)])
def test_a_false_filter_is_a_filter_not_an_absent_one(run, spy, text, value):
    seen = spy("get_author_books")
    run(["author", "books", AUTHOR, "--explicit", text])
    assert seen["kwargs"]["filters"] == {"explicit": value}


@pytest.mark.parametrize("base, fn, default_order", SHAPED)
@pytest.mark.parametrize("sort", BOOK_SORT_FIELDS)
def test_each_sort_field_and_both_orders_are_accepted(run, spy, base, fn, default_order, sort):
    seen = spy(fn)
    for order in ("asc", "desc"):
        assert run([*base, "--sort", sort, "--order", order]).code == 0
        assert (seen["kwargs"]["sort"], seen["kwargs"]["order"]) == (sort, order)


@pytest.mark.parametrize("text, value", [("0", 0), ("1000000", 1_000_000)])
def test_the_ends_of_the_length_range_are_accepted(run, spy, text, value):
    seen = spy("get_author_books")
    assert run(["author", "books", AUTHOR, "--longer-than", text]).code == 0
    assert seen["kwargs"]["filters"] == {"longer_than": value}


def test_the_flags_come_from_the_librarys_own_filter_specs():
    from libex_core.cli.parser import build_parser
    from tests.libex_core._cli_support import walk_parsers

    expected = {_flag(spec) for spec in BOOK_FILTER_SPECS} | {"--sort", "--order"}
    shaped = {
        " ".join(path)
        for path, node in walk_parsers(build_parser())
        if path[:1] != ("db",) and expected & {f for a in node._actions for f in a.option_strings}
    }
    assert shaped == {
        "series books", "book bulk", "author books", "author books-by-name",
        "releases new", "releases coming-soon",
    }
    for path, node in walk_parsers(build_parser()):
        flags = {f for a in node._actions for f in a.option_strings}
        if " ".join(path) in shaped:
            assert expected <= flags, path


def test_the_db_commands_take_the_filters_too_with_no_default_region():
    """The db commands read the store, which holds every region, so their
    book --region filter is optional and carries no default; the shared filter
    walk above is for the commands that ask Audible, where it defaults to us."""
    from libex_core.cli.parser import build_parser
    from tests.libex_core._cli_support import walk_parsers

    expected = {_flag(spec) for spec in BOOK_FILTER_SPECS} | {"--sort", "--order"}
    shaped = {
        path[1]
        for path, node in walk_parsers(build_parser())
        if path[:1] == ("db",) and expected & {f for a in node._actions for f in a.option_strings}
    }
    # narrators has none of the book filters but takes --language, --sort and
    # --order of its own, which is why the walk finds it.
    assert shaped == {
        "books", "author-books", "series-books", "narrator-books", "plan", "vvab",
        "new-releases", "coming-soon", "narrators",
    }
    shaped.discard("narrators")
    for path, node in walk_parsers(build_parser()):
        if path[:1] == ("db",) and len(path) == 2 and path[1] in shaped and path[1] != "author-books":
            region = [a for a in node._actions if "--region" in a.option_strings]
            if region:
                assert region[0].default is None, path


# ============================================================
# A REFUSED VALUE IS REFUSED BEFORE ANYTHING IS ASKED
# ============================================================

# Flags whose value is checked against a list of choices.
_CHOICE_REFUSALS = [
    ("--explicit", PLANTED),
    ("--whisper-sync", "yes"),
    ("--has-pdf", "1"),
    ("--is-vvab", "True"),
    ("--sort", PLANTED),
    ("--order", PLANTED),
    ("--order", "ASC"),
]
# Flags whose value is checked by the tool's own type, which has a fixed message.
_TYPED_REFUSALS = [
    ("--rating-better-than", "nan"),
    ("--rating-better-than", "inf"),
    ("--rating-worse-than", PLANTED),
    ("--rating-worse-than", ""),
    ("--longer-than", "-1"),
    ("--longer-than", "1000001"),
    ("--longer-than", "1.5"),
    ("--shorter-than", PLANTED),
    ("--shorter-than", ""),
]


@pytest.mark.parametrize("base, fn, default_order", SHAPED)
@pytest.mark.parametrize("flag, value", [*_CHOICE_REFUSALS, *_TYPED_REFUSALS])
def test_a_bad_filter_or_sort_value_is_a_usage_error_before_any_request(
    run, base, fn, default_order, flag, value
):
    get = AsyncMock()
    result = run([*base, flag, value], get)
    assert result.code == 2
    assert result.stdout == b""
    get.assert_not_called()


@pytest.mark.parametrize("base, fn, default_order", SHAPED)
@pytest.mark.parametrize("flag, value", _TYPED_REFUSALS)
def test_a_value_the_tool_refuses_is_not_repeated(run, base, fn, default_order, flag, value):
    result = run([*base, flag, value], AsyncMock())
    assert PLANTED not in result.err
    if value:
        assert value not in result.err.replace("1000000", "").replace("0 to", "")


SECRET_URL = "https://user:hunter2@example.invalid/path?token=abc123"

_CHOICE_ARGVS = [
    pytest.param(["series", "books", SERIES, "--explicit"], id="boolean filter"),
    pytest.param(["author", "books", AUTHOR, "--sort"], id="sort"),
    pytest.param(["author", "books", AUTHOR, "--order"], id="order"),
    pytest.param(["releases", "new", "--days"], id="days"),
    pytest.param(["book", "get", "B0LOOK0001", "--region"], id="region"),
    pytest.param(["releases", "categories", "--region"], id="region on categories"),
]


@pytest.mark.parametrize("value", [PLANTED, SECRET_URL])
@pytest.mark.parametrize("argv", _CHOICE_ARGVS)
def test_a_choice_flag_refusal_does_not_repeat_the_value(run, argv, value):
    get = AsyncMock()
    result = run([*argv, value], get)
    assert result.code == 2 and result.stdout == b""
    assert "invalid choice (choose from" in result.err
    for part in (PLANTED, "hunter2", "abc123", "example.invalid"):
        assert part not in result.err
    get.assert_not_called()


@pytest.mark.parametrize("argv, listed", [
    (["releases", "new", "--days"], "30, 60, 90, 120, 240, 365"),
    (["author", "books", AUTHOR, "--order"], "asc, desc"),
    (["book", "get", "B0LOOK0001", "--region"], "us, uk, ca, au, de, fr, it, es, jp, in, br"),
])
def test_the_refusal_still_lists_what_is_allowed(run, argv, listed):
    assert listed in run([*argv, "nope"], AsyncMock()).err


@pytest.mark.parametrize("argv", [
    [PLANTED],
    ["book", PLANTED],
    ["author", PLANTED],
    ["releases", PLANTED],
    ["completion", PLANTED],
    ["completion", "--shell", PLANTED],
], ids=lambda a: " ".join(a[:-1]) or "top")
def test_a_command_or_shell_refusal_does_not_repeat_what_was_typed(run, argv):
    result = run(argv, AsyncMock())
    assert result.code == 2 and result.stdout == b""
    assert PLANTED not in result.err


@pytest.mark.parametrize("argv", [
    ["book", "get", "B0LOOK0001", PLANTED],
    ["book", "get", "B0LOOK0001", "--" + PLANTED],
    ["book", "get", "B0LOOK0001", "--region=us", SECRET_URL],
    ["releases", "new", PLANTED, SECRET_URL],
    ["author", "books", AUTHOR, f"--{PLANTED}=x"],
])
def test_leftover_arguments_are_refused_without_being_listed(run, argv):
    get = AsyncMock()
    result = run(argv, get)
    assert result.code == 2 and result.stdout == b""
    assert "unrecognized arguments" in result.err
    for part in (PLANTED, "hunter2", "abc123"):
        assert part not in result.err
    get.assert_not_called()


@pytest.mark.parametrize("argv", [
    ["releases", "new", "--da", "30"],
    ["releases", "new", "--d", "30"],
    ["releases", "new", "--cat", "1"],
    ["author", "books", AUTHOR, "--lon", "10"],
    ["author", "books", AUTHOR, "--so", "title"],
    ["book", "get", "B0LOOK0001", "--reg", "de"],
    ["book", "get", "B0LOOK0001", "--reg=de"],
    ["author", "books", AUTHOR, f"--s={PLANTED}"],
])
def test_a_flag_abbreviation_is_refused_and_the_exact_flag_is_not(run, argv):
    get = AsyncMock()
    result = run(argv, get)
    assert result.code == 2 and result.stdout == b""
    assert "unrecognized arguments" in result.err
    assert PLANTED not in result.err
    get.assert_not_called()


@pytest.mark.parametrize("argv", [
    ["releases", "new", "--days", "30"],
    ["releases", "new", "--days=30"],
    ["author", "books", AUTHOR, "--longer-than", "10"],
    ["book", "get", "B0LOOK0001", "--region", "de"],
])
def test_the_exact_flag_works_in_both_spellings(run, argv):
    assert run(argv).code in (0, 3)


# ============================================================
# RELEASES
# ============================================================

def test_the_command_line_windows_are_the_librarys():
    assert tuple(int(w) for w in releases_command.WINDOWS) == RELEASE_WINDOWS


@pytest.mark.parametrize("sub", ["new", "coming-soon"])
@pytest.mark.parametrize("days", RELEASE_WINDOWS)
def test_each_window_is_sent_as_a_number(run, spy, sub, days):
    seen = spy("new_releases" if sub == "new" else "coming_soon")
    assert run(["releases", sub, "--days", str(days), "--category", "7"]).code == 0
    assert seen["args"][1:3] == (days, "7")
    assert isinstance(seen["args"][1], int)


@pytest.mark.parametrize("sub, fn", [("new", "new_releases"), ("coming-soon", "coming_soon")])
def test_the_defaults_are_thirty_days_and_no_category(run, spy, sub, fn):
    seen = spy(fn)
    assert run(["releases", sub]).code == 0
    assert seen["args"][1:3] == (30, None)


@pytest.mark.parametrize("sub", ["new", "coming-soon"])
@pytest.mark.parametrize("days", ["0", "29", "31", "366", "-30", "", "abc", "30.0"])
def test_a_window_that_is_not_offered_is_a_usage_error_before_any_request(run, sub, days):
    get = AsyncMock()
    result = run(["releases", sub, "--days", days], get)
    assert result.code == 2 and result.stdout == b""
    get.assert_not_called()


@pytest.mark.parametrize("sub", ["new", "coming-soon"])
@pytest.mark.parametrize("category", ["", "abc", "12a", "-1", "1 2", "1234567890123", "123\n", PLANTED])
def test_a_category_that_is_not_a_numeric_id_is_refused_without_being_repeated(
    run, sub, category
):
    get = AsyncMock()
    result = run(["releases", sub, "--category", category], get)
    assert result.code == 2 and result.stdout == b""
    assert "numeric category id" in result.err
    assert PLANTED not in result.err
    get.assert_not_called()


def test_a_twelve_digit_category_is_sent_as_given(run, spy):
    seen = spy("new_releases")
    assert run(["releases", "new", "--category", "123456789012"]).code == 0
    assert seen["args"][2] == "123456789012"


@pytest.mark.parametrize("flags, kwargs", [
    ([], {"flat": False, "depth": None}),
    (["--flat"], {"flat": True, "depth": None}),
    (["--depth", "1"], {"flat": False, "depth": 1}),
    (["--flat", "--depth", "9"], {"flat": True, "depth": 9}),
])
def test_categories_flat_and_depth_reach_the_library(run, spy, flags, kwargs):
    seen = spy("categories")
    assert run(["releases", "categories", *flags]).code == 0
    assert seen["kwargs"] == {"region": "us", **kwargs}


@pytest.mark.parametrize("depth", ["0", "10", "-1", "1.5", "", PLANTED])
def test_a_depth_outside_one_to_nine_is_refused_without_being_repeated(run, depth):
    get = AsyncMock()
    result = run(["releases", "categories", "--depth", depth], get)
    assert result.code == 2 and result.stdout == b""
    assert PLANTED not in result.err
    assert "from 1 to 9" in result.err
    get.assert_not_called()


def test_categories_prints_the_tree_and_the_flat_list(run):
    tree = json.loads(run(["releases", "categories"]).out)
    flat = json.loads(run(["releases", "categories", "--flat"]).out)
    assert [n["name"] for n in tree] == ["Alpha", "Zed"]
    assert {n["id"] for n in flat} == {"1", "2", "3"}
    assert flat[0].keys() >= {"id", "name", "ancestors"}


# ============================================================
# AN AUTHOR'S BOOKS WARN WHEN THE LIST MAY NOT BE WHOLE
# ============================================================

_WARNING = re.compile(
    r"^libex-core: WARNING: the list of books may be incomplete "
    r"\((?P<reasons>[a-z]+(?:-[a-z]+)*(?:, [a-z]+(?:-[a-z]+)*)*)\)$"
)

_AUTHOR_BOOKS = [
    pytest.param(("series", "books", SERIES), "get_series_books", id="series books"),
    pytest.param(("author", "books", AUTHOR), "get_author_books", id="by asin"),
    pytest.param(
        ("author", "books-by-name", AUTHOR_NAME), "get_author_books_by_name", id="by name"
    ),
]


def _returns(monkeypatch, name, result):
    monkeypatch.setattr(lookup, name, AsyncMock(return_value=result))


@pytest.mark.parametrize("argv, fn", _AUTHOR_BOOKS)
@pytest.mark.parametrize("reasons", [
    ("discovery-incomplete",),
    ("hydration-failed", "hydration-not-found"),
    INCOMPLETE_REASONS,
], ids=["one", "two", "all four"])
def test_an_incomplete_list_is_printed_whole_with_status_zero_and_one_warning(
    run, monkeypatch, argv, fn, reasons
):
    _returns(monkeypatch, fn, BookList(books=[], complete=False, incomplete_reasons=reasons))
    result = run(argv)
    assert result.code == 0
    assert result.out == "[]\n"
    (line,) = result.err.splitlines()
    match = _WARNING.match(line)
    assert match, line
    assert match["reasons"] == ", ".join(reasons)


@pytest.mark.parametrize("argv, fn", _AUTHOR_BOOKS)
def test_a_complete_list_prints_no_warning(run, monkeypatch, argv, fn):
    _returns(monkeypatch, fn, BookList(books=[], complete=True))
    result = run(argv)
    assert (result.code, result.out, result.err) == (0, "[]\n", "")


@pytest.mark.parametrize("argv, fn", _AUTHOR_BOOKS)
def test_quiet_silences_the_warning_and_changes_nothing_else(run, monkeypatch, argv, fn):
    _returns(
        monkeypatch, fn,
        BookList(books=[], complete=False, incomplete_reasons=("hydration-failed",)),
    )
    loud = run(argv)
    quiet = run(["-q", *argv])
    assert quiet.err == ""
    assert (quiet.code, quiet.out) == (loud.code, loud.out) == (0, "[]\n")
    assert loud.err != ""


def test_the_warning_holds_only_the_fixed_words(run, monkeypatch):
    _returns(
        monkeypatch, "get_author_books_by_name",
        BookList(books=[], complete=False, incomplete_reasons=INCOMPLETE_REASONS),
    )
    result = run(["author", "books-by-name", PLANTED])
    assert PLANTED not in result.err and PLANTED not in result.out


def test_an_author_books_list_is_the_books_alone_not_the_object(run):
    printed = json.loads(run(["author", "books-by-name", AUTHOR_NAME]).out)
    assert isinstance(printed, list) and printed
    assert all("asin" in book for book in printed)


def test_a_real_run_that_cannot_confirm_the_end_warns_with_the_librarys_reason(run):
    result = run(["author", "books", AUTHOR])
    assert result.code == 0
    # The library logs its own account of the walk beside the notice.
    (notice,) = [line for line in result.err.splitlines() if "may be incomplete" in line]
    assert _WARNING.match(notice)
    assert "discovery-incomplete" in notice
    assert len(json.loads(result.out)) == len(BOOKS)


# ============================================================
# EXIT STATUS AGAINST THE HOSTED ROUTE
# ============================================================

def _case(name):
    return next(c for c in CASES if c.name == name)


def _nf():
    return AsyncMock(side_effect=NotFoundException("gone"))


def _down():
    return AsyncMock(side_effect=AudibleAPIException("boom", upstream_status=503))


@pytest.mark.parametrize("name", sorted(BY_NAME_NAMES))
def test_a_404_on_the_first_page_of_a_by_name_lookup_is_an_outage_on_both(run, hosted, name):
    case = _case(name)
    path, params = case.route("us")
    assert hosted(_nf(), path, params).status_code == 503
    result = run(case.argv, _nf())
    assert result.code == 4
    assert "upstream_unavailable" in result.err


@pytest.mark.parametrize("name", ["author books", "author books shaped"])
def test_a_404_on_every_source_of_an_authors_books_is_a_404_and_status_three(run, hosted, name):
    case = _case(name)
    path, params = case.route("us")
    assert hosted(_nf(), path, params).status_code == 404
    assert run(case.argv, _nf()).code == 3


@pytest.mark.parametrize("name", sorted(ROUTE_503_ON_AUDIBLE_404))
def test_a_404_on_the_release_scans_is_status_three_where_the_route_says_503(run, hosted, name):
    """Pinned deviation: the command has no stored copy to answer from, and a
    404 is Audible's own answer, so it is a not-found and not an outage."""
    case = _case(name)
    path, params = case.route("us")
    assert hosted(_nf(), path, params).status_code == 503
    result = run(case.argv, _nf())
    assert result.code == 3
    assert result.stdout == b""
    assert "(code: not_on_audible)" in result.err


@pytest.mark.parametrize("name", sorted(ROUTE_503_ON_AUDIBLE_404))
def test_a_release_scan_with_audible_down_is_a_503_and_status_four(run, hosted, name):
    case = _case(name)
    path, params = case.route("us")
    assert hosted(_down(), path, params).status_code == 503
    assert run(case.argv, _down()).code == 4


@pytest.mark.parametrize("argv, route, params", [
    (("series", "search", "q"), "/series/search", {"name": "q"}),
    (("author", "search", "jane"), "/author", {"name": "jane"}),
])
def test_a_search_whose_every_candidate_failed_is_status_four_where_the_route_says_404(
    run, hosted, argv, route, params
):
    """Pinned deviation: with candidates found and none fetchable, an empty
    answer would pass an outage off as a confirmed absence."""
    async def candidates_fail(region, path, query=None, extra_headers=None):
        if "contributors/" in path or "/series/" in path or path.rsplit("/", 1)[-1].startswith(
            "B0SERIES"
        ):
            raise AudibleAPIException("boom", upstream_status=503)
        return await fake_get(region, path, query, extra_headers)

    assert hosted(candidates_fail, route, {**params, "region": "us"}).status_code == 404
    result = run(argv, AsyncMock(side_effect=candidates_fail))
    assert result.code == 4
    assert result.stdout == b""


@pytest.mark.parametrize("argv, route, params", [
    (("series", "search", "q"), "/series/search", {"name": "q"}),
    (("author", "search", "jane"), "/author", {"name": "jane"}),
    (("author", "get", AUTHOR), f"/author/{AUTHOR}", {}),
])
def test_an_absence_is_a_404_and_status_three_for_the_lookups_that_confirm_one(
    run, hosted, argv, route, params
):
    assert hosted(_nf(), route, {**params, "region": "us"}).status_code == 404
    assert run(argv, _nf()).code == 3


# ============================================================
# NOTHING THE CALLER TYPED COMES BACK
# ============================================================

_TYPED = [
    ("series search", ["series", "search", PLANTED]),
    ("author get", ["author", "get", PLANTED]),
    ("author search", ["author", "search", PLANTED]),
    ("author books", ["author", "books", PLANTED]),
    ("author books-by-name", ["author", "books-by-name", PLANTED]),
    ("series books", ["series", "books", PLANTED]),
]


@pytest.mark.parametrize("verbosity", [[], ["-v"], ["-vv"]], ids=["quiet", "v", "vv"])
@pytest.mark.parametrize("argv", [a for _, a in _TYPED], ids=[n for n, _ in _TYPED])
@pytest.mark.parametrize("get", [_nf, _down], ids=["not-found", "outage"])
def test_no_failure_path_repeats_typed_text(run, argv, get, verbosity):
    result = run([*verbosity, *argv], get())
    assert result.code != 0
    assert PLANTED not in result.err
    assert PLANTED not in result.out


@pytest.mark.parametrize("argv", [a for _, a in _TYPED], ids=[n for n, _ in _TYPED])
def test_an_empty_answer_does_not_repeat_typed_text(run, argv):
    get = AsyncMock(return_value={"products": [], "model": {"items": []}, "categories": []})
    result = run(["-vv", *argv], get)
    assert PLANTED not in result.err
    assert PLANTED not in result.out


# ============================================================
# A SERIES' BOOKS WARN THE SAME WAY
# ============================================================

def test_a_series_with_a_missing_book_warns_with_the_librarys_reason(run, monkeypatch):
    monkeypatch.setattr(
        support, "CATALOGUE",
        {k: v for k, v in support.CATALOGUE.items() if k != "B0SCR00002"},
    )
    result = run(["series", "books", SERIES])
    assert result.code == 0
    (notice,) = [line for line in result.err.splitlines() if "may be incomplete" in line]
    assert _WARNING.match(notice)
    assert notice.endswith("(hydration-not-found)")
    assert len(json.loads(result.out)) == len(BOOKS) - 1
    assert run(["-q", "series", "books", SERIES]).err == ""


def test_a_whole_series_prints_no_notice(run):
    result = run(["series", "books", SERIES])
    assert result.code == 0 and "incomplete" not in result.err


def test_a_null_where_audible_sends_an_object_is_status_four(run):
    async def nulls(region, path, params=None, extra_headers=None):
        if "contributors/" in path:
            return {"contributor": None}
        return {"response_groups": ["a", "b"], "product": {"relationships": None}}

    for argv in (["author", "get", AUTHOR], ["series", "books", SERIES]):
        result = run(argv, AsyncMock(side_effect=nulls))
        assert result.code == 4, argv
        assert result.stdout == b""
