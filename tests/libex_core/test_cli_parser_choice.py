"""
The CLI Parser's refusal of a bad choice is fixed text.

The Parser overrides argparse's private `_check_value`, which quotes the
offending value, so a mistyped secret never reaches the screen or a log. The
override leans on a private name, so these tests are what notices if a Python
release stops calling it.
"""

# Standard library
import argparse

# Third party
import pytest

# Local
from libex_core.cli._args import Parser

PLANTED = "hunter2-planted-secret"


def _parser() -> Parser:
    parser = Parser(prog="libex-core", add_help=False)
    parser.add_argument("--region", choices=("us", "uk"))
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("book")
    return parser


def _refusal(argv, capsys) -> str:
    with pytest.raises(SystemExit) as raised:
        _parser().parse_args(argv)
    assert raised.value.code == 2
    return capsys.readouterr().err


def test_a_bad_option_choice_is_refused_with_fixed_text(capsys):
    err = _refusal(["--region", PLANTED], capsys)
    assert "invalid choice (choose from us, uk)" in err
    assert PLANTED not in err


def test_a_bad_subcommand_is_refused_with_fixed_text(capsys):
    err = _refusal([PLANTED], capsys)
    assert "invalid choice (choose from" in err
    assert PLANTED not in err


def test_a_valid_choice_is_accepted():
    assert _parser().parse_args(["--region", "uk"]).region == "uk"


def test_the_override_is_what_keeps_the_value_out():
    """argparse's own refusal quotes the value. If it ever stops doing so,
    the override is no longer doing the work this file says it does."""
    plain = argparse.ArgumentParser(prog="libex-core", add_help=False)
    plain.add_argument("--region", choices=("us", "uk"))
    with pytest.raises(argparse.ArgumentError) as raised:
        plain._check_value(plain._actions[-1], PLANTED)
    assert PLANTED in str(raised.value)
