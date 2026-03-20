"""
Authors service unit tests.
Tests normalization helpers without hitting Audible.
"""

# Third party
import pytest

# Local
from app.services.audible.authors import _normalize_author, _generate_session_id


# ============================================================
# SESSION ID TESTS
# ============================================================

def test_generate_session_id_returns_string():
    """Session ID is a string."""
    assert isinstance(_generate_session_id(), str)


def test_generate_session_id_has_correct_length():
    """Session ID is 32 characters long."""
    assert len(_generate_session_id()) == 32


def test_generate_session_id_is_unique():
    """Each session ID is unique."""
    ids = {_generate_session_id() for _ in range(100)}
    assert len(ids) == 100


def test_generate_session_id_is_alphanumeric():
    """Session ID contains only lowercase alphanumeric characters."""
    session_id = _generate_session_id()
    assert session_id.isalnum()
    assert session_id == session_id.lower()


# ============================================================
# NORMALIZE AUTHOR TESTS
# ============================================================

def test_normalize_author_extracts_name():
    """Normalized author includes name from contributor."""
    data = {"contributor": {"name": "Frank Herbert", "bio": None, "profile_image_url": None}}
    result = _normalize_author(data, "B000APF21M", "us")
    assert result["name"] == "Frank Herbert"


def test_normalize_author_extracts_bio():
    """Normalized author includes bio as description."""
    data = {"contributor": {"name": "Frank Herbert", "bio": "An author.", "profile_image_url": None}}
    result = _normalize_author(data, "B000APF21M", "us")
    assert result["description"] == "An author."


def test_normalize_author_extracts_image():
    """Normalized author includes profile image URL."""
    data = {"contributor": {"name": "Frank Herbert", "bio": None, "profile_image_url": "https://example.com/img.jpg"}}
    result = _normalize_author(data, "B000APF21M", "us")
    assert result["image"] == "https://example.com/img.jpg"


def test_normalize_author_sets_asin():
    """Normalized author includes provided ASIN."""
    data = {"contributor": {"name": "Frank Herbert", "bio": None, "profile_image_url": None}}
    result = _normalize_author(data, "B000APF21M", "us")
    assert result["asin"] == "B000APF21M"


def test_normalize_author_sets_region():
    """Normalized author includes provided region."""
    data = {"contributor": {"name": "Frank Herbert", "bio": None, "profile_image_url": None}}
    result = _normalize_author(data, "B000APF21M", "uk")
    assert result["region"] == "uk"


def test_normalize_author_strips_tabs_from_name():
    """Normalized author name has tabs stripped."""
    data = {"contributor": {"name": "\tFrank Herbert\t", "bio": None, "profile_image_url": None}}
    result = _normalize_author(data, "B000APF21M", "us")
    assert result["name"] == "Frank Herbert"


def test_normalize_author_empty_bio_returns_none():
    """Empty bio returns None for description."""
    data = {"contributor": {"name": "Frank Herbert", "bio": "", "profile_image_url": None}}
    result = _normalize_author(data, "B000APF21M", "us")
    assert result["description"] is None


def test_normalize_author_handles_missing_contributor():
    """Normalizer handles response without contributor wrapper."""
    data = {"name": "Frank Herbert", "bio": "An author.", "profile_image_url": None}
    result = _normalize_author(data, "B000APF21M", "us")
    assert result["asin"] == "B000APF21M"