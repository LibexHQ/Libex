"""
The built wheel, installed, and the `libex-core` console script it puts on the
path, run as a real process.

test_packaging reads the archives; nothing there proves an installed copy runs.
This installs the wheel into a venv of its own with the dependencies it needs
taken from the hash-pinned dev lock, so no repository source is on the
import path, and drives the script: every --help, one lookup against a local
stand-in for Audible, and the db commands against a throwaway SQLite file.

The stand-in is an HTTP proxy, because a proxy is the one way the command line
lets a request be pointed anywhere: it answers CONNECT and then speaks TLS for
api.audible.com with a certificate made for the test. The client trusts only
the certifi bundle and ignores the environment, so the venv's own copy of that
bundle is the one thing the test adds the certificate to. Nothing leaves the
machine.
"""

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import libex_core
from libex_core.cli.parser import build_parser
from tests.libex_core._cli_support import walk_parsers

REPO_ROOT = Path(__file__).resolve().parents[2]
# The dev lock, because aiosqlite, the storage extra's driver, is pinned only
# there; it is a strict superset of the application lock.
LOCK = REPO_ROOT / "requirements-dev.lock"

# Same skip as test_packaging: --no-isolation needs the backend and the
# frontend already importable, and the unit step's environment has neither.
_BUILD_TOOLS = ("build", "flit_core")
_MISSING = [m for m in _BUILD_TOOLS if importlib.util.find_spec(m) is None]

# Building, installing and the first run happen inside the first test's setup,
# well past the suite's 30 second tripwire.
pytestmark = [
    pytest.mark.skipif(
        bool(_MISSING),
        reason=f"packaging build tools not installed: {', '.join(_MISSING)}",
    ),
    pytest.mark.timeout(300),
]

# The runtime requirements and the storage extra, the two things the installed
# program needs. Spelled out for the same reason test_packaging spells out its
# extras: a change in pyproject.toml should be a decision made here too.
_ROOTS = ("httpx", "pydantic", "sqlalchemy", "alembic", "aiosqlite")

ASIN = "B000000001"
HOST = "api.audible.com"
CHILD_ENV_BASE = {"PATH": "/usr/bin:/bin"}


# ============================================================
# BUILDING AND INSTALLING
# ============================================================

def _closure(roots):
    """Every distribution the roots need, read from this environment's own
    metadata. This environment was installed from the dev lock, so each name
    here has a block in it."""
    seen, pending = set(), [canonicalize_name(r) for r in roots]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for text in importlib.metadata.requires(name) or []:
            requirement = Requirement(text)
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            pending.append(canonicalize_name(requirement.name))
    return seen


def _lock_blocks():
    blocks, current = {}, None
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9._-]+)==", line)
        if match:
            current = canonicalize_name(match.group(1))
            blocks[current] = []
        if current is not None and line.strip():
            blocks[current].append(line)
    return blocks


def _lock_subset(names):
    blocks = _lock_blocks()
    missing = sorted(names - blocks.keys())
    assert not missing, f"not in requirements-dev.lock: {missing}"
    return "\n".join(line for name in sorted(names) for line in blocks[name]) + "\n"


def _run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=240, **kwargs)
    assert result.returncode == 0, f"{args}\n{result.stdout}\n{result.stderr}"
    return result


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    outdir = tmp_path_factory.mktemp("dist")
    _run(
        [sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(outdir), str(REPO_ROOT)]
    )
    wheels = list(outdir.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.fixture(scope="module")
def venv(tmp_path_factory, wheel):
    root = tmp_path_factory.mktemp("venv")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True, timeout=60)
    python = root / "bin" / "python"
    requirements = root / "requirements.txt"
    requirements.write_text(_lock_subset(_closure(_ROOTS)), encoding="utf-8")
    # Without PYTHONPATH, which is how CI lends the build tools to this run:
    # inherited, it would put them in front of pip's view of the new venv.
    pip_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    pip = [sys.executable, "-m", "pip", "--python", str(python), "--disable-pip-version-check"]
    # Hash-checked, so the dependencies are the locked ones. The wheel itself
    # has no hash and goes in on its own, with its dependencies already there.
    _run([*pip, "install", "--require-hashes", "--no-deps", "-r", str(requirements)], env=pip_env)
    _run([*pip, "install", "--no-deps", str(wheel)], env=pip_env)
    _run([*pip, "check"], env=pip_env)
    return root


def _child(venv, args, env=None, cwd=None):
    """Run a program from the venv in an environment holding only what is named."""
    return subprocess.run(
        [str(venv / "bin" / args[0]), *args[1:]],
        env={**CHILD_ENV_BASE, **(env or {})},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


# ============================================================
# THE STAND-IN FOR AUDIBLE
# ============================================================

def _make_certificate(directory):
    if shutil.which("openssl") is None:
        pytest.fail("openssl is needed to make the stand-in's certificate")
    cert, key = directory / "stub.pem", directory / "stub.key"
    _run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-days", "2", "-subj", f"/CN={HOST}", "-addext", f"subjectAltName=DNS:{HOST}",
            "-keyout", str(key), "-out", str(cert),
        ]
    )
    return cert, key


