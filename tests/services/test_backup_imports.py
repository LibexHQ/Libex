"""
Every module in the backup package imports.

This exists for one class of defect, and it is a class rather than an
instance. The Destination seam names a method `list()`, so inside a class
body every annotation evaluated after that `def` sees the method rather than
the builtin, and `-> list[RemoteArtifact]` on any later method raises
TypeError while the module is being imported. Ruff does not see it, review
did not see it, and nothing in the suite would see it either unless
something imports the module -- which, for a package the API deliberately
never imports, nothing otherwise does.

The walk is deliberately blunt: import everything under the package and
require it to succeed. Every transport added to this seam inherits the same
trap, and a test naming ftps.py would not cover the next one.

Run in a child interpreter on purpose. Half of these modules are already in
sys.modules by the time any test runs, so an import statement here would
find them cached and evaluate no annotation at all -- the check has to
happen in a process where nothing has been imported yet.
"""

# Standard library
import subprocess
import sys
from pathlib import Path

# Third party
import pytest


PACKAGE = "app.services.backup"

# The modules that must be reachable. Named so that the walk failing to
# find anything -- a renamed package, a missing __init__ -- fails loudly
# rather than passing over an empty set.
EXPECTED_MODULES = {
    f"{PACKAGE}.artifact",
    f"{PACKAGE}.destinations",
    f"{PACKAGE}.destinations.base",
    f"{PACKAGE}.destinations.ftps",
    f"{PACKAGE}.dump",
    f"{PACKAGE}.retention",
    f"{PACKAGE}.runner",
    f"{PACKAGE}.schedule",
}

_WALK = f"""
import importlib
import pkgutil
import sys

import {PACKAGE} as package

found = []
for info in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + "."):
    importlib.import_module(info.name)
    found.append(info.name)
print("\\n".join(sorted(found)))
print("app.db.session in sys.modules:", "app.db.session" in sys.modules)
"""


@pytest.fixture(scope="module")
def walk():
    """
    The child runs with the repository root as its working directory, so
    `app` is importable there without depending on where pytest was
    invoked from -- `python -c` puts the working directory on sys.path and
    nothing else.
    """
    return subprocess.run(
        [sys.executable, "-c", _WALK],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=Path(__file__).resolve().parents[2],
    )


def test_importing_every_module_in_the_package_succeeds(walk):
    assert walk.returncode == 0, walk.stderr


def test_the_walk_actually_reached_every_module(walk):
    """A walk that finds nothing returns 0 too. This is what stops the test
    above from being satisfied by an empty package."""
    found = {line for line in walk.stdout.splitlines() if line.startswith(f"{PACKAGE}.")}

    assert EXPECTED_MODULES <= found


def test_a_method_named_list_shadows_the_builtin_for_later_annotations():
    """
    The language behaviour the walk is there to catch, stated once so the
    walk reads as a guard rather than as ceremony. This is evaluated at
    class creation, which is import time for a module -- there is no call
    and no test to fail, only a package that will not load.
    """
    body = (
        "class Seam:\n"
        "    async def list(self):\n"
        "        ...\n"
        "    def later(self) -> list[int]:\n"
        "        ...\n"
    )

    with pytest.raises(TypeError):
        exec(body, {})


def test_the_module_scope_alias_is_what_later_annotations_use():
    """The fix, and why it works: a class namespace is not in the lexical
    scope of the methods defined in it, so an alias bound at module scope is
    unshadowed even inside the class body."""
    body = (
        "_Ints = list[int]\n"
        "class Seam:\n"
        "    async def list(self):\n"
        "        ...\n"
        "    def later(self) -> _Ints:\n"
        "        ...\n"
    )
    namespace = {}

    exec(body, namespace)

    assert namespace["Seam"].later.__annotations__["return"] == list[int]


def test_importing_the_package_does_not_build_a_connection_pool(walk):
    """
    app.db.session creates a SQLAlchemy engine at import with pool_size=10
    and max_overflow=10, against a max_connections budget already fully
    spoken for by six API workers and the seeder. The backup container would
    reserve twenty connections it never opens -- pg_dump brings its own
    libpq connection and the ORM is never used here.

    Asserted in the same child as the walk, because it is only true of a
    process that imported nothing else: in the test process app.db.session
    is loaded long before this file runs.
    """
    assert "app.db.session in sys.modules: False" in walk.stdout
