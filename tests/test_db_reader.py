"""
DB reader service unit tests.
Tests dict reconstruction, field mapping, and error handling.
All DB interactions are mocked — we test our logic not SQLAlchemy.
"""

# Standard library
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

# Third party
import pytest

# Local
from app.services.db.reader import (
    _book_to_dict,
    _audible_link,
    _get_series_positions_batch,
    get_book_from_db,
    get_books_from_db,
    get_author_from_db,
    get_author_books_from_db,
    get_series_from_db,
    get_series_books_from_db,
    search_series_from_db,
    get_track_from_db,
    get_db_stats,
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
async def test_get_book_from_db_coerces_null_plans_to_empty_list():
    """A stored NULL in the nullable plans column must not reach the response
    model as None. BookResponse declares plans as a list, and its
    default_factory only applies when the key is absent — an explicit None
    raises ResponseValidationError, which escapes the middleware stack as a
    dropped connection rather than a 5xx, so the caller gets nothing at all."""
    book = _make_book()
    book.plans = None
    session = _make_session_with_book(book)
    result = await get_book_from_db(session, "B08G9PRS1K")
    assert result["plans"] == []


@pytest.mark.asyncio
async def test_get_book_from_db_null_plans_row_satisfies_the_response_model():
    """The reader's output for a NULL-plans row must validate against the
    response model that the route declares — the reader returning [] is only
    half the contract."""
    from app.api.routes.books.schemas import BookResponse

    book = _make_book()
    book.plans = None
    session = _make_session_with_book(book)
    result = await get_book_from_db(session, "B08G9PRS1K")
    assert BookResponse.model_validate(result).plans == []


@pytest.mark.asyncio
async def test_get_book_from_db_preserves_populated_plans():
    """Coercing NULL must not flatten a real plan list."""
    book = _make_book()
    book.plans = ["US Minerva", "Radio"]
    session = _make_session_with_book(book)
    result = await get_book_from_db(session, "B08G9PRS1K")
    assert result["plans"] == ["US Minerva", "Radio"]


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
    result_mock.scalars.return_value.all.return_value = [author]
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
    result_mock.scalars.return_value.all.return_value = []
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
    result_mock.scalars.return_value.all.return_value = [author]
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
    result_mock.scalars.return_value.all.return_value = [author]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")
    assert result["genres"] == []


@pytest.mark.asyncio
async def test_get_author_from_db_includes_regions_list():
    """Author dict includes regions list."""
    author = _make_author()
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [author]
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


@pytest.mark.asyncio
async def test_get_author_from_db_single_row_output_unchanged():
    """A single matching row produces the exact dict the pre-merge code did —
    the multi-row merge path must be a no-op in the overwhelmingly common case."""
    author = _make_author()
    author.genres = [_make_genre()]
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [author]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result == {
        "id": author.id,
        "asin": author.asin,
        "name": author.name,
        "description": author.description,
        "image": author.image,
        "region": author.region,
        "regions": [author.region],
        "genres": [
            {
                "asin": "G001",
                "name": "Science Fiction",
                "type": "Genres",
                "betterType": "genre",
                "updatedAt": author.genres[0].updated_at.isoformat(),
            }
        ],
        "updatedAt": author.updated_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_get_author_from_db_merges_duplicate_rows_content_independent_of_identity():
    """When two rows share (asin, region) under different name spellings, the
    oldest row supplies identity regardless of which row carries more content
    — but description and image are still pulled from whichever row actually
    holds them, and both rows' genres are unioned into the result."""
    sparse = _make_author(id_=2, name="Frank  Herbert")
    sparse.description = None
    sparse.image = None
    sparse.genres = [_make_genre(asin="G002", name="Fantasy")]

    rich = _make_author(id_=5, name="Frank Herbert")
    rich.description = "A full biography."
    rich.image = "https://example.com/rich.jpg"
    rich.genres = [_make_genre(asin="G001", name="Science Fiction")]

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [sparse, rich]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    # Identity is the oldest row (sparse, id 2) even though it contributes
    # neither description nor image to the merged output.
    assert result["id"] == 2
    assert result["name"] == "Frank  Herbert"
    assert result["description"] == "A full biography."
    assert result["image"] == "https://example.com/rich.jpg"
    assert {g["asin"] for g in result["genres"]} == {"G001", "G002"}


@pytest.mark.asyncio
async def test_get_author_from_db_description_coalesces_from_sibling_when_base_lacks_one():
    """A base row whose own description is absent still gets a real one from
    a sibling row, since the base's null measures as absent against the
    sibling's real text. The base row's own image is left untouched here
    because it already carries a real value — image's own id-order
    preference is exercised separately, not by this fixture."""
    base = _make_author(id_=1)
    base.description = None
    base.image = "https://example.com/img.jpg"

    sibling = _make_author(id_=9)
    sibling.description = "Filled in from the sibling."
    sibling.image = None

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [base, sibling]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result["id"] == 1
    assert result["description"] == "Filled in from the sibling."
    assert result["image"] == "https://example.com/img.jpg"


@pytest.mark.asyncio
async def test_get_author_from_db_base_identity_is_always_the_oldest_row():
    """Identity (id/name/region) always comes from the oldest row — there is
    no scoring or tie-break involved, the choice is unconditional."""
    older = _make_author(id_=3)
    newer = _make_author(id_=7)

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [newer, older]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")
    assert result["id"] == 3


@pytest.mark.asyncio
async def test_get_author_from_db_no_duplicate_log_on_single_row(caplog):
    """No duplicate-collapse warning fires for the common single-row case."""
    author = _make_author()
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [author]
    session.execute = AsyncMock(return_value=result_mock)

    with caplog.at_level(logging.WARNING):
        result = await get_author_from_db(session, "B000APF21M", "us")

    assert result is not None
    assert not any("Multiple author rows" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_author_from_db_logs_duplicate_collapse_with_structured_fields(caplog):
    """The duplicate-collapse log carries asin/region/row_count as structured
    extra fields rather than interpolated into the message, so Axiom can
    aggregate across authors instead of grouping every asin separately."""
    rich = _make_author(id_=5)
    sparse = _make_author(id_=2)

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [sparse, rich]
    session.execute = AsyncMock(return_value=result_mock)

    with caplog.at_level(logging.WARNING):
        result = await get_author_from_db(session, "B000APF21M", "us")

    assert result is not None
    matches = [r for r in caplog.records if "Multiple author rows" in r.getMessage()]
    assert len(matches) == 1
    record = matches[0]
    assert "B000APF21M" not in record.getMessage()
    assert record.asin == "B000APF21M"
    assert record.region == "us"
    assert record.row_count == 2


@pytest.mark.asyncio
async def test_get_author_from_db_image_prefers_base_but_description_does_not():
    """Image selection is purely order-based (first truthy value in id
    order), so the base row's own populated image always wins when present.
    Description selection is length-based and does not care which row
    supplies identity — a longer sibling description replaces the base's own
    shorter one, because content selection and identity selection are
    independent criteria."""
    base = _make_author(id_=1)
    base.description = "Base bio."
    base.image = "https://example.com/base.jpg"

    sibling = _make_author(id_=9)
    sibling.description = "Sibling bio, considerably longer than the base row's own text."
    sibling.image = "https://example.com/sibling.jpg"

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [base, sibling]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result["id"] == 1
    assert result["image"] == "https://example.com/base.jpg"
    assert result["description"] == sibling.description


@pytest.mark.asyncio
async def test_get_author_from_db_genre_union_dedups_by_asin_not_name_or_type():
    """Genre identity for the union is the genre's asin, not its name or type —
    two rows carrying the same genre asin under a different name/type collapse
    into one entry (first-seen wins), while two different genre asins that
    happen to share a name stay distinct."""
    base = _make_author(id_=1)
    base.genres = [
        _make_genre(asin="G001", name="Science Fiction", type_="Genres"),
        _make_genre(asin="G003", name="Adventure", type_="Genres"),
    ]

    sibling = _make_author(id_=9)
    sibling.genres = [
        _make_genre(asin="G001", name="Sci-Fi", type_="Tags"),
        _make_genre(asin="G004", name="Adventure", type_="Genres"),
    ]

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [base, sibling]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    genre_asins = {g["asin"] for g in result["genres"]}
    assert genre_asins == {"G001", "G003", "G004"}
    g001 = next(g for g in result["genres"] if g["asin"] == "G001")
    assert g001["name"] == "Science Fiction"


@pytest.mark.asyncio
async def test_get_author_from_db_merges_three_duplicate_rows():
    """The merge generalizes past two rows — three duplicate spellings still
    converge on the oldest id as base identity, select the longest available
    description and the first available image across all three (not just a
    pair), and union genres from all three. Fed out of id order to prove the
    defensive sort, not just the ORDER BY, is doing the work."""
    a1 = _make_author(id_=10, name="Frank Herbert")
    a1.description = None
    a1.image = None
    a1.genres = [_make_genre(asin="G001", name="Science Fiction")]

    a2 = _make_author(id_=4, name="Frank  Herbert")
    a2.description = "Filled from a2."
    a2.image = None
    a2.genres = [_make_genre(asin="G002", name="Fantasy")]

    a3 = _make_author(id_=7, name="Frank Herbert ")
    a3.description = None
    a3.image = "https://example.com/a3.jpg"
    a3.genres = [_make_genre(asin="G003", name="Horror")]

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [a1, a2, a3]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    # Identity is unconditionally the oldest id: a2 (4). a2 also happens to
    # hold the only description, so it wins that too. Image comes from a3
    # (7) — the first id-ordered row after a2 that actually has one.
    assert result["id"] == 4
    assert result["description"] == "Filled from a2."
    assert result["image"] == "https://example.com/a3.jpg"
    assert {g["asin"] for g in result["genres"]} == {"G001", "G002", "G003"}


@pytest.mark.asyncio
async def test_get_author_from_db_empty_string_never_shadows_real_content():
    """An empty-string description/image is exactly as sparse as a null one:
    it must not block a genuinely populated sibling from supplying either
    field. Identity still comes from the oldest row regardless of which row
    supplied the content."""
    empty = _make_author(id_=2)
    empty.description = ""
    empty.image = ""

    populated = _make_author(id_=6)
    populated.description = "Has content."
    populated.image = "https://example.com/img.jpg"

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [empty, populated]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result["id"] == 2
    assert result["description"] == "Has content."
    assert result["image"] == "https://example.com/img.jpg"


@pytest.mark.asyncio
async def test_get_author_from_db_whitespace_only_description_loses_to_real_content():
    """A whitespace-only description measures as absent, exactly like
    _longer_wins on the write path — it must not block a real description
    held by another row, even though the whitespace-only row is the older one
    and therefore still supplies identity."""
    whitespace = _make_author(id_=3)
    whitespace.description = "   "
    whitespace.image = None

    real = _make_author(id_=8)
    real.description = "A real biography."
    real.image = None

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [whitespace, real]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result["id"] == 3
    assert result["description"] == "A real biography."


@pytest.mark.asyncio
async def test_get_author_from_db_older_row_with_shorter_description_loses_to_longer_sibling():
    """The mainline duplicate case: both rows carry a real description (e.g.
    upsert_author_profile writes both description and image on every profile
    fetch, so two name spellings both end up fully populated). The older row
    still supplies identity, but the longer description wins regardless of
    which row is older — content and identity are not the same decision."""
    older_short = _make_author(id_=2)
    older_short.description = "A short stub."
    older_short.image = None

    newer_long = _make_author(id_=9)
    newer_long.description = (
        "A much longer biography with considerably more detail than the "
        "short stub the older row happened to be written with."
    )
    newer_long.image = None

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [older_short, newer_long]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result["id"] == 2
    assert result["description"] == newer_long.description


@pytest.mark.asyncio
async def test_get_author_from_db_output_is_order_independent():
    """The same candidate rows fed to the merge in a different order produce
    byte-identical output — determinism does not depend on the order the
    query happens to return rows in."""
    a = _make_author(id_=2)
    a.description = "Short."
    a.image = None
    a.genres = [_make_genre(asin="G001", name="Science Fiction")]

    b = _make_author(id_=9)
    b.description = "A considerably longer biography than the other row."
    b.image = "https://example.com/img.jpg"
    b.genres = [_make_genre(asin="G002", name="Fantasy")]

    forward_session = AsyncMock()
    forward_result_mock = MagicMock()
    forward_result_mock.scalars.return_value.all.return_value = [a, b]
    forward_session.execute = AsyncMock(return_value=forward_result_mock)

    reversed_session = AsyncMock()
    reversed_result_mock = MagicMock()
    reversed_result_mock.scalars.return_value.all.return_value = [b, a]
    reversed_session.execute = AsyncMock(return_value=reversed_result_mock)

    forward = await get_author_from_db(forward_session, "B000APF21M", "us")
    reversed_ = await get_author_from_db(reversed_session, "B000APF21M", "us")

    assert forward == reversed_


@pytest.mark.asyncio
async def test_get_author_from_db_whitespace_only_image_loses_to_real_url():
    """A whitespace-only image on the older row must not shadow a real URL
    held by a sibling — the same absent-measurement that protects description
    against a whitespace-only value applies to image too."""
    whitespace = _make_author(id_=1)
    whitespace.description = "An author."
    whitespace.image = "   "

    real = _make_author(id_=4)
    real.description = "An author."
    real.image = "https://example.com/real.jpg"

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [whitespace, real]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result["id"] == 1
    assert result["image"] == "https://example.com/real.jpg"


@pytest.mark.asyncio
async def test_get_author_from_db_single_row_empty_string_image_stays_empty_string():
    """A lone row's blank-but-not-null image (`""`) is returned verbatim, not
    coerced to null — that distinction is what tells "Audible answered blank"
    apart from "Audible never answered", and the single-row path must match
    what returning that row's raw field would have produced."""
    author = _make_author()
    author.image = ""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [author]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result["image"] == ""


@pytest.mark.asyncio
async def test_get_author_from_db_updated_at_is_the_max_across_candidates():
    """updatedAt reflects the most recently changed candidate row, not
    necessarily the base row's own timestamp — content can come from a newer
    sibling, so the caller-visible updatedAt must move too."""
    base = _make_author(id_=1)
    base.updated_at = datetime(2023, 1, 1, tzinfo=timezone.utc)

    newer_sibling = _make_author(id_=5)
    newer_sibling.updated_at = datetime(2025, 6, 1, tzinfo=timezone.utc)

    older_sibling = _make_author(id_=8)
    older_sibling.updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [base, newer_sibling, older_sibling]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result["id"] == 1
    assert result["updatedAt"] == newer_sibling.updated_at.isoformat()


@pytest.mark.asyncio
async def test_get_author_from_db_description_tie_at_equal_length_keeps_older_row():
    """Two candidates with descriptions of exactly equal trimmed length do not
    replace each other — the comparison is strict '>', so the older row's
    text is kept rather than the later-processed row's equally-long one."""
    older = _make_author(id_=2)
    older.description = "Nine char."

    newer = _make_author(id_=7)
    newer.description = "Ten chars!"
    assert len(older.description) == len(newer.description)

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [older, newer]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result["description"] == "Nine char."


@pytest.mark.asyncio
async def test_get_author_from_db_all_none_updated_at_returns_record_not_none():
    """Every candidate row missing updated_at must not crash the whole read —
    the record still comes back, just with updatedAt: None, rather than the
    author collapsing to None the way an uncaught exception in this function
    would. This guards the max() default against ever being dropped again."""
    older = _make_author(id_=2)
    older.updated_at = None

    newer = _make_author(id_=7)
    newer.updated_at = None

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [older, newer]
    session.execute = AsyncMock(return_value=result_mock)

    result = await get_author_from_db(session, "B000APF21M", "us")

    assert result is not None
    assert result["updatedAt"] is None


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


# ============================================================
# get_db_stats
# ============================================================


def _stats_count_result(count: int) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = count
    return result


def _cache_miss_result() -> MagicMock:
    """Mimics cache.get's underlying select finding no live row."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    return result


class _TransactionAbortingSession:
    """
    Models Postgres's real transaction-abort semantics for the cache-error
    path: once a statement raises inside a transaction, Postgres aborts that
    transaction and every later statement fails too — with
    InFailedSQLTransactionError — until rollback() actually runs. A plain
    AsyncMock side_effect queue doesn't model this: queued results come back
    regardless of whether the handler ever calls rollback(), which would let
    a test pass even if the rollback() call were deleted from product code.
    Measured live: an un-rolled-back cache-read error returns all zeros to
    the public stats endpoint, not the live counts.
    """

    def __init__(self, first_error: Exception, live_results: list):
        self._first_error = first_error
        self._live_results = list(live_results)
        self._first_call_made = False
        self._aborted = False
        self.rollback_called = False

    async def execute(self, *args, **kwargs):
        if not self._first_call_made:
            self._first_call_made = True
            self._aborted = True
            raise self._first_error
        if self._aborted:
            raise Exception(
                "current transaction is aborted, commands ignored until end of transaction block"
            )
        return self._live_results.pop(0)

    async def rollback(self):
        self.rollback_called = True
        self._aborted = False

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_get_db_stats_returns_counts_for_all_five_metrics():
    """Maps each of the five count queries to its own response key, in order."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _cache_miss_result(),      # cache read (miss)
        _stats_count_result(150),  # books
        _stats_count_result(42),   # authors
        _stats_count_result(85),   # narrators
        _stats_count_result(18),   # series
        _stats_count_result(7),    # booksWithChapters
        MagicMock(),                # cache write
    ])

    result = await get_db_stats(session)

    assert result == {
        "books": 150,
        "authors": 42,
        "narrators": 85,
        "series": 18,
        "booksWithChapters": 7,
    }


@pytest.mark.asyncio
async def test_get_db_stats_books_with_chapters_counts_tracks_table():
    """
    booksWithChapters must be a count of the tracks table — one row per book
    that actually has chapter data stored. A count of
    Book.chapters_checked_at IS NOT NULL is a different, larger population
    (it includes books that were checked and found to have no chapters, e.g.
    ISBN-keyed records that 404 and bundle ASINs that will never have
    chapters) and would overstate what Libex holds, so the underlying query
    must target the tracks table rather than filter the books table.
    """
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _cache_miss_result(),
        _stats_count_result(150),
        _stats_count_result(42),
        _stats_count_result(85),
        _stats_count_result(18),
        _stats_count_result(7),
        MagicMock(),
    ])

    await get_db_stats(session)

    # Index 5: cache-read miss (0), then books/authors/narrators/series (1-4),
    # then booksWithChapters (5), before the cache write.
    books_with_chapters_stmt = session.execute.call_args_list[5][0][0]
    froms = books_with_chapters_stmt.get_final_froms()
    assert [f.name for f in froms] == ["tracks"]
    assert "chapters_checked_at" not in str(books_with_chapters_stmt).lower()