class StubAudible:
    """A CONNECT proxy that answers for api.audible.com itself, over TLS, with
    one product. Keeps what it was asked, so a test can show the request went
    through it."""

    def __init__(self, cert, key):
        self.requests = []
        self.connects = []
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(str(cert), str(key))
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _head(conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            data += chunk
        return data.decode("latin-1").split("\r\n")[0]

    def _serve(self, conn):
        try:
            with conn:
                connect = self._head(conn)
                if connect is None or not connect.startswith("CONNECT "):
                    return
                self.connects.append(connect.split()[1])
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                with self._context.wrap_socket(conn, server_side=True) as tls:
                    request = self._head(tls)
                    if request is None:
                        return
                    path = request.split()[1].split("?")[0]
                    self.requests.append(path)
                    body = json.dumps(
                        {"product": {
                            "asin": ASIN,
                            "title": "Installed Wheel",
                            "publication_datetime": "2020-01-01T00:00:00Z",
                        }}
                    ).encode()
                    tls.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Connection: close\r\nContent-Length: %d\r\n\r\n%b" % (len(body), body)
                    )
        except (OSError, ssl.SSLError):
            return

    def close(self):
        self._server.close()


@pytest.fixture(scope="module")
def stub(tmp_path_factory, venv):
    directory = tmp_path_factory.mktemp("stub")
    cert, key = _make_certificate(directory)
    # The client trusts the certifi bundle and nothing from the environment,
    # so the venv's copy, which is thrown away with it, is where the stand-in's
    # certificate is trusted.
    bundle = _child(venv, ["python", "-c", "import certifi; print(certifi.where())"])
    assert bundle.returncode == 0, bundle.stderr
    bundle_path = Path(bundle.stdout.strip())
    assert venv in bundle_path.parents
    with bundle_path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + cert.read_text(encoding="utf-8"))
    server = StubAudible(cert, key)
    yield server
    server.close()


# ============================================================
# THE INSTALL ITSELF
# ============================================================

def test_the_installed_package_is_the_venvs_not_the_repositorys(venv, tmp_path):
    result = _child(
        venv,
        ["python", "-I", "-c", "import libex_core; print(libex_core.__file__); print(libex_core.__version__)"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    location, version = result.stdout.split()
    assert venv in Path(location).parents
    assert REPO_ROOT not in Path(location).parents
    assert version == libex_core.__version__


def test_the_console_script_reports_the_version(venv, tmp_path):
    result = _child(venv, ["libex-core", "--version"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"libex-core {libex_core.__version__}\n"


# ============================================================
# --help, ROOT AND EVERY SUBCOMMAND
# ============================================================

_PATHS = [path for path, _ in walk_parsers(build_parser())]


def test_the_help_walk_covers_the_whole_tree():
    assert () in _PATHS and ("db", "upgrade") in _PATHS and ("book", "get") in _PATHS
    assert len(_PATHS) > 40


def test_every_help_exits_zero_and_names_its_own_command(venv, tmp_path):
    def run(path):
        return path, _child(venv, ["libex-core", *path, "--help"], cwd=tmp_path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, _PATHS))
    for path, result in results:
        words = " ".join(("libex-core", *path))
        assert result.returncode == 0, f"{words}: {result.stderr}"
        assert result.stdout.startswith(f"usage: {words} "), words
        assert result.stderr == "", words


# ============================================================
# A LOOKUP, AGAINST THE STAND-IN
# ============================================================

def _env(stub, storage):
    return {
        "HOME": str(storage.parent),
        "LIBEX_CORE_PROXY_URL": f"http://127.0.0.1:{stub.port}",
        "LIBEX_CORE_STORAGE": str(storage),
    }


def test_the_db_commands_and_a_lookup_run_end_to_end(venv, stub, tmp_path):
    storage = tmp_path / "store.db"
    env = _env(stub, storage)

    # A store that was never created is refused, and nothing is created for it.
    early = _child(venv, ["libex-core", "db", "status"], env, tmp_path)
    assert early.returncode == 5, early.stderr
    assert json.loads(early.stdout)["state"] == "not-initialised"
    assert not storage.exists()

    upgrade = _child(venv, ["libex-core", "db", "upgrade"], env, tmp_path)
    assert upgrade.returncode == 0, upgrade.stderr
    revision = json.loads(upgrade.stdout)
    assert storage.exists()

    status = _child(venv, ["libex-core", "db", "status"], env, tmp_path)
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["state"] == "ok"
    assert revision

    # Nothing stored yet: a read that finds nothing exits 3.
    empty = _child(venv, ["libex-core", "db", "book", ASIN], env, tmp_path)
    assert empty.returncode == 3, empty.stderr
    assert empty.stdout == ""

    # The lookup goes out through the stand-in and is kept in the store.
    lookup = _child(venv, ["libex-core", "book", "get", ASIN], env, tmp_path)
    assert lookup.returncode == 0, lookup.stderr
    book = json.loads(lookup.stdout)
    assert book["asin"] == ASIN
    assert book["title"] == "Installed Wheel"
    assert stub.connects == [f"{HOST}:443"]
    assert stub.requests == [f"/1.0/catalog/products/{ASIN}"]

    # And now the read finds it, from the store alone.
    requests_before = list(stub.requests)
    stored = _child(venv, ["libex-core", "db", "book", ASIN], env, tmp_path)
    assert stored.returncode == 0, stored.stderr
    assert json.loads(stored.stdout)["title"] == "Installed Wheel"
    stats = _child(venv, ["libex-core", "db", "stats"], env, tmp_path)
    assert stats.returncode == 0, stats.stderr
    assert json.loads(stats.stdout)
    assert stub.requests == requests_before


def test_a_lookup_with_no_proxy_and_no_opt_in_is_refused(venv, tmp_path):
    result = _child(venv, ["libex-core", "book", "get", ASIN], {"HOME": str(tmp_path)}, tmp_path)
    assert result.returncode == 5
    assert result.stdout == ""
    assert "LIBEX_CORE_PROXY_URL" in result.stderr
