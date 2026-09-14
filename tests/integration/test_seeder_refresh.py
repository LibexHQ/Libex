"""
Integration tests for the seeder's release-window refresh selection.

Verifies _select_refresh_asins against real Postgres: the proximity tiers pick
the right staleness threshold on both sides of a release, a book stays in the
rotation for POST_RELEASE_WINDOW_DAYS after it releases and drops out beyond
that, the pre- and post-release tiers meet at the release instant with no gap
or overlap, fresh books inside a tier are skipped, a post-release book only
re-enters rotation if Libex had it on record before it released (discovered
after release means it was fetched with settled data and has nothing left for
the window to correct), and results come back oldest-first.
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


async def _book(session, asin, *, release_in_days, updated_days_ago, created_days_ago=None):
    """
    Insert a book releasing `release_in_days` from now, last updated
    `updated_days_ago`.

    `created_days_ago` controls discovery time independently of staleness --
    it defaults to `updated_days_ago` (as if the book has never been updated
    since it was first found), which is what every pre-release-tier test
    wants and what most post-release tests want too. Post-release tests that
    specifically need to control whether the book was discovered before or
    after its own release date pass it explicitly; everything else leaves it
    alone.
    """
    if created_days_ago is None:
        created_days_ago = updated_days_ago
    await session.execute(
        insert(Book).values(
            asin=asin,
            title=f"Book {asin}",
            region="us",
            release_date=NOW + timedelta(days=release_in_days),
            created_at=NOW - timedelta(days=created_days_ago),
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
async def test_released_book_inside_window_is_selected_when_stale(db_session):
    """
    A book past its release date but still inside POST_RELEASE_WINDOW_DAYS is
    reachable by refresh when stale.
    """
    await _book(db_session, "B0DONE01", release_in_days=-5, updated_days_ago=999)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0DONE01" in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_release_tier3_threshold(db_session):
    """
    The (0, 1) tier (0-3 days past release) refreshes daily: stale at >1 day,
    not at 12h.

    Both books were discovered well before their release date, so the
    post-release discovery gate (see test_post_release_gate_*) is satisfied
    for both and the threshold is the only thing distinguishing them.
    """
    await _book(db_session, "B0POST3ST", release_in_days=-1, updated_days_ago=2, created_days_ago=10)
    await _book(db_session, "B0POST3FR", release_in_days=-1, updated_days_ago=0.5, created_days_ago=10)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0POST3ST" in asins
    assert "B0POST3FR" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_release_tier2_threshold(db_session):
    """
    The (-3, 3) tier (3-14 days past release) refreshes every 3 days.

    Both books here were discovered well before their release date, so the
    post-release discovery gate (see test_post_release_gate_*) is satisfied
    for both and the threshold is the only thing distinguishing them.
    """
    await _book(db_session, "B0POST2ST", release_in_days=-7, updated_days_ago=4, created_days_ago=15)
    await _book(db_session, "B0POST2FR", release_in_days=-7, updated_days_ago=2, created_days_ago=15)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0POST2ST" in asins
    assert "B0POST2FR" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_release_tier1_threshold(db_session):
    """
    The (-14, 7) tier (14-30 days past release) refreshes every 7 days.

    Both books here were discovered well before their release date, so the
    post-release discovery gate is satisfied for both and the threshold is
    the only thing distinguishing them.
    """
    await _book(db_session, "B0POST1ST", release_in_days=-20, updated_days_ago=8, created_days_ago=30)
    await _book(db_session, "B0POST1FR", release_in_days=-20, updated_days_ago=6, created_days_ago=30)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0POST1ST" in asins
    assert "B0POST1FR" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_release_gate_excludes_books_discovered_after_release(db_session):
    """
    A post-release tier only refreshes a book Libex had on record BEFORE it
    released: discovered after release, it was fetched fresh with already-
    settled data and has nothing left for the window to correct.

    Same release date, same staleness -- discovery time is the only variable.
    The book discovered before release is selected; the one discovered after
    is not, even though both are equally stale in the same tier.
    """
    await _book(
        db_session, "B0GATEBEF", release_in_days=-10, updated_days_ago=5, created_days_ago=15
    )
    await _book(
        db_session, "B0GATEAFT", release_in_days=-10, updated_days_ago=5, created_days_ago=2
    )
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0GATEBEF" in asins
    assert "B0GATEAFT" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forward_tier_gate_does_not_apply_to_unreleased_books(db_session):
    """
    The created_at < release_date gate is scoped to post-release tiers
    (max_days <= 0) only -- a pre-release book is selected on staleness alone,
    regardless of its created_at, guarding against someone "simplifying" the
    predicate onto the whole tier loop.

    created_at is deliberately set to a moment AFTER this book's own (still
    future) release_date -- nonsensical for a real row, but the cleanest way
    to prove the gate isn't being evaluated here at all: if it were, this book
    would incorrectly fail it and get dropped from a tier it plainly belongs
    in on staleness.
    """
    await _book(
        db_session, "B0FWDGATE", release_in_days=10, updated_days_ago=2, created_days_ago=-15
    )
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0FWDGATE" in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_release_window_outer_bound(db_session):
    """
    POST_RELEASE_WINDOW_DAYS is the outer bound in both directions: a book
    exactly 30 days past release falls outside it (the comparison is a strict
    `>`, so the boundary instant itself is excluded), a book 29 days past
    release is inside it and reachable, and a book 31 days past release is
    outside it no matter how stale -- this is the guard against the outer
    bound quietly regressing to `now` and reducing the whole feature back to
    forward-only selection while the post-release tiers still sit there
    looking correct.
    """
    await _book(db_session, "B0WINEDGE", release_in_days=-30, updated_days_ago=999)
    await _book(db_session, "B0WININ", release_in_days=-29, updated_days_ago=999)
    await _book(db_session, "B0WINOUT", release_in_days=-31, updated_days_ago=999)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0WINEDGE" not in asins
    assert "B0WININ" in asins
    assert "B0WINOUT" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tier_chain_meets_at_release_instant_with_no_gap_or_overlap(db_session):
    """
    A book at the release instant falls in exactly the (0, 1) tier: selected
    when stale by more than a day, not when fresher -- the same threshold as
    the tier immediately ahead of it, so this is the one boundary the daily
    thresholds alone can't distinguish, but it confirms the chain has no gap
    at the instant itself (release_date == now lands in a tier at all).
    """
    await _book(db_session, "B0INSTANT", release_in_days=0, updated_days_ago=2)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0INSTANT" in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tier_chain_meets_at_minus14_with_no_gap_or_overlap(db_session):
    """
    A book exactly 14 days past release belongs to the (-14, 7) tier (upper
    bound inclusive), not the (-3, 3) tier next to it: selected once stale
    past the 7-day threshold, but NOT selected at 5 days stale even though
    that would clear the neighboring tier's 3-day threshold -- if it were,
    the chain would be overlapping rather than meeting cleanly at the border.

    Both books were discovered well before release, so the post-release
    discovery gate is satisfied for both and doesn't interfere with this
    boundary check.
    """
    await _book(db_session, "B0BOUND14A", release_in_days=-14, updated_days_ago=8, created_days_ago=25)
    await _book(db_session, "B0BOUND14B", release_in_days=-14, updated_days_ago=5, created_days_ago=25)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0BOUND14A" in asins
    assert "B0BOUND14B" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tier_chain_meets_at_minus3_with_no_gap_or_overlap(db_session):
    """
    A book exactly 3 days past release belongs to the (-3, 3) tier (upper
    bound inclusive), not the (0, 1) tier next to it: selected once stale past
    the 3-day threshold, but NOT selected at 2 days stale even though that
    would clear the neighboring tier's 1-day threshold.

    Both books were discovered well before their release date, so the
    post-release discovery gate is satisfied for both and the threshold is
    the only thing distinguishing them.
    """
    await _book(db_session, "B0BOUND3A", release_in_days=-3, updated_days_ago=4, created_days_ago=10)
    await _book(db_session, "B0BOUND3B", release_in_days=-3, updated_days_ago=2, created_days_ago=10)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0BOUND3A" in asins
    assert "B0BOUND3B" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_max_days_zero_is_a_real_upper_bound_not_a_falsy_one(db_session):
    """
    The (0, 1) tier's 0 is a real upper bound, the release instant, and must
    be compared with `is None` rather than truth-tested -- 0 is falsy, so a
    truthiness check silently treats it like the unbounded final tier and
    drops its upper limit, leaking the 1-day threshold onto everything ahead
    of it in the chain.

    A book 300 days out belongs in the (365, 60) tier and is nowhere near its
    60-day threshold at 2 days stale, so it must not be selected. If the (0,
    1) tier's window were truth-tested and lost its upper bound, this book
    would incorrectly satisfy that leaked, unbounded, 1-day-threshold
    condition instead.
    """
    await _book(db_session, "B0FARMILD", release_in_days=300, updated_days_ago=2)
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0FARMILD" not in asins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_null_release_date_excluded(db_session):
    """A book with no release date is never selected."""
    await db_session.execute(
        insert(Book).values(
            asin="B0NULLDT",
            title="No release date",
            region="us",
            release_date=None,
            created_at=NOW - timedelta(days=999),
            updated_at=NOW - timedelta(days=999),
        )
    )
    await db_session.commit()
    asins = await _select_refresh_asins(db_session, "us", NOW)
    assert "B0NULLDT" not in asins


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