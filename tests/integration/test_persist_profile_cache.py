"""
Integration tests for the cache entries the background author and series
persists write, against real Postgres.

The writer keeps the richer value when Audible answers with less, so the cache
entry written beside it has to be the stored row and not the payload, or the
cached read serves the shrunken answer the store refused.
"""

# Standard library
from unittest.mock import patch

# Third party
import pytest
from sqlalchemy import select

# Local
from app.db.models import Cache
from app.services.cache.manager import author_key, chapters_key, series_key
from app.services.db import persist_queue
from app.services.db.writer import upsert_book

REGION = "us"


async def _persist(persist, data, *extra):
    """Runs the task a persist_*_background entry point would have spawned, to
    completion, against the real session factory."""
    spawned = []
    with patch.object(persist_queue, "_spawn", side_effect=lambda make, n: spawned.append(make)):
        persist(data, *extra, REGION)
    await spawned[0]()


async def _cached(session, key):
    result = await session.execute(select(Cache.value).where(Cache.key == key))
    return result.scalar_one_or_none()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_author_cache_holds_the_stored_description(db_session):
    asin = "B0PROFAUT1"
    base = {"asin": asin, "name": "Some Author", "region": REGION, "image": None}
    await _persist(persist_queue.persist_author_background, {**base, "description": "A real biography"})
    await _persist(persist_queue.persist_author_background, {**base, "description": None})

    cached = await _cached(db_session, author_key(asin, REGION))
    assert cached["description"] == "A real biography"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_series_cache_holds_the_stored_description(db_session):
    asin = "B0PROFSER1"
    base = {"asin": asin, "name": "Some Series", "region": REGION}
    await _persist(persist_queue.persist_series_background, {**base, "description": "A real summary"})
    await _persist(persist_queue.persist_series_background, {**base, "description": None})

    cached = await _cached(db_session, series_key(asin, REGION))
    assert cached["description"] == "A real summary"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_chapters_cache_holds_the_stored_listing(db_session):
    asin = "B0PROFTRK1"
    await upsert_book(db_session, {"asin": asin, "title": "Some Book", "region": REGION})
    full = {"asin": asin, "region": REGION, "chapters": [{"title": "One", "lengthMs": 1000}]}
    empty = {"asin": asin, "region": REGION, "chapters": []}
    await _persist(persist_queue.persist_track_background, asin, full)
    await _persist(persist_queue.persist_track_background, asin, empty)

    cached = await _cached(db_session, chapters_key(asin, REGION))
    assert len(cached["chapters"]) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_chapters_payload_is_cached_when_no_book_row_is_stored(db_session):
    asin = "B0PROFTRK2"
    payload = {"asin": asin, "region": REGION, "chapters": [{"title": "One", "lengthMs": 1000}]}
    await _persist(persist_queue.persist_track_background, asin, payload)

    assert await _cached(db_session, chapters_key(asin, REGION)) == payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nothing_is_cached_when_the_chapters_read_back_fails(db_session):
    asin = "B0PROFTRK3"
    await upsert_book(db_session, {"asin": asin, "title": "Some Book", "region": REGION})
    payload = {"asin": asin, "region": REGION, "chapters": [{"title": "One", "lengthMs": 1000}]}
    with patch.object(persist_queue._books, "get_track", side_effect=RuntimeError("read failed")):
        await _persist(persist_queue.persist_track_background, asin, payload)

    assert await _cached(db_session, chapters_key(asin, REGION)) is None
