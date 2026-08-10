"""
Authors service unit tests.
Tests normalization helpers without hitting Audible.
"""

# Standard library
import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.services.audible.authors import (
    _normalize_author,
    _generate_session_id,
    _select_asin_rows,
    _extract_row_asins,
    _extract_next_token,
    _ScreenBooksResult,
    SCREENS_REASON_COMPLETED,
    SCREENS_REASON_TOKEN_REPEATED,
    SCREENS_REASON_TOKEN_REJECTED,
    SCREENS_REASON_PAGE_CAP,
    SCREENS_REASON_ASIN_CAP,
    SCREENS_REASON_PAGE_ERROR,
    SCREENS_REASON_NON_DICT_PAGE,
    SCREENS_REASON_TIME_BUDGET,
    SCREENS_REASON_GRID_NOT_FOUND,
    SCREENS_REASON_TRUNCATED,
    SCREENS_REASON_PLATEAU_TRUNCATED,
    SCREENS_CLEAN_REASONS,
    SCREENS_MAX_SECTIONS,
    SCREENS_MAX_ROWS_PER_PAGE,
    SCREENS_PAGE_SIZE,
    _CatalogBooksResult,
)

# ============================================================
# SCREEN FIXTURE LOADER
#
# Fixtures under tests/fixtures/screens/ are trimmed captures of real
# audible-android-author-detail responses (us/au sanderson, us christie).
# Row counts are cut down for size, but every key path the parser reads
# (sections[].model.__component_type, .header.header_model.product_count,
# .rows[].product_metadata.asin/.authors, sections[].pagination) is real
# structure, and every ASIN, title, and continuation token is a real
# captured value — none of it is hand-invented.
# ============================================================

_SCREEN_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "screens"


def _load_screen_fixture(name: str) -> dict:
    return json.loads((_SCREEN_FIXTURES_DIR / f"{name}.json").read_text())


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
         patch("app.services.audible.authors.persist_author_background") as mock_persist, \
         patch("app.services.audible.authors.cache.get", return_value=None):
        await get_author("B000APF21M", "us", mock_session)
        mock_persist.assert_called_once()


# ============================================================
# AUTHOR BOOKS BY NAME — catalog search fallback
# (fetch_author_books_by_name: renamed from the private
# _fetch_author_books_by_name and now shared with the seeder)
# ============================================================

def _product_by_name(asin, author_name, language="english"):
    return {
        "asin": asin,
        "authors": [{"name": author_name}],
        "language": language,
    }


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_matches_author_case_insensitively():
    """Author-name matching is case-insensitive exact match, same as the
    deleted seeder helper it replaces."""
    from app.services.audible.authors import fetch_author_books_by_name

    page = {"products": [_product_by_name("B0MATCH0001", "FRANK HERBERT")]}
    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        asins, _ = await fetch_author_books_by_name("frank herbert", "us")
    assert asins == ["B0MATCH0001"]


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_excludes_non_matching_author():
    from app.services.audible.authors import fetch_author_books_by_name

    page = {"products": [_product_by_name("B0OTHER0001", "Some Other Author")]}
    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        asins, _ = await fetch_author_books_by_name("Frank Herbert", "us")
    assert asins == []


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_includes_every_language_no_filter():
    """The English-only language filter was removed entirely from
    _accept_name_search_products (see its docstring) — region alone scopes
    results now, since region scoping is per-host and a store's catalogue
    already IS what that region means. Verified live: the removed filter was
    silently dropping 490 of Christie's 1100 US ASINs (125 German, 118
    Spanish, 94 Italian, 63 Swedish and more). A non-English edition matching
    the author must now be included, not dropped — this replaces the old
    test_fetch_author_books_by_name_requires_english_language, which pinned
    the exact opposite (and now-deleted) behavior."""
    from app.services.audible.authors import fetch_author_books_by_name

    page = {"products": [
        _product_by_name("B0ENGLISH01", "Frank Herbert", language="english"),
        _product_by_name("B0GERMAN001", "Frank Herbert", language="german"),
    ]}
    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        asins, _ = await fetch_author_books_by_name("Frank Herbert", "us")
    assert set(asins) == {"B0ENGLISH01", "B0GERMAN001"}


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_accepts_englisch_language_label():
    """Regression pin, kept post-filter-removal: an Audible DE-region product
    labeled 'Englisch' (not 'english') was previously at risk of being
    dropped by a language-label mismatch specifically, distinct from the
    broader every-language case above — now moot as a special case since no
    language field is inspected at all, but still worth pinning directly so a
    future reintroduction of language filtering doesn't quietly regress this
    label."""
    from app.services.audible.authors import fetch_author_books_by_name

    page = {"products": [_product_by_name("B0ENGLISH02", "Frank Herbert", language="Englisch")]}
    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        asins, _ = await fetch_author_books_by_name("Frank Herbert", "us")
    assert asins == ["B0ENGLISH02"]


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_dedupes_asins():
    from app.services.audible.authors import fetch_author_books_by_name

    page = {"products": [
        _product_by_name("B0DUPE00001", "Frank Herbert"),
        _product_by_name("B0DUPE00001", "Frank Herbert"),
    ]}
    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        asins, _ = await fetch_author_books_by_name("Frank Herbert", "us")
    assert asins.count("B0DUPE00001") == 1


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_stops_on_short_page():
    """A page shorter than num_results (50) ends the walk without a further page."""
    from app.services.audible.authors import fetch_author_books_by_name

    page = {"products": [_product_by_name("B0SHORT0001", "Frank Herbert")]}
    mock_get = AsyncMock(return_value=page)
    with patch("app.services.audible.authors.audible_get", new=mock_get):
        await fetch_author_books_by_name("Frank Herbert", "us")
    assert mock_get.await_count == 1


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_paginates_full_pages():
    """A full 50-product page continues on to the next page."""
    from app.services.audible.authors import fetch_author_books_by_name

    full_page = {"products": [_product_by_name(f"B0FULL{i:05d}", "Frank Herbert") for i in range(50)]}
    short_page = {"products": [_product_by_name("B0LASTPAGE1", "Frank Herbert")]}
    mock_get = AsyncMock(side_effect=[full_page, short_page])
    with patch("app.services.audible.authors.audible_get", new=mock_get):
        asins, _ = await fetch_author_books_by_name("Frank Herbert", "us")
    assert mock_get.await_count == 2
    assert "B0LASTPAGE1" in asins
    assert len(asins) == 51


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_never_passes_extra_headers():
    """The catalog-search call is one of the pre-existing five call sites —
    it must never carry the screens-only device header."""
    from app.services.audible.authors import fetch_author_books_by_name

    page = {"products": [_product_by_name("B0NOHEADER1", "Frank Herbert")]}
    mock_get = AsyncMock(return_value=page)
    with patch("app.services.audible.authors.audible_get", new=mock_get):
        await fetch_author_books_by_name("Frank Herbert", "us")
    assert "extra_headers" not in mock_get.await_args.kwargs


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_never_overrides_concurrency():
    """The seeder-facing wrapper must never pass a concurrency override --
    _fetch_author_books_by_name_detailed defaults to concurrency=1 (fully
    sequential) specifically so the seeder's own paced per-author loop,
    which calls this wrapper, is untouched by the live request path's
    parallel batching. A future edit defaulting this wrapper to pass a
    concurrency override would silently turn every seeder author into a
    concurrent-request burst, defeating that pacing on a shared IP that has
    already been throttled into a VPN rotation once from an amplified
    request burst."""
    from app.services.audible.authors import fetch_author_books_by_name

    mock_detailed = AsyncMock(return_value=(["B0SEEDER001"], 1, True))
    with patch("app.services.audible.authors._fetch_author_books_by_name_detailed", new=mock_detailed):
        await fetch_author_books_by_name("Frank Herbert", "us")

    assert "concurrency" not in mock_detailed.await_args.kwargs


# ============================================================
# SCREEN ROW SELECTION — the two-section trap (_select_asin_rows)
# ============================================================

def _row(asin, authors=None):
    metadata = {"asin": asin}
    if authors is not None:
        metadata["authors"] = authors
    return {"product_metadata": metadata}


def _asin_section(rows, product_count=None, pagination=None):
    header_model = {"product_count": product_count} if product_count is not None else {"title": "Most Popular"}
    return {
        "model": {
            "__component_type": "StandardAsinRowList",
            "header": {"header_model": header_model},
            "rows": rows,
        },
        "pagination": pagination,
    }


def test_select_asin_rows_finds_grid_when_teaser_comes_first():
    """Page 1 carries the 'most popular' teaser before the full grid — a
    parser that takes the first StandardAsinRowList match would return the
    teaser instead of the grid."""
    grid_rows = [_row("B0GRIDA"), _row("B0GRIDB")]
    teaser_rows = [_row("B0TEASERA")]
    data = {"sections": [_asin_section(teaser_rows), _asin_section(grid_rows, product_count=2)]}

    grid, other, count, truncated = _select_asin_rows(data)

    assert grid == grid_rows
    assert other == teaser_rows
    assert count == 2
    assert truncated == 0


def test_select_asin_rows_finds_grid_when_grid_comes_first():
    """Order-independence: the grid is identified by product_count, not position."""
    grid_rows = [_row("B0GRIDA")]
    teaser_rows = [_row("B0TEASERA")]
    data = {"sections": [_asin_section(grid_rows, product_count=1), _asin_section(teaser_rows)]}

    grid, other, count, truncated = _select_asin_rows(data)

    assert grid == grid_rows
    assert other == teaser_rows
    assert count == 1
    assert truncated == 0


def test_select_asin_rows_single_section_is_grid_by_elimination():
    """A single StandardAsinRowList section with no product_count is
    ambiguous, not 'the grid by elimination' — it must route through the
    same attribution-required path as any other unidentified candidate.
    This is the exact defect the panel found: a lone candidate's rows used
    to be admitted unconditionally with no attribution check, so a page
    whose real grid sat past SCREENS_MAX_SECTIONS could serve an unrelated
    rail's books as the author's catalogue. A decoy row attributed to
    someone else must still be rejected once routed through
    _extract_row_asins, proving the attribution gate actually bites rather
    than this section being pre-admitted as the grid."""
    decoy_rows = [_row("B0DECOY0001", authors=[{"asin": "B000OTHERX", "name": "Other"}])]
    data = {"sections": [_asin_section(decoy_rows)]}

    grid, other, count, truncated = _select_asin_rows(data)

    assert grid == []
    assert other == decoy_rows
    assert count is None
    assert truncated == 0

    asins, invalid, rejected, _ = _extract_row_asins(other, required_author_asin="B000TARGET")
    assert asins == []
    assert rejected == 1


def test_select_asin_rows_ambiguous_multi_section_without_product_count_returns_all_as_other():
    """When more than one candidate section exists and none reports
    product_count, no section can be trusted as the unconstrained grid, so
    every row needs attribution rather than being assumed safe."""
    rows_a = [_row("B0AMBIGA")]
    rows_b = [_row("B0AMBIGB")]
    data = {"sections": [_asin_section(rows_a), _asin_section(rows_b)]}

    grid, other, count, truncated = _select_asin_rows(data)

    assert grid == []
    assert sorted(r["product_metadata"]["asin"] for r in other) == sorted(
        r["product_metadata"]["asin"] for r in rows_a + rows_b
    )
    assert count is None
    assert truncated == 0


def test_select_asin_rows_missing_sections_key_returns_empty():
    assert _select_asin_rows({}) == ([], [], None, 0)


def test_select_asin_rows_ignores_non_asin_row_list_components():
    data = {"sections": [{
        "model": {"__component_type": "SomeOtherWidget", "rows": [{"whatever": True}]},
        "pagination": None,
    }]}
    assert _select_asin_rows(data) == ([], [], None, 0)


# ============================================================
# SCREEN ROW EXTRACTION — the attribution guard (_extract_row_asins)
# ============================================================

def test_extract_row_asins_grid_rows_accepted_unconditionally():
    """Grid rows carry no 'authors' key at all and must be accepted without
    an attribution check."""
    rows = [_row("B0GRIDROW1")]
    asins, invalid, rejected, truncated = _extract_row_asins(rows)
    assert asins == [rows[0]["product_metadata"]["asin"]]
    assert invalid == 0
    assert rejected == 0
    assert truncated == 0


def test_extract_row_asins_teaser_row_by_requested_author_included():
    """A teaser row is included when it names the requested author — matched
    case-insensitively."""
    rows = [_row("B0TEASERGD", authors=[{"asin": "b000target", "name": "Target"}])]
    asins, invalid, rejected, truncated = _extract_row_asins(rows, required_author_asin="B000TARGET")
    assert asins == [rows[0]["product_metadata"]["asin"]]
    assert invalid == 0
    assert rejected == 0
    assert truncated == 0


def test_extract_row_asins_teaser_row_by_different_author_excluded():
    """A teaser row by a different author must not enter the result — this
    is the load-bearing asymmetry: it is the only thing stopping a foreign
    author's title from slipping in through a non-grid rail. The rejection
    is counted as attribution_rejected, distinct from invalid_skipped."""
    rows = [_row("B0TEASERBD", authors=[{"asin": "B000OTHERX", "name": "Other"}])]
    asins, invalid, rejected, truncated = _extract_row_asins(rows, required_author_asin="B000TARGET")
    assert asins == []
    assert invalid == 0  # unattributable, not malformed
    assert rejected == 1
    assert truncated == 0


def test_extract_row_asins_row_without_authors_key_admitted_regardless_of_required_author_asin():
    """Admission is row-shape driven, not required-flag driven: a row that
    carries no authors key at all is accepted even when required_author_asin
    is passed, since only rows that carry an authors list get an attribution
    check at all. Live grid rows never carry product_metadata.authors in
    this position, so this exercises the defensive shape rule directly
    rather than describing a page shape Audible actually sends."""
    rows = [_row("B0NOAUTHOR")]
    asins, invalid, rejected, truncated = _extract_row_asins(rows, required_author_asin="B000TARGET")
    assert asins == [rows[0]["product_metadata"]["asin"]]
    assert invalid == 0
    assert rejected == 0
    assert truncated == 0


