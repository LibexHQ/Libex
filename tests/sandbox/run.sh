#!/usr/bin/env bash
# Runs tests/sandbox against a throwaway Libex built from the commit at HEAD:
# its own image, its own Postgres, its own compose project. Nothing here ever
# talks to a server, a registry push, FTPS, or the real libex stack.
#
#   tests/sandbox/run.sh empty    [--log-level DEBUG] [--smoke]
#   tests/sandbox/run.sh snapshot --dump PATH --dump-sha256 HEX \
#                                 --rollback-image REF@sha256:DIGEST [--log-level DEBUG] [--smoke]
#
# empty     a fresh volume, migrated from base by the image's entrypoint.
# snapshot  a backup dump, fetched by hand into ~/libex-sandbox/ (0700 dir,
#           0600 file), restored before the API starts so the new migrations
#           run on real data and are timed. After the tests pass, rehearses the
#           rollback: stamp back to PROD_HEAD, run the production image on the
#           same volume, upgrade again. The dump is deleted on exit.
# --smoke   pre-flight, build, start, /health, teardown. No tests, no Audible.
#
# What pytest gets, and nothing more: LIBEX_SANDBOX_URL, _PROJECT, _COMPOSE
# (the docker invocation as a shell-quoted string, no secret in it), _MODE,
# _AUDIBLE_BUDGET, and the two tokens, _TOKEN (ingest) and _SEED_TOKEN, which
# are prefix-assigned on the pytest command alone. A test that needs the API
# with different settings appends `--env-file <its own file>` to _COMPOSE; a
# later env file wins, and its values are not re-checked by the pre-flight.
#
# Log conventions the tests have to follow: every canary starts SBXCANARY- and
# must never be logged; the user-agent positive control starts SBXUA- and must
# be. Set LIBEX_SANDBOX_WRAP to a command that heavy steps (build, restore, pytest) run under, such as a memory gate.
set -euo pipefail
umask 077

die() { printf 'sandbox: %s\n' "$*" >&2; exit 1; }
say() { printf 'sandbox: %s\n' "$*"; }

# Tokens live in shell variables in this process; a trace would print them.
[[ $- == *x* ]] && die "refusing to run with xtrace on"

PROD_HEAD=3c8e5a1f7d94      # main's alembic head, what a production dump is at
AUDIBLE_BUDGET=120
read -r -a HEAVY <<< "${LIBEX_SANDBOX_WRAP:-}"
PROXIES='"", "http://127.0.0.1:9", "http://192.0.2.1:8888"'

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
compose_file="$here/compose.yml"

mode=${1:-}
[[ $mode == empty || $mode == snapshot ]] || die "usage: run.sh empty|snapshot [options]"
shift
log_level=INFO smoke=0 dump="" dump_sha="" rollback_image="" own_dump=""
while (($#)); do
    case $1 in
        --log-level) log_level=${2:?}; shift 2 ;;
        --smoke) smoke=1; shift ;;
        --dump) dump=${2:?}; shift 2 ;;
        --dump-sha256) dump_sha=${2:?}; shift 2 ;;
        --rollback-image) rollback_image=${2:?}; shift 2 ;;
        *) die "unknown option $1" ;;
    esac
done
[[ $log_level == INFO || $log_level == DEBUG ]] || die "--log-level is INFO or DEBUG"
if [[ $mode == snapshot ]]; then
    [[ -n $dump && $dump_sha =~ ^[0-9a-f]{64}$ ]] || die "snapshot needs --dump and --dump-sha256"
    [[ $rollback_image =~ @sha256:[0-9a-f]{64}$ ]] || die "snapshot needs --rollback-image pinned by digest"
else
    [[ -z $dump && -z $rollback_image ]] || die "--dump and --rollback-image are snapshot-only"
fi

# ---------------------------------------------------------------- pre-flight

# Compose loads an override file and .env from the project directory on its
# own; neither may exist here, whatever else is passed.
leftover=$(find "$here" -maxdepth 1 \( -name '*compose.override.y*ml' -o -name '.env*' \) -print -quit)
[[ -z $leftover ]] || die "refusing: $leftover sits in tests/sandbox"

