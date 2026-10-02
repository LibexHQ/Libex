"""
The db commands' option tables are written out in libex_core.cli._db_args so
that building the parser imports nothing heavy. Written-out copies drift, so
each is held equal here to the thing it copies: the readers' own parameters,
the hosted /db routes' query parameters and the library's sort fields. A reader
that gains a filter, or a route that does, fails these until the command line
has it too.
"""

# Standard library
import inspect
import typing

# Third party
import pytest

# Local
from app.api.routes.db import filters as hosted
from libex_core.cli._db_args import (
    AUDIOBOOKS_PRODUCED,
    BOOK_FILTERS,
    NARRATOR_SORT_FIELDS,
)
from libex_core.cli.commands import db as db_command
from libex_core.cli.parser import build_parser
from libex_core.shaping import BOOK_SORT_FIELDS
from libex_core.storage import filtering, sorting
from libex_core.storage.read import books, people, series
from tests.libex_core._cli_support import walk_parsers

TABLE = {name: kind for name, kind, _ in BOOK_FILTERS}
PAGING_AND_SORT = {"sort", "order", "limit", "page"}


def _params(function, drop=()):
    return {n: p for n, p in inspect.signature(function).parameters.items() if n not in drop}


def _kind(parameter):
    """The type a parameter takes, with `| None` taken off."""
    hint = parameter.annotation
    args = [a for a in typing.get_args(hint) if a is not type(None)]
    return args[0] if args else hint


# ============================================================
# THE FILTER TABLE IS THE READERS' FILTERS
# ============================================================

def test_the_table_has_every_filter_the_search_reader_takes_and_no_other():
    taken = set(_params(books.search_books, {"session", *PAGING_AND_SORT}))
    assert set(TABLE) == taken
    assert len(BOOK_FILTERS) == len(TABLE), "a filter listed twice"


def test_the_table_is_the_filter_builder_s_parameters_in_its_order():
    builder = list(_params(filtering.apply_book_filters, {"stmt"}))
    assert [name for name, _, _ in BOOK_FILTERS] == builder


@pytest.mark.parametrize("name", sorted(TABLE))
def test_each_filters_type_is_the_type_the_reader_declares(name):
    assert _kind(_params(books.search_books)[name]) is TABLE[name]


def test_every_filter_has_a_help_text():
    assert all(text.strip() for _, _, text in BOOK_FILTERS)


# (command, reader, the table names the command leaves out, what else the reader takes)
SHAPED = [
    ("books", books.search_books, set(), set()),
    ("author-books", people.get_author_books, {"region", "author_name"}, {"book_region"}),
    ("series-books", series.get_series_books, {"series_name"}, set()),
    ("narrator-books", people.get_narrator_books, set(), set()),
    ("plan", books.get_books_by_plan, {"plan_name"}, set()),
    ("vvab", books.get_vvab_books, {"is_vvab"}, set()),
    ("new-releases", books.get_new_releases, set(), {"days"}),
    ("coming-soon", books.get_coming_soon, set(), {"days"}),
]


@pytest.mark.parametrize("command, reader, left_out, extra", SHAPED, ids=[s[0] for s in SHAPED])
def test_each_command_offers_the_filters_its_reader_takes(command, reader, left_out, extra):
    """A reader's filters are its optional parameters; the required ones are
    what the command is about (an author's region, a plan's name)."""
    optional = {n for n, p in _params(reader).items() if p.default is not inspect.Parameter.empty}
    assert optional & set(TABLE) == set(TABLE) - left_out
    # the author and series lists are not paged, on the route or in the reader
    paged = command not in ("author-books", "series-books")
    assert optional - set(TABLE) == ({"sort", "order"} | extra | ({"limit", "page"} if paged else set()))
    parsers = dict(walk_parsers(build_parser()))
    assert ("--limit" in _flags(parsers[("db", command)])) is paged


def _flags(parser):
    return {f for a in parser._actions for f in a.option_strings}