def test_extract_row_asins_invalid_and_attribution_rejected_counted_separately():
    """invalid_skipped (malformed ASIN) and attribution_rejected (wrong
    author) are two different failure modes and must be tallied in two
    different counters, not folded into one."""
    rows = [
        _row("TOO-SHORT"),
        _row("B0MISMATCH", authors=[{"asin": "B000OTHERX", "name": "Other"}]),
    ]
    asins, invalid, rejected, truncated = _extract_row_asins(rows, required_author_asin="B000TARGET")
    assert asins == []
    assert invalid == 1
    assert rejected == 1
    assert truncated == 0


def test_extract_row_asins_invalid_asin_skipped_not_fatal():
    rows = [_row("TOO-SHORT"), _row("B0VALIDROW")]
    asins, invalid, rejected, truncated = _extract_row_asins(rows)
    assert asins == [rows[1]["product_metadata"]["asin"]]
    assert invalid == 1
    assert rejected == 0
    assert truncated == 0


def test_extract_row_asins_uppercases_result():
    rows = [_row("b0lowercas")]
    asins, _, _, _ = _extract_row_asins(rows)
    assert asins == ["B0LOWERCAS"]


def test_extract_row_asins_non_dict_rows_are_skipped():
    asins, invalid, rejected, truncated = _extract_row_asins(["not-a-row", None, 12345])
    assert asins == []
    assert invalid == 0
    assert rejected == 0
    assert truncated == 0


# ============================================================
# CONTINUATION TOKEN (_extract_next_token)
# ============================================================

def test_extract_next_token_reads_last_section_not_first():
    """The Android app reads the last section's pagination field, not the
    titles section — a fixture where the two disagree proves the right one
    is read."""
    data = {"sections": [{"pagination": "FIRSTSECTIONTOKEN"}, {"pagination": "LASTSECTIONTOKEN"}]}
    assert _extract_next_token(data) == ("LASTSECTIONTOKEN", False)


def test_extract_next_token_non_string_is_rejected_not_absent():
    """A present-but-wrong-typed token is a rejection, distinguishable from
    a token that's genuinely absent."""
    assert _extract_next_token({"sections": [{"pagination": 12345}]}) == (None, True)


def test_extract_next_token_empty_string_reads_as_no_more_pages():
    """An explicit empty string is one of the genuinely-absent shapes, not a
    malformed one — it must not mark the walk unclean."""
    assert _extract_next_token({"sections": [{"pagination": ""}]}) == (None, False)


def test_extract_next_token_over_max_length_is_rejected():
    assert _extract_next_token({"sections": [{"pagination": "A" * 513}]}) == (None, True)


def test_extract_next_token_accepts_exactly_max_length():
    token = "A" * 512
    assert _extract_next_token({"sections": [{"pagination": token}]}) == (token, False)


def test_extract_next_token_invalid_characters_are_rejected():
    assert _extract_next_token({"sections": [{"pagination": "has a space"}]}) == (None, True)


def test_extract_next_token_accepts_full_charset():
    token = "Az09+/=_-"
    assert _extract_next_token({"sections": [{"pagination": token}]}) == (token, False)


def test_extract_next_token_missing_field_reads_as_no_more_pages():
    assert _extract_next_token({"sections": [{}]}) == (None, False)


def test_extract_next_token_no_sections_reads_as_no_more_pages():
    assert _extract_next_token({"sections": []}) == (None, False)


# ============================================================
# SCREEN WALK TEST HELPERS -- fan-out mocking
#
# Pages 2..N of the screens walk are now fetched concurrently, addressed by
# a minted token rather than one discovered by walking the page before it
# (see _fanout_screen_pages). A fixed-length side_effect list, sized for the
# old one-page-at-a-time walk, silently runs out the moment more than one
# page is requested per batch. These helpers route each request to the
# right response by decoding the page number back out of its own token,
# the same way a real upstream would answer any page it's asked for
# regardless of request order.
# ============================================================

def _decode_screen_token_page_num(token: str) -> int:
    payload = json.loads(base64.b64decode(token))
    return int(payload["pagination_info"]["page_num"])


def _screen_page_router(pages_by_num, default=None):
    """
    Builds an audible_get side_effect that serves a screens-walk request by
    the page number encoded in its continuation token (page 1, which
    carries none, maps to pages_by_num[1]) instead of by call order --
    required once pages 2..N are fetched concurrently rather than one at a
    time. `default`, when given, serves every page number not explicitly
    present in pages_by_num; otherwise a request for an unmapped page fails
    loudly rather than a test silently passing against the wrong content.
    """
    async def _side_effect(region, path, params, extra_headers=None):
        token = params.get("pageSectionContinuationToken")
        page_num = _decode_screen_token_page_num(token) if token else 1
        if page_num in pages_by_num:
            return pages_by_num[page_num]
        if default is not None:
            return default
        raise AssertionError(f"screen page router received unmapped page_num={page_num}")
    return _side_effect


# ============================================================
# SCREEN FETCH WALK (_fetch_author_books_by_screen)
# ============================================================

