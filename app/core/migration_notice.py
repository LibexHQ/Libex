"""
Single source of truth for the libexdb.com migration notice.

`build_migration_notice` is the ONLY predicate for whether the notice is on —
it requires the flag, the new host, and both dates to be present, stripped,
and parseable, with sunset not before announced. Every call site (the
middleware that stamps headers, `/health`, the OpenAPI description/servers,
and the generic 500 handler) consumes the one `MigrationNotice` this returns
instead of re-deriving the condition, so a half-configured environment can
never render a notice in one place while staying silent in another.

Off by default: self-hosters who set nothing get `None` back. The only wire
difference that ships regardless is `Access-Control-Expose-Headers` naming
Deprecation/Sunset/Link on any response carrying an `Origin` header —
everything else is byte-identical.
"""

# Standard library
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formatdate
from urllib.parse import urlparse

# Core
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger()

MIGRATION_HEADER_NAMES = ("Deprecation", "Sunset", "Link")


@dataclass(frozen=True)
class MigrationNotice:
    """Everything every call site needs, derived exactly once at startup."""

    new_host: str
    new_host_hostname: str
    sunset_display: str
    info_url: str
    headers: dict[str, str]
    health_message: str


def build_migration_notice(settings: Settings) -> MigrationNotice | None:
    """
    Returns the validated migration notice, or None if the feature is off or
    misconfigured. Every branch that refuses logs why, except the top one —
    the flag simply being off is the ordinary self-hosted case, not a
    misconfiguration.
    """
    if not settings.migration_notice_enabled:
        return None

    new_host = settings.migration_new_host.strip()
    announced_raw = settings.migration_announced.strip()
    sunset_raw = settings.migration_sunset.strip()
    info_url = settings.migration_info_url.strip()

    missing = [
        field
        for field, value in (
            ("migration_new_host", new_host),
            ("migration_announced", announced_raw),
            ("migration_sunset", sunset_raw),
        )
        if not value
    ]
    if missing:
        logger.warning(
            "Migration notice enabled but missing required config: %s; skipping migration notice",
            ", ".join(missing),
        )
        return None

    try:
        announced = datetime.strptime(announced_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        sunset = datetime.strptime(sunset_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning(
            "Migration notice enabled but migration_announced/migration_sunset are not valid "
            "ISO dates (YYYY-MM-DD); skipping migration notice"
        )
        return None

    if sunset < announced:
        logger.warning(
            "Migration notice enabled but migration_sunset is before migration_announced; "
            "RFC 9745 §4 forbids that, skipping migration notice"
        )
        return None

    new_host_hostname = (urlparse(new_host).hostname or "").lower()
    if not new_host_hostname:
        logger.warning(
            "Migration notice enabled but migration_new_host %r has no parseable hostname; "
            "skipping migration notice",
            new_host,
        )
        return None

    # Deprecation is an RFC 9745 structured-field Date (@ + epoch seconds).
    # Sunset is an RFC 8594 HTTP-date (IMF-fixdate, literal "GMT"). The two
    # formats diverge on purpose — do not normalise them to match.
    headers = {
        "Deprecation": f"@{int(announced.timestamp())}",
        "Sunset": formatdate(sunset.timestamp(), usegmt=True),
        "Link": f'<{new_host}>; rel="canonical"',
    }
    if info_url:
        headers["Link"] += f', <{info_url}>; rel="deprecation"; type="text/html"'

    notice = MigrationNotice(
        new_host=new_host,
        new_host_hostname=new_host_hostname,
        sunset_display=sunset_raw,
        info_url=info_url,
        headers=headers,
        health_message=f"Libex is moving to {new_host}; this host stops serving after {sunset_raw}.",
    )

    logger.info(
        "Migration notice active: new_host=%s sunset=%s",
        new_host,
        sunset_raw,
    )
    return notice


def is_new_host_request(notice: MigrationNotice, request_hostname: str | None) -> bool:
    """
    True when the incoming request arrived on the new host itself — the one
    case the notice must stay silent for, so libexdb.com never announces its
    own deprecation to the consumers being asked to move onto it. Compared
    on hostname only (no port, no scheme) since that is what Starlette's
    `Request.url.hostname` gives back regardless of whether a port or a
    proxy-forwarded Host header is present.
    """
    # Trailing dot: `Host: libexdb.com.` is a valid FQDN form (the root
    # label) that some clients emit. Traefik normalises it for routing but
    # forwards the header through verbatim, so strip a single trailing dot
    # from both sides before comparing rather than treating it as a mismatch.
    incoming = request_hostname.lower().rstrip(".") if request_hostname else ""
    return bool(incoming) and incoming == notice.new_host_hostname.rstrip(".")