@pytest.mark.asyncio
async def test_get_db_stats_fallback_on_exception_includes_books_with_chapters():
    """
    The literal fallback dict returned on DB error must carry every
    StatsResponse key. A key missing here degrades the all-zeros fallback
    into a response the caller can no longer treat as complete.
    """
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception("DB error"))

    result = await get_db_stats(session)

    assert result == {
        "books": 0,
        "authors": 0,
        "narrators": 0,
        "series": 0,
        "booksWithChapters": 0,
    }


@pytest.mark.asyncio
async def test_get_db_stats_returns_cached_value_without_querying_counts():
    """A fresh, key-complete cache hit skips all five count queries."""
    session = AsyncMock()
    cached = {
        "books": 150,
        "authors": 42,
        "narrators": 85,
        "series": 18,
        "booksWithChapters": 7,
    }
    hit = MagicMock()
    hit.scalar_one_or_none.return_value = MagicMock(value=cached)
    session.execute = AsyncMock(return_value=hit)

    result = await get_db_stats(session)

    assert result == cached
    assert session.execute.call_count == 1


@pytest.mark.asyncio
async def test_get_db_stats_stale_cache_shape_is_treated_as_miss():
    """
    A cached entry missing a key (e.g. written before a new stat existed)
    must not be served as-is — StatsResponse's per-field `= 0` default would
    silently render zero for the missing stat on a public badge. It's treated
    as a miss and the live counts are recomputed instead.
    """
    session = AsyncMock()
    stale_cached = {"books": 150, "authors": 42, "narrators": 85, "series": 18}
    hit = MagicMock()
    hit.scalar_one_or_none.return_value = MagicMock(value=stale_cached)

    session.execute = AsyncMock(side_effect=[
        hit,
        _stats_count_result(150),
        _stats_count_result(42),
        _stats_count_result(85),
        _stats_count_result(18),
        _stats_count_result(7),
        MagicMock(),
    ])

    result = await get_db_stats(session)

    assert result == {
        "books": 150,
        "authors": 42,
        "narrators": 85,
        "series": 18,
        "booksWithChapters": 7,
    }


