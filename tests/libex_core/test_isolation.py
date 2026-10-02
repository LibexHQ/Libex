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

# Third party
import pytest

# Local
import libex_core as _libex_core_package
from tests.libex_core._cli_support import clean_env, run_python, walk_actions

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


# ============================================================
# (c) AST WALK -- the process environment is read in one place only
# ============================================================

ENVIRONMENT_MODULE = LIBEX_CORE_DIR / "cli" / "environment.py"

_ENV_NAMES = frozenset(
    {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}
)
_OS_MODULES = frozenset({"os", "posix", "nt"})


def _is_dotenv(name: str) -> bool:
    return name == "dotenv" or name.startswith("dotenv.")


class _EnvironmentWalk(ast.NodeVisitor):
    """Finds every way the source can reach the process environment.

    `deferred` is true only inside a function body, which runs when called.
    A decorator, a default value, an annotation, a class body and module
    level all run when the module is imported, so a read there is an import-
    time read. A lambda inherits whatever context it sits in, which is the
    strict reading: a module-level lambda is not a function body.
    """

    def __init__(self) -> None:
        self.reads: list[tuple[int, str, bool]] = []
        self.dotenv: list[int] = []
        self._deferred = False

    def _read(self, node: ast.AST, what: str) -> None:
        self.reads.append((node.lineno, what, self._deferred))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _ENV_NAMES:
            self._read(node, f".{node.attr}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level == 0 and module in _OS_MODULES:
            for alias in node.names:
                if alias.name in _ENV_NAMES or alias.name == "*":
                    self._read(node, f"from {module} import {alias.name}")
        if node.level == 0 and _is_dotenv(module):
            self.dotenv.append(node.lineno)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _is_dotenv(alias.name):
                self.dotenv.append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # getattr(os, "environ"), getattr(os, name), getattr(anything, "environ")
        if isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}:
            target = node.args[0] if node.args else None
            name = node.args[1] if len(node.args) > 1 else None
            on_os = isinstance(target, ast.Name) and target.id in _OS_MODULES
            named = isinstance(name, ast.Constant) and name.value in _ENV_NAMES
            if on_os or named:
                self._read(node, f"{node.func.id}(...)")
        self.generic_visit(node)

    def _visit_function(self, node) -> None:
        outer = self._deferred
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns:
            self.visit(node.returns)
        self._deferred = True
        for statement in node.body:
            self.visit(statement)
        self._deferred = outer

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function


def _environment_use(source: str, filename: str = "<planted>"):
    walk = _EnvironmentWalk()
    walk.visit(ast.parse(source, filename=filename))
    return walk


def _environment_violations(path: Path, source: str) -> list[str]:
    walk = _environment_use(source, str(path))
    inside = path == ENVIRONMENT_MODULE
    problems = []
    for lineno, what, deferred in walk.reads:
        if not inside:
            problems.append(f"{path.name}:{lineno} reads the environment ({what})")
        elif not deferred:
            problems.append(f"{path.name}:{lineno} reads the environment outside a function body ({what})")
    if not inside:
        problems.extend(f"{path.name}:{lineno} imports dotenv" for lineno in walk.dotenv)
    return problems


def test_only_environment_dot_py_reads_the_environment_and_only_in_functions():
    problems = []
    for path in sorted(LIBEX_CORE_DIR.rglob("*.py")):
        problems.extend(_environment_violations(path, path.read_text()))
    assert problems == []


def test_environment_dot_py_reads_the_environment_at_all():
    """A walk that found nothing in the one module allowed to read would
    satisfy the test above for the wrong reason."""
    walk = _environment_use(ENVIRONMENT_MODULE.read_text())
    assert walk.reads, "no environment read found in cli/environment.py"
    assert all(deferred for _, _, deferred in walk.reads)


_ELSEWHERE = LIBEX_CORE_DIR / "cli" / "other.py"

_FORMS_FLAGGED_ANYWHERE = {
    "os.environ attribute": "import os\nx = os.environ['A']\n",
    "os.environ.get": "import os\ndef f():\n    return os.environ.get('A')\n",
    "os.environb": "import os\ndef f():\n    return os.environb\n",
    "os.getenv": "import os\ndef f():\n    return os.getenv('A')\n",
    "os.getenvb": "import os\ndef f():\n    return os.getenvb(b'A')\n",
    "os.putenv": "import os\ndef f():\n    os.putenv('A', 'b')\n",
    "os.unsetenv": "import os\ndef f():\n    os.unsetenv('A')\n",
    "aliased os": "import os as o\ndef f():\n    return o.environ\n",
    "from os import environ": "from os import environ\n",
    "from os import getenv as g": "from os import getenv as g\n",
    "from os import star": "from os import *\n",
    "from posix import environ": "from posix import environ\n",
    "getattr(os, 'environ')": "import os\ndef f():\n    return getattr(os, 'environ')\n",
    "getattr(os, name)": "import os\ndef f(n):\n    return getattr(os, n)\n",
    "getattr(x, 'getenv')": "def f(x):\n    return getattr(x, 'getenv')\n",
    "hasattr(os, name)": "import os\ndef f(n):\n    return hasattr(os, n)\n",
    "import dotenv": "import dotenv\n",
    "import dotenv.main": "import dotenv.main\n",
    "from dotenv import": "from dotenv import load_dotenv\n",
    "from dotenv.main import": "from dotenv.main import find_dotenv\n",
}


