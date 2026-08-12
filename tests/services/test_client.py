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


# ============================================================
# RETRY / BACKOFF / CONCURRENCY
# ============================================================

def _mock_response(status_code, headers=None, json_body=None):
    from unittest.mock import MagicMock

    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.json.return_value = json_body if json_body is not None else {"ok": True}
    return response


@pytest.mark.asyncio
async def test_audible_get_retries_429_then_succeeds():
    """A 429 is retried, not raised immediately, and the eventual 200 is
    returned once the retry succeeds."""
    from unittest.mock import AsyncMock, patch
    from app.services.audible.client import audible_get

    responses = [_mock_response(429), _mock_response(200, json_body={"ok": True})]
    get_mock = AsyncMock(side_effect=responses)

    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await audible_get("us", "/1.0/catalog/products", {"page": 0})

    assert result == {"ok": True}
    assert get_mock.await_count == 2
    mock_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_audible_get_retries_5xx_then_succeeds():
    """A 5xx is retried the same way a 429 is."""
    from unittest.mock import AsyncMock, patch
    from app.services.audible.client import audible_get

    responses = [_mock_response(502), _mock_response(200, json_body={"ok": True})]
    get_mock = AsyncMock(side_effect=responses)

    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()):
        result = await audible_get("us", "/1.0/catalog/products", {"page": 0})

    assert result == {"ok": True}
    assert get_mock.await_count == 2


@pytest.mark.asyncio
async def test_audible_get_exhausts_retries_and_raises():
    """A 429 on every attempt is retried up to AUDIBLE_MAX_ATTEMPTS times
    total (1 initial + AUDIBLE_MAX_ATTEMPTS - 1 retries), then raises --
    never retried indefinitely."""
    from unittest.mock import AsyncMock, patch
    from app.services.audible.client import audible_get, AUDIBLE_MAX_ATTEMPTS
    from app.core.exceptions import AudibleAPIException

    get_mock = AsyncMock(return_value=_mock_response(429))

    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(AudibleAPIException):
            await audible_get("us", "/1.0/catalog/products", {"page": 0})

    assert get_mock.await_count == AUDIBLE_MAX_ATTEMPTS
    # sleeps once between every pair of attempts, never after the last one
    assert mock_sleep.await_count == AUDIBLE_MAX_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_audible_get_404_is_never_retried():
    """A 404 is terminal -- it must raise NotFoundException on the very
    first attempt, with no retry and no sleep at all. This database carries
    ISBN-keyed records that 404 for chapters in every region; retrying those
    would burn requests against an already-throttled-once shared IP for
    nothing."""
    from unittest.mock import AsyncMock, patch
    from app.services.audible.client import audible_get
    from app.core.exceptions import NotFoundException

    get_mock = AsyncMock(return_value=_mock_response(404))

    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(NotFoundException):
            await audible_get("us", "/1.0/catalog/products/BADASIN000", {})

    assert get_mock.await_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_audible_get_other_4xx_not_retried():
    """Every 4xx other than 404 or 429 is a real answer too -- raised
    immediately on the first attempt, never retried."""
    from unittest.mock import AsyncMock, patch
    from app.services.audible.client import audible_get
    from app.core.exceptions import AudibleAPIException

    get_mock = AsyncMock(return_value=_mock_response(400))

    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(AudibleAPIException):
            await audible_get("us", "/1.0/catalog/products", {"page": 0})

    assert get_mock.await_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_audible_get_timeout_not_retried():
    """httpx.TimeoutException is deliberately NOT retried -- a single
    attempt, then AudibleAPIException, never a retry loop on a slow/hung
    connection."""
    import httpx
    from unittest.mock import AsyncMock, patch
    from app.services.audible.client import audible_get
    from app.core.exceptions import AudibleAPIException

    get_mock = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(AudibleAPIException):
            await audible_get("us", "/1.0/catalog/products", {"page": 0})

    assert get_mock.await_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_audible_get_request_error_not_retried():
    """httpx.RequestError (connection failures) is deliberately NOT retried
    either -- a single attempt, then AudibleAPIException."""
    import httpx
    from unittest.mock import AsyncMock, patch
    from app.services.audible.client import audible_get
    from app.core.exceptions import AudibleAPIException

    get_mock = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(AudibleAPIException):
            await audible_get("us", "/1.0/catalog/products", {"page": 0})

    assert get_mock.await_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_audible_get_honors_retry_after_numeric_seconds_over_computed_backoff():
    """A numeric Retry-After header wins outright over computed backoff --
    the sleep duration must equal the header's value, not a jittered
    exponential guess."""
    from unittest.mock import AsyncMock, patch
    from app.services.audible.client import audible_get

    responses = [
        _mock_response(429, headers={"Retry-After": "3"}),
        _mock_response(200, json_body={"ok": True}),
    ]
    get_mock = AsyncMock(side_effect=responses)

    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await audible_get("us", "/1.0/catalog/products", {"page": 0})

    mock_sleep.assert_awaited_once_with(3.0)


