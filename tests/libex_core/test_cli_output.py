"""
What the libex-core command line writes and where: JSON or nothing on
standard output, logs and the error line on standard error, no color, control
characters escaped, no handler left behind, and the signal-shaped exits.
"""

import argparse
import io
import json
import logging
import sys

import pytest

from libex_core.cli import exit_codes, output
from libex_core.cli.environment import ALLOW_DIRECT_EGRESS_VARIABLE, PROXY_URL_VARIABLE
from libex_core.cli.exit_codes import ExitCode, Failure
from libex_core.cli.output import LOGGER_NAME, escape_controls, error_line


@pytest.fixture
def with_command(monkeypatch, cli_main_module):
    """Grafts a `probe` command onto the real parser, so the output and exit
    paths can be driven by a handler that does what the test needs. Patched
    through the module object: libex_core.cli.main also names the function."""
    real_build_parser = cli_main_module.build_parser

    def install(handler):
        def build_parser():
            parser = real_build_parser()
            container = next(
                a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
            )
            container.add_parser("probe").set_defaults(handler=handler)
            return parser

        monkeypatch.setattr(cli_main_module, "build_parser", build_parser)

    return install


_CONFIG_ENV = {ALLOW_DIRECT_EGRESS_VARIABLE: "1"}


# ============================================================
# STREAMS
# ============================================================

def test_success_is_valid_json_on_stdout_and_nothing_on_stderr(run_cli):
    result = run_cli(["config"], _CONFIG_ENV)
    assert result.code == 0
    assert json.loads(result.out) == {"transport": {"mode": "direct", "host": None}}
    assert result.stderr == b""
    assert result.out.endswith("\n") and "\r" not in result.out


@pytest.mark.parametrize(
    ("argv", "env", "code"),
    [
        (["config"], {}, 5),
        (["config"], {ALLOW_DIRECT_EGRESS_VARIABLE: "garbage"}, 5),
        (["config"], {PROXY_URL_VARIABLE: "not a url"}, 5),
        ([], {}, 2),
        (["-q", "-v", "config"], {}, 2),
        (["completion", "ksh"], {}, 2),
    ],
    ids=["no-env", "bad-switch", "bad-proxy", "no-args", "q-and-v", "bad-shell"],
)
def test_an_error_leaves_standard_output_empty(run_cli, argv, env, code):
    result = run_cli(argv, env)
    assert result.code == code
    assert result.stdout == b""
    assert result.stderr != b""


def test_an_unexpected_failure_leaves_standard_output_empty(run_cli, with_command):
    def boom(args):
        raise RuntimeError("secret detail")

    with_command(boom)
    result = run_cli(["probe"])
    assert result.code == ExitCode.ERROR
    assert result.stdout == b""
    assert result.err == (
        "libex-core: error: unexpected RuntimeError; rerun with -vv for a "
        "traceback (code: unexpected_error)\n"
    )
    assert "secret detail" not in result.err


def test_an_unexpected_failures_traceback_is_on_stderr_at_vv_only(run_cli, with_command):
    def boom(args):
        raise RuntimeError("detail for the traceback")

    with_command(boom)
    quiet = run_cli(["probe"])
    loud = run_cli(["-vv", "probe"])
    assert "Traceback" not in quiet.err
    assert "Traceback" in loud.err and "detail for the traceback" in loud.err
    assert loud.stdout == b"" and quiet.stdout == b""


def test_log_lines_go_to_stderr_and_never_to_stdout(run_cli, with_command):
    def chatty(args):
        log = logging.getLogger(LOGGER_NAME)
        log.debug("a debug line")
        log.info("an info line")
        log.warning("a warning line")
        output.emit_json({"ok": True})
        return ExitCode.OK

    with_command(chatty)
    result = run_cli(["-vv", "probe"])
    assert result.code == 0
    assert json.loads(result.out) == {"ok": True}
    assert result.err.splitlines() == [
        "libex-core: DEBUG: a debug line",
        "libex-core: INFO: an info line",
        "libex-core: WARNING: a warning line",
    ]


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], ["WARNING"]),
        (["-v"], ["INFO", "WARNING"]),
        (["-vv"], ["DEBUG", "INFO", "WARNING"]),
        (["-vvv"], ["DEBUG", "INFO", "WARNING"]),
        (["-q"], []),
    ],
)
def test_verbosity_selects_which_levels_appear(run_cli, with_command, flags, expected):
    def chatty(args):
        log = logging.getLogger(LOGGER_NAME)
        log.debug("d")
        log.info("i")
        log.warning("w")
        return ExitCode.OK

    with_command(chatty)
    result = run_cli([*flags, "probe"])
    levels = [line.split(": ")[1] for line in result.err.splitlines()]
    assert levels == expected
    assert result.stdout == b""