@pytest.mark.asyncio
async def test_get_db_stats_cache_read_error_falls_back_to_live_query():
    """
    A cache read failure must not fail the request — falls back to live
    counts. Uses _TransactionAbortingSession rather than a plain AsyncMock
    side_effect queue: the queue would let this pass even without the
    handler's rollback() call, since queued results come back regardless of
    transaction state. Here, every execute() after the cache-read error
    keeps raising until rollback() actually runs, so the test can only pass
    if get_db_stats really rolls back before issuing the live queries.
    """
    session = _TransactionAbortingSession(
        first_error=Exception("cache unavailable"),
        live_results=[
            _stats_count_result(150),
            _stats_count_result(42),
            _stats_count_result(85),
            _stats_count_result(18),
            _stats_count_result(7),
            MagicMock(),
        ],
    )

    result = await get_db_stats(session)

    assert result == {
        "books": 150,
        "authors": 42,
        "narrators": 85,
        "series": 18,
        "booksWithChapters": 7,
    }
    assert session.rollback_called is True


@pytest.mark.asyncio
async def test_get_db_stats_cache_write_error_still_returns_live_counts():
    """A cache write failure must not fail the request — the live result still returns."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _cache_miss_result(),
        _stats_count_result(150),
        _stats_count_result(42),
        _stats_count_result(85),
        _stats_count_result(18),
        _stats_count_result(7),
        Exception("cache write unavailable"),
    ])

    result = await get_db_stats(session)

    assert result == {
        "books": 150,
        "authors": 42,
        "narrators": 85,
        "series": 18,
        "booksWithChapters": 7,
    }


# ============================================================
# _get_series_positions_batch — chunked IN query
# ============================================================
# The batch reads series positions for every book its caller holds, and
# get_author_books_from_db applies no limit, so the ASIN list is as long as
# a stored catalogue. asyncpg refuses a statement carrying more than 32767
# bind parameters, so the list is chunked at 5000 -- the same ceiling the
# seeder's _get_missing_asins uses, pinned in test_seeder_new_releases.py.
# What a mocked session can show is the statements that were sent, which is
# exactly where this failure lives: an oversized IN list raises before it
# returns a row.

def _positions_session():
    """A session whose every execute answers with no rows, so what a test
    reads back is the statements sent rather than the result built."""
    session = AsyncMock()

    def _execute(_stmt):
        result = MagicMock()
        result.fetchall.return_value = []
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _executed_asin_chunks(session):
    """The ASIN list bound into each statement, one entry per execute call."""
    return [
        call.args[0].whereclause.right.value
        for call in session.execute.call_args_list
    ]


@pytest.mark.asyncio
async def test_series_positions_batch_fires_no_query_for_no_books():
    """An empty book list short-circuits: an IN () query would cost a round
    trip to answer nothing, and every author with no stored books takes
    this path."""
    session = _positions_session()

    assert await _get_series_positions_batch(session, []) == {}
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_series_positions_batch_sends_one_statement_at_the_chunk_ceiling():
    """5000 ASINs is the chunk size, so they still travel as one statement.
    Chunking that started early would cost the batch its whole point --
    this function exists to replace one query per book."""
    session = _positions_session()

    await _get_series_positions_batch(session, [f"B{i:09d}" for i in range(5000)])

    assert [len(chunk) for chunk in _executed_asin_chunks(session)] == [5000]


@pytest.mark.asyncio
async def test_series_positions_batch_splits_the_list_one_past_the_ceiling():
    """One ASIN past the ceiling is two statements, split 5000 then 1. The
    boundary is the assertion: a huge list asserting merely 'more than one
    statement' passes against a chunk size of 2 and against one of 30000,
    and the second of those still exceeds the bind-parameter cap. The
    concatenation check proves the chunking splits the list rather than
    dropping part of it -- a lost ASIN reads downstream as a book with no
    series rather than as an error."""
    asins = [f"B{i:09d}" for i in range(5001)]
    session = _positions_session()

    await _get_series_positions_batch(session, asins)

    chunks = _executed_asin_chunks(session)
    assert [len(chunk) for chunk in chunks] == [5000, 1]
    assert [asin for chunk in chunks for asin in chunk] == asins