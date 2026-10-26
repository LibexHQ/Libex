"""
The dependency audit blocks only on advisories a change introduces, compared
by advisory id against a base ref. These run scripts/audit-new-advisories.sh
against a throwaway git repo with a stub pip-audit on PATH, so they need no
network: each "lock" holds the JSON the stub reports for it.
"""

# Standard library
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Third party
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "audit-new-advisories.sh"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
LOCKS = ("requirements.lock", "requirements-dev.lock", "requirements-tooling.lock")

# Writes the lock's own content as the report, and refuses to run without the
# flags CI depends on, so dropping either from the script fails these tests.
STUB = """\
import json, sys
args = sys.argv[1:]
if "--strict" not in args or "--require-hashes" not in args:
    sys.exit(2)
body = open(args[args.index("-r") + 1]).read()
if body.startswith("CRASH"):
    print("Traceback: boom", file=sys.stderr)
    sys.exit(1)
if body.startswith("NOFINDINGS-EXIT1"):
    body, code = '{"dependencies": []}', 1
else:
    code = 1 if any(d.get("vulns") for d in json.loads(body)["dependencies"]) else 0
open(args[args.index("-o") + 1], "w").write(body)
sys.exit(code)
"""

needs_tools = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("git")), reason="needs bash and git"
)


def _report(*deps):
    return json.dumps({"dependencies": [
        {"name": name, "version": version, "vulns": [{"id": i, "aliases": aliases} for i in ids]}
        for name, version, ids, aliases in deps
    ]})


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "pip-audit"
    stub.write_text(f"#!{sys.executable}\n{STUB}")
    stub.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "t")
    return work, bin_dir


def _commit_base(work, **locks):
    for name, body in locks.items():
        (work / name).write_text(body)
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "base")
    _git(work, "tag", "base")


def _run(work, bin_dir, *locks, base="base", **heads):
    for name, body in heads.items():
        (work / name).write_text(body)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "LIBEX_AUDIT_BASE": base}
    return subprocess.run(
        ["bash", str(SCRIPT), *locks], cwd=work, env=env, capture_output=True, text=True
    )


CLEAN = _report(("idna", "3.15", [], []))
CERTIFI = _report(("certifi", "2026.7.22", ["PYSEC-2026-4320"], []))


@needs_tools
def test_a_new_advisory_blocks(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": CLEAN})
    result = _run(work, bin_dir, "a.lock", **{"a.lock": CERTIFI})
    assert result.returncode == 1
    assert "NEW advisories" in result.stderr
    assert "certifi==2026.7.22 PYSEC-2026-4320" in result.stderr


@needs_tools
def test_an_advisory_already_on_the_base_is_reported_and_passes(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": CERTIFI})
    result = _run(work, bin_dir, "a.lock")
    assert result.returncode == 0, result.stderr
    assert "already on base" in result.stdout
    assert "certifi==2026.7.22 PYSEC-2026-4320" in result.stdout
    assert "NEW" not in result.stderr


@needs_tools
def test_a_version_bump_still_affected_by_a_known_id_is_not_new(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": CERTIFI})
    bumped = _report(("certifi", "2026.7.23", ["PYSEC-2026-4320"], []))
    result = _run(work, bin_dir, "a.lock", **{"a.lock": bumped})
    assert result.returncode == 0, result.stderr
    assert "certifi==2026.7.23 PYSEC-2026-4320" in result.stdout


@needs_tools
def test_a_new_id_on_a_package_that_already_had_one_blocks(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": CERTIFI})
    more = _report(("certifi", "2026.7.22", ["PYSEC-2026-4320", "GHSA-new"], []))
    result = _run(work, bin_dir, "a.lock", **{"a.lock": more})
    assert result.returncode == 1
    assert "GHSA-new" in result.stderr
    assert "PYSEC-2026-4320" not in result.stderr


@needs_tools
def test_the_same_id_on_a_different_package_is_new(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": CERTIFI})
    moved = _report(("urllib3", "2.0.0", ["PYSEC-2026-4320"], []))
    result = _run(work, bin_dir, "a.lock", **{"a.lock": moved})
    assert result.returncode == 1


@needs_tools
def test_an_id_matching_a_base_alias_is_known(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": _report(("certifi", "1", ["PYSEC-1"], ["GHSA-1"]))})
    result = _run(work, bin_dir, "a.lock", **{"a.lock": _report(("certifi", "1", ["GHSA-1"], []))})
    assert result.returncode == 0, result.stderr


@needs_tools
def test_a_lock_missing_from_the_base_treats_everything_as_new(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": CLEAN})
    result = _run(work, bin_dir, "b.lock", **{"b.lock": CERTIFI})
    assert result.returncode == 1


@needs_tools
@pytest.mark.parametrize("side", ["head", "base"])
def test_a_pip_audit_crash_fails(repo, side):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": "CRASH" if side == "base" else CLEAN})
    result = _run(work, bin_dir, "a.lock", **{"a.lock": "CRASH" if side == "head" else CLEAN})
    assert result.returncode != 0
    assert "pip-audit failed" in result.stderr


@needs_tools
def test_a_base_crash_on_a_later_lock_cannot_reuse_an_earlier_report(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": CERTIFI, "b.lock": "CRASH"})
    result = _run(work, bin_dir, "a.lock", "b.lock", **{"b.lock": CERTIFI})
    assert result.returncode != 0


@needs_tools
def test_exit_1_without_findings_fails(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": CLEAN})
    result = _run(work, bin_dir, "a.lock", **{"a.lock": "NOFINDINGS-EXIT1"})
    assert result.returncode != 0
    assert "disagrees" in result.stderr


@needs_tools
def test_a_skipped_package_fails(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": CLEAN})
    skipped = json.dumps({"dependencies": [{"name": "x", "skip_reason": "could not resolve"}]})
    result = _run(work, bin_dir, "a.lock", **{"a.lock": skipped})
    assert result.returncode != 0


@needs_tools
def test_a_base_ref_that_does_not_resolve_fails(repo):
    work, bin_dir = repo
    _commit_base(work, **{"a.lock": CERTIFI})
    result = _run(work, bin_dir, "a.lock", base="origin/nowhere")
    assert result.returncode != 0
    assert "does not resolve" in result.stderr


def _audit_step():
    steps = yaml.safe_load(TESTS_WORKFLOW.read_text())["jobs"]["backend"]["steps"]
    return next(s for s in steps if s.get("name") == "Audit dependencies")


def test_ci_audits_all_three_locks_through_the_script():
    run = _audit_step()["run"]
    calls = [line for line in run.replace("\\\n", " ").splitlines() if "audit-new-advisories.sh" in line]
    assert len(calls) == 1, run
    assert calls[0].split("audit-new-advisories.sh", 1)[1].split() == list(LOCKS)
    assert "$RUNNER_TEMP/tooling/bin" in calls[0]
    # Bypassing the script with a direct call would put back the old rule.
    assert "pip-audit" not in run.replace("audit-new-advisories", "")
