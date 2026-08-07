"""
Middleware configuration for Libex.
CORS and request validation.
"""

# Standard library
import re
import time
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

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        start = time.monotonic()
        response = await call_next(request)
        took = round((time.monotonic() - start) * 1000, 2)

        if request.url.path == "/health":
            return response

        ip = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("x-real-ip")
            or (request.client.host if request.client else None)
        )
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
                "status": response.status_code,
                "userAgent": user_agent,
                "took": took,
                "ip": ip,
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