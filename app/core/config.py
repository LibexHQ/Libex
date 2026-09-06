"""
Core configuration for Libex.
Settings are loaded from environment variables with sensible defaults.
"""

# Standard library
from functools import lru_cache

# Third party
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "Libex"
    app_version: str = "1.21.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 3333

    # Cache
    cache_enabled: bool = True
    cache_ttl: int = 86400         # 24 hours default

    # Audible
    default_region: str = "us"
    audible_proxy_url: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://libex:libex@localhost:5432/libex"
    database_echo: bool = False
    db_password: str = ""

    # Logging
    log_retention_days: int = 7    # 0 = infinite, N = keep N days of rotated logs
    log_level: str = "INFO"        # DEBUG, INFO, WARNING, ERROR — overrides the INFO default

    # Logging - Axiom (optional)
    axiom_token: str = ""
    axiom_dataset: str = "libex"

    # Seeder
    seeder_interval_hours: int = 24
    seeder_request_delay: float = 1.0
    seeder_regions: str = "us"
    seeder_new_releases_interval_hours: int = 24    # How often the new-releases worker runs
    seeder_refresh_enabled: bool = False             # Re-fetch upcoming pre-orders as their release date approaches

    # Internal seed endpoint
    seed_secret: str = ""  # Empty = endpoint disabled. Set in env only.

    # Backup — scheduling
    #
    # Read by the backup runner (the libex-backup service) and by nothing
    # else. None of the BACKUP_* names is in docker-compose.yml's libex
    # `environment:` allowlist, so the API container never receives a value
    # for any of them and always takes the default here — a mistyped backup
    # knob can stop backups, but it cannot touch the API. The exception is a
    # deployment run outside Docker off a shared .env file, which this class
    # reads directly and where that separation does not exist.
    #
    # period, time, day-of-week and timezone are plain strings rather than
    # enums or parsed times on purpose: their valid sets are not Python
    # scalar types, and a value pydantic rejects raises while Settings is
    # being constructed, before any code can report it. In the backup
    # container that is a restart cycle rather than a message. The runner
    # validates them and says what it found, so an unusable value leaves the
    # container idle and complaining instead of gone.
    backup_timezone: str = "UTC"          # IANA name, e.g. Europe/London — zoneinfo, stdlib on 3.12
    backup_period: str = "daily"          # daily, weekly or monthly
    backup_time: str = "03:00"            # HH:MM, local to backup_timezone
    backup_day_of_week: str = "sunday"    # weekly only; ignored otherwise
    backup_day_of_month: int = 1          # monthly only; ignored otherwise

    # Backup — retention, three tiers kept together rather than one count.
    # backup_retention_recent is the last N artefacts. backup_retention_aged_days
    # adds two: the newest artefact at least that many days old, and the oldest
    # one not yet that old. The middle tier is what survives damage noticed
    # late — six dailies are six copies of the same bad state if the
    # corruption landed a week ago. The third exists because without it the
    # middle one can never hold anything: the recent tier deletes every
    # candidate at about six days, so nothing reaches thirty, and the nightly
    # prune logs a clean line while the copy the whole scheme exists to
    # preserve is never created. Reserving the next artefact in line before it
    # qualifies is what gives the middle tier a successor to promote. Eight
    # artefacts at these defaults, not seven. 0 disables both aged tiers; the
    # recent tier has no off switch, because a backup system that keeps
    # nothing is a backup system that is not running.
    backup_retention_recent: int = 6
    backup_retention_aged_days: int = 30

    # Backup — operational
    #
    # The spool path inside the container is fixed, the same way the listening
    # port is: docker-compose.yml mounts the spool at this exact path and sets
    # this exact value, and what an operator relocates is the host side
    # (BACKUP_SPOOL_PATH), never the container side.
    #
    # The dump timeout is not fitted to a measurement. A clean pg_dump of the
    # live database measured 8m39s for a 2.38 GB archive; this is roughly
    # seven times that, so a host under IO contention, or a database several
    # times larger, still finishes inside it. It is finite rather than
    # enormous because pg_dump holds one transaction snapshot open for its
    # whole run, which holds autovacuum back from reclaiming anything newer —
    # a dump wedged for a day costs more than a backup missed for a day.
    backup_spool_dir: str = "/backup-spool"
    backup_dump_timeout_seconds: int = 3600
    backup_destination_timeout_seconds: int = 3600

    # Backup — FTPS destination
    #
    # Empty is a working deployment, never an error: with no host the
    # destination is inactive, the way an empty axiom_token means stdout only
    # and an empty seed_secret disables the internal endpoint. A partial set
    # is the same — inactive, with a warning naming what is missing, never a
    # half-configured upload attempt.
    #
    # model_config sets extra="ignore", so a misspelled BACKUP_FTPS_ name is
    # discarded in silence and the field keeps its empty default. With
    # seed_secret that mistake left an endpoint disabled; here it leaves
    # backups that never run, which announces itself only when someone needs
    # a restore. Nothing in this class can catch it — which is why the runner
    # reports what it resolved when it starts, and why these are separate
    # fields rather than one connection blob: "host and user set, password
    # missing" is a sentence something can actually say.
    #
    # backup_ftps_port defaults to 21 because this is explicit FTPS: the
    # connection opens in the clear on the control port and is upgraded by
    # AUTH TLS. It is not implicit FTPS on 990, which negotiates TLS before
    # the first command and is a different protocol dressed in the same name;
    # operators conflate the two constantly. Point this at 990 and the
    # handshake never happens.
    #
    # backup_ftps_ca_bundle is the server's own certificate, and it is not
    # really optional — a self-signed certificate is the normal case on a
    # NAS. ssl.create_default_context(cafile=...) loads that bundle and
    # nothing else: measured, a context built with a cafile holds exactly one
    # CA where the default holds 121 system roots. That makes it a private
    # trust anchor rather than web PKI, which is to say certificate pinning,
    # and it is a stronger position than trusting every public CA on earth to
    # vouch for a box on a LAN. Anyone adding load_default_certs() to "fix" a
    # verification failure would be turning pinning back into ordinary PKI
    # and should be reaching for the right bundle instead.
    #
    # There is deliberately no setting that can turn verification off. Note
    # what that has to defend against: ftplib.FTP_TLS's own default context
    # is ssl._create_stdlib_context(), which is CERT_NONE with
    # check_hostname False and an empty trust store — the unverified mode is
    # what you get by not passing a context, not by asking for one. A
    # destination that cannot verify is inactive.
    #
    # backup_ftps_server_hostname overrides the name checked against the
    # certificate, for the common case of a NAS certificate whose CN is not
    # the address you reach it on. ftplib has no parameter for it: both
    # FTP_TLS.auth() and FTP_TLS.ntransfercmd() pass self.host as
    # server_hostname, and FTP.connect() is what assigns self.host — so the
    # override has to be set after connect() and before login(), which means
    # constructing FTP_TLS with no host and connecting by hand rather than
    # letting __init__ do both.
    #
    # Two of the four traps ftps.py names are configuration decisions and are
    # named here, because this is where someone will look. First, FTP_TLS
    # encrypts the control channel and leaves the data channel in the clear
    # unless prot_p() is called:
    # __init__ sets _prot_p = False and ntransfercmd wraps the data socket
    # only if that flag is true, so a login that looks encrypted can be
    # followed by the whole database crossing the network as plaintext.
    # Second, the way that mistake gets made: FTP_TLS does no TLS session
    # resumption on the data connection, while vsftpd ships
    # require_ssl_reuse=YES, so a correct prot_p() upload will likely be
    # refused by a default server. Deleting prot_p() makes it work, which is
    # exactly why it is the first thing anyone tries. The fix is a subclass
    # passing session=self.sock.session into wrap_socket — stdlib, no
    # dependency.
    #
    # SecretStr on the password only: it renders as SecretStr('**********')
    # wherever a Settings object is logged, and forces .get_secret_value() at
    # the one place it is used, which is what makes that use greppable in
    # review. Host, user and path are not credentials.
    #
    # db_password, axiom_token and seed_secret stay plain str, not SecretStr,
    # despite sitting two fields above one that is. db_password is never read
    # in app/ at all -- only database_url reaches the container, built by
    # compose's own interpolation before Settings ever sees it (see
    # tests/test_compose_env_parity.py). axiom_token is passed straight into
    # axiom_py's Client(token=...) constructor (app/core/logging.py), and
    # seed_secret is compared directly against a caller's Authorization header
    # in app/api/routes/internal/router.py. Wrapping either means touching
    # that call site to unwrap it first, and neither has been done -- being
    # unwrapped is a fact about today's code, not an endorsement.
    backup_ftps_host: str = ""
    backup_ftps_port: int = 21
    backup_ftps_user: str = ""
    backup_ftps_password: SecretStr = SecretStr("")
    backup_ftps_path: str = ""
    backup_ftps_ca_bundle: str = ""
    backup_ftps_server_hostname: str = ""

    # Every backup_* field typed as int rather than str reintroduces, for
    # that field, exactly the failure the comment above backup_timezone
    # describes and was written to avoid for the string ones: a value
    # pydantic rejects raises inside Settings() construction, before
    # setup_logging() has run and before any logger exists, which under
    # `restart: unless-stopped` is a silent restart cycle with nothing useful
    # in `docker logs`. Left un-parsed, "1st", "twenty-one" or "1h" in any of
    # them would do exactly that.
    #
    # The validator below finds those fields itself, from cls.model_fields,
    # rather than naming them one by one -- a hand-maintained list here
    # beside a hand-maintained set of int-typed fields above is what let two
    # of them (backup_retention_aged_days, backup_destination_timeout_seconds)
    # go uncovered the first time this was written, found only when a later
    # reviewer checked the comment's own count against the class. Any
    # backup_* field declared int picks up the guard the moment it is added;
    # nothing here has to be touched.
    #
    # These fields stay int rather than widening to str, because unlike
    # period/time/day-of-week/timezone their valid sets ARE a Python scalar
    # type and every reader of e.g. self.settings.backup_ftps_port elsewhere
    # expects an int, not a string it would have to re-parse. Instead the bad
    # value is caught here, before pydantic's own type coercion gets to it,
    # logged by name and value, and replaced with the field's own default so
    # construction still succeeds. The log line reaches `docker logs` even
    # this early: nothing has called setup_logging() yet, but nothing has
    # attached a handler to the root logger either, so Python's own
    # lastResort handler writes it to stderr regardless.
    @model_validator(mode="before")
    @classmethod
    def _default_bad_backup_ints(cls, data):
        if not isinstance(data, dict):
            return data
        for name, field in cls.model_fields.items():
            if not name.startswith("backup_") or field.annotation is not int:
                continue
            if name not in data:
                continue
            v = data[name]
            if v is None or isinstance(v, int):
                continue
            try:
                data[name] = int(v)
            except (TypeError, ValueError):
                # Deferred import, same reason as check_retired_env_vars
                # below: logging imports config at module load, so config
                # can't import logging at top without a circular import.
                from app.core.logging import get_logger

                get_logger().error(
                    f"Invalid value for {name.upper()}: {v!r} is not a "
                    f"whole number. Using default {field.default} instead."
                )
                data[name] = field.default
        return data

    # Migration notice
    migration_notice_enabled: bool = False
    migration_new_host: str = ""        # e.g. https://libexdb.com
    migration_announced: str = ""       # ISO date, e.g. 2026-08-06
    migration_sunset: str = ""          # ISO date, e.g. 2026-11-04
    migration_info_url: str = ""        # URL of the pinned GitHub issue


