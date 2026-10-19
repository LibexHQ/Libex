"""
A name Audible sends as null is a missing name, not a crash: the writers skip
the row and keep the rest of the book.
"""

# Third party
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Local
from libex_core.storage.dialect import configure_sqlite
from libex_core.storage.models import Author, Narrator
from libex_core.storage.write.books import write_books
from libex_core.storage.write.entities import upsert_author, upsert_narrator, write_author_profile
from tests.libex_core.storage import _write_cases as cases


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'libex.db'}")
    configure_sqlite(engine)
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: cases.Base.metadata.create_all(c, tables=cases.core_tables()))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _count(session, entity) -> int:
    return await session.scalar(select(func.count()).select_from(entity))


async def test_a_book_with_null_author_and_narrator_names_is_still_written(factory):
    book = {
        "asin": "B0NULLNAME",
        "region": "us",
        "title": "kept",
        "authors": [
            {"asin": "A1", "name": None, "region": "us"},
            {"asin": "A2", "name": "Real Author", "region": "us"},
        ],
        "narrators": [{"name": None}, {"name": "Real Narrator"}],
    }
    async with factory() as session:
        await write_books(session, [book])
        await session.commit()
    async with factory() as session:
        assert (await session.scalars(select(Narrator.name))).all() == ["Real Narrator"]
        assert "Real Author" in (await session.scalars(select(Author.name))).all()


async def test_the_single_row_upserts_skip_a_null_name(factory):
    async with factory() as session:
        assert await upsert_narrator(session, {"name": None}) is None
        assert await upsert_author(session, {"asin": "A1", "name": None, "region": "us"}) is None
        assert await write_author_profile(session, {"asin": "A1", "name": None, "region": "us"}) is None
        assert await _count(session, Narrator) == 0
        assert await _count(session, Author) == 0
