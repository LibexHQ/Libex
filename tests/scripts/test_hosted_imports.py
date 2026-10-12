"""
The hosted image is Postgres-only: requirements.lock carries asyncpg and not
aiosqlite, while the dev lock carries both. Importing a name from a
libex_core.storage package goes through that package's lazy __getattr__ and
require_storage(), which refuses to run without aiosqlite. The hosted code
must import the defining submodules directly, and a suite that has aiosqlite
installed cannot notice when it doesn't. Each module is imported here in a
fresh interpreter with aiosqlite made unfindable, as it is in the image.
"""

# Standard library
import subprocess
import sys
from pathlib import Path

# Third party
import pytest

ROOT = Path(__file__).resolve().parents[2]

MODULES = sorted(
    f"scripts.{p.stem}" for p in (ROOT / "scripts").glob("*.py") if p.stem != "__init__"
) + ["app.main"]

_PROBE = """
import importlib.util
import sys

_real = importlib.util.find_spec


def _find_spec(name, *args, **kwargs):
    if name.split(".")[0] == "aiosqlite":
        return None
    return _real(name, *args, **kwargs)


importlib.util.find_spec = _find_spec
sys.modules.pop("aiosqlite", None)
sys.modules["aiosqlite"] = None

from libex_core.storage import StorageUnavailable
import importlib

try:
    importlib.import_module(sys.argv[1])
except StorageUnavailable as exc:
    print(f"StorageUnavailable: {exc}")
    sys.exit(3)
"""


def test_probe_hides_aiosqlite():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE.replace(
                "importlib.import_module(sys.argv[1])",
                "from libex_core.storage import require_storage; require_storage()",
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stderr


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_without_aiosqlite(module):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 3, result.stdout
    assert result.returncode == 0, result.stderr[-2000:]
