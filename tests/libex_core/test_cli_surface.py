"""
The libex-core command line's surface: help, version, usage errors, and the
completion scripts, including the proof that the committed man page and
completions are what the parser renders today.
"""

import locale

import pytest

import libex_core
from libex_core.cli import _render
from libex_core.cli.parser import build_parser
from tests.libex_core._cli_support import DATA_DIR, clean_env, run_python, walk_parsers

_PATHS = [path for path, _ in walk_parsers(build_parser())]


def _committed(relative: str) -> bytes:
    return (DATA_DIR / relative).read_bytes()


_COMMITTED_FOR_SHELL = {
    "bash": _render.BASH_PATH,
    "zsh": _render.ZSH_PATH,
    "fish": _render.FISH_PATH,
}


# ============================================================
# HELP
# ============================================================

def test_the_walk_found_the_root_and_every_command():
    """A walk that found nothing would pass every help test for free."""
    assert () in _PATHS
    assert len(_PATHS) > 1
    assert ("completion",) in _PATHS and ("config",) in _PATHS


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("path", _PATHS, ids=lambda p: " ".join(p) or "root")
def test_help_exits_zero_with_usage_on_stdout_only(run_cli, path, flag):
    result = run_cli([*path, flag])
    assert result.code == 0
    assert result.out.startswith("usage: libex-core")
    assert result.stderr == b""


def test_help_lists_every_command(run_cli):
    out = run_cli(["--help"]).out
    for path in _PATHS:
        if path:
            assert path[0] in out


# ============================================================
# VERSION
# ============================================================

def test_version_prints_the_package_version_on_stdout(run_cli):
    result = run_cli(["--version"])
    assert result.code == 0
    assert result.out == f"libex-core {libex_core.__version__}\n"
    assert result.stderr == b""


# ============================================================
# USAGE ERRORS
# ============================================================

@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--no-such-option", "config"],
        ["no-such-command"],
        ["completion"],
        ["completion", "ksh"],
        ["completion", "bash", "extra"],
        ["config", "extra"],
        ["config", "--no-such-option"],
        ["-q", "-v", "config"],
        ["-v", "-q", "config"],
        ["-qv", "config"],
        ["--quiet", "--verbose", "config"],
        ["config", "--proxy-url", "http://x:1"],
    ],
    ids=lambda argv: " ".join(argv) or "no arguments",
)
def test_a_usage_error_exits_two_with_nothing_on_stdout(run_cli, argv):
    result = run_cli(argv)
    assert result.code == 2
    assert result.stdout == b""
    assert result.err.startswith("usage: libex-core")
    assert "error:" in result.err


def test_quiet_and_verbose_are_reported_as_a_conflict(run_cli):
    assert "not allowed with" in run_cli(["-q", "-v", "config"]).err


def test_an_unknown_shell_lists_the_real_ones(run_cli):
    err = run_cli(["completion", "ksh"]).err
    for shell in _COMMITTED_FOR_SHELL:
        assert shell in err


# ============================================================
# COMPLETION
# ============================================================

@pytest.mark.parametrize("shell", sorted(_COMMITTED_FOR_SHELL))
def test_completion_is_the_committed_file_byte_for_byte(run_cli, shell):
    result = run_cli(["completion", shell])
    assert result.code == 0
    assert result.stderr == b""
    assert result.stdout == _committed(_COMMITTED_FOR_SHELL[shell])
    assert result.stdout, "an empty script would equal an empty file"


@pytest.mark.parametrize("shell", sorted(_COMMITTED_FOR_SHELL))
def test_completion_is_byte_identical_across_runs(run_cli, shell):
    first = run_cli(["completion", shell]).stdout
    second = run_cli(["completion", shell]).stdout
    assert first == second


@pytest.mark.parametrize("shell", sorted(_COMMITTED_FOR_SHELL))
def test_completion_ends_in_one_newline_and_has_no_carriage_return(run_cli, shell):
    out = run_cli(["completion", shell]).stdout
    assert out.endswith(b"\n") and not out.endswith(b"\n\n")
    assert b"\r" not in out


def test_completion_names_every_command_the_parser_has(run_cli):
    for shell in _COMMITTED_FOR_SHELL:
        out = run_cli(["completion", shell]).out
        for path in _PATHS:
            if path:
                assert path[0] in out, (shell, path)


# ============================================================
# DRIFT -- the committed files are what the parser renders
# ============================================================

def test_committed_files_match_a_fresh_render_in_process():
    rendered = _render.artefacts(build_parser())
    assert set(rendered) == {
        _render.MAN_PATH,
        *_COMMITTED_FOR_SHELL.values(),
    }
    for relative, text in rendered.items():
        assert text.encode("utf-8") == _committed(relative), relative


def _locale_available(name: str) -> bool:
    previous = locale.setlocale(locale.LC_CTYPE)
    try:
        locale.setlocale(locale.LC_CTYPE, name)
        return True
    except locale.Error:
        return False
    finally:
        locale.setlocale(locale.LC_CTYPE, previous)


# Only the locales this machine has: naming one it lacks would make glibc fall
# back to C silently and the run would claim to cover something it did not.
_LOCALES = [
    name
    for name in ("C", "C.UTF-8", "de_DE.UTF-8", "tr_TR.UTF-8", "ja_JP.UTF-8")
    if _locale_available(name)
]


@pytest.mark.parametrize("columns", ["20", "500"])
@pytest.mark.parametrize("lc_all", _LOCALES)
def test_a_render_in_another_locale_and_width_matches_the_committed_files(
    tmp_path, lc_all, columns
):
    """The generator is a pure function of the parser and this package's own
    strings. argparse's formatter is the part that is not: it reads COLUMNS
    and translates its own wording through gettext, so the render must not
    pass through it."""
    result = run_python(
        ["-m", "libex_core.cli._render", "--write", str(tmp_path)],
        env=clean_env(LC_ALL=lc_all, LANG=lc_all, LANGUAGE=lc_all[:2], COLUMNS=columns),
        cwd=tmp_path,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for relative in (_render.MAN_PATH, *_COMMITTED_FOR_SHELL.values()):
        assert (tmp_path / relative).read_bytes() == _committed(relative), relative


def test_the_render_writes_exactly_four_files(tmp_path):
    result = run_python(
        ["-m", "libex_core.cli._render", "--write", str(tmp_path)],
        env=clean_env(),
        cwd=tmp_path,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    written = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert written == {_render.MAN_PATH, *_COMMITTED_FOR_SHELL.values()}
