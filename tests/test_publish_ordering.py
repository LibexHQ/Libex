"""
Ordering guards in .github/workflows/publish.yml.

Tests runs on main can finish out of order. If an older commit's image were
tagged `latest` after a newer one had already migrated the database, the API
would restart on a schema it has no revision for and crash-loop. The workflow
prevents that two ways, and both are pinned here: publishes are serialised
without cancelling one in flight or one waiting, and `latest` is applied only
when the commit being published is still the tip of main. The sha tag is
pushed every time.
"""

# Standard library
import os
import subprocess
from pathlib import Path

# Third party
import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "publish.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list[dict]:
    return _workflow()["jobs"]["build-and-push"]["steps"]


def _step(step_id: str) -> dict:
    matches = [s for s in _steps() if s.get("id") == step_id]
    assert len(matches) == 1, f"expected one step with id {step_id!r}"
    return matches[0]


def _tag_lines() -> list[str]:
    tags = _step("meta")["with"]["tags"]
    return [line.strip() for line in tags.splitlines() if line.strip()]


def test_publishes_are_serialised_and_never_cancelled():
    assert _workflow()["concurrency"] == {
        "group": "publish-main",
        "cancel-in-progress": False,
        "queue": "max",
    }


def test_latest_is_conditional_on_the_tip_of_main_check():
    latest = [line for line in _tag_lines() if "value=latest" in line]
    assert latest == [
        "type=raw,value=latest,enable=${{ steps.tip.outputs.latest == 'true' }}"
    ]


def test_sha_tag_is_unconditional_and_uses_the_tested_commit():
    sha = [line for line in _tag_lines() if "sha-" in line]
    assert sha == ["type=raw,value=sha-${{ steps.tip.outputs.short }}"]
    assert "enable=" not in sha[0]


def test_revision_label_is_the_tested_commit():
    labels = _step("meta")["with"]["labels"]
    assert "org.opencontainers.image.revision=${{ github.event.workflow_run.head_sha }}" in labels


def test_tip_check_runs_before_tags_and_compares_against_main():
    ids = [s.get("id") for s in _steps()]
    assert ids.index("tip") < ids.index("meta")
    tip = _step("tip")
    assert tip["env"]["HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert "git ls-remote origin refs/heads/main" in tip["run"]
    assert '[ "$tip" = "$HEAD_SHA" ]' in tip["run"]


TESTED = "a" * 40
NEWER = "b" * 40


def _run_tip_check(tmp_path: Path, tip: str) -> tuple[subprocess.CompletedProcess, dict]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(f"#!/bin/sh\nprintf '%s\\trefs/heads/main\\n' '{tip}'\n" if tip else "#!/bin/sh\n")
    fake_git.chmod(0o755)
    output = tmp_path / "out"
    output.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HEAD_SHA": TESTED,
        "GITHUB_OUTPUT": str(output),
    }
    proc = subprocess.run(
        ["bash", "-e", "-c", _step("tip")["run"]], env=env, capture_output=True, text=True
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return proc, outputs


@pytest.mark.parametrize(("tip", "latest"), [(TESTED, "true"), (NEWER, "false")])
def test_tip_check_sets_latest_only_for_the_tip(tmp_path, tip, latest):
    proc, outputs = _run_tip_check(tmp_path, tip)
    assert proc.returncode == 0, proc.stderr
    assert outputs == {"short": TESTED[:7], "latest": latest}


def test_tip_check_fails_when_main_cannot_be_read(tmp_path):
    proc, outputs = _run_tip_check(tmp_path, "")
    assert proc.returncode != 0
    assert "latest" not in outputs
