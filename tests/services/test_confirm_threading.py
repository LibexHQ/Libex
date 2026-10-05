"""
The confirmation stamp is requested by exactly the hosted call sites whose
answer is a real Audible answer for the row's own entity, and by none other.

Each test drives one call site with the layer below it replaced and asserts
what that layer was asked: confirm=True where Audible just answered for the
entity, nothing where it did not. Dropping the keyword at any site fails the
test for that site.
"""

# Standard library
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest
from sqlalchemy.dialects import postgresql

# Local
from app.services.db import persist_queue as pq

ASIN = "B08G9PRS1K"
CHAPTERS_DATA = {
    "content_metadata": {"chapter_info": {"runtime_length_ms": 1000, "chapters": [
        {"length_ms": 1000, "start_offset_ms": 0, "title": "One"},
    ]}}
}


# ============================================================
# THE CALL SITES
# ============================================================

@pytest.mark.asyncio
async def test_a_product_fetch_asks_for_the_book_stamp():
    from app.services.audible.books import get_books_by_asins

    product = {
        "asin": ASIN, "title": "Dune", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [],
        "publication_datetime": "2021-01-01T00:00:00Z",
    }
    with patch("app.services.audible.books.audible_get", return_value={"product": product}), \
         patch("app.services.audible.books.persist_books_background") as persist, \
         patch("app.services.audible.books.cache.get", return_value=None):
        await get_books_by_asins([ASIN], "us", AsyncMock())

    assert persist.call_args.kwargs == {"confirm": True}


@pytest.mark.asyncio
async def test_a_chapters_listing_asks_for_the_chapters_stamp():
    from app.services.audible.books import get_chapters

    with patch("app.services.audible.books.audible_get", return_value=CHAPTERS_DATA), \
         patch("app.services.audible.books.persist_track_background") as persist:
        await get_chapters(ASIN, "us", AsyncMock())

    assert persist.call_args.kwargs == {"confirm": True}


@pytest.mark.asyncio
async def test_a_series_profile_fetch_asks_for_the_series_stamp():
    from app.services.audible.series import get_series

    response = {
        "response_groups": ["product_attrs", "product_desc"],
        "product": {"asin": "B00SERIES1", "title": "Saga", "publisher_summary": "x"},
    }
    with patch("app.services.audible.series.audible_get", return_value=response), \
         patch("app.services.audible.series.persist_series_background") as persist, \
         patch("app.services.audible.series.cache.get", return_value=None):
        await get_series("B00SERIES1", "us", AsyncMock())

    assert persist.call_args.kwargs == {"confirm": True}


@pytest.mark.asyncio
async def test_an_author_profile_fetch_asks_for_the_author_stamp():
    from app.services.audible.authors import get_author

    data = {"contributor": {"name": "Frank Herbert", "bio": "x", "profile_image_url": None}}
    with patch("app.services.audible.authors.profile.audible_get", return_value=data), \
         patch("app.services.audible.authors.profile.persist_author_background") as persist, \
         patch("app.services.audible.authors.profile.cache.get", return_value=None):
        await get_author("B000APF21M", "us", AsyncMock())

    assert persist.call_args.kwargs == {"confirm": True}


@pytest.mark.asyncio
async def test_resolving_an_author_name_from_the_profile_asks_for_the_author_stamp():
    from app.services.audible.authors import _resolve_author_name

    data = {"contributor": {"name": "Frank Herbert", "bio": "x", "profile_image_url": None}}
    with patch("app.services.audible.authors.get_author_from_db", new=AsyncMock(return_value=None)), \
         patch("app.services.audible.authors._fetch_author_details", new=AsyncMock(return_value=data)), \
         patch("app.services.audible.authors.persist_author_background") as persist:
        assert await _resolve_author_name("B000APF21M", "us", AsyncMock()) == "Frank Herbert"

    assert persist.call_args.kwargs == {"confirm": True}


# ============================================================
# THE QUEUE PASS-THROUGH
# ============================================================

async def _run_queued(persist, *args, **kwargs):
    """Calls a persist_*_background entry point and runs the task it would
    have spawned, with the session, the permit and the cache write replaced."""
    spawned = []
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=AsyncMock())
    session.__aexit__ = AsyncMock(return_value=False)
    with patch.object(pq, "_spawn", side_effect=lambda make, n: spawned.append(make)), \
         patch.object(pq, "_BackgroundSession", lambda: session), \
         patch.object(pq, "_get_bg_write_semaphore", lambda: asyncio.Semaphore(1)), \
         patch.object(pq.cache, "set", new=AsyncMock()):
        persist(*args, **kwargs)
        await spawned[0]()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", [True, False])
