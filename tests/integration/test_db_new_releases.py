"""
Integration tests for /db/new-releases windowing.

Verifies the date window against real Postgres: books inside the look-back
window are returned, books outside it (older or future pre-orders) are excluded,
the default order is newest-first, and the shared filters still compose.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
import pytest
from sqlalchemy import insert

# Local
from app.db.models import Book
from app.services.db.reader import get_new_releases_from_db

NOW = datetime.now(timezone.utc)


async def _book(session, asin, title, released, *, rating=None, language="english"):
    await session.execute(
        insert(Book).values(
            asin=asin,
            title=title,
            region="us",
            release_date=released,
            rating=rating,
            language=language,
            created_at=NOW,
            updated_at=NOW,
        )
    )


async def _seed_window(session):
    await _book(session, "B00NEW001", "Five days ago", NOW - timedelta(days=5))
    await _book(session, "B00NEW002", "Twenty days ago", NOW - timedelta(days=20))
    await _book(session, "B00NEW003", "Forty days ago", NOW - timedelta(days=40))
    await _book(session, "B00NEW004", "Pre-order next month", NOW + timedelta(days=30))
    await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_releases_default_30_day_window(db_session):
    """Default 30-day window returns only books released in the last 30 days."""
    await _seed_window(db_session)
    books = await get_new_releases_from_db(db_session, days=30)
    asins = {b["asin"] for b in books}
    assert asins == {"B00NEW001", "B00NEW002"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_releases_wider_window_includes_older(db_session):
    """A 60-day window picks up the 40-days-ago book too."""
    await _seed_window(db_session)
    books = await get_new_releases_from_db(db_session, days=60)
    asins = {b["asin"] for b in books}
    assert asins == {"B00NEW001", "B00NEW002", "B00NEW003"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_releases_excludes_future_preorders(db_session):
    """Future-dated pre-orders never appear, even in the widest window."""
    await _seed_window(db_session)
    books = await get_new_releases_from_db(db_session, days=365)
    asins = {b["asin"] for b in books}
    assert "B00NEW004" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_releases_default_sort_newest_first(db_session):
    """With no explicit sort, books come back newest first."""
    await _seed_window(db_session)
    books = await get_new_releases_from_db(db_session, days=60)
    assert [b["asin"] for b in books] == ["B00NEW001", "B00NEW002", "B00NEW003"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_releases_filter_composes(db_session):
    """Shared book filters still narrow the windowed set."""
    await _book(db_session, "B00NEW010", "Recent English", NOW - timedelta(days=3), language="english")
    await _book(db_session, "B00NEW011", "Recent German", NOW - timedelta(days=3), language="german")
    await db_session.commit()
    books = await get_new_releases_from_db(db_session, days=30, language="german")
    assert [b["asin"] for b in books] == ["B00NEW011"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_releases_explicit_sort_overrides(db_session):
    """Passing a sort field overrides the newest-first default."""
    await _book(db_session, "B00NEW020", "Lower", NOW - timedelta(days=2), rating=3.0)
    await _book(db_session, "B00NEW021", "Higher", NOW - timedelta(days=10), rating=4.8)
    await db_session.commit()
    books = await get_new_releases_from_db(db_session, days=30, sort="rating", order="desc")
    assert [b["asin"] for b in books] == ["B00NEW021", "B00NEW020"]