"""
Shared utility functions.
"""

# Standard library
import re


def strip_html(text: str | None) -> str | None:
    """
    Strips HTML tags from text.
    Returns None for empty, whitespace-only, or None input.
    Used to normalize Audible API responses before returning to consumers.
    """
    if not text:
        return None
    cleaned = re.sub(r'<[^>]+>', '', text)
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