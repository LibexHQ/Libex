"""
Authors service unit tests.
Tests normalization helpers without hitting Audible.
"""

# Standard library
from unittest.mock import AsyncMock, patch

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


def test_generate_session_id_has_correct_format():
    """Session ID matches AudiMeta format: 000-XXXXXXX-XXXXXXX."""
    session_id = _generate_session_id()
    parts = session_id.split("-")
    assert len(parts) == 3
    assert parts[0] == "000"
    assert len(parts[1]) == 7
    assert len(parts[2]) == 7


def test_generate_session_id_has_correct_length():
    """Session ID is 19 characters long."""
    assert len(_generate_session_id()) == 19


def test_generate_session_id_segments_are_digits():
    """Session ID variable segments contain only digits."""
    session_id = _generate_session_id()
    parts = session_id.split("-")
    assert parts[1].isdigit()
    assert parts[2].isdigit()


def test_generate_session_id_is_unique():
    """Each session ID is unique."""
    ids = {_generate_session_id() for _ in range(100)}
    assert len(ids) == 100


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


def test_normalize_author_sets_regions_list():
    """Normalized author includes regions list matching AudiMeta MinimalAuthorDto."""
    data = {"contributor": {"name": "Frank Herbert", "bio": None, "profile_image_url": None}}
    result = _normalize_author(data, "B000APF21M", "us")
    assert result["regions"] == ["us"]


def test_normalize_author_includes_id_field():
    """Normalized author includes id field matching AudiMeta MinimalAuthorDto."""
    data = {"contributor": {"name": "Frank Herbert", "bio": None, "profile_image_url": None}}
    result = _normalize_author(data, "B000APF21M", "us")
    assert "id" in result
    assert result["id"] is None


def test_normalize_author_includes_updated_at():
    """Normalized author includes updatedAt field."""
    data = {"contributor": {"name": "Frank Herbert", "bio": None, "profile_image_url": None}}
    result = _normalize_author(data, "B000APF21M", "us")
    assert "updatedAt" in result
    assert result["updatedAt"] is not None


def test_normalize_author_includes_genres():
    """Normalized author includes empty genres list matching AudiMeta AuthorDto."""
    data = {"contributor": {"name": "Frank Herbert", "bio": None, "profile_image_url": None}}
    result = _normalize_author(data, "B000APF21M", "us")
    assert result["genres"] == []


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


# ============================================================
# DB FALLBACK TESTS
# ============================================================

@pytest.mark.asyncio
async def test_get_author_falls_back_to_db_when_audible_fails():
    """Falls back to DB when Audible is unavailable."""
    from app.services.audible.authors import get_author

    mock_session = AsyncMock()
    db_author = {
        "id": 1, "asin": "B000APF21M", "name": "Frank Herbert",
        "region": "us", "regions": ["us"], "description": "From DB",
        "image": None, "genres": [], "updatedAt": "2024-01-01T00:00:00+00:00",
    }

    with patch("app.services.audible.authors.audible_get", side_effect=Exception("Audible down")), \
         patch("app.services.audible.authors.get_author_from_db", new_callable=AsyncMock, return_value=db_author), \
         patch("app.services.audible.authors.cache.get", return_value=None):
        result = await get_author("B000APF21M", "us", mock_session)
        assert result["name"] == "Frank Herbert"
        assert result["description"] == "From DB"


@pytest.mark.asyncio
async def test_get_author_falls_back_to_cache_when_db_empty():
    """Falls back to cache when Audible is down and DB has no results."""
    from app.services.audible.authors import get_author

    mock_session = AsyncMock()
    cached_author = {
        "id": None, "asin": "B000APF21M", "name": "Frank Herbert (cached)",
        "region": "us", "regions": ["us"], "description": None,
        "image": None, "genres": [], "updatedAt": None,
    }

    with patch("app.services.audible.authors.audible_get", side_effect=Exception("Audible down")), \
         patch("app.services.audible.authors.get_author_from_db", new_callable=AsyncMock, return_value=None), \
         patch("app.services.audible.authors.cache.get", return_value=cached_author):
        result = await get_author("B000APF21M", "us", mock_session)
        assert result["name"] == "Frank Herbert (cached)"


@pytest.mark.asyncio
async def test_get_author_writes_to_db_on_success():
    """Writes author profile to DB after successful Audible fetch."""
    from app.services.audible.authors import get_author

    mock_session = AsyncMock()
    mock_response = {
        "contributor": {
            "name": "Frank Herbert",
            "bio": "An author.",
            "profile_image_url": None,
        }
    }

    with patch("app.services.audible.authors.audible_get", return_value=mock_response), \
         patch("app.services.audible.authors.upsert_author_profile", new_callable=AsyncMock) as mock_upsert, \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.cache.set", new_callable=AsyncMock):
        await get_author("B000APF21M", "us", mock_session)
        mock_upsert.assert_called_once()