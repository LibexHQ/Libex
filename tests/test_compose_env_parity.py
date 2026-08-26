"""
Structural parity between app/core/config.py, docker-compose.yml and
.env.example -- three files that must agree on WHICH settings exist, with
nothing that fails loudly if any one of them drifts from the other two.

Two real incidents motivate this, neither of which involved anyone editing
the server directly:

1. `9e76b02` deleted AXIOM_*, SEEDER_*, AUDIBLE_PROXY_URL and SEED_SECRET
   from docker-compose.yml's `environment:` block. Two names came back later,
   two did not.
2. The five MIGRATION_* settings landed in app/core/config.py in #184 and
   have never existed in docker-compose.yml or .env.example --
   `git log -S MIGRATION_NOTICE_ENABLED -- docker-compose.yml .env.example`
   returns nothing. On a repo-based deploy the migration notice's disabled
   branch deliberately doesn't log (off is the normal self-hosted case), so
   this was silent.

Both are possible because pydantic supplies a default for every Settings
field: forgetting to wire a setting into compose, or forgetting to remove a
retired one, never crashes anything. This test is the mechanical check that
RETIRED_ENV_VARS / check_retired_env_vars() does not cover -- that function
warns about names removed from code but still set in the environment; there
is no mirror for names added to code and never plumbed through. This is that
mirror, plus the .env.example half.

The seeder used to run inside every libex API worker, then briefly as its own
service in this same file behind a `seeder` compose profile. It is now a
wholly separate Compose project, `docker-compose.seeder.yml`, deployed as its
own standalone Portainer stack the way the chapter backfill and corpus
refresh scripts already are -- a profile-gated service pasted into a stack of
its own deploys nothing at all (`services: {}`, exit 0), which is exactly the
failure a profile-only libex-seeder would have reproduced. Its five SEEDER_*
settings live entirely in docker-compose.seeder.yml's own `environment:`
block now, in a file with no libex service at all. Directions 1 and 3 below
therefore reason about BOTH files -- docker-compose.yml's libex service and
docker-compose.seeder.yml's libex-seeder service -- not one file alone, since
a setting or a knob can legitimately live in either.

Three directions:

1. `test_settings_configurable_via_compose` (config.py -> compose): every
   Settings field an operator is meant to be able to set has a matching name
   in the `environment:` block of libex (docker-compose.yml) OR libex-seeder
   (docker-compose.seeder.yml) -- the union of both, since either service is
   a valid place for a setting to be configurable. Would have caught the
   MIGRATION_* gap. A union alone cannot tell "on the right service" from "on
   the wrong one, or on both", which is why
   `test_seeder_only_settings_are_scoped_to_seeder_service` exists alongside
   it: every name in SEEDER_ONLY_SETTINGS must be present on libex-seeder AND
   absent from libex, restoring the per-service catch the union gives up.
2. `test_env_example_names_are_reachable_in_compose` (.env.example ->
   compose): every name in .env.example is either an environment key or an
   interpolation token (`${NAME...}`) somewhere in docker-compose.yml OR
   docker-compose.seeder.yml. Would have caught the 9e76b02 deletions.
   Changed by the seeder move, unlike the other two directions: the SEEDER_*
   names' `${NAME:-default}`-style references no longer appear anywhere in
   docker-compose.yml's text at all, so this direction now has to scan both
   files' text, not one.
3. `test_compose_environment_has_no_dead_knobs` (compose -> config.py): every
   name in the `environment:` block of libex (docker-compose.yml) OR
   libex-seeder (docker-compose.seeder.yml) is either a Settings field or an
   explicitly documented non-Settings knob, checked per service so a dead
   knob is attributed to the service that actually carries it. Would have
   caught PORT being passed for months while uvicorn reads only
   WEB_CONCURRENCY and FORWARDED_ALLOW_IPS, and the Dockerfile CMD hardcodes
   `--port 3333`. Extended to libex-seeder on the same reasoning as direction
   1: an unused knob on the seeder container is the same defect as one on the
   API container, regardless of which file carries it.

Honest limit, stated once here rather than at each call site: this compares
NAME SETS only. It has no opinion on values, hosts, or paths -- it would not
catch the healthcheck pointing at the wrong host, a volume bound to the wrong
directory, or the `/app/data` mount that exposed the Postgres data directory
to the API container. A green run here means the files agree on which
settings exist and, for the five SEEDER_ONLY_SETTINGS, on which service they
belong to. It says nothing about whether they are wired to the right values,
and nothing about any Settings field outside that named set being on its
"correct" service -- only the five explicitly listed seeder-only names get
that per-service check; a hypothetical future setting shared by both
services, or one that should move but doesn't have a SEEDER_ONLY_SETTINGS
entry, is invisible to this file until someone adds it there.

A separate blind spot exists in principle: a name that appears only as a
`${NAME...}` interpolation token, is never a Settings field (so directions 1
and 3 never see it, since they only look at Settings fields and
`environment:` blocks) and is never assigned in .env.example (so direction 2
never sees it either, since it only iterates names .env.example actually
assigns) would be invisible to every direction in this file at once. DB_HOST
used to be exactly that name, required by docker-compose.seeder.yml's
DATABASE_URL, until the seeder started reaching postgres by container name
over a shared network instead and DB_HOST left both compose files' text
entirely -- at which point a docstring paragraph describing it as a
deliberate, named exception kept describing it, with nothing here to notice
that the variable it described no longer existed anywhere. DB_BIND
(docker-compose.yml's `ports: ["${DB_BIND:-127.0.0.1}:5432:5432"]`) looked
like it might become the next such name, but it is documented in
.env.example, which means direction 2 already confirms it resolves to a
real interpolation reference -- it is not in this blind spot. As of this
writing no name in either compose file actually sits outside all three
directions at once; if one appears, the incident above is the shape to
watch for: a docstring naming an exception with nothing mechanical tying
that name to its continued existence.

Three deliberate, narrow exceptions that check a value, not just a name:

- `test_web_concurrency_is_a_hardened_literal` below, because for
  WEB_CONCURRENCY specifically a value regression -- `- WEB_CONCURRENCY=6`
  quietly becoming `- WEB_CONCURRENCY=${WEB_CONCURRENCY:-6}` again -- is
  invisible to all three name-only directions above; they would pass
  identically either way. That same test also asserts WEB_CONCURRENCY is
  absent from libex-seeder entirely, since the seeder runs no uvicorn and
  the variable would govern nothing there. See that test's docstring for why
  the value matters.
- `test_seeder_only_settings_are_scoped_to_seeder_service` above, which is a
  presence/absence check per service rather than a name-set union, for the
  reason given under direction 1.
- `test_shared_defaults_match_across_compose_files` below, for the handful
  of names in SHARED_DEFAULTS that are meant to be configurable identically
  on both libex and libex-seeder. Every direction above only checks that a
  name is present somewhere; none of them would notice the two files
  quietly drifting to different default values for the same name.
"""

