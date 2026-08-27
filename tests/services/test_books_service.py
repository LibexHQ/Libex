"""
Books service unit tests.
Tests normalization and helper functions without hitting Audible.
"""

# Standard library
import asyncio
import time
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.services.audible.books import (
    _best_image,
    _parse_authors,
    _parse_narrators,
    _parse_series,
    _parse_genres,
    _normalize_product,
    _filter_products,
    _parse_release_date,
)
from app.services.cache.manager import book_key
from app.core.response_headers import (
    REASON_HYDRATION_DEADLINE,
    REASON_HYDRATION_FAILED,
    REASON_HYDRATION_NOT_FOUND,
    ResponseFacts,
    SOURCE_AUDIBLE,
    SOURCE_CACHE,
    SOURCE_DB,
)


# ============================================================
# IMAGE TESTS
# ============================================================

def test_best_image_returns_highest_resolution():
    """Returns URL for highest resolution image."""
    images = {"500": "http://example.com/500.jpg", "2400": "http://example.com/2400.jpg"}
    result = _best_image(images)
    assert result == "http://example.com/2400.jpg"


def test_best_image_strips_size_suffix():
    images = {"500": "http://example.com/image._SX500_.jpg"}
    result = _best_image(images)
    assert result is not None
    assert "._" not in result


def test_best_image_returns_none_for_empty():
    """Returns None for empty image dict."""
    assert _best_image({}) is None


def test_best_image_returns_none_for_none():
    """Returns None for None input."""
    assert _best_image(None) is None


# ============================================================
# RELEASE DATE TESTS
# ============================================================

def test_parse_release_date_converts_to_iso():
    """Converts Audible date string to ISO 8601 format matching AudiMeta .toISO()."""
    result = _parse_release_date("2021-03-02")
    assert result == "2021-03-02T00:00:00+00:00"


def test_parse_release_date_returns_none_for_none():
    """Returns None for None input."""
    assert _parse_release_date(None) is None


def test_parse_release_date_returns_none_for_empty():
    """Returns None for empty string."""
    assert _parse_release_date("") is None


def test_parse_release_date_includes_timezone():
    """Converted date includes UTC timezone offset."""
    result = _parse_release_date("2021-03-02")
    assert result is not None
    assert "+00:00" in result


def test_parse_release_date_fallback_on_bad_format():
    """Returns raw string if format is unrecognized."""
    result = _parse_release_date("not-a-date")
    assert result == "not-a-date"


# ============================================================
# AUTHOR PARSING TESTS
# ============================================================

def test_parse_authors_returns_list():
    """Returns list of author dicts."""
    product = {"authors": [{"name": "Frank Herbert", "asin": "B000APF21M"}]}
    result = _parse_authors(product, "us")
    assert isinstance(result, list)
    assert len(result) == 1


def test_parse_authors_includes_name():
    """Author dict includes name."""
    product = {"authors": [{"name": "Frank Herbert", "asin": "B000APF21M"}]}
    result = _parse_authors(product, "us")
    assert result[0]["name"] == "Frank Herbert"


def test_parse_authors_includes_region():
    """Author dict includes region."""
    product = {"authors": [{"name": "Frank Herbert", "asin": "B000APF21M"}]}
    result = _parse_authors(product, "uk")
    assert result[0]["region"] == "uk"


def test_parse_authors_includes_regions_list():
    """Author dict includes regions list matching AudiMeta MinimalAuthorDto."""
    product = {"authors": [{"name": "Frank Herbert", "asin": "B000APF21M"}]}
    result = _parse_authors(product, "us")
    assert result[0]["regions"] == ["us"]


def test_parse_authors_strips_tabs():
    """Author name has tabs stripped."""
    product = {"authors": [{"name": "\tFrank Herbert\t", "asin": "B000APF21M"}]}
    result = _parse_authors(product, "us")
    assert result[0]["name"] == "Frank Herbert"


def test_parse_authors_rejects_long_asin():
    """Author ASIN longer than 12 chars is set to None."""
    product = {"authors": [{"name": "Author", "asin": "TOOLONGASIN123"}]}
    result = _parse_authors(product, "us")
    assert result[0]["asin"] is None


def test_parse_authors_returns_empty_for_no_authors():
    """Returns empty list when no authors."""
    assert _parse_authors({}, "us") == []


def test_parse_authors_includes_id_field():
    """Author dict includes id field matching AudiMeta MinimalAuthorDto."""
    product = {"authors": [{"name": "Frank Herbert", "asin": "B000APF21M"}]}
    result = _parse_authors(product, "us")
    assert "id" in result[0]


# ============================================================
# NARRATOR TESTS
# ============================================================

def test_parse_narrators_returns_dicts():
    """Returns list of narrator dicts with name and updatedAt."""
    product = {"narrators": [{"name": "Scott Brick"}, {"name": "Kate Reading"}]}
    result = _parse_narrators(product)
    assert len(result) == 2
    assert result[0]["name"] == "Scott Brick"
    assert "updatedAt" in result[0]


def test_parse_narrators_returns_empty_for_none():
    """Returns empty list when no narrators."""
    assert _parse_narrators({}) == []


# ============================================================
# SERIES TESTS
# ============================================================

def test_parse_series_extracts_series():
    """Extracts series from relationships."""
    product = {
        "relationships": [
            {"relationship_type": "series", "asin": "B000SERIES1", "title": "Dune", "sequence": "1"}
        ]
    }
    result = _parse_series(product, "us")
    assert len(result) == 1
    assert result[0]["asin"] == "B000SERIES1"


def test_parse_series_uses_name_field():
    """Series dict uses name field matching AudiMeta MinimalSeriesDto."""
    product = {
        "relationships": [
            {"relationship_type": "series", "asin": "B000SERIES1", "title": "Dune", "sequence": "1"}
        ]
    }
    result = _parse_series(product, "us")
    assert "name" in result[0]
    assert result[0]["name"] == "Dune"


def test_parse_series_ignores_non_series():
    """Ignores relationships that are not series."""
    product = {
        "relationships": [
            {"relationship_type": "episode", "asin": "B000EP1", "title": "Episode 1"}
        ]
    }
    result = _parse_series(product, "us")
    assert result == []


def test_parse_series_returns_empty_for_no_relationships():
    """Returns empty list when no relationships."""
    assert _parse_series({}, "us") == []


# ============================================================
# GENRE TESTS
# ============================================================

def test_parse_genres_extracts_dicts():
    """Extracts genre dicts from category ladders."""
    product = {
        "category_ladders": [
            {"ladder": [{"name": "Science Fiction"}, {"name": "Space Opera"}]}
        ]
    }
    result = _parse_genres(product)
    names = [g["name"] for g in result]
    assert "Science Fiction" in names


def test_parse_genres_includes_type():
    """Genre dict includes type field."""
    product = {
        "category_ladders": [
            {"ladder": [{"name": "Fiction"}]}
        ]
    }
    result = _parse_genres(product)
    assert "type" in result[0]
    assert result[0]["type"] == "Genres"


def test_parse_genres_includes_better_type():
    """Genre dict includes betterType field matching AudiMeta GenreDto."""
    product = {
        "category_ladders": [
            {"ladder": [{"name": "Fiction"}]}
        ]
    }
    result = _parse_genres(product)
    assert "betterType" in result[0]


def test_parse_genres_tags_for_nested():
    """Second rung in ladder gets Tags type."""
    product = {
        "category_ladders": [
            {"ladder": [{"name": "Fiction"}, {"name": "Thriller"}]}
        ]
    }
    result = _parse_genres(product)
    assert result[0]["type"] == "Genres"
    assert result[1]["type"] == "Tags"


def test_parse_genres_deduplicates():
    """Does not return duplicate genre names."""
    product = {
        "category_ladders": [
            {"ladder": [{"name": "Fiction"}]},
            {"ladder": [{"name": "Fiction"}]},
        ]
    }
    result = _parse_genres(product)
    names = [g["name"] for g in result]
    assert names.count("Fiction") == 1


# ============================================================
# FILTER TESTS
# ============================================================

def test_filter_products_removes_unreleased():
    """Removes products with unreleased placeholder date."""
    products = [
        {"title": "Real Book", "publication_datetime": "2021-01-01T00:00:00Z"},
        {"title": "Future Book", "publication_datetime": "2200-01-01T00:00:00Z"},
    ]
    result = _filter_products(products)
    assert len(result) == 1
    assert result[0]["title"] == "Real Book"


def test_filter_products_removes_untitled():
    """Removes products without a title."""
    products = [
        {"title": "Real Book", "publication_datetime": "2021-01-01T00:00:00Z"},
        {"title": None, "publication_datetime": "2021-01-01T00:00:00Z"},
    ]
    result = _filter_products(products)
    assert len(result) == 1


