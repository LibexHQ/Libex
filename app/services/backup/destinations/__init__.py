"""
Builds the configured destinations from settings.

One rule runs through everything here: A DESTINATION THAT IS NOT FULLY AND
SAFELY CONFIGURED IS INACTIVE, AND SAYS SO. It is never half configured, it
never falls back to a weaker security posture than the one that was asked
for, and it never raises during construction -- a raise in a container with
restart: unless-stopped is a restart loop, which is the one failure mode
that produces no readable explanation anywhere.

This is also the only place that can catch a mistake config.py cannot.
Settings uses extra="ignore", so BACKUP_FTPS_HSOT is discarded in silence
and the field keeps its empty default; nothing in pydantic will ever
mention it. What an operator actually has to read is this module's account
of what it resolved and what it found missing, which is why a partial
configuration names the fields it lacked rather than logging "destination
disabled".
"""

# Standard library
import os
import ssl
import threading

# Core
from app.core.logging import get_logger

# Services
from app.services.backup.destinations.base import Destination
from app.services.backup.destinations.ftps import FTPSDestination, build_context


logger = get_logger()

__all__ = ["Destination", "build_destinations"]


def build_destinations(settings, abort: threading.Event) -> list[Destination]:
    """
    Every destination this deployment has configured, in the order they will
    be attempted.

    abort is one threading.Event shared by the whole run and set by the
    runner when the process is asked to stop. It is a constructor argument
    rather than a fourth protocol method deliberately: only a transport
    doing blocking work in a thread has any use for it, an async-native
    transport would implement an empty method forever, and widening a
    three-method seam to carry an implementation detail of one transport is
    how seams stop meaning anything. Constructor arguments are not part of
    the protocol.
    """
    destinations: list[Destination] = []

    ftps = _build_ftps(settings, abort)
    if ftps is not None:
        destinations.append(ftps)

    return destinations


def _build_ftps(settings, abort: threading.Event) -> Destination | None:
    host = settings.backup_ftps_host.strip()
    user = settings.backup_ftps_user.strip()
    password = settings.backup_ftps_password.get_secret_value()
    remote_dir = settings.backup_ftps_path.strip()
    ca_bundle = settings.backup_ftps_ca_bundle.strip()
    server_hostname = settings.backup_ftps_server_hostname.strip()

    # Nothing set at all is a working deployment with no FTPS destination,
    # exactly as an empty axiom_token means stdout only. Silent, because
    # there is nothing wrong to report.
    if not any([host, user, password, remote_dir, ca_bundle, server_hostname]):
        return None

    # PATH is on this list with the credentials, and it is the one that was
    # missing. Without it the destination resolved ACTIVE with an empty
    # remote directory, which _remote_path renders as a bare filename --
    # uploading 2.38 GB into whatever the login lands in, silently, while
    # four files in this repo state that a partial configuration is
    # inactive. Requiring it is both the safer answer and the one those
    # four files already promise; an operator who genuinely wants the login
    # directory can say so with BACKUP_FTPS_PATH=. rather than by leaving it
    # blank and hoping.
    missing = [
        name
        for name, value in (
            ("BACKUP_FTPS_HOST", host),
            ("BACKUP_FTPS_USER", user),
            ("BACKUP_FTPS_PASSWORD", password),
            ("BACKUP_FTPS_PATH", remote_dir),
        )
        if not value
    ]
    if missing:
        # The field names, which are ours, and never their values. "host and
        # user set, password missing" is a sentence an operator can act on,
        # and it is the sentence extra="ignore" makes impossible anywhere
        # else.
        logger.warning(
            "Backup: FTPS destination is incompletely configured and will not be used",
            extra={"provider": "ftps", "missing": ",".join(missing)},
        )
        return None

    # A configured bundle that cannot be loaded makes the destination
    # inactive. It does not fall back to the system trust store: the
    # operator asked for a specific trust anchor, and quietly substituting a
    # broader one turns a pin into ordinary web PKI at the moment the pin
    # stops working -- which is precisely when it might be doing its job.
    try:
        context = build_context(ca_bundle)
    except (OSError, ssl.SSLError) as exc:
        logger.error(
            "Backup: FTPS CA bundle could not be loaded, destination will not be used",
            extra={
                "provider": "ftps",
                "error_type": type(exc).__name__,
                "ca_bundle_configured": True,
                "ca_bundle_exists": os.path.exists(ca_bundle) if ca_bundle else False,
            },
        )
        return None

    if not ca_bundle:
        # Verification is still on -- there is no way through this module to
        # turn it off -- but the trust anchor is now every public CA rather
        # than one certificate, which is a materially weaker position for a
        # box on a LAN and usually means the bundle was meant to be set.
        logger.warning(
            "Backup: FTPS destination has no CA bundle, verifying against the system trust store",
            extra={"provider": "ftps"},
        )

    port = int(settings.backup_ftps_port)
    if port == 990:
        # Not fatal, because a server may genuinely be reachable this way,
        # but it is the single most common misconfiguration of this
        # protocol: 990 is implicit FTPS, which expects TLS before the first
        # command, while ftplib speaks explicit FTPS and sends AUTH TLS over
        # a cleartext control connection. The handshake never happens and
        # the failure looks like a hung connection.
        logger.warning(
            "Backup: FTPS port 990 is implicit FTPS; this client speaks explicit FTPS, normally port 21",
            extra={"provider": "ftps", "port": port},
        )

    return FTPSDestination(
        host=host,
        port=port,
        user=user,
        password=password,
        remote_dir=remote_dir,
        context=context,
        server_hostname=server_hostname,
        deadline_seconds=float(settings.backup_destination_timeout_seconds),
        abort=abort,
    )