@pytest.mark.parametrize("command, reader, left_out, extra", SHAPED, ids=[s[0] for s in SHAPED])
def test_each_command_has_a_flag_for_exactly_those_filters(command, reader, left_out, extra):
    parsers = dict((path, node) for path, node in walk_parsers(build_parser()))
    flags = _flags(parsers[("db", command)])
    wanted = {"--" + n.replace("_", "-") for n in set(TABLE) - left_out}
    missing = wanted - flags
    assert not missing
    gone = {"--" + n.replace("_", "-") for n in left_out}
    # author-books keeps --region, as the author's marketplace, not as a filter
    assert not (gone - {"--region"}) & flags


def test_the_commands_that_take_the_filters_are_the_ones_this_file_lists():
    parsers = walk_parsers(build_parser())
    carrying = {
        path[1] for path, node in parsers
        if path[:1] == ("db",) and "--title" in _flags(node) and len(path) == 2
    }
    assert carrying == {s[0] for s in SHAPED}


# ============================================================
# THE FILTER TABLE IS THE HOSTED ROUTES' FILTERS
# ============================================================

def test_the_table_is_the_hosted_routes_filter_set():
    assert {name: kind for name, (kind, _) in hosted.BOOK_FILTER_FIELDS.items()} == TABLE


# ============================================================
# SORT FIELDS, NARRATORS, PAGING
# ============================================================

def test_the_book_sort_choices_are_the_libraries_and_the_stores():
    assert set(sorting.BOOK_SORT_FIELDS) == set(BOOK_SORT_FIELDS)
    parsers = dict(walk_parsers(build_parser()))
    for command, *_ in SHAPED:
        sort = next(a for a in parsers[("db", command)]._actions if "--sort" in a.option_strings)
        assert tuple(sort.choices) == BOOK_SORT_FIELDS, command


def test_the_narrator_sort_choices_are_the_stores():
    assert set(NARRATOR_SORT_FIELDS) == set(sorting.NARRATOR_SORT_FIELDS)
    narrators = dict(walk_parsers(build_parser()))[("db", "narrators")]
    sort = next(a for a in narrators._actions if "--sort" in a.option_strings)
    assert tuple(sort.choices) == NARRATOR_SORT_FIELDS


def test_the_narrator_command_offers_every_option_the_reader_takes():
    taken = set(_params(people.search_narrators, {"session", "name", *PAGING_AND_SORT}))
    narrators = dict(walk_parsers(build_parser()))[("db", "narrators")]
    flags = _flags(narrators)
    assert {"--" + n.replace("_", "-") for n in taken} <= flags
    assert {"--sort", "--order", "--limit", "--page"} <= flags


def test_the_audiobooks_produced_buckets_are_the_hosted_routes_with_one_word_names():
    assert set(AUDIOBOOKS_PRODUCED.values()) == {e.value for e in hosted.AudiobooksProduced}
    assert all(" " not in key for key in AUDIOBOOKS_PRODUCED)
    assert len(AUDIOBOOKS_PRODUCED) == len(set(AUDIOBOOKS_PRODUCED.values()))
    narrators = dict(walk_parsers(build_parser()))[("db", "narrators")]
    choices = next(a for a in narrators._actions if "--audiobooks-produced" in a.option_strings).choices
    assert tuple(choices) == tuple(AUDIOBOOKS_PRODUCED)


def test_the_paging_the_commands_take_is_the_hosted_routes():
    parsers = dict(walk_parsers(build_parser()))
    for command in ("books", "narrators", "narrator-books", "plan", "vvab", "new-releases", "coming-soon"):
        by_flag = {f: a for a in parsers[("db", command)]._actions for f in a.option_strings}
        assert by_flag["--limit"].default == 20 and by_flag["--page"].default == 1
        assert by_flag["--limit"].type("100") == 100
        with pytest.raises(Exception):
            by_flag["--limit"].type("101")
        with pytest.raises(Exception):
            by_flag["--limit"].type("0")


def test_what_a_command_leaves_out_is_what_this_file_says_it_does():
    assert db_command._AUTHOR_BOOKS_EXCLUDED == {"region", "author_name"}
    assert db_command._SERIES_BOOKS_EXCLUDED == {"series_name"}
