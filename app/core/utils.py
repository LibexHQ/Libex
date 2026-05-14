"""
Shared utility functions.
"""

# Standard library
import re


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
    # Strip leading/trailing quotes left by Audible's bio wrapping
    cleaned = cleaned.strip().strip('"').strip()
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