@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_unions_grid_and_attributed_teaser():
    """The grid is read (not just the first section), a teaser row by the
    requested author is unioned in, and a teaser row by someone else is not."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    grid_rows = [_row("B0GRID0001"), _row("B0GRID0002"), _row("B0GRID0003")]
    teaser_rows = [
        _row("B0TEASGOOD", authors=[{"asin": "b000target", "name": "Target Author"}]),
        _row("B0TEASBADX", authors=[{"asin": "B000OTHERX", "name": "Other Author"}]),
    ]
    page1 = {"sections": [
        _asin_section(teaser_rows, pagination=None),
        _asin_section(grid_rows, product_count=3, pagination=None),
    ]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page1)):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert set(result.asins) == {"B0GRID0001", "B0GRID0002", "B0GRID0003", "B0TEASGOOD"}
    assert "B0TEASBADX" not in result.asins


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_accepts_single_section_continuation_page():
    """Pages 2+ carry only one section, with no product_count — those rows
    must still be accepted, not dropped for lacking a grid marker."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = {"sections": [_asin_section([_row("B0PAGE1001")], product_count=2, pagination="NEXTTOKEN01")]}
    page2 = {"sections": [_asin_section([_row("B0PAGE2001")], pagination=None)]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=[page1, page2])):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert set(result.asins) == {"B0PAGE1001", "B0PAGE2001"}
    assert result.pages_fetched == 2


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_stops_on_repeated_token():
    """A server echoing the same continuation token must terminate the walk
    immediately — this is the amplification control, not a nicety. Without
    it, an echoing server turns one inbound request into a page-cap burst
    of outbound requests."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    echoing_page = {"sections": [_asin_section([_row("B0ECHO0001")], pagination="SAMETOKEN")]}
    mock_get = AsyncMock(return_value=echoing_page)

    with patch("app.services.audible.authors.audible_get", new=mock_get):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert mock_get.await_count == 2  # initial fetch + one continuation, then the repeat stops it
    assert result.pages_fetched == 2
    assert result.termination_reason == SCREENS_REASON_TOKEN_REPEATED
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_stops_on_rejected_token():
    """A token present but failing its shape check (wrong type here) must
    stop the walk on that page rather than being silently treated as absent
    — and it must be distinguishable from a genuine end-of-pagination."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    bad_token_page = {"sections": [_asin_section([_row("B0BADTOK01")], pagination=12345)]}
    mock_get = AsyncMock(return_value=bad_token_page)

    with patch("app.services.audible.authors.audible_get", new=mock_get):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert mock_get.await_count == 1
    assert result.asins == ["B0BADTOK01"]  # harvest from the one page is kept
    assert result.termination_reason == SCREENS_REASON_TOKEN_REJECTED
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_non_dict_page_stops_walk_but_keeps_prior_harvest():
    """A page that comes back as something other than a dict (e.g. Audible
    returning a bare list or string) must stop the walk without discarding
    ASINs already harvested from earlier pages."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = {"sections": [_asin_section([_row("B0GOODPG01")], pagination="TOK0001")]}
    mock_get = AsyncMock(side_effect=[page1, ["not", "a", "dict"]])

    with patch("app.services.audible.authors.audible_get", new=mock_get):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert mock_get.await_count == 2
    assert result.asins == ["B0GOODPG01"]
    assert result.pages_fetched == 2
    assert result.termination_reason == SCREENS_REASON_NON_DICT_PAGE
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_page_error_keeps_prior_pages_harvest():
    """A transient failure fetching page N must end the walk without
    discarding ASINs already harvested on pages 1..N-1 — collapsing partial
    progress to nothing (or to a single ASIN from a weaker fallback) is
    exactly the defect this walk exists to avoid."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = _load_screen_fixture("us_sanderson_page01")
    page2 = _load_screen_fixture("us_sanderson_page02")
    mock_get = AsyncMock(side_effect=[page1, page2, RuntimeError("upstream reset")])

    with patch("app.services.audible.authors.audible_get", new=mock_get):
        result = await _fetch_author_books_by_screen("B001IGFHW6", "us")

    expected_from_page1 = {"B002V0QCYU", "1250759781", "B0718Z5K4C", "B00HWF0MHW", "B002VA9IKK", "B002V5GLQ4"}
    expected_from_page2 = {"B019P7DVPE", "B005ZUI3OA", "B0B5M28HZK", "B0D18CT2VY", "B018UG5HJY"}
    assert set(result.asins) == expected_from_page1 | expected_from_page2
    # pages_fetched only increments on a successful fetch; the failing
    # third attempt that triggers PAGE_ERROR is not counted.
    assert result.pages_fetched == 2
    assert result.termination_reason == SCREENS_REASON_PAGE_ERROR
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_exceeding_deadline_stops_walk_and_sets_time_budget():
    """A deadline that's already passed by the time the next page would be
    fetched stops the walk there, keeps whatever was already harvested, and
    is distinguishable from every other termination reason."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = {"sections": [_asin_section([_row("B0BUDGET01"), _row("B0BUDGET02")], pagination="TOK0001")]}
    mock_get = AsyncMock(return_value=page1)

    with patch("app.services.audible.authors.audible_get", new=mock_get), \
         patch("app.services.audible.authors.time.monotonic", side_effect=[0.0, 100.0]):
        result = await _fetch_author_books_by_screen("B000TARGET", "us", deadline=50.0)

    assert mock_get.await_count == 1
    assert set(result.asins) == {"B0BUDGET01", "B0BUDGET02"}
    assert result.termination_reason == SCREENS_REASON_TIME_BUDGET
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


def test_screens_clean_reasons_contains_only_completed():
    """Every reason except a confirmed 'no more pages' counts as unclean —
    this is a direct pin on the set membership, not an inference from
    behavior."""
    assert SCREENS_CLEAN_REASONS == frozenset({SCREENS_REASON_COMPLETED})
    for reason in (
        SCREENS_REASON_TOKEN_REPEATED,
        SCREENS_REASON_TOKEN_REJECTED,
        SCREENS_REASON_PAGE_CAP,
        SCREENS_REASON_ASIN_CAP,
        SCREENS_REASON_PAGE_ERROR,
        SCREENS_REASON_NON_DICT_PAGE,
        SCREENS_REASON_TIME_BUDGET,
    ):
        assert reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_token_travels_in_params_not_path():
    """The token must be passed via params, never f-strung into path —
    audible_get builds its exception message from the URL alone, and
    main.py returns that message verbatim in a public 502 body."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = {"sections": [_asin_section([_row("B0PAGE1001")], product_count=2, pagination="NEXTTOKEN01")]}
    page2 = {"sections": [_asin_section([_row("B0PAGE2001")], pagination=None)]}
    mock_get = AsyncMock(side_effect=[page1, page2])

    with patch("app.services.audible.authors.audible_get", new=mock_get):
        await _fetch_author_books_by_screen("B000TARGET", "us")

    calls = mock_get.await_args_list
    assert len(calls) == 2
    paths = [call.args[1] for call in calls]
    assert paths[0] == paths[1]
    assert "NEXTTOKEN01" not in paths[1]

    second_params = calls[1].args[2]
    assert second_params["pageSectionContinuationToken"] == "NEXTTOKEN01"


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_sends_device_header():
    from app.services.audible.authors import _fetch_author_books_by_screen
    from app.services.audible.client import ANDROID_DEVICE_TYPE_ID

    page = {"sections": [_asin_section([_row("B0DEVICE01")], pagination=None)]}
    mock_get = AsyncMock(return_value=page)
    with patch("app.services.audible.authors.audible_get", new=mock_get):
        await _fetch_author_books_by_screen("B000TARGET", "us")

    assert mock_get.await_args.kwargs["extra_headers"] == {"X-Device-Type-Id": ANDROID_DEVICE_TYPE_ID}
    # Pinned against the literal, not just the constant reflected back at
    # itself: a wrong device id 200s with an empty page rather than raising,
    # so this is the only thing standing between a silent regression here
    # and a passing suite (see test_client.py's dedicated literal pin for
    # the constant's own value).
    assert mock_get.await_args.kwargs["extra_headers"] == {"X-Device-Type-Id": "A10KISP2GWF0E4"}


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_real_page_sends_exact_device_header_literal():
    """Walks a trimmed real page-1 capture and asserts the outbound header
    carries the exact literal device id — not the module constant compared
    to itself — on a call shaped like production traffic."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = _load_screen_fixture("us_sanderson_page01")
    mock_get = AsyncMock(side_effect=[page1, {"sections": []}])
    with patch("app.services.audible.authors.audible_get", new=mock_get):
        await _fetch_author_books_by_screen("B001IGFHW6", "us")

    first_call = mock_get.await_args_list[0]
    assert first_call.kwargs["extra_headers"] == {"X-Device-Type-Id": "A10KISP2GWF0E4"}


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_sends_author_asin_param():
    """author_asin travels as a query param on every page of the walk — the
    screens endpoint uses it (not the path ASIN alone) to scope the title
    grid to the requested author."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = {"sections": [_asin_section([_row("B0PAGE1001")], product_count=2, pagination="NEXTTOKEN01")]}
    page2 = {"sections": [_asin_section([_row("B0PAGE2001")], pagination=None)]}
    mock_get = AsyncMock(side_effect=[page1, page2])

    with patch("app.services.audible.authors.audible_get", new=mock_get):
        await _fetch_author_books_by_screen("B000TARGET", "us")

    for call in mock_get.await_args_list:
        assert call.args[2]["author_asin"] == "B000TARGET"


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_bogus_author_returns_empty_without_raising():
    """The screen endpoint 200s with zero rows for a bogus author ASIN — that
    must surface as an empty result, not an exception."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    empty_page = {"sections": []}
    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=empty_page)):
        result = await _fetch_author_books_by_screen("B000BOGUS0", "us")

    assert result.asins == []
    assert result.pages_fetched == 1


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_invalid_input_asin_short_circuits():
    from app.services.audible.authors import _fetch_author_books_by_screen

    mock_get = AsyncMock()
    with patch("app.services.audible.authors.audible_get", new=mock_get):
        result = await _fetch_author_books_by_screen("not-an-asin", "us")

    mock_get.assert_not_awaited()
    assert result.asins == []


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_stops_at_page_cap_and_warns_once():
    """Silent truncation is exactly what the less-data invariant forbids —
    when the page cap is hit, a WARNING must fire. The dedicated shortfall
    warning that used to compare len(asins) against product_count inside this
    function was removed (see the comment above the `if unclean:` block in
    _fetch_author_books_by_screen): product_count over-claims by more than 2x
    on real prolific authors, so that comparison was noise here. The
    equivalent completeness signal now lives one level up, computed against
    get_author_books' own union total rather than a single source's reach —
    see the GET AUTHOR BOOKS shortfall-warning tests."""
    from app.services.audible.authors import _fetch_author_books_by_screen, SCREENS_MAX_PAGES

    call_count = {"n": 0}

    # total_pages is now computed as ceil(product_count / SCREENS_PAGE_SIZE)
    # (see _fetch_author_books_by_screen) rather than discovered by walking
    # page-to-page, so forcing the walk to exhaust the page cap requires a
    # product_count whose implied page count -- not its raw value -- exceeds
    # SCREENS_MAX_PAGES.
    reported_product_count = (SCREENS_MAX_PAGES + 1000) * SCREENS_PAGE_SIZE

    async def _get(region, path, params, extra_headers=None):
        call_count["n"] += 1
        n = call_count["n"]
        product_count = reported_product_count if n == 1 else None
        return {"sections": [_asin_section(
            [_row(f"B0PAGE{n:04d}")], product_count=product_count, pagination=None,
        )]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.pages_fetched == SCREENS_MAX_PAGES
    assert len(result.asins) == SCREENS_MAX_PAGES

    mock_logger.warning.assert_called_once_with(
        "Audible Author Books screen ended without confirmed completion",
        extra={
            "author_asin": "B000TARGET",
            "region": "us",
            "termination_reason": SCREENS_REASON_PAGE_CAP,
            "pages_fetched": SCREENS_MAX_PAGES,
            "asins_collected": SCREENS_MAX_PAGES,
            "product_count": reported_product_count,
            "page_error": None,
            "sections_truncated": 0,
            "rows_truncated": 0,
        },
    )
    assert result.termination_reason == SCREENS_REASON_PAGE_CAP


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_stops_at_asin_cap_and_warns_once():
    from app.services.audible.authors import _fetch_author_books_by_screen, SCREENS_MAX_ASINS

    call_count = {"n": 0}
    reported_product_count = SCREENS_MAX_ASINS + 5000

    async def _get(region, path, params, extra_headers=None):
        call_count["n"] += 1
        n = call_count["n"]
        # Fixed-width hex index keeps every ASIN exactly 10 chars (B + 9 hex
        # digits) no matter how large n or i get, so none are skipped as
        # invalid at this scale -- a decimal, non-padded scheme silently
        # produced too-long, invalid ASINs once n exceeded 3 digits and
        # undercounted the real cap-hit total.
        rows = [_row(f"B{(n * 100 + i):09X}") for i in range(100)]
        product_count = reported_product_count if n == 1 else None
        return {"sections": [_asin_section(rows, product_count=product_count, pagination=f"TOK{n:04d}")]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert len(result.asins) == SCREENS_MAX_ASINS

    # Only the unclean-termination warning fires here now (see the sibling
    # page-cap test's docstring for why the old per-screen shortfall warning
    # is gone).
    mock_logger.warning.assert_called_once()
    unclean_extra = mock_logger.warning.call_args.kwargs["extra"]
    assert unclean_extra["author_asin"] == "B000TARGET"
    assert unclean_extra["termination_reason"] == SCREENS_REASON_ASIN_CAP
    assert unclean_extra["asins_collected"] == SCREENS_MAX_ASINS
    assert unclean_extra["product_count"] == reported_product_count

    assert result.termination_reason == SCREENS_REASON_ASIN_CAP


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_fanout_walk_matching_product_count_exactly_fires_no_warning():
    """A multi-page fan-out walk that collects exactly product_count titles
    across real SCREENS_PAGE_SIZE-row pages must not fire either warning —
    the truncation WARNING requires an actual shortfall, not merely landing
    on a page boundary, and the fan-out path must compute that the same way
    the sequential path already does.

    This intentionally stays well under SCREENS_MAX_PAGES rather than
    reproducing the old test's literal "page cap hit but zero shortfall"
    shape: under the new arithmetic, total_pages is derived directly from
    product_count (see _fetch_author_books_by_screen), so whenever
    product_count genuinely implies at least SCREENS_MAX_PAGES pages,
    _fanout_screen_pages' page_cap_clamped check is true unconditionally —
    even on a walk that ran to completion with every reported title
    collected and no plateau ever seen — and SCREENS_REASON_PAGE_CAP (an
    unclean reason) fires the "ended without confirmed completion" warning
    regardless of shortfall. There is no way to construct "genuinely at the
    page cap" and "zero warnings" together under the current code; that
    asymmetry is noted here rather than reproduced or asserted correct."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    total_pages = 10
    reported_product_count = total_pages * SCREENS_PAGE_SIZE
    call_count = {"n": 0}

    async def _get(region, path, params, extra_headers=None):
        call_count["n"] += 1
        n = call_count["n"]
        rows = [_row(f"B{(n * 100 + i):09X}") for i in range(SCREENS_PAGE_SIZE)]
        product_count = reported_product_count if n == 1 else None
        return {"sections": [_asin_section(rows, product_count=product_count, pagination=None)]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.pages_fetched == total_pages
    assert len(result.asins) == reported_product_count == result.product_count
    assert result.termination_reason == SCREENS_REASON_COMPLETED
    mock_logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_no_warning_on_normal_complete_walk():
    """A walk that ends because pagination legitimately ran out (not because
    a cap was hit) must not fire the truncation warning."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = {"sections": [_asin_section(
        [_row("B0PAGE1001"), _row("B0PAGE1002")], product_count=3, pagination="TOK0002",
    )]}
    page2 = {"sections": [_asin_section([_row("B0PAGE2001")], pagination=None)]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=[page1, page2])), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert len(result.asins) == 3 == result.product_count
    assert result.termination_reason == SCREENS_REASON_COMPLETED
    mock_logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_unclean_warning_fires_independent_of_shortfall():
    """The unclean-termination warning and the shortfall warning are two
    independent checks. A walk with no known product_count at all can still
    be unclean (e.g. a repeated token) and must still fire the unclean
    warning even though there is nothing to compute a shortfall against."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    echoing_page = {"sections": [_asin_section([_row("B0ECHO0002")], pagination="SAMETOKEN")]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=echoing_page)), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.product_count is None
    assert result.termination_reason == SCREENS_REASON_TOKEN_REPEATED
    mock_logger.warning.assert_called_once()
    warning_extra = mock_logger.warning.call_args.kwargs["extra"]
    assert warning_extra["author_asin"] == "B000TARGET"
    assert warning_extra["termination_reason"] == SCREENS_REASON_TOKEN_REPEATED
    assert "shortfall" not in warning_extra


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_plateau_on_real_shaped_pages_scores_unclean():
    """A fan-out walk over real captures that plateaus (Audible's own
    end-of-catalog signal — repeated page content, see _fanout_screen_pages)
    must score SCREENS_REASON_PLATEAU_TRUNCATED, not SCREENS_REASON_COMPLETED
    — plateauing means the walk stopped without upstream ever confirming
    nothing remains, distinct from a genuine null-continuation-token end (see
    SCREENS_REASON_PLATEAU_TRUNCATED's own docstring). This replaces the
    deleted test_fetch_author_books_by_screen_shortfall_warning_fires_on_clean_completion_of_real_shaped_pages,
    which pinned the old (pre-plateau-distinction) behavior where this same
    walk scored COMPLETED and fired a dedicated per-screen shortfall warning
    that has since been removed entirely (see
    test_fetch_author_books_by_screen_stops_at_page_cap_and_warns_once). The
    page-1 product_count here (1115, a real reported catalog size) is never
    approached because this capture only spans two real pages before the
    plateau — product_count over-claiming the real catalog is exactly why a
    per-screen shortfall comparison was noise; it is not asserted here."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = _load_screen_fixture("us_christie_page01")
    page2 = _load_screen_fixture("us_christie_page02")
    # Every page from 3 onward echoes page 2's exact content -- the plateau
    # -- since this author's own walk was never captured out to a genuine
    # null-token end.
    mock_get = AsyncMock(side_effect=_screen_page_router({1: page1, 2: page2}, default=page2))

    with patch("app.services.audible.authors.audible_get", new=mock_get), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await _fetch_author_books_by_screen("B000APENBC", "us")

    assert result.termination_reason == SCREENS_REASON_PLATEAU_TRUNCATED
    assert result.termination_reason not in SCREENS_CLEAN_REASONS
    assert result.product_count == 1115
    assert len(result.asins) < result.product_count
    mock_logger.warning.assert_called_once()
    warning_extra = mock_logger.warning.call_args.kwargs["extra"]
    assert warning_extra["author_asin"] == "B000APENBC"
    assert warning_extra["termination_reason"] == SCREENS_REASON_PLATEAU_TRUNCATED
    assert warning_extra["product_count"] == 1115


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_ambiguous_multi_section_admits_grid_shaped_rows():
    """When a page carries more than one StandardAsinRowList section and
    none reports product_count, no section can be trusted as the
    unconstrained grid — but grid-shaped rows (no authors key) among them
    must still be admitted rather than silently dropped, since admission is
    row-shape driven, not section driven."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    grid_shaped_row = _row("B0AMBIGRID")
    attributed_row = _row("B0AMBIATTR", authors=[{"asin": "b000target", "name": "Target"}])
    page = {"sections": [
        _asin_section([grid_shaped_row], pagination=None),
        _asin_section([attributed_row], pagination=None),
    ]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert "B0AMBIGRID" in result.asins
    assert "B0AMBIATTR" in result.asins


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_tracks_invalid_and_attribution_rejected_separately():
    """At the whole-walk result level, invalid_skipped and attribution_rejected
    must accumulate as two distinct counters, not one merged figure."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    grid_rows = [_row("TOO-SHORT")]
    teaser_rows = [_row("B0FOREIGN1", authors=[{"asin": "B000OTHERX", "name": "Other"}])]
    page = {"sections": [
        _asin_section(teaser_rows, pagination=None),
        _asin_section(grid_rows, pagination=None),
    ]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.invalid_skipped == 1
    assert result.attribution_rejected == 1
    assert result.asins == []


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_repeated_page_content_scores_plateau_not_clean():
    """Pins the current, deliberate behavior: a page whose ASIN content
    repeats an already-seen page's during the fan-out is NOT the same signal
    as upstream confirming nothing remains (see SCREENS_REASON_PLATEAU_TRUNCATED's
    own docstring — a real 404 past the true last page is the only genuine
    completion signal; a minted-token request past it plateaus instead,
    repeating the last real page forever). So this scores
    SCREENS_REASON_PLATEAU_TRUNCATED -- unclean -- not SCREENS_REASON_COMPLETED.
    This replaces the deleted
    test_fetch_author_books_by_screen_repeated_page_content_scores_clean_not_unclean,
    which pinned the exact opposite classification from before this
    distinction was introduced. Only the unclean-termination warning fires
    here now; the dedicated per-screen shortfall warning it used to fire
    alongside was removed entirely (see
    test_fetch_author_books_by_screen_stops_at_page_cap_and_warns_once)."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    echoing_page = {"sections": [_asin_section(
        [_row("B0BOTH0001")], product_count=1000, pagination="SAMETOKEN",
    )]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=echoing_page)), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.termination_reason == SCREENS_REASON_PLATEAU_TRUNCATED
    assert result.termination_reason not in SCREENS_CLEAN_REASONS
    assert result.asins == ["B0BOTH0001"]
    mock_logger.warning.assert_called_once_with(
        "Audible Author Books screen ended without confirmed completion",
        extra={
            "author_asin": "B000TARGET",
            "region": "us",
            "termination_reason": SCREENS_REASON_PLATEAU_TRUNCATED,
            "pages_fetched": 2,
            "asins_collected": 1,
            "product_count": 1000,
            "page_error": None,
            "sections_truncated": 0,
            "rows_truncated": 0,
        },
    )


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_real_capture_walk_unions_teaser_and_grid_across_pages():
    """Walks real trimmed captures spanning the start, an early continuation
    page, and (structurally) the end of a live pagination sequence and
    asserts the exact real ASINs collected — proving the parser against
    authentic response shapes, not hand-built approximations of them.

    product_count on page 1 (179) implies 9 pages at SCREENS_PAGE_SIZE, so
    the walk fans out pages 2-9 concurrently (see _fanout_screen_pages);
    page 2 is the real capture, and every page from 3 onward echoes the
    "structurally final" capture, so the plateau is confirmed one page
    after it first appears (page 4 repeats page 3's content) — pages
    fetched is 4 (page 1 sequential + pages 2, 3, 4 from the fan-out), not
    the 3 requests the old one-page-at-a-time walk made for this same
    fixture set. Scores SCREENS_REASON_PLATEAU_TRUNCATED, not
    SCREENS_REASON_COMPLETED: plateauing is the fan-out noticing content has
    stopped changing on its own, not upstream confirming the catalog's real
    end (see SCREENS_REASON_PLATEAU_TRUNCATED's own docstring)."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = _load_screen_fixture("us_sanderson_page01")
    page2 = _load_screen_fixture("us_sanderson_page02")
    # Structurally the end of a live pagination sequence (no product_count,
    # no continuation) -- served for page 3 and, unchanged, for every page
    # after it, so the fan-out's own plateau detection confirms completion.
    page_end = _load_screen_fixture("us_sanderson_page09")

    mock_get = AsyncMock(side_effect=_screen_page_router({1: page1, 2: page2}, default=page_end))
    with patch("app.services.audible.authors.audible_get", new=mock_get):
        result = await _fetch_author_books_by_screen("B001IGFHW6", "us")

    expected = {
        "B002V0QCYU", "1250759781", "B0718Z5K4C", "B00HWF0MHW", "B002VA9IKK", "B002V5GLQ4",
        "B019P7DVPE", "B005ZUI3OA", "B0B5M28HZK", "B0D18CT2VY", "B018UG5HJY",
        "B097RX4375", "B00TWHSWPW", "B0CJG4GJV5", "B0BR8FCS2V", "3837151905",
    }
    assert set(result.asins) == expected
    assert result.pages_fetched == 4
    assert result.product_count == 179
    assert result.termination_reason == SCREENS_REASON_PLATEAU_TRUNCATED
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_real_capture_walk_threads_au_region():
    """The same real-shaped walk against the au-region capture, proving
    region threading survives an actual non-US payload rather than only a
    US-shaped mock. product_count (152) implies 8 pages; page 08 here is
    structurally the real final page, served for page 2 onward, so the
    fan-out's plateau detection (page 3 repeats page 2's content) confirms
    completion well short of the implied 8 -- and scores
    SCREENS_REASON_PLATEAU_TRUNCATED, the same distinction the sibling
    us-region test above pins."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = _load_screen_fixture("au_sanderson_page01")
    page2 = _load_screen_fixture("au_sanderson_page08")
    mock_get = AsyncMock(side_effect=_screen_page_router({1: page1}, default=page2))

    with patch("app.services.audible.authors.audible_get", new=mock_get):
        result = await _fetch_author_books_by_screen("B001IGFHW6", "au")

    expected = {
        "B00FEZJ7SM", "B00FGHUJ44", "B00FGG100W", "B00FGA8FA6", "B08KRKV8NT",
        "B00THECIJG", "3837151905", "B0GV1KT9XC", "B09G3GS3PJ",
    }
    assert set(result.asins) == expected
    assert result.product_count == 152
    assert result.termination_reason == SCREENS_REASON_PLATEAU_TRUNCATED
    assert result.termination_reason not in SCREENS_CLEAN_REASONS
    for call in mock_get.await_args_list:
        assert call.args[0] == "au"


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_real_capture_walk_de_region_non_english():
    """The same real-shaped walk against a real de-region capture (Frank
    Schätzing, B001ITXLB2) — the only non-English fixture in this suite.
    Every prior real-capture fixture is us/au, both English and Latin-script,
    so the parser's structural assumptions (grid rows carrying no
    product_metadata.authors, a teaser row self-attributed to the author,
    the header/product_count path, the opaque pagination token) were
    unverified against an actual non-English payload. Page 2 of this same
    real capture also carries two ISBN-keyed rows (non-B-format ASINs,
    "8831001531" / "8831007165") alongside ordinary B-format ones, proving
    the parser doesn't assume every ASIN is B-prefixed."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = _load_screen_fixture("de_schaetzing_page01")
    page2 = _load_screen_fixture("de_schaetzing_page02")
    mock_get = AsyncMock(side_effect=[page1, page2])

    with patch("app.services.audible.authors.audible_get", new=mock_get):
        result = await _fetch_author_books_by_screen("B001ITXLB2", "de")

    expected = {
        "B00IOTKUDG", "B007NG27L4", "B004V09PMY", "B0D5D75Z37",
        "B0D268743S", "B01G8Y21BW", "8831001531", "8831007165",
        "B09Y9H3696", "B0FY6M69KD",
    }
    assert set(result.asins) == expected
    assert result.pages_fetched == 2
    assert result.product_count == 26
    assert result.termination_reason == SCREENS_REASON_COMPLETED
    for call in mock_get.await_args_list:
        assert call.args[0] == "de"


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_threads_region_for_all_regions():
    """Region is threaded through the screens URL for every supported
    region, not just US."""
    from app.services.audible.authors import _fetch_author_books_by_screen
    from app.services.audible.client import VALID_REGIONS

    empty_page = {"sections": []}
    for region in sorted(VALID_REGIONS):
        mock_get = AsyncMock(return_value=empty_page)
        with patch("app.services.audible.authors.audible_get", new=mock_get):
            await _fetch_author_books_by_screen("B000TARGET", region)
        assert mock_get.await_args.args[0] == region, f"region not threaded for {region}"


# ============================================================
# SCREEN FAN-OUT MACHINERY (_mint_screen_token, _fanout_screen_pages)
# ============================================================

def test_mint_screen_token_exact_shape():
    """Pins the exact minted-token shape proven live (see
    _mint_screen_token's own docstring): base64 of a compact-JSON object
    with page_num as a STRING and slot as the literal "center-10" -- an
    omitted slot 400s, a wrong value 404s, and neither is derivable from
    the code without the referenced live investigation, so this is pinned
    as a literal rather than inferred from behavior."""
    from app.services.audible.authors import _mint_screen_token, _SCREENS_TOKEN_PAGE_LOAD_ID

    token = _mint_screen_token(5)
    raw = base64.b64decode(token)

    assert b" " not in raw  # no whitespace, matching what Audible's own client sends
    payload = json.loads(raw)
    assert payload == {
        "scheduling_info": {
            "page_load_id": _SCREENS_TOKEN_PAGE_LOAD_ID,
            "slot": "center-10",
        },
        "pagination_info": {"page_num": "5"},
    }
    assert isinstance(payload["pagination_info"]["page_num"], str)


def test_mint_screen_token_page_num_stringified_not_left_as_int():
    """A regression the shape check above wouldn't catch on its own: JSON
    itself round-trips an int-typed page_num back as an int on decode, so
    this asserts against the pre-decode literal, not just the decoded
    type."""
    from app.services.audible.authors import _mint_screen_token

    raw = base64.b64decode(_mint_screen_token(12)).decode("ascii")
    assert '"page_num":"12"' in raw
    assert '"page_num":12' not in raw


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_fanout_reassembles_in_page_order_not_completion_order():
    """asyncio.gather preserves input order regardless of which request
    finishes first (see _fanout_screen_pages) -- pin that end to end: page 3
    is made to resolve before page 2, and the final ASIN order must still
    be page 1, then page 2, then page 3, not completion order."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = {"sections": [_asin_section([_row("B0ORDER001")], product_count=45, pagination=None)]}
    pages_by_num = {
        2: {"sections": [_asin_section([_row("B0ORDER002")], pagination=None)]},
        3: {"sections": [_asin_section([_row("B0ORDER003")], pagination=None)]},
    }
    delays = {2: 0.05, 3: 0.0}  # page 3 answers first, page 2 answers last

    async def _get(region, path, params, extra_headers=None):
        token = params.get("pageSectionContinuationToken")
        if not token:
            return page1
        page_num = _decode_screen_token_page_num(token)
        await asyncio.sleep(delays.get(page_num, 0))
        return pages_by_num[page_num]

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=_get)):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.asins == ["B0ORDER001", "B0ORDER002", "B0ORDER003"]
    assert result.termination_reason == SCREENS_REASON_COMPLETED


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_fanout_plateau_stops_at_first_confirming_repeat():
    """Requesting a page past a real author's last one doesn't error or come
    back empty -- probed live, it PLATEAUS, returning the last real page's
    content again, byte-identical (see _fanout_screen_pages). The walk must
    stop at the first page whose content repeats an already-seen page's
    signature, keep everything collected up to and including that
    confirming page, and use nothing from pages dispatched in the same
    batch beyond it -- even though every page in the batch was already
    fetched over the wire."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    # product_count implies 6 pages (ceil(101 / SCREENS_PAGE_SIZE)), so the
    # whole fan-out (pages 2-6) fits in a single batch under
    # SCREENS_FANOUT_CONCURRENCY -- pages 5 and 6 are dispatched but must
    # never be folded in.
    product_count = 101
    page1 = {"sections": [_asin_section([_row("B0PLAT0001")], product_count=product_count, pagination=None)]}
    real_pages = {
        2: {"sections": [_asin_section([_row("B0PLAT0002")], pagination=None)]},
        3: {"sections": [_asin_section([_row("B0PLAT0003")], pagination=None)]},
    }
    plateau_page = real_pages[3]  # page 4 onward echoes page 3's exact content

    async def _get(region, path, params, extra_headers=None):
        token = params.get("pageSectionContinuationToken")
        if not token:
            return page1
        page_num = _decode_screen_token_page_num(token)
        return real_pages.get(page_num, plateau_page)

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=_get)):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.asins == ["B0PLAT0001", "B0PLAT0002", "B0PLAT0003"]
    # page 1 (sequential) + page 2, page 3, page 4 (page 4 is the repeat
    # that confirms the plateau and ends the walk).
    assert result.pages_fetched == 4
    # Confirming a plateau is not the same signal as upstream's own
    # confirmed end (a null continuation token, or a real 404 past the last
    # page) -- see SCREENS_REASON_PLATEAU_TRUNCATED's own docstring -- so
    # this scores unclean rather than SCREENS_REASON_COMPLETED even though
    # the walk correctly stops here rather than looping on repeated content.
    assert result.termination_reason == SCREENS_REASON_PLATEAU_TRUNCATED
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_total_pages_uses_ceiling_not_floor_division():
    """total_pages must be ceil(product_count / SCREENS_PAGE_SIZE), not a
    truncating floor division -- product_count=21 needs 2 pages at a fixed
    20-row page size. Page 1 deliberately carries no continuation token of
    its own (unlike most of this suite's real captures, which happen to
    carry one anyway and would silently reach page 2 via the sequential
    fallback even if total_pages were computed wrong), so only a correctly
    computed total_pages > 1 can reach page 2 here at all."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = {"sections": [_asin_section([_row("B0CEIL0001")], product_count=21, pagination=None)]}
    page2 = {"sections": [_asin_section([_row("B0CEIL0002")], pagination=None)]}

    mock_get = AsyncMock(side_effect=_screen_page_router({1: page1, 2: page2}))
    with patch("app.services.audible.authors.audible_get", new=mock_get):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.asins == ["B0CEIL0001", "B0CEIL0002"]
    assert result.pages_fetched == 2
    assert result.termination_reason == SCREENS_REASON_COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("unusable_product_count", [None, 0, -5, "179", 3.5])
