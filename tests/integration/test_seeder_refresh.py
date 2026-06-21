"""
Integration tests for the seeder's upcoming-refresh selection.

Verifies _select_refresh_asins against real Postgres: the proximity tiers pick
the right staleness threshold, already-released books are excluded, fresh books
inside a tier are skipped, and results come back oldest-first.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
import pytest
from sqlalchemy import insert

# Local
from app.db.models import Book
from app.services.seeder import _select_refresh_asins

NOW = datetime.now(timezone.utc)


async def _book(session, asin, *, release_in_days, updated_days_ago):
    """Insert a book releasing `release_in_days` from now, last updated `updated_days_ago`."""
    await session.execute(
        insert(Book).values(
            asin=asin,
            title=f"Book {asin}",
            region="us",
            release_date=NOW + timedelta(days=release_in_days),
            created_at=NOW - timedelta(days=updated_days_ago),
            updated_at=NOW - timedelta(days=updated_days_ago),
        )
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_near_release_refreshes_when_a_day_stale(db_session):
    """Within 14 days of release, a book stale by >1 day is selected."""
    await _book(db_session, "B0NEAR01", release_in_days=10, updated_days_ago=2)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0NEAR01" in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_near_release_skipped_when_fresh(db_session):
    """Within 14 days but updated only hours ago (well under 1 day) is skipped."""
    await _book(db_session, "B0NEAR02", release_in_days=10, updated_days_ago=0)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0NEAR02" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_far_future_uses_slow_cadence(db_session):
    """A book ~200 days out (180-365 tier, 60-day threshold) is skipped at 30 days stale."""
    await _book(db_session, "B0FAR01", release_in_days=200, updated_days_ago=30)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0FAR01" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_far_future_refreshes_when_past_slow_threshold(db_session):
    """That same ~200-day-out book is selected once stale beyond 60 days."""
    await _book(db_session, "B0FAR02", release_in_days=200, updated_days_ago=70)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0FAR02" in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_beyond_a_year_slow_cadence(db_session):
    """A book 500 days out (beyond-year tier, 90-day threshold) is skipped at 60 days stale."""
    await _book(db_session, "B0YEAR01", release_in_days=500, updated_days_ago=60)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0YEAR01" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_released_books_never_selected(db_session):
    """Already-released books are never refreshed, no matter how stale."""
    await _book(db_session, "B0DONE01", release_in_days=-5, updated_days_ago=999)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0DONE01" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_region_scoped(db_session):
    """Only books in the requested region are selected."""
    await _book(db_session, "B0REG01", release_in_days=10, updated_days_ago=5)
    await db_session.execute(
        insert(Book).values(
            asin="B0REG02",
            title="Other region",
            region="uk",
            release_date=NOW + timedelta(days=10),
            created_at=NOW - timedelta(days=5),
            updated_at=NOW - timedelta(days=5),
        )
    )
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0REG01" in asins
    assert "B0REG02" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ordered_oldest_first(db_session):
    """Results come back oldest updated_at first so the most stale get priority."""
    await _book(db_session, "B0ORD_NEW", release_in_days=10, updated_days_ago=2)
    await _book(db_session, "B0ORD_OLD", release_in_days=10, updated_days_ago=30)
    await _book(db_session, "B0ORD_MID", release_in_days=10, updated_days_ago=10)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    # all three qualify; oldest update first
    assert asins == ["B0ORD_OLD", "B0ORD_MID", "B0ORD_NEW"]