def test_filter_products_returns_empty_for_empty_input():
    """Returns empty list for empty input."""
    assert _filter_products([]) == []


def test_filter_products_drops_the_hollow_not_found_stub_before_it_reaches_normalization():
    """The stub Audible returns for an ASIN that doesn't resolve in the
    queried region carries no title and no `plans` key at all -- this is the
    only real shape that can trip _parse_plans' silence branch, and it never
    reaches _normalize_product because it has no title either."""
    hollow_stub = {"asin": "B0NOTFOUND1", "product_state": "NOT_AVAILABLE_FOR_PURCHASE"}
    assert _filter_products([hollow_stub]) == []


# ============================================================
# NORMALIZE TESTS
# ============================================================

def test_normalize_product_returns_required_fields():
    """Normalized product contains all required fields matching AudiMeta BookDto."""
    product = {
        "asin": "B08G9PRS1K",
        "title": "Dune",
        "authors": [{"name": "Frank Herbert", "asin": "B000APF21M"}],
        "narrators": [{"name": "Scott Brick"}],
        "relationships": [],
        "product_images": {"500": "http://example.com/500.jpg"},
        "category_ladders": [],
        "rating": {"overall_distribution": {"average_rating": 4.8}},
    }
    result = _normalize_product(product, "us")
    required = [
        "asin", "title", "authors", "narrators", "series", "region",
        "imageUrl", "lengthMinutes", "releaseDate", "hasPdf", "whisperSync",
        "regions", "link", "isListenable", "isBuyable",
    ]
    for field in required:
        assert field in result, f"Missing field: {field}"


def test_normalize_product_sets_region():
    """Normalized product has correct region."""
    product = {
        "asin": "B08G9PRS1K", "title": "Dune", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [], "rating": {}
    }
    result = _normalize_product(product, "uk")
    assert result["region"] == "uk"


def test_normalize_product_sets_regions_list():
    """Normalized product includes regions list."""
    product = {
        "asin": "B08G9PRS1K", "title": "Dune", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [], "rating": {}
    }
    result = _normalize_product(product, "us")
    assert result["regions"] == ["us"]


def test_normalize_product_sets_link():
    """Normalized product includes Audible link."""
    product = {
        "asin": "B08G9PRS1K", "title": "Dune", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [], "rating": {}
    }
    result = _normalize_product(product, "us")
    assert "audible.com" in result["link"]
    assert "B08G9PRS1K" in result["link"]


def test_normalize_product_release_date_is_iso():
    """Normalized product releaseDate is in ISO 8601 format."""
    product = {
        "asin": "B08G9PRS1K", "title": "Dune", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [], "rating": {},
        "release_date": "2021-03-02",
    }
    result = _normalize_product(product, "us")
    assert result["releaseDate"] == "2021-03-02T00:00:00+00:00"


def test_normalize_product_episode_fields_none_for_non_podcast():
    """episodeNumber and episodeType are None for non-podcast content."""
    product = {
        "asin": "B08G9PRS1K", "title": "Dune", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [], "rating": {},
        "content_type": "Book",
        "episode_number": 5,
        "episode_type": "full",
    }
    result = _normalize_product(product, "us")
    assert result["episodeNumber"] is None
    assert result["episodeType"] is None


def test_normalize_product_episode_fields_set_for_podcast():
    """episodeNumber and episodeType are set for podcast content."""
    product = {
        "asin": "B08G9PRS1K", "title": "Podcast Ep", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [], "rating": {},
        "content_type": "Podcast",
        "episode_number": 5,
        "episode_type": "full",
    }
    result = _normalize_product(product, "us")
    assert result["episodeNumber"] == "5"
    assert result["episodeType"] == "full"


# ============================================================
# DB FALLBACK TESTS
# ============================================================