async def test_fetch_author_books_by_screen_sequential_fallback_when_product_count_unusable(
    unusable_product_count,
):
    """Any product_count that isn't a usable positive int (missing, zero,
    negative, or non-int) must keep the walk on the original sequential,
    token-chasing path -- the fan-out is never attempted. Patching
    _fanout_screen_pages and asserting it was never called proves this
    directly rather than inferring it from request counts."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    page1 = {"sections": [_asin_section(
        [_row("B0SEQ00001")], product_count=unusable_product_count, pagination="TOK0002",
    )]}
    page2 = {"sections": [_asin_section([_row("B0SEQ00002")], pagination=None)]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=[page1, page2])), \
         patch("app.services.audible.authors._fanout_screen_pages", new=AsyncMock()) as mock_fanout:
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    mock_fanout.assert_not_called()
    assert result.asins == ["B0SEQ00001", "B0SEQ00002"]
    assert result.pages_fetched == 2
    assert result.termination_reason == SCREENS_REASON_COMPLETED


# ============================================================
# CATALOG MULTI-SORT WALK (_fetch_author_books_by_catalog) --
# ceiling_saturated boundary
# ============================================================

@pytest.mark.asyncio
async def test_fetch_author_books_by_catalog_ceiling_saturated_false_at_exact_boundary():
    """ceiling_saturated is False when total_results sits exactly at
    len(_CATALOG_SORTS) * CATALOG_RESULT_CEILING -- the walk is structurally
    capable of reaching that many, so this is not yet the saturated case;
    only strictly exceeding it is. Deliberately expressed via the constants
    rather than their current product literal, since CATALOG_RESULT_CEILING
    is a measured, live-probed value that can be corrected independently of
    this boundary rule."""
    from app.services.audible.authors import (
        _fetch_author_books_by_catalog, _CATALOG_SORTS, CATALOG_RESULT_CEILING,
    )

    boundary_total = len(_CATALOG_SORTS) * CATALOG_RESULT_CEILING

    async def _get(region, path, params):
        return {"total_results": boundary_total, "products": []}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=_get)):
        result = await _fetch_author_books_by_catalog("B000AUTHOR", "Some Author", "us")

    assert result.total_results == boundary_total
    assert result.ceiling_saturated is False


def _catalog_asin_match_products(prefix, n):
    """n catalog products, each carrying the requested author's own ASIN so
    _classify_catalog_product accepts every one of them as an authoritative
    asin_match -- used to drive _fetch_author_books_by_catalog's plateau
    detection directly rather than through the higher-level get_author_books
    mocks used elsewhere in this file."""
    return [
        {"asin": f"B0{prefix}{i:04d}", "authors": [{"asin": "B000AUTHOR"}]}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_fetch_author_books_by_catalog_ceiling_saturated_true_from_observed_plateau():
    """ceiling_saturated is measured from what the walk actually observed on
    the wire -- a sort's own pages plateauing (re-serving an earlier page's
    exact content) while the union it fed still falls short of upstream's
    own total_results claim -- never from CATALOG_RESULT_CEILING arithmetic
    alone (see _CatalogBooksResult's own docstring). Mocking every page as
    empty products, as this test used to, can never plateau -- there is
    nothing to repeat -- so it drives a walk whose -ReleaseDate pages
    genuinely plateau instead: page 0 and page 1 each carry new content,
    and every page after that re-serves page 1's exact content, matching
    the live Conan Doyle/Christie behaviour the docstring describes."""
    from app.services.audible.authors import _fetch_author_books_by_catalog, _CATALOG_SORTS

    plateau_products = _catalog_asin_match_products("PLATEAU", 50)

    async def _get(region, path, params):
        sort = params["products_sort_by"]
        page = params["page"]
        if sort != _CATALOG_SORTS[0]:
            # total_results=500 keeps sorts_needed at 1 -- only page 0 of
            # every other sort is ever fetched (wave 2 fetches it
            # regardless of whether that sort ends up needed).
            return {"total_results": 500, "products": _catalog_asin_match_products(f"OTHER{_CATALOG_SORTS.index(sort)}", 10)}
        if page == 0:
            return {"total_results": 500, "products": _catalog_asin_match_products("PAGE0", 50)}
        # page 1 is new content; every page after it re-serves page 1's
        # exact signature -- the plateau.
        return {"products": plateau_products}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=_get)):
        result = await _fetch_author_books_by_catalog("B000AUTHOR", "Some Author", "us")

    assert result.total_results == 500
    assert result.ceiling_saturated is True
    assert len(result.asins) < 500  # the union genuinely fell short of the claim


@pytest.mark.asyncio
async def test_fetch_author_books_by_catalog_ceiling_saturated_false_when_walk_reaches_total_without_plateauing():
    """Complement to the observed-plateau case above: a walk whose pages
    never repeat, reaching upstream's total_results through genuinely new
    content on every page, must not be reported as ceiling_saturated --
    reaching the claimed total cleanly is a complete walk, not a saturated
    one, regardless of how many pages it took."""
    from app.services.audible.authors import _fetch_author_books_by_catalog, _CATALOG_SORTS

    async def _get(region, path, params):
        sort = params["products_sort_by"]
        page = params["page"]
        if sort != _CATALOG_SORTS[0]:
            return {"total_results": 100, "products": []}
        if page == 0:
            return {"total_results": 100, "products": _catalog_asin_match_products("PAGE0", 50)}
        # page 1: genuinely new content, never repeats page 0's signature.
        return {"products": _catalog_asin_match_products("PAGE1", 50)}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=_get)):
        result = await _fetch_author_books_by_catalog("B000AUTHOR", "Some Author", "us")

    assert result.total_results == 100
    assert len(result.asins) == 100
    assert result.ceiling_saturated is False


@pytest.mark.asyncio
async def test_fetch_author_books_by_catalog_ceiling_saturated_false_when_no_total_results():
    """No total_results reported anywhere means nothing is known to compare
    against the ceiling -- ceiling_saturated must default False, never be
    inferred as saturated purely from the claim's absence."""
    from app.services.audible.authors import _fetch_author_books_by_catalog

    async def _get(region, path, params):
        return {"products": []}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=_get)):
        result = await _fetch_author_books_by_catalog("B000AUTHOR", "Some Author", "us")

    assert result.total_results is None
    assert result.ceiling_saturated is False