# Standard library
import re
from pathlib import Path

# Third party
import yaml

# Local
from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
# The seeder's own standalone stack -- a separate Compose project, never
# combined with COMPOSE_PATH by `-f`, per docker-compose.seeder.yml's own
# header. Every direction below that used to read a single file now reads
# this one too wherever the seeder's own service or environment matters.
SEEDER_COMPOSE_PATH = REPO_ROOT / "docker-compose.seeder.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"

# Settings fields deliberately NOT operator-configurable through
# docker-compose.yml. This is the written record of that decision -- an
# entry here without a real reason is exactly how the test above it stops
# catching things, so every reason cites where it was checked.
NOT_OPERATOR_CONFIGURABLE: dict[str, str] = {
    "app_name": (
        "settings.app_name is never read anywhere in app/ -- the FastAPI "
        "title is the hardcoded literal 'Libex' in app/main.py."
    ),
    "app_version": (
        "the CHANGELOG-driven release version (app/main.py, /health) -- "
        "not something a deployment varies independently of the image."
    ),
    "debug": (
        "read once, in app/core/logging.py. docker-compose.yml's "
        "`environment:` block deliberately omits it (see its own leading "
        "comment); it stays in .env.example only for the case of running "
        "the app directly against a local .env file, outside Docker, where "
        "Settings' env_file=\".env\" picks it up without compose at all."
    ),
    "host": (
        "never read anywhere in app/ -- the Dockerfile CMD hardcodes "
        "'--host 0.0.0.0'. Present in .env.example for the same "
        "outside-Docker, direct-.env-file case as debug."
    ),
    "port": (
        "never read anywhere in app/ -- the Dockerfile CMD hardcodes "
        "'--port 3333' and docker-compose.yml's own comment explains PORT "
        "is the published *host* port, deliberately not part of the "
        "environment block that reaches the app."
    ),
    "database_echo": (
        "the SQLAlchemy engine echo flag (app/db/session.py) -- a debugging "
        "knob, absent from both compose and .env.example."
    ),
    "db_password": (
        "never reaches the libex container directly. Only DATABASE_URL "
        "does, built from DB_USER/DB_PASSWORD/DB_NAME by compose's own "
        "interpolation; DB_PASSWORD itself is passed to the postgres "
        "service as POSTGRES_PASSWORD, a different name."
    ),
}

