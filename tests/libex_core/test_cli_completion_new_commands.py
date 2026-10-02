"""
Shell completion for the series search, author and releases commands and the
filter and sort flags: bash is driven for real by sourcing the committed
script, and the fish script the generator produces (which another test holds
equal to the committed one) is checked by evaluating every row's condition, as
fish does, on the command line that reaches each command, so a row can neither
be missing where it belongs nor fire where it does not.
"""

# Standard library
import re

# Third party
import pytest

# Local
from libex_core.cli import _render
from libex_core.cli.commands.releases import WINDOWS
from libex_core.cli.parser import build_parser
from libex_core.shaping import BOOK_FILTER_SPECS, BOOK_SORT_FIELDS
from tests.libex_core._cli_support import walk_parsers
from tests.libex_core.test_cli_groups import _bash_complete, needs_bash

_PARSER = build_parser()
_FISH = _render.completion_script(_PARSER, "fish")
_SHAPE_FLAGS = sorted(
    ["--" + spec.name.replace("_", "-") for spec in BOOK_FILTER_SPECS] + ["--sort", "--order"]
)

# Section: bash, driven for real


def _offered(*words, cword=None):
    line = ["libex-core", *words]
    return _bash_complete(line, len(line) - 1 if cword is None else cword)


@needs_bash
def test_bash_completes_the_series_search_flags():
    assert _offered("series", "search", "--") == ["--help", "--region"]
    assert _offered("series", "search", "--re") == ["--region"]
    assert _offered("series", "search", "--region", "d") == ["de"]


@needs_bash
@pytest.mark.parametrize("path", [
    ("author", "books"), ("author", "books-by-name"), ("series", "books"),
    ("releases", "new"), ("releases", "coming-soon"),
], ids=" ".join)
def test_bash_offers_every_filter_and_sort_flag_on_a_command_that_takes_them(path):
    flags = set(_offered(*path, "--"))
    assert set(_SHAPE_FLAGS) <= flags
    assert "--region" in flags and "--help" in flags


@needs_bash
def test_bash_offers_book_bulk_the_same_flags_and_nothing_stale():
    flags = set(_offered("book", "bulk", "--"))
    assert set(_SHAPE_FLAGS) <= flags


@needs_bash
def test_bash_completes_a_flag_from_its_prefix_under_each_new_group():
    assert _offered("author", "books", "--long") == ["--longer-than"]
    assert _offered("author", "books-by-name", "--l") == ["--language", "--longer-than"]
    assert _offered("releases", "categories", "--d") == ["--depth"]
    assert _offered("releases", "new", "--da") == ["--days"]


@needs_bash
def test_bash_completes_the_values_of_the_flags_that_have_a_list():
    assert _offered("author", "books", "--sort", "") == list(BOOK_SORT_FIELDS)
    assert _offered("author", "books", "--sort", "re") == ["releaseDate"]
    assert _offered("author", "books", "--order", "") == ["asc", "desc"]
    assert _offered("series", "books", "--explicit", "") == ["true", "false"]
    assert _offered("releases", "new", "--has-pdf", "t") == ["true"]


@needs_bash
def test_bash_completes_the_release_windows_after_days():
    assert _offered("releases", "new", "--days", "") == list(WINDOWS)
    assert _offered("releases", "coming-soon", "--days", "2") == ["240"]
    assert _offered("releases", "new", "--days", "9") == ["90"]


@needs_bash
def test_bash_offers_no_value_for_a_flag_that_takes_free_text():
    assert _offered("releases", "new", "--category", "1") == []
    assert _offered("releases", "categories", "--depth", "1") == []
    assert _offered("author", "books", "--longer-than", "1") == []
    assert _offered("author", "books", "--genre", "f") == []


@needs_bash
def test_bash_lists_the_commands_of_each_new_group_and_not_flags():
    assert _offered("author", "") == ["get", "search", "books", "books-by-name"]
    assert _offered("releases", "") == ["new", "coming-soon", "categories"]
    assert _offered("series", "") == ["get", "books", "search"]


@needs_bash
def test_bash_still_completes_the_regions_after_region_on_each_new_command():
    for path in (("series", "search"), ("author", "get"), ("author", "search"),
                 ("author", "books"), ("author", "books-by-name"),
                 ("releases", "new"), ("releases", "coming-soon"),
                 ("releases", "categories")):
        assert _offered(*path, "--region", "j") == ["jp"], path