@pytest.mark.asyncio
async def test_get_books_falls_back_to_db_when_audible_fails():
    """Falls back to DB when Audible is unavailable."""
    from app.services.audible.books import get_books_by_asins

    mock_session = AsyncMock()

    with patch("app.services.audible.books.audible_get", side_effect=Exception("Audible down")), \
         patch("app.services.audible.books.get_books_from_db", new_callable=AsyncMock, return_value=[{"asin": "B08G9PRS1K", "title": "Dune"}]), \
         patch("app.services.audible.books.cache.get", return_value=None):
        result = await get_books_by_asins(["B08G9PRS1K"], "us", mock_session)
        assert len(result) == 1
        assert result[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_books_falls_back_to_cache_when_db_empty():
    """Falls back to cache when Audible is down and DB has no results.

    The outage fallback reads the cache in one batched lookup keyed by
    cache key, so the stand-in returns {book_key: value} rather than a
    bare value -- a miss is the key's absence from that dict."""
    from app.services.audible.books import get_books_by_asins

    mock_session = AsyncMock()
    cached_book = {"asin": "B08G9PRS1K", "title": "Dune (cached)"}

    with patch("app.services.audible.books.audible_get", side_effect=Exception("Audible down")), \
         patch("app.services.audible.books.get_books_from_db", new_callable=AsyncMock, return_value=[]), \
         patch("app.services.audible.books.cache.get_many",
               return_value={book_key("B08G9PRS1K", "us"): cached_book}):
        result = await get_books_by_asins(["B08G9PRS1K"], "us", mock_session)
        assert len(result) == 1
        assert result[0]["title"] == "Dune (cached)"


@pytest.mark.asyncio
async def test_get_books_outage_cache_fallback_reads_all_misses_in_one_lookup():
    """The outage fallback reads the cache for every miss in ONE batched
    lookup, and keeps the hits in requested order while dropping the ASINs
    that were not cached.

    The single-ASIN fallback test above cannot see the batching -- one ASIN
    is one lookup either way. This one is the guard for it, and the path is
    worth guarding precisely because of when it runs: Audible is down and
    the DB has nothing, so a per-ASIN loop would fire one query per ASIN
    against a database already carrying the whole outage's traffic. The
    bulk book route's 1000-ASIN cap is not the ceiling on that count: this
    fallback runs on any Audible failure whatever use_cache was, and the
    author routes reach it with a whole unbounded catalogue -- thousands
    of ASINs."""
    from app.services.audible.books import get_books_by_asins

    mock_session = AsyncMock()
    asins = ["B08G9PRS1K", "B0MISSING1", "B0CACHED03"]
    hits = {
        book_key("B08G9PRS1K", "us"): {"asin": "B08G9PRS1K", "title": "Dune (cached)"},
        book_key("B0CACHED03", "us"): {"asin": "B0CACHED03", "title": "Elantris (cached)"},
    }

    async def _get_many(session, keys):
        return {key: hits[key] for key in keys if key in hits}

    with patch("app.services.audible.books.audible_get", side_effect=Exception("Audible down")), \
         patch("app.services.audible.books.get_books_from_db", new_callable=AsyncMock, return_value=[]), \
         patch("app.services.audible.books.cache.get_many",
               new=AsyncMock(side_effect=_get_many)) as mock_cache_get_many:
        result = await get_books_by_asins(asins, "us", mock_session)

    assert mock_cache_get_many.await_count == 1
    assert mock_cache_get_many.await_args.args[1] == [book_key(a, "us") for a in asins]
    assert [b["asin"] for b in result] == ["B08G9PRS1K", "B0CACHED03"]
    assert [b["title"] for b in result] == ["Dune (cached)", "Elantris (cached)"]


@pytest.mark.asyncio
async def test_get_books_writes_to_db_on_success():
    """Writes book data to DB after successful Audible fetch."""
    from app.services.audible.books import get_books_by_asins

    mock_session = AsyncMock()
    mock_product = {
        "asin": "B08G9PRS1K", "title": "Dune", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [],
        "rating": {"overall_distribution": {"average_rating": 4.8}},
        "publication_datetime": "2021-01-01T00:00:00Z",
    }

    with patch("app.services.audible.books.audible_get", return_value={"product": mock_product}), \
         patch("app.services.audible.books.persist_books_background") as mock_persist, \
         patch("app.services.audible.books.cache.get", return_value=None):
        await get_books_by_asins(["B08G9PRS1K"], "us", mock_session)
        mock_persist.assert_called_once()


# ============================================================
# CACHE-FIRST READ TESTS (use_cache=True, Audible healthy)
# ============================================================
# The DB-fallback tests above only exercise the except block's cache
# read, when Audible itself is down -- they say nothing about the
# use_cache=True short-circuit branches (the ones the ASIN author routes
# now reach for the first time on this branch, see router.py's use_cache
# wiring). These pin that a cache hit short-circuits Audible entirely
# (audible_get is never called) and that the value handed back is not
# merely "a dict" but the SAME normalized shape a live fetch itself
# would have produced -- the cached value used here is captured from an
# actual live fetch through the real normalization path, then round-
# tripped through the cache-hit branch and compared for exact equality,
# rather than asserting on a hand-built stand-in dict that could drift
# from what normalization genuinely emits.

@pytest.mark.asyncio
async def test_get_books_by_asins_single_asin_cache_hit_matches_live_fetch_shape_and_skips_audible():
    """use_cache=True with exactly one ASIN takes the single-ASIN cache
    branch (book.py's own first use_cache check). A hit there must return
    precisely what a live fetch of the same ASIN would have normalized to,
    and must never touch Audible at all -- asserted on the audible_get mock
    directly (not on a side_effect exception) so a bug that reaches Audible
    but still stumbles onto the right answer via the unrelated except-block
    DB/cache fallback (which also reads the cache) cannot pass this by
    accident; that fallback is starved here by leaving get_books_from_db
    unmocked against a bare AsyncMock session, which raises rather than
    quietly returning an empty list the way it would with a real DB."""
    from app.services.audible.books import get_books_by_asins

    asin = "B08G9PRS1K"

    with patch("app.services.audible.books.audible_get", return_value={"product": _hydration_product(asin)}), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get", return_value=None):
        live_result = await get_books_by_asins([asin], "us", AsyncMock())
    assert len(live_result) == 1
    live_dto = live_result[0]

    with patch("app.services.audible.books.audible_get", new_callable=AsyncMock) as mock_audible_get, \
         patch("app.services.audible.books.cache.get", new=AsyncMock(return_value=live_dto)) as mock_cache_get:
        cached_result = await get_books_by_asins([asin], "us", AsyncMock(), use_cache=True)

    mock_audible_get.assert_not_called()
    mock_cache_get.assert_awaited_once()
    assert cached_result == [live_dto]


@pytest.mark.asyncio
async def test_get_books_by_asins_all_cache_hits_matches_live_fetch_shape_and_skips_audible():
    """use_cache=True with more than one ASIN takes the batch cache branch
    -- when every requested ASIN is a hit, fetch_asins ends up empty and
    the function returns straight from the cache list without ever
    reaching the chunk fan-out / Audible at all. Same live-fetch-shape
    parity check and same not-called (rather than side_effect-raises)
    discriminator as the single-ASIN sibling above, across two ASINs.

    The await_count == 1 assertion below is load-bearing, not incidental
    bookkeeping: the branch looks up the whole ASIN list in ONE batched
    cache call, and that count is the only thing in the suite that fails
    if it reverts to a per-ASIN lookup. Two ASINs would then be two awaits
    and 1000 ASINs 1000 of them -- the N+1 this branch exists to remove,
    restored silently with every other assertion here still green. The
    count is the behaviour under test."""
    from app.services.audible.books import get_books_by_asins

    asins = ["B08G9PRS1K", "B0CACHED02"]

    async def _get(region, path, params):
        requested = params["asins"].split(",")
        return {"products": [_hydration_product(a) for a in requested]}

    with patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get", return_value=None):
        live_result = await get_books_by_asins(asins, "us", AsyncMock())
    assert len(live_result) == 2
    live_by_asin = {b["asin"]: b for b in live_result}

    # The batch getter returns only the keys that hit, keyed by cache key,
    # so the stand-in is built the same way the real one answers.
    hits = {book_key(asin, "us"): dto for asin, dto in live_by_asin.items()}

    async def _cache_get_many(session, keys):
        return {key: hits[key] for key in keys if key in hits}

    with patch("app.services.audible.books.audible_get", new_callable=AsyncMock) as mock_audible_get, \
         patch("app.services.audible.books.cache.get_many",
               new=AsyncMock(side_effect=_cache_get_many)) as mock_cache_get_many:
        cached_result = await get_books_by_asins(asins, "us", AsyncMock(), use_cache=True)

    mock_audible_get.assert_not_called()
    assert mock_cache_get_many.await_count == 1
    assert mock_cache_get_many.await_args.args[1] == [book_key(a, "us") for a in asins]
    assert {b["asin"] for b in cached_result} == set(asins)
    assert cached_result == [live_by_asin[a] for a in asins]


# ============================================================
# PARALLEL HYDRATION TESTS (get_books_by_asins chunk fan-out)
# ============================================================

def _hydration_product(asin):
    """Minimal product shape that survives _normalize_product/_filter_products
    unscathed -- a real title and a non-placeholder publication_datetime."""
    return {
        "asin": asin, "title": f"Book {asin}", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [],
        "rating": {}, "publication_datetime": "2021-01-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_get_books_by_asins_preserves_chunk_order_regardless_of_completion_order():
    """asyncio.gather preserves input order regardless of which chunk's
    request finishes first -- chunk 1 (50 ASINs) is made to resolve slower
    than chunk 2 (5 ASINs), but the returned book list must still be in the
    original requested-ASIN order, not completion order."""
    from app.services.audible.books import get_books_by_asins

    mock_session = AsyncMock()
    chunk1_asins = [f"B0CHUNK1{i:02d}" for i in range(50)]
    chunk2_asins = [f"B0CHNK2{i:03d}" for i in range(5)]
    all_asins = chunk1_asins + chunk2_asins

    async def _get(region, path, params):
        asins = params["asins"].split(",")
        if asins == chunk1_asins:
            await asyncio.sleep(0.03)  # slower chunk, still must end up first
        return {"products": [_hydration_product(a) for a in asins]}

    with patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get", return_value=None):
        result = await get_books_by_asins(all_asins, "us", mock_session)

    assert [b["asin"] for b in result] == all_asins


@pytest.mark.asyncio
async def test_get_books_by_asins_not_found_chunk_does_not_discard_other_chunks():
    """A 404 on one chunk marks only that chunk's ASINs not-found and must
    not discard results already fetched from other chunks -- the old
    sequential code discarded every already-fetched chunk on any failure."""
    from app.services.audible.books import get_books_by_asins
    from app.core.exceptions import NotFoundException

    mock_session = AsyncMock()
    good_asins = [f"B0GOOD{i:03d}" for i in range(50)]
    missing_asin = "B0MISSING1"  # 51st ASIN -> its own single-ASIN chunk
    all_asins = good_asins + [missing_asin]

    async def _get(region, path, params):
        if "asins" in params:
            asins = params["asins"].split(",")
            return {"products": [_hydration_product(a) for a in asins]}
        raise NotFoundException()  # the single-ASIN chunk's 404

    with patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.get_books_from_db", new_callable=AsyncMock) as mock_backstop, \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get", return_value=None):
        result = await get_books_by_asins(all_asins, "us", mock_session)

    assert len(result) == 50
    assert {b["asin"] for b in result} == set(good_asins)
    # A confirmed 404 must never fall through to the DB backstop -- that
    # backstop is scoped to transient_failed_asins alone (see
    # get_books_by_asins' own comment at the DB-backstop read): a 404 is a
    # confirmed absence, not a retry signal, and papering over it with
    # whatever stale data the DB happens to hold would be exactly the kind
    # of silent, wrong content the data contract forbids. Asserting the mock
    # was never called (rather than just checking missing_asin isn't in the
    # result) is deliberate: with get_books_from_db left unmocked, a real
    # call here still returns [] against a bare AsyncMock session, so the
    # weaker assertion would pass whether or not the backstop had actually
    # fired for this confirmed-404 ASIN -- exactly the kind of gap that let
    # this branch's own not_found/transient split ship with nothing to
    # prove it holds.
    mock_backstop.assert_not_called()


@pytest.mark.asyncio
async def test_get_books_by_asins_not_found_and_transient_together_backstop_scoped_to_transient_only():
    """Both buckets populated in the same request -- a 404'd single-ASIN
    chunk and a separately-failing 50-ASIN transient chunk -- alongside a
    third, successful 50-ASIN batch (forcing all_products non-empty so the
    run actually reaches the DB-backstop branch at all, unlike a
    not-found-only call, which returns early before ever reaching it --
    see the sibling test above, and note that early return is exactly the
    gap that let the mutated backstop condition
    `transient_failed_asins or not_found_asins` pass every pre-existing
    test in this file, including that sibling, untouched). The DB
    genuinely has rows for every ASIN in both failed buckets; only the
    transient ones may be resurrected -- the 404'd one, a confirmed
    absence, must never appear even though the same DB call would have
    handed it back if the scoping were wrong."""
    from app.services.audible.books import get_books_by_asins
    from app.core.exceptions import NotFoundException

    mock_session = AsyncMock()
    good_asins = [f"B0GOOD{i:03d}" for i in range(50)]
    bad_asins = [f"B0BAD{i:03d}" for i in range(50)]  # whole-chunk transient failure
    missing_asin = "B0MISSING1"  # its own single-ASIN chunk -> 404
    all_asins = good_asins + bad_asins + [missing_asin]
    stale_missing_book = {"asin": missing_asin, "title": "Stale, no longer on Audible"}
    stale_bad_books = [{"asin": a, "title": "From DB backstop"} for a in bad_asins]

    async def _get(region, path, params):
        if "asins" in params:
            asins = params["asins"].split(",")
            if asins == good_asins:
                return {"products": [_hydration_product(a) for a in asins]}
            raise RuntimeError("Audible 500")  # the bad_asins batch chunk
        raise NotFoundException()  # missing_asin's own single-ASIN chunk

    async def _db_backstop(session, asins):
        assert missing_asin not in asins, "not_found_asins leaked into the DB-backstop call"
        return [b for b in stale_bad_books + [stale_missing_book] if b["asin"] in asins]

    with patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.get_books_from_db", new=AsyncMock(side_effect=_db_backstop)) as mock_backstop, \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get", return_value=None):
        result = await get_books_by_asins(all_asins, "us", mock_session)

    mock_backstop.assert_awaited_once_with(mock_session, bad_asins)
    assert set(bad_asins) <= {b["asin"] for b in result}
    assert stale_missing_book not in result
    assert missing_asin not in {b["asin"] for b in result}


