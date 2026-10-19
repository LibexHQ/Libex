"""
Two writers that meet the same genres, narrators or co-authors in opposite
orders must not deadlock each other. Every unique-keyed insert takes a row lock
on the key it writes, so the order those rows are touched in has to be the same
for every writer.
"""

import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from libex_core.storage.models import Author, Genre, Narrator, author_book, book_genre, book_narrator
from libex_core.storage.write.books import write_books
from tests.libex_core.storage import _write_cases as cases

pytestmark = pytest.mark.integration

docker = pytest.importorskip("docker")
postgres_container = pytest.importorskip("testcontainers.postgres")


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


if not _docker_available():
    pytest.skip("Docker daemon not available", allow_module_level=True)

ROUNDS = 8
WIDTH = 40


@pytest.fixture(scope="module")
def postgres_url():
    container = postgres_container.PostgresContainer("postgres:16")
    container.start()
    try:
        url = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql+asyncpg://")

        async def prepare():
            engine = create_async_engine(url, poolclass=NullPool)
            async with engine.begin() as connection:
                await connection.run_sync(lambda c: cases.Base.metadata.create_all(c, tables=cases.core_tables()))
            await engine.dispose()

        asyncio.run(prepare())
        yield url
    finally:
        container.stop()


@pytest.fixture
async def factory(postgres_url):
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {', '.join(cases.TABLES)} RESTART IDENTITY CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _book(asin: str, round_: int, order: list[int], kind: str) -> dict:
    """A book carrying only one kind of relationship, so that kind is the sole
    thing the two writers can collide on: a writer that is ordered on an earlier
    kind would otherwise queue the other behind it before it reached this one."""
    book = {"asin": asin, "region": "us", "title": asin}
    if kind == "genres":
        book["genres"] = [{"asin": f"G{round_}-{i:03d}", "name": f"genre {round_} {i}", "type": "Genres"} for i in order]
    elif kind == "narrators":
        book["narrators"] = [{"name": f"narrator {round_} {i:03d}"} for i in order]
    else:
        book["authors"] = [
            {"asin": f"A{round_}-{i:03d}", "name": f"author {round_} {i:03d}", "region": "us"} for i in order
        ]
    return book


async def _write(factory, books: list[dict]) -> None:
    async with factory() as session:
        await write_books(session, books)
        await session.commit()


@pytest.mark.parametrize(
    ("kind", "entity", "link"),
    [("genres", Genre, book_genre), ("narrators", Narrator, book_narrator), ("authors", Author, author_book)],
)
async def test_crossed_orders_do_not_deadlock(factory, kind, entity, link):
    forward = list(range(WIDTH))
    backward = forward[::-1]
    for round_ in range(ROUNDS):
        results = await asyncio.gather(
            _write(factory, [_book(f"B{round_}F", round_, forward, kind)]),
            _write(factory, [_book(f"B{round_}R", round_, backward, kind)]),
            return_exceptions=True,
        )
        assert results == [None, None], results

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(entity)) == ROUNDS * WIDTH
        assert await session.scalar(select(func.count()).select_from(link)) == ROUNDS * WIDTH * 2