def test_quiet_still_prints_the_error_line(run_cli, with_command):
    with_command(lambda args: (_ for _ in ()).throw(RuntimeError("x")))
    result = run_cli(["-q", "probe"])
    assert result.code == 1
    assert result.err.startswith("libex-core: error: ")


def test_emit_json_is_compact_when_piped_and_indented_on_a_terminal(monkeypatch, capsysbinary):
    output.emit_json({"a": [1, 2]})
    assert capsysbinary.readouterr().out == b'{"a":[1,2]}\n'

    class Terminal:
        def __init__(self):
            self.buffer = io.BytesIO()

        def isatty(self):
            return True

    terminal = Terminal()
    monkeypatch.setattr(sys, "stdout", terminal)
    output.emit_json({"a": [1, 2]})
    assert terminal.buffer.getvalue() == b'{\n  "a": [\n    1,\n    2\n  ]\n}\n'


def test_emit_json_dumps_a_model_by_its_aliases(capsysbinary):
    class Model:
        def model_dump(self, mode, by_alias):
            assert mode == "json" and by_alias is True
            return {"seriesName": "x"}

    output.emit_json({"item": Model(), "items": [Model()]})
    assert json.loads(capsysbinary.readouterr().out) == {
        "item": {"seriesName": "x"},
        "items": [{"seriesName": "x"}],
    }


# ============================================================
# NO COLOR
# ============================================================

@pytest.mark.parametrize("environment", [{}, {"NO_COLOR": "1"}, {"FORCE_COLOR": "1"}, {"NO_COLOR": "", "FORCE_COLOR": "1"}])
@pytest.mark.parametrize(
    "argv",
    [["--help"], ["config", "--help"], ["--version"], ["config"], ["-vv", "config"], [], ["-q", "-v"], ["completion", "bash"]],
    ids=lambda argv: " ".join(argv) or "no arguments",
)
def test_neither_stream_ever_carries_an_escape_sequence(run_cli, argv, environment):
    result = run_cli(argv, {**_CONFIG_ENV, **environment})
    assert b"\x1b" not in result.stdout
    assert b"\x1b" not in result.stderr


def test_a_failure_traceback_carries_no_escape_sequence(run_cli, with_command):
    with_command(lambda args: (_ for _ in ()).throw(ValueError("x")))
    result = run_cli(["-vv", "probe"], {"FORCE_COLOR": "1"})
    assert result.stderr
    assert b"\x1b" not in result.stderr


# ============================================================
# ESCAPING
# ============================================================

_C0 = [chr(c) for c in range(0x00, 0x20)]
_C1 = [chr(c) for c in range(0x7F, 0xA0)]


@pytest.mark.parametrize("char", _C0 + _C1, ids=lambda c: f"U+{ord(c):04X}")
def test_every_control_character_is_escaped(char):
    escaped = escape_controls(f"a{char}b")
    assert escaped == f"a\\x{ord(char):02x}b"
    assert char not in escaped


def test_ordinary_text_passes_through_the_escaper_unchanged():
    text = "plain text, punctuation (ok) and unicode: café ブラ"
    assert escape_controls(text) == text


def test_the_error_line_is_one_line_whatever_the_message_holds():
    line = error_line("first\nforged: error\r\x1b[31mred\x1b[0m\x85\x9b", "code_x")
    assert "\n" not in line and "\r" not in line and "\x1b" not in line
    assert not any(0x7F <= ord(c) <= 0x9F or ord(c) < 0x20 for c in line)
    assert line.startswith("libex-core: error: first\\x0aforged")
    assert line.endswith("(code: code_x)")


