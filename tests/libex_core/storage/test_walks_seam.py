"""
write_walk_result, delete_walk_result and get_walk_result on SQLite, and on
Postgres under the integration marker.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.base import Base
from libex_core.storage.read.walks import get_walk_result
from libex_core.storage.store import LocalStore
from libex_core.storage.walk_limits import MAX_WALK_ASINS
from libex_core.storage.write import delete_walk_result, write_walk_result
from tests.libex_core.storage._support import core_tables

pytest.importorskip("aiosqlite")

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
ASINS = ["B0000000B2", "B0000000A1", "B0000000C3"]


@pytest_asyncio.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
async def session(request):
    if request.param == "sqlite":
        store = LocalStore("sqlite+aiosqlite://")
        await store.upgrade()
        await store.open()
        async with store.write() as writer:
            yield writer
        await store.close()
        return
    url = request.getfixturevalue("postgres_url")
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=core_tables())
        await conn.run_sync(Base.metadata.create_all, tables=core_tables())
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


async def _write(session, at=T0, **over):
    args = dict(
        kind="series_books", asin="B0SERIES01", region="us", book_asins=list(ASINS),
        complete=True, incomplete_reasons=[], at=at,
    )
    args.update(over)
    return await write_walk_result(session, **args)


async def _read(session, kind="series_books", asin="B0SERIES01", region="us"):
    return await get_walk_result(session, kind, asin, region)


async def test_absent_is_none(session):
    assert await _read(session) is None


async def test_write_then_read_keeps_order_and_columns(session):
    assert await _write(session, incomplete_reasons=["x"], complete=False) is True
    row = await _read(session)
    assert row == {
        "book_asins": ASINS, "complete": False, "incomplete_reasons": ["x"], "confirmed_at": T0,
    }
    assert row["confirmed_at"].tzinfo is not None


async def test_newer_replaces_every_column(session):
    await _write(session)
    assert await _write(session, at=T0 + timedelta(seconds=1), book_asins=["B0000000Z9"],
                        complete=False, incomplete_reasons=["r"]) is True
    row = await _read(session)
    assert row["book_asins"] == ["B0000000Z9"] and row["complete"] is False
    assert row["incomplete_reasons"] == ["r"]
    assert row["confirmed_at"] == T0 + timedelta(seconds=1)


async def test_older_and_equal_do_not_overwrite(session):
    await _write(session)
    assert await _write(session, at=T0 - timedelta(seconds=1), book_asins=["B0000000Z9"]) is False
    assert await _write(session, at=T0, book_asins=["B0000000Z9"]) is False
    assert (await _read(session))["book_asins"] == ASINS


async def test_future_dated_row_is_overwritten(session):
    future = datetime.now(timezone.utc) + timedelta(days=30)
    await _write(session, at=future)
    assert await _write(session, at=T0, book_asins=["B0000000Z9"]) is True
    assert (await _read(session))["book_asins"] == ["B0000000Z9"]


async def test_key_isolation_by_region_and_kind(session):
    await _write(session)
    await _write(session, region="uk", book_asins=["B0000000U1"])
    await _write(session, kind="author_books", book_asins=["B0000000K1"])
    assert (await _read(session))["book_asins"] == ASINS
    assert (await _read(session, region="uk"))["book_asins"] == ["B0000000U1"]
    assert (await _read(session, kind="author_books"))["book_asins"] == ["B0000000K1"]


async def test_invalid_entry_forces_incomplete_and_is_dropped(session):
    await _write(session, book_asins=["B0000000A1", "bad", "b0000000a2", "B0000000A1", 5])
    row = await _read(session)
    assert row["book_asins"] == ["B0000000A1", "B0000000A2"]
    assert row["complete"] is False


async def test_duplicates_alone_do_not_force_incomplete(session):
    await _write(session, book_asins=["B0000000A1", "B0000000A1"])
    row = await _read(session)
    assert row["book_asins"] == ["B0000000A1"] and row["complete"] is True


async def test_over_cap_is_truncated_and_incomplete(session):
    many = [f"B{n:09d}" for n in range(MAX_WALK_ASINS + 3)]
    await _write(session, book_asins=many)
    row = await _read(session)
    assert len(row["book_asins"]) == MAX_WALK_ASINS and row["complete"] is False


@pytest.mark.parametrize("over", [
    {"kind": "other"}, {"region": "xx"}, {"asin": "nope"}, {"asin": "ﬁ" * 9},
    {"at": datetime(2026, 3, 1)},
])
async def test_bad_arguments_raise_before_writing(session, over):
    with pytest.raises(ValueError):
        await _write(session, **over)
    assert await _read(session) is None


async def test_delete_guard(session):
    await _write(session)
    assert await delete_walk_result(session, kind="series_books", asin="B0SERIES01",
                                    region="us", at=T0 - timedelta(seconds=1)) is False
    assert await _read(session) is not None
    assert await delete_walk_result(session, kind="series_books", asin="B0SERIES01",
                                    region="us", at=T0 + timedelta(seconds=1)) is True
    assert await _read(session) is None


async def test_delete_removes_future_dated_and_is_keyed(session):
    await _write(session, at=datetime.now(timezone.utc) + timedelta(days=30))
    await _write(session, region="uk")
    assert await delete_walk_result(session, kind="series_books", asin="B0SERIES01",
                                    region="us", at=T0) is True
    assert await _read(session, region="uk") is not None


async def test_delete_rejects_bad_arguments(session):
    with pytest.raises(ValueError):
        await delete_walk_result(session, kind="x", asin="B0SERIES01", region="us", at=T0)
    with pytest.raises(ValueError):
        await delete_walk_result(session, kind="series_books", asin="B0SERIES01",
                                 region="us", at=datetime(2026, 1, 1))
