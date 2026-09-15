"""
Cache-TTL timing helper for date-windowed endpoints.
"""

# Standard library
from datetime import datetime, time, timedelta, timezone


def seconds_until_utc_midnight(now: datetime | None = None) -> int:
    """
    Returns the number of seconds until the next UTC midnight.

    Used as a cache TTL for date-windowed endpoints (new releases, coming
    soon) whose answer can't change until the calendar date rolls over.
    Caching until the next UTC midnight means the cached response is never
    stale within the day and refreshes lazily on the first request after
    the rollover. Always returns at least 1 second.
    """
    now = now or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date()
    next_midnight = datetime.combine(tomorrow, time.min, tzinfo=timezone.utc)
    return max(1, int((next_midnight - now).total_seconds()))