# ============================================================
# GET AUTHOR BOOKS — four-source union (screens, catalog, DB, cache)
# ============================================================

def _screen_result(
    asins,
    pages_fetched=1,
    product_count=None,
    invalid_skipped=0,
    attribution_rejected=0,
    termination_reason=SCREENS_REASON_COMPLETED,
    page_error=None,
):
    return _ScreenBooksResult(
        asins=asins,
        pages_fetched=pages_fetched,
        product_count=product_count,
        invalid_skipped=invalid_skipped,
        attribution_rejected=attribution_rejected,
        termination_reason=termination_reason,
        page_error=page_error,
    )


def _catalog_result(
    asins,
    pages_fetched=1,
    total_results=None,
    sorts_used=1,
    asin_match_count=None,
    asin_reject_count=0,
    name_match_count=0,
    name_reject_count=0,
    sort_errors=None,
    truncated_by_deadline=False,
    ceiling_saturated=False,
):
    asins = list(asins)
    return _CatalogBooksResult(
        asins=asins,
        pages_fetched=pages_fetched,
        total_results=total_results,
        sorts_used=sorts_used,
        asin_match_count=len(asins) if asin_match_count is None else asin_match_count,
        asin_reject_count=asin_reject_count,
        name_match_count=name_match_count,
        name_reject_count=name_reject_count,
        sort_errors=list(sort_errors) if sort_errors else [],
        truncated_by_deadline=truncated_by_deadline,
        ceiling_saturated=ceiling_saturated,
    )


@pytest.mark.asyncio
async def test_get_author_books_catalog_search_still_runs_when_screens_alone_would_suffice():
    """The catalog half is unconditional once a name resolves — it is not
    skipped just because the screens walk on its own already returns a
    usable result."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)
    catalog_result = _catalog_result(["B0CATALOG01"], total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)) as mock_catalog, \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0CATALOG01", "B0SCREEN001"]
    mock_catalog.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_author_books_catalog_results_survive_when_screens_raises():
    """A screens-path exception must not prevent the independent catalog
    results from reaching the union -- each of the four sources fails in
    isolation (see get_author_books' own docstring)."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    catalog_result = _catalog_result(["B0CATALOG02"], total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(side_effect=RuntimeError("screen boom"))), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Frank Herbert")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0CATALOG02"]


@pytest.mark.asyncio
async def test_get_author_books_screen_results_survive_when_catalog_raises():
    """The mirror image of the previous test: a catalog-path exception must
    not prevent the independent screens results from reaching the union."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN003"], product_count=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Frank Herbert")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(side_effect=RuntimeError("catalog boom"))), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0SCREEN003"]


@pytest.mark.asyncio
async def test_get_author_books_catalog_results_survive_when_screens_empty():
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    empty_screen = _screen_result([])
    catalog_result = _catalog_result(["B0CATALOG03"], total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=empty_screen)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Frank Herbert")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0CATALOG03"]


@pytest.mark.asyncio
async def test_get_author_books_falls_back_to_db_when_catalog_and_screens_empty():
    """When both live sources come back genuinely empty, the DB backstop
    (unioned unconditionally on every request, not just degraded ones) is
    still what stands between a real result and NotFoundException."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    empty_screen = _screen_result([], pages_fetched=0)
    empty_catalog = _catalog_result([], total_results=0)
    db_asins = ["B0DBFALLBK1"]

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=empty_screen)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Frank Herbert")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=empty_catalog)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=db_asins)), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0DBFALLBK1"]


@pytest.mark.asyncio
async def test_get_author_books_all_legitimately_empty_raises_not_found_without_swallowing():
    """When screens, catalog, and DB all legitimately come up empty, the
    NotFoundException raised for it must propagate as-is — not get swallowed
    into the generic 'Audible unavailable' message."""
    from app.services.audible.authors import get_author_books
    from app.core.exceptions import NotFoundException

    mock_session = AsyncMock()
    empty_screen = _screen_result([], pages_fetched=0)
    empty_catalog = _catalog_result([], total_results=0)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=empty_screen)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Frank Herbert")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=empty_catalog)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None):
        with pytest.raises(NotFoundException) as exc:
            await get_author_books("B000AUTHOR", "us", mock_session)

    assert "No books found" in str(exc.value)


@pytest.mark.asyncio
async def test_get_author_books_transient_catalog_failure_falls_to_db_then_cache():
    """A transient failure in the catalog wave (not a clean empty result)
    still falls through DB then cache before giving up."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    empty_screen = _screen_result([], pages_fetched=0)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=empty_screen)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Frank Herbert")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(side_effect=RuntimeError("Audible down"))), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=["B0CACHED001"]):
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0CACHED001"]


@pytest.mark.asyncio
async def test_get_author_books_transient_failure_all_empty_raises_not_found():
    from app.services.audible.authors import get_author_books
    from app.core.exceptions import NotFoundException

    mock_session = AsyncMock()
    empty_screen = _screen_result([], pages_fetched=0)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=empty_screen)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Frank Herbert")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(side_effect=RuntimeError("Audible down"))), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None):
        with pytest.raises(NotFoundException):
            await get_author_books("B000AUTHOR", "us", mock_session)


# ============================================================
# GET AUTHOR BOOKS — union floor (never smaller than catalog alone)
# ============================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_asins,screen_asins", [
    (["B0NAME00001", "B0NAME00002", "B0NAME00003"], []),
    ([], ["B0SCRN00001", "B0SCRN00002"]),
    (["B0SHARED001", "B0NAME00002"], ["B0SHARED001", "B0SCRN00001"]),
    (["B0SAME00001"], ["B0SAME00001"]),
    (["B0NAME00001", "B0NAME00002", "B0NAME00003", "B0NAME00004"], ["B0SCRN00001"]),
    (["B0NAME00001"], ["B0SCRN00001", "B0SCRN00002", "B0SCRN00003", "B0SCRN00004"]),
])
async def test_get_author_books_union_never_smaller_than_catalog_alone(catalog_asins, screen_asins):
    """The union of the sources can never fall below the catalog result by
    itself, across a spread of overlap shapes."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(list(screen_asins))
    catalog_result = _catalog_result(list(catalog_asins), total_results=len(catalog_asins))

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert len(result) >= len(catalog_asins)
    assert set(catalog_asins).issubset(set(result))


@pytest.mark.asyncio
async def test_get_author_books_union_floor_holds_when_screens_path_raises():
    """The floor holds even in the degenerate case where the screens path
    fails outright — the union then equals the catalog result exactly,
    never less."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    catalog_asins = ["B0NAME00001", "B0NAME00002"]
    catalog_result = _catalog_result(catalog_asins, total_results=2)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert len(result) >= len(catalog_asins)
    assert result == catalog_asins


# ============================================================
# GET AUTHOR BOOKS — union ordering (consumer-visible contract)
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_union_orders_catalog_first_then_screens_only_extras():
    """catalog ASINs come first in their own order (already -ReleaseDate-
    first internally); screens-only extras are appended after, in screens
    order. ASINs shared by both keep their catalog position and are not
    duplicated."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    catalog_asins = ["B0NAME00001", "B0SHARED002", "B0NAME00003"]
    screen_asins = ["B0SHARED002", "B0SCRNONLY1", "B0NAME00001", "B0SCRNONLY2"]
    screen_result = _screen_result(screen_asins)
    catalog_result = _catalog_result(catalog_asins, total_results=len(catalog_asins))

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == [
        "B0NAME00001", "B0SHARED002", "B0NAME00003", "B0SCRNONLY1", "B0SCRNONLY2",
    ]


@pytest.mark.asyncio
async def test_get_author_books_union_orders_db_only_extras_last():
    """DB-only ASINs the two live sources didn't surface this run are
    appended after both catalog and screens, never inserted in the middle
    -- the DB is the last-resort backstop, unioned in at the tail on every
    request (see get_author_books' own docstring)."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    catalog_asins = ["B0CATALOG01"]
    screen_asins = ["B0SCREEN001"]
    db_asins = ["B0CATALOG01", "B0DBTAIL0001", "B0SCREEN001", "B0DBTAIL0002"]
    screen_result = _screen_result(screen_asins, product_count=1)
    catalog_result = _catalog_result(catalog_asins, total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=db_asins)), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0CATALOG01", "B0SCREEN001", "B0DBTAIL0001", "B0DBTAIL0002"]


@pytest.mark.asyncio
async def test_get_author_books_catalog_asin_keeps_its_index_when_screens_adds_titles():
    """An ASIN a caller already sees today (from the catalog walk) keeps
    its index once screens starts contributing extra titles — only new
    titles append, nothing already-returned reshuffles."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    catalog_asins = ["B0NAME00001", "B0NAME00002", "B0NAME00003"]
    catalog_result = _catalog_result(catalog_asins, total_results=3)

    async def _run(screen_asins):
        screen_result = _screen_result(screen_asins)
        with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
             patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
             patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
             patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
             patch("app.services.audible.authors.cache.get", return_value=None), \
             patch("app.services.audible.authors.persist_author_books_cache_background"):
            return await get_author_books("B000AUTHOR", "us", mock_session)

    before = await _run([])
    after = await _run(["B0SCRNONLY1", "B0SCRNONLY2"])

    for asin in catalog_asins:
        assert before.index(asin) == after.index(asin)


