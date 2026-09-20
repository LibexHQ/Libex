"""
libex_core carries no dependency on the application it is embedded in.

The package's own docstring makes the claim: no database, no cache, no web
framework, and no environment configuration of its own, so it can be
embedded elsewhere without dragging any of that in behind it. Nothing
enforces that claim at import time -- a stray `import app.core.config` or
`from sqlalchemy import ...` inside libex_core would work today, because the
test suite that exercises libex_core's own functions already has app and
its dependencies loaded into sys.modules by the time any of those functions
run. The only way to see the leak is to check in a process that has
imported nothing else, which is what the subprocess below does, and to read
the source text directly for the one import that would cause it, which is
what the AST walk does.
"""

# Standard library
import ast
import pkgutil
import subprocess
import sys
from pathlib import Path

# Local
import libex_core as _libex_core_package

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIBEX_CORE_DIR = REPO_ROOT / "libex_core"

# What a genuinely standalone libex_core must never pull in. Each of these
# is a real dependency of the hosted application, not of the metadata-
# normalisation logic itself -- fastapi/starlette (the web layer), sqlalchemy/
# asyncpg (the database), pydantic_settings (env-var configuration),
# axiom_py/pythonjsonlogger (the app's own logging pipeline), and app itself.
_FORBIDDEN_MODULES = (
    "app",
    "fastapi",
    "starlette",
    "sqlalchemy",
    "asyncpg",
    "pydantic_settings",
    "axiom_py",
    "pythonjsonlogger",
)

# Discovered here, in the parent process, purely to give the walk below a
# non-empty expectation to be checked against -- the parent has already
# imported half the forbidden list itself by the time this module loads, so
# it is never used to test isolation, only to know what "found everything"
# should look like.
_EXPECTED_MODULES = {_libex_core_package.__name__} | {
    info.name
    for info in pkgutil.walk_packages(
        _libex_core_package.__path__, prefix=_libex_core_package.__name__ + "."
    )
}

# Run inside a child process against libex_core's real __path__, with the
# forbidden list interpolated in from the same constant this module asserts
# against, so the two can never drift apart from each other.
_CHILD_SCRIPT = """
import importlib
import pkgutil
import sys

import libex_core as package

found = [package.__name__]
for info in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + "."):
    importlib.import_module(info.name)
    found.append(info.name)

forbidden = {forbidden!r}
leaked = [name for name in forbidden if name in sys.modules]

print("MODULES:" + ",".join(sorted(found)))
print("LEAKED:" + ",".join(leaked))
""".format(forbidden=_FORBIDDEN_MODULES)


def _isolated_child_env() -> dict:
    """
    Builds the child's environment from nothing rather than from this
    process's own environment with something filtered out, so no variable
    this process happens to hold -- a database password, a seed secret, a
    proxy URL with embedded credentials, or anything else -- reaches the
    child unless it is named here. PYTHONPATH is the only entry: it is set
    to the repository root rather than relying on cwd, because the child's
    working directory is deliberately tmp_path, not the repo -- `python -c`
    only puts the working directory on sys.path, and here that would find
    nothing. Neither PATH, HOME, nor a locale variable is included, because
    none is needed -- the interpreter is invoked by its resolved absolute
    path rather than looked up on PATH, and nothing under libex_core reads
    HOME, consults a locale, or shells out to another process.
    """
    return {"PYTHONPATH": str(REPO_ROOT)}


def _run_isolated_import(tmp_path) -> subprocess.CompletedProcess:
    """Runs the child script from an empty working directory with the
    minimal environment above, so the only way libex_core can be found is
    through the PYTHONPATH set for it, not through anything ambient."""
    return subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        cwd=tmp_path,
        env=_isolated_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )


# ============================================================
# (a) SUBPROCESS -- nothing forbidden ends up in sys.modules
# ============================================================

