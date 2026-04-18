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

logger = get_logger()


# ============================================================
# INPUT VALIDATION
# ============================================================

ASIN_PATTERN = re.compile(r'^[A-Z0-9]{10}$')


def is_valid_asin(asin: str) -> bool:
    """Validates that a string matches Audible ASIN format."""
    return bool(ASIN_PATTERN.match(asin.upper()))


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
        print(f"DISPATCH {request.method} {request.url.path}", flush=True)
        request_id = str(uuid.uuid4())
        start = time.monotonic()
        try:
            response = await call_next(request)
            print(f"CALL_NEXT OK {response.status_code}", flush=True)
        except Exception as e:
            print(f"CALL_NEXT FAILED: {e}", flush=True)
            raise
        took = round((time.monotonic() - start) * 1000, 2)
        print("LOGGING NOW", flush=True)
        ip = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("x-real-ip")
            or (request.client.host if request.client else None)
        )
        logger.info(
            "Request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "url": request.url.path,
                "status": response.status_code,
                "userAgent": request.headers.get("user-agent"),
                "took": took,
                "ip": ip,
            },
        )
        for handler in logger.handlers:
            handler.flush()
        return response


# ============================================================
# SETUP
# ============================================================

def setup_middleware(app: FastAPI) -> None:
    """Configures all middleware for the application."""

    app.add_middleware(LoggingMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    logger.info("Middleware configured")