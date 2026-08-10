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
import asyncio
import datetime
import random
from email.utils import parsedate_to_datetime
from typing import Any

# Third party
import httpx

# Local
from app.core.config import get_settings
from app.core.exceptions import AudibleAPIException, RegionException
from app.core.logging import get_logger

settings = get_settings()
logger = get_logger()

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

# Device type id for the Android screens endpoints (e.g. the author-detail
# screen). Scoped per-call via audible_get's extra_headers, never merged into
# get_region_headers — stamping every call with one stable device id across a
# single exit IP is exactly the shape a per-device throttle keys on.
ANDROID_DEVICE_TYPE_ID = "A10KISP2GWF0E4"


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
# CONCURRENCY BOUND
# ============================================================

# Every fan-out in this app sets its own per-walk concurrency constant
# (NAME_SEARCH_CONCURRENCY, SCREENS_FANOUT_CONCURRENCY in authors.py), but
# those only bound one walk at a time -- two simultaneous requests for a
# large author already double the in-flight count, and nothing upstream of
# this module caps the total across every walk running at once. Libex
# reaches Audible from an IP shared with the live seeder, and that IP has
# already been throttled into a VPN rotation once. audible_get is the one
# place every outbound Audible call passes through, so the bound lives here,
# process-wide, instead of at any individual call site: a per-call-site
# limit only expresses how eagerly that one walk wants to go, never what the
# shared IP can take at once across all of them.
AUDIBLE_CONCURRENCY_LIMIT = 10

# asyncio.Semaphore binds its internal waiter state to whichever event loop
# is running the first time it's touched. Under uvicorn that's one long-lived
# loop, but the test suite creates and tears down a fresh loop per test, and
# a semaphore built once at import time and reused across those loops risks
# waiters left over from a closed loop. Keying the instance to the current
# running loop and rebuilding it whenever that loop changes sidesteps this:
# a long-lived process creates it exactly once and keeps reusing it, and each
# fresh test loop gets its own fresh semaphore instead of inheriting stale
# state from whatever loop ran before it.
_audible_semaphore: asyncio.Semaphore | None = None
_audible_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_audible_semaphore() -> asyncio.Semaphore:
    global _audible_semaphore, _audible_semaphore_loop
    loop = asyncio.get_running_loop()
    if _audible_semaphore is None or _audible_semaphore_loop is not loop:
        _audible_semaphore = asyncio.Semaphore(AUDIBLE_CONCURRENCY_LIMIT)
        _audible_semaphore_loop = loop
    return _audible_semaphore


# ============================================================
# SHARED HTTP CLIENT
# ============================================================

# The semaphore already guarantees at most AUDIBLE_CONCURRENCY_LIMIT calls are
# ever in flight at once, so the pool is sized to match it exactly rather than
# httpx's defaults (100 / 20): there is never a use for more than 10 open
# connections, and keeping all 10 alive -- instead of the smaller default
# keepalive pool -- means a fan-out that reuses the same shared client across
# dozens of sequential requests (a prolific-author walk is ~57) gets a
# reused, already-negotiated connection almost every time instead of paying a
# fresh TCP+TLS handshake per call.
_AUDIBLE_POOL_LIMITS = httpx.Limits(
    max_connections=AUDIBLE_CONCURRENCY_LIMIT,
    max_keepalive_connections=AUDIBLE_CONCURRENCY_LIMIT,
)

# audible_get is called from three different lifetimes -- request handlers,
# asyncio.create_task background persisters, and the seeder's long-running
# task -- with no single object owning all three, exactly the situation
# _get_audible_semaphore above already solves. An httpx.AsyncClient binds to
# the running loop through its connection pool the same way a Semaphore binds
# through its waiter state, so this follows the identical shape: built lazily,
# keyed to the loop that built it, rebuilt when that loop changes. Under
# uvicorn that's once for the life of the process; under pytest-asyncio,
# which hands every test function its own loop, each test gets its own client
# instead of reusing pooled connections tied to a loop that's already gone.
_audible_client: httpx.AsyncClient | None = None
_audible_client_loop: asyncio.AbstractEventLoop | None = None


async def _close_stale_client(client: httpx.AsyncClient) -> None:
    """Closes a client left behind by a loop change. Best-effort: the client
    may never have opened a real connection (nothing to close), and awaiting
    aclose() on a loop other than the one that built it is unusual enough
    that a failure here should never surface as this request's error."""
    try:
        await client.aclose()
    except Exception:
        logger.debug("Failed closing a stale Audible HTTP client", exc_info=True)


def _get_audible_client() -> httpx.AsyncClient:
    global _audible_client, _audible_client_loop
    loop = asyncio.get_running_loop()
    if _audible_client is None or _audible_client_loop is not loop:
        stale = _audible_client
        # settings is process-wide and never reloaded at runtime (get_settings
        # is lru_cache'd), so reading audible_proxy_url once at construction
        # here -- instead of per call, as the old per-request client did --
        # can't go stale for the life of the process.
        _audible_client = httpx.AsyncClient(
            proxy=settings.audible_proxy_url or None,
            limits=_AUDIBLE_POOL_LIMITS,
        )
        _audible_client_loop = loop
        if stale is not None:
            asyncio.create_task(_close_stale_client(stale))
    return _audible_client


# ============================================================
# RETRY / BACKOFF
# ============================================================

