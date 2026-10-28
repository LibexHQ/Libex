"""
manualEntry on the book surfaces: /book/{asin}, /book?asins=, /search and /db/book
each gain that one key and no other. The live normalizer never emits it, so a
fresh answer from Audible reads false. A response served from Libex's stored
copy carries the stored value: /db always, and /book on a cache hit or the
outage fallback (tests/integration/test_manual_entry_cache.py). This file mocks
the service, so it pins the key set and the false of a fresh answer.

The expected key set is the normalizer's own output plus manualEntry, so a key
the model gained or lost for any other reason fails here too.
"""

# Standard library
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from httpx import AsyncClient, ASGITransport

# Local
from app.db.models import Book
from app.main import app
from libex_core.audible.books import normalize_product
from libex_core.storage.read.shapes import book_to_dict
from tests.fixtures.audible_product import AUDIBLE_PRODUCT

ASIN = "B0MANUAL01"
# One episode relationship makes the normalizer withhold something, so its
# output holds extrasWithheld too and the key set is the maximal one.
PRODUCT = {
    **AUDIBLE_PRODUCT,
    "asin": ASIN,
    "relationships": [{"asin": "B0EPISODE1", "relationship_type": "episode", "sort": "1"}],
}
LIVE_KEYS = set(normalize_product(PRODUCT, "us"))
SURFACE_KEYS = LIVE_KEYS | {"manualEntry"}


@pytest.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _live() -> dict:
    return normalize_product(PRODUCT, "us")


def _stored(manual: bool) -> dict:
    row = Book(
        asin=ASIN, region="us", title="Delisted", extras_withheld={"episodes": "not modelled"},
        explicit=False, whisper_sync=False, has_pdf=False, is_listenable=True, is_buyable=True,
        is_vvab=False, plans=[],
        manual_entry_at=datetime(2026, 3, 1, tzinfo=timezone.utc) if manual else None,
    )
    return book_to_dict(row, {})


def test_the_live_normalizer_never_emits_the_key():
    assert "manualEntry" not in LIVE_KEYS


async def test_live_single_book_gains_exactly_the_key_and_reads_false(async_client):
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = _live()
        body = (await async_client.get(f"/book/{ASIN}")).json()
    assert set(body) == SURFACE_KEYS
    assert body["manualEntry"] is False


async def test_bulk_books_gain_exactly_the_key_and_read_false(async_client):
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        mock.return_value = [_live()]
        body = (await async_client.get(f"/book?asins={ASIN}")).json()
    assert len(body["books"]) == 1
    assert set(body["books"][0]) == SURFACE_KEYS
    assert body["books"][0]["manualEntry"] is False


async def test_search_gains_exactly_the_key_and_reads_false(async_client):
    with patch("app.api.routes.search.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [_live()]
        body = (await async_client.get("/search?title=Delisted")).json()
    assert set(body[0]) == SURFACE_KEYS
    assert body[0]["manualEntry"] is False


@pytest.mark.parametrize("manual", [True, False])
async def test_db_book_list_reads_the_stored_provenance(async_client, manual):
    with patch("app.api.routes.db.router.search_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [_stored(manual)]
        body = (await async_client.get("/db/book?title=Delisted")).json()
    assert set(body[0]) == SURFACE_KEYS
    assert body[0]["manualEntry"] is manual


@pytest.mark.parametrize("manual", [True, False])
async def test_db_book_by_asin_reads_the_stored_provenance(async_client, manual):
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = _stored(manual)
        body = (await async_client.get(f"/db/book/{ASIN}")).json()
    assert set(body) == SURFACE_KEYS
    assert body["manualEntry"] is manual
    assert "manual_entry_at" not in body  # the timestamp is not exposed
