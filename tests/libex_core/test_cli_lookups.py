"""
The libex-core lookup commands, run through main() against a stand-in session:
that each prints exactly what its library function returns, the exit status of
each way a lookup can end, the region and paging options, the bulk command's
input handling and caps, and that nothing a caller typed comes back on
standard error.
"""

# Standard library
import argparse
import asyncio
import io
import json
import types
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.audible.client import VALID_REGIONS
from libex_core.cli import _args
from libex_core.cli.commands import book as book_command
from libex_core.cli.parser import build_parser
from libex_core.exceptions import AudibleAPIException, NotFoundException
from tests.libex_core._cli_lookup_support import (
    CASE_IDS,
    CASES,
    EGRESS,
    PLAIN_NOT_FOUND_CASES,
    PLANTED,
    install_session,
    library_json,
    unclocked,
)
from tests.libex_core._cli_support import walk_parsers
from tests.libex_core.test_lookup import _asins, _batch_get

MAX_FILE_BYTES = 1 << 20


@pytest.fixture(params=CASES, ids=CASE_IDS)
def case(request):
    return request.param


@pytest.fixture
def run(run_cli, monkeypatch):
    """Runs a command against a given stand-in `get`, with the egress switch
    the commands need set."""

    def go(argv, get, **env):
        install_session(monkeypatch, get)
        return run_cli(list(argv), env={**EGRESS, **env})

    return go


# ============================================================
# OUTPUT -- what each command prints is what its library function returns
# ============================================================

def test_each_command_prints_its_lookup_result_as_one_line_of_json(run, case):
    result = run(case.argv, case.make_get())
    assert result.code == 0, result.err
    # A shortfall in a bulk answer is a warning on standard error, never
    # anything else; the body stays whole on standard output.
    assert all(
        line.startswith("libex-core: WARNING: ") for line in result.err.splitlines()
    )
    assert result.out.endswith("\n") and result.out.count("\n") == 1
    printed = json.loads(result.out)
    assert unclocked(printed) == unclocked(library_json(case))
    assert printed, "an empty result would equal an empty result"


# A suggestions request carries a fresh session id and the time, which differ
# between any two runs of the same question.
_PER_RUN = {"session_id", "local_time"}


def _requests(get):
    # Sorted: a bulk lookup sends its chunks concurrently, in no fixed order.
    return sorted(
        repr((
            call.args[0],
            call.args[1],
            {k: v for k, v in (call.args[2] if len(call.args) > 2 else {}).items()
             if k not in _PER_RUN},
        ))
        for call in get.await_args_list
    )


def test_each_command_sends_audible_the_requests_its_lookup_sends(run, case):
    """The printed JSON cannot show an option that was dropped on the way to
    the lookup, because the stand-in answers every request alike. The
    requests can."""
    get = case.make_get()
    assert run(case.argv, get).code == 0
    direct = case.make_get()
    asyncio.run(case.lookup(direct, "us"))
    assert _requests(get) == _requests(direct)
    assert _requests(get)


@pytest.mark.parametrize("region", sorted(VALID_REGIONS))
def test_each_command_asks_the_region_it_was_given(run, case, region):
    get = case.make_get()
    result = run([*case.argv, "--region", region], get)
    assert result.code == 0, result.err
    assert {call.args[0] for call in get.await_args_list} == {region}
    assert unclocked(json.loads(result.out)) == unclocked(library_json(case, region))


def test_each_command_asks_us_when_no_region_is_given(run, case):
    get = case.make_get()
    assert run(case.argv, get).code == 0
    assert {call.args[0] for call in get.await_args_list} == {"us"}


def test_a_book_in_another_region_says_so_in_what_is_printed(run):
    get = _batch_get(known={"B0LOOK0001"})
    out = json.loads(run(["book", "get", "B0LOOK0001", "--region", "jp"], get).out)
    assert out["region"] == "jp"


def test_the_asin_given_in_lower_case_is_asked_for_in_its_canonical_form(run):
    get = _batch_get(known={"B0LOOK0001"})
    result = run(["book", "get", "b0look0001"], get)
    assert json.loads(result.out)["asin"] == "B0LOOK0001"
    assert get.await_args.args[1].endswith("/B0LOOK0001")


# ============================================================
# REGIONS
# ============================================================

def test_the_cli_region_list_is_the_library_region_list():
    assert set(_args.REGIONS) == VALID_REGIONS
    assert len(_args.REGIONS) == len(set(_args.REGIONS)) == 11


