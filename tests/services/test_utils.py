"""
Core utility function tests.
"""

# Local
from app.core.utils import strip_html, strip_image_size_suffix


# ============================================================
# STRIP HTML TESTS
# ============================================================

def test_strip_html_removes_tags():
    """HTML tags are removed from text."""
    assert strip_html("<p>Hello world</p>") == "Hello world"


def test_strip_html_removes_nested_tags():
    """Nested HTML tags are removed."""
    assert strip_html("<p><strong>Bold</strong> text</p>") == "Bold text"


def test_strip_html_returns_none_for_none():
    """None input returns None."""
    assert strip_html(None) is None


def test_strip_html_returns_none_for_empty():
    """Empty string returns None."""
    assert strip_html("") is None


def test_strip_html_returns_none_for_whitespace():
    """Whitespace-only string returns None."""
    assert strip_html("   ") is None


def test_strip_html_strips_whitespace():
    """Leading and trailing whitespace is stripped."""
    assert strip_html("  Hello  ") == "Hello"


def test_strip_html_returns_none_for_tags_only():
    """String containing only tags returns None."""
    assert strip_html("<p></p>") is None


def test_strip_html_preserves_text_content():
    result = strip_html("<p>Project Hail Mary</p>")
    assert result is not None
    assert "Project Hail Mary" in result


def test_strip_html_handles_complex_html():
    """Complex nested HTML is fully stripped."""
    html = "<p><b>THE #1</b> <i>NEW YORK TIMES</i> BESTSELLER</p>"
    assert strip_html(html) == "THE #1 NEW YORK TIMES BESTSELLER"


# ============================================================
# STRIP HTML — ESCAPE SEQUENCE CLEANING
# ============================================================

def test_strip_html_cleans_escaped_quotes():
    """Escaped quotes are unescaped."""
    assert strip_html('He said \\"hello\\"') == 'He said "hello"'


def test_strip_html_cleans_literal_newlines():
    """Literal \\r\\n sequences become actual newlines."""
    result = strip_html('First paragraph.\\r\\n\\r\\nSecond paragraph.')
    assert '\n' in result
    assert '\\r' not in result
    assert '\\n' not in result


def test_strip_html_strips_wrapping_quotes():
    """Leading and trailing quotes from Audible bio wrapping are removed."""
    assert strip_html('"A great author."') == "A great author."


def test_strip_html_collapses_excessive_newlines():
    """Three or more consecutive newlines collapse to two."""
    result = strip_html('First.\n\n\n\n\nSecond.')
    assert result is not None
    assert '\n\n\n' not in result
    assert 'First.' in result
    assert 'Second.' in result


def test_strip_html_collapses_multiple_spaces():
    """Multiple consecutive spaces collapse to one."""
    assert strip_html('Hello    world') == 'Hello world'


def test_strip_html_handles_audible_bio_mess():
    """Handles the full Audible contributor bio format."""
    bio = '\\"Ted Dekker is a true master.\\r\\n\\r\\nHe has sold millions.\\"'
    result = strip_html(bio)
    assert result is not None
    assert '\\"' not in result
    assert '\\r' not in result
    assert '\\n' not in result
    assert 'Ted Dekker' in result
    assert 'He has sold millions.' in result


# ============================================================
# STRIP IMAGE SIZE SUFFIX TESTS
# ============================================================

def test_strip_image_size_suffix_removes_suffix():
    url = "https://example.com/image._SX500_.jpg"
    result = strip_image_size_suffix(url)
    assert result is not None
    assert "._SX500_." not in result


def test_strip_image_size_suffix_returns_none_for_none():
    """None input returns None."""
    assert strip_image_size_suffix(None) is None


def test_strip_image_size_suffix_returns_none_for_empty():
    """Empty string returns None."""
    assert strip_image_size_suffix("") is None


def test_strip_image_size_suffix_preserves_base_url():
    url = "https://m.media-amazon.com/images/I/81Nzlrfud+L._SX500_.jpg"
    result = strip_image_size_suffix(url)
    assert result is not None
    assert "m.media-amazon.com" in result


def test_strip_image_size_suffix_handles_url_without_suffix():
    """URL without size suffix is returned unchanged."""
    url = "https://example.com/image.jpg"
    result = strip_image_size_suffix(url)
    assert result == url


def test_strip_image_size_suffix_handles_various_sizes():
    suffixes = ["._SX500_.", "._SY300_."]
    for suffix in suffixes:
        url = f"https://example.com/image{suffix}jpg"
        result = strip_image_size_suffix(url)
        assert result is not None
        assert "._" not in result, f"Suffix not removed: {suffix}"