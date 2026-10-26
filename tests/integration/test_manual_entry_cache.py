"""
manualEntry on the cache path, against real Postgres.

A manual-entry row is written through the real chunk persist, which reads the
stored row back and caches it. Every response served from that stored copy
carries manualEntry true: the cache entry itself, /book/{asin} and
/book?asins= on a cache hit, and the outage fallback. A fresh answer from
Audible reads false, and a non-manual row never reads true on any of them.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

# Local
from app.main import app
from app.services.audible import books as books_mod
from app.services.cache.manager import book_key, get
from app.services.db.persist_queue import _write_book_chunk
from tests.fixtures.audible_product import AUDIBLE_PRODUCT

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

REGION = "us"
MANUAL = "B0MANUAL01"
PLAIN = "B0PLAIN001"


def _book(asin, **fields):
    return {"asin": asin, "region": REGION, "title": f"Title {asin}", **fields}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def stored(db_session):
    await _write_book_chunk(
        db_session, [_book(MANUAL, manualEntry=True), _book(PLAIN)], REGION
    )


def _outage():
    return patch.object(books_mod, "_fetch_chunk", new=AsyncMock(side_effect=RuntimeError("down")))


@pytest.mark.asyncio
async def test_the_cache_entry_carries_the_stored_flag(db_session, stored):
    assert (await get(db_session, book_key(MANUAL, REGION)))["manualEntry"] is True
    assert (await get(db_session, book_key(PLAIN, REGION)))["manualEntry"] is False


@pytest.mark.asyncio
async def test_a_single_book_cache_hit_serves_the_flag(client, stored):
    fetch = AsyncMock()
    with patch.object(books_mod, "_fetch_chunk", new=fetch):
        manual = (await client.get(f"/book/{MANUAL}")).json()
        plain = (await client.get(f"/book/{PLAIN}")).json()
    fetch.assert_not_called()
    assert manual["manualEntry"] is True
    assert plain["manualEntry"] is False


@pytest.mark.asyncio
async def test_a_bulk_cache_hit_serves_the_flag_per_book(client, stored):
    fetch = AsyncMock()
    with patch.object(books_mod, "_fetch_chunk", new=fetch):
        body = (await client.get(f"/book?asins={MANUAL},{PLAIN}")).json()
    fetch.assert_not_called()
    assert {b["asin"]: b["manualEntry"] for b in body["books"]} == {MANUAL: True, PLAIN: False}


@pytest.mark.asyncio
async def test_the_outage_fallback_serves_the_stored_flag(client, stored):
    with _outage():
        manual = await client.get(f"/book/{MANUAL}?cache=false")
        plain = await client.get(f"/book/{PLAIN}?cache=false")
    assert manual.json()["manualEntry"] is True
    assert plain.json()["manualEntry"] is False


@pytest.mark.asyncio
async def test_the_outage_fallback_to_the_cache_serves_the_flag(client, db_session, stored):
    await db_session.execute(text("DELETE FROM books"))
    await db_session.commit()
    with _outage():
        manual = await client.get(f"/book/{MANUAL}?cache=false")
        plain = await client.get(f"/book/{PLAIN}?cache=false")
    assert manual.json()["manualEntry"] is True
    assert plain.json()["manualEntry"] is False


@pytest.mark.asyncio
async def test_a_fresh_answer_from_audible_reads_false_even_for_a_stored_manual_row(client, stored):
    product = {**AUDIBLE_PRODUCT, "asin": MANUAL}
    with patch.object(books_mod, "_fetch_chunk", new=AsyncMock(return_value=[product])), \
         patch.object(books_mod, "persist_books_background"):
        response = await client.get(f"/book/{MANUAL}?cache=false")
    assert response.status_code == 200
    assert response.json()["manualEntry"] is False