# The db commands answer from the store, which holds every region: their book
# --region filter is optional, and db stats scopes by it only when given.
_DB_REGION_UNSET = {
    ("db", "books"), ("db", "series-books"), ("db", "narrator-books"), ("db", "plan"),
    ("db", "vvab"), ("db", "new-releases"), ("db", "coming-soon"), ("db", "stats"),
    ("db", "book"), ("db", "chapters"), ("db", "series"),
}
# The author's marketplace is part of an author's identity, so it defaults.
_DB_REGION_DEFAULTED = {("db", "author"), ("db", "author-books")}


def test_the_default_region_is_us_on_every_command_that_makes_a_request():
    seen = 0
    for path, parser in walk_parsers(build_parser()):
        if path[:1] == ("db",):
            continue
        for action in parser._actions:
            if "--region" in action.option_strings:
                seen += 1
                assert action.default == "us", path
                assert tuple(action.choices) == _args.REGIONS, path
    assert seen == 18  # every command that makes a request


def test_the_db_commands_region_is_optional_except_where_it_names_an_author_s_marketplace():
    unset, defaulted = set(), set()
    for path, parser in walk_parsers(build_parser()):
        if path[:1] != ("db",):
            continue
        for action in parser._actions:
            if "--region" in action.option_strings:
                assert tuple(action.choices) == _args.REGIONS, path
                (unset if action.default is None else defaulted).add(path)
                if action.default is not None:
                    assert action.default == "us", path
    assert unset == _DB_REGION_UNSET
    assert defaulted == _DB_REGION_DEFAULTED


@pytest.mark.parametrize("region", ["xx", "US", "", "us ", "u"])
def test_an_unknown_region_is_a_usage_error_before_any_request(run, region):
    get = AsyncMock()
    result = run(["book", "get", "B0LOOK0001", "--region", region], get)
    assert result.code == 2
    assert result.stdout == b""
    get.assert_not_called()


# ============================================================
# EXIT STATUS -- 0, 2, 3 and 4, per path
# ============================================================

def _failing(exc):
    return AsyncMock(side_effect=exc)


def _error_lines(result):
    return [
        line for line in result.err.splitlines() if line.startswith("libex-core: error: ")
    ]


# Commands whose "nothing there" answer is a 404 on the route and status 3 here.
# A bulk lookup reports its misses in the body, a by-name lookup reads a 404
# on its first page as an outage, and the release scans are a 503 on the route;
# test_cli_author_releases.py covers each of those.
_NOT_FOUND_CASES = PLAIN_NOT_FOUND_CASES


@pytest.mark.parametrize("case", _NOT_FOUND_CASES, ids=lambda c: c.name)
def test_audible_saying_it_is_not_there_exits_three(run, case):
    result = run(case.argv, _failing(NotFoundException("gone")))
    assert result.code == 3
    assert result.stdout == b""
    (line,) = _error_lines(result)
    assert line.endswith("(code: not_on_audible)")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_audible_being_unreachable_exits_four(run, case):
    result = run(case.argv, _failing(AudibleAPIException("boom", upstream_status=503)))
    assert result.code == 4
    assert result.stdout == b""
    assert "upstream_unavailable" in result.err


@pytest.mark.parametrize(
    "argv",
    [
        ["book", "get", "not-an-asin"],
        ["book", "chapters", "not-an-asin"],
        ["series", "get", "not-an-asin"],
        ["series", "books", "not-an-asin"],
        ["book", "bulk", "not-an-asin"],
        ["book", "bulk"],
        ["abs", "quick-search"],
    ],
    ids=lambda argv: " ".join(argv),
)
def test_a_rejected_argument_exits_two_without_asking_audible(run, argv):
    get = AsyncMock()
    result = run(argv, get)
    assert result.code == 2
    assert result.stdout == b""
    assert "(code: invalid_request)" in result.err
    get.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        ["book", "get"],
        ["book", "get", "B0LOOK0001", "extra"],
        ["book"],
        ["series"],
        ["abs"],
        ["narrator"],
        ["narrator", "books"],
        ["quick-search"],
        ["search", "--no-such-flag"],
        ["book", "nope"],
    ],
    ids=lambda argv: " ".join(argv),
)
def test_a_command_line_that_is_not_understood_exits_two(run, argv):
    get = AsyncMock()
    result = run(argv, get)
    assert result.code == 2
    assert result.stdout == b""
    assert result.err.startswith("usage: libex-core")
    get.assert_not_called()


def test_no_proxy_and_no_egress_switch_is_a_config_error_not_a_request(run_cli):
    result = run_cli(["book", "get", "B0LOOK0001"])
    assert result.code == 5
    assert result.stdout == b""
    assert result.err.endswith("(code: config_error)\n")