@pytest.mark.asyncio
async def test_audible_get_retry_after_is_capped():
    """A Retry-After value beyond AUDIBLE_RETRY_AFTER_CAP_SECONDS is capped,
    not honored verbatim -- one large value must not stall a fan-out far
    past what a few retries should ever cost."""
    from unittest.mock import AsyncMock, patch
    from app.services.audible.client import audible_get, AUDIBLE_RETRY_AFTER_CAP_SECONDS

    responses = [
        _mock_response(429, headers={"Retry-After": "9999"}),
        _mock_response(200, json_body={"ok": True}),
    ]
    get_mock = AsyncMock(side_effect=responses)

    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await audible_get("us", "/1.0/catalog/products", {"page": 0})

    mock_sleep.assert_awaited_once_with(AUDIBLE_RETRY_AFTER_CAP_SECONDS)


@pytest.mark.asyncio
async def test_audible_get_backoff_used_when_no_retry_after_header():
    """With no Retry-After header at all, the computed full-jitter backoff
    is used instead -- pinned here by patching random.uniform to identity
    the ceiling it was called with, rather than asserting an exact sleep
    duration (which is randomized by design)."""
    from unittest.mock import AsyncMock, patch
    from app.services.audible.client import audible_get, AUDIBLE_RETRY_BASE_SECONDS

    responses = [_mock_response(429), _mock_response(200, json_body={"ok": True})]
    get_mock = AsyncMock(side_effect=responses)

    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep, \
         patch("random.uniform", return_value=0.0) as mock_uniform:
        await audible_get("us", "/1.0/catalog/products", {"page": 0})

    # attempt 0 (the only attempt made before the successful retry): ceiling
    # is AUDIBLE_RETRY_BASE_SECONDS * 2**0.
    mock_uniform.assert_called_once_with(0, AUDIBLE_RETRY_BASE_SECONDS)
    mock_sleep.assert_awaited_once_with(0.0)


def test_parse_retry_after_numeric_seconds():
    """A plain numeric Retry-After is parsed as seconds."""
    from app.services.audible.client import _parse_retry_after

    assert _parse_retry_after("5") == 5.0


def test_parse_retry_after_strips_whitespace():
    """Whitespace around the header value is stripped before parsing."""
    from app.services.audible.client import _parse_retry_after

    assert _parse_retry_after("  7  ") == 7.0


def test_parse_retry_after_negative_clamped_to_zero():
    """A negative numeric value is clamped to 0.0, never a negative sleep."""
    from app.services.audible.client import _parse_retry_after

    assert _parse_retry_after("-5") == 0.0


def test_parse_retry_after_http_date_form():
    """An HTTP-date Retry-After (the spec's other valid form) parses to a
    positive number of seconds when it names a near-future time."""
    from email.utils import format_datetime
    import datetime
    from app.services.audible.client import _parse_retry_after

    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=5)
    result = _parse_retry_after(format_datetime(future, usegmt=True))
    assert result is not None
    assert 0 <= result <= 6  # allow a little slack for test execution time


def test_parse_retry_after_returns_none_for_none():
    """A missing header returns None so the caller falls back to computed
    backoff."""
    from app.services.audible.client import _parse_retry_after

    assert _parse_retry_after(None) is None


def test_parse_retry_after_returns_none_for_empty_string():
    """An empty header value returns None."""
    from app.services.audible.client import _parse_retry_after

    assert _parse_retry_after("") is None


def test_parse_retry_after_returns_none_for_unparseable_garbage():
    """A value that is neither a number nor an HTTP-date returns None
    rather than raising."""
    from app.services.audible.client import _parse_retry_after

    assert _parse_retry_after("not-a-valid-value") is None


def test_compute_backoff_seconds_prefers_retry_after_when_present():
    """When Retry-After is present, it wins outright over the exponential
    computation, capped."""
    from app.services.audible.client import _compute_backoff_seconds, AUDIBLE_RETRY_AFTER_CAP_SECONDS

    assert _compute_backoff_seconds(attempt=0, retry_after=2.0) == 2.0
    assert _compute_backoff_seconds(attempt=5, retry_after=2.0) == 2.0  # attempt is irrelevant here
    assert _compute_backoff_seconds(attempt=0, retry_after=9999.0) == AUDIBLE_RETRY_AFTER_CAP_SECONDS