def test_importing_every_libex_core_module_pulls_in_none_of_the_forbidden_set(tmp_path):
    result = _run_isolated_import(tmp_path)

    assert result.returncode == 0, result.stderr

    leaked_line = next(
        (line for line in result.stdout.splitlines() if line.startswith("LEAKED:")), ""
    )
    leaked = leaked_line[len("LEAKED:"):]
    assert leaked == "", f"libex_core import pulled in: {leaked}"


def test_the_isolated_import_actually_walked_every_libex_core_module(tmp_path):
    """A walk that silently found nothing would satisfy the assertion above
    for the wrong reason. This is the same non-vacuousness check as
    tests/services/test_backup_imports.py, applied to libex_core."""
    result = _run_isolated_import(tmp_path)

    modules_line = next(
        (line for line in result.stdout.splitlines() if line.startswith("MODULES:")), ""
    )
    found = set(modules_line[len("MODULES:"):].split(","))

    assert found == _EXPECTED_MODULES


# ============================================================
# (a2) SENTINEL -- a credential set in this process does not reach the child
# ============================================================

# Named for the kind of thing that must never cross, not for any value
# that's actually secret here -- a database password, a seed secret, and a
# proxy URL (which can carry embedded credentials of its own) are each set
# to an obviously-fake value in the parent for the one test below, and the
# child is asked only whether the name is present in its environment, never
# for the value, so the value never has to be handled at all.
_SENTINEL_ENV_KEYS = ("DB_PASSWORD", "SEED_SECRET", "AUDIBLE_PROXY_URL")

_SENTINEL_CHILD_SCRIPT = """
import os

keys = {keys!r}
present = [key for key in keys if key in os.environ]

print("PRESENT:" + ",".join(sorted(present)))
""".format(keys=_SENTINEL_ENV_KEYS)


def test_none_of_the_parents_credentials_reach_the_child(tmp_path, monkeypatch):
    """A denylist that only strips one prefix would let a differently-named
    credential straight through, and 'nothing leaked' would still print
    for the wrong reason: the parent never happened to hold one in this
    run. Planting known names in the parent and asking the child to report
    only their presence is what tells the two cases apart."""
    for key in _SENTINEL_ENV_KEYS:
        monkeypatch.setenv(key, f"sentinel-{key.lower()}-must-not-cross")

    result = subprocess.run(
        [sys.executable, "-c", _SENTINEL_CHILD_SCRIPT],
        cwd=tmp_path,
        env=_isolated_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr

    present_line = next(
        (line for line in result.stdout.splitlines() if line.startswith("PRESENT:")), ""
    )
    present = present_line[len("PRESENT:"):]
    assert present == "", f"parent credentials reached the child: {present}"


# ============================================================
# (b) AST WALK -- no import of app anywhere in libex_core's source
# ============================================================

def _app_imports_in(path: Path) -> list[str]:
    """Every module named by an `import`/`from ... import` statement in
    `path` that is `app` or a dotted submodule of it. Reads the source as
    text and parses it rather than importing it, so this catches the import
    even in a module that would itself fail to import for an unrelated
    reason, and never executes libex_core code to check it."""
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name
                for alias in node.names
                if alias.name == "app" or alias.name.startswith("app.")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and (module == "app" or module.startswith("app.")):
                offenders.append(module)
    return offenders


def test_no_libex_core_source_file_imports_app():
    offenders = {
        str(path.relative_to(REPO_ROOT)): found
        for path in LIBEX_CORE_DIR.rglob("*.py")
        if (found := _app_imports_in(path))
    }

    assert offenders == {}, f"libex_core files importing app: {offenders}"


def test_the_ast_walk_actually_checked_every_libex_core_file():
    """Same non-vacuousness concern as the subprocess walk above, for the
    AST check: a glob that matched no files would pass test_no_libex_core_
    source_file_imports_app for free."""
    checked = {path for path in LIBEX_CORE_DIR.rglob("*.py")}

    assert checked, "no .py files found under libex_core/"
