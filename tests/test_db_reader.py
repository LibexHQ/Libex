"""
DB reader service unit tests.
Tests dict reconstruction, field mapping, and error handling.
All DB interactions are mocked — we test our logic not SQLAlchemy.
"""

# Standard library
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from app.services.db.reader import (
    _book_to_dict,
    _audible_link,
    get_book_from_db,
    get_books_from_db,
    get_author_from_db,
    get_author_books_from_db,
    get_series_from_db,
    get_series_books_from_db,
    search_series_from_db,
    get_track_from_db,
)


# ============================================================
# MOCK FACTORIES
# ============================================================

def _make_genre(asin="G001", name="Science Fiction", type_="Genres"):
    g = MagicMock()
    g.asin = asin
    g.name = name
    g.type = type_
    g.updated_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return g


def _make_narrator(name="Test Narrator"):
    n = MagicMock()
    n.name = name
    n.updated_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return n


def _make_author(id_=1, asin="B000APF21M", name="Frank Herbert", region="us"):
    a = MagicMock()
    a.id = id_
    a.asin = asin
    a.name = name
    a.region = region
    a.description = "An author."
    a.image = "https://example.com/img.jpg"
    a.updated_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    a.genres = []
    return a


def _make_series(asin="B00SERIES1", title="Dune Chronicles", region="us"):
    s = MagicMock()
    s.asin = asin
    s.title = title
    s.description = "A great series."
    s.region = region
    s.updated_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return s


def _make_book(asin="B08G9PRS1K", title="Test Book", region="us"):
    b = MagicMock()
    b.asin = asin
    b.title = title
    b.subtitle = None
    b.region = region
    b.description = "A description."
    b.summary = "A summary."
    b.publisher = "Test Publisher"
    b.copyright = None
    b.isbn = "9780000000000"
    b.language = "english"
    b.rating = 4.5
    b.release_date = datetime(2021, 1, 1, tzinfo=timezone.utc)
    b.length_minutes = 600
    b.explicit = False
    b.whisper_sync = False
    b.has_pdf = False
    b.image = "https://example.com/cover.jpg"
    b.book_format = None
    b.content_type = "Product"
    b.content_delivery_type = "SinglePartBook"
    b.episode_number = None
    b.episode_type = None
    b.sku = None
    b.sku_group = None
    b.is_listenable = True
    b.is_buyable = True
    b.updated_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    b.authors = [_make_author()]
    b.narrators = [_make_narrator()]
    b.genres = [_make_genre()]
    b.series = [_make_series()]
    return b


def _make_session_with_book(book=None):
    """Session that returns a single book from scalar_one_or_none."""
    session = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = book
    positions_result = MagicMock()
    positions_result.fetchall.return_value = []
    session.execute = AsyncMock(side_effect=[scalar_result, positions_result])
    return session


def _make_session_with_books(books=None):
    """Session that returns multiple books from scalars().all()."""
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.scalars.return_value.all.return_value = books or []
    positions_result = MagicMock()
    positions_result.fetchall.return_value = []
    # For each book we need a positions query, so side_effect rotates
    session.execute = AsyncMock(side_effect=[scalars_result] + [positions_result] * len(books or []))
    return session


# ============================================================
# _audible_link
# ============================================================

def test_audible_link_us_region():
    """US region produces correct Audible URL."""
    assert _audible_link("B08G9PRS1K", "us") == "https://audible.com/pd/B08G9PRS1K"


def test_audible_link_uk_region():
    """UK region produces correct Audible URL."""
    assert _audible_link("B08G9PRS1K", "uk") == "https://audible.co.uk/pd/B08G9PRS1K"


def test_audible_link_unknown_region_falls_back_to_com():
    """Unknown region falls back to .com TLD."""
    assert _audible_link("B08G9PRS1K", "xx") == "https://audible.com/pd/B08G9PRS1K"


# ============================================================
# _book_to_dict
# ============================================================

