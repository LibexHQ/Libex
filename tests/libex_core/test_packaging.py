"""
The built distribution holds exactly what is meant to ship, and says so.

The wheel and sdist are built for real and every archive member is checked
against an explicit allowlist, because the failure being guarded is silent: a
packaging backend sweeps in whatever sits beside the package, and nothing at
build time says a stray file came along. Members are read from the archives
without extracting them, so a hostile or malformed name is inspected as a
string rather than acted on.
"""

import importlib.util
import re
import subprocess
import sys
import tarfile
import zipfile
from email import message_from_bytes
from pathlib import Path, PurePosixPath

import pytest

import libex_core

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "libex_core"

# Built with --no-isolation so the build never reaches for a package index;
# that needs the backend and the frontend already present in the environment.
_BUILD_TOOLS = ("build", "flit_core")
_MISSING = [m for m in _BUILD_TOOLS if importlib.util.find_spec(m) is None]

pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=f"packaging build tools not installed: {', '.join(_MISSING)}",
)

_DIST_INFO = f"libex_core-{libex_core.__version__}.dist-info"
_SDIST_ROOT = f"libex_core-{libex_core.__version__}"

_PACKAGE_FILES = {
    path.relative_to(REPO_ROOT).as_posix()
    for path in PACKAGE_DIR.rglob("*")
    if path.is_file()
    and "__pycache__" not in path.parts
    and path.suffix != ".pyc"
}

# flit ships every non-bytecode file inside the package directory, so the
# package's own changelog travels in the wheel. It is the only non-Python
# file allowed to.
_PACKAGE_ALLOWED_SUFFIXES = {".py"}
_PACKAGE_ALLOWED_NAMES = {"py.typed", "CHANGELOG.md"}

_WHEEL_METADATA_ALLOWED = {
    f"{_DIST_INFO}/WHEEL",
    f"{_DIST_INFO}/METADATA",
    f"{_DIST_INFO}/RECORD",
    f"{_DIST_INFO}/licenses/LICENSE",
}

_SDIST_ROOT_ALLOWED = {
    f"{_SDIST_ROOT}/LICENSE",
    f"{_SDIST_ROOT}/pyproject.toml",
    f"{_SDIST_ROOT}/PKG-INFO",
}

_FORBIDDEN_PATTERNS = (
    re.compile(r"(^|/)__pycache__(/|$)"),
    re.compile(r"\.py[co]$"),
    re.compile(r"(^|/)\.env"),
    re.compile(r"(^|/)app(/|$)"),
    re.compile(r"(^|/)tests(/|$)"),
    re.compile(r"(^|/)\.claude(/|$)"),
    re.compile(r"(^|/)CLAUDE"),
    re.compile(r"(^|/)LIBEX_"),
    re.compile(r"\.data(/|$)"),
)

_EXPECTED_REQUIRES_DIST = {
    "httpx>=0.28.1,<0.29",
    "pydantic>=2.13.4,<3",
}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    outdir = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(outdir), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(outdir.glob("*.whl"))
    sdists = list(outdir.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1
    return wheels[0], sdists[0]


def _wheel_names(wheel):
    with zipfile.ZipFile(wheel) as zf:
        return zf.namelist()


def _sdist_members(sdist):
    with tarfile.open(sdist) as tf:
        return tf.getmembers()


def _assert_safe_name(name):
    path = PurePosixPath(name)
    assert not path.is_absolute(), name
    assert ".." not in path.parts, name
    assert "\\" not in name, name
    for pattern in _FORBIDDEN_PATTERNS:
        assert not pattern.search(name), f"{name} matches {pattern.pattern}"


def _assert_package_member(name):
    path = PurePosixPath(name)
    assert path.parts[0] == "libex_core", name
    assert path.name in _PACKAGE_ALLOWED_NAMES or path.suffix in _PACKAGE_ALLOWED_SUFFIXES, name


def test_wheel_contains_only_the_allowlist(built):
    wheel, _ = built
    names = _wheel_names(wheel)
    for name in names:
        _assert_safe_name(name)
        if name.startswith("libex_core/"):
            _assert_package_member(name)
        else:
            assert name in _WHEEL_METADATA_ALLOWED, name
    assert {n for n in names if n.startswith("libex_core/")} == _PACKAGE_FILES
    assert _WHEEL_METADATA_ALLOWED <= set(names)


def test_wheel_ships_the_typing_marker(built):
    wheel, _ = built
    assert "libex_core/py.typed" in _wheel_names(wheel)


def test_sdist_contains_only_the_allowlist(built):
    _, sdist = built
    members = _sdist_members(sdist)
    for member in members:
        assert member.isreg(), f"{member.name} is not a regular file"
        _assert_safe_name(member.name)
        if member.name.startswith(f"{_SDIST_ROOT}/libex_core/"):
            _assert_package_member(member.name.removeprefix(f"{_SDIST_ROOT}/"))
        else:
            assert member.name in _SDIST_ROOT_ALLOWED, member.name
    names = {m.name for m in members}
    assert _SDIST_ROOT_ALLOWED <= names
    assert {n.removeprefix(f"{_SDIST_ROOT}/") for n in names if "/libex_core/" in n} == _PACKAGE_FILES


def test_wheel_has_no_symlinks_or_special_entries(built):
    wheel, _ = built
    with zipfile.ZipFile(wheel) as zf:
        for info in zf.infolist():
            assert not info.is_dir(), info.filename
            mode = info.external_attr >> 16
            assert (mode & 0o170000) in (0, 0o100000), f"{info.filename} is not a regular file"


def _metadata(wheel):
    with zipfile.ZipFile(wheel) as zf:
        return message_from_bytes(zf.read(f"{_DIST_INFO}/METADATA"))


def test_metadata_version_matches_the_package(built):
    wheel, _ = built
    assert _metadata(wheel)["Version"] == libex_core.__version__


def test_metadata_name_and_python_floor(built):
    wheel, _ = built
    meta = _metadata(wheel)
    assert meta["Name"] == "libex-core"
    assert meta["Requires-Python"] == ">=3.12"
    assert "Typing :: Typed" in meta.get_all("Classifier")


def test_runtime_dependencies_are_exactly_the_two(built):
    wheel, sdist = built
    assert set(_metadata(wheel).get_all("Requires-Dist")) == _EXPECTED_REQUIRES_DIST
    with tarfile.open(sdist) as tf:
        pkg_info = message_from_bytes(tf.extractfile(f"{_SDIST_ROOT}/PKG-INFO").read())
    assert set(pkg_info.get_all("Requires-Dist")) == _EXPECTED_REQUIRES_DIST
