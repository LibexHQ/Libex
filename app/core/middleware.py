"""
Middleware configuration for Libex.
CORS and request validation.
"""

# Standard library
import re

# Third party
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

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
# SETUP
# ============================================================

def setup_middleware(app: FastAPI) -> None:
    """Configures all middleware for the application."""

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    logger.info("Middleware configured")