def test_compute_backoff_seconds_exponential_ceiling_capped():
    """With no Retry-After, the exponential ceiling itself is capped at
    AUDIBLE_RETRY_MAX_BACKOFF_SECONDS regardless of how large attempt gets --
    pinned by patching random.uniform to report the ceiling it was called
    with."""
    from unittest.mock import patch
    from app.services.audible.client import (
        _compute_backoff_seconds, AUDIBLE_RETRY_MAX_BACKOFF_SECONDS, AUDIBLE_RETRY_BASE_SECONDS,
    )

    with patch("random.uniform", side_effect=lambda lo, hi: hi) as mock_uniform:
        # A large attempt count would blow past the max backoff via naive
        # exponentiation (base * 2**attempt) if the ceiling weren't capped.
        result = _compute_backoff_seconds(attempt=20, retry_after=None)

    mock_uniform.assert_called_once_with(0, AUDIBLE_RETRY_MAX_BACKOFF_SECONDS)
    assert result == AUDIBLE_RETRY_MAX_BACKOFF_SECONDS
    assert AUDIBLE_RETRY_BASE_SECONDS * (2 ** 20) > AUDIBLE_RETRY_MAX_BACKOFF_SECONDS


def test_is_retryable_status_429():
    """429 is retryable."""
    from app.services.audible.client import _is_retryable_status

    assert _is_retryable_status(429) is True


def test_is_retryable_status_5xx_range():
    """Every 5xx in range is retryable."""
    from app.services.audible.client import _is_retryable_status

    assert _is_retryable_status(500) is True
    assert _is_retryable_status(503) is True
    assert _is_retryable_status(599) is True


def test_is_retryable_status_404_not_retryable():
    """404 is explicitly not in the retryable set -- terminal."""
    from app.services.audible.client import _is_retryable_status

    assert _is_retryable_status(404) is False


def test_is_retryable_status_other_4xx_not_retryable():
    """A non-429 4xx is not retryable."""
    from app.services.audible.client import _is_retryable_status

    assert _is_retryable_status(400) is False
    assert _is_retryable_status(403) is False


def test_is_retryable_status_600_out_of_range_not_retryable():
    """Anything at or past 600 falls outside the 5xx retry range."""
    from app.services.audible.client import _is_retryable_status

    assert _is_retryable_status(600) is False


# ============================================================
# CONCURRENCY SEMAPHORE
# ============================================================

@pytest.mark.asyncio
async def test_get_audible_semaphore_bounds_in_flight_requests(monkeypatch):
    """The semaphore actually bounds how many callers can hold it at once to
    AUDIBLE_CONCURRENCY_LIMIT -- proven by driving real concurrency past the
    limit and watching the observed peak never exceed it."""
    import asyncio as asyncio_module
    from app.services.audible import client as client_module

    monkeypatch.setattr(client_module, "AUDIBLE_CONCURRENCY_LIMIT", 2)
    monkeypatch.setattr(client_module, "_audible_semaphore", None)
    monkeypatch.setattr(client_module, "_audible_semaphore_loop", None)

    in_flight = 0
    max_in_flight = 0
    lock = asyncio_module.Lock()

    async def worker():
        nonlocal in_flight, max_in_flight
        async with client_module._get_audible_semaphore():
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            await asyncio_module.sleep(0.01)
            async with lock:
                in_flight -= 1

    await asyncio_module.gather(*(worker() for _ in range(8)))

    assert max_in_flight == 2


def test_get_audible_semaphore_reuses_instance_within_same_running_loop():
    """A second call inside the same running event loop must return the
    exact same Semaphore instance, not a fresh one -- otherwise waiters from
    the first instance would never see permits released via the second."""
    import asyncio as asyncio_module
    from app.services.audible import client as client_module

    client_module._audible_semaphore = None
    client_module._audible_semaphore_loop = None

    async def get_two():
        first = client_module._get_audible_semaphore()
        second = client_module._get_audible_semaphore()
        return first, second

    loop = asyncio_module.new_event_loop()
    try:
        first, second = loop.run_until_complete(get_two())
    finally:
        loop.close()
        client_module._audible_semaphore = None
        client_module._audible_semaphore_loop = None

    assert first is second


def test_get_audible_semaphore_rekeys_when_running_loop_changes():
    """A fresh event loop must get a fresh Semaphore instance, not one bound
    to a now-closed loop's waiter state -- this is exactly what makes the
    module safe under pytest's own function-scoped event loops, each of
    which is a 'new running loop' from this function's point of view."""
    import asyncio as asyncio_module
    from app.services.audible import client as client_module

    client_module._audible_semaphore = None
    client_module._audible_semaphore_loop = None

    async def get_sem():
        return client_module._get_audible_semaphore()

    loop1 = asyncio_module.new_event_loop()
    try:
        sem1 = loop1.run_until_complete(get_sem())
    finally:
        loop1.close()

    loop2 = asyncio_module.new_event_loop()
    try:
        sem2 = loop2.run_until_complete(get_sem())
    finally:
        loop2.close()
        client_module._audible_semaphore = None
        client_module._audible_semaphore_loop = None

    assert sem1 is not sem2
