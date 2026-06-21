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
    app_version: str = "1.1.0"
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

    # Logging - Axiom (optional)
    axiom_token: str = ""
    axiom_dataset: str = "libex"

    # Seeder
    seeder_enabled: bool = False
    seeder_interval_hours: int = 24
    seeder_request_delay: float = 1.0
    seeder_regions: str = "us"
    seeder_new_releases_interval_hours: int = 24    # How often the new-releases worker runs
    seeder_new_releases_pages: int = 20             # Pages (50 books each) the new-releases scan walks per region

    # Internal seed endpoint
    seed_secret: str = ""  # Empty = endpoint disabled. Set in env only.


@lru_cache()
def get_settings() -> Settings:
    return Settings()