def test_a_failure_prints_one_error_line_and_no_traceback(run):
    result = run(
        ["book", "get", "B0LOOK0001"], _failing(AudibleAPIException("boom"))
    )
    assert len(_error_lines(result)) == 1
    assert result.err.splitlines()[-1].startswith("libex-core: error: ")
    assert b"Traceback" not in result.stderr


# ============================================================
# PARTIAL ANSWERS -- still a result, still status 0
# ============================================================

def test_a_bulk_answer_with_unfetched_books_is_printed_whole_with_status_zero(run):
    asins = _asins(51)
    get = _batch_get(known=set(asins), fail={asins[50]})
    result = run(["book", "bulk", *asins], get)
    assert result.code == 0
    assert "libex-core: WARNING: Partial hydration shortfall" in result.err
    assert _error_lines(result) == []
    body = json.loads(result.out)
    assert set(body) == {"books", "notFound", "placeholderRecords", "notFetched"}
    assert body["notFetched"] == [asins[50]]
    assert [b["asin"] for b in body["books"]] == asins[:50]
    assert body["notFound"] == [] and body["placeholderRecords"] == []


def test_bulk_misses_and_placeholders_are_reported_beside_the_books(run):
    get = _batch_get(known={"B0LOOK0001"}, placeholders={"B0LOOK0002"})
    result = run(["book", "bulk", "B0LOOK0001", "B0LOOK0002", "B0LOOK0003"], get)
    assert result.code == 0
    body = json.loads(result.out)
    assert [b["asin"] for b in body["books"]] == ["B0LOOK0001"]
    assert body["placeholderRecords"] == ["B0LOOK0002"]
    assert body["notFound"] == ["B0LOOK0003"]
    assert body["notFetched"] == []


def test_a_bulk_request_nothing_could_answer_is_an_outage_not_a_result(run):
    get = _batch_get(known={"B0LOOK0001"}, fail={"B0LOOK0001"})
    result = run(["book", "bulk", "B0LOOK0001"], get)
    assert result.code == 4
    assert result.stdout == b""


# ============================================================
# BULK INPUT -- splitting, the 1000 cap, the file and its cap
# ============================================================

@pytest.mark.parametrize(
    "parts, expected",
    [
        (["A,B"], ["A", "B"]),
        (["A, B ,,C"], ["A", "B", "C"]),
        (["A B\tC\nD\r\nE"], ["A", "B", "C", "D", "E"]),
        (["A", "B,C", " D "], ["A", "B", "C", "D"]),
        (["", " ", ",", ",\n,"], []),
        ([",A,"], ["A"]),
    ],
)
def test_split_asins_takes_commas_and_white_space_and_drops_empties(parts, expected):
    assert book_command.split_asins(parts) == expected


def _books_of(result):
    assert result.code == 0, result.err
    return [b["asin"] for b in json.loads(result.out)["books"]]


def test_positionals_and_a_file_are_both_read_in_that_order(run, tmp_path):
    path = tmp_path / "asins.txt"
    path.write_text("B0LOOK0003\nB0LOOK0004, B0LOOK0005\n")
    asins = [f"B0LOOK000{i}" for i in range(1, 6)]
    result = run(
        ["book", "bulk", "B0LOOK0001,B0LOOK0002", "--file", str(path)],
        _batch_get(known=set(asins)),
    )
    assert _books_of(result) == asins


def test_a_file_alone_is_enough(run, tmp_path):
    path = tmp_path / "asins.txt"
    path.write_text("B0LOOK0001 B0LOOK0002")
    result = run(["book", "bulk", "--file", str(path)], _batch_get(known={"B0LOOK0001", "B0LOOK0002"}))
    assert _books_of(result) == ["B0LOOK0001", "B0LOOK0002"]


def test_dash_reads_the_list_from_standard_input(run, monkeypatch):
    monkeypatch.setattr(
        "sys.stdin", types.SimpleNamespace(buffer=io.BytesIO(b"B0LOOK0001\nB0LOOK0002\n"))
    )
    result = run(
        ["book", "bulk", "--file", "-"], _batch_get(known={"B0LOOK0001", "B0LOOK0002"})
    )
    assert _books_of(result) == ["B0LOOK0001", "B0LOOK0002"]


