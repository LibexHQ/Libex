"""
Commands that only hold further commands (`book`, `series`, `abs`, `narrator`)
in the generated man page and completion scripts: the man page documents each
nested command under its full name and never the group alone, the scripts offer
the group's own commands where the group is typed, and a new nested command
changes every generated file, so the drift check can tell.
"""

# Standard library
import argparse
import re

# Third party
import pytest

# Local
from libex_core.cli import _render
from libex_core.cli._args import add_command, add_group, add_region_option
from libex_core.cli.parser import build_parser
from tests.libex_core._cli_support import DATA_DIR, walk_parsers


def _is_group(parser):
    return any(isinstance(a, argparse._SubParsersAction) for a in parser._actions)


def _leaves(parser):
    return [(path, node) for path, node in walk_parsers(parser) if path and not _is_group(node)]


def _groups(parser):
    return {
        path[0]: [p[1] for p, _ in _leaves(parser) if p[0] == path[0]]
        for path, node in walk_parsers(parser)
        if len(path) == 1 and _is_group(node)
    }


_PARSER = build_parser()
_LEAVES = _leaves(_PARSER)
_GROUPS = _groups(_PARSER)
_MAN = _render.man_page(_PARSER)
_SCRIPTS = {
    shell: _render.completion_script(_PARSER, shell) for shell in ("bash", "zsh", "fish")
}


def _headings(man):
    return re.findall(r"^\.SS (.+)$", man, flags=re.M)


def _sections(man):
    parts = re.split(r"^\.SS (.+)$", man, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _roff(text):
    return text.replace("-", r"\-")


# ============================================================
# THE WALK FOUND THE GROUPS
# ============================================================

def test_the_four_groups_and_their_commands_are_what_this_file_checks():
    """A walk that found no groups would pass everything below for free."""
    assert _GROUPS == {
        "book": ["get", "bulk", "chapters"],
        "series": ["get", "books"],
        "abs": ["search", "quick-search"],
        "narrator": ["books"],
    }


# ============================================================
# MAN PAGE
# ============================================================

def test_the_man_page_documents_every_command_that_does_something_and_nothing_else():
    expected = [" ".join(path) for path, _ in _LEAVES]
    assert _headings(_MAN) == expected
    assert "book" not in _headings(_MAN)
    assert not set(_GROUPS) & set(_headings(_MAN))


@pytest.mark.parametrize("path, leaf", _LEAVES, ids=lambda v: " ".join(v) if isinstance(v, tuple) else "")
def test_each_nested_command_is_documented_in_full_under_its_whole_name(path, leaf):
    section = _sections(_MAN)[" ".join(path)]
    # Compared with the hyphen escapes undone: whether a command's own name
    # is escaped is not what this checks.
    assert f"\\fBlibex-core {' '.join(path)}\\fR" in section.replace("\\-", "-")
    for action in leaf._actions:
        for flag in action.option_strings:
            assert _roff(flag) in section, (path, flag)
        if not action.option_strings and action.dest != "help":
            name = action.metavar or action.dest
            assert f"\\fI{name}\\fR" in section, (path, name)
    assert _render._roff(leaf.description) in section


def test_the_man_page_carries_each_nested_commands_own_text():
    sections = _sections(_MAN)
    assert "Exits 3 when Audible has no such book" in sections["book get"]
    assert "1000 books" in sections["book bulk"]
    assert "Exits 3 when there is no chapter list" in sections["book chapters"]
    assert "series record" in sections["series get"]
    assert "Exits 3 when nothing matched" in sections["narrator books"]


# ============================================================
# COMPLETIONS
# ============================================================

@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_the_top_level_offers_the_groups_and_not_what_is_inside_them(shell):
    script = _SCRIPTS[shell]
    for group in _GROUPS:
        assert re.search(rf"\b{group}\b", script)
    if shell == "bash":
        top = next(line for line in script.splitlines() if line.lstrip().startswith('"") candidates'))
        assert set(top.split("(")[1].rstrip(") ;").split()) >= {*_GROUPS, "search", "quick-search"}
        for nested in ("get", "bulk", "chapters", "books"):
            assert nested not in top.split()


def test_bash_offers_a_groups_commands_at_the_word_after_it():
    for group, names in _GROUPS.items():
        branch = re.search(
            rf"^        {group}\)\n(.*?)^            ;;", _SCRIPTS["bash"], flags=re.M | re.S
        )
        assert branch, group
        assert f"candidates=({' '.join(names)})" in branch.group(1)


def test_zsh_offers_a_groups_commands_as_its_first_argument():
    for group, names in _GROUPS.items():
        assert (
            f"                {group})\n"
            f"                    _arguments \\\n"
            f"                        '1:command:({' '.join(names)})'"
        ) in _SCRIPTS["zsh"], group


def test_fish_offers_a_groups_commands_after_the_group_word():
    for group, names in _GROUPS.items():
        assert (
            f"complete -c libex-core -n '__fish_seen_subcommand_from {group}' "
            f"-a '{' '.join(names)}'"
        ) in _SCRIPTS["fish"], group


def test_a_group_is_listed_with_its_summary_in_zsh_and_fish():
    for group in _GROUPS:
        assert re.search(rf"^                '{group}:[A-Za-z ]+'$", _SCRIPTS["zsh"], re.M)
        assert re.search(
            rf"-n __fish_use_subcommand -a {group} -d '[A-Za-z ]+'", _SCRIPTS["fish"]
        )


# ============================================================
# DRIFT -- a nested command changes every generated file
# ============================================================

def _parser_with(extra_leaf):
    parser = build_parser()
    container = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    book = next(
        a for a in container.choices["book"]._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    if extra_leaf:
        added = add_command(book, "frobnicate", "frob a book", "Frob one book.")
        added.add_argument("asin", metavar="ASIN", help="ASIN of the book")
        add_region_option(added)
        added.set_defaults(handler=lambda args: 0)
    return parser


def test_the_unchanged_parser_renders_the_committed_files_and_a_nested_addition_does_not():
    committed = {
        _render.MAN_PATH: (DATA_DIR / _render.MAN_PATH).read_bytes(),
        _render.BASH_PATH: (DATA_DIR / _render.BASH_PATH).read_bytes(),
        _render.ZSH_PATH: (DATA_DIR / _render.ZSH_PATH).read_bytes(),
        _render.FISH_PATH: (DATA_DIR / _render.FISH_PATH).read_bytes(),
    }
    same = _render.artefacts(_parser_with(False))
    changed = _render.artefacts(_parser_with(True))
    for relative, text in committed.items():
        assert same[relative].encode("utf-8") == text, relative
        assert changed[relative].encode("utf-8") != text, relative
    assert "book frobnicate" in _headings(changed[_render.MAN_PATH])
    assert "get bulk chapters frobnicate" in changed[_render.BASH_PATH]


def test_a_group_built_with_the_shared_helpers_renders_in_all_four_files():
    parser = argparse.ArgumentParser(prog="libex-core", add_help=False)
    container = parser.add_subparsers(dest="command")
    inner = add_group(container, "fruit", "look at fruit", "Look at fruit.")
    peel = add_command(inner, "peel", "peel one", "Peel one fruit.")
    peel.add_argument("name", metavar="NAME", help="name of the fruit")
    add_region_option(peel)
    files = _render.artefacts(parser)
    assert _headings(files[_render.MAN_PATH]) == ["fruit peel"]
    assert "candidates=(peel)" in files[_render.BASH_PATH]
    assert "'1:command:(peel)'" in files[_render.ZSH_PATH]
    assert "-a 'peel'" in files[_render.FISH_PATH]
