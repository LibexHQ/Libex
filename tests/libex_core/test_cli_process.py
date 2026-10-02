"""
The libex-core command line as a real process: `python -m libex_core` in an
environment holding nothing, and the broken-pipe path against a reader that
has really gone away.
"""

import json
import subprocess
import sys

import libex_core
from tests.libex_core._cli_support import clean_env, run_python


def test_dash_m_version_exits_zero_in_a_clean_environment(tmp_path):
    result = run_python(["-m", "libex_core", "--version"], env=clean_env(), cwd=tmp_path, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"libex-core {libex_core.__version__}\n"
    assert result.stderr == ""


def test_dash_m_with_no_arguments_exits_two_with_usage(tmp_path):
    result = run_python(["-m", "libex_core"], env=clean_env(), cwd=tmp_path, text=True)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("usage: libex-core")


def test_dash_m_config_reports_a_config_error_without_an_environment(tmp_path):
    result = run_python(["-m", "libex_core", "config"], env=clean_env(), cwd=tmp_path, text=True)
    assert result.returncode == 5
    assert result.stdout == ""
    assert result.stderr.endswith("(code: config_error)\n")


def test_dash_m_config_succeeds_with_a_proxy(tmp_path):
    result = run_python(
        ["-m", "libex_core", "config"],
        env=clean_env(LIBEX_CORE_PROXY_URL="http://u:pw@proxy.example.net:8080"),
        cwd=tmp_path,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"transport": {"mode": "proxy", "host": "proxy.example.net"}}
    assert "pw" not in result.stdout + result.stderr


def test_the_program_name_is_fixed_when_run_as_a_module(tmp_path):
    result = run_python(["-m", "libex_core", "--help"], env=clean_env(), cwd=tmp_path, text=True)
    assert result.stdout.startswith("usage: libex-core ")
    assert "__main__" not in result.stdout


def test_a_reader_that_has_gone_away_ends_the_process_with_141(tmp_path):
    """The read end is closed before the child can write, so its flush at the
    end of the run meets a broken pipe for real, with no patching."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "libex_core", "completion", "bash"],
        cwd=tmp_path,
        env=clean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdout.close()
    stderr = proc.stderr.read()
    proc.stderr.close()
    assert proc.wait(timeout=30) == 141
    assert b"Traceback" not in stderr and b"Exception ignored" not in stderr