@pytest.mark.parametrize("form", sorted(_FORMS_FLAGGED_ANYWHERE))
def test_a_planted_env_read_outside_environment_dot_py_is_caught(form):
    problems = _environment_violations(_ELSEWHERE, _FORMS_FLAGGED_ANYWHERE[form])
    assert problems, form


def test_ordinary_os_use_is_not_flagged():
    source = "import os\nfrom os import path\nx = os.devnull\nos.open(x, 0)\n"
    assert _environment_violations(_ELSEWHERE, source) == []


_IN_ENVIRONMENT_MODULE_OK = "import os\ndef load():\n    return os.environ.get('A')\n"

_IN_ENVIRONMENT_MODULE_BAD = {
    "module level": "import os\nA = os.environ.get('A')\n",
    "class body": "import os\nclass C:\n    A = os.environ.get('A')\n",
    "default argument": "import os\ndef f(a=os.environ.get('A')):\n    return a\n",
    "keyword-only default": "import os\ndef f(*, a=os.getenv('A')):\n    return a\n",
    "decorator": "import os\n@os.environ.get\ndef f():\n    pass\n",
    "annotation": "import os\ndef f(a: os.environ):\n    pass\n",
    "module-level lambda": "import os\nf = lambda: os.environ.get('A')\n",
    "module-level from-import": "from os import environ\n",
    "module-level getattr": "import os\nA = getattr(os, 'environ')\n",
}


def test_a_read_inside_a_function_body_is_allowed_in_environment_dot_py():
    assert _environment_violations(ENVIRONMENT_MODULE, _IN_ENVIRONMENT_MODULE_OK) == []


@pytest.mark.parametrize("where", sorted(_IN_ENVIRONMENT_MODULE_BAD))
def test_a_read_that_runs_at_import_is_caught_in_environment_dot_py(where):
    assert _environment_violations(ENVIRONMENT_MODULE, _IN_ENVIRONMENT_MODULE_BAD[where]), where


# ============================================================
# (d) THE COMMAND LINE -- no proxy option, and importing it does nothing
# ============================================================

def test_no_option_or_command_in_the_parser_is_about_a_proxy():
    """A proxy URL can carry credentials, which must come from the environment
    and never from an argument a process listing would show."""
    from libex_core.cli.parser import build_parser

    actions = walk_actions(build_parser())
    assert actions, "the walk found no actions"
    for path, action in actions:
        names = [action.dest, *action.option_strings, *(action.choices or ())]
        if action.metavar:
            names.append(str(action.metavar))
        for name in names:
            assert "proxy" not in str(name).lower(), (path, name)


def test_the_proxy_check_catches_a_planted_option():
    import argparse

    from libex_core.cli.parser import build_parser

    parser = build_parser()
    container = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    container.choices["config"].add_argument("--proxy-url")
    flagged = [
        (path, action)
        for path, action in walk_actions(parser)
        if any("proxy" in str(n).lower() for n in [action.dest, *action.option_strings])
    ]
    assert flagged


_IMPORT_EFFECTS_SCRIPT = """
import importlib
import logging
import os
import pkgutil
import sys
import threading

# Every snapshot is taken before the first import of the package: one taken
# after would already include whatever the import itself did.
before_env = dict(os.environ)
before_argv = list(sys.argv)
before_threads = threading.active_count()
before_logging = {
    name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level)
    for name in ("", "libex")
}

import libex_core.cli as package

names = [package.__name__] + [
    info.name
    for info in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + ".")
]
for name in names:
    importlib.import_module(name)

after_logging = {
    name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level)
    for name in ("", "libex")
}
print("MODULES:" + ",".join(sorted(names)))
print("ENV:" + str(dict(os.environ) == before_env))
print("ARGV:" + str(sys.argv == before_argv))
print("THREADS:" + str(threading.active_count() == before_threads))
print("LOGGING:" + str(after_logging == before_logging))
"""


def _line(stdout: str, prefix: str) -> str:
    return next(line for line in stdout.splitlines() if line.startswith(prefix))[len(prefix):]


def test_importing_every_cli_module_has_no_side_effects(tmp_path):
    result = run_python(
        ["-c", _IMPORT_EFFECTS_SCRIPT],
        env=clean_env(),
        cwd=tmp_path,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    stdout_lines = result.stdout.splitlines()
    assert result.stderr == ""
    # Nothing was printed beyond the five report lines.
    assert len(stdout_lines) == 5, result.stdout
    modules = _line(result.stdout, "MODULES:").split(",")
    assert "libex_core.cli.main" in modules and "libex_core.cli.environment" in modules
    assert len(modules) >= 8
    assert _line(result.stdout, "ENV:") == "True"
    assert _line(result.stdout, "ARGV:") == "True"
    assert _line(result.stdout, "THREADS:") == "True"
    assert _line(result.stdout, "LOGGING:") == "True"


def test_importing_the_entry_point_module_runs_nothing(tmp_path):
    """The module is the target of `python -m`; the guard under it is what
    keeps an import from parsing sys.argv and exiting."""
    result = run_python(
        ["-c", "import libex_core.__main__ as m; print('imported', hasattr(m, 'main'))"],
        env=clean_env(),
        cwd=tmp_path,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "imported True\n"
    assert result.stderr == ""
