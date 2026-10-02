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
import shutil
import subprocess

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


def _holders(word):
    """The groups that hold a command named word, in the order the parser
    lists them. A top-level command of that name is switched off in fish after
    any of them, because the test for its name matches anywhere on the line."""
    return " ".join(group for group, names in _GROUPS.items() if word in names)
_MAN = _render.man_page(_PARSER)
_SCRIPTS = {
    shell: _render.completion_script(_PARSER, shell) for shell in ("bash", "zsh", "fish")
}


def _headings(man):
    return [h.replace("\\-", "-") for h in re.findall(r"^\.SS (.+)$", man, flags=re.M)]


def _sections(man):
    parts = re.split(r"^\.SS (.+)$", man, flags=re.M)
    return {parts[i].replace("\\-", "-"): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _roff(text):
    return text.replace("-", r"\-")


# ============================================================
# THE WALK FOUND THE GROUPS
# ============================================================

def test_the_groups_and_their_commands_are_what_this_file_checks():
    """A walk that found no groups would pass everything below for free."""
    assert _GROUPS == {
        "book": ["get", "bulk", "chapters"],
        "series": ["get", "books", "search"],
        "author": ["get", "search", "books", "books-by-name"],
        "abs": ["search", "quick-search"],
        "narrator": ["books"],
        "releases": ["new", "coming-soon", "categories"],
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
    assert f"\\fBlibex\\-core {_roff(' '.join(path))}\\fR" in section
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


def test_zsh_offers_a_groups_commands_when_none_is_named_yet():
    for group, names in _GROUPS.items():
        assert re.search(
            rf"^                {group}\)\n\s+case \$\{{line\[2\]\}} in\n"
            rf"(?:.*\n)*?\s+\*\)\n\s+_arguments \\\n"
            rf"\s+'1:command:\({' '.join(names)}\)'\n",
            _SCRIPTS["zsh"],
            re.M,
        ), group


def test_fish_offers_a_groups_commands_until_one_is_named():
    for group, names in _GROUPS.items():
        words = " ".join(names)
        assert (
            f"complete -c libex-core -n '__fish_seen_subcommand_from {group}; and "
            f"not __fish_seen_subcommand_from {words}' -a '{words}'\n"
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


# ============================================================
# LEAF FLAGS COMPLETE UNDER A GROUP
# ============================================================

_REGIONS = "us uk ca au de fr it es jp in br".split()
_REQUEST_PATHS = [
    path for path, node in _LEAVES
    if any("--region" in a.option_strings for a in node._actions)
]


def _flags(leaf):
    return sorted(f for a in leaf._actions for f in a.option_strings if f.startswith("--"))


def _bash_complete(words, cword):
    """What bash's own completion would offer, by sourcing the committed
    script in a real bash and calling its function."""
    script = DATA_DIR / _render.BASH_PATH
    quoted = " ".join(f"'{w}'" for w in words)
    result = subprocess.run(
        [
            "bash", "-c",
            f"source '{script}'; COMP_WORDS=({quoted}); COMP_CWORD={cword}; "
            '_libex_core; printf "%s\\n" "${COMPREPLY[@]}"',
        ],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return result.stdout.split()


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")


@needs_bash
def test_bash_completes_a_nested_commands_flag_from_its_prefix():
    assert _bash_complete(["libex-core", "book", "get", "--re"], 3) == ["--region"]
    assert _bash_complete(["libex-core", "book", "bulk", "--f"], 3) == ["--file"]
    assert _bash_complete(["libex-core", "abs", "quick-search", "--k"], 3) == ["--keywords"]
    assert _bash_complete(["libex-core", "narrator", "books", "--l"], 3) == ["--limit"]


@needs_bash
def test_bash_completes_the_regions_after_the_region_flag():
    assert _bash_complete(["libex-core", "book", "get", "--region", ""], 4) == _REGIONS
    assert _bash_complete(["libex-core", "book", "get", "--region", "u"], 4) == ["us", "uk"]
    assert _bash_complete(["libex-core", "abs", "search", "--region", "j"], 4) == ["jp"]
    assert _bash_complete(["libex-core", "search", "--region", "b"], 3) == ["br"]


@needs_bash
@pytest.mark.parametrize("path", _REQUEST_PATHS, ids=" ".join)
def test_bash_offers_every_flag_of_every_request_command(path):
    leaf = dict(_LEAVES)[path]
    offered = _bash_complete(["libex-core", *path, "-"], len(path) + 1)
    assert sorted(f for f in offered if f.startswith("--")) == _flags(leaf)
    assert "-h" in offered
    regions = _bash_complete(["libex-core", *path, "--region", ""], len(path) + 2)
    assert regions == _REGIONS


@needs_bash
def test_bash_still_offers_the_group_commands_and_not_flags_at_the_group_word():
    assert _bash_complete(["libex-core", "book", ""], 2) == ["get", "bulk", "chapters"]
    assert _bash_complete(["libex-core", ""], 1)[-3:] == ["releases", "completion", "config"]


@needs_bash
def test_bash_offers_nothing_for_a_command_it_does_not_know():
    assert _bash_complete(["libex-core", "book", "nope", ""], 3) == []


def _zsh_leaf_block(group, leaf):
    match = re.search(
        rf"^                {group}\)\n\s+case \$\{{line\[2\]\}} in\n(.*?)^                    esac\n",
        _SCRIPTS["zsh"],
        re.M | re.S,
    )
    assert match, group
    inner = re.search(rf"^                        {leaf}\)\n(.*?)^                            ;;", match.group(1), re.M | re.S)
    assert inner, (group, leaf)
    return inner.group(1)


@pytest.mark.parametrize("path", [p for p in _REQUEST_PATHS if len(p) == 2], ids=" ".join)
def test_zsh_spec_holds_each_nested_commands_flags_and_region_choices(path):
    block = _zsh_leaf_block(*path)
    for flag in _flags(dict(_LEAVES)[path]):
        if flag != "--help":
            assert f"'{flag}[" in block, (path, flag)
    assert f":region:({' '.join(_REGIONS)})'" in block
    assert "'(- *)'{-h,--help}" in block


@pytest.mark.parametrize("path", [p for p in _REQUEST_PATHS if len(p) == 2], ids=" ".join)
def test_fish_spec_holds_each_nested_commands_flags_and_region_choices(path):
    group, leaf = path
    condition = f"-n '__fish_seen_subcommand_from {group}; and __fish_seen_subcommand_from {leaf}'"
    lines = [row for row in _SCRIPTS["fish"].splitlines() if condition in row]
    for flag in _flags(dict(_LEAVES)[path]):
        assert any(f"-l {flag[2:]}" in row for row in lines), (path, flag)
    (region,) = [row for row in lines if "-l region" in row]
    assert f"-r -a '{' '.join(_REGIONS)}'" in region


def test_the_top_level_request_commands_complete_their_flags_too():
    assert "'--region[" in _SCRIPTS["zsh"]
    held = _holders("search")
    assert (
        f"-n '__fish_seen_subcommand_from search; and not __fish_seen_subcommand_from {held}' -l region"
        in _SCRIPTS["fish"]
    )
    assert "-l sort-by" in _SCRIPTS["fish"] and "'--sort-by[" in _SCRIPTS["zsh"]


# ============================================================
# FISH -- `search` is both a top-level command and an abs command
# ============================================================

_TOP_ONLY = ("narrator", "publisher", "sort-by", "limit", "page")


def _fish_lines():
    return [
        row for row in _SCRIPTS["fish"].splitlines()
        if row.startswith("complete -c libex-core -n '__fish_seen_subcommand_from")
    ]


@pytest.mark.parametrize("word", ["search", "quick-search"])
def test_fish_top_level_search_flags_are_not_offered_after_abs(word):
    """`abs search` contains the word `search`, so a bare test for it would
    offer the top-level flags (--narrator, --limit...) that abs search does
    not take."""
    condition = (
        f"-n '__fish_seen_subcommand_from {word}; and not __fish_seen_subcommand_from "
        f"{_holders(word)}'"
    )
    top = [row for row in _fish_lines() if f"seen_subcommand_from {word};" in row
           and not any(f"seen_subcommand_from {g};" in row for g in _GROUPS)]
    assert top, word
    for row in top:
        assert condition in row, row
    flags = {m for row in top for m in re.findall(r" -l ([a-z-]+)", row)}
    assert flags == {a[2:] for a in _flags(dict(_LEAVES)[(word,)])}


def test_fish_abs_leaves_keep_their_own_flags_and_never_the_top_level_only_ones():
    for leaf in ("search", "quick-search"):
        condition = (
            f"-n '__fish_seen_subcommand_from abs; and __fish_seen_subcommand_from {leaf}'"
        )
        rows = [row for row in _fish_lines() if condition in row]
        flags = {m for row in rows for m in re.findall(r" -l ([a-z-]+)", row)}
        assert flags == {a[2:] for a in _flags(dict(_LEAVES)[("abs", leaf)])}
        assert not flags & set(_TOP_ONLY)
    assert not [r for r in _fish_lines() if "seen_subcommand_from abs;" in r and " -l limit" in r]


def test_every_top_level_search_row_is_conditioned_on_abs_not_being_named():
    prefixes = ("-n '__fish_seen_subcommand_from search;", "-n '__fish_seen_subcommand_from quick-search;")
    rows = [row for row in _fish_lines() if any(p in row for p in prefixes)]
    assert len(rows) == 13  # search: help, nine options, region; quick-search: help, region
    for row in rows:
        word = "quick-search" if "from quick-search;" in row else "search"
        assert f"; and not __fish_seen_subcommand_from {_holders(word)}'" in row, row
