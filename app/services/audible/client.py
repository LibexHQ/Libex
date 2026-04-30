"""
Audible API client.
Handles headers, region mapping, and the shared httpx session.
All Audible service files call through this client exclusively.

DESIGN PHILOSOPHY: Audible-first.
Every request hits Audible directly for fresh data.
Cache is used only as a fallback when Audible is unavailable.
This ensures data accuracy and freshness at all times.
"""

# Standard library
import random
from typing import Any

# Third party
import httpx

# Local
from app.core.config import get_settings
from app.core.exceptions import AudibleAPIException, RegionException

settings = get_settings()

# ============================================================
# REGION MAPS
# ============================================================

REGION_MAP: dict[str, str] = {
    "us": ".com",
    "uk": ".co.uk",
    "ca": ".ca",
    "au": ".com.au",
    "de": ".de",
    "fr": ".fr",
    "it": ".it",
    "es": ".es",
    "jp": ".co.jp",
    "in": ".in",
    "br": ".com.br",
}

LOCALE_MAP: dict[str, str] = {
    "us": "en-US",
    "uk": "en-GB",
    "ca": "en-CA",
    "au": "en-AU",
    "de": "de-DE",
    "fr": "fr-FR",
    "it": "it-IT",
    "es": "es-ES",
    "jp": "ja-JP",
    "in": "en-IN",
    "br": "pt-BR",
}

VALID_REGIONS = set(REGION_MAP.keys())

# ============================================================
# HEADERS
# ============================================================

BASE_HEADERS: dict[str, str] = {
    "User-Agent": "Audible/4.15.0 Android/14 Build/SM-S928U",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    "Accept-Charset": "utf-8",
}


def get_region_headers(region: str) -> dict[str, str]:
    """Returns region-specific headers including locale, language, and X-ADP-SW."""
    locale = LOCALE_MAP.get(region, "en-US")
    return {
        **BASE_HEADERS,
        "ACCEPTED-LANGUAGE": locale,
        "Accept-Language": locale,
        "X-ADP-SW": str(random.randint(10000000, 99999999)),
    }


# ============================================================
# CLIENT
# ============================================================

def validate_region(region: str) -> str:
    """Validates and normalises region string. Raises RegionException if invalid."""
    region = region.lower().strip()
    if region not in VALID_REGIONS:
        raise RegionException(region)
    return region


def get_audible_url(region: str, path: str) -> str:
    """Builds a full Audible API URL for the given region and path."""
    tld = REGION_MAP.get(region, ".com")
    return f"https://api.audible{tld}{path}"


async def audible_get(
    region: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """
    Makes a GET request to the Audible API.
    Returns parsed JSON response.
    Raises AudibleAPIException on non-200 responses.
    """
    region = validate_region(region)
    url = get_audible_url(region, path)
    headers = get_region_headers(region)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                url,
                headers=headers,
                params=params,
                timeout=30.0,
            )
        except httpx.TimeoutException:
            raise AudibleAPIException(f"Audible API timed out: {url}")
        except httpx.RequestError as e:
            raise AudibleAPIException(f"Audible API request failed: {e}")

    if response.status_code == 404:
        from app.core.exceptions import NotFoundException
        raise NotFoundException()

    if response.status_code != 200:
        raise AudibleAPIException(
            f"Audible API returned {response.status_code} for {url}"
        )

    return response.json()