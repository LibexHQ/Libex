#!/usr/bin/env bash
# Audits each lock file and fails only on advisories this change introduces.
# An advisory already present on the base ref is reported, not blocking: it
# was published against something that already shipped, and it gets fixed
# through the normal dependency update path rather than by holding up every
# unrelated change until a fix clears the cooldown.
#
# Usage: scripts/audit-new-advisories.sh <lock>...
# Base ref: LIBEX_AUDIT_BASE, default origin/dev. Run from the repo root, with
# the tooling venv's pip-audit first on PATH.
#
# Each lock is audited on its own: the three locks are deliberately
# unsatisfiable together, so they can never go through one pip-audit call.
#
# "New" is decided per package by advisory id, ignoring version, so bumping
# certifi to a release that is still affected by an advisory the base already
# had is not a new finding, while a new id on the same package is. An id also
# counts as known when it matches one of the base finding's aliases, so the
# same vulnerability reported as PYSEC on one side and GHSA on the other does
# not read as new.
#
# Anything short of a clean, complete audit fails, on either side: a
# pip-audit crash, unreadable output, a skipped package, or a base ref that
# does not resolve. Without a trustworthy base there is no way to tell new
# from known, so it fails closed.
set -euo pipefail

base="${LIBEX_AUDIT_BASE:-origin/dev}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

if ! git rev-parse --verify --quiet "$base^{commit}" >/dev/null; then
    echo "audit base $base does not resolve to a commit; fetch it first" >&2
    exit 1
fi

# Same flags on both sides. --require-hashes matches how CI installs the
# locks, and --strict makes a package pip-audit cannot collect fail the run
# instead of being skipped quietly.
audit() { # <requirements file> <json out>
    rm -f "$2"
    local rc=0
    pip-audit --strict --require-hashes -f json -r "$1" -o "$2" >/dev/null 2>"$tmp/err" || rc=$?
    # 0 is clean and 1 is findings; the parser below checks the output agrees.
    if [[ $rc -gt 1 || ! -s "$2" ]]; then
        echo "pip-audit failed on $1 (exit $rc):" >&2
        cat "$tmp/err" >&2
        exit 1
    fi
    echo "$rc" > "$2.rc"
}

status=0
for lock in "$@"; do
    audit "$lock" "$tmp/now.json"
    if git cat-file -e "$base:$lock" 2>/dev/null; then
        git show "$base:$lock" > "$tmp/base.lock"
        audit "$tmp/base.lock" "$tmp/was.json"
    else
        echo '{"dependencies": []}' > "$tmp/was.json"
        echo 0 > "$tmp/was.json.rc"
    fi
    python3 - "$lock" "$base" "$tmp/now.json" "$tmp/was.json" <<'PY' || status=1
import json
import sys

lock, base, now_path, was_path = sys.argv[1:]


def load(path):
    # A crash that still wrote a file, or exited 1 with no findings, is not
    # an audit result.
    try:
        deps = json.load(open(path))["dependencies"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        sys.exit(f"{lock}: unreadable pip-audit output: {exc!r}")
    findings = []
    for dep in deps:
        if "skip_reason" in dep:
            sys.exit(f"{lock}: pip-audit skipped {dep.get('name')}: {dep['skip_reason']}")
        for vuln in dep.get("vulns", []):
            findings.append((dep["name"].lower(), dep["version"], vuln["id"], set(vuln.get("aliases", []))))
    rc = int(open(path + ".rc").read())
    if rc != (1 if findings else 0):
        sys.exit(f"{lock}: pip-audit exit {rc} disagrees with its own findings")
    return findings


now, was = load(now_path), load(was_path)
known_ids = {}
for name, _, vid, aliases in was:
    known_ids.setdefault(name, set()).update({vid} | aliases)

known, new = [], []
for name, version, vid, aliases in sorted(now):
    line = f"  {name}=={version} {vid}"
    (known if ({vid} | aliases) & known_ids.get(name, set()) else new).append(line)

if known:
    print(f"{lock}: already on {base} (not blocking, update through the normal path):")
    print("\n".join(known))
if new:
    print(f"{lock}: NEW advisories introduced by this change:", file=sys.stderr)
    print("\n".join(new), file=sys.stderr)
    sys.exit(1)
print(f"{lock}: no new advisories")
PY
done
exit $status