# Names in docker-compose.yml's libex `environment:` block that are real,
# used variables but never a Settings field, because they are consumed
# outside pydantic entirely.
# Settings fields that are operator-configurable only through
# docker-compose.seeder.yml's libex-seeder service `environment:` block,
# never docker-compose.yml's libex service. app/services/seeder.py is the
# only reader of any of these and only libex-seeder runs it. Direction 1
# (test_settings_configurable_via_compose) checks the UNION of libex's and
# libex-seeder's environment blocks across both files, so on its own it
# cannot tell "configurable on the right service" from "configurable on the
# wrong one, or on both". This set is what
# test_seeder_only_settings_are_scoped_to_seeder_service checks instead:
# every name here must be present on libex-seeder (docker-compose.seeder.yml)
# AND absent from libex (docker-compose.yml).
SEEDER_ONLY_SETTINGS: set[str] = {
    "SEEDER_INTERVAL_HOURS",
    "SEEDER_REQUEST_DELAY",
    "SEEDER_REGIONS",
    "SEEDER_NEW_RELEASES_INTERVAL_HOURS",
    "SEEDER_REFRESH_ENABLED",
}

# Settings fields that are shared between libex and libex-seeder and must
# render to the identical value in both compose files' `environment:`
# blocks, not merely be present in both. CACHE_TTL is the load-bearing one:
# both services read and write the same cache table, so a divergent TTL
# does not fail loudly, it just makes one service expire rows the other
# still expects to be live. The rest (LOG_RETENTION_DAYS, LOG_LEVEL,
# AXIOM_TOKEN, AXIOM_DATASET) share the same reasoning at lower stakes -- one
# logging pipeline, one retention policy, one Axiom destination, so the two
# services silently disagreeing about any of them is drift, not a deliberate
# per-service choice the way the WEB_CONCURRENCY or SEEDER_* differences are.
SHARED_DEFAULTS: set[str] = {
    "CACHE_TTL",
    "LOG_RETENTION_DAYS",
    "LOG_LEVEL",
    "AXIOM_TOKEN",
    "AXIOM_DATASET",
}

COMPOSE_KNOBS_WITHOUT_SETTINGS: dict[str, str] = {
    "WEB_CONCURRENCY": (
        "read directly from the OS environment by uvicorn (0.46.0 checks "
        "WEB_CONCURRENCY only when --workers is absent) -- never a Settings "
        "field. See docker-compose.yml's own comment block and the "
        "Dockerfile CMD comment. Also deliberately fixed rather than "
        "operator-tunable -- every per-process budget in the codebase is "
        "sized against this exact number rather than derived from it at "
        "runtime; see test_web_concurrency_is_a_hardened_literal below."
    ),
}


def _settings_field_names() -> set[str]:
    """Every Settings field name, uppercased to match env-var convention."""
    return {name.upper() for name in Settings.model_fields}


def _load_text(path: Path) -> str:
    return path.read_text()


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(_load_text(path))


def _load_compose_text() -> str:
    return _load_text(COMPOSE_PATH)


def _load_compose_yaml() -> dict:
    return _load_yaml(COMPOSE_PATH)


def _load_seeder_compose_text() -> str:
    return _load_text(SEEDER_COMPOSE_PATH)


def _load_seeder_compose_yaml() -> dict:
    return _load_yaml(SEEDER_COMPOSE_PATH)


def _service_environment_names(compose: dict, service: str) -> set[str]:
    """
    Names set in the given service's `environment:` list -- the actual
    allowlist a running container sees, per docker-compose.yml's own
    comment on the libex block. Never called with "postgres": that
    service's environment block uses the postgres image's own variable
    names (POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD), which are not
    Libex Settings fields and would be false positives everywhere in this
    file.
    """
    env_list = compose["services"][service]["environment"]
    return {item.partition("=")[0] for item in env_list}