@pytest.mark.asyncio
async def test_get_books_by_asins_transient_chunk_failure_is_skipped_not_fatal():
    """A non-404 exception on one chunk is logged and skipped from the
    Audible-hydration list; the other chunk's results still come back rather
    than the whole request failing. The DB backstop scoped to exactly the
    transiently-failed ASINs is unioned into the return value rather than
    silently dropped -- the defect this test previously had
    LOCKED IN: without mocking get_books_from_db, the call went through to
    the real function against a bare AsyncMock session and gave no signal
    either way on whether the backstop fired."""
    from app.services.audible.books import get_books_by_asins

    mock_session = AsyncMock()
    good_asins = [f"B0GOOD{i:03d}" for i in range(50)]
    bad_asin = "B0BADCHUNK"  # single-ASIN chunk that fails transiently
    all_asins = good_asins + [bad_asin]
    db_backstop_book = {"asin": bad_asin, "title": "From DB backstop"}

    async def _get(region, path, params):
        if "asins" in params:
            asins = params["asins"].split(",")
            return {"products": [_hydration_product(a) for a in asins]}
        raise RuntimeError("Audible 500")

    with patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.get_books_from_db", new_callable=AsyncMock, return_value=[db_backstop_book]) as mock_backstop, \
         patch("app.services.audible.books.cache.get", return_value=None):
        result = await get_books_by_asins(all_asins, "us", mock_session)

    mock_backstop.assert_awaited_once_with(mock_session, [bad_asin])
    assert {b["asin"] for b in result} == set(good_asins) | {bad_asin}
    assert db_backstop_book in result


@pytest.mark.asyncio
async def test_get_books_by_asins_reraises_when_only_transient_failures_and_nothing_came_back():
    """When every chunk fails transiently (no 404s) and nothing at all came
    back, the first transient exception re-raises to preserve the
    DB-then-cache fallback in the except block below -- this must not
    silently collapse to an empty-list return instead of falling back."""
    from app.services.audible.books import get_books_by_asins

    mock_session = AsyncMock()
    asins = [f"B0BAD{i:03d}" for i in range(60)]  # 2 chunks, both fail transiently
    db_book = {"asin": asins[0], "title": "From DB"}

    with patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=RuntimeError("Audible 500"))), \
         patch("app.services.audible.books.get_books_from_db", new=AsyncMock(return_value=[db_book])), \
         patch("app.services.audible.books.cache.get", return_value=None):
        result = await get_books_by_asins(asins, "us", mock_session)

    assert result == [db_book]
    mock_session.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_get_books_by_asins_partial_shortfall_warning_fires_on_not_found_alone():
    """The 'Partial hydration shortfall' warning fires when the not-found
    bucket alone is non-empty, with zero transient failures."""
    from app.services.audible.books import get_books_by_asins
    from app.core.exceptions import NotFoundException

    mock_session = AsyncMock()
    good_asins = [f"B0GOOD{i:03d}" for i in range(50)]
    missing_asin = "B0MISSING1"
    all_asins = good_asins + [missing_asin]

    async def _get(region, path, params):
        if "asins" in params:
            asins = params["asins"].split(",")
            return {"products": [_hydration_product(a) for a in asins]}
        raise NotFoundException()

    with patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get", return_value=None), \
         patch("app.services.audible.books.logger") as mock_logger:
        await get_books_by_asins(all_asins, "us", mock_session)

    shortfall_calls = [
        c for c in mock_logger.warning.call_args_list if c.args[0] == "Partial hydration shortfall"
    ]
    assert len(shortfall_calls) == 1
    extra = shortfall_calls[0].kwargs["extra"]
    assert extra["not_found_asins"] == 1
    assert extra["failed_asins"] == 0


@pytest.mark.asyncio
async def test_get_books_by_asins_partial_shortfall_warning_fires_on_transient_alone():
    """The 'Partial hydration shortfall' warning fires when the transient-
    failure bucket alone is non-empty, with zero not-found ASINs, and the
    DB backstop scoped to that same transient-failure bucket is still
    unioned into the result. Same unmocked-get_books_from_db gap as its
    sibling above: without the mock this gave no real signal on the fix."""
    from app.services.audible.books import get_books_by_asins

    mock_session = AsyncMock()
    good_asins = [f"B0GOOD{i:03d}" for i in range(50)]
    bad_asin = "B0BADCHUNK"
    all_asins = good_asins + [bad_asin]
    db_backstop_book = {"asin": bad_asin, "title": "From DB backstop"}

    async def _get(region, path, params):
        if "asins" in params:
            asins = params["asins"].split(",")
            return {"products": [_hydration_product(a) for a in asins]}
        raise RuntimeError("Audible 500")

    with patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.get_books_from_db", new_callable=AsyncMock, return_value=[db_backstop_book]), \
         patch("app.services.audible.books.cache.get", return_value=None), \
         patch("app.services.audible.books.logger") as mock_logger:
        result = await get_books_by_asins(all_asins, "us", mock_session)

    shortfall_calls = [
        c for c in mock_logger.warning.call_args_list if c.args[0] == "Partial hydration shortfall"
    ]
    assert len(shortfall_calls) == 1
    extra = shortfall_calls[0].kwargs["extra"]
    assert extra["not_found_asins"] == 0
    assert extra["failed_asins"] == 1
    assert db_backstop_book in result


# ============================================================
# RESPONSE FACTS -- SOURCE ATTRIBUTION (get_books_by_asins)
# ============================================================
# get_books_by_asins is the one function in the module where a single
# response can genuinely mix cache, fresh Audible, and DB-backstop elements
# (see its own docstring), so it is the one call site worth pinning per-key,
# not just per-call. Every test below asserts the invariant
# sum(facts.source_counts.values()) == len(facts.source_by_key) alongside
# the per-key values themselves -- that invariant only holds while every
# call site uses record_source_keys (never the aggregate-only record_source)
# for this function's own returns, so it would catch a call site silently
# reverting to the aggregate form as surely as a wrong count would.
#
# Two of these need more than the obvious mock shape to actually exercise
# what they claim to:
#
# - The two outage-fallback tests below reach the except block with a
#   non-empty pre-fetch cached_results segment. audible_get failing alone
#   does not get there: the except block's own re-raise inside the try is
#   conditioned on `not cached_results`, so with cached_results already
#   populated the run falls through to the in-try DB backstop read instead
#   and never reaches the outage branch at all. What reaches it here is the
#   in-try DB-backstop read itself failing -- get_books_from_db mocked to
#   raise on its first call, then answer normally on its second (the outage
#   branch's own, separate call to the same function).
# - The combined-cache-segments test additionally needs a call-count-aware
#   cache.get_many stand-in. A single fixed return value answers both the
#   pre-fetch batch lookup and the outage fallback's own lookup identically;
#   if that fixed value already included the fallback-only ASIN, the
#   pre-fetch lookup would swallow it, empty fetch_asins, and take the
#   all-cache-hits early return before Audible or the outage path is ever
#   reached. The two real call sites are answered differently here so each
#   only sees the hit it is actually meant to see.

@pytest.mark.asyncio
async def test_get_books_by_asins_facts_records_single_asin_cache_hit():
    """use_cache=True with exactly one ASIN takes the single-ASIN cache
    branch (the first use_cache check in the function) and must credit
    facts with exactly that one ASIN's source, keyed to it -- not merely an
    aggregate count."""
    from app.services.audible.books import get_books_by_asins

    asin = "B08G9PRS1K"
    cached_book = {"asin": asin, "title": "Dune (cached)"}
    facts = ResponseFacts()

    with patch("app.services.audible.books.cache.get", new=AsyncMock(return_value=cached_book)):
        result = await get_books_by_asins(
            [asin], "us", AsyncMock(), use_cache=True, facts=facts
        )

    assert result == [cached_book]
    assert facts.source_counts == {"audible": 0, "cache": 1, "db": 0}
    assert facts.source_by_key == {asin: SOURCE_CACHE}
    assert facts.is_complete
    assert sum(facts.source_counts.values()) == len(facts.source_by_key)


