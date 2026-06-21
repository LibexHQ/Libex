"""
Shared utility functions.
"""

# Standard library
import re
from datetime import datetime, time, timedelta, timezone


def strip_html(text: str | None) -> str | None:
    """
    Strips HTML tags and cleans up Audible text artifacts.
    Handles: HTML tags, escaped quotes, literal \\r\\n sequences,
    leading/trailing quotes, and excessive whitespace.
    Returns None for empty, whitespace-only, or None input.
    """
    if not text:
        return None
    # Strip HTML tags
    cleaned = re.sub(r'<[^>]+>', '', text)
    # Replace literal \r\n and \n sequences with actual newlines
    cleaned = cleaned.replace('\\r\\n', '\n').replace('\\n', '\n')
    cleaned = cleaned.replace('\r\n', '\n').replace('\r', '\n')
    # Clean up escaped quotes
    cleaned = cleaned.replace('\\"', '"')
    # Strip wrapping quotes only if the entire string is quoted
    cleaned = cleaned.strip()
    if len(cleaned) >= 2 and cleaned[0] == '"' and cleaned[-1] == '"':
        cleaned = cleaned[1:-1].strip()
    # Collapse multiple newlines into double (paragraph breaks)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    # Collapse multiple spaces into one
    cleaned = re.sub(r' {2,}', ' ', cleaned)
    return cleaned.strip() or None


def strip_image_size_suffix(url: str | None) -> str | None:
    """
    Strips Audible image size suffixes from URLs.
    Converts e.g. https://example.com/image._SX500_.jpg
    to https://example.com/image.jpg
    """
    if not url:
        return None
    return re.sub(r'\._\w+_', '', url)


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