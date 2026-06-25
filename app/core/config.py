"""
Core configuration for Libex.
Settings are loaded from environment variables with sensible defaults.
"""

# Standard library
from functools import lru_cache

# Third party
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
    app_version: str = "1.7.0"
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
    seeder_enabled: bool = False
    seeder_interval_hours: int = 24
    seeder_request_delay: float = 1.0
    seeder_regions: str = "us"
    seeder_new_releases_interval_hours: int = 24    # How often the new-releases worker runs
    seeder_refresh_enabled: bool = False             # Re-fetch upcoming pre-orders as their release date approaches

    # Internal seed endpoint
    seed_secret: str = ""  # Empty = endpoint disabled. Set in env only.


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