def _service_environment_values(compose: dict, service: str) -> dict[str, str]:
    """
    Name -> raw right-hand side (interpolation syntax and all, e.g.
    "${CACHE_TTL:-86400}") for every entry in the given service's
    `environment:` list. Used only where the value itself, not just whether
    the name is present, is what a check cares about.
    """
    env_list = compose["services"][service]["environment"]
    return dict(item.partition("=")[::2] for item in env_list)


def _libex_environment_names(compose: dict) -> set[str]:
    """
    Names set in the libex (API) service's `environment:` list only, from
    the docker-compose.yml dict handed in.
    """
    return _service_environment_names(compose, "libex")


def _seeder_environment_names() -> set[str]:
    """
    Names set in the libex-seeder service's `environment:` list, read from
    docker-compose.seeder.yml -- a separate Compose file and Portainer
    stack, not a service inside docker-compose.yml, so this loads its own
    file rather than taking docker-compose.yml's dict as a parameter.
    """
    return _service_environment_names(_load_seeder_compose_yaml(), "libex-seeder")


def _libex_image_environment_names(compose: dict) -> set[str]:
    """
    Union of names set across every service running Libex's own image --
    currently docker-compose.yml's libex and docker-compose.seeder.yml's
    libex-seeder. This answers "can an operator set this in this deployment
    at all", which is what direction 1 below needs now that the seeder is a
    wholly separate stack rather than a code path inside the libex
    container: a setting can be legitimately configurable while living only
    in docker-compose.seeder.yml's `environment:` block. A union is
    strictly weaker than checking either service alone -- it does not
    know or care which service a name is on -- which is exactly why
    SEEDER_ONLY_SETTINGS and test_seeder_only_settings_are_scoped_to_seeder_service
    exist below: they are the per-service check that keeps this union from
    being the only thing standing guard.
    """
    return _libex_environment_names(compose) | _seeder_environment_names()


def _interpolated_names(text: str) -> set[str]:
    """
    Every `${NAME...}` interpolation reference anywhere in a block of text.
    This is deliberately a text scan rather than a YAML-structure walk:
    `${...}` interpolation is Compose's own convention layered on top of
    plain YAML string scalars, so YAML parsing has no structural opinion on
    it -- the reference lives inside a string, indistinguishable from any
    other string content to a YAML parser.
    """
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", text))


def _env_example_names() -> set[str]:
    """Every variable name assigned (uncommented) in .env.example."""
    names = set()
    for line in ENV_EXAMPLE_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name:
            names.add(name)
    return names


def test_exclusion_lists_name_real_fields():
    """
    Guards the exclusion lists themselves: a typo'd or stale entry in
    NOT_OPERATOR_CONFIGURABLE would silently stop excluding the field it
    meant to, one in COMPOSE_KNOBS_WITHOUT_SETTINGS that happens to match a
    real Settings field would silently stop checking that field in
    direction 3, and a typo'd entry in SEEDER_ONLY_SETTINGS would silently
    stop test_seeder_only_settings_are_scoped_to_seeder_service from
    checking the field it meant to.
    """
    settings_names = _settings_field_names()
    bad_exclusions = {
        name for name in NOT_OPERATOR_CONFIGURABLE if name.upper() not in settings_names
    }
    assert not bad_exclusions, (
        f"NOT_OPERATOR_CONFIGURABLE names {sorted(bad_exclusions)} are not "
        "Settings fields in app/core/config.py -- fix the typo or remove "
        "the stale entry."
    )

    overlap = set(COMPOSE_KNOBS_WITHOUT_SETTINGS) & settings_names
    assert not overlap, (
        f"COMPOSE_KNOBS_WITHOUT_SETTINGS names {sorted(overlap)} ARE real "
        "Settings fields -- remove them from that list so direction 3 "
        "actually checks them."
    )

    bad_seeder_only = SEEDER_ONLY_SETTINGS - settings_names
    assert not bad_seeder_only, (
        f"SEEDER_ONLY_SETTINGS names {sorted(bad_seeder_only)} are not "
        "Settings fields in app/core/config.py -- fix the typo or remove "
        "the stale entry, or test_seeder_only_settings_are_scoped_to_seeder_service "
        "is silently not checking the field it meant to."
    )


