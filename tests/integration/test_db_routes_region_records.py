"""
/db routes against real Postgres when one ASIN is stored under two regions.

A book or series is keyed by (asin, region), so the same ASIN can have a
record in us and one in uk. The single-record routes take an optional
`region`; without it they answer with the first-stored record, and a region
the ASIN was never stored under is a 404. Unfiltered lists give one row per
ASIN, SKU lookups give every region's variant, and /db/stats counts both.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

# Local
from app.db.models import Author, Book, Series, Track, author_book
from app.db.session import get_session
from app.main import app

ASIN = "B0REGIONKY"
SERIES_ASIN = "B0REGSERIE"
AUTHOR_ASIN = "B0REGAUTHR"
SKU = "BK_REGION_KEYS"
FIRST = datetime(2024, 1, 1, tzinfo=timezone.utc)
SECOND = FIRST + timedelta(days=30)


@pytest.fixture
async def client(db_session):
    async def _session():
        yield db_session

    app.dependency_overrides[get_session] = _session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


async def _seed(session, *, first="us", second="uk"):
    """One book, series and chapter listing under each region; `first` was
    stored earlier than `second`."""
    for region, stamp in ((first, FIRST), (second, SECOND)):
        await session.execute(insert(Book).values(
            asin=ASIN, region=region, title=f"Title {region}", sku_group=SKU,
            created_at=stamp, updated_at=stamp, is_primary=region == first,
        ))
        await session.execute(insert(Series).values(
            asin=SERIES_ASIN, region=region, title=f"Series {region}",
            created_at=stamp, updated_at=stamp, is_primary=region == first,
        ))
        await session.execute(insert(Track).values(
            asin=ASIN, region=region, chapters={"chapters": [{"title": region}]},
            created_at=stamp, updated_at=stamp,
        ))
    author_id = (await session.execute(insert(Author).values(
        asin=AUTHOR_ASIN, region="us", name="An Author",
        created_at=FIRST, updated_at=FIRST,
    ).returning(Author.id))).scalar_one()
    for region in (first, second):
        await session.execute(insert(author_book).values(
            author_id=author_id, book_asin=ASIN, book_region=region,
        ))
    await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, field, key",
    [
        (f"/db/book/{ASIN}", "region", "region"),
        (f"/db/series/{SERIES_ASIN}", "region", "region"),
    ],
)
async def test_single_record_default_region_and_miss(client, db_session, path, field, key):
    await _seed(db_session)

    default = await client.get(path)
    assert default.status_code == 200
    assert default.json()[key] == "us"

    uk = await client.get(path, params={"region": "uk"})
    assert uk.status_code == 200
    assert uk.json()[key] == "uk"

    miss = await client.get(path, params={"region": "de"})
    assert miss.status_code == 404
    assert miss.json()["code"] == "not_in_libex"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_default_is_first_stored_not_alphabetical(client, db_session):
    """uk sorts after us alphabetically only by accident of the pair; storing
    uk first must make uk the default."""
    await _seed(db_session, first="uk", second="us")

    book = await client.get(f"/db/book/{ASIN}")
    assert book.json()["region"] == "uk"
    series = await client.get(f"/db/series/{SERIES_ASIN}")
    assert series.json()["region"] == "uk"
    chapters = await client.get(f"/db/book/{ASIN}/chapters")
    assert chapters.json()["chapters"][0]["title"] == "uk"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chapters_default_region_and_miss(client, db_session):
    await _seed(db_session)
    path = f"/db/book/{ASIN}/chapters"

    assert (await client.get(path)).json()["chapters"][0]["title"] == "us"
    assert (await client.get(path, params={"region": "uk"})).json()["chapters"][0]["title"] == "uk"
    miss = await client.get(path, params={"region": "de"})
    assert miss.status_code == 404
    assert miss.json()["code"] == "not_in_libex"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_region_is_400(client, db_session):
    await _seed(db_session)
    for path in (f"/db/book/{ASIN}", f"/db/book/{ASIN}/chapters", f"/db/series/{SERIES_ASIN}"):
        response = await client.get(path, params={"region": "zz"})
        assert response.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unfiltered_list_returns_one_row_per_asin(client, db_session):
    await _seed(db_session)

    response = await client.get("/db/book", params={"sort": "title"})
    assert response.status_code == 200
    rows = [b for b in response.json() if b["asin"] == ASIN]
    assert len(rows) == 1
    assert rows[0]["region"] == "us"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sku_route_returns_every_variant_ordered_by_region(client, db_session):
    await _seed(db_session, first="us", second="uk")

    response = await client.get(f"/db/book/sku/{SKU}")
    assert response.status_code == 200
    assert [(b["region"], b["asin"]) for b in response.json()] == [("uk", ASIN), ("us", ASIN)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_author_books_default_to_every_linked_book_with_a_region_filter(client, db_session):
    await _seed(db_session)
    path = f"/db/author/{AUTHOR_ASIN}/books"

    default = await client.get(path)
    assert sorted(b["region"] for b in default.json()) == ["uk", "us"]

    override = await client.get(path, params={"book_region": "uk"})
    assert [b["region"] for b in override.json()] == ["uk"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stats_expose_distinct_book_asins(client, db_session):
    await _seed(db_session)

    stats = (await client.get("/db/stats")).json()
    assert stats["books"] == 2
    assert stats["distinctBookAsins"] == 1

    scoped = (await client.get("/db/stats", params={"region": "uk"})).json()
    assert scoped["books"] == 1
    assert scoped["distinctBookAsins"] == 1
