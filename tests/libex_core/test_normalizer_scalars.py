"""
A scalar Audible sends as the wrong type, in the book and series normalizers.

Each field is settled one of two ways, and the split is the point. A text
field whose key is withheld from audibleExtras because the first-class field
carries it (content_type, merchandising_summary, publisher_summary) is
published as no value and its raw value rides into the blob under its own
key: no well-formed response writes that key there, so nothing stored can be
overwritten. A field whose raw value is in the blob regardless (release_date,
product_images) keeps raising, because a book that normalized would write the
malformed value over the stored copy through the shallow union.

Falsy values of any type are how Audible says nothing and read exactly as
they always did.
"""

# Third party
import pytest

# Local
from app.services.audible import books as hosted_books
from libex_core.audible.authors.profile import normalize_author
from libex_core.audible.books import (
    _best_image,
    fetch_products,
    _parse_release_date,
    normalize_product,
)
from libex_core.audible.series import normalize_series
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import get_books
from tests.libex_core._lookup_support import asins

REGION = "us"
ASIN = "B0SCALAR01"

# Truthy, not a string: every one of these used to raise.
TRUTHY_NON_TEXT = [12, 1.5, True, ["a"], {"a": 1}]
# Falsy, not a string: every one of these always read as no value.
FALSY_NON_TEXT = [0, 0.0, False, [], {}]


def _product(**extra):
    return {"asin": ASIN, "title": "A Book", **extra}


# ============================================================
# TEXT FIELDS: DEFAULTED, RAW VALUE KEPT
# ============================================================

@pytest.mark.parametrize("raw", TRUTHY_NON_TEXT)
def test_a_content_type_that_is_not_text_is_no_content_type_and_the_raw_value_is_kept(raw):
    book = normalize_product(_product(content_type=raw, episode_number=4), REGION)

    assert book["contentType"] is None
    assert book["episodeNumber"] is None and book["episodeType"] is None
    assert book["audibleExtras"]["content_type"] == raw


@pytest.mark.parametrize("raw", TRUTHY_NON_TEXT)
@pytest.mark.parametrize("key,field", [
    ("merchandising_summary", "description"),
    ("publisher_summary", "summary"),
])
def test_a_summary_that_is_not_text_is_no_text_and_the_raw_value_is_kept(raw, key, field):
    book = normalize_product(_product(**{key: raw}), REGION)

    assert book[field] is None
    assert book["audibleExtras"][key] == raw


def test_the_other_summary_is_untouched_when_one_is_unreadable():
    book = normalize_product(
        _product(merchandising_summary=12, publisher_summary="<p>Real</p>"), REGION
    )

    assert book["description"] is None
    assert book["summary"] == "Real"
    assert "publisher_summary" not in book["audibleExtras"]


@pytest.mark.parametrize("key", ["content_type", "merchandising_summary", "publisher_summary"])
def test_a_well_formed_text_field_still_stays_out_of_the_blob(key):
    book = normalize_product(_product(**{key: "podcast"}), REGION)

    assert book["audibleExtras"] is None or key not in book["audibleExtras"]


@pytest.mark.parametrize("raw", FALSY_NON_TEXT)
def test_a_falsy_non_text_content_type_reads_as_it_always_did(raw):
    book = normalize_product(_product(content_type=raw), REGION)

    assert book["contentType"] == raw
    assert book["episodeNumber"] is None
    assert book["audibleExtras"] is None or "content_type" not in book["audibleExtras"]


@pytest.mark.parametrize("raw", FALSY_NON_TEXT)
def test_a_falsy_non_text_summary_reads_as_no_text_and_stays_out_of_the_blob(raw):
    book = normalize_product(
        _product(merchandising_summary=raw, publisher_summary=raw), REGION
    )

    assert book["description"] is None and book["summary"] is None
    assert book["audibleExtras"] is None or not (
        {"merchandising_summary", "publisher_summary"} & set(book["audibleExtras"])
    )


def test_a_podcast_still_reads_its_episode_fields():
    book = normalize_product(
        _product(content_type="Podcast", episode_number=4, episode_type="full"), REGION
    )

    assert book["episodeNumber"] == "4" and book["episodeType"] == "full"