# The sandbox triggers are created by psql only and named sbx_; one in the
# product code would mean it ships.
if git -C "$repo" grep -nE 'sbx_|B0SBX|pg_sleep' -- migrations app libex_core scripts; then
    die "refusing: sandbox trigger names found in product code (above)"
fi

tmp=$(mktemp -d /tmp/libex-sandbox.XXXXXX)
[[ $tmp != "$repo"* ]] || die "temp dir inside the repo"
env_file="$tmp/sandbox.env"
: >"$env_file"

# Every docker call goes through these. env -i drops the caller's environment,
# because a shell variable beats --env-file and COMPOSE_PROJECT_NAME beats name:.
D=(env -i "PATH=$PATH" "HOME=$HOME" docker --context default)
DC=("${D[@]}" compose -p libex-sandbox -f "$compose_file" --env-file "$env_file")
psql_() { "${DC[@]}" exec -T db psql -U libex -d libex -v ON_ERROR_STOP=1 -Atq "$@"; }

image="" image_built=0 rollback_pulled=0
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    set +e
    if [[ -s $env_file ]]; then
        "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1
    fi
    ((rollback_pulled)) && "${D[@]}" image rm "$rollback_image" >/dev/null 2>&1
    ((image_built)) && "${D[@]}" image rm "$image" >/dev/null 2>&1
    rm -rf "$tmp"
    # Only a dump that passed every check below is ours to delete; until then
    # --dump is whatever path was typed, and could be the master copy.
    [[ -n $own_dump ]] && rm -f "$own_dump"
    local kind left
    for kind in container volume network; do
        if [[ $kind == container ]]; then
            left=$("${D[@]}" ps -aq --filter label=com.docker.compose.project=libex-sandbox)
        else
            left=$("${D[@]}" "$kind" ls -q --filter label=com.docker.compose.project=libex-sandbox)
        fi
        if [[ -n $left ]]; then
            printf 'sandbox: teardown left %s(s): %s\n' "$kind" "$left" >&2
            status=1
        fi
    done
    ((status == 0)) && say "PASS, torn down" || say "FAIL ($status), torn down"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