@pytest.mark.asyncio
async def test_get_books_by_asins_facts_records_all_cache_hits_early_return():
    """use_cache=True with more than one ASIN, every one a hit, takes the
    batch cache branch's early return (fetch_asins ends up empty) and must
    credit facts with every ASIN keyed to cache, not just the aggregate
    count of two."""
    from app.services.audible.books import get_books_by_asins

    asins = ["B08G9PRS1K", "B0CACHED02"]
    hits = {book_key(a, "us"): {"asin": a, "title": f"{a} cached"} for a in asins}
    facts = ResponseFacts()

    async def _get_many(session, keys):
        return {key: hits[key] for key in keys if key in hits}

    with patch("app.services.audible.books.cache.get_many", new=AsyncMock(side_effect=_get_many)):
        result = await get_books_by_asins(
            asins, "us", AsyncMock(), use_cache=True, facts=facts
        )

    assert {b["asin"] for b in result} == set(asins)
    assert facts.source_counts == {"audible": 0, "cache": 2, "db": 0}
    assert facts.source_by_key == {a: SOURCE_CACHE for a in asins}
    assert facts.is_complete
    assert sum(facts.source_counts.values()) == len(facts.source_by_key)


@pytest.mark.asyncio
async def test_get_books_by_asins_facts_records_mixed_cache_and_audible_sources():
    """use_cache=True with one cache hit and one miss: the miss is fetched
    live from Audible in the same call, so the returned list -- and facts --
    genuinely mix two sources in one response, each attributed to the right
    key rather than the pair collapsing to "mixed" with no per-element
    truth behind it."""
    from app.services.audible.books import get_books_by_asins

    cached_asin = "B0CACHED01"
    fetched_asin = "B0FETCHED1"
    cached_book = {"asin": cached_asin, "title": "Cached"}
    facts = ResponseFacts()

    async def _get_many(session, keys):
        return {book_key(cached_asin, "us"): cached_book}

    async def _get(region, path, params):
        return {"product": _hydration_product(fetched_asin)}

    with patch("app.services.audible.books.cache.get_many", new=AsyncMock(side_effect=_get_many)), \
         patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.persist_books_background"):
        result = await get_books_by_asins(
            [cached_asin, fetched_asin], "us", AsyncMock(), use_cache=True, facts=facts
        )

    assert {b["asin"] for b in result} == {cached_asin, fetched_asin}
    assert facts.source_counts == {"audible": 1, "cache": 1, "db": 0}
    assert facts.source_by_key == {cached_asin: SOURCE_CACHE, fetched_asin: SOURCE_AUDIBLE}
    assert facts.is_complete
    assert sum(facts.source_counts.values()) == len(facts.source_by_key)


@pytest.mark.asyncio
async def test_get_books_by_asins_facts_records_db_backstop_after_transient_failure():
    """One chunk succeeds live, a separate chunk fails transiently and the
    in-try DB backstop only partially recovers it -- facts must credit the
    live chunk to Audible, the one recovered ASIN to DB, leave the
    unrecovered one out of source_by_key entirely (nothing to attribute),
    and mark the response incomplete for the shortfall the backstop itself
    could not cover."""
    from app.services.audible.books import get_books_by_asins

    good_asins = [f"B0GOOD{i:03d}" for i in range(50)]
    bad_asins = ["B0BAD0001", "B0BAD0002"]
    recovered_book = {"asin": bad_asins[0], "title": "From DB backstop"}
    facts = ResponseFacts()

    async def _get(region, path, params):
        asins = params["asins"].split(",")
        if asins == good_asins:
            return {"products": [_hydration_product(a) for a in asins]}
        raise RuntimeError("Audible 500")

    async def _db_backstop(session, asins):
        assert set(asins) == set(bad_asins)
        return [recovered_book]

    with patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.get_books_from_db", new=AsyncMock(side_effect=_db_backstop)), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get", return_value=None):
        result = await get_books_by_asins(
            good_asins + bad_asins, "us", AsyncMock(), facts=facts
        )

    assert {b["asin"] for b in result} == set(good_asins) | {bad_asins[0]}
    assert facts.source_counts == {"audible": 50, "cache": 0, "db": 1}
    assert facts.source_by_key[bad_asins[0]] == SOURCE_DB
    assert all(facts.source_by_key[a] == SOURCE_AUDIBLE for a in good_asins)
    assert bad_asins[1] not in facts.source_by_key
    assert not facts.is_complete
    assert facts.incomplete_reasons == {REASON_HYDRATION_FAILED}
    assert sum(facts.source_counts.values()) == len(facts.source_by_key)


@pytest.mark.asyncio
async def test_get_books_by_asins_facts_records_hydration_not_found_for_a_hollow_stub():
    """Audible's batch endpoint never 404s for an ASIN it doesn't recognize
    -- it answers 200 with a titleless stub that _filter_products drops
    (see that function and its own unit test above). One real product and
    one hollow stub in the same batch response: the real one must hydrate
    normally, and the stub's ASIN -- present in the request, absent from
    what came back after filtering -- must record REASON_HYDRATION_NOT_FOUND,
    the same as a chunk that raised NotFoundException outright, not a
    REASON_HYDRATION_FAILED transient-retry reason it never actually hit."""
    from app.services.audible.books import get_books_by_asins

    found_asin = "B0FOUND001"
    stub_asin = "B0NOTFOUND1"

    async def _get(region, path, params):
        asins = params["asins"].split(",")
        assert set(asins) == {found_asin, stub_asin}
        return {"products": [
            _hydration_product(found_asin),
            {"asin": stub_asin, "product_state": "NOT_AVAILABLE_FOR_PURCHASE"},
        ]}

    facts = ResponseFacts()

    with patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.get_books_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        result = await get_books_by_asins(
            [found_asin, stub_asin], "us", AsyncMock(), use_cache=True, facts=facts
        )

    assert {b["asin"] for b in result} == {found_asin}
    assert facts.source_by_key == {found_asin: SOURCE_AUDIBLE}
    assert not facts.is_complete
    assert facts.incomplete_reasons == {REASON_HYDRATION_NOT_FOUND}
    assert sum(facts.source_counts.values()) == len(facts.source_by_key)


@pytest.mark.asyncio
async def test_get_books_by_asins_facts_records_outage_db_fallback():
    """Audible down, a pre-fetch cache hit already in hand, and the in-try
    DB backstop read itself failing -- the only way to reach the
    except-block DB fallback with a non-empty cached_results segment (see
    the section note above). facts must credit the pre-fetch hit to cache
    and the outage-fallback DB row to DB. X-Libex-Complete now asserts
    element coverage, not "did some internal retry occur along the way" --
    and the outage-fallback DB read here recovers the one ASIN it was asked
    for, so despite the outage this response is fully covered and must be
    complete, not incomplete."""
    from app.services.audible.books import get_books_by_asins

    cached_asin = "B0CACHED01"
    failed_asin = "B0FAILED01"
    cached_book = {"asin": cached_asin, "title": "Cached"}
    outage_book = {"asin": failed_asin, "title": "From DB, post-outage"}
    facts = ResponseFacts()

    async def _get_many(session, keys):
        return {book_key(cached_asin, "us"): cached_book}

    backstop_calls = []

    async def _db_backstop(session, asins):
        backstop_calls.append(list(asins))
        if len(backstop_calls) == 1:
            raise RuntimeError("DB backstop read failed")
        return [outage_book]

    with patch("app.services.audible.books.cache.get_many", new=AsyncMock(side_effect=_get_many)), \
         patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=RuntimeError("Audible down"))), \
         patch("app.services.audible.books.get_books_from_db", new=AsyncMock(side_effect=_db_backstop)):
        result = await get_books_by_asins(
            [cached_asin, failed_asin], "us", AsyncMock(), use_cache=True, facts=facts
        )

    assert len(backstop_calls) == 2
    assert result == [cached_book, outage_book]
    assert facts.source_counts == {"audible": 0, "cache": 1, "db": 1}
    assert facts.source_by_key == {cached_asin: SOURCE_CACHE, failed_asin: SOURCE_DB}
    assert facts.is_complete
    assert facts.incomplete_reasons == set()
    assert sum(facts.source_counts.values()) == len(facts.source_by_key)


