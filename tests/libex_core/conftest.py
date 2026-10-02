"""
Fixtures for the libex-core command line tests. Nothing here is autouse, so
the library's own tests in this directory are unaffected.
"""

import sys
from dataclasses import dataclass

import pytest

from tests.libex_core._cli_support import AMBIENT_VARIABLES


@dataclass(frozen=True)
class CliResult:
    code: int
    stdout: bytes
    stderr: bytes

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8")


@pytest.fixture
def cli_main_module():
    """The module, not the name: libex_core.cli re-exports the function as
    `main`, so a dotted string target would patch the wrong object."""
    import libex_core.cli.main  # noqa: F401

    return sys.modules["libex_core.cli.main"]


@pytest.fixture
def run_cli(monkeypatch, capsysbinary):
    """Runs main() in this process under an environment holding only what
    the call names, and returns the exit status with both streams as bytes."""
    from libex_core.cli.main import main

    for name in AMBIENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    def run(argv, env=None):
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)
        capsysbinary.readouterr()
        code = main(argv)
        captured = capsysbinary.readouterr()
        return CliResult(int(code), captured.out, captured.err)

    return run


@pytest.fixture
def hosted(client):
    """ask(get, path, params): a hosted route answered from `get` alone. The
    imports are inside so the tests here that never ask a route do not load
    the application."""
    from tests.libex_core._hosted_support import hosted_asker

    with hosted_asker(client) as ask:
        yield ask
