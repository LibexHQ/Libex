"""
How an exception becomes an exit status and an error line: the table from the
library's ErrorCode vocabulary, the two codes that belong to this tool alone,
and what happens to a code the table has not met.
"""

import enum

import pytest

from libex_core.cli import exit_codes
from libex_core.cli.environment import ConfigError
from libex_core.cli.exit_codes import ExitCode, classify
from libex_core.exceptions import (
    AudibleAPIException,
    ErrorCode,
    LibexException,
    NotFoundException,
)

_EXPECTED = {
    ErrorCode.INVALID_REQUEST: ExitCode.USAGE,
    ErrorCode.NOT_ON_AUDIBLE: ExitCode.NOT_FOUND,
    ErrorCode.NOT_IN_LIBEX: ExitCode.NOT_FOUND,
    ErrorCode.WITHHELD: ExitCode.NOT_FOUND,
    ErrorCode.UPSTREAM_UNAVAILABLE: ExitCode.UPSTREAM_UNAVAILABLE,
}


def test_every_error_code_has_an_entry_in_the_table():
    """A code added to the vocabulary without a decision here would fall to 1
    and look like a bug in the tool rather than a gap in this table."""
    assert set(exit_codes._BY_ERROR_CODE) == set(ErrorCode)


def test_the_table_is_the_expected_one():
    assert exit_codes._BY_ERROR_CODE == _EXPECTED
    assert len(_EXPECTED) == len(ErrorCode)


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda c: c.value)
def test_each_error_code_maps_to_its_exit_status(code):
    failure = classify(LibexException("m", code=code))
    assert failure.exit_code == _EXPECTED[code]
    assert failure.code == code.value


def test_the_default_codes_of_the_library_exceptions_map_as_documented():
    assert classify(NotFoundException()).exit_code == ExitCode.NOT_FOUND
    assert classify(AudibleAPIException()).exit_code == ExitCode.UPSTREAM_UNAVAILABLE
    assert classify(LibexException("x")).exit_code == ExitCode.UPSTREAM_UNAVAILABLE


def test_a_config_error_is_config_with_its_own_code():
    failure = classify(ConfigError("fixed text"))
    assert (failure.exit_code, failure.code, failure.message) == (
        ExitCode.CONFIG,
        "config_error",
        "fixed text",
    )
    assert "config_error" not in {c.value for c in ErrorCode}


def test_a_config_error_is_checked_before_a_library_exception():
    class Both(ConfigError, LibexException):
        pass

    exc = Both("x")
    exc.code = ErrorCode.INVALID_REQUEST
    assert classify(exc).exit_code == ExitCode.CONFIG


def test_an_unmapped_code_falls_to_one_and_keeps_its_value():
    class NewCode(enum.StrEnum):
        BRAND_NEW = "brand_new"

    exc = LibexException("something new")
    exc.code = NewCode.BRAND_NEW
    failure = classify(exc)
    assert failure.exit_code == ExitCode.ERROR
    assert failure.code == "brand_new"
    assert failure.message == "something new"


def test_a_table_without_an_entry_falls_to_one(monkeypatch):
    monkeypatch.delitem(exit_codes._BY_ERROR_CODE, ErrorCode.WITHHELD)
    assert classify(LibexException("m", code=ErrorCode.WITHHELD)).exit_code == ExitCode.ERROR


def test_an_unmapped_exception_prints_its_class_never_its_text():
    failure = classify(RuntimeError("secret detail"))
    assert (failure.exit_code, failure.code) == (ExitCode.ERROR, "unexpected_error")
    assert "secret detail" not in failure.message
    assert "RuntimeError" in failure.message


def test_unexpected_error_is_not_in_the_library_vocabulary():
    assert "unexpected_error" not in {c.value for c in ErrorCode}


# ============================================================
# Through main(): the status, the message and the suffix together
# ============================================================

@pytest.fixture
def raising(monkeypatch, cli_main_module):
    import argparse

    real = cli_main_module.build_parser

    def install(exc):
        def build_parser():
            parser = real()
            container = next(
                a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
            )

            def handler(args):
                raise exc

            container.add_parser("probe").set_defaults(handler=handler)
            return parser

        monkeypatch.setattr(cli_main_module, "build_parser", build_parser)

    return install


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda c: c.value)
def test_the_error_line_carries_the_message_and_the_code_value(run_cli, raising, code):
    raising(LibexException("it went wrong", code=code))
    result = run_cli(["probe"])
    assert result.code == _EXPECTED[code]
    assert result.stdout == b""
    assert result.err == f"libex-core: error: it went wrong (code: {code.value})\n"


def test_the_message_of_a_library_exception_is_escaped(run_cli, raising):
    raising(
        LibexException(
            "first\nlibex-core: error: forged\x1b[31m\x85\x9b",
            code=ErrorCode.NOT_ON_AUDIBLE,
        )
    )
    result = run_cli(["probe"])
    assert result.code == 3
    assert len(result.err.splitlines()) == 1
    assert b"\x1b" not in result.stderr
    assert "first\\x0alibex-core: error: forged\\x1b[31m\\x85\\x9b" in result.err
    assert result.err.endswith("(code: not_on_audible)\n")


def test_the_status_is_the_numbers_a_script_branches_on(run_cli, raising):
    seen = {}
    for code in ErrorCode:
        raising(LibexException("m", code=code))
        seen[code.value] = run_cli(["probe"]).code
    assert seen == {
        "invalid_request": 2,
        "not_on_audible": 3,
        "not_in_libex": 3,
        "withheld": 3,
        "upstream_unavailable": 4,
    }