@pytest.mark.asyncio
async def test_get_books_by_asins_facts_records_outage_db_fallback_short_coverage():
    """The sibling above's short-coverage counterpart. A pre-fetch cache hit
    is still needed to reach the except-block DB fallback at all (see the
    section note -- without it, the empty-cached_results re-raise fires
    before the in-try backstop is even attempted); what's new here is that
    the outage-fallback DB read only recovers one of its own two ASINs.
    facts must credit the pre-fetch hit to cache, the recovered ASIN to DB,
    and mark the response incomplete for the one still genuinely missing --
    coverage, not "an internal retry happened", is what decides this now."""
    from app.services.audible.books import get_books_by_asins

    cached_asin = "B0CACHED02"
    recovered_asin = "B0RECOVER1"
    unrecovered_asin = "B0MISSING1"
    cached_book = {"asin": cached_asin, "title": "Cached"}
    recovered_book = {"asin": recovered_asin, "title": "From DB, post-outage"}
    facts = ResponseFacts()

    async def _get_many(session, keys):
        return {book_key(cached_asin, "us"): cached_book}

    backstop_calls = []

    async def _db_backstop(session, asins):
        backstop_calls.append(list(asins))
        if len(backstop_calls) == 1:
            raise RuntimeError("DB backstop read failed")
        return [recovered_book]

    with patch("app.services.audible.books.cache.get_many", new=AsyncMock(side_effect=_get_many)), \
         patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=RuntimeError("Audible down"))), \
         patch("app.services.audible.books.get_books_from_db", new=AsyncMock(side_effect=_db_backstop)):
        result = await get_books_by_asins(
            [cached_asin, recovered_asin, unrecovered_asin], "us", AsyncMock(), use_cache=True, facts=facts
        )

    assert len(backstop_calls) == 2
    assert result == [cached_book, recovered_book]
    assert facts.source_counts == {"audible": 0, "cache": 1, "db": 1}
    assert facts.source_by_key == {cached_asin: SOURCE_CACHE, recovered_asin: SOURCE_DB}
    assert not facts.is_complete
    assert facts.incomplete_reasons == {REASON_HYDRATION_FAILED}
    assert sum(facts.source_counts.values()) == len(facts.source_by_key)


@pytest.mark.asyncio
async def test_get_books_by_asins_facts_records_outage_cache_fallback_combining_segments():
    """Audible down, the in-try DB backstop read failing (reaching the
    outage branch the same way the DB-fallback sibling above does), and the
    outage branch's own DB read this time coming back empty -- falling
    through to the outage cache fallback. The pre-fetch cache hit and the
    fallback-only cache hit are two different ASINs answered by two
    different calls to cache.get_many (see the section note above); facts
    must combine both into a single cache attribution, one entry per key.
    The cache fallback here recovers the one ASIN it owed an answer for, so
    the response is fully covered and must be complete."""
    from app.services.audible.books import get_books_by_asins

    prefetch_asin = "B0PREFETCH"
    fallback_asin = "B0FALLBACK"
    prefetch_book = {"asin": prefetch_asin, "title": "Cache, pre-fetch"}
    fallback_book = {"asin": fallback_asin, "title": "Cache, outage fallback"}
    facts = ResponseFacts()

    get_many_calls = []

    async def _get_many(session, keys):
        get_many_calls.append(list(keys))
        if len(get_many_calls) == 1:
            return {book_key(prefetch_asin, "us"): prefetch_book}
        return {book_key(fallback_asin, "us"): fallback_book}

    db_backstop_calls = []

    async def _db_backstop(session, asins):
        db_backstop_calls.append(list(asins))
        if len(db_backstop_calls) == 1:
            raise RuntimeError("DB backstop read failed")
        return []

    with patch("app.services.audible.books.cache.get_many", new=AsyncMock(side_effect=_get_many)), \
         patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=RuntimeError("Audible down"))), \
         patch("app.services.audible.books.get_books_from_db", new=AsyncMock(side_effect=_db_backstop)):
        result = await get_books_by_asins(
            [prefetch_asin, fallback_asin], "us", AsyncMock(), use_cache=True, facts=facts
        )

    assert len(get_many_calls) == 2
    assert len(db_backstop_calls) == 2
    assert result == [prefetch_book, fallback_book]
    assert facts.source_counts == {"audible": 0, "cache": 2, "db": 0}
    assert facts.source_by_key == {prefetch_asin: SOURCE_CACHE, fallback_asin: SOURCE_CACHE}
    assert facts.is_complete
    assert facts.incomplete_reasons == set()
    assert sum(facts.source_counts.values()) == len(facts.source_by_key)


@pytest.mark.asyncio
async def test_get_books_by_asins_facts_records_outage_cache_fallback_short_coverage():
    """The combining-segments sibling's short-coverage counterpart: the
    outage cache fallback recovers only one of the two ASINs it was asked
    to answer for after the DB fallback came back empty for both. facts
    must combine the recovered segments into one cache attribution and mark
    the response incomplete for the ASIN neither the DB nor the cache
    fallback ever produced."""
    from app.services.audible.books import get_books_by_asins

    prefetch_asin = "B0PREFETCH"
    recovered_asin = "B0RECOVER1"
    unrecovered_asin = "B0MISSING1"
    prefetch_book = {"asin": prefetch_asin, "title": "Cache, pre-fetch"}
    recovered_book = {"asin": recovered_asin, "title": "Cache, outage fallback"}
    facts = ResponseFacts()

    get_many_calls = []

    async def _get_many(session, keys):
        get_many_calls.append(list(keys))
        if len(get_many_calls) == 1:
            return {book_key(prefetch_asin, "us"): prefetch_book}
        return {book_key(recovered_asin, "us"): recovered_book}

    db_backstop_calls = []

    async def _db_backstop(session, asins):
        db_backstop_calls.append(list(asins))
        if len(db_backstop_calls) == 1:
            raise RuntimeError("DB backstop read failed")
        return []

    with patch("app.services.audible.books.cache.get_many", new=AsyncMock(side_effect=_get_many)), \
         patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=RuntimeError("Audible down"))), \
         patch("app.services.audible.books.get_books_from_db", new=AsyncMock(side_effect=_db_backstop)):
        result = await get_books_by_asins(
            [prefetch_asin, recovered_asin, unrecovered_asin], "us", AsyncMock(), use_cache=True, facts=facts
        )

    assert len(get_many_calls) == 2
    assert len(db_backstop_calls) == 2
    assert result == [prefetch_book, recovered_book]
    assert facts.source_counts == {"audible": 0, "cache": 2, "db": 0}
    assert facts.source_by_key == {prefetch_asin: SOURCE_CACHE, recovered_asin: SOURCE_CACHE}
    assert not facts.is_complete
    assert facts.incomplete_reasons == {REASON_HYDRATION_FAILED}
    assert sum(facts.source_counts.values()) == len(facts.source_by_key)


@pytest.mark.asyncio
async def test_get_books_by_asins_facts_full_backstop_recovery_of_a_deadline_chunk_stays_complete():
    """A chunk abandoned at the deadline is not itself recorded as an
    incomplete reason (see the comment at the `results.append` site for
    the cancelled-task branch in books.py) -- only the residue the DB
    backstop can't cover is. When the backstop fully recovers every ASIN
    from the abandoned chunk, the response must be complete despite the
    deadline having fired partway through."""
    import app.services.audible.books as books_mod

    fast_asins = [f"B0FAST{i:05d}" for i in range(50)]
    slow_asins = [f"B0SLOW{i:05d}" for i in range(50)]
    stored = [{"asin": a, "title": "from the db", "region": "us"} for a in slow_asins]

    async def _one_fast_one_hanging(asins, region):
        if asins[0].startswith("B0FAST"):
            return [{"asin": a, "title": "live", "region": region} for a in asins]
        await asyncio.sleep(30)
        return []

    session = AsyncMock()
    session.rollback = AsyncMock()
    facts = ResponseFacts()

    with patch.object(books_mod, "_fetch_chunk", new=AsyncMock(side_effect=_one_fast_one_hanging)), \
         patch.object(books_mod, "get_books_from_db", new=AsyncMock(return_value=stored)), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        books = await books_mod.get_books_by_asins(
            fast_asins + slow_asins, "us", session, deadline=time.monotonic() + 0.25, facts=facts
        )

    assert {b["asin"] for b in books} == set(fast_asins) | set(slow_asins)
    assert facts.is_complete
    assert facts.incomplete_reasons == set()