def test_book_to_dict_returns_asin():
    """Converted dict includes book ASIN."""
    book = _make_book()
    result = _book_to_dict(book, {})
    assert result["asin"] == "B08G9PRS1K"


def test_book_to_dict_returns_title():
    """Converted dict includes book title."""
    book = _make_book()
    result = _book_to_dict(book, {})
    assert result["title"] == "Test Book"


def test_book_to_dict_release_date_is_iso():
    """Release date is converted to ISO 8601 string."""
    book = _make_book()
    result = _book_to_dict(book, {})
    assert result["releaseDate"] is not None
    assert "T" in result["releaseDate"]


def test_book_to_dict_release_date_none_when_missing():
    """Missing release date returns None."""
    book = _make_book()
    book.release_date = None
    result = _book_to_dict(book, {})
    assert result["releaseDate"] is None


def test_book_to_dict_region_in_regions_list():
    """regions list contains the book's region."""
    book = _make_book()
    result = _book_to_dict(book, {})
    assert result["regions"] == ["us"]


def test_book_to_dict_authors_mapped():
    """Authors are mapped to list of dicts with correct fields."""
    book = _make_book()
    result = _book_to_dict(book, {})
    assert isinstance(result["authors"], list)
    assert len(result["authors"]) == 1
    assert result["authors"][0]["name"] == "Frank Herbert"
    assert result["authors"][0]["asin"] == "B000APF21M"
    assert "regions" in result["authors"][0]


def test_book_to_dict_narrators_mapped():
    """Narrators are mapped to list of dicts."""
    book = _make_book()
    result = _book_to_dict(book, {})
    assert isinstance(result["narrators"], list)
    assert result["narrators"][0]["name"] == "Test Narrator"


def test_book_to_dict_genres_include_better_type():
    """Genre dicts include betterType with trailing s stripped."""
    book = _make_book()
    result = _book_to_dict(book, {})
    assert result["genres"][0]["betterType"] == "genre"


def test_book_to_dict_genre_tags_better_type():
    """Tag genre type produces betterType of 'tag'."""
    book = _make_book()
    book.genres = [_make_genre(type_="Tags")]
    result = _book_to_dict(book, {})
    assert result["genres"][0]["betterType"] == "tag"


def test_book_to_dict_series_position_from_positions_dict():
    """Series position is pulled from the positions dict."""
    book = _make_book()
    result = _book_to_dict(book, {"B00SERIES1": "3"})
    assert result["series"][0]["position"] == "3"


def test_book_to_dict_series_position_none_when_missing():
    """Series position is None when not in positions dict."""
    book = _make_book()
    result = _book_to_dict(book, {})
    assert result["series"][0]["position"] is None


def test_book_to_dict_podcast_exposes_episode_fields():
    """Podcast content type exposes episode_number and episode_type."""
    book = _make_book()
    book.content_type = "Podcast"
    book.episode_number = "42"
    book.episode_type = "full"
    result = _book_to_dict(book, {})
    assert result["episodeNumber"] == "42"
    assert result["episodeType"] == "full"


def test_book_to_dict_non_podcast_hides_episode_fields():
    """Non-podcast content type hides episode fields."""
    book = _make_book()
    book.content_type = "Product"
    book.episode_number = "42"
    book.episode_type = "full"
    result = _book_to_dict(book, {})
    assert result["episodeNumber"] is None
    assert result["episodeType"] is None


def test_book_to_dict_audible_link_is_present():
    """Converted dict includes an Audible link."""
    book = _make_book()
    result = _book_to_dict(book, {})
    assert result["link"].startswith("https://audible")
    assert "B08G9PRS1K" in result["link"]


def test_book_to_dict_is_available_mirrors_is_buyable():
    """isAvailable field mirrors isBuyable."""
    book = _make_book()
    book.is_buyable = True
    result = _book_to_dict(book, {})
    assert result["isAvailable"] == result["isBuyable"]