def test_an_unreadable_text_field_is_logged_without_its_value(caplog):
    import libex_core.audible.books as books_mod

    books_mod._unreadable_text_last_logged.clear()
    with caplog.at_level("WARNING", logger="libex"):
        normalize_product(_product(content_type={"secret": "value"}), REGION)

    record = next(r for r in caplog.records if r.getMessage() == "Audible sent a text field that is not text")
    assert record.text_field == "content_type" and record.region == REGION
    assert "secret" not in " ".join(f"{r.getMessage()} {r.__dict__!r}" for r in caplog.records)


def test_the_unreadable_text_warning_is_windowed_per_key_and_counts_the_suppressed(caplog):
    import libex_core.audible.books as books_mod

    books_mod._unreadable_text_last_logged.clear()
    books_mod._unreadable_text_counts.clear()
    message = "Audible sent a text field that is not text"
    with caplog.at_level("WARNING", logger="libex"):
        for _ in range(3):
            normalize_product(_product(content_type=12), REGION)
        normalize_product(_product(merchandising_summary=12), REGION)

    records = [r for r in caplog.records if r.getMessage() == message]
    assert sorted(r.text_field for r in records) == ["content_type", "merchandising_summary"]
    assert books_mod._unreadable_text_counts["content_type"] == 2

    books_mod._unreadable_text_last_logged["content_type"] -= (
        books_mod._UNREADABLE_TEXT_LOG_INTERVAL_SECONDS + 1
    )
    caplog.clear()
    with caplog.at_level("WARNING", logger="libex"):
        normalize_product(_product(content_type=12), REGION)
    again = [r for r in caplog.records if r.getMessage() == message]
    assert len(again) == 1 and again[0].occurrences == 3


def test_an_unreadable_series_summary_is_logged_without_its_value(caplog):
    with caplog.at_level("WARNING", logger="libex"):
        normalize_series(
            {"asin": "B0SERIES01", "title": "S", "publisher_summary": {"secret": "value"}}, REGION
        )

    record = next(r for r in caplog.records if r.getMessage() == "Audible sent a text field that is not text")
    assert record.text_field == "publisher_summary" and record.region == REGION
    assert "secret" not in " ".join(f"{r.getMessage()} {r.__dict__!r}" for r in caplog.records)


@pytest.mark.parametrize("raw", TRUTHY_NON_TEXT)
def test_a_series_summary_that_is_not_text_is_no_description_and_the_raw_value_is_kept(raw):
    series = normalize_series({"asin": "B0SERIES01", "title": "S", "publisher_summary": raw}, REGION)

    assert series["description"] is None
    assert series["audibleExtras"] == {"publisher_summary": raw}


def test_a_well_formed_series_summary_still_stays_out_of_the_blob():
    series = normalize_series({"asin": "B0SERIES01", "title": "S", "publisher_summary": "<p>x</p>"}, REGION)

    assert series["description"] == "x"
    assert "audibleExtras" not in series


# ============================================================
# RELEASE DATE, IMAGE, BIO: STILL RAISING
# ============================================================

@pytest.mark.parametrize("raw", TRUTHY_NON_TEXT)
def test_a_release_date_that_is_not_text_still_fails_the_book(raw):
    with pytest.raises(TypeError):
        normalize_product(_product(release_date=raw), REGION)
    with pytest.raises(TypeError):
        _parse_release_date(raw)


@pytest.mark.parametrize("raw", FALSY_NON_TEXT)
def test_a_falsy_non_text_release_date_is_no_date(raw):
    assert _parse_release_date(raw) is None
    assert normalize_product(_product(release_date=raw), REGION)["releaseDate"] is None


@pytest.mark.parametrize("url", TRUTHY_NON_TEXT)
def test_an_image_url_that_is_not_text_still_fails_the_book(url):
    with pytest.raises(TypeError):
        _best_image({"500": url})
    with pytest.raises(TypeError):
        normalize_product(_product(product_images={"500": url}), REGION)


@pytest.mark.parametrize("url", FALSY_NON_TEXT)
def test_a_falsy_non_text_image_url_is_no_image(url):
    assert _best_image({"500": url}) is None


