"""
Integration tests for scripts/backfill_chapters.py against books and tracks
keyed (asin, region), on real Postgres.

The same book ASIN can be stored for two regions as two rows. The walk, the
stamp and the track write all have to treat them as two books: a keyset page
that ended between them must not skip the second, a stamp for one must not
stamp the other as asked about, and one region's listing must not overwrite
the other's.
"""

# Third party
import pytest
from sqlalchemy import insert, select

# Local
from app.db.models import Book, Track
from scripts.backfill_chapters import (
    _advance_cursor,
    _mark_checked,
    _read_page,
    _store_chapters,
)

ASIN = "B00TWOREGNS"


async def _seed_two_regions(session):
    await session.execute(
        insert(Book),
        [
            {"asin": ASIN, "title": "Same title", "region": "us"},
            {"asin": ASIN, "title": "Same title", "region": "de"},
        ],
    )
    await session.commit()


def _listing(titles):
    return {"chapters": [{"title": t} for t in titles]}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_walk_paged_one_row_at_a_time_visits_both_regions_of_an_asin(db_session):
    await _seed_two_regions(db_session)

    seen = []
    cursor = None
    for _ in range(5):
        rows = await _read_page(db_session, cursor, 1)
        if not rows:
            break
        seen.append((rows[0][0], rows[0][1]))
        cursor, _wrapped, _done = _advance_cursor(rows, cursor, False)

    # Region sorts in the enum's declared order, not alphabetically; what
    # matters is that the walk saw each row once and none was stepped over.
    assert len(seen) == 2
    assert set(seen) == {(ASIN, "de"), (ASIN, "us")}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stamping_one_region_leaves_the_other_unasked(db_session):
    await _seed_two_regions(db_session)

    await _mark_checked(db_session, ASIN, "de")

    rows = await db_session.execute(
        select(Book.region, Book.chapters_checked_at).order_by(Book.region)
    )
    stamps = dict(rows.all())
    assert stamps["de"] is not None
    assert stamps["us"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_each_regions_listing_is_its_own_track_row(db_session):
    await _seed_two_regions(db_session)

    await _store_chapters(db_session, ASIN, _listing(["Eins", "Zwei"]), region="de")
    await _store_chapters(db_session, ASIN, _listing(["One"]), region="us")

    rows = await db_session.execute(
        select(Track.region, Track.chapters).order_by(Track.region)
    )
    stored = {region: [c["title"] for c in chapters["chapters"]] for region, chapters in rows.all()}
    assert stored == {"de": ["Eins", "Zwei"], "us": ["One"]}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_chapterless_answer_in_one_region_keeps_that_regions_listing_only(db_session):
    await _seed_two_regions(db_session)
    await _store_chapters(db_session, ASIN, _listing(["Eins", "Zwei"]), region="de")
    await _store_chapters(db_session, ASIN, _listing(["One"]), region="us")

    await _store_chapters(db_session, ASIN, _listing([]), region="de")

    rows = await db_session.execute(
        select(Track.region, Track.chapters).order_by(Track.region)
    )
    stored = {region: len(chapters["chapters"]) for region, chapters in rows.all()}
    assert stored == {"de": 2, "us": 1}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_listing_confirms_chapters_for_its_own_region_only(db_session):
    await _seed_two_regions(db_session)

    await _store_chapters(db_session, ASIN, _listing(["Eins"]), region="de")

    rows = await db_session.execute(
        select(Book.region, Book.chapters_confirmed_at).order_by(Book.region)
    )
    stamps = dict(rows.all())
    assert stamps["de"] is not None
    assert stamps["us"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_empty_answer_confirms_chapters_for_its_own_region_only(db_session):
    await _seed_two_regions(db_session)

    await _mark_checked(db_session, ASIN, "de", confirmed=True)

    rows = await db_session.execute(
        select(Book.region, Book.chapters_checked_at, Book.chapters_confirmed_at).order_by(
            Book.region
        )
    )
    by_region = {region: (checked, confirmed) for region, checked, confirmed in rows.all()}
    assert by_region["de"][0] is not None and by_region["de"][1] is not None
    assert by_region["us"] == (None, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_plain_mark_checked_confirms_nothing(db_session):
    await _seed_two_regions(db_session)

    await _mark_checked(db_session, ASIN, "us")

    rows = await db_session.execute(
        select(Book.chapters_checked_at, Book.chapters_confirmed_at).where(Book.region == "us")
    )
    checked, confirmed = rows.one()
    assert checked is not None
    assert confirmed is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_confirmation_stamp_never_moves_backwards(db_session):
    await _seed_two_regions(db_session)
    await _mark_checked(db_session, ASIN, "de", confirmed=True)
    first = (
        await db_session.execute(
            select(Book.chapters_confirmed_at).where(Book.region == "de")
        )
    ).scalar_one()

    await _store_chapters(db_session, ASIN, _listing(["Eins"]), region="de")

    later = (
        await db_session.execute(
            select(Book.chapters_confirmed_at).where(Book.region == "de")
        )
    ).scalar_one()
    assert later >= first
