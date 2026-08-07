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


def test_is_valid_asin_accepts_isbn_style_numeric_key():
    """Ten-digit ISBN-style keys (no letters) are valid ASINs — this database
    has records keyed this way and they must keep validating."""
    assert is_valid_asin("0008182221") is True


def test_is_valid_asin_rejects_trailing_newline():
    """A trailing newline must not sneak a would-be-valid ASIN past validation.
    re.match's '$' matches immediately before a trailing '\\n', so a pattern
    built on match() (rather than fullmatch()) wrongly accepts this — and this
    guard sits directly upstream of a URL path-segment interpolation."""
    assert is_valid_asin("B0ABCDEFGH\n") is False


def test_is_valid_asin_rejects_trailing_newline_after_valid_isbn_style_key():
    """The newline-smuggling case also fails for the all-digit ISBN-style form."""
    assert is_valid_asin("0008182221\n") is False


def test_is_valid_asin_still_accepts_asin_without_trailing_newline():
    """The fullmatch tightening doesn't regress the plain valid case."""
    assert is_valid_asin("B08G9PRS1K") is True


# ============================================================
# X-ADP-SW / DEVICE HEADER ISOLATION
# ============================================================

def test_x_adp_sw_present_and_numeric():
    """X-ADP-SW must stay a numeric string. It's deliberately still
    random.randint(...) — a future 'simplification' to a static non-numeric
    string would 404 the screens endpoint."""
    headers = get_region_headers("us")
    assert "X-ADP-SW" in headers
    assert headers["X-ADP-SW"].isdigit()


def test_x_adp_sw_varies_across_calls():
    """X-ADP-SW is randomly generated per call, not a fixed constant."""
    values = {get_region_headers("us")["X-ADP-SW"] for _ in range(20)}
    assert len(values) > 1


def test_get_region_headers_never_carries_device_type_id():
    """The Android device-type header is scoped to the screens call via
    audible_get's extra_headers and must never leak into the shared
    get_region_headers output — every other call site would otherwise be
    silently stamped with a stable per-device id."""
    from app.services.audible.client import ANDROID_DEVICE_TYPE_ID

    for region in VALID_REGIONS:
        headers = get_region_headers(region)
        assert "X-Device-Type-Id" not in headers
        assert ANDROID_DEVICE_TYPE_ID not in headers.values()


def test_android_device_type_id_pinned_literal_value():
    """Pins the constant against the literal string, not against itself.
    A wrong device id doesn't raise anywhere in this path — the screens
    endpoint 200s with an empty page — so this is the one assertion in the
    suite that would actually catch that value silently changing."""
    from app.services.audible.client import ANDROID_DEVICE_TYPE_ID

    assert ANDROID_DEVICE_TYPE_ID == "A10KISP2GWF0E4"


# ============================================================
# AUDIBLE_GET EXTRA_HEADERS OVERLAY
# ============================================================

@pytest.mark.asyncio
async def test_audible_get_without_extra_headers_uses_region_headers_unchanged():
    """Omitting extra_headers (every pre-existing call site) leaves the headers
    byte-identical to get_region_headers' own output — no stray keys added."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.services.audible import client as client_module

    fixed_headers = {"User-Agent": "fixed", "X-ADP-SW": "12345678"}
    captured = {}

    async def _get(url, headers=None, params=None, timeout=None, follow_redirects=None):
        captured["headers"] = headers
        captured["follow_redirects"] = follow_redirects
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"ok": True}
        return response

    with patch.object(client_module, "get_region_headers", return_value=fixed_headers), \
         patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        await client_module.audible_get("us", "/1.0/catalog/products", {"page": 0})

    assert captured["headers"] == fixed_headers
    # a redirect on this outbound call would mean an attacker-chosen host
    # gets followed silently; this must stay pinned to False.
    assert captured["follow_redirects"] is False


@pytest.mark.asyncio
async def test_audible_get_extra_headers_overlays_region_headers():
    """extra_headers is overlaid on top of get_region_headers for that one call
    only, without dropping or mutating the base headers."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.services.audible import client as client_module

    fixed_headers = {"User-Agent": "fixed", "X-ADP-SW": "12345678"}
    captured = {}

    async def _get(url, headers=None, params=None, timeout=None, follow_redirects=None):
        captured["headers"] = headers
        captured["follow_redirects"] = follow_redirects
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"ok": True}
        return response

    with patch.object(client_module, "get_region_headers", return_value=fixed_headers), \
         patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        await client_module.audible_get(
            "us",
            "/1.0/screens/audible-android-author-detail/B000APF21M",
            {"author_asin": "B000APF21M"},
            extra_headers={"X-Device-Type-Id": "A10KISP2GWF0E4"},
        )

    assert captured["headers"] == {
        "User-Agent": "fixed",
        "X-ADP-SW": "12345678",
        "X-Device-Type-Id": "A10KISP2GWF0E4",
    }
    # base headers untouched by the overlay
    assert fixed_headers == {"User-Agent": "fixed", "X-ADP-SW": "12345678"}
    # a redirect on this outbound call would mean an attacker-chosen host
    # gets followed silently; this must stay pinned to False.
    assert captured["follow_redirects"] is False


# ============================================================
# AUDIBLE_GET ERROR MESSAGE
# ============================================================

@pytest.mark.asyncio
async def test_request_error_with_empty_message_includes_type():
    """An httpx.RequestError with an empty str() still produces a diagnosable
    message (the exception type + URL), not a blank one."""
    import httpx
    from unittest.mock import patch, AsyncMock
    from app.services.audible.client import audible_get
    from app.core.exceptions import AudibleAPIException

    # httpx.ConnectError("") stringifies to "" — the case that produced the
    # blank "Audible API request failed: " in the wild.
    failing = AsyncMock(side_effect=httpx.ConnectError(""))
    with patch("httpx.AsyncClient.get", new=failing):
        with pytest.raises(AudibleAPIException) as exc:
            await audible_get("us", "/1.0/catalog/products", {"page": 0})

    msg = str(exc.value)
    assert "ConnectError" in msg          # the type is named
    assert "request failed:" in msg
    assert msg.strip() != "Audible API request failed:"  # not blank
