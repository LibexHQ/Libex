"""
The retry and back-off policy for Audible requests: which statuses are worth
retrying, how many attempts, how long to wait, and how a Retry-After header
is read.
"""

# Standard library
import datetime
import random
from email.utils import parsedate_to_datetime

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


# Kept small on purpose. AUTHOR_BOOKS_TIME_BUDGET_SECONDS in authors/__init__.py caps
# a whole discovery walk's wall-clock time, but that deadline is checked by
# the callers between requests -- it never reaches LibexClient.get, since
# this method's signature (region, path, params, extra_headers) doesn't
# carry one. A wide fan-out (author-books discovery alone can fire ~60 concurrent
# requests) turning every throttled response into several extra seconds
# would eat into that budget fast with no way for this module to know it's
# happening, so attempts and backoff both stay deliberately small rather
# than aggressive. Making retries budget-aware would need an explicit
# optional deadline parameter threaded from authors.py's existing deadline
# value down through every intermediate call into LibexClient.get itself --
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