@pytest.mark.asyncio
async def test_get_books_by_asins_facts_partial_backstop_recovery_of_a_deadline_chunk_is_hydration_deadline_not_failed():
    """The backstop only partially covering an abandoned chunk's ASINs must
    record REASON_HYDRATION_DEADLINE, not REASON_HYDRATION_FAILED -- the two
    loss classes are checked independently (see `_has_uncovered` and its two
    call sites in the in-try DB backstop) precisely so a deadline residue
    doesn't masquerade as a generic hydration failure it never actually
    hit."""
    import app.services.audible.books as books_mod

    fast_asins = [f"B0FAST{i:05d}" for i in range(50)]
    slow_asins = [f"B0SLOW{i:05d}" for i in range(50)]
    partially_stored = [{"asin": a, "title": "from the db", "region": "us"} for a in slow_asins[:40]]

    async def _one_fast_one_hanging(asins, region):
        if asins[0].startswith("B0FAST"):
            return [{"asin": a, "title": "live", "region": region} for a in asins]
        await asyncio.sleep(30)
        return []

    session = AsyncMock()
    session.rollback = AsyncMock()
    facts = ResponseFacts()

    with patch.object(books_mod, "_fetch_chunk", new=AsyncMock(side_effect=_one_fast_one_hanging)), \
         patch.object(books_mod, "get_books_from_db", new=AsyncMock(return_value=partially_stored)), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        books = await books_mod.get_books_by_asins(
            fast_asins + slow_asins, "us", session, deadline=time.monotonic() + 0.25, facts=facts
        )

    assert len(books) == 90
    assert not facts.is_complete
    assert facts.incomplete_reasons == {REASON_HYDRATION_DEADLINE}


@pytest.mark.asyncio
async def test_get_chapters_falls_back_to_db():
    """Chapters falls back to DB when Audible is unavailable."""
    from app.services.audible.books import get_chapters

    mock_session = AsyncMock()
    cached_chapters = {"chapters": [], "runtimeLengthMs": 0}

    with patch("app.services.audible.books.audible_get", side_effect=Exception("Audible down")), \
         patch("app.services.audible.books.get_track_from_db", new_callable=AsyncMock, return_value=cached_chapters), \
         patch("app.services.audible.books.cache.get", return_value=None):
        result = await get_chapters("B08G9PRS1K", "us", mock_session)
        assert result == cached_chapters

# ============================================================
# FETCH AND STORE CHAPTERS TESTS
# ============================================================

@pytest.mark.asyncio
async def test_fetch_and_store_chapters_stores_and_marks():
    """On success: stores the track, marks the book checked, returns 'stored'."""
    from app.services.audible.books import fetch_and_store_chapters

    mock_session = AsyncMock()
    data = {"content_metadata": {"chapter_info": {"chapters": []}}}

    with patch("app.services.audible.books.audible_get", return_value=data), \
         patch("app.services.audible.books.upsert_track", new_callable=AsyncMock) as mock_upsert:
        result = await fetch_and_store_chapters("B08G9PRS1K", "us", mock_session)

    assert result == "stored"
    mock_upsert.assert_called_once()
    # marked checked (the update + commit happened)
    mock_session.execute.assert_awaited()
    mock_session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_fetch_and_store_chapters_none_when_no_chapter_info():
    """Resolved but no chapter_info: marks checked, stores nothing, returns 'none'."""
    from app.services.audible.books import fetch_and_store_chapters

    mock_session = AsyncMock()
    data = {"content_metadata": {}}

    with patch("app.services.audible.books.audible_get", return_value=data), \
         patch("app.services.audible.books.upsert_track", new_callable=AsyncMock) as mock_upsert:
        result = await fetch_and_store_chapters("B08G9PRS1K", "us", mock_session)

    assert result == "none"
    mock_upsert.assert_not_called()
    mock_session.commit.assert_awaited()  # still marked checked


@pytest.mark.asyncio
async def test_fetch_and_store_chapters_not_found_marks_checked():
    """A 404 (NotFoundException) is terminal: marks checked, returns 'not_found'."""
    from app.services.audible.books import fetch_and_store_chapters
    from app.core.exceptions import NotFoundException

    mock_session = AsyncMock()

    with patch("app.services.audible.books.audible_get", side_effect=NotFoundException()), \
         patch("app.services.audible.books.upsert_track", new_callable=AsyncMock) as mock_upsert:
        result = await fetch_and_store_chapters("0008278482", "us", mock_session)

    assert result == "not_found"
    mock_upsert.assert_not_called()
    mock_session.commit.assert_awaited()  # marked so it isn't retried


@pytest.mark.asyncio
async def test_fetch_and_store_chapters_error_does_not_mark():
    """A transient error does NOT mark checked (so it retries) and returns 'error'."""
    from app.services.audible.books import fetch_and_store_chapters

    mock_session = AsyncMock()

    with patch("app.services.audible.books.audible_get", side_effect=Exception("Audible 500")), \
         patch("app.services.audible.books.upsert_track", new_callable=AsyncMock) as mock_upsert:
        result = await fetch_and_store_chapters("B08G9PRS1K", "us", mock_session)

    assert result == "error"
    mock_upsert.assert_not_called()
    mock_session.commit.assert_not_awaited()  # NOT marked


@pytest.mark.asyncio
async def test_fetch_and_store_chapters_never_raises_on_store_failure():
    """A write failure is swallowed (returns 'error'), never propagates."""
    from app.services.audible.books import fetch_and_store_chapters

    mock_session = AsyncMock()
    data = {"content_metadata": {"chapter_info": {"chapters": []}}}

    with patch("app.services.audible.books.audible_get", return_value=data), \
         patch("app.services.audible.books.upsert_track", new_callable=AsyncMock, side_effect=Exception("DB write failed")):
        result = await fetch_and_store_chapters("B08G9PRS1K", "us", mock_session)

    assert result == "error"
    mock_session.rollback.assert_awaited()


def test_the_unreadable_plans_warning_fires_on_a_freshly_booted_process():
    """The first "plans present but unreadable" warning must not be lost on a
    process younger than the log window.

    time.monotonic() counts from boot, so a 0.0 "never reported" sentinel
    makes the window check true for the first minute of a process's life and
    swallows the first report. This warning is the only thing that catches an
    upstream rename before the plans column quietly empties across the
    corpus, and six worker processes all start fresh on every deploy -- so
    the minute it was silent for is exactly the minute after a deploy.

    Driven against a fresh-boot clock rather than the ambient one, because
    the ambient one hides it: any machine with more than a minute of uptime
    passes regardless of which sentinel is used."""
    import app.services.audible.books as books_mod

    with patch.object(books_mod, "_unreadable_plans_last_logged", None), \
         patch.object(books_mod, "_unreadable_plans_count", 0), \
         patch.object(books_mod.time, "monotonic", return_value=12.0), \
         patch.object(books_mod, "logger") as mock_logger:
        books_mod._log_unreadable_plans("B08G9PRS1K")

    mock_logger.warning.assert_called_once()
    assert "unreadable" in mock_logger.warning.call_args.args[0].lower()


def test_the_unreadable_plans_warning_still_windows_after_the_first():
    """The window must still close, or the fix trades a swallowed first
    report for a per-book flood."""
    import app.services.audible.books as books_mod

    with patch.object(books_mod, "_unreadable_plans_last_logged", 12.0), \
         patch.object(books_mod, "_unreadable_plans_count", 0), \
         patch.object(books_mod.time, "monotonic", return_value=12.0), \
         patch.object(books_mod, "logger") as mock_logger:
        books_mod._log_unreadable_plans("B08G9PRS1K")

    mock_logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_hydration_stops_at_the_deadline_and_keeps_what_landed():
    """Hydration is bounded by the request's own budget, and an abandoned
    chunk does not discard the chunks that already came back.

    Before this, the deadline reached discovery and nothing else: the walk
    stopped at its budget and then handed an unbounded fan-out to a caller
    the proxy was already timing out on, so the worst case was the discovery
    budget PLUS however long the books took. asyncio.wait rather than
    wait_for is what keeps the landed chunks -- wait_for cancels the whole
    gather and throws them away."""
    import app.services.audible.books as books_mod

    async def _one_fast_one_hanging(asins, region):
        if asins[0].startswith("B0FAST"):
            return [{"asin": a, "title": "t", "region": region} for a in asins]
        await asyncio.sleep(30)
        return []

    session = AsyncMock()
    session.rollback = AsyncMock()
    # Distinct ASINs: the fetch dedupes, and chunking is at 50, so this is
    # exactly one fast chunk followed by one that never returns.
    asins = [f"B0FAST{i:05d}" for i in range(50)] + [f"B0SLOW{i:05d}" for i in range(50)]

    with patch.object(books_mod, "_fetch_chunk", new=AsyncMock(side_effect=_one_fast_one_hanging)), \
         patch.object(books_mod, "get_books_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        books = await books_mod.get_books_by_asins(
            asins, "us", session, deadline=time.monotonic() + 0.25
        )

    # The fast chunk survived; the hanging one was abandoned rather than
    # taking the whole response down with it.
    assert len(books) == 50
    assert all(b["asin"].startswith("B0FAST") for b in books)


