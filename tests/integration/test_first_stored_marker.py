"""
The first-stored marker on books and series, against real Postgres.

Whether a new row is the primary one of its ASIN is decided when it is
inserted, from whether another region already holds the ASIN. Two transactions
that insert the same ASIN for different regions cannot see each other's
uncommitted row, so without the writer's per-ASIN advisory lock both would
decide they were first. The test holds one transaction open, starts the other,
and checks what each decided; the same run with the lock removed is the
control that shows the test would notice.
"""

# Standard library
import asyncio
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Local
import app.db.session as db_session_module
from app.services.db.writer import write_books

pytestmark = pytest.mark.integration

ASIN = "0000000001"


def _product(region: str) -> dict:
    return {
        "asin": ASIN,
        "region": region,
        "title": f"title {region}",
        "series": [{"asin": "B0SERIES01", "name": "Saga"}],
    }


async def _marks(session: AsyncSession, table: str, asin: str) -> dict[str, bool]:
    rows = await session.execute(
        text(f"SELECT region::text, is_primary FROM {table} WHERE asin = :a"), {"a": asin}
    )
    return {region: primary for region, primary in rows.all()}


async def _a_writer_is_waiting_on_an_advisory_lock(session: AsyncSession) -> bool:
    waiting = await session.execute(
        text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
    )
    return waiting.scalar() > 0


async def _race(db_session: AsyncSession) -> None:
    factory = async_sessionmaker(db_session_module.engine, expire_on_commit=False)
    inserted = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with factory() as session:
            await write_books(session, [_product("us")])
            inserted.set()
            await release.wait()
            await session.commit()

    async def second() -> None:
        await inserted.wait()
        async with factory() as session:
            await write_books(session, [_product("uk")])
            await session.commit()

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await inserted.wait()
    # Give the second writer time to reach the lock, or to finish without one.
    for _ in range(40):
        if await _a_writer_is_waiting_on_an_advisory_lock(db_session) or tasks[1].done():
            break
        await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(*tasks)
    await db_session.rollback()


async def test_two_regions_inserting_one_asin_at_once_leave_one_primary(db_session):
    await _race(db_session)

    assert await _marks(db_session, "books", ASIN) == {"us": True, "uk": False}
    assert await _marks(db_session, "series", "B0SERIES01") == {"us": True, "uk": False}


async def test_without_the_lock_both_would_claim_it(db_session):
    with patch("libex_core.storage.write.books.lock_asins", new=AsyncMock()):
        await _race(db_session)

    assert await _marks(db_session, "books", ASIN) == {"us": True, "uk": True}
