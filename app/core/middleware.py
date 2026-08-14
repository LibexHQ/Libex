"""
Middleware configuration for Libex.
CORS and request validation.
"""

# Standard library
import re
import time
import urllib.parse
import uuid

# Third party
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Services
from app.services.audible.client import validate_region

# Core
from app.core.logging import get_logger
from app.core.exceptions import RegionException
from app.core.migration_notice import MigrationNotice, MIGRATION_HEADER_NAMES, is_new_host_request

logger = get_logger()


# ============================================================
# INPUT VALIDATION
# ============================================================

ASIN_PATTERN = re.compile(r'^[A-Z0-9]{10}$')


def is_valid_asin(asin: str) -> bool:
    """Validates that a string matches Audible ASIN format."""
    return bool(ASIN_PATTERN.fullmatch(asin.upper()))


# ============================================================
# REGION VALIDATION
# ============================================================

def valid_region(
    region: str = Query(default="us", description="Audible region code")
) -> str:
    """FastAPI dependency that validates and normalises region parameter."""
    try:
        return validate_region(region)
    except RegionException:
        raise


# ============================================================
# HTTP LOGGING
# ============================================================

# Query params whose values are safe to log: region selectors, pagination,
# sort order, and catalogue facets. Every one of them describes HOW a caller
# asked, never WHAT they typed. Anything absent from this set is treated as
# caller-authored text and has its value replaced before the line is written.
#
# An allowlist rather than a denylist, deliberately. A denylist leaks every
# param added after it was written, silently and by default, and the failure
# is invisible until someone reads the logs. This fails closed instead: a new
# param is redacted until someone decides otherwise.
_SAFE_QUERY_PARAMS = frozenset({
    "region", "book_region", "cache", "limit", "page", "sort", "order",
    "flat", "depth", "days", "source", "products_sort_by",
    "category", "genre", "book_format", "language", "plan_name",
    "explicit", "has_pdf", "is_vvab", "whisper_sync",
    "audiobooks_produced", "cultural_heritage", "gender",
    "longer_than", "shorter_than", "rating_better_than", "rating_worse_than",
})

# Deliberately free of characters urlencode would escape -- "<redacted>"
# comes back as "%3Credacted%3E", which is unreadable in exactly the logs
# this field exists to make readable.
_REDACTED = "REDACTED"


def _redact_query(raw_query: str) -> str:
    """
    Replaces the value of every non-allowlisted query param, keeping the key.

    Libex logs the query string because the path alone cannot answer which
    params consumers actually use -- but on the search and name-lookup routes
    that string is whatever a person typed, and Libex deliberately records
    nothing that identifies a caller or reveals what they were looking for.
    Keeping the key and dropping the value preserves the operational answer
    ("this route was called with name=") without keeping the content.

    Malformed input returns the empty string rather than raising. This runs on
    every request against an attacker-controlled URL, and losing the query
    field on one log line beats losing the request -- and beats falling back
    to the raw string, which would leak exactly what this exists to withhold.
    """
    if not raw_query:
        return ""

    try:
        pairs = urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
    except (ValueError, UnicodeDecodeError):
        return ""

    return urllib.parse.urlencode([
        (key, value if key in _SAFE_QUERY_PARAMS else _REDACTED)
        for key, value in pairs
    ])


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        start = time.monotonic()
        response = await call_next(request)
        took = round((time.monotonic() - start) * 1000, 2)

        if request.url.path == "/health":
            return response

        # No client address is read here, from any header or from the
        # connection. Libex does not record who called it -- not in full, not
        # truncated, not hashed. Every field below describes the request, not
        # the requester, which is what keeps per-endpoint failure rates and
        # latency visible while leaving nothing that identifies a caller.
        #
        # The user agent stays because it names client SOFTWARE, not a person
        # -- it is how a spike gets traced to a particular consumer version.
        # With no address logged alongside it, it cannot be tied back to an
        # individual, so removing it on privacy grounds would blind the
        # monitoring for nothing.
        user_agent = request.headers.get("user-agent", "")
        # The only signal, once both libex.lostcartographer.xyz and libexdb.com serve
        # the same container, that tells old-host traffic apart from new-host traffic.
        host = request.headers.get("host", "")
        logger.info(
            "Request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "url": request.url.path,
                # Path alone cannot answer which parameters consumers actually
                # use. Values are allowlisted -- see _redact_query -- so this
                # answers that without keeping what a caller typed. No secret
                # travels this way either: the internal routes authenticate on
                # an Authorization header, not a query param.
                "query": _redact_query(request.url.query),
                "status": response.status_code,
                "userAgent": user_agent,
                "took": took,
                "host": host,
            },
        )
        return response

# ============================================================
# MIGRATION NOTICE
# ============================================================

class MigrationNoticeMiddleware(BaseHTTPMiddleware):
    """
    Adds the Deprecation/Sunset/Link headers to responses served on the old
    host. Responses served on migration_new_host itself never get them — see
    is_new_host_request — so libexdb.com never announces its own deprecation
    once both hostnames serve the same container. Not reached for a 500 built
    by the bare-Exception handler, since Starlette's ServerErrorMiddleware sits
    outside every middleware registered here; app.main applies the same
    headers itself on that one path.
    """

    def __init__(self, app: FastAPI, notice: MigrationNotice):
        super().__init__(app)
        self._notice = notice

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if not is_new_host_request(self._notice, request.url.hostname):
            response.headers.update(self._notice.headers)
        return response

# ============================================================
# SETUP
# ============================================================

def setup_middleware(app: FastAPI, migration_notice: MigrationNotice | None = None) -> None:
    """Configures all middleware for the application."""

    app.add_middleware(LoggingMiddleware)

    if migration_notice is not None:
        app.add_middleware(MigrationNoticeMiddleware, notice=migration_notice)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
        expose_headers=list(MIGRATION_HEADER_NAMES),
    )

    logger.info("Middleware configured")