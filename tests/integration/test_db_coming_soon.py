"""
Integration tests for /db/coming-soon windowing.

Verifies the forward window against real Postgres: future releases inside the
window are returned, past releases and far-future placeholders (Audible's
year-2200 "no date yet" sentinel) are excluded, the default order is
soonest-first, and the shared filters still compose.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
import pytest
from sqlalchemy import insert

# Local
from app.db.models import Book
from app.services.db.reader import get_coming_soon_from_db

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
    await _book(session, "B00SOON001", "Five days out", NOW + timedelta(days=5))
    await _book(session, "B00SOON002", "Twenty days out", NOW + timedelta(days=20))
    await _book(session, "B00SOON003", "Forty days out", NOW + timedelta(days=40))
    await _book(session, "B00SOON004", "Already released", NOW - timedelta(days=10))
    # Audible "no date yet" placeholder
    await _book(session, "B00SOON005", "Placeholder TBD", datetime(2200, 1, 1, tzinfo=timezone.utc))
    await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coming_soon_default_30_day_window(db_session):
    """Default 30-day window returns only books releasing in the next 30 days."""
    await _seed_window(db_session)
    books = await get_coming_soon_from_db(db_session, days=30)
    asins = {b["asin"] for b in books}
    assert asins == {"B00SOON001", "B00SOON002"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coming_soon_wider_window_includes_further_out(db_session):
    """A 60-day window picks up the 40-days-out book too."""
    await _seed_window(db_session)
    books = await get_coming_soon_from_db(db_session, days=60)
    asins = {b["asin"] for b in books}
    assert asins == {"B00SOON001", "B00SOON002", "B00SOON003"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coming_soon_excludes_already_released(db_session):
    """Past releases never appear in coming soon."""
    await _seed_window(db_session)
    books = await get_coming_soon_from_db(db_session, days=365)
    asins = {b["asin"] for b in books}
    assert "B00SOON004" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coming_soon_excludes_placeholder(db_session):
    """The year-2200 'no date yet' placeholder is excluded even in the widest window."""
    await _seed_window(db_session)
    books = await get_coming_soon_from_db(db_session, days=365)
    asins = {b["asin"] for b in books}
    assert "B00SOON005" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coming_soon_default_sort_soonest_first(db_session):
    """With no explicit sort, books come back soonest first."""
    await _seed_window(db_session)
    books = await get_coming_soon_from_db(db_session, days=60)
    assert [b["asin"] for b in books] == ["B00SOON001", "B00SOON002", "B00SOON003"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coming_soon_filter_composes(db_session):
    """Shared book filters still narrow the windowed set."""
    await _book(db_session, "B00SOON010", "Upcoming English", NOW + timedelta(days=3), language="english")
    await _book(db_session, "B00SOON011", "Upcoming German", NOW + timedelta(days=3), language="german")
    await db_session.commit()
    books = await get_coming_soon_from_db(db_session, days=30, language="german")
    assert [b["asin"] for b in books] == ["B00SOON011"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coming_soon_explicit_sort_overrides(db_session):
    """Passing a sort field overrides the soonest-first default."""
    await _book(db_session, "B00SOON020", "Sooner lower", NOW + timedelta(days=2), rating=3.0)
    await _book(db_session, "B00SOON021", "Later higher", NOW + timedelta(days=10), rating=4.8)
    await db_session.commit()
    books = await get_coming_soon_from_db(db_session, days=30, sort="rating", order="desc")
    assert [b["asin"] for b in books] == ["B00SOON021", "B00SOON020"]