def test_a_thousand_asins_are_accepted_and_a_thousand_and_one_are_not(run):
    asins = _asins(1001)
    get = _batch_get(known=set(asins))
    result = run(["book", "bulk", *asins[:1000]], get)
    assert result.code == 0, result.err
    assert len(json.loads(result.out)["books"]) == 1000

    refused = AsyncMock()
    result = run(["book", "bulk", *asins], refused)
    assert result.code == 2
    assert result.stdout == b""
    assert "(code: invalid_request)" in result.err
    refused.assert_not_called()


def test_a_file_of_exactly_one_mebibyte_is_read(run, tmp_path):
    path = tmp_path / "asins.txt"
    path.write_bytes(b"B0LOOK0001" + b" " * (MAX_FILE_BYTES - 10))
    assert path.stat().st_size == MAX_FILE_BYTES
    result = run(["book", "bulk", "--file", str(path)], _batch_get(known={"B0LOOK0001"}))
    assert _books_of(result) == ["B0LOOK0001"]


def test_a_file_one_byte_over_a_mebibyte_is_refused_unread(run, tmp_path):
    path = tmp_path / "asins.txt"
    path.write_bytes(b"B0LOOK0001" + b" " * (MAX_FILE_BYTES - 9))
    assert path.stat().st_size == MAX_FILE_BYTES + 1
    get = AsyncMock()
    result = run(["book", "bulk", "--file", str(path)], get)
    assert result.code == 2
    assert result.stdout == b""
    get.assert_not_called()


def test_standard_input_is_capped_too(run, monkeypatch):
    monkeypatch.setattr(
        "sys.stdin",
        types.SimpleNamespace(buffer=io.BytesIO(b"B0LOOK0001" + b" " * MAX_FILE_BYTES)),
    )
    result = run(["book", "bulk", "--file", "-"], AsyncMock())
    assert result.code == 2


def test_the_read_never_asks_for_more_than_the_cap_plus_one_byte(run, monkeypatch):
    asked = []

    class Stream(io.BytesIO):
        def read(self, size=-1):
            asked.append(size)
            return super().read(size)

    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(buffer=Stream(b"B0LOOK0001")))
    run(["book", "bulk", "--file", "-"], _batch_get(known={"B0LOOK0001"}))
    assert asked == [MAX_FILE_BYTES + 1]


def test_a_named_file_is_read_no_further_than_the_cap_plus_one_byte(run, monkeypatch):
    asked = []

    class Handle(io.BytesIO):
        def read(self, size=-1):
            asked.append(size)
            return super().read(size)

    class FakePath:
        def __init__(self, name):
            pass

        def open(self, mode):
            return Handle(b"B0LOOK0001")

    monkeypatch.setattr(book_command, "Path", FakePath)
    result = run(["book", "bulk", "--file", "list.txt"], _batch_get(known={"B0LOOK0001"}))
    assert _books_of(result) == ["B0LOOK0001"]
    assert asked == [MAX_FILE_BYTES + 1]


def test_an_unreadable_file_exits_two_and_does_not_repeat_the_path(run, tmp_path):
    missing = tmp_path / f"{PLANTED}-missing.txt"
    directory = tmp_path / f"{PLANTED}-dir"
    directory.mkdir()
    binary = tmp_path / f"{PLANTED}-binary.txt"
    binary.write_bytes(b"\xff\xfe\x00B0LOOK0001")
    for path in (missing, directory, binary):
        get = AsyncMock()
        result = run(["book", "bulk", "--file", str(path)], get)
        assert result.code == 2, path.name
        assert result.stdout == b""
        assert PLANTED not in result.err and str(tmp_path) not in result.err
        assert "(code: invalid_request)" in result.err
        get.assert_not_called()


def test_an_unreadable_file_leaves_no_path_even_at_debug_verbosity(run, tmp_path):
    missing = tmp_path / f"{PLANTED}-missing.txt"
    result = run(["-vv", "book", "bulk", "--file", str(missing)], AsyncMock())
    assert result.code == 2
    assert PLANTED not in result.err and str(tmp_path) not in result.err


# ============================================================
# PAGING -- bounded_int and its fixed message
# ============================================================

@pytest.mark.parametrize("text, value", [("1", 1), ("50", 50), ("7", 7)])
def test_bounded_int_accepts_the_range_and_its_ends(text, value):
    assert _args.bounded_int(1, 50)(text) == value


@pytest.mark.parametrize(
    "text", ["0", "51", "-1", "x", "", "1.5", "True", "9" * 5000, "five", "\x00"]
)
def test_bounded_int_rejects_with_a_message_that_is_the_same_for_every_input(text):
    convert = _args.bounded_int(1, 50)
    with pytest.raises(argparse.ArgumentTypeError) as info:
        convert(text)
    assert str(info.value) == "must be a whole number from 1 to 50"