endpoint=$("${D[@]}" context inspect default --format '{{.Endpoints.docker.Host}}')
[[ $endpoint == unix:///var/run/docker.sock ]] || die "refusing: default context is $endpoint"
driver=$("${D[@]}" info --format '{{.LoggingDriver}}')
[[ $driver == json-file || $driver == local ]] || die "refusing: log driver $driver"
[[ -z $("${D[@]}" ps -aq --filter label=com.docker.compose.project=libex-sandbox) ]] \
    || die "refusing: libex-sandbox containers already exist"

# Generated here, not by the router's __main__, which imports the app, reads
# .env and prints a curl line. Only the digests are ever written down.
read -r ingest_token ingest_digest seed_token seed_digest < <(python3 -I -c '
import hashlib, secrets
for _ in range(2):
    t = secrets.token_urlsafe(32)
    print(t, hashlib.sha256(t.encode()).hexdigest(), end=" ")
print()')
[[ ${#ingest_token} == 43 && ${#seed_token} == 43 && $ingest_digest != "$seed_digest" ]] \
    || die "token generation failed"

# --------------------------------------------------------------------- image

sha=$(git -C "$repo" rev-parse HEAD)
image="libex:sandbox-${sha:0:7}"
say "building $image from $sha"
# git archive is the commit and nothing else: no untracked file, no .env, the
# same tree publish.yml checks out. Unpacked to a directory rather than piped
# to `build -`, because a tar on stdin skips .dockerignore and ships tests/.
mkdir "$tmp/src"
git -C "$repo" archive --format=tar "$sha" | tar -x -C "$tmp/src"
image_built=1
"${HEAVY[@]}" "${D[@]}" build -t "$image" "$tmp/src" >"$tmp/build.log" 2>&1 \
    || { tail -n 30 "$tmp/build.log" >&2; die "image build failed"; }
rm -rf "$tmp/src"
image_id=$("${D[@]}" image inspect --format '{{.Id}}' "$image")
say "image $image_id"

check_cmd() {
    "${D[@]}" image inspect --format '{{json .Config.Cmd}}' "$1" | grep -qF '"--no-access-log"' \
        || die "refusing: $1 CMD lacks --no-access-log"
}
check_cmd "$image"
"${D[@]}" run --rm --network none --entrypoint sh "$image" -c '
    test ! -e /app/.env || { echo "/app/.env"; exit 1; }
    test ! -e /app/tests || { echo "/app/tests"; exit 1; }
    hits=$(find /app -iname "*sandbox*"; grep -rlE "sbx_|B0SBX|SBXCANARY|SBXUA-" /app 2>/dev/null)
    test -z "$hits" || { echo "$hits"; exit 1; }' \
    || die "refusing: the image carries something it must not (above)"

printf '%s\n' "LIBEX_SANDBOX_IMAGE=$image" "LIBEX_SANDBOX_INGEST_DIGEST=$ingest_digest" \
    "LIBEX_SANDBOX_SEED_DIGEST=$seed_digest" "LIBEX_SANDBOX_LOG_LEVEL=$log_level" >"$env_file"

# Checks the resolved config in memory; it holds the digests, so it is never
# printed. Prints the published port and nothing else.
# check_config EXPECTED_IMAGE [extra compose args, e.g. --env-file FILE]
check_config() {
    local want=$1
    shift
    "${DC[@]}" "$@" config --format json | python3 -I -c '
import json, sys
image, proxies = sys.argv[1], json.loads("[" + sys.argv[2] + "]")
c = json.load(sys.stdin)
def fail(m): sys.exit("refusing: compose config: " + m)
if c.get("name") != "libex-sandbox": fail("project name")
s = c.get("services") or {}
if set(s) != {"api", "db"}: fail("services are %s" % sorted(s))
banned = {"container_name", "network_mode", "pid", "ipc", "privileged", "cap_add",
          "devices", "extra_hosts", "env_file", "volumes_from"}
for name, svc in s.items():
    for key in banned & set(svc): fail("%s sets %s" % (name, key))
    for v in svc.get("volumes") or []:
        if v.get("type") != "volume": fail("%s mounts a %s" % (name, v.get("type")))
    for p in svc.get("ports") or []:
        if p.get("host_ip") != "127.0.0.1": fail("%s publishes off loopback" % name)
    if set(svc.get("networks") or {}) - {"default"}: fail("%s joins another network" % name)
    if (svc.get("logging") or {}).get("driver") != "json-file": fail("%s logging" % name)
    for k, v in (svc.get("environment") or {}).items():
        if not isinstance(v, str): fail("%s env %s has no =value" % (name, k))
for kind in ("networks", "volumes"):
    for k, v in (c.get(kind) or {}).items():
        if (v or {}).get("external"): fail("external %s %s" % (kind, k))
if s["db"].get("ports"): fail("db publishes a port")
api = s["api"]
if api.get("command") is not None or api.get("entrypoint") is not None: fail("api overrides command or entrypoint")
if api.get("image") != image: fail("api image is not " + image)
if api.get("pull_policy") != "never": fail("api pull_policy")
env = api["environment"]
# config folds an env_file: into environment and drops the key, so the
# names themselves are pinned: nothing can arrive that compose.yml does not list.
if set(env) != {"DATABASE_URL", "CACHE_ENABLED", "CACHE_TTL", "DEFAULT_REGION",
                "WEB_CONCURRENCY", "LOG_RETENTION_DAYS", "LOG_LEVEL", "AXIOM_TOKEN",
                "SEED_SECRET", "INGEST_SECRET", "AUDIBLE_PROXY_URL",
                "MIGRATION_NOTICE_ENABLED"}: fail("api env names differ from compose.yml")
if set(s["db"].get("environment") or {}) != {"POSTGRES_DB", "POSTGRES_USER",
                                             "POSTGRES_PASSWORD"}: fail("db env names")
if env.get("AXIOM_TOKEN") != "": fail("AXIOM_TOKEN is not empty")
if env.get("AUDIBLE_PROXY_URL") not in proxies: fail("proxy not in the allowed set")
if env.get("WEB_CONCURRENCY") != "6": fail("WEB_CONCURRENCY")
ports = api.get("ports") or []
if len(ports) != 1: fail("api ports")
print(ports[0]["published"])' "$want" "$PROXIES"
}
port=$(check_config "$image")
url="http://127.0.0.1:$port"

health() {
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$url/health") || true
    [[ $code == 200 ]] || die "/health returned ${code:-nothing}"
}

# Every Audible API host has to resolve before anything starts, or a resolver
# fault (a SERVFAIL for one TLD) reads as failed tests. Resolved in a one-off
# api container on the sandbox network, which is the DNS the app really uses,
# and the hosts come from the image's own REGION_MAP, so the list can't drift.
# A name lookup only: nothing is sent to Audible.
"${DC[@]}" run --rm --no-deps -T --entrypoint python api -c '
import socket, sys
from libex_core.audible.client import REGION_MAP
hosts = sorted({"api.audible" + tld for tld in REGION_MAP.values()})
if len(hosts) != len(REGION_MAP):
    sys.exit("refusing: REGION_MAP has %d regions but %d hosts" % (len(REGION_MAP), len(hosts)))
bad = []
for host in hosts:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        bad.append("%s (%s)" % (host, exc))
if bad:
    sys.exit("refusing: does not resolve from the sandbox network: " + ", ".join(bad))
print("sandbox: all %d Audible API hosts resolve" % len(hosts))' \
    || die "fix DNS for the hosts above before running; this is the environment, not the tests"

# ------------------------------------------------------------------ restore

if [[ $mode == snapshot ]]; then
    real=$(realpath -e "$dump") || die "no dump at $dump"
    [[ $real == "$HOME/libex-sandbox/"* ]] || die "the dump must sit in ~/libex-sandbox/"
    [[ $(stat -c %a "$HOME/libex-sandbox") == 700 && $(stat -c %a "$real") == 600 ]] \
        || die "~/libex-sandbox must be 0700 and the dump 0600"
    dump=$real
    [[ $(sha256sum "$dump" | cut -d' ' -f1) == "$dump_sha" ]] || die "dump sha256 mismatch"
    own_dump=$dump
    say "dump $dump sha256 $dump_sha"

    "${DC[@]}" up -d --wait db
    dump_kb=$(($(stat -c %s "$dump") / 1024 + 1))
    free_kb=$("${DC[@]}" exec -T db df -Pk /var/lib/postgresql/data | awk 'NR == 2 {print $4}')
    ((free_kb >= 10 * dump_kb)) || die "need ${dump_kb}0 KiB free for the restore, have $free_kb"

    "${DC[@]}" cp "$dump" db:/tmp/libex.dump
    "${DC[@]}" exec -T db pg_restore --list /tmp/libex.dump >"$tmp/dump.list"
    # Object types the migrations create, and nothing else: a function,
    # trigger, extension or server in a dump would run inside the sandbox.
    python3 -I -c '
import re, sys
allowed = re.compile(r"(SCHEMA|TYPE|TABLE|TABLE DATA|SEQUENCE|SEQUENCE OWNED BY|SEQUENCE SET"
                     r"|DEFAULT|CONSTRAINT|FK CONSTRAINT|INDEX|COMMENT - SCHEMA|ACL - SCHEMA) ")
bad = {}
for line in open(sys.argv[1]):
    if not line.strip() or line.startswith(";"):
        continue
    m = re.match(r"\d+; \d+ \d+ (.*)", line)
    if m is None:
        # a shape this does not know is refused, never skipped past
        bad["unparseable line"] = bad.get("unparseable line", 0) + 1
        continue
    rest = m.group(1)
    if not allowed.match(rest) or re.match(r"(DEFAULT ACL|TABLE ATTACH) ", rest):
        kind = " ".join(rest.split()[:2])
        bad[kind] = bad.get(kind, 0) + 1
if bad:
    sys.exit("refusing: dump holds object types outside the allowlist: %s" % bad)' "$tmp/dump.list"

    say "restoring"
    start=$SECONDS
    "${HEAVY[@]}" "${DC[@]}" exec -T db pg_restore -U libex -d libex --no-owner --no-privileges \
        --exit-on-error -j 4 /tmp/libex.dump
    "${DC[@]}" exec -T db rm -f /tmp/libex.dump
    "${HEAVY[@]}" "${DC[@]}" exec -T db vacuumdb -U libex -d libex --analyze --quiet
    say "restored in $((SECONDS - start))s, pg_database_size $(psql_ -c "SELECT pg_size_pretty(pg_database_size('libex'))")"

    at=$(psql_ -c "SELECT version_num FROM alembic_version")
    [[ $at == "$PROD_HEAD" ]] || die "dump is at revision $at, expected $PROD_HEAD"

    # The upgrade runs every pending revision in one transaction, so a lock
    # taken early is held to the end. A second session reads books under a 2s
    # statement_timeout once a second while it runs; every refusal is time the
    # deploy leaves books unreadable.
    say "timing alembic upgrade head"
    start=$SECONDS
    "${DC[@]}" run --rm --no-deps -T api alembic upgrade head >"$tmp/upgrade.log" 2>&1 &
    upgrade=$!
    blocked=0 probes=0 locked=0
    while kill -0 "$upgrade" 2>/dev/null; do
        probes=$((probes + 1))
        psql_ -c "SET statement_timeout = '2s'" -c "SELECT 1 FROM books LIMIT 1" >/dev/null 2>&1 \
            || blocked=$((blocked + 1))
        seen=$(psql_ -c "SELECT count(*) > 0 FROM pg_locks
                  WHERE relation = 'books'::regclass AND mode = 'AccessExclusiveLock'" \
            2>/dev/null) || true
        [[ $seen == t ]] && locked=$((locked + 1))
        sleep 1
    done
    wait "$upgrade" || { cat "$tmp/upgrade.log" >&2; die "alembic upgrade head failed"; }
    say "upgrade took $((SECONDS - start))s; books unreadable on $blocked of $probes probes;" \
        "AccessExclusiveLock on books seen on $locked of $probes probes"
fi

# ---------------------------------------------------------------------- up

"${DC[@]}" up -d --wait --wait-timeout 300
health
say "up at $url, /health 200"
((smoke)) && exit 0

# ------------------------------------------------------------------- pytest

cd "$repo"
export LIBEX_SANDBOX_URL="$url" LIBEX_SANDBOX_PROJECT=libex-sandbox LIBEX_SANDBOX_MODE="$mode" \
    LIBEX_SANDBOX_AUDIBLE_BUDGET="$AUDIBLE_BUDGET"
LIBEX_SANDBOX_COMPOSE=$(printf '%q ' "${DC[@]}")
export LIBEX_SANDBOX_COMPOSE
# No -l, no junit, no cache, no live log: each is a place a token could be written.
set +e
LIBEX_SANDBOX_TOKEN="$ingest_token" LIBEX_SANDBOX_SEED_TOKEN="$seed_token" \
    "${HEAVY[@]}" python3 -m pytest tests/sandbox/ -m sandbox -p no:cacheprovider -o log_cli=false \
    --tb=short -rs 2>&1 | tee "$tmp/pytest.out"
tests_status=${PIPESTATUS[0]}
set -e
failed=0
((tests_status == 0)) || { say "pytest exited $tests_status"; failed=1; }
# A skip, or a test left out by the marker, would read as green without running.
if grep -qE '[0-9]+ (skipped|deselected|xfailed|xpassed)' "$tmp/pytest.out"; then
    say "pytest skipped or deselected tests"
    failed=1
fi

# ---------------------------------------------------------------- log greps

# Before teardown, while the logs still exist. The patterns are read from
# files, never argv, and a hit is reported by label, never by value.
"${DC[@]}" logs --no-color api db >"$tmp/compose.log" 2>&1
"${DC[@]}" cp api:/app/logs "$tmp/applogs" >/dev/null
gateway=$("${D[@]}" network inspect libex-sandbox_default \
    --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}')
mkdir "$tmp/patterns"
pattern() { printf '%s\n' "$2" >"$tmp/patterns/$1"; }
pattern ingest-token "$ingest_token"
pattern seed-token "$seed_token"
pattern ingest-digest "$ingest_digest"
pattern seed-digest "$seed_digest"
pattern canary SBXCANARY-
pattern bearer Bearer
pattern test-net-ip 203.0.113.77
for ip in $gateway; do pattern "gateway-$ip" "$ip"; done
for f in "$tmp"/patterns/*; do
    # grep exits 1 for no match; 2 means a file was missing or unreadable,
    # which is not the same as clean.
    counts=$(grep -rFc -f "$f" "$tmp/compose.log" "$tmp/applogs" "$tmp/pytest.out") && rc=0 || rc=$?
    ((rc <= 1)) || { say "leak grep for ${f##*/} failed (exit $rc)"; failed=1; }
    hits=$(grep -v ':0$' <<<"$counts" || true)
    [[ -z $hits ]] || { say "leak: ${f##*/} found in: ${hits//$tmp\//}"; failed=1; }
done
# Positive controls: a grep that finds nothing proves nothing unless the
# things that should be logged are there.
for want in 'Book entry complete' request_id SBXUA-; do
    grep -rqF -- "$want" "$tmp/applogs" || { say "positive control missing: $want"; failed=1; }
done
probes=$(cat "$tmp"/applogs/* | grep -cF 'Probed Audible listing' || true)
say "Audible probes logged: $probes (a lower bound; budget $AUDIBLE_BUDGET)"
((probes <= AUDIBLE_BUDGET)) || failed=1
((failed == 0)) || die "FAIL before the rollback rehearsal"

# --------------------------------------------------------- rollback rehearsal

if [[ $mode == snapshot ]]; then
    say "rollback rehearsal against $rollback_image"
    [[ $(psql_ -c "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'sbx\_%'") == 0 &&
        $(psql_ -c "SELECT count(*) FROM pg_proc WHERE proname LIKE 'sbx\_%'") == 0 ]] \
        || die "sandbox triggers or functions are still in the database"
    manual() {
        psql_ -c "COPY (SELECT asin, region, manual_entry_at FROM books
                  WHERE manual_entry_at IS NOT NULL ORDER BY asin, region) TO STDOUT WITH CSV"
    }
    manual >"$tmp/manual-before.csv"

    if ! "${D[@]}" image inspect "$rollback_image" >/dev/null 2>&1; then
        "${D[@]}" pull -q "$rollback_image" >/dev/null
        rollback_pulled=1
    fi
    check_cmd "$rollback_image"
    printf '%s\n' "LIBEX_SANDBOX_IMAGE=$rollback_image" >"$tmp/rollback.env"
    check_config "$rollback_image" --env-file "$tmp/rollback.env" >/dev/null

    # Stamp, not downgrade: both new revisions are IF NOT EXISTS and the old
    # ORM ignores the extra column, so nothing is lost.
    "${DC[@]}" stop -t 30 api
    "${DC[@]}" run --rm --no-deps -T api alembic stamp "$PROD_HEAD"
    "${DC[@]}" --env-file "$tmp/rollback.env" up -d --wait --wait-timeout 300 api
    health
    region=$(psql_ -c "SELECT region FROM books LIMIT 1")
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "$url/db/book?region=$region&limit=1")
    [[ $code == 200 ]] || die "rollback image: /db/book returned $code"
    say "rollback image serves /health and /db/book"

    "${DC[@]}" up -d --wait --wait-timeout 300 api
    health
    head=$("${DC[@]}" run --rm --no-deps -T api alembic heads | awk '{print $1; exit}')
    [[ $(psql_ -c "SELECT version_num FROM alembic_version") == "$head" ]] \
        || die "re-upgrade did not reach $head"
    manual >"$tmp/manual-after.csv"
    cmp -s "$tmp/manual-before.csv" "$tmp/manual-after.csv" \
        || die "manual_entry_at rows changed across the rollback"
    say "rollback rehearsal green: $(grep -c . "$tmp/manual-before.csv" || true) manual rows kept"
fi