def test_settings_configurable_via_compose():
    """
    Direction 1: every Settings field an operator is meant to be able to
    set has a matching name in the `environment:` block of at least one
    service running Libex's own image -- libex or libex-seeder. This is the
    check that would have caught the MIGRATION_* gap -- those five fields
    existed in config.py for weeks with no way for a repo-based deployment
    to ever set them to anything but their defaults.

    Checking the union rather than the libex service alone is deliberate,
    and deliberately weaker on its own: the seeder moved out of the API
    into its own service (docker-compose.yml's libex-seeder,
    app/services/seeder.py), so a setting can be legitimately configurable
    while appearing only on libex-seeder's block, never libex's. A union
    check alone would pass identically whether a name landed on the right
    service, the wrong one, or both -- see
    test_seeder_only_settings_are_scoped_to_seeder_service below for the
    per-service check that this test does not, by itself, perform.
    """
    compose = _load_compose_yaml()
    compose_names = _libex_image_environment_names(compose)
    configurable = _settings_field_names() - {
        name.upper() for name in NOT_OPERATOR_CONFIGURABLE
    }

    missing = sorted(configurable - compose_names)
    assert not missing, (
        f"Settings field(s) {missing} exist in app/core/config.py but are "
        "not in the `environment:` block of docker-compose.yml's libex "
        "service or docker-compose.seeder.yml's libex-seeder service, so an "
        "operator can never set them to anything but the compiled-in "
        "default. Add each to the appropriate file (and .env.example), or "
        "if it's deliberately fixed, list it in NOT_OPERATOR_CONFIGURABLE "
        "in this file with the reason."
    )


def test_seeder_only_settings_are_scoped_to_seeder_service():
    """
    The per-service check that test_settings_configurable_via_compose's
    union cannot perform on its own: every name in SEEDER_ONLY_SETTINGS
    must appear in docker-compose.seeder.yml's libex-seeder `environment:`
    block (where app/services/seeder.py can actually read it) AND be absent
    from docker-compose.yml's libex `environment:` block (where nothing
    reads it). This catches two regressions the union above would miss
    entirely -- a seeder setting drifting back into the API container, and a
    seeder setting quietly disappearing from libex-seeder while the union
    still finds it parked on libex instead.

    Now a stronger check than it was when both services lived in one file:
    the two environment blocks are in separate Compose files entirely, so
    "leaked into libex" and "missing from libex-seeder" are structural
    facts about two different documents, not just two blocks of one.
    """
    libex_names = _libex_environment_names(_load_compose_yaml())
    seeder_names = _seeder_environment_names()

    missing_from_seeder = sorted(SEEDER_ONLY_SETTINGS - seeder_names)
    assert not missing_from_seeder, (
        f"Settings field(s) {missing_from_seeder} are listed in "
        "SEEDER_ONLY_SETTINGS but are missing from "
        "docker-compose.seeder.yml's libex-seeder `environment:` block, so "
        "app/services/seeder.py -- the only reader of any of these -- can "
        "never see anything but the compiled-in default. Add each to the "
        "libex-seeder service."
    )

    leaked_into_libex = sorted(SEEDER_ONLY_SETTINGS & libex_names)
    assert not leaked_into_libex, (
        f"Settings field(s) {leaked_into_libex} are in SEEDER_ONLY_SETTINGS "
        "-- seeder-only settings read exclusively by "
        "app/services/seeder.py -- but also appear in docker-compose.yml's "
        "libex `environment:` block, where nothing reads them. Remove them "
        "from the libex service, or if the setting has genuinely become "
        "shared between both services, remove it from SEEDER_ONLY_SETTINGS "
        "instead."
    )