@pytest.mark.parametrize("images", [
    12, True, "abc", "500", ["a"], ["x", "y"], ["500"], [1, 2], {"a": 1}.items(),
    {"a": 1}, {"x": "http://example.com/a.jpg"},
])
def test_product_images_that_is_not_an_object_of_sizes_fails_the_book(images):
    with pytest.raises(TypeError):
        _best_image(images)
    with pytest.raises(TypeError):
        normalize_product(_product(product_images=images), REGION)


@pytest.mark.parametrize("images", [None, 0, False, "", [], {}])
def test_falsy_product_images_is_no_image(images):
    assert _best_image(images) is None
    assert normalize_product(_product(product_images=images), REGION)["imageUrl"] is None


@pytest.mark.parametrize("raw", TRUTHY_NON_TEXT)
def test_an_author_bio_that_is_not_text_still_fails_the_response(raw):
    with pytest.raises(TypeError):
        normalize_author({"contributor": {"name": "N", "bio": raw}}, "B000AUTHOR", REGION)


# ============================================================
# products: null IS AN OUTAGE, NEVER AN ABSENCE
# ============================================================

async def _null_products(region, path, params=None, extra_headers=None):
    return {"products": None}


@pytest.mark.asyncio
async def test_a_batch_answered_with_null_products_is_an_outage_in_the_library():
    with pytest.raises(AudibleAPIException) as raised:
        await get_books(_null_products, asins(3), region=REGION)

    assert not isinstance(raised.value, NotFoundException)


@pytest.mark.asyncio
async def test_a_batch_answered_with_null_products_is_an_outage_in_the_hosted_service(monkeypatch):
    from unittest.mock import AsyncMock, patch

    session = AsyncMock()
    session.rollback = AsyncMock()
    monkeypatch.setattr(hosted_books, "audible_get", _null_products)

    with patch.object(hosted_books, "get_books_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        with pytest.raises(AudibleAPIException) as raised:
            await hosted_books.get_books_by_asins(asins(3), REGION, session)

    assert not isinstance(raised.value, NotFoundException)


# ============================================================
# A SINGLE-ASIN 200 WITH product: null IS AN OUTAGE
# ============================================================

async def _null_product(region, path, params=None, extra_headers=None):
    return {"product": None}


@pytest.mark.asyncio
async def test_fetch_products_raises_an_outage_for_a_null_single_product():
    with pytest.raises(AudibleAPIException) as raised:
        await fetch_products(_null_product, [ASIN], REGION)

    assert not isinstance(raised.value, NotFoundException)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [{}, {"product": {}}])
async def test_a_single_product_answer_without_a_product_is_still_empty(answer):
    async def get(region, path, params=None, extra_headers=None):
        return answer

    assert await fetch_products(get, [ASIN], REGION) == []


@pytest.mark.asyncio
async def test_a_null_single_product_is_an_outage_in_the_library():
    with pytest.raises(AudibleAPIException) as raised:
        await get_books(_null_product, [ASIN], region=REGION)

    assert not isinstance(raised.value, NotFoundException)


@pytest.mark.asyncio
async def test_a_null_single_product_is_an_outage_in_the_hosted_service(monkeypatch):
    from unittest.mock import AsyncMock, patch

    session = AsyncMock()
    session.rollback = AsyncMock()
    monkeypatch.setattr(hosted_books, "audible_get", _null_product)

    with patch.object(hosted_books, "get_books_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        with pytest.raises(AudibleAPIException) as raised:
            await hosted_books.get_book_by_asin(ASIN, REGION, session)

    assert not isinstance(raised.value, NotFoundException)


@pytest.mark.asyncio
async def test_a_null_single_product_serves_the_stored_copy_in_the_hosted_service(monkeypatch):
    from unittest.mock import AsyncMock, patch

    session = AsyncMock()
    session.rollback = AsyncMock()
    monkeypatch.setattr(hosted_books, "audible_get", _null_product)
    stored = {"asin": ASIN, "title": "Stored"}

    with patch.object(hosted_books, "get_books_from_db", new=AsyncMock(return_value=[stored])), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        book = await hosted_books.get_book_by_asin(ASIN, REGION, session)

    assert book == stored