async def test_the_queued_series_write_passes_confirm_on(confirm):
    with patch.object(pq, "upsert_series_profile", new=AsyncMock()) as write:
        await _run_queued(
            pq.persist_series_background, {"asin": "B00SERIES1", "name": "S"}, "us", confirm=confirm
        )
    assert write.await_args.kwargs == {"confirm": confirm}


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", [True, False])
async def test_the_queued_author_write_passes_confirm_on(confirm):
    with patch.object(pq, "upsert_author_profile", new=AsyncMock()) as write:
        await _run_queued(
            pq.persist_author_background, {"asin": "B000APF21M", "name": "A"}, "us", confirm=confirm
        )
    assert write.await_args.kwargs == {"confirm": confirm}


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", [True, False])
async def test_the_queued_track_write_passes_confirm_on(confirm):
    with patch.object(pq, "upsert_track", new=AsyncMock()) as write:
        await _run_queued(
            pq.persist_track_background, ASIN, {"chapters": []}, "us", confirm=confirm
        )
    assert write.await_args.kwargs == {"region": "us", "confirm": confirm}


@pytest.mark.asyncio
async def test_the_queued_writes_default_to_no_stamp():
    with patch.object(pq, "upsert_series_profile", new=AsyncMock()) as series:
        await _run_queued(pq.persist_series_background, {"asin": "B00SERIES1"}, "us")
    with patch.object(pq, "upsert_author_profile", new=AsyncMock()) as author:
        await _run_queued(pq.persist_author_background, {"asin": "B000APF21M"}, "us")
    with patch.object(pq, "upsert_track", new=AsyncMock()) as track:
        await _run_queued(pq.persist_track_background, ASIN, {}, "us")

    assert series.await_args.kwargs == {"confirm": False}
    assert author.await_args.kwargs == {"confirm": False}
    assert track.await_args.kwargs == {"region": "us", "confirm": False}


# ============================================================
# THE SEEDER'S CHAPTERS MARK
# ============================================================

def _update_of(session):
    call = session.execute.await_args
    statement = call.args[0]
    params = call.args[1] if len(call.args) > 1 else {}
    return statement.compile(dialect=postgresql.dialect()), params


@pytest.mark.asyncio
async def test_a_confirmed_mark_stamps_the_chapters_with_the_instant_it_checked():
    from app.services.audible.books import _mark_chapters_checked

    session = AsyncMock()
    await _mark_chapters_checked(session, ASIN, "us", confirmed=True)

    compiled, params = _update_of(session)
    sql = str(compiled)
    assert "chapters_checked_at" in sql
    assert "chapters_confirmed_at=greatest(books.chapters_confirmed_at" in sql.replace(" ", "")
    assert set(params) == {"stamp"}
    assert isinstance(params["stamp"], datetime) and params["stamp"].tzinfo == timezone.utc
    assert params["stamp"] == compiled.params["chapters_checked_at"]
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unconfirmed_mark_leaves_the_chapters_stamp_alone():
    from app.services.audible.books import _mark_chapters_checked

    session = AsyncMock()
    await _mark_chapters_checked(session, ASIN, "us")

    compiled, params = _update_of(session)
    assert "chapters_checked_at" in str(compiled)
    assert "chapters_confirmed_at" not in str(compiled)
    assert params == {}


@pytest.mark.asyncio
async def test_the_seeder_confirms_an_empty_answer_and_nothing_else():
    from app.services.audible.books import fetch_and_store_chapters
    from libex_core.exceptions import NotFoundException

    def session_with_book():
        session = AsyncMock()
        found = MagicMock()
        found.first.return_value = (ASIN,)
        session.execute.return_value = found
        return session

    async def run(get_side, valid=ASIN):
        marks = []

        async def mark(session, asin, region, **kw):
            marks.append(kw)

        with patch("app.services.audible.books._mark_chapters_checked", new=mark), \
             patch("app.services.audible.books.audible_get", **get_side), \
             patch("app.services.audible.books.upsert_track", new=AsyncMock()):
            result = await fetch_and_store_chapters(valid, "us", session_with_book())
        return result, marks

    assert await run({"side_effect": NotFoundException()}) == ("not_found", [{"confirmed": True}])
    assert await run({"return_value": {"content_metadata": {}}}) == ("none", [{"confirmed": True}])
    # A listing is confirmed by the track write, so the mark does not claim it.
    assert await run({"return_value": CHAPTERS_DATA}) == ("stored", [{}])
    # Never sent to Audible, so it answers nothing.
    assert await run({"return_value": CHAPTERS_DATA}, valid="nope") == ("not_found", [{}])
