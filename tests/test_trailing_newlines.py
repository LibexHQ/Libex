"""
Every tracked text file ends with exactly one newline.
"""

# Standard library
import shutil
import subprocess
from pathlib import Path

# Third party
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SNIFF_BYTES = 8192


def _tracked_files() -> list[str]:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout (for example an sdist test run)")
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return [name for name in result.stdout.decode().split("\0") if name]


def _newline_problem(data: bytes) -> str | None:
    """Return why a text file's ending is wrong, or None when it is fine."""
    if not data or b"\0" in data[:_SNIFF_BYTES]:
        return None
    if not data.endswith(b"\n"):
        return "missing trailing newline"
    if data.endswith(b"\n\n") or data.endswith(b"\r\n\r\n"):
        return "extra blank line at end of file"
    return None


def test_tracked_text_files_end_with_one_newline():
    offenders = []
    for name in _tracked_files():
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        problem = _newline_problem(path.read_bytes())
        if problem:
            offenders.append(f"{name}: {problem}")
    assert not offenders, (
        f"{len(offenders)} tracked file(s) do not end with exactly one newline:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"x\n", None),
        (b"x", "missing trailing newline"),
        (b"x\n\n", "extra blank line at end of file"),
        (b"x\r\n\r\n", "extra blank line at end of file"),
        (b"", None),
        (b"\0\x01binary", None),
    ],
)
def test_newline_problem_classification(data, expected):
    assert _newline_problem(data) == expected
