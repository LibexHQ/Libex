"""
Audible client unit tests.
Tests region validation, URL building, and header generation.
"""

# Third party
import pytest

from app.services.audible.client import (
    validate_region,
    get_audible_url,
    get_region_headers,
    REGION_MAP,
    LOCALE_MAP,
    VALID_REGIONS,
)

from app.core.exceptions import RegionException
from app.core.middleware import is_valid_asin


# ============================================================
# REGION VALIDATION TESTS
# ============================================================

def test_validate_region_accepts_valid_region():
    """Valid region passes validation."""
    assert validate_region("us") == "us"


def test_validate_region_normalizes_uppercase():
    """Uppercase region is normalized to lowercase."""
    assert validate_region("US") == "us"


def test_validate_region_strips_whitespace():
    """Region with whitespace is stripped."""
    assert validate_region("  us  ") == "us"


def test_validate_region_raises_for_invalid():
    """Invalid region raises RegionException."""
    with pytest.raises(RegionException):
        validate_region("xx")


def test_validate_region_raises_for_empty():
    """Empty string raises RegionException."""
    with pytest.raises(RegionException):
        validate_region("")


def test_validate_all_supported_regions():
    """All documented regions pass validation."""
    for region in VALID_REGIONS:
        assert validate_region(region) == region


# ============================================================
# URL BUILDING TESTS
# ============================================================

def test_get_audible_url_us_region():
    """US region builds correct Audible URL."""
    url = get_audible_url("us", "/1.0/catalog/products/B08G9PRS1K")
    assert url == "https://api.audible.com/1.0/catalog/products/B08G9PRS1K"


def test_get_audible_url_uk_region():
    """UK region builds correct Audible URL."""
    url = get_audible_url("uk", "/1.0/catalog/products/B08G9PRS1K")
    assert url == "https://api.audible.co.uk/1.0/catalog/products/B08G9PRS1K"


def test_get_audible_url_de_region():
    """DE region builds correct Audible URL."""
    url = get_audible_url("de", "/1.0/catalog/products/B08G9PRS1K")
    assert url == "https://api.audible.de/1.0/catalog/products/B08G9PRS1K"


def test_get_audible_url_all_regions():
    """All supported regions build valid URLs."""
    for region, tld in REGION_MAP.items():
        url = get_audible_url(region, "/test")
        assert f"audible{tld}" in url


def test_get_audible_url_includes_path():
    """URL includes the provided path."""
    url = get_audible_url("us", "/1.0/catalog/products/B08G9PRS1K")
    assert "/1.0/catalog/products/B08G9PRS1K" in url


# ============================================================
# HEADER TESTS
# ============================================================

def test_get_region_headers_returns_dict():
    """Headers are returned as a dictionary."""
    headers = get_region_headers("us")
    assert isinstance(headers, dict)


def test_get_region_headers_includes_user_agent():
    """Headers include User-Agent."""
    headers = get_region_headers("us")
    assert "User-Agent" in headers


def test_get_region_headers_includes_accept():
    """Headers include Accept."""
    headers = get_region_headers("us")
    assert "Accept" in headers


def test_get_region_headers_us_locale():
    """US region headers include en-US locale."""
    headers = get_region_headers("us")
    assert "en-US" in headers.get("Accept-Language", "")


def test_get_region_headers_de_locale():
    """DE region headers include de-DE locale."""
    headers = get_region_headers("de")
    assert "de-DE" in headers.get("Accept-Language", "")


def test_get_region_headers_jp_locale():
    """JP region headers include ja-JP locale."""
    headers = get_region_headers("jp")
    assert "ja-JP" in headers.get("Accept-Language", "")


def test_region_map_covers_all_valid_regions():
    """Every valid region has a TLD mapping."""
    for region in VALID_REGIONS:
        assert region in REGION_MAP, f"Missing TLD for region: {region}"


def test_locale_map_covers_all_valid_regions():
    """Every valid region has a locale mapping."""
    for region in VALID_REGIONS:
        assert region in LOCALE_MAP, f"Missing locale for region: {region}"
    
def test_is_valid_asin_accepts_valid():
    """Valid ASIN passes validation."""
    assert is_valid_asin("B08G9PRS1K") is True


def test_is_valid_asin_rejects_too_short():
    """ASIN shorter than 10 chars fails validation."""
    assert is_valid_asin("B08G9PRS") is False


def test_is_valid_asin_rejects_too_long():
    """ASIN longer than 10 chars fails validation."""
    assert is_valid_asin("B08G9PRS1K1") is False


def test_is_valid_asin_rejects_special_chars():
    """ASIN with special characters fails validation."""
    assert is_valid_asin("not-an-asin") is False


def test_is_valid_asin_accepts_uppercase():
    """ASIN validation is case insensitive."""
    assert is_valid_asin("b08g9prs1k") is True