@pytest.mark.asyncio
async def test_hydration_without_a_deadline_still_waits_for_every_chunk():
    """The bound is opt-in. Every caller that passes no deadline -- the seeder,
    the refresh, every non-author route -- must behave exactly as it did
    before the parameter existed."""
    import app.services.audible.books as books_mod

    async def _slow_but_finite(asins, region):
        await asyncio.sleep(0.05)
        return [{"asin": a, "title": "t", "region": region} for a in asins]

    session = AsyncMock()
    session.rollback = AsyncMock()

    with patch.object(books_mod, "_fetch_chunk", new=AsyncMock(side_effect=_slow_but_finite)), \
         patch.object(books_mod, "get_books_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        books = await books_mod.get_books_by_asins(
            [f"B0AAA{i:05d}" for i in range(50)], "us", session
        )

    assert len(books) == 50


@pytest.mark.asyncio
async def test_cancelling_the_request_cancels_the_chunks_still_in_flight():
    """An outer cancellation must take the fan-out with it.

    asyncio.gather cancels its children when the coroutine awaiting it is
    cancelled; asyncio.wait does NOT, and swapping one for the other silently
    dropped that guarantee. Without the try/finally, a graceful shutdown --
    or anything that later wraps these routes in wait_for -- unwinds out of
    the wait and leaves every in-flight chunk running detached, each holding
    an Audible pool permit and an httpx connection, with any exception it
    raises never retrieved.

    Discovery has a test for exactly this on its own leader await; hydration
    did not, which is how the regression got in."""
    import app.services.audible.books as books_mod

    started = asyncio.Event()
    chunk_tasks: list[asyncio.Task] = []

    async def _hangs(asins, region):
        started.set()
        await asyncio.sleep(30)
        return []

    session = AsyncMock()
    session.rollback = AsyncMock()

    real_ensure_future = asyncio.ensure_future

    def _track(coro):
        task = real_ensure_future(coro)
        chunk_tasks.append(task)
        return task

    with patch.object(books_mod, "_fetch_chunk", new=AsyncMock(side_effect=_hangs)), \
         patch.object(books_mod.asyncio, "ensure_future", new=_track), \
         patch.object(books_mod, "get_books_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        outer = asyncio.ensure_future(
            books_mod.get_books_by_asins([f"B0AAA{i:05d}" for i in range(50)], "us", session)
        )
        await started.wait()
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

    assert chunk_tasks, "the fan-out never started, so this asserts nothing"
    # Let the cancellations settle, then confirm nothing is left running.
    await asyncio.gather(*chunk_tasks, return_exceptions=True)
    assert all(t.done() for t in chunk_tasks), "a chunk survived the request being cancelled"


@pytest.mark.asyncio
async def test_an_abandoned_chunk_falls_through_to_the_stored_copy():
    """A chunk cut off by the deadline must still answer from the database.

    This is HydrationDeadlineExceeded's entire reason for being an Exception
    rather than a CancelledError: it has to land in the branch that routes a
    failed chunk to the DB backstop, so the books are served from what Libex
    already stored instead of being dropped. Routing it away from the backstop
    passes both other hydration tests, which is how the gap was found."""
    import app.services.audible.books as books_mod

    stored = [{"asin": f"B0SLOW{i:05d}", "title": "from the db", "region": "us"}
              for i in range(50)]

    async def _one_fast_one_hanging(asins, region):
        if asins[0].startswith("B0FAST"):
            return [{"asin": a, "title": "live", "region": region} for a in asins]
        await asyncio.sleep(30)
        return []

    session = AsyncMock()
    session.rollback = AsyncMock()
    asins = [f"B0FAST{i:05d}" for i in range(50)] + [f"B0SLOW{i:05d}" for i in range(50)]

    with patch.object(books_mod, "_fetch_chunk", new=AsyncMock(side_effect=_one_fast_one_hanging)), \
         patch.object(books_mod, "get_books_from_db", new=AsyncMock(return_value=stored)) as mock_db, \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        books = await books_mod.get_books_by_asins(
            asins, "us", session, deadline=time.monotonic() + 0.25
        )

    # The abandoned chunk's ASINs were handed to the backstop...
    mock_db.assert_awaited()
    backstop_asins = mock_db.await_args.args[1] if len(mock_db.await_args.args) > 1 else []
    assert any(a.startswith("B0SLOW") for a in backstop_asins), (
        "the abandoned chunk's ASINs never reached the DB backstop"
    )
    # ...and their stored copies are in the response alongside the live ones.
    returned = {b["asin"] for b in books}
    assert any(a.startswith("B0FAST") for a in returned), "lost the chunk that landed"
    assert any(a.startswith("B0SLOW") for a in returned), "lost the stored fallback"


# ============================================================
# PERSIST OUTCOME -- persist_outcome OUT-PARAMETER
# ============================================================
# get_books_by_asins' persist_outcome mirrors the facts idiom above: a list
# a caller can pass in to learn something this call decided internally, with
# None costing every existing caller nothing. What it reports is whether the
# background persist queue admitted or shed the books this call just fetched
# from Audible -- see app.services.db.persist_queue.PersistOutcome. The seeder
# is the caller that actually gates on it (see tests/services/test_seeder_
# shed_awareness.py); these tests pin the plumbing in this module alone.
#
# The shed case below forces app.services.db.persist_queue's real backlog
# state full rather than mocking persist_books_background's return value, so
# the assertion is against the real admission decision, not a stand-in for
# it. That is safe to do without touching Postgres or the network: a shed
# batch returns out of _spawn before any task is created (see PersistOutcome
# and _spawn's own docstrings), so nothing here ever reaches _BackgroundSession.
# The admitted case, by contrast, does mock persist_books_background -- an
# admitted call schedules a real background write, and this module's own
# tests never let that write actually run against a real engine.

@pytest.mark.asyncio
async def test_get_books_by_asins_persist_outcome_records_a_real_forced_shed():
    """Forces app.services.db.persist_queue's actual backlog to capacity --
    the same technique tests/services/test_persist_queue.py uses to force
    shedding deterministically, rather than waiting on 5000 real books --
    and asserts persist_outcome is populated with the queue's own, genuine
    SHED decision. persist_books_background is not mocked: this is the one
    test in this module that lets the real admission check run."""
    import app.services.db.persist_queue as pq
    from app.services.audible.books import get_books_by_asins
    from app.services.db.persist_queue import PersistOutcome

    asin = "B0SHED0001"
    session = AsyncMock()
    session.rollback = AsyncMock()
    outcome: list[PersistOutcome] = []

    original_queued = pq._queued_books
    pq._queued_books = pq._PERSIST_BACKLOG_MAX_BOOKS
    try:
        with patch(
            "app.services.audible.books.audible_get",
            new=AsyncMock(return_value={"product": _hydration_product(asin)}),
        ):
            result = await get_books_by_asins(
                [asin], "us", session, persist_outcome=outcome
            )
    finally:
        pq._queued_books = original_queued
        pq._inflight.clear()

    assert result[0]["asin"] == asin
    assert outcome == [PersistOutcome.SHED]
    # A shed batch never reaches _spawn's task-creation branch.
    assert pq._inflight == set()


@pytest.mark.asyncio
async def test_get_books_by_asins_persist_outcome_records_admitted():
    """The normal, non-degraded case: persist_books_background reports
    ADMITTED and the out-parameter carries that value through unchanged.
    Mocked rather than exercised against the real queue -- an admitted call
    schedules a real background write, which this module's tests never let
    run against a real engine."""
    from app.services.audible.books import get_books_by_asins
    from app.services.db.persist_queue import PersistOutcome

    asin = "B0ADMIT001"
    session = AsyncMock()
    session.rollback = AsyncMock()
    outcome: list[PersistOutcome] = []

    with patch(
        "app.services.audible.books.audible_get",
        new=AsyncMock(return_value={"product": _hydration_product(asin)}),
    ), patch(
        "app.services.audible.books.persist_books_background",
        return_value=PersistOutcome.ADMITTED,
    ) as mock_persist:
        result = await get_books_by_asins(
            [asin], "us", session, persist_outcome=outcome
        )

    assert result[0]["asin"] == asin
    mock_persist.assert_called_once()
    assert outcome == [PersistOutcome.ADMITTED]


@pytest.mark.asyncio
async def test_get_books_by_asins_persist_outcome_defaults_to_none_harmlessly():
    """Every existing caller that doesn't pass persist_outcome at all --
    every book route -- must be unaffected: the call still fetches, still
    persists, and raises nothing for not asking."""
    from app.services.audible.books import get_books_by_asins
    from app.services.db.persist_queue import PersistOutcome

    asin = "B0NOARG001"
    session = AsyncMock()
    session.rollback = AsyncMock()

    with patch(
        "app.services.audible.books.audible_get",
        new=AsyncMock(return_value={"product": _hydration_product(asin)}),
    ), patch(
        "app.services.audible.books.persist_books_background",
        return_value=PersistOutcome.ADMITTED,
    ) as mock_persist:
        result = await get_books_by_asins([asin], "us", session)

    assert result[0]["asin"] == asin
    mock_persist.assert_called_once()