def test_bounded_int_reports_its_own_bounds():
    with pytest.raises(argparse.ArgumentTypeError) as info:
        _args.bounded_int(0, 9)("10")
    assert str(info.value) == "must be a whole number from 0 to 9"


_PAGE_FLAGS = [("--limit", ["0", "51", "-1"]), ("--page", ["10", "-1"])]
_PREFIXES = [("search",), ("narrator", "books", "N")]


@pytest.mark.parametrize("prefix", _PREFIXES, ids=["search", "narrator"])
@pytest.mark.parametrize(
    "flag, value",
    [(f, v) for f, values in _PAGE_FLAGS for v in values]
    + [("--limit", "abc"), ("--page", "x"), ("--limit", ""), ("--page", "1.5")],
)
def test_an_out_of_range_page_option_is_a_usage_error_before_any_request(
    run, prefix, flag, value
):
    get = AsyncMock()
    result = run([*prefix, flag, value], get)
    assert result.code == 2
    assert result.stdout == b""
    assert "must be a whole number from" in result.err
    get.assert_not_called()


@pytest.mark.parametrize("prefix", _PREFIXES, ids=["search", "narrator"])
@pytest.mark.parametrize(
    "flag, value",
    [("--limit", "7777777"), ("--limit", "abc7777"), ("--page", "8888888"),
     ("--page", "x8888"), ("--limit", "-6666666")],
)
def test_a_rejected_page_option_is_not_echoed_back(run, prefix, flag, value):
    result = run([*prefix, flag, value], AsyncMock())
    assert result.code == 2
    assert value not in result.err.split("error:", 1)[1]
    assert value.lstrip("-") not in result.err.split("error:", 1)[1]


@pytest.mark.parametrize("limit, page", [("1", "0"), ("50", "9")])
def test_the_ends_of_the_paging_range_are_sent_to_audible(run, limit, page):
    get = AsyncMock(return_value={"products": [{"asin": "B0LOOK0001", "title": "t",
                                                "publication_datetime": "2020-01-01T00:00:00Z"}]})
    result = run(["search", "--title", "t", "--limit", limit, "--page", page], get)
    assert result.code == 0, result.err
    params = get.await_args.args[2]
    assert (params["num_results"], params["page"]) == (int(limit), int(page))


def test_paging_defaults_are_ten_results_from_the_first_page(run):
    get = AsyncMock(return_value={"products": [{"asin": "B0LOOK0001", "title": "t",
                                                "publication_datetime": "2020-01-01T00:00:00Z"}]})
    assert run(["search", "--title", "t"], get).code == 0
    params = get.await_args.args[2]
    assert (params["num_results"], params["page"]) == (10, 0)


# ============================================================
# NOTHING THE CALLER TYPED COMES BACK
# ============================================================

_TYPED = [
    ("book get", ["book", "get", PLANTED]),
    ("book chapters", ["book", "chapters", PLANTED]),
    ("series get", ["series", "get", PLANTED]),
    ("series books", ["series", "books", PLANTED]),
    ("book bulk", ["book", "bulk", PLANTED]),
    ("search", ["search", "--title", PLANTED, "--author", PLANTED, "--keywords", PLANTED]),
    ("quick-search", ["quick-search", PLANTED]),
    ("abs search", ["abs", "search", "--title", PLANTED]),
    ("abs quick-search", ["abs", "quick-search", "--keywords", PLANTED]),
    ("narrator books", ["narrator", "books", PLANTED]),
]


@pytest.mark.parametrize("verbosity", [[], ["-v"], ["-vv"]], ids=["quiet", "v", "vv"])
@pytest.mark.parametrize("argv", [a for _, a in _TYPED], ids=[n for n, _ in _TYPED])
@pytest.mark.parametrize(
    "exc",
    [NotFoundException("gone"), AudibleAPIException("boom", upstream_status=502)],
    ids=["not-found", "outage"],
)
def test_no_failure_path_repeats_typed_text_on_standard_error(run, argv, exc, verbosity):
    result = run([*verbosity, *argv], _failing(exc))
    assert result.code != 0
    assert PLANTED not in result.err
    assert PLANTED not in result.out


@pytest.mark.parametrize("argv", [a for _, a in _TYPED], ids=[n for n, _ in _TYPED])
def test_an_empty_search_answer_does_not_repeat_typed_text(run, argv):
    get = AsyncMock(return_value={"products": [], "model": {"items": []}})
    result = run(["-vv", *argv], get)
    assert PLANTED not in result.err