def test_env_example_names_are_reachable_in_compose():
    """
    Direction 2: every name in .env.example is either an environment key or
    an interpolation token somewhere in docker-compose.yml OR
    docker-compose.seeder.yml. This is the check that would have caught
    9e76b02 -- AXIOM_*, SEEDER_*, AUDIBLE_PROXY_URL and SEED_SECRET were
    deleted from compose while staying in .env.example, and the
    inconsistency sat there, machine-checkable, for two months.

    Scans both files now, not one: the SEEDER_* names' interpolation
    references (`${SEEDER_PROXY_URL:?...}`, `${SEEDER_MEM_LIMIT:-1g}` on
    `mem_limit:`, and the rest) no longer appear anywhere in
    docker-compose.yml's text at all -- they moved entirely into
    docker-compose.seeder.yml when the seeder became its own stack. A scan
    of docker-compose.yml alone would now read every one of them as a
    genuine gap.

    DB_HOST is no longer an exception this direction has to reason about:
    the seeder used to require it (docker-compose.seeder.yml's DATABASE_URL),
    and .env.example deliberately never documented it, so it sat outside
    env_example_names entirely -- but the seeder now reaches postgres by
    container name over a shared network instead of a published host port,
    and DB_HOST does not appear in either compose file any more. DB_BIND
    (the postgres `ports:` bind address in docker-compose.yml) needs no
    carve-out either: it is assigned in .env.example, referenced as
    `${DB_BIND:-127.0.0.1}` in docker-compose.yml, and this direction checks
    it exactly like any other name -- it is not a Settings field, but this
    direction has never required one; direction 1 is the one that reasons
    about Settings fields, not this one.
    """
    compose_text = _load_compose_text()
    seeder_compose_text = _load_seeder_compose_text()
    compose = yaml.safe_load(compose_text)
    referenced = (
        _libex_environment_names(compose)
        | _seeder_environment_names()
        | _interpolated_names(compose_text)
        | _interpolated_names(seeder_compose_text)
    )

    env_example_names = _env_example_names()
    missing = env_example_names - referenced

    # A name that matches a field this codebase has already decided is not
    # operator-configurable through Docker (debug, host) is expected to be
    # absent from compose -- .env.example documents it for the direct,
    # outside-Docker .env-file case instead. Anything else missing is a
    # genuine gap between the two files.
    allowed_missing = {name.upper() for name in NOT_OPERATOR_CONFIGURABLE}
    unexpected = sorted(missing - allowed_missing)

    assert not unexpected, (
        f"Name(s) {unexpected} are set in .env.example but never appear in "
        "docker-compose.yml or docker-compose.seeder.yml -- neither as an "
        "`environment:` key nor as a `${NAME...}` interpolation reference. "
        "An operator following .env.example would set a variable neither "
        "compose file silently ever passes through."
    )


def test_compose_environment_has_no_dead_knobs():
    """
    Direction 3: every name in the `environment:` block of libex
    (docker-compose.yml) OR libex-seeder (docker-compose.seeder.yml) is
    either a Settings field or a documented non-Settings knob. This is the
    check that would have caught PORT being passed for months while nothing
    in app/ reads settings.port and the Dockerfile CMD hardcodes --port
    3333.

    Checked per service, not as a union, so a dead knob is attributed to
    the service that actually carries it -- an unused name on libex-seeder
    is the same defect as one on libex and gets caught the same way,
    rather than being let through because some other service's block
    happens to also set it. The two services now live in separate files,
    so each is read from the file that actually defines it.
    """
    settings_names = _settings_field_names()
    known = settings_names | set(COMPOSE_KNOBS_WITHOUT_SETTINGS)

    service_environments = {
        "libex": _service_environment_names(_load_compose_yaml(), "libex"),
        "libex-seeder": _service_environment_names(
            _load_seeder_compose_yaml(), "libex-seeder"
        ),
    }
    dead_by_service = {
        service: sorted(names - known) for service, names in service_environments.items()
    }
    dead_by_service = {service: dead for service, dead in dead_by_service.items() if dead}

    assert not dead_by_service, (
        f"Name(s) are set in an `environment:` block but match no field in "
        "app/core/config.py's Settings and no entry in "
        "COMPOSE_KNOBS_WITHOUT_SETTINGS in this file, per service: "
        f"{dead_by_service} -- nothing in the app actually reads them. "
        "Either remove the dead knob from that service (docker-compose.yml "
        "for libex, docker-compose.seeder.yml for libex-seeder), or if "
        "something outside Settings genuinely consumes it (like uvicorn "
        "reading WEB_CONCURRENCY), document it in "
        "COMPOSE_KNOBS_WITHOUT_SETTINGS."
    )