def test_a_message_with_controls_reaches_stderr_escaped_through_main(
    run_cli, with_command, monkeypatch, cli_main_module
):
    hostile = "line one\nlibex-core: error: forged\x1b]0;title\x07\x9b31m"
    monkeypatch.setattr(
        cli_main_module,
        "classify",
        lambda exc: Failure(ExitCode.ERROR, "unexpected_error", hostile),
    )
    with_command(lambda args: (_ for _ in ()).throw(RuntimeError("x")))
    result = run_cli(["probe"])
    assert result.code == 1
    assert result.stdout == b""
    assert len(result.err.splitlines()) == 1
    assert b"\x1b" not in result.stderr and b"\x07" not in result.stderr
    assert "\\x0a" in result.err and "\\x1b" in result.err and "\\x9b" in result.err


def test_a_log_message_with_controls_is_escaped_on_stderr(run_cli, with_command):
    def chatty(args):
        logging.getLogger(LOGGER_NAME).warning("one\nlibex-core: error: forged\x1b[2J")
        return ExitCode.OK

    with_command(chatty)
    result = run_cli(["probe"])
    assert len(result.err.splitlines()) == 1
    assert b"\x1b" not in result.stderr
    assert "\\x0a" in result.err


# ============================================================
# HANDLERS AND LEVELS DO NOT ACCUMULATE
# ============================================================

def test_repeated_calls_leave_no_handler_behind(run_cli, with_command):
    with_command(lambda args: ExitCode.OK)
    library = logging.getLogger(LOGGER_NAME)
    root = logging.getLogger()
    library_before = list(library.handlers)
    root_before = list(root.handlers)
    level_before = library.level
    for _ in range(5):
        for flags in ([], ["-vv"], ["-q"]):
            assert run_cli([*flags, "probe"]).code == 0
    assert library.handlers == library_before
    assert root.handlers == root_before
    assert library.level == level_before


def test_a_failed_run_also_detaches_its_handler(run_cli, with_command):
    with_command(lambda args: (_ for _ in ()).throw(RuntimeError("x")))
    library = logging.getLogger(LOGGER_NAME)
    before = list(library.handlers)
    for _ in range(3):
        assert run_cli(["-vv", "probe"]).code == 1
    assert library.handlers == before


def test_each_run_logs_each_line_once(run_cli, with_command):
    def chatty(args):
        logging.getLogger(LOGGER_NAME).warning("once")
        return ExitCode.OK

    with_command(chatty)
    for _ in range(4):
        last = run_cli(["probe"])
    assert last.err.count("once") == 1


def test_the_library_root_logger_is_never_configured(run_cli, with_command):
    with_command(lambda args: ExitCode.OK)
    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    run_cli(["-vv", "probe"])
    assert (list(root.handlers), root.level) == before


# ============================================================
# SIGNAL-SHAPED EXITS
# ============================================================

def test_a_broken_pipe_exits_141_and_prints_nothing_more(run_cli, with_command):
    def closed(args):
        raise BrokenPipeError

    with_command(closed)
    result = run_cli(["probe"])
    assert result.code == 141 == ExitCode.BROKEN_PIPE
    assert result.stderr == b""


def test_a_keyboard_interrupt_exits_130(run_cli, with_command):
    def interrupted(args):
        raise KeyboardInterrupt

    with_command(interrupted)
    result = run_cli(["probe"])
    assert result.code == 130 == ExitCode.INTERRUPTED
    assert result.stdout == b""


def test_an_interrupt_still_detaches_logging(run_cli, with_command):
    with_command(lambda args: (_ for _ in ()).throw(KeyboardInterrupt()))
    library = logging.getLogger(LOGGER_NAME)
    before = list(library.handlers)
    run_cli(["probe"])
    assert library.handlers == before


def test_a_handlers_return_value_becomes_the_exit_status(run_cli, with_command):
    with_command(lambda args: ExitCode.NOT_FOUND)
    assert run_cli(["probe"]).code == 3


# ============================================================
# EXIT CODE TABLE
# ============================================================

def test_exit_status_numbers_are_the_published_ones():
    assert {code.name: int(code) for code in ExitCode} == {
        "OK": 0,
        "ERROR": 1,
        "USAGE": 2,
        "NOT_FOUND": 3,
        "UPSTREAM_UNAVAILABLE": 4,
        "CONFIG": 5,
        "INTERRUPTED": 130,
        "BROKEN_PIPE": 141,
    }


def test_every_exit_status_is_described_for_the_man_page():
    assert set(exit_codes.DESCRIPTIONS) == set(ExitCode)
