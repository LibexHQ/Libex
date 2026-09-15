"""
Guards the forcing in tests/conftest.py: a test run must never ship
telemetry, whatever a developer's .env file or already-exported shell
environment holds for AXIOM_TOKEN/AXIOM_DATASET.

Deliberately its own file rather than a case added to test_logging.py: that
file's own autouse fixture clears and rebuilds the "libex" logger's handlers
around every test defined there, which would make a check placed there
assert against a logger that test file had just reset rather than the one
app.main actually built when the session started. This file adds no such
fixture, so it reads the same "libex" logger object and the same
lru_cache'd Settings the rest of the suite runs against, whatever test ran
immediately before it.

The two in-process checks below only prove the forcing worked for THIS run,
under whatever AXIOM_TOKEN/AXIOM_DATASET this process's own environment
happens to hold. If that environment is already blank -- true of this
suite's own CI job, which sets no AXIOM_* and has no .env -- they would keep
passing even with the forcing lines deleted from conftest.py, because
nothing left in the environment would tell them apart from the poisoned
case. They stay because they are still real defence for whatever this
actual run's ambient environment turns out to be -- a future workflow change
that starts exporting a real AXIOM_TOKEN into the tests job would trip them
immediately -- but they are not a regression guard for the forcing lines
themselves. test_conftest_forcing_survives_a_poisoned_ambient_environment
below is that guard: it manufactures its own poisoned environment rather
than depending on the one it happens to run in.
"""

# Standard library
import logging
import subprocess
import sys
from pathlib import Path

# Local
from app.core.config import get_settings
from app.core.logging import _DroppingQueueHandler

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_axiom_handler_attached_to_the_libex_logger():
    logger = logging.getLogger("libex")
    assert not any(isinstance(h, _DroppingQueueHandler) for h in logger.handlers), (
        'an Axiom queue handler is attached to the "libex" logger during a '
        "test run -- tests/conftest.py should force AXIOM_TOKEN and "
        "AXIOM_DATASET to empty before app.main is ever imported"
    )


def test_settings_resolve_an_empty_axiom_token_during_a_test_run():
    assert get_settings().axiom_token == "", (
        "Settings resolved a non-empty axiom_token during a test run -- "
        "tests/conftest.py should force AXIOM_TOKEN to empty before "
        "app.main (and so Settings) is ever imported"
    )


# Stands in for a value a real ambient environment might already hold -- an
# exported shell variable, a CI secret, or a non-empty .env -- so the check
# below does not depend on the environment it happens to run in actually
# being blank. If tests/conftest.py's forcing is doing its job, this value
# never reaches Settings or the "libex" logger no matter what it is.
_POISON_TOKEN = "poison-token-conftest-must-blank"
_POISON_DATASET = "poison-dataset-conftest-must-blank"

# Stands in for the real axiom_py package inside the child process spawned
# by test_conftest_forcing_survives_a_poisoned_ambient_environment. It
# carries none of axiom_py's own dependencies -- no requests, no socket,
# nothing capable of opening a connection -- so Client.ingest_events cannot
# reach a network by construction, whatever settings.axiom_token and
# settings.axiom_dataset resolve to in that process. That is what lets the
# child safely set a real-looking token and dataset and exercise
# app.core.logging.setup_logging()'s Axiom branch end to end -- including
# the handler it attaches and the "Axiom logging enabled" line it emits
# through that handler -- without any risk of a connection attempt to
# Axiom's real endpoint, whether the forcing under test holds or not.
_STUB_AXIOM_PY_SOURCE = '''
class Client:
    def __init__(self, *args, **kwargs):
        pass

    def ingest_events(self, *args, **kwargs):
        return None
'''

# Reproduces exactly the import pytest performs when it loads this test
# suite -- `import tests.conftest`, which runs the forcing lines and then
# imports app.main -- inside a fresh interpreter that starts with the
# poisoned environment above already set, rather than the blank one this
# suite itself runs under. Prints rather than asserts, so a failure reads as
# a value mismatch in the parent's own assertion rather than an opaque
# nonzero exit code.
_CHILD_SCRIPT = """
import logging

import tests.conftest  # noqa: F401 -- the forcing under test runs on import

from app.core.config import get_settings
from app.core.logging import _DroppingQueueHandler

logger = logging.getLogger("libex")
attached = any(isinstance(h, _DroppingQueueHandler) for h in logger.handlers)
token = get_settings().axiom_token

print("HANDLER_ATTACHED=" + str(attached))
print("TOKEN=" + repr(token))
"""


def _run_forcing_check(tmp_path: Path) -> subprocess.CompletedProcess:
    """
    Runs _CHILD_SCRIPT in a subprocess whose environment already holds the
    poisoned AXIOM_TOKEN/AXIOM_DATASET above, with the axiom_py stub ahead of
    the real package on its import path. The child's environment is built
    from nothing rather than inherited, so nothing this process's own
    environment happens to hold reaches it unnoticed. PYTHONPATH carries the
    stub directory first, so `import axiom_py` resolves to it rather than
    the real package in site-packages, and the repository root after it, so
    `import tests.conftest` and the `import app.main` inside it still
    resolve.
    """
    stub_dir = tmp_path / "axiom_stub"
    stub_dir.mkdir()
    (stub_dir / "axiom_py.py").write_text(_STUB_AXIOM_PY_SOURCE)

    env = {
        "AXIOM_TOKEN": _POISON_TOKEN,
        "AXIOM_DATASET": _POISON_DATASET,
        "PYTHONPATH": f"{stub_dir}:{REPO_ROOT}",
    }
    return subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_conftest_forcing_survives_a_poisoned_ambient_environment(tmp_path):
    """
    Whatever a real ambient environment already holds for AXIOM_TOKEN and
    AXIOM_DATASET, tests/conftest.py's forcing must blank both before
    Settings or the "libex" logger ever see them. Simulated here with a
    poisoned value rather than relying on this process's own environment
    happening to already be blank -- a blank ambient environment would pass
    this check whether or not the forcing lines are even still there.
    """
    result = _run_forcing_check(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "HANDLER_ATTACHED=False" in result.stdout, result.stdout
    assert "TOKEN=''" in result.stdout, result.stdout
