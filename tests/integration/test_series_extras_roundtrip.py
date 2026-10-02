"""
Series audibleExtras and extrasWithheld, through the real writer and reader
against real Postgres.

A series profile now carries every product key beyond asin, title and
publisher_summary. The unit suite checks the normalizer and the response
model; neither can see a column that exists on the model and in the reader
but is never bound in the writer, or a merge that overwrites where it should
union. Both only show on the second write or the read after the first, so
this walks a series the whole way round with nothing mocked.

The merge is the books one (_extras_union), shared by construction, and the
rules pinned here are the ones the shrinkage invariant rests on: a thinner
answer never shrinks what is stored, two answers carrying different keys
leave both, and a series reaching the table through a book's relationships
-- which carries no extras at all -- never wipes what a profile fetch stored.
"""

# Third party
import pytest
from sqlalchemy import select

# Local
from app.db.models import Series
from app.services.db.reader import get_series_from_db, search_series_from_db
from app.services.db.writer import upsert_book, upsert_series_profile, write_books
from libex_core.audible.series import normalize_series

REGION = "us"
ASIN = "B0SERIES1X"


def _product(**extra):
    return {"asin": ASIN, "title": "A Series", "publisher_summary": "<p>Sum</p>", **extra}


async def _write(session, product):
    await upsert_series_profile(session, normalize_series(product, REGION))
    session.expire_all()


async def _row(session, asin=ASIN):
    return (await session.execute(select(Series).where(Series.asin == asin))).scalar_one()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_stored_series_serves_exactly_the_extras_the_live_dict_carried(db_session):
    live = normalize_series(
        _product(
            language="english",
            nested={"k": ["x", {"y": 1}]},
            relationships=[
                {"relationship_type": "series", "asin": ASIN},
                {"relationship_type": "episode", "asin": "B0EPISODE1"},
            ],
        ),
        REGION,
    )
    assert live["extrasWithheld"] == {"relationships": {"episode": 1}}
    await upsert_series_profile(db_session, live)
    db_session.expire_all()

    stored = await get_series_from_db(db_session, ASIN)
    assert stored["audibleExtras"] == live["audibleExtras"]
    assert stored["extrasWithheld"] == live["extrasWithheld"]
    assert stored["name"] == live["name"] and stored["description"] == live["description"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_series_without_extras_stores_null_for_both_and_reads_them_back_as_none(db_session):
    live = normalize_series(_product(), REGION)
    assert "audibleExtras" not in live and "extrasWithheld" not in live
    await upsert_series_profile(db_session, live)
    db_session.expire_all()

    row = await _row(db_session)
    assert row.audible_extras is None and row.extras_withheld is None
    stored = await get_series_from_db(db_session, ASIN)
    assert stored["audibleExtras"] is None and stored["extrasWithheld"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_absent_blob_is_sql_null_not_the_json_null_scalar(db_session):
    """Reading the row through the ORM turns a stored JSON null back into None,
    so the test above cannot tell it from SQL NULL; IS NULL in the database can,
    and the NULL arms of the merge only recognise the latter."""
    await upsert_series_profile(db_session, normalize_series(_product(), REGION))
    db_session.expire_all()

    for column in (Series.audible_extras, Series.extras_withheld):
        found = (
            await db_session.execute(select(Series.asin).where(Series.asin == ASIN, column.is_(None)))
        ).scalar_one_or_none()
        assert found == ASIN, f"{column.key} was not stored as SQL NULL"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_from_db_serves_both_keys(db_session):
    await _write(db_session, _product(language="english"))
    results = await search_series_from_db(db_session, "A Series")
    assert [r["asin"] for r in results] == [ASIN]
    assert results[0]["audibleExtras"] == {"language": "english"}
    assert results[0]["extrasWithheld"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_later_answer_with_no_extras_keeps_the_stored_ones(db_session):
    await _write(db_session, _product(language="english"))
    await _write(db_session, _product())
    assert (await _row(db_session)).audible_extras == {"language": "english"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_blob_dropped_whole_keeps_the_stored_one(db_session):
    await _write(db_session, _product(language="english"))
    deep = cur = {}
    for _ in range(40):
        cur["d"] = {}
        cur = cur["d"]
    dropped = normalize_series(_product(deep=deep), REGION)
    assert dropped["audibleExtras"] is None
    await upsert_series_profile(db_session, dropped)
    db_session.expire_all()
    assert (await _row(db_session)).audible_extras == {"language": "english"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_thinner_answer_keeps_the_richer_stored_blob(db_session):
    rich = {"language": "english", "relationships": [
        {"relationship_type": "series", "asin": ASIN, "sort": "1"},
        {"relationship_type": "series", "asin": "B0SERIES2X", "sort": "2"},
    ]}
    await _write(db_session, _product(**rich))
    await _write(db_session, _product(language="english"))
    assert (await _row(db_session)).audible_extras == rich


@pytest.mark.integration
@pytest.mark.asyncio
async def test_answers_carrying_different_keys_leave_both(db_session):
    await _write(db_session, _product(first_key="first"))
    await _write(db_session, _product(second_key="second"))
    assert (await _row(db_session)).audible_extras == {"first_key": "first", "second_key": "second"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_second_withholding_does_not_erase_the_first(db_session):
    episode = {"relationship_type": "episode", "asin": "B0EPISODE1"}
    await _write(db_session, _product(relationships=[episode]))
    deep_free = _product(plain="x", relationships=[], bad="a\x00b")
    await _write(db_session, deep_free)
    withheld = (await _row(db_session)).extras_withheld
    assert withheld == {"relationships": {"episode": 1}, "sanitized": {"nulCharacters": 1}}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_series_arriving_through_a_books_relationships_leaves_a_stored_profile_untouched(db_session):
    await _write(db_session, _product(language="english", gone=["a"]))
    before = await _row(db_session)
    assert before.audible_extras == {"language": "english", "gone": ["a"]}

    book = {
        "asin": "B0BOOK0001", "title": "A Book", "region": REGION,
        "series": [{"asin": ASIN, "name": "A Series", "region": REGION}],
    }
    await write_books(db_session, [book])
    await db_session.commit()
    db_session.expire_all()

    after = await _row(db_session)
    assert after.audible_extras == {"language": "english", "gone": ["a"]}
    assert after.extras_withheld is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_series_first_seen_through_a_book_stores_null_not_an_empty_object(db_session):
    book = {
        "asin": "B0BOOK0002", "title": "A Book", "region": REGION,
        "series": [{"asin": "B0SERIES9X", "name": "Only Via Book", "region": REGION}],
    }
    await write_books(db_session, [book])
    await db_session.commit()
    db_session.expire_all()

    row = await _row(db_session, "B0SERIES9X")
    assert row.audible_extras is None and row.extras_withheld is None
    # An upsert_book that carries the series the same way lands identically.
    await upsert_book(db_session, {**book, "asin": "B0BOOK0003"})
    db_session.expire_all()
    assert (await _row(db_session, "B0SERIES9X")).audible_extras is None
