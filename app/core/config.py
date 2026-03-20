"""
Core configuration for Libex.
Settings are loaded from environment variables with sensible defaults.
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Application
    app_name: str = "Libex"
    app_version: str = "0.1.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8080

    # Cache
    cache_enabled: bool = True
    cache_backend: str = "memory"  # memory or redis
    cache_ttl: int = 86400         # 24 hours default
    redis_url: str = "redis://localhost:6379"

    # Audible
    default_region: str = "us"
    
    # Logging - Axiom (optional)
    axiom_token: str = ""
    axiom_dataset: str = "libex"
    axiom_org_id: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