# ============================================================
# GET AUTHOR BOOKS — no cache write on a degraded result
# (less-data-never-accepted: the union is only ever persisted as
# authoritative when screens_clean and catalog_clean both hold)
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_persists_cache_when_screens_and_catalog_both_clean():
    """Positive control: when the screens walk is clean and the catalog
    walk ran without error, the union IS persisted — the negative cases
    below only mean something in contrast to this one."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)
    catalog_result = _catalog_result(["B0NAME00001"], total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0NAME00001", "B0SCREEN001"]
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_get_author_books_persists_cache_when_name_never_resolved():
    """catalog_clean is trivially True when no author name resolved at all
    (author_name is None) -- nothing to search the catalog with is not a
    degraded catalog outcome, the same distinction the old name_clean gate
    drew. A clean screens walk alone is enough to persist here, and the
    catalog wave must never even be scheduled."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value=None)), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock()) as mock_catalog, \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0SCREEN001"]
    mock_catalog.assert_not_awaited()
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_get_author_books_no_cache_write_on_unclean_screens_termination():
    """An unclean screens termination must not be written back as if it
    were the authoritative list — but the caller still gets the degraded
    union, not an error."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(
        ["B0SCREEN001"], termination_reason=SCREENS_REASON_TOKEN_REPEATED,
    )
    catalog_result = _catalog_result(["B0NAME00001"], total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0NAME00001", "B0SCREEN001"]
    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_get_author_books_no_cache_write_on_grid_not_found():
    """A walk reclassified from completed to grid_not_found is unclean and
    must not be persisted as the authoritative list."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(
        ["B0SCREEN001"], termination_reason=SCREENS_REASON_GRID_NOT_FOUND,
    )
    catalog_result = _catalog_result(["B0NAME00001"], total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0NAME00001", "B0SCREEN001"]
    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_get_author_books_no_cache_write_on_truncated_termination():
    """A walk demoted from completed to sections_or_rows_truncated (a
    SCREENS_MAX_SECTIONS or SCREENS_MAX_ROWS_PER_PAGE cap trimmed data mid-
    walk) is unclean and must not be persisted as the authoritative list --
    some upstream content was never even looked at, so it's not a confirmed-
    complete read regardless of how pagination itself ended."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(
        ["B0SCREEN001"], termination_reason=SCREENS_REASON_TRUNCATED,
    )
    catalog_result = _catalog_result(["B0NAME00001"], total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0NAME00001", "B0SCREEN001"]
    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_get_author_books_no_cache_write_on_plateau_truncated_termination():
    """A screens walk that plateaus (SCREENS_REASON_PLATEAU_TRUNCATED) is
    unclean the same way a token repeat or a page cap is -- upstream never
    confirmed the walk saw everything, so it must not be persisted as the
    authoritative list either."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(
        ["B0SCREEN001"], termination_reason=SCREENS_REASON_PLATEAU_TRUNCATED,
    )
    catalog_result = _catalog_result(["B0NAME00001"], total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0NAME00001", "B0SCREEN001"]
    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_get_author_books_persists_cache_despite_known_shortfall():
    """A clean termination with a shortfall against product_count IS still
    persisted — product_count no longer vetoes the write anywhere in this
    path. Audible's own reported product_count routinely over-counts
    (measured live for prolific authors like Sanderson and Christie), so
    vetoing on it made the write essentially never fire for exactly the
    prolific authors this union exists to serve. The gate keys on clean
    termination (screens_clean and catalog_clean) only; product_count still
    drives the union-level shortfall WARNING (see the shortfall-warning
    tests below) but no longer blocks persistence on its own."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=10)
    catalog_result = _catalog_result(["B0NAME00001"], total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0NAME00001", "B0SCREEN001"]
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_get_author_books_no_cache_write_when_catalog_errored():
    """Even a clean, no-shortfall screens walk must not be persisted if the
    catalog wave that seeds the front of the list errored outright — the
    union is still missing whatever the catalog would have contributed."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(side_effect=RuntimeError("catalog boom"))), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0SCREEN001"]
    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_get_author_books_no_cache_write_when_catalog_has_sort_errors():
    """catalog_clean requires an empty sort_errors list, not merely the
    absence of a top-level catalog_error -- a catalog walk that ran but had
    one sort fail partway (sort_errors non-empty) must still block the
    write, even though _fetch_author_books_by_catalog itself didn't raise."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)
    catalog_result = _catalog_result(
        ["B0NAME00001"], total_results=1,
        sort_errors=["-Title page 0: RuntimeError: upstream 502"],
    )

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0NAME00001", "B0SCREEN001"]
    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_get_author_books_no_cache_write_when_catalog_truncated_by_deadline():
    """A catalog walk cut short by the shared deadline (truncated_by_deadline
    True) is degraded the same way an error is, even with no sort_errors and
    no exception raised."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)
    catalog_result = _catalog_result(
        ["B0NAME00001"], total_results=1, truncated_by_deadline=True,
    )

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0NAME00001", "B0SCREEN001"]
    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_get_author_books_no_cache_write_when_catalog_ceiling_saturated():
    """catalog_ceiling_saturated (upstream's own total_results exceeding
    len(_CATALOG_SORTS) * CATALOG_RESULT_CEILING) blocks the cache write the
    same way an outright catalog error does -- but unlike an error, this is
    known arithmetically before a single further page is fetched, not
    inferred from the union's eventual size. The full union is still
    returned to the caller; only the cache write is suppressed. The dedicated
    saturation warning fires with the exact ceiling numbers, and the
    generic ratio-based shortfall warning does NOT also fire for the same
    condition -- the two would otherwise double up on an identical cause.
    The threshold is expressed via the constants, not their current product
    literal, since CATALOG_RESULT_CEILING is a measured, live-probed value
    independent of this boundary rule."""
    from app.services.audible.authors import (
        get_author_books, _CATALOG_SORTS, CATALOG_RESULT_CEILING,
    )

    mock_session = AsyncMock()
    saturated_total = len(_CATALOG_SORTS) * CATALOG_RESULT_CEILING + 1
    catalog_asins = [f"B0CATALOG{i:04d}" for i in range(600)]
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)
    catalog_result = _catalog_result(
        catalog_asins, total_results=saturated_total, ceiling_saturated=True,
    )

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist, \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    # The union is still returned in full -- suppressing the cache write
    # must not have become a truncated or empty response.
    assert result == catalog_asins + ["B0SCREEN001"]
    assert len(result) == 601
    mock_persist.assert_not_called()

    saturation_calls = [
        c for c in mock_logger.warning.call_args_list
        if c.args[0] == "Audible Author Books catalog saturated its sort ceiling, cache write suppressed"
    ]
    assert len(saturation_calls) == 1
    extra = saturation_calls[0].kwargs["extra"]
    assert extra["author_asin"] == "B000AUTHOR"
    assert extra["region"] == "us"
    assert extra["catalog_total_results"] == saturated_total
    assert extra["catalog_max_fetchable"] == len(_CATALOG_SORTS) * CATALOG_RESULT_CEILING
    assert extra["author_book_num"] == 601

    shortfall_calls = [
        c for c in mock_logger.warning.call_args_list
        if c.args[0] == "Audible Author Books union fell short of the claimed total"
    ]
    assert shortfall_calls == []


@pytest.mark.asyncio
async def test_get_author_books_appends_db_known_asins_at_tail_when_catalog_degraded_and_screens_present():
    """When the catalog half is degraded (errored) and screens returned
    something, DB-known ASINs the union is still missing are appended after
    the catalog+screens prefix rather than serving screens-only — the
    existing prefix order is undisturbed, only extended, and this union is
    never treated as authoritative for caching."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001", "B0SCREEN002"], product_count=2)
    db_asins = [
        "B0SCREEN001",   # already present -- must not duplicate
        "B0DBTAIL0001",  # DB-only -- must append at the tail
    ]

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(side_effect=RuntimeError("catalog boom"))), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=db_asins)), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0SCREEN001", "B0SCREEN002", "B0DBTAIL0001"]
    mock_persist.assert_not_called()


# ============================================================
# GET AUTHOR BOOKS — the DB union is unconditional (fires on every
# request, not only on a degraded path)
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_db_union_fires_even_on_a_fully_clean_non_degraded_request():
    """The DB backstop is unioned in on every request now, not only when a
    live source came back short or empty (see get_author_books' own
    docstring) -- this is the less-data-never-accepted invariant applied at
    the response level: once Libex has seen a book for an author, it can
    never vanish just because a live source happened not to resurface it
    this run, even when nothing about this run is degraded."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)
    catalog_result = _catalog_result(["B0CATALOG01"], total_results=1)
    db_asins = ["B0CATALOG01", "B0SCREEN001", "B0DBONLY0001"]

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=db_asins)) as mock_db, \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0CATALOG01", "B0SCREEN001", "B0DBONLY0001"]
    mock_db.assert_awaited_once()
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_get_author_books_db_union_backstop_when_catalog_degraded_and_screens_truly_empty():
    """The DB union still fires and fills the gap when the catalog half is
    degraded (carrying sort_errors, not a confirmed clean walk) and the
    screens half contributed nothing at all."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    empty_screen = _screen_result([])
    catalog_asins = ["B0NAME00001", "B0NAME00002"]
    catalog_result = _catalog_result(
        catalog_asins, total_results=5, sort_errors=["-Title page 1: RuntimeError: boom"],
    )
    db_asins = ["B0NAME00001", "B0NAME00002", "B0DBEXTRA01", "B0DBEXTRA02", "B0DBEXTRA03"]

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=empty_screen)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=db_asins)) as mock_db, \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0NAME00001", "B0NAME00002", "B0DBEXTRA01", "B0DBEXTRA02", "B0DBEXTRA03"]
    mock_db.assert_awaited_once()
    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_get_author_books_db_union_backstop_when_screens_degraded_and_catalog_partial():
    """Both halves degraded and non-empty -- screens page-capped (unclean,
    but contributed titles) alongside a catalog walk carrying sort_errors.
    The DB union still fires and fills the gap regardless."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    degraded_screen = _screen_result(
        ["B0SCREEN001", "B0SCREEN002"], termination_reason=SCREENS_REASON_PAGE_CAP,
    )
    catalog_result = _catalog_result(
        ["B0NAME00001"], total_results=5, sort_errors=["ReleaseDate page 2: RuntimeError: boom"],
    )
    db_asins = ["B0SCREEN001", "B0DBTAIL0001"]

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=degraded_screen)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=db_asins)) as mock_db, \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0NAME00001", "B0SCREEN001", "B0SCREEN002", "B0DBTAIL0001"]
    mock_db.assert_awaited_once()
    mock_persist.assert_not_called()


# ============================================================
# GET AUTHOR BOOKS — union-level completeness/shortfall warning
# (claimed_total from catalog total_results, falling back to screens'
# product_count; fires only past a 10% shortfall, never on overshoot)
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_shortfall_warning_fires_past_ten_percent_gap():
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_asins = [f"B0SCREEN{i:04d}" for i in range(500)]
    screen_result = _screen_result(screen_asins, product_count=500)
    catalog_result = _catalog_result([f"B0CATALOG{i:04d}" for i in range(300)], total_results=1000)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    # 300 catalog + 500 screens, disjoint by construction == 800 total,
    # against a claimed 1000 -- a 20% shortfall, comfortably past the 10%
    # threshold.
    assert len(result) == 800
    shortfall_calls = [
        c for c in mock_logger.warning.call_args_list
        if c.args[0] == "Audible Author Books union fell short of the claimed total"
    ]
    assert len(shortfall_calls) == 1
    extra = shortfall_calls[0].kwargs["extra"]
    assert extra["author_asin"] == "B000AUTHOR"
    assert extra["author_book_num"] == 800
    assert extra["claimed_total"] == 1000
    assert extra["shortfall"] == 200
    assert extra["shortfall_ratio"] == 0.2
    assert extra["screens_plateau_truncated"] is False


@pytest.mark.asyncio
async def test_get_author_books_shortfall_warning_does_not_fire_on_a_small_healthy_gap():
    """Christie-shaped case, per get_author_books' own shortfall comment: a
    real, healthy result sits a handful short of the claimed total -- a
    ~0.4% gap that must not warn, or the signal is noise from day one."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    catalog_asins = [f"B0CATALOG{i:04d}" for i in range(1133)]
    catalog_result = _catalog_result(catalog_asins, total_results=1138)
    screen_result = _screen_result([], product_count=1138)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert len(result) == 1133
    shortfall_calls = [
        c for c in mock_logger.warning.call_args_list
        if c.args[0] == "Audible Author Books union fell short of the claimed total"
    ]
    assert shortfall_calls == []


@pytest.mark.asyncio
async def test_get_author_books_shortfall_warning_does_not_fire_on_overshoot():
    """The union can legitimately exceed the claimed total -- screens and
    the DB backstop both surface ASINs the catalog sorts never do -- so an
    overshoot must never be treated as a shortfall."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    catalog_result = _catalog_result(["B0CATALOG01"], total_results=1)
    screen_result = _screen_result(
        ["B0SCREEN001", "B0SCREEN002", "B0SCREEN003"], product_count=1,
    )

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert len(result) == 4
    shortfall_calls = [
        c for c in mock_logger.warning.call_args_list
        if c.args[0] == "Audible Author Books union fell short of the claimed total"
    ]
    assert shortfall_calls == []


@pytest.mark.asyncio
async def test_get_author_books_shortfall_band_below_ceiling_still_writes_cache_and_warns():
    """PINS CURRENT BEHAVIOR, NOT DESIRED BEHAVIOR. len(_CATALOG_SORTS) *
    CATALOG_RESULT_CEILING is an optimistic upper bound: the three sorts walk
    the same catalog in different orders and overlap heavily, so the
    genuinely reachable union is lower than that product in practice. For a
    total_results claim that requires every sort (i.e. anywhere past
    (len(_CATALOG_SORTS) - 1) * CATALOG_RESULT_CEILING) but does not exceed
    the full product, ceiling_saturated stays False even though the walk may
    fall well short of the claim -- so this case still fires the generic
    ratio shortfall warning AND still writes to cache, unlike the
    ceiling-exceeded case one test above, which suppresses the write. This
    test pins the worst case of that band -- total_results at the exact
    ceiling, the closest a claim can get to saturation while still reading
    False. This is characterised here as the boundary this slice leaves in
    place; whether it should also suppress the write is a separate
    question, out of scope for this test."""
    from app.services.audible.authors import (
        get_author_books, _CATALOG_SORTS, CATALOG_RESULT_CEILING,
    )

    mock_session = AsyncMock()
    band_total = len(_CATALOG_SORTS) * CATALOG_RESULT_CEILING  # at the ceiling, not past it
    catalog_asins = [f"B0CATALOG{i:04d}" for i in range(600)]
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)
    catalog_result = _catalog_result(
        catalog_asins, total_results=band_total, ceiling_saturated=False,
    )

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist, \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert len(result) == 601
    # Currently writes despite a real shortfall in this band.
    mock_persist.assert_called_once()

    shortfall_calls = [
        c for c in mock_logger.warning.call_args_list
        if c.args[0] == "Audible Author Books union fell short of the claimed total"
    ]
    assert len(shortfall_calls) == 1
    assert shortfall_calls[0].kwargs["extra"]["claimed_total"] == band_total
    assert shortfall_calls[0].kwargs["extra"]["shortfall"] == band_total - 601

    saturation_calls = [
        c for c in mock_logger.warning.call_args_list
        if c.args[0] == "Audible Author Books catalog saturated its sort ceiling, cache write suppressed"
    ]
    assert saturation_calls == []


@pytest.mark.asyncio
async def test_get_author_books_shortfall_uses_screen_product_count_when_catalog_has_no_total():
    """claimed_total falls back to screens' own product_count only when
    catalog produced none at all (catalog never ran, or its page 0 never
    carried total_results) -- the two describe the same underlying catalog
    size, never added together."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=100)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value=None)), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock()) as mock_catalog, \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    mock_catalog.assert_not_awaited()
    assert result == ["B0SCREEN001"]
    shortfall_calls = [
        c for c in mock_logger.warning.call_args_list
        if c.args[0] == "Audible Author Books union fell short of the claimed total"
    ]
    assert len(shortfall_calls) == 1
    assert shortfall_calls[0].kwargs["extra"]["claimed_total"] == 100


@pytest.mark.asyncio
async def test_get_author_books_completeness_check_skipped_logged_at_info_when_no_claimed_total():
    """When neither catalog nor screens ever carried a claimed total at
    all, no shortfall can be computed against nothing -- an INFO line
    records the skip rather than silently omitting the check."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=None)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value=None)), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock()), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"), \
         patch("app.services.audible.authors.logger") as mock_logger:
        await get_author_books("B000AUTHOR", "us", mock_session)

    skip_calls = [
        c for c in mock_logger.info.call_args_list
        if c.args[0] == "Audible Author Books completeness check skipped: no claimed total from catalog or screens"
    ]
    assert len(skip_calls) == 1
    assert skip_calls[0].kwargs["extra"]["author_asin"] == "B000AUTHOR"