def test_web_concurrency_is_a_hardened_literal():
    """
    WEB_CONCURRENCY is the one name in this file that is deliberately NOT
    a knob at all, operator-facing or otherwise -- unlike every other entry
    in docker-compose.yml's libex `environment:` block, it must be a bare
    literal (`WEB_CONCURRENCY=<int>`), never `${WEB_CONCURRENCY:-<int>}`
    interpolation, and it must not reappear in .env.example.

    This is arithmetic, not a style preference, and none of it fails loudly
    if it silently doubles: docker-compose.yml's own comment above this line
    records that the count is the multiplier on every per-process budget in
    the codebase -- the connection pool (pool_size 10 + max_overflow 10 per
    process) is sized so the worker count fits under Postgres's own
    max_connections=200, the two Audible concurrency limits in
    services/audible/client.py are deliberately NOT divided by worker count
    (dividing them caused a live 504 outage, so raising the count multiplies
    outbound concurrency on one exit IP instead), and persist_queue.py's
    backlog cap is ~45 MB per worker. Turning this back into an
    operator-tunable knob lets any one of those be silently exceeded by
    whatever a stack environment happens to set, with nothing in the app
    warning that it happened.

    This is also a guard against a regression that already shipped once:
    reverting the line to `${WEB_CONCURRENCY:-6}` reads as ordinary
    consistency with every other entry in this block, which is exactly how
    it got in. None of the three name-parity directions above catch it --
    `_libex_environment_names` (used by all three) splits each entry on
    `=` and keeps only the name, so `WEB_CONCURRENCY=6` and
    `WEB_CONCURRENCY=${WEB_CONCURRENCY:-6}` are indistinguishable to every
    other test in this file. If this test starts failing because the count
    itself is being deliberately changed, that is a real checkpoint, not
    friction: update the literal in docker-compose.yml and this test
    together, having re-checked the pool/concurrency/backlog arithmetic in
    docker-compose.yml's comment first, rather than reintroducing
    interpolation or a default.
    """
    compose_text = _load_compose_text()
    compose = yaml.safe_load(compose_text)
    env_list = compose["services"]["libex"]["environment"]
    entries = [item for item in env_list if item.partition("=")[0] == "WEB_CONCURRENCY"]

    assert entries, (
        "WEB_CONCURRENCY is missing entirely from docker-compose.yml's "
        "libex `environment:` block. It is not optional: uvicorn defaults "
        "to a single worker without it, and the multi-worker crash recovery "
        "docker-compose.yml's own comment describes (a wedged event loop is "
        "routed around, not recovered, by additional workers) depends on "
        "there being more than one."
    )
    entry = entries[0]
    assert re.fullmatch(r"WEB_CONCURRENCY=\d+", entry), (
        f"docker-compose.yml's WEB_CONCURRENCY entry is {entry!r}, not a "
        "bare integer literal. WEB_CONCURRENCY is deliberately NOT an "
        "operator-tunable knob: the worker count is the multiplier on the "
        "connection pool (sized against Postgres's max_connections=200), "
        "the Audible concurrency limits (deliberately not divided by worker "
        "count after a live 504 outage), and persist_queue.py's per-worker "
        "backlog cap -- see the comment above this line in docker-compose.yml "
        "for the arithmetic. Raising it via `${WEB_CONCURRENCY:-N}` "
        "interpolation lets a stack environment exceed all three silently. "
        "If the count is being deliberately changed, edit the literal "
        "directly (re-checking that arithmetic first); do not reintroduce "
        "interpolation."
    )

    env_example_names = _env_example_names()
    assert "WEB_CONCURRENCY" not in env_example_names, (
        "WEB_CONCURRENCY has reappeared in .env.example. It is deliberately "
        "absent: docker-compose.yml sets it as a bare literal, so a "
        ".env.example entry would document a knob that does not exist and "
        "invite an operator to set a value docker-compose.yml silently "
        "never reads. If the value ever becomes operator-configurable "
        "again that is a deliberate, arithmetic-checked reversal of the "
        "decision this test guards -- update this test in the same change, "
        "do not just add the line back to .env.example."
    )

    seeder_names = _seeder_environment_names()
    assert "WEB_CONCURRENCY" not in seeder_names, (
        "WEB_CONCURRENCY appears in docker-compose.seeder.yml's libex-seeder "
        "`environment:` block. libex-seeder runs `python -m scripts.seed`, "
        "never uvicorn, so this variable would govern nothing there -- it "
        "is uvicorn's own setting, read only when --workers is absent, and "
        "docker-entrypoint.sh's startup line that announces it is printed "
        "only for a uvicorn command. Passing it to the seeder container "
        "names a knob that does nothing and misleadingly suggests the "
        "seeder has its own worker count to tune. Remove it from the "
        "libex-seeder service."
    )


