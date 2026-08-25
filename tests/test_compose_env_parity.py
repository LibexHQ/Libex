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

Three directions:

1. `test_settings_configurable_via_compose` (config.py -> compose): every
   Settings field an operator is meant to be able to set has a matching name
   in docker-compose.yml's libex `environment:` block. Would have caught the
   MIGRATION_* gap.
2. `test_env_example_names_are_reachable_in_compose` (.env.example ->
   compose): every name in .env.example is either an environment key or an
   interpolation token (`${NAME...}`) somewhere in docker-compose.yml. Would
   have caught the 9e76b02 deletions.
3. `test_compose_environment_has_no_dead_knobs` (compose -> config.py): every
   name in docker-compose.yml's libex `environment:` block is either a
   Settings field or an explicitly documented non-Settings knob. Would have
   caught PORT being passed for months while uvicorn reads only
   WEB_CONCURRENCY and FORWARDED_ALLOW_IPS, and the Dockerfile CMD hardcodes
   `--port 3333`.

Honest limit, stated once here rather than at each call site: this compares
NAME SETS only. It has no opinion on values, hosts, or paths -- it would not
catch the healthcheck pointing at the wrong host, a volume bound to the wrong
directory, or the `/app/data` mount that exposed the Postgres data directory
to the API container. A green run here means the three files agree on which
settings exist. It says nothing about whether they are wired to the right
values.
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
COMPOSE_KNOBS_WITHOUT_SETTINGS: dict[str, str] = {
    "WEB_CONCURRENCY": (
        "read directly from the OS environment by uvicorn (0.46.0 checks "
        "WEB_CONCURRENCY only when --workers is absent) -- never a Settings "
        "field. See docker-compose.yml's own comment block and the "
        "Dockerfile CMD comment."
    ),
}


def _settings_field_names() -> set[str]:
    """Every Settings field name, uppercased to match env-var convention."""
    return {name.upper() for name in Settings.model_fields}


def _load_compose_text() -> str:
    return COMPOSE_PATH.read_text()


def _load_compose_yaml() -> dict:
    return yaml.safe_load(_load_compose_text())


def _libex_environment_names(compose: dict) -> set[str]:
    """
    Names set in the libex service's `environment:` list -- the actual
    allowlist a running container sees, per docker-compose.yml's own
    comment. Deliberately scoped to the libex service only: the postgres
    service's environment block uses the postgres image's own variable
    names (POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD), which are not
    Libex Settings fields and would be false positives here.
    """
    env_list = compose["services"]["libex"]["environment"]
    return {item.partition("=")[0] for item in env_list}


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
    meant to, and one in COMPOSE_KNOBS_WITHOUT_SETTINGS that happens to
    match a real Settings field would silently stop checking that field in
    direction 3.
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


def test_settings_configurable_via_compose():
    """
    Direction 1: every Settings field an operator is meant to be able to
    set has a matching name in docker-compose.yml's libex `environment:`
    block. This is the check that would have caught the MIGRATION_* gap --
    those five fields existed in config.py for weeks with no way for a
    repo-based deployment to ever set them to anything but their defaults.
    """
    compose = _load_compose_yaml()
    compose_names = _libex_environment_names(compose)
    configurable = _settings_field_names() - {
        name.upper() for name in NOT_OPERATOR_CONFIGURABLE
    }

    missing = sorted(configurable - compose_names)
    assert not missing, (
        f"Settings field(s) {missing} exist in app/core/config.py but are "
        "not in docker-compose.yml's libex `environment:` block, so an "
        "operator can never set them to anything but the compiled-in "
        "default. Add each to docker-compose.yml (and .env.example), or if "
        "it's deliberately fixed, list it in NOT_OPERATOR_CONFIGURABLE in "
        "this file with the reason."
    )


def test_env_example_names_are_reachable_in_compose():
    """
    Direction 2: every name in .env.example is either an environment key or
    an interpolation token somewhere in docker-compose.yml. This is the
    check that would have caught 9e76b02 -- AXIOM_*, SEEDER_*,
    AUDIBLE_PROXY_URL and SEED_SECRET were deleted from compose while
    staying in .env.example, and the inconsistency sat there, machine-
    checkable, for two months.
    """
    compose_text = _load_compose_text()
    compose = yaml.safe_load(compose_text)
    referenced = _libex_environment_names(compose) | _interpolated_names(compose_text)

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
        "docker-compose.yml -- neither as an `environment:` key nor as a "
        "`${NAME...}` interpolation reference. An operator following "
        ".env.example would set a variable docker-compose.yml silently "
        "never passes through."
    )


def test_compose_environment_has_no_dead_knobs():
    """
    Direction 3: every name in docker-compose.yml's libex `environment:`
    block is either a Settings field or a documented non-Settings knob.
    This is the check that would have caught PORT being passed for months
    while nothing in app/ reads settings.port and the Dockerfile CMD
    hardcodes --port 3333.
    """
    compose = _load_compose_yaml()
    compose_names = _libex_environment_names(compose)
    settings_names = _settings_field_names()

    dead = sorted(compose_names - settings_names - set(COMPOSE_KNOBS_WITHOUT_SETTINGS))
    assert not dead, (
        f"Name(s) {dead} are set in docker-compose.yml's libex "
        "`environment:` block but match no field in app/core/config.py's "
        "Settings and no entry in COMPOSE_KNOBS_WITHOUT_SETTINGS in this "
        "file -- nothing in the app actually reads them. Either remove the "
        "dead knob from docker-compose.yml, or if something outside "
        "Settings genuinely consumes it (like uvicorn reading "
        "WEB_CONCURRENCY), document it in COMPOSE_KNOBS_WITHOUT_SETTINGS."
    )