# ============================================================
# GET AUTHOR BOOKS — 45s deadline threading
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_threads_deadline_into_screens_and_catalog():
    """The same absolute deadline (start + 45s) reaches both the screens
    walk and the catalog walk, not just one of the two live Audible
    fetches."""
    from app.services.audible.authors import get_author_books, AUTHOR_BOOKS_TIME_BUDGET_SECONDS

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"])
    catalog_result = _catalog_result(["B0NAME00001"], total_results=1)
    mock_screen = AsyncMock(return_value=screen_result)
    mock_catalog = AsyncMock(return_value=catalog_result)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=mock_screen), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=mock_catalog), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"), \
         patch("app.services.audible.authors.time.monotonic", return_value=1000.0):
        await get_author_books("B000AUTHOR", "us", mock_session)

    expected_deadline = 1000.0 + AUTHOR_BOOKS_TIME_BUDGET_SECONDS
    assert mock_screen.await_args.kwargs["deadline"] == expected_deadline
    assert mock_catalog.await_args.kwargs["deadline"] == expected_deadline


# ============================================================
# GET AUTHOR BOOKS — region threading at the union's entry point
# ============================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["uk", "de", "jp", "in", "br"])
async def test_get_author_books_threads_region_unmodified_into_both_fetches(region):
    """get_author_books is the function this whole slice rewrote -- every
    other test in this module hardcodes region="us", so someone hardcoding
    "us" inside it, or dropping the param on one of the two underlying
    fetches, would pass every other test here. This asserts the exact
    region string handed to get_author_books reaches BOTH
    _fetch_author_books_by_screen and _fetch_author_books_by_catalog
    unmodified, across a spread of non-"us" regions."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)
    catalog_result = _catalog_result(["B0NAME00001"], total_results=1)
    mock_screen = AsyncMock(return_value=screen_result)
    mock_catalog = AsyncMock(return_value=catalog_result)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=mock_screen), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=mock_catalog), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        await get_author_books("B000AUTHOR", region, mock_session)

    assert mock_screen.await_args.args[1] == region
    # _fetch_author_books_by_catalog's signature is (author_asin, author_name,
    # region, deadline=...) -- region is the third positional argument, not
    # the second (see _fetch_author_books_by_catalog).
    assert mock_catalog.await_args.args[2] == region


# ============================================================
# GET AUTHOR BOOKS — logging (author_asin on every line)
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_info_log_carries_author_asin():
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)
    catalog_result = _catalog_result(["B0NAME00001"], total_results=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value="Some Author")), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=AsyncMock(return_value=catalog_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"), \
         patch("app.services.audible.authors.logger") as mock_logger:
        await get_author_books("B000AUTHOR", "us", mock_session)

    mock_logger.info.assert_called_once()
    assert mock_logger.info.call_args.kwargs["extra"]["author_asin"] == "B000AUTHOR"


# ============================================================
# GET AUTHOR BOOKS — wave ordering (name resolution before the parallel
# screens+catalog wave)
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_resolves_name_before_screens_and_catalog_run():
    """Wave 1 (name resolution) completes before wave 2&3 (screens and
    catalog) start at all -- pinned directly on the order of operations.
    Screens and catalog themselves run concurrently via asyncio.gather (see
    get_author_books' own docstring), so no ordering between the two of
    them is asserted here -- that would pin an implementation detail of
    asyncio scheduling, not a real guarantee. This replaces the deleted
    test_get_author_books_runs_name_search_before_screens_walk, which
    pinned a fully sequential resolve -> name-search -> screens order that
    no longer holds now that the catalog walk runs concurrently with
    screens rather than before it."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    call_order = []

    async def _fake_resolve(*args, **kwargs):
        call_order.append("resolve_name")
        return "Some Author"

    async def _fake_catalog(*args, **kwargs):
        call_order.append("catalog")
        return _catalog_result(["B0NAME00001"], total_results=1)

    async def _fake_screen(*args, **kwargs):
        call_order.append("screens")
        return _screen_result(["B0SCREEN001"], product_count=1)

    with patch("app.services.audible.authors._resolve_author_name", new=_fake_resolve), \
         patch("app.services.audible.authors._fetch_author_books_by_catalog", new=_fake_catalog), \
         patch("app.services.audible.authors._fetch_author_books_by_screen", new=_fake_screen), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background"):
        await get_author_books("B000AUTHOR", "us", mock_session)

    assert call_order[0] == "resolve_name"
    assert set(call_order[1:]) == {"catalog", "screens"}


# ============================================================
# _fetch_author_books_by_name_detailed — completion signal (Cluster A)
# ============================================================

@pytest.mark.asyncio
async def test_fetch_author_books_by_name_detailed_deadline_truncation_sets_completed_false():
    """A deadline that has already passed by the time the next page would be
    fetched stops the walk there and reports completed=False — distinct
    from a short page, which is the catalog endpoint's own confirmed-end
    signal and reports True."""
    from app.services.audible.authors import _fetch_author_books_by_name_detailed

    full_page = {"products": [_product_by_name(f"B0FULL{i:05d}", "Frank Herbert") for i in range(50)]}
    mock_get = AsyncMock(return_value=full_page)

    # A finite side_effect list here would starve: _fetch_author_books_by_name_detailed
    # is parallelised via asyncio.gather, and CPython's event loop makes its
    # own internal time.monotonic() calls the moment any real Task-based
    # concurrency exists (asyncio/base_events.py), even for a single-item
    # gather -- those consume from a finite list too and eventually raise an
    # uncaught StopIteration deep inside asyncio. An unbounded stub (the
    # first call -- this function's own pre-batch-0 deadline check, made
    # before asyncio.gather is ever awaited -- returns "before the deadline";
    # every call after that, ours or asyncio's own internal ones, returns
    # "after it") tolerates however many internal calls the event loop makes.
    call_count = {"n": 0}

    def _fake_monotonic():
        call_count["n"] += 1
        return 0.0 if call_count["n"] == 1 else 100.0

    with patch("app.services.audible.authors.audible_get", new=mock_get), \
         patch("app.services.audible.authors.time.monotonic", side_effect=_fake_monotonic):
        asins, pages_fetched, completed = await _fetch_author_books_by_name_detailed(
            "Frank Herbert", "us", deadline=50.0
        )

    assert mock_get.await_count == 1
    assert len(asins) == 50
    assert pages_fetched == 1
    assert completed is False


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_detailed_short_page_sets_completed_true():
    """A page shorter than num_results is the catalog endpoint's own
    confirmed-end signal, distinct from a deadline cutting the walk short."""
    from app.services.audible.authors import _fetch_author_books_by_name_detailed

    short_page = {"products": [_product_by_name("B0SHORT0002", "Frank Herbert")]}
    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=short_page)):
        asins, pages_fetched, completed = await _fetch_author_books_by_name_detailed(
            "Frank Herbert", "us"
        )

    assert completed is True
    assert asins == ["B0SHORT0002"]


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_detailed_mid_batch_failure_truncates_at_last_contiguous_success():
    """A page that fails partway through a concurrent batch truncates the
    result at the last contiguous successful page in ascending index order
    -- a later page in the same batch that happened to complete (page 3
    here) is discarded, never used to paper over the gap left by page 2's
    failure, the same 'clean prefix, never a hole' guarantee the old
    sequential walk gave. completed stays False since a mid-walk failure is
    not one of the walk's two genuine end signals."""
    from app.services.audible.authors import _fetch_author_books_by_name_detailed

    page0 = {"products": [_product_by_name(f"B0PAGE0{i:03d}", "Frank Herbert") for i in range(50)]}
    page1 = {"products": [_product_by_name(f"B0PAGE1{i:03d}", "Frank Herbert") for i in range(50)]}
    page3 = {"products": [_product_by_name(f"B0PAGE3{i:03d}", "Frank Herbert") for i in range(50)]}

    async def _get(region, path, params):
        page = params["page"]
        if page == 0:
            return page0
        if page == 1:
            return page1
        if page == 2:
            raise RuntimeError("upstream 500")
        if page == 3:
            return page3
        raise AssertionError(f"unexpected page requested: {page}")

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.authors.logger") as mock_logger:
        asins, pages_fetched, completed = await _fetch_author_books_by_name_detailed(
            "Frank Herbert", "us", concurrency=3,
        )

    assert all(a.startswith("B0PAGE0") or a.startswith("B0PAGE1") for a in asins)
    assert not any(a.startswith("B0PAGE3") for a in asins)
    assert len(asins) == 100
    assert pages_fetched == 2
    assert completed is False

    mock_logger.warning.assert_called_once()
    warning_extra = mock_logger.warning.call_args.kwargs["extra"]
    assert warning_extra["page"] == 2
    assert warning_extra["error"] == "RuntimeError: upstream 500"


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_detailed_total_results_ends_walk_without_a_short_page():
    """When page 0's response carries a genuine total_results, the walk
    knows the true last page in advance and stops there directly -- it
    never needs to probe an extra page past the known end just to observe
    a short/empty page, even though the last known page here is itself a
    full (non-short) page."""
    from app.services.audible.authors import _fetch_author_books_by_name_detailed

    page0 = {
        "total_results": 100,
        "products": [_product_by_name(f"B0PAGE0{i:03d}", "Frank Herbert") for i in range(50)],
    }
    page1 = {"products": [_product_by_name(f"B0PAGE1{i:03d}", "Frank Herbert") for i in range(50)]}
    mock_get = AsyncMock(side_effect=[page0, page1])

    with patch("app.services.audible.authors.audible_get", new=mock_get):
        asins, pages_fetched, completed = await _fetch_author_books_by_name_detailed(
            "Frank Herbert", "us"
        )

    assert mock_get.await_count == 2
    assert pages_fetched == 2
    assert len(asins) == 100
    assert completed is True


@pytest.mark.asyncio
async def test_fetch_author_books_by_name_detailed_content_repeat_stops_walk_but_is_not_completed():
    """Audible's catalog endpoint can report a total_results far past its
    real internal retrieval ceiling for a query (confirmed live against
    Arthur Conan Doyle: total_results claimed 5367, but page 10 was the
    last with new content -- every later page repeated page 10's exact
    content forever rather than going short). A repeated page signature
    is this walk noticing its own plateau, not upstream confirming nothing
    further remains -- the same distinction the screens walk draws between
    SCREENS_REASON_PLATEAU_TRUNCATED and SCREENS_REASON_COMPLETED. It ends
    the walk (so it doesn't paginate through wasted, identical pages
    trusting the inflated total_results) but must NOT be reported as
    completed -- a caller relying on this list as exhaustive needs to know
    the walk merely stopped, not that it finished. The repeated page still
    counts toward pages_fetched (it was genuinely fetched) but contributes
    no new ASINs."""
    from app.services.audible.authors import _fetch_author_books_by_name_detailed

    page0 = {
        "total_results": 5367,
        "products": [_product_by_name(f"B0PAGE0{i:03d}", "Frank Herbert") for i in range(50)],
    }
    repeated_products = [_product_by_name(f"B0PLATEAU{i:03d}", "Frank Herbert") for i in range(50)]
    page1 = {"products": repeated_products}
    page2 = {"products": repeated_products}  # identical signature to page1 -- the plateau
    mock_get = AsyncMock(side_effect=[page0, page1, page2])

    with patch("app.services.audible.authors.audible_get", new=mock_get):
        asins, pages_fetched, completed = await _fetch_author_books_by_name_detailed(
            "Frank Herbert", "us"
        )

    assert mock_get.await_count == 3
    assert pages_fetched == 3
    assert len(asins) == 100  # page0's 50 + page1's 50 unique -- page2 contributes nothing new
    assert completed is False


# ============================================================
# _resolve_author_name — terminal None vs. propagating failure (Cluster A)
# ============================================================

@pytest.mark.asyncio
async def test_resolve_author_name_returns_none_on_contributor_404():
    """A confirmed Audible 404 on the contributor is the terminal
    'no name on record' case and must return None, not raise."""
    from app.services.audible.authors import _resolve_author_name
    from app.core.exceptions import NotFoundException

    mock_session = AsyncMock()
    with patch("app.services.audible.authors.get_author_from_db", new=AsyncMock(return_value=None)), \
         patch("app.services.audible.authors._fetch_author_details", new=AsyncMock(side_effect=NotFoundException("no such contributor"))):
        result = await _resolve_author_name("B000AUTHOR", "us", mock_session)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_author_name_returns_none_on_empty_name_field():
    """A 200 response with an empty name field is the other confirmed
    'no name on record' shape and must also return None."""
    from app.services.audible.authors import _resolve_author_name

    mock_session = AsyncMock()
    empty_name_response = {"contributor": {"name": "", "bio": None, "profile_image_url": None}}
    with patch("app.services.audible.authors.get_author_from_db", new=AsyncMock(return_value=None)), \
         patch("app.services.audible.authors._fetch_author_details", new=AsyncMock(return_value=empty_name_response)):
        result = await _resolve_author_name("B000AUTHOR", "us", mock_session)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_author_name_propagates_non_404_exception():
    """A failure that is not a confirmed absence must propagate to the
    caller so get_author_books can set name_error, rather than collapsing
    into the same silent None as a genuine no-name-on-record author."""
    from app.services.audible.authors import _resolve_author_name
    from app.core.exceptions import AudibleAPIException

    mock_session = AsyncMock()
    with patch("app.services.audible.authors.get_author_from_db", new=AsyncMock(return_value=None)), \
         patch("app.services.audible.authors._fetch_author_details", new=AsyncMock(side_effect=AudibleAPIException("upstream 500"))):
        with pytest.raises(AudibleAPIException):
            await _resolve_author_name("B000AUTHOR", "us", mock_session)


# ============================================================
# SCREENS_REASON_GRID_NOT_FOUND (Cluster B)
# ============================================================

