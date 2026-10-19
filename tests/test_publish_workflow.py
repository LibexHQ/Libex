"""
The image publish workflow runs on `workflow_run` after Tests, with
`branches: [main]`. That filter matches the head branch name only, and Tests
also runs on pull_request, so a fork PR from a branch called main completes a
Tests run that passes the filter. Without a guard on the job, that run would
push the fork's commit to both registries as `latest`. These checks hold the
guard in place.
"""

# Standard library
from pathlib import Path

# Third party
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
PUBLISH_PATH = WORKFLOWS_DIR / "publish.yml"

REQUIRED_CONDITIONS = (
    "github.event.workflow_run.conclusion == 'success'",
    "github.event.workflow_run.event == 'push'",
    "github.event.workflow_run.head_repository.full_name == github.repository",
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _triggers(workflow: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, dict):
        return on
    if isinstance(on, str):
        return {on: None}
    return {name: None for name in on or ()}


def test_publish_job_only_runs_for_a_push_to_this_repository():
    jobs = _load(PUBLISH_PATH)["jobs"]
    assert jobs, "publish.yml has no jobs"
    for name, job in jobs.items():
        condition = " ".join(str(job.get("if", "")).split())
        for required in REQUIRED_CONDITIONS:
            assert required in condition, f"job {name!r} is missing `{required}`"
        assert "||" not in condition, f"job {name!r} can bypass the guard with ||"


def test_every_workflow_run_consumer_carries_the_same_guard():
    consumers = []
    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        workflow = _load(path)
        if "workflow_run" not in _triggers(workflow):
            continue
        consumers.append(path.name)
        for name, job in workflow["jobs"].items():
            condition = " ".join(str(job.get("if", "")).split())
            for required in REQUIRED_CONDITIONS[1:]:
                assert required in condition, (
                    f"{path.name} job {name!r} is missing `{required}`"
                )
    assert "publish.yml" in consumers