@lru_cache()
def get_settings() -> Settings:
    return Settings()


# Environment variables Libex no longer uses. If one is still set, the app warns
# at startup so the operator can remove it — it never crashes. Add to this map
# whenever a setting is retired.
RETIRED_ENV_VARS: dict[str, str] = {
    "SEEDER_NEW_RELEASES_PAGES": (
        "Retired in 1.4.0 — the new-releases seeder now scans by genre and walks "
        "each genre to its catalog limit instead of a fixed page count. Safe to remove."
    ),
    "SEEDER_NEW_RELEASES_DAYS": (
        "Retired in 1.4.0 — the new-releases seeder now collects all reachable "
        "releases per genre rather than a fixed day window. Safe to remove."
    ),
    "SEEDER_ENABLED": (
        "Retired in 1.18.0 — the seeder runs as its own container "
        "(scripts/seed.py, its own stack in docker-compose.seeder.yml), so "
        "deploying that stack is what enables it. Safe to remove."
    ),
}


def check_retired_env_vars() -> None:
    """
    Warns (never crashes) if any retired env vars are still set. Checks the raw
    environment rather than Settings, since retired vars are no longer Settings
    fields and pydantic would silently ignore them.
    """
    # Standard library
    import os

    # Core — deferred to avoid a config <- logging circular import (logging
    # imports config at module load, so config can't import logging at top).
    from app.core.logging import get_logger

    logger = get_logger()
    for var, message in RETIRED_ENV_VARS.items():
        if os.environ.get(var) is not None:
            logger.warning(f"Retired env var {var} is set. {message}")