@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_absent_sections_triggers_grid_not_found():
    """A page with no sections key at all carries no positive evidence the
    grid was ever found -- it must not score as a clean, complete walk."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value={})):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.pages_fetched == 1
    assert result.asins == []
    assert result.termination_reason == SCREENS_REASON_GRID_NOT_FOUND
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_empty_sections_triggers_grid_not_found():
    """An explicitly empty sections list is the same 'no positive evidence'
    case as an absent one."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value={"sections": []})):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.termination_reason == SCREENS_REASON_GRID_NOT_FOUND
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_renamed_component_type_triggers_grid_not_found():
    """A page whose grid section carries a __component_type upstream has
    silently renamed is indistinguishable from a page with no grid section
    at all -- both must be caught by the same reclassification."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    renamed_section = {
        "model": {
            "__component_type": "SomeRenamedGridComponentV3",
            "header": {"header_model": {"product_count": 5}},
            "rows": [_row("B0RENAMED1")],
        },
        "pagination": None,
    }
    page = {"sections": [renamed_section]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.asins == []
    assert result.termination_reason == SCREENS_REASON_GRID_NOT_FOUND
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_grid_not_found_fires_unclean_warning():
    from app.services.audible.authors import _fetch_author_books_by_screen

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value={})), \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.termination_reason == SCREENS_REASON_GRID_NOT_FOUND
    mock_logger.warning.assert_called_once()
    warning_extra = mock_logger.warning.call_args.kwargs["extra"]
    assert warning_extra["termination_reason"] == SCREENS_REASON_GRID_NOT_FOUND


# ============================================================
# SCREENS_MAX_SECTIONS / SCREENS_MAX_ROWS_PER_PAGE — the two slicing bounds
# ============================================================

@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_sections_truncated_counter_and_demotion():
    """A page carrying more non-grid candidate sections than
    SCREENS_MAX_SECTIONS reports the exact overflow count on
    sections_truncated, and an otherwise-clean (null-token) walk is demoted
    from completed to sections_or_rows_truncated -- some of what upstream
    sent was never even looked at, so it cannot score as having seen
    everything upstream had to offer."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    grid_section = _asin_section([_row("B0GRID0001")], product_count=1)
    overflow = 3
    decoy_sections = [_asin_section([]) for _ in range(SCREENS_MAX_SECTIONS + overflow)]
    page = {"sections": [grid_section] + decoy_sections}
    # The last section's pagination is what _extract_next_token reads; it's
    # already None by default (_asin_section's default), so this walk would
    # otherwise terminate as a clean, confirmed-complete COMPLETED walk.

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.sections_truncated == overflow
    assert result.rows_truncated == 0
    assert result.asins == ["B0GRID0001"]
    assert result.termination_reason == SCREENS_REASON_TRUNCATED
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_rows_truncated_counter_and_demotion():
    """A single section carrying more rows than SCREENS_MAX_ROWS_PER_PAGE
    reports the exact overflow count on rows_truncated, and an otherwise-
    clean walk is demoted to sections_or_rows_truncated the same way a
    section overflow is -- the rows beyond the cap were never inspected at
    all, not silently dropped without a trace."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    overflow = 5
    total_rows = SCREENS_MAX_ROWS_PER_PAGE + overflow
    rows = [_row(f"B{i:09X}") for i in range(total_rows)]
    # product_count deliberately does NOT match total_rows -- a real page
    # never carries more than ~20 rows, so a product_count that size would
    # imply hundreds of further pages and route this walk into the
    # multi-page fan-out (see _fetch_author_books_by_screen), which is not
    # what this test is exercising. Sized to imply exactly one page keeps
    # this isolated to the single-page row cap.
    page = {"sections": [_asin_section(rows, product_count=1)]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    assert result.rows_truncated == overflow
    assert result.sections_truncated == 0
    assert len(result.asins) == SCREENS_MAX_ROWS_PER_PAGE
    assert result.termination_reason == SCREENS_REASON_TRUNCATED
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


# ============================================================
# THE MISATTRIBUTION REGRESSION — the worst defect the panel found
# ============================================================

@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_grid_beyond_section_cap_is_not_misattributed():
    """The exact repro of the headline defect: a wall of decoy rail sections
    (each belonging to a different author) sits ahead of the real grid,
    which lands past SCREENS_MAX_SECTIONS. Before the fix, the section cap
    was applied BEFORE the grid was identified, and a lone/ambiguous
    candidate within the cap window was admitted by elimination with no
    attribution check -- so a page shaped exactly like this served an
    unrelated rail's books as the author's catalogue, reported `completed`,
    and was cache-eligible. This must no longer happen: only the real
    grid's ASINs are admitted, every decoy is attribution-rejected (not
    silently dropped and not admitted), and the walk is not reported as a
    clean, confirmed-complete one."""
    from app.services.audible.authors import _fetch_author_books_by_screen

    decoy_count = SCREENS_MAX_SECTIONS + 50
    decoy_sections = [
        _asin_section(
            [_row(f"B0DECOY{i:03d}", authors=[{"asin": "B000OTHERX", "name": "Someone Else"}])]
        )
        for i in range(decoy_count)
    ]
    real_grid_rows = [_row(f"B0REALGRD{i}") for i in range(5)]
    grid_section = _asin_section(real_grid_rows, product_count=5)
    # The real grid sits AFTER every decoy -- well past SCREENS_MAX_SECTIONS.
    page = {"sections": decoy_sections + [grid_section]}

    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=page)):
        result = await _fetch_author_books_by_screen("B000TARGET", "us")

    expected_grid_asins = {row["product_metadata"]["asin"] for row in real_grid_rows}
    assert set(result.asins) == expected_grid_asins
    assert not any(asin.startswith("B0DECOY") for asin in result.asins)
    assert result.product_count == 5
    assert result.attribution_rejected == SCREENS_MAX_SECTIONS
    assert result.sections_truncated == decoy_count - SCREENS_MAX_SECTIONS
    assert result.termination_reason != SCREENS_REASON_COMPLETED
    assert result.termination_reason not in SCREENS_CLEAN_REASONS


# ============================================================
# DIAGNOSTICS (Cluster C)
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_total_failure_logs_warning_before_raising():
    """The total-failure branch must log a WARNING carrying every diagnostic
    field before raising NotFoundException -- it must not raise silently.
    Name resolution failing outright means author_name stays None, so the
    catalog wave is never scheduled at all (see get_author_books' own
    docstring) -- catalog_error and catalog_sort_errors reflect that "never
    ran" state rather than a catalog-specific failure."""
    from app.services.audible.authors import get_author_books
    from app.core.exceptions import NotFoundException, AudibleAPIException

    mock_session = AsyncMock()
    empty_screen = _screen_result(
        [], pages_fetched=1, termination_reason=SCREENS_REASON_PAGE_ERROR,
        page_error="AudibleAPIException: upstream 502",
    )

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=empty_screen)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(side_effect=AudibleAPIException("boom"))), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.logger") as mock_logger:
        with pytest.raises(NotFoundException):
            await get_author_books("B000AUTHOR", "us", mock_session)

    mock_logger.warning.assert_called_once()
    warning_extra = mock_logger.warning.call_args.kwargs["extra"]
    assert warning_extra["author_asin"] == "B000AUTHOR"
    assert warning_extra["region"] == "us"
    assert warning_extra["screen_error"] is None
    assert warning_extra["screen_page_error"] == "AudibleAPIException: upstream 502"
    assert warning_extra["catalog_error"] is None
    assert warning_extra["catalog_sort_errors"] == []
    assert warning_extra["name_resolution_error"] == "AudibleAPIException: boom"


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_page_error_distinguishes_exception_types():
    """An AudibleAPIException, a NotFoundException, and a plain bug
    (TypeError) must each produce a page_error string that names its own
    exception type -- collapsing them into the same static string would
    make a genuine code defect indistinguishable from an upstream failure
    downstream in logs."""
    from app.services.audible.authors import _fetch_author_books_by_screen
    from app.core.exceptions import AudibleAPIException, NotFoundException

    cases = [
        (AudibleAPIException("upstream 502"), "AudibleAPIException: upstream 502"),
        (NotFoundException("gone"), "NotFoundException: gone"),
        (TypeError("unexpected shape"), "TypeError: unexpected shape"),
    ]
    for exc, expected in cases:
        with patch("app.services.audible.authors.audible_get", new=AsyncMock(side_effect=exc)):
            result = await _fetch_author_books_by_screen("B000TARGET", "us")
        assert result.page_error == expected, f"mismatch for {type(exc).__name__}"
        assert result.termination_reason == SCREENS_REASON_PAGE_ERROR


@pytest.mark.asyncio
async def test_get_author_books_degraded_path_warning_fires_on_successful_non_empty_request():
    """The 'served from a degraded path' warning must fire even when the
    request succeeds and the union it contributed to is non-empty -- a
    degraded path that still produced output is not exempt from the
    warning. Name resolution failing outright means author_name stays None
    and the catalog wave is never scheduled (see get_author_books' own
    docstring), so catalog_error/catalog_sort_errors reflect that "never
    ran" state, not a catalog-specific failure.

    Also guards the regression this exact scenario used to hide: with
    catalog never having run, the union being cached is screens-only --
    catalog_clean now explicitly excludes the name_resolution_error case
    (rather than reading "no author name" as trivially complete regardless
    of why), so persist_author_books_cache_background must NOT fire here.
    A screens-only partial view must never be written back as the
    authoritative cached list."""
    from app.services.audible.authors import get_author_books

    mock_session = AsyncMock()
    screen_result = _screen_result(["B0SCREEN001"], product_count=1)

    with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(side_effect=RuntimeError("name boom"))), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.cache.get", return_value=None), \
         patch("app.services.audible.authors.persist_author_books_cache_background") as mock_persist, \
         patch("app.services.audible.authors.logger") as mock_logger:
        result = await get_author_books("B000AUTHOR", "us", mock_session)

    assert result == ["B0SCREEN001"]
    mock_persist.assert_not_called()
    mock_logger.warning.assert_called_once_with(
        "Audible Author Books served from a degraded path",
        extra={
            "author_asin": "B000AUTHOR",
            "region": "us",
            "screen_error": None,
            "screen_page_error": None,
            "catalog_error": None,
            "catalog_sort_errors": [],
            "name_resolution_error": "RuntimeError: name boom",
            "db_error": None,
        },
    )


@pytest.mark.asyncio
async def test_fetch_author_books_by_screen_every_warning_line_carries_author_asin():
    """Restores the coverage of the deleted test of the same name (removed
    in the commit that dropped _fetch_author_books_by_screen's own
    per-screen shortfall warning, since that removal left only one warning
    site in that function -- the original two-warnings-in-one-walk
    assertion no longer applies there). Rescoped against the CURRENT
    warning set across the whole ASIN-scoped author-books path -- which has
    since grown a new warning line (the catalog ceiling-saturation warning)
    -- rather than only the single warning left inside
    _fetch_author_books_by_screen itself: every WARNING this path can emit,
    from the screens-level unclean-termination line through every
    get_author_books-level line (total failure, union shortfall, catalog
    ceiling saturation, degraded path), must carry author_asin. This is
    deliberately narrower than every warning module-wide:
    _fetch_author_books_by_name_detailed's own page-fetch-failure warning
    (a name-only walk with no ASIN in scope at all) carries author_name
    instead, by design, and is not covered here."""
    from app.services.audible.authors import (
        get_author_books, _fetch_author_books_by_screen,
        _CATALOG_SORTS, CATALOG_RESULT_CEILING,
    )
    from app.core.exceptions import NotFoundException

    asin = "B000TARGET"
    collected_extras = []

    async def _run_get_author_books(
        screen_result, resolve_name_result, catalog_outcome,
        resolve_name_raises=False, catalog_raises=False,
    ):
        mock_session = AsyncMock()
        resolve_name_mock = (
            AsyncMock(side_effect=resolve_name_result)
            if resolve_name_raises
            else AsyncMock(return_value=resolve_name_result)
        )
        catalog_mock = (
            AsyncMock(side_effect=catalog_outcome)
            if catalog_raises
            else AsyncMock(return_value=catalog_outcome)
        )
        with patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
             patch("app.services.audible.authors._resolve_author_name", new=resolve_name_mock), \
             patch("app.services.audible.authors._fetch_author_books_by_catalog", new=catalog_mock), \
             patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
             patch("app.services.audible.authors.cache.get", return_value=None), \
             patch("app.services.audible.authors.persist_author_books_cache_background"), \
             patch("app.services.audible.authors.logger") as mock_logger:
            try:
                await get_author_books(asin, "us", mock_session)
            except NotFoundException:
                pass
        collected_extras.extend(c.kwargs["extra"] for c in mock_logger.warning.call_args_list)

    # Scenario 1: total failure -- screen errors, no name resolved (catalog
    # never scheduled), DB and cache both empty -> "unavailable from every
    # path".
    await _run_get_author_books(
        _screen_result([], termination_reason=SCREENS_REASON_PAGE_ERROR, page_error="boom"),
        None, _catalog_result([]),
    )

    # Scenario 2: union shortfall past the 10% threshold -> "union fell
    # short of the claimed total".
    await _run_get_author_books(
        _screen_result([f"B0S{i:04d}" for i in range(500)], product_count=500),
        "Some Author",
        _catalog_result([f"B0C{i:04d}" for i in range(300)], total_results=1000),
    )

    # Scenario 3: catalog ceiling saturated -> the dedicated saturation
    # warning (the new line this slice added).
    saturated_total = len(_CATALOG_SORTS) * CATALOG_RESULT_CEILING + 1
    await _run_get_author_books(
        _screen_result(["B0SCREEN001"], product_count=1),
        "Some Author",
        _catalog_result(["B0CAT0001"], total_results=saturated_total, ceiling_saturated=True),
    )

    # Scenario 4: catalog errors outright, screens fine -> "served from a
    # degraded path".
    await _run_get_author_books(
        _screen_result(["B0SCREEN002"], product_count=1),
        "Some Author",
        RuntimeError("catalog boom"),
        catalog_raises=True,
    )

    assert len(collected_extras) == 4
    for extra in collected_extras:
        assert extra["author_asin"] == asin

    # Scenario 5: the one warning site that still lives inside
    # _fetch_author_books_by_screen itself -- exercised directly, since
    # get_author_books' own mocking of that function bypasses its internals
    # entirely.
    echoing_page = {"sections": [_asin_section(
        [_row("B0BOTH0001")], product_count=1000, pagination="SAMETOKEN",
    )]}
    with patch("app.services.audible.authors.audible_get", new=AsyncMock(return_value=echoing_page)), \
         patch("app.services.audible.authors.logger") as mock_logger:
        await _fetch_author_books_by_screen(asin, "us")

    mock_logger.warning.assert_called_once()
    assert mock_logger.warning.call_args.kwargs["extra"]["author_asin"] == asin