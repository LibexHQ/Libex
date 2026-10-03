"""
docker-entrypoint.sh: which commands migrate, and what a failed migration does.

The script is run for real under sh, with stand-ins for alembic, uvicorn and
python placed first on PATH. Each stand-in appends its argv to a shared log
and exits with whatever status the test sets, so a test can read back what
the entrypoint called, in what order, and what it exec'd.
"""

# Standard library
import os
import shutil
import subprocess
from pathlib import Path

# Third party
import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker-entrypoint.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


@pytest.fixture
def fakes(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("alembic", "uvicorn", "python"):
        stub = bin_dir / name
        upper = name.upper()
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "{name} $*" >> "$CALL_LOG"\n'
            f'exit "${{FAKE_{upper}_STATUS:-0}}"\n'
        )
        stub.chmod(0o755)
    return bin_dir, log


def _run(fakes, *argv, alembic_status=0):
    bin_dir, log = fakes
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "CALL_LOG": str(log),
        "FAKE_ALEMBIC_STATUS": str(alembic_status),
    }
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), *argv], env=env, capture_output=True, text=True, timeout=30
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


UVICORN_CMD = ("uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3333", "--no-access-log")


def test_uvicorn_migrates_then_starts(fakes):
    result, calls = _run(fakes, *UVICORN_CMD)

    assert result.returncode == 0
    assert calls == ["alembic upgrade head", "uvicorn " + " ".join(UVICORN_CMD[1:])]
    assert "Database migrations applied" in result.stdout


def test_failed_migration_refuses_to_start_with_alembic_status(fakes):
    result, calls = _run(fakes, *UVICORN_CMD, alembic_status=3)

    assert result.returncode == 3
    assert calls == ["alembic upgrade head"], "uvicorn ran after a failed migration"
    assert "Refusing to start: alembic upgrade head exited 3" in result.stderr
    assert "Database migrations applied" not in result.stdout


def test_uvicorn_by_resolved_path_also_migrates(fakes):
    bin_dir, _ = fakes
    result, calls = _run(fakes, str(bin_dir / "uvicorn"), "app.main:app", alembic_status=1)

    assert result.returncode == 1
    assert calls == ["alembic upgrade head"]


@pytest.mark.parametrize(
    "argv",
    [
        ("python", "-m", "scripts.region_keys", "verify", "--pre-window"),
        ("python", "-m", "scripts.region_keys", "unfinalize", "--i-have-stopped-writers"),
        ("python", "scripts/region_keys.py", "expand"),
        ("python", "-m", "scripts.seed"),
        ("python", "scripts/backfill_chapters.py"),
        ("python", "-m", "scripts.refresh_corpus", "--dry-run"),
    ],
)
def test_operator_scripts_never_migrate(fakes, argv):
    """A failing migration is set up and must not matter: the script is
    exec'd and alembic is never called."""
    result, calls = _run(fakes, *argv, alembic_status=1)

    assert result.returncode == 0
    assert calls == [" ".join(argv)]
    assert "Refusing to start" not in result.stderr


def test_explicit_alembic_command_is_not_preceded_by_an_upgrade(fakes):
    result, calls = _run(fakes, "alembic", "downgrade", "b8d2e5a71c46")

    assert result.returncode == 0
    assert calls == ["alembic downgrade b8d2e5a71c46"]