# 429 and 5xx are the only responses worth retrying: they mean Audible (or
# its edge) is asking for a retry, not answering the request. A 404 is a
# real, permanent answer -- this database carries ~84k ISBN-keyed records
# that 404 for chapters in every region, and retrying those would just burn
# requests against the same already-throttled-once IP for nothing. Every
# other 4xx is a real answer too and is left alone the same way.
_RETRYABLE_STATUS_CODES = {429}


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES or 500 <= status_code < 600


# Kept small on purpose. AUTHOR_BOOKS_TIME_BUDGET_SECONDS in authors.py caps
# a whole discovery walk's wall-clock time, but that deadline is checked by
# the callers between requests -- it never reaches audible_get, since this
# function's signature (region, path, params, extra_headers) doesn't carry
# one. A wide fan-out (author-books discovery alone can fire ~60 concurrent
# requests) turning every throttled response into several extra seconds
# would eat into that budget fast with no way for this module to know it's
# happening, so attempts and backoff both stay deliberately small rather
# than aggressive. Making retries budget-aware would need an explicit
# optional deadline parameter threaded from authors.py's existing deadline
# value down through every intermediate call into audible_get itself --
# that's a real signature change and out of scope here.
AUDIBLE_MAX_ATTEMPTS = 3
AUDIBLE_RETRY_BASE_SECONDS = 0.5
AUDIBLE_RETRY_MAX_BACKOFF_SECONDS = 8.0
# Retry-After is Audible telling us exactly how long it wants us to wait.
# Honoring it is the point, but it's still capped so one large value can't
# stall a fan-out far past what a few retries should ever cost.
AUDIBLE_RETRY_AFTER_CAP_SECONDS = 10.0


def _parse_retry_after(value: str | None) -> float | None:
    """Parses a Retry-After header, which per spec is either a number of
    seconds or an HTTP-date. Returns None on anything unparseable so the
    caller falls back to computed backoff instead of guessing."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (retry_at - now).total_seconds())


def _compute_backoff_seconds(attempt: int, retry_after: float | None) -> float:
    """attempt is the zero-indexed count of attempts already made. Retry-After,
    when present, wins outright (capped); otherwise full-jitter exponential
    backoff, so a burst of concurrent callers hitting the same throttle
    don't all retry in lockstep."""
    if retry_after is not None:
        return min(retry_after, AUDIBLE_RETRY_AFTER_CAP_SECONDS)
    ceiling = min(
        AUDIBLE_RETRY_MAX_BACKOFF_SECONDS,
        AUDIBLE_RETRY_BASE_SECONDS * (2 ** attempt),
    )
    return random.uniform(0, ceiling)


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
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """
    Makes a GET request to the Audible API.
    Returns parsed JSON response.
    Raises AudibleAPIException on non-200 responses.

    extra_headers overlays get_region_headers for this call only and must
    contain module-level constants only — never a request-derived value.

    Bounded process-wide by AUDIBLE_CONCURRENCY_LIMIT concurrent in-flight
    requests regardless of caller, and retries a 429 or 5xx up to
    AUDIBLE_MAX_ATTEMPTS times with backoff (see the CONCURRENCY BOUND and
    RETRY / BACKOFF sections above). A 404 stays terminal and is never
    retried; neither is any other 4xx, nor a timeout or connection failure.
    """
    region = validate_region(region)
    url = get_audible_url(region, path)
    headers = get_region_headers(region)
    if extra_headers:
        headers = {**headers, **extra_headers}

    client = _get_audible_client()
    for attempt in range(AUDIBLE_MAX_ATTEMPTS):
        async with _get_audible_semaphore():
            try:
                response = await client.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=30.0,
                    follow_redirects=False,
                )
            except httpx.TimeoutException as e:
                raise AudibleAPIException(
                    f"Audible API timed out: {type(e).__name__} for {url}"
                )
            except httpx.RequestError as e:
                # Many httpx.RequestError subclasses (ConnectError, ReadError, etc.)
                # have an empty str(), so include the type and URL or the message is
                # blank and the failure is undiagnosable.
                detail = str(e) or type(e).__name__
                raise AudibleAPIException(
                    f"Audible API request failed: {detail} for {url}"
                )

        if response.status_code == 404:
            from app.core.exceptions import NotFoundException
            raise NotFoundException()

        if response.status_code == 200:
            return response.json()

        if _is_retryable_status(response.status_code):
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            attempts_left = AUDIBLE_MAX_ATTEMPTS - attempt - 1
            # A 429 is the early warning for the exact failure that cost
            # this project a VPN rotation once already, so it's logged
            # every time it's seen, whether or not this call still has
            # attempts left to absorb it -- a retry succeeding on the
            # next attempt must not make this go quiet.
            logger.warning(
                "Audible API throttled or degraded",
                extra={
                    "status_code": response.status_code,
                    "region": region,
                    "path": path,
                    "attempt": attempt + 1,
                    "max_attempts": AUDIBLE_MAX_ATTEMPTS,
                    "retry_after": retry_after,
                    "attempts_left": attempts_left,
                },
            )
            if attempts_left > 0:
                sleep_for = _compute_backoff_seconds(attempt, retry_after)
                await asyncio.sleep(sleep_for)
                continue

        raise AudibleAPIException(
            f"Audible API returned {response.status_code} for {url}"
        )