def test_compose_services_are_not_gated_behind_a_profile():
    """
    Guards the exact failure mode that motivated splitting the seeder out
    into docker-compose.seeder.yml in the first place: each of
    docker-compose.yml and docker-compose.seeder.yml is pasted, whole, into
    its own standalone Portainer stack -- neither is ever combined with the
    other by `-f`, and neither stack sets a `COMPOSE_PROFILES` value or
    names a service directly on a command line the way `--profile seeder up
    -d` once did. A service defined only behind a `profiles:` key is
    invisible to a plain `docker compose up`/`config` in a stack that never
    enables that profile -- verified against Compose 2.40.3 for the
    seeder's previous `seeder` profile: `services: {}` and exit 0,
    deploying nothing while reporting success. That silent no-op is the
    live bug that motivated moving the seeder into its own file.

    This is a structural check on the parsed YAML -- "does this service
    definition have a `profiles:` key at all" -- not a text or name search,
    so it stays true regardless of what a future profile might be named. It
    intentionally does not try to guess whether some hypothetical future
    profile might be safe: every service in both files today is meant to
    always run in the stack it lives in, so `profiles:` reappearing on any
    service is exactly the shape of the original mistake and gets flagged
    rather than judged case by case.
    """
    gated_by_file: dict[str, list[str]] = {}
    for path in (COMPOSE_PATH, SEEDER_COMPOSE_PATH):
        compose = _load_yaml(path)
        gated = sorted(
            name for name, definition in compose["services"].items() if "profiles" in definition
        )
        if gated:
            gated_by_file[path.name] = gated

    assert not gated_by_file, (
        f"Service(s) defined behind a `profiles:` key: {gated_by_file}. "
        f"Both {COMPOSE_PATH.name} and {SEEDER_COMPOSE_PATH.name} are "
        "pasted whole into their own standalone Portainer stack -- a "
        "service gated behind a profile that stack's environment never "
        "enables deploys nothing at all, silently reporting success. This "
        "is the exact bug that motivated moving the seeder into its own "
        "file; do not reintroduce a profile on any service in either file."
    )


def test_shared_defaults_match_across_compose_files():
    """
    Every name in SHARED_DEFAULTS must render to the identical raw value
    (interpolation syntax included) in both docker-compose.yml's libex
    `environment:` block and docker-compose.seeder.yml's libex-seeder one.
    Presence checks alone -- every other direction in this file -- cannot
    catch the two files agreeing that a name exists while disagreeing on
    what it defaults to.

    CACHE_TTL is the load-bearing case: libex and libex-seeder read and
    write the same cache table, so a divergent default does not error, it
    just makes one service start expiring rows the other still expects to
    be live -- a correctness bug with no exception and no log line. The
    other four names in SHARED_DEFAULTS (LOG_RETENTION_DAYS, LOG_LEVEL,
    AXIOM_TOKEN, AXIOM_DATASET) carry the same reasoning at lower stakes:
    one retention policy, one log level, one Axiom destination, so the two
    files disagreeing about any of them is drift rather than a deliberate
    per-service choice -- unlike WEB_CONCURRENCY or the SEEDER_* names,
    which are deliberately different or deliberately one-sided.
    """
    libex_values = _service_environment_values(_load_compose_yaml(), "libex")
    seeder_values = _service_environment_values(_load_seeder_compose_yaml(), "libex-seeder")

    mismatches = {
        name: (libex_values.get(name), seeder_values.get(name))
        for name in SHARED_DEFAULTS
        if libex_values.get(name) != seeder_values.get(name)
    }

    assert not mismatches, (
        f"SHARED_DEFAULTS name(s) render to different values in "
        f"docker-compose.yml's libex service vs. docker-compose.seeder.yml's "
        f"libex-seeder service: {mismatches} (libex value, libex-seeder "
        "value). These are meant to be one policy applied identically to "
        "both services -- for CACHE_TTL specifically, both services read "
        "and write the same cache table, so a mismatched default is a "
        "correctness bug, not a configuration preference. Bring both files "
        "back into agreement, or if the two services now genuinely need "
        "different values, remove the name from SHARED_DEFAULTS and record "
        "why."
    )