# Section: fish, every row's condition evaluated


def _rows():
    """(condition, payload) for every `complete` row of the committed fish
    script. payload is what the row offers: the flags it defines and the
    words it completes."""
    rows = []
    for line in _FISH.splitlines():
        if not line.startswith("complete -c libex-core"):
            continue
        condition = re.search(r" -n (?:'([^']*)'|(\S+))", line)
        if condition is None:
            continue  # the unconditional row that turns file completion off
        text = condition.group(1) or condition.group(2)
        flags = set(re.findall(r" -l ([a-z-]+)", line)) | set(re.findall(r" -s ([a-z])\b", line))
        flags = {("--" + f if len(f) > 1 else "-" + f) for f in flags}
        words = re.search(r" -a (?:'([^']*)'|(\S+))", line)
        offered = (words.group(1) or words.group(2)).split() if words else []
        rows.append((text, (frozenset(flags), tuple(offered))))
    return rows


def _holds(condition, words):
    """Whether fish would offer a row with this -n condition after the given
    words. Only the grammar the generated script uses is understood; anything
    else fails here so this evaluator is updated with the script."""
    if condition == "__fish_use_subcommand":
        return len(words) == 0
    for term in condition.split("; and "):
        negate = term.startswith("not ")
        term = term[4:] if negate else term
        assert term.startswith("__fish_seen_subcommand_from "), condition
        named = term.split()[1:]
        if any(word in named for word in words) == negate:
            return False
    return True


def _expected(path, node):
    """What a command line ending at this parser should be offered, from the
    parser alone."""
    import argparse

    holder = next(
        (a for a in node._actions if isinstance(a, argparse._SubParsersAction)), None
    )
    if holder is not None and path:
        # A group offers its commands where it is typed and nothing else: its
        # own help flag is not completed there.
        return [(frozenset(), tuple(holder.choices))]
    offered = []
    for action in node._actions:
        if isinstance(action, argparse._SubParsersAction):
            offered.extend((frozenset(), (name,)) for name in action.choices)
        elif action.option_strings:
            flags = frozenset(action.option_strings)
            offered.append((flags, tuple(action.choices or ())))
        elif action.choices:
            offered.append((frozenset(), tuple(action.choices)))
    return sorted(offered, key=repr)


_PATHS = [(path, node) for path, node in walk_parsers(_PARSER)]


@pytest.mark.parametrize("path, node", _PATHS, ids=lambda v: " ".join(v) if isinstance(v, tuple) else "")
def test_the_rows_that_fire_after_a_command_are_exactly_that_commands_own(path, node):
    fired = sorted(
        (payload for condition, payload in _rows() if _holds(condition, list(path))),
        key=repr,
    )
    assert fired == _expected(path, node), path


@pytest.mark.parametrize("path, node", [p for p in _PATHS if p[0]],
                         ids=lambda v: " ".join(v) if isinstance(v, tuple) else "")
def test_typing_flags_and_values_after_a_command_changes_nothing_the_rows_see(path, node):
    """fish's test looks at words, so a flag and its values after the command
    must not switch any row on or off."""
    plain = [row for row in _rows() if _holds(row[0], list(path))]
    typed = [row for row in _rows() if _holds(row[0], [*path, "--region", "de", "B0LOOK0001"])]
    assert typed == plain


def test_no_row_fires_for_a_command_the_parser_does_not_have():
    assert not [c for c, _ in _rows() if _holds(c, ["nonsense"]) and c != "__fish_use_subcommand"]


def test_the_evaluator_notices_a_row_that_fires_where_it_should_not():
    """A row for the top-level search flags, switched on by the word search
    alone, would fire after `series search`; the guard it carries is what
    stops it, and removing the guard is seen."""
    guarded = next(c for c, _ in _rows() if c.startswith("__fish_seen_subcommand_from search; and not"))
    assert _holds(guarded, ["search"])
    assert not _holds(guarded, ["series", "search"])
    assert not _holds(guarded, ["author", "search"])
    assert not _holds(guarded, ["abs", "search"])
    bare = "__fish_seen_subcommand_from search"
    assert _holds(bare, ["series", "search"])