def test_book_to_dict_empty_relationships():
    """Book with no authors/narrators/genres/series returns empty lists."""
    book = _make_book()
    book.authors = []
    book.narrators = []
    book.genres = []
    book.series = []
    result = _book_to_dict(book, {})
    assert result["authors"] == []
    assert result["narrators"] == []
    assert result["genres"] == []
    assert result["series"] == []


# ============================================================
# get_book_from_db
# ============================================================

@pytest.mark.asyncio
async def test_get_book_from_db_returns_dict_on_hit():
    """Returns a dict when book is found."""
    book = _make_book()
    session = _make_session_with_book(book)
    result = await get_book_from_db(session, "B08G9PRS1K")
    assert result is not None
    assert result["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_book_from_db_returns_none_on_miss():
    """Returns None when book is not found."""
    session = _make_session_with_book(None)
    result = await get_book_from_db(session, "B08G9PRS1K")
    assert result is None


@pytest.mark.asyncio
async def test_get_book_from_db_returns_none_on_exception():
    """Returns None when DB raises an exception."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception("DB error"))
    result = await get_book_from_db(session, "B08G9PRS1K")
    assert result is None


# ============================================================
# get_books_from_db
# ============================================================

@pytest.mark.asyncio
async def test_get_books_from_db_returns_list():
    """Returns a list of dicts for multiple ASINs."""
    books = [_make_book("B08G9PRS1K"), _make_book("B08G9PRS2K")]
    session = _make_session_with_books(books)
    result = await get_books_from_db(session, ["B08G9PRS1K", "B08G9PRS2K"])
    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_books_from_db_returns_empty_list_on_exception():
    """Returns empty list when DB raises an exception."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception("DB error"))
    result = await get_books_from_db(session, ["B08G9PRS1K"])
    assert result == []


# ============================================================
# get_author_from_db
# ============================================================

@pytest.mark.asyncio
async def test_get_author_from_db_returns_dict_on_hit():
    """Returns a dict when author is found."""
    author = _make_author()
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = author
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")
    assert result is not None
    assert result["asin"] == "B000APF21M"
    assert result["name"] == "Frank Herbert"


@pytest.mark.asyncio
async def test_get_author_from_db_returns_none_on_miss():
    """Returns None when author is not found."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")
    assert result is None


@pytest.mark.asyncio
async def test_get_author_from_db_maps_genres():
    """Author genres are mapped to dicts with betterType — not hardcoded empty list."""
    author = _make_author()
    author.genres = [_make_genre()]
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = author
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")
    assert isinstance(result["genres"], list)
    assert len(result["genres"]) == 1
    assert result["genres"][0]["name"] == "Science Fiction"
    assert result["genres"][0]["betterType"] == "genre"


@pytest.mark.asyncio
async def test_get_author_from_db_empty_genres_returns_empty_list():
    """Author with no genres returns empty list, not None."""
    author = _make_author()
    author.genres = []
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = author
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")
    assert result["genres"] == []


@pytest.mark.asyncio
async def test_get_author_from_db_includes_regions_list():
    """Author dict includes regions list."""
    author = _make_author()
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = author
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")
    assert result["regions"] == ["us"]


@pytest.mark.asyncio
async def test_get_author_from_db_returns_none_on_exception():
    """Returns None when DB raises an exception."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception("DB error"))
    result = await get_author_from_db(session, "B000APF21M", "us")
    assert result is None


# ============================================================
# get_author_books_from_db
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_from_db_returns_list():
    """Returns a list of book dicts for an author."""
    books = [_make_book()]
    session = _make_session_with_books(books)
    result = await get_author_books_from_db(session, "B000APF21M", "us")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_author_books_from_db_returns_empty_list_on_miss():
    """Returns empty list when author has no books."""
    session = _make_session_with_books([])
    result = await get_author_books_from_db(session, "B000APF21M", "us")
    assert result == []


@pytest.mark.asyncio
async def test_get_author_books_from_db_returns_empty_list_on_exception():
    """Returns empty list when DB raises an exception."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception("DB error"))
    result = await get_author_books_from_db(session, "B000APF21M", "us")
    assert result == []


# ============================================================
# get_series_from_db
# ============================================================

@pytest.mark.asyncio
async def test_get_series_from_db_returns_dict_on_hit():
    """Returns a dict when series is found."""
    series = _make_series()
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = series
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_series_from_db(session, "B00SERIES1")
    assert result is not None
    assert result["asin"] == "B00SERIES1"
    assert result["name"] == "Dune Chronicles"


@pytest.mark.asyncio
async def test_get_series_from_db_returns_none_on_miss():
    """Returns None when series is not found."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_series_from_db(session, "B00SERIES1")
    assert result is None


@pytest.mark.asyncio
async def test_get_series_from_db_title_mapped_to_name():
    """Series title column is mapped to name field in response."""
    series = _make_series(title="Dune Chronicles")
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = series
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_series_from_db(session, "B00SERIES1")
    assert result["name"] == "Dune Chronicles"
    assert "title" not in result


@pytest.mark.asyncio
async def test_get_series_from_db_returns_none_on_exception():
    """Returns None when DB raises an exception."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception("DB error"))
    result = await get_series_from_db(session, "B00SERIES1")
    assert result is None


# ============================================================
# get_series_books_from_db
# ============================================================

@pytest.mark.asyncio
async def test_get_series_books_from_db_returns_list():
    """Returns a list of book dicts for a series."""
    books = [_make_book()]
    session = _make_session_with_books(books)
    result = await get_series_books_from_db(session, "B00SERIES1")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_series_books_from_db_returns_empty_list_on_miss():
    """Returns empty list when series has no books."""
    session = _make_session_with_books([])
    result = await get_series_books_from_db(session, "B00SERIES1")
    assert result == []


@pytest.mark.asyncio
async def test_get_series_books_from_db_returns_empty_list_on_exception():
    """Returns empty list when DB raises an exception."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception("DB error"))
    result = await get_series_books_from_db(session, "B00SERIES1")
    assert result == []


# ============================================================
# search_series_from_db
# ============================================================

@pytest.mark.asyncio
async def test_search_series_from_db_returns_list():
    """Returns a list of series dicts matching the name."""
    series_list = [_make_series(), _make_series(asin="B00SERIES2", title="Dune Messiah")]
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = series_list
    session.execute = AsyncMock(return_value=result_mock)

    result = await search_series_from_db(session, "Dune")
    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_search_series_from_db_maps_title_to_name():
    """Series title is mapped to name in search results."""
    series_list = [_make_series(title="Dune Chronicles")]
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = series_list
    session.execute = AsyncMock(return_value=result_mock)

    result = await search_series_from_db(session, "Dune")
    assert result[0]["name"] == "Dune Chronicles"
    assert "title" not in result[0]


@pytest.mark.asyncio
async def test_search_series_from_db_returns_empty_list_on_exception():
    """Returns empty list when DB raises an exception."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception("DB error"))
    result = await search_series_from_db(session, "Dune")
    assert result == []


# ============================================================
# get_track_from_db
# ============================================================

@pytest.mark.asyncio
async def test_get_track_from_db_returns_chapters_on_hit():
    """Returns the chapters JSONB dict when track is found."""
    track = MagicMock()
    track.chapters = {"chapters": [], "runtimeLengthMs": 4800000}
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = track
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_track_from_db(session, "B08G9PRS1K")
    assert result == {"chapters": [], "runtimeLengthMs": 4800000}


@pytest.mark.asyncio
async def test_get_track_from_db_returns_none_on_miss():
    """Returns None when no track data exists."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_track_from_db(session, "B08G9PRS1K")
    assert result is None


@pytest.mark.asyncio
async def test_get_track_from_db_returns_none_on_exception():
    """Returns None when DB raises an exception."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception("DB error"))
    result = await get_track_from_db(session, "B08G9PRS1K")
    assert result is None