"""
Sort direction and NULL placement in the stored-catalog readers.

The equivalence tests compare SQLite with Postgres, and both run the same
statement, so a flipped direction would change both answers alike. These pin the
order itself against the seeded rows, on SQLite everywhere and on Postgres when
Docker is there.
"""

# Third party
import pytest

# Local
from libex_core.storage.read import books, people

pytest.importorskip("aiosqlite")


def _asins(rows):
    return [r["asin"][-2:] for r in rows]


# Seeded ratings, ascending: 06 1.1, 07 1.2, 08 1.3, 09 1.4, 10 1.5, 11 1.6,
# 12 1.7, 13 1.8, 14 1.9, 15 2.0, 05 2.5, 02 3.0, 01 4.5, 04 5.0; 03 has none.
_RATING_ASC = ["06", "07", "08", "09", "10", "11", "12", "13", "14", "15", "05", "02", "01", "04", "03"]
# Seeded lengths, ascending; 03 has none.
_LENGTH_ASC = ["05", "02", "06", "07", "08", "09", "10", "11", "12", "13", "14", "15", "04", "01"]


async def _search(session, sort, order):
    return _asins(await books.search_books(session, sort=sort, order=order, limit=50))


async def check_book_direction_and_nulls(session):
    asc = await _search(session, "rating", "asc")
    assert asc == _RATING_ASC
    desc = await _search(session, "rating", "desc")
    assert desc == _RATING_ASC[-2::-1] + ["03"]
    # no order given means ascending
    assert await _search(session, "rating", None) == _RATING_ASC
    # the missing value goes last whichever way the rest runs
    assert asc[-1] == desc[-1] == "03"

    length_asc = await _search(session, "lengthMinutes", "asc")
    length_desc = await _search(session, "lengthMinutes", "desc")
    assert length_asc[:-1] == _LENGTH_ASC and length_asc[-1] == "03"
    assert length_desc[:-1] == _LENGTH_ASC[::-1] and length_desc[-1] == "03"

    # 05 is the one book with no release date
    date_asc = await _search(session, "releaseDate", "asc")
    date_desc = await _search(session, "releaseDate", "desc")
    assert date_asc[0] == "15" and date_asc[-2:] == ["04", "05"]
    assert date_desc[0] == "04" and date_desc[-2:] == ["15", "05"]
    assert date_asc[:-1] == date_desc[:-1][::-1]


async def check_narrator_direction_and_nulls(session):
    # Only Nina Voice has a sourceUpdatedAt; the other three are NULL.
    for order in ("asc", "desc"):
        rows = await people.search_narrators(session, "", sort="sourceUpdatedAt", order=order)
        assert rows[0]["name"] == "Nina Voice"
        assert len(rows) == 4
    by_source = {
        order: [r["name"] for r in await people.search_narrators(session, "", sort="source", order=order)]
        for order in ("asc", "desc")
    }
    assert by_source["asc"] == ["Émile Lecteur", "Oscar Reader", "Nina Voice", "Zed"]
    assert by_source["desc"] == by_source["asc"][::-1]


async def check_default_release_orders(session):
    # Newest first: inside the 90-day window the seeded releases are 08 (2 days
    # ago), 01 (5) and 02 (40).
    assert _asins(await books.get_new_releases(session, days=90)) == ["08", "01", "02"]
    # Soonest first: 03 is 10 days out and 04 is 60.
    assert _asins(await books.get_coming_soon(session, days=90)) == ["03", "04"]


@pytest.mark.asyncio
async def test_book_sorts_run_each_way_with_nulls_last_on_sqlite(sqlite_session):
    await check_book_direction_and_nulls(sqlite_session)


@pytest.mark.asyncio
async def test_narrator_sorts_run_each_way_with_nulls_last_on_sqlite(sqlite_session):
    await check_narrator_direction_and_nulls(sqlite_session)


@pytest.mark.asyncio
async def test_default_release_orders_on_sqlite(sqlite_session):
    await check_default_release_orders(sqlite_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_book_sorts_run_each_way_with_nulls_last_on_postgres(postgres_session):
    await check_book_direction_and_nulls(postgres_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_narrator_sorts_run_each_way_with_nulls_last_on_postgres(postgres_session):
    await check_narrator_direction_and_nulls(postgres_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_default_release_orders_on_postgres(postgres_session):
    await check_default_release_orders(postgres_session)
