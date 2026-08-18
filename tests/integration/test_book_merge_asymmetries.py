"""
Integration tests for the book upsert's merge rules, against real Postgres.

The book row is written by one statement whose insert clause and update clause
deliberately disagree in four places. Every one of those disagreements is
invisible in a single write and only shows on the second — which is exactly the
shape of failure a unit test on a mocked session cannot see, because what is
being asserted is what SURVIVES a later, thinner response rather than what a
statement contained.

The disagreements, and what each one would cost if it were flattened into a
uniform "take the incoming value":

  created_at  written on insert, absent from the update. Flattened, every
              book's real creation time is reset on its next write, silently
              and unrecoverably.
  region      written on insert, updates to itself. Flattened, a response
              fetched for one marketplace moves a book into another, on a
              NOT NULL enum.
  title       falls back to '' on insert (NOT NULL) and to the stored title on
              update. Flattened either way, a response that omits the title
              blanks one already stored.
  booleans    NOT NULL, so there is no null to fall back on and the merge has
              to run on asserted-versus-silent instead. Flattened to the
              insert default, a stored false Audible asserted becomes true;
              flattened to the update default, a stored true becomes false.

The boolean pair is also a regression test for a live bug: is_listenable and
is_buyable used to read their default through _to_bool's default argument on
insert and through dict.get's default on update, which agree for every value
except an explicit null and disagree for that one — so a response carrying
isListenable: null inserted true and updated to false.
"""

# Third party
import pytest
from sqlalchemy import select

# Local
from app.db.models import Book
from app.services.audible.books import _normalize_product
from app.services.db.writer import upsert_book, write_books


REGION = "us"


def _book(asin, **overrides):
    """A minimally complete book, overridden field by field per test."""
    return {"asin": asin, "title": "Original Title", "region": REGION, **overrides}


async def _stored(session, asin):
    result = await session.execute(select(Book).where(Book.asin == asin))
    return result.scalar_one()


# ============================================================
# created_at — WRITTEN ONCE, NEVER UPDATED
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_created_at_survives_a_later_write(db_session):
    """The second write must not touch created_at. A merge derived from the
    insert's column list would carry excluded.created_at into the update and
    reset the whole corpus's creation times one book at a time, with nothing
    failing and no way to tell afterwards which value was real."""
    await upsert_book(db_session, _book("B0CREATED01"))
    first = (await _stored(db_session, "B0CREATED01")).created_at

    await upsert_book(db_session, _book("B0CREATED01", title="Second Write"))

    db_session.expire_all()
    assert (await _stored(db_session, "B0CREATED01")).created_at == first


@pytest.mark.integration
@pytest.mark.asyncio
async def test_updated_at_does_move_on_a_later_write(db_session):
    """The complement, so the test above cannot pass by the statement simply
    failing to update anything: updated_at is meant to move and does."""
    await upsert_book(db_session, _book("B0UPDATED01"))
    first = (await _stored(db_session, "B0UPDATED01")).updated_at

    await upsert_book(db_session, _book("B0UPDATED01", title="Second Write"))

    db_session.expire_all()
    assert (await _stored(db_session, "B0UPDATED01")).updated_at > first


# ============================================================
# region — WRITTEN ONCE, UPDATES TO ITSELF
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_region_is_not_moved_by_a_write_carrying_another_region(db_session):
    """A book ASIN belongs to one marketplace. Nothing in the writer may move
    it to another, however the caller's payload is labelled."""
    await upsert_book(db_session, _book("B0REGION001"))

    await upsert_book(db_session, _book("B0REGION001", region="de"))

    db_session.expire_all()
    assert (await _stored(db_session, "B0REGION001")).region == REGION


# ============================================================
# title — '' ON INSERT, STORED VALUE ON UPDATE
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_response_without_a_title_keeps_the_stored_one(db_session):
    """title is NOT NULL, so the insert has to substitute something for a
    missing one — and the value it substitutes must never reach the update,
    because coalesce('', books.title) is '' and would blank the stored title
    on every thin response."""
    await upsert_book(db_session, _book("B0TITLE0001"))

    await upsert_book(db_session, {"asin": "B0TITLE0001", "region": REGION})

    db_session.expire_all()
    assert (await _stored(db_session, "B0TITLE0001")).title == "Original Title"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_first_write_without_a_title_still_stores_the_book(db_session):
    """The other half of the same NOT NULL problem: a book whose first write
    carries no title is stored with an empty one rather than raising and
    costing the whole chunk its transaction."""
    await upsert_book(db_session, {"asin": "B0TITLE0002", "region": REGION})

    assert (await _stored(db_session, "B0TITLE0002")).title == ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_later_title_still_replaces_the_stored_one(db_session):
    """The guard must only refuse silence, not every update — a response that
    does carry a title still writes it."""
    await upsert_book(db_session, _book("B0TITLE0003"))

    await upsert_book(db_session, _book("B0TITLE0003", title="Corrected Title"))

    db_session.expire_all()
    assert (await _stored(db_session, "B0TITLE0003")).title == "Corrected Title"


# ============================================================
# NOT NULL BOOLEANS — ASSERTED VERSUS SILENT
# ============================================================

@pytest.mark.integration
@pytest.mark.parametrize(
    "field, column",
    [("isListenable", "is_listenable"), ("isBuyable", "is_buyable"), ("isVvab", "is_vvab")],
)
@pytest.mark.asyncio
async def test_an_asserted_false_is_stored(db_session, field, column):
    """Audible saying false is Audible answering, and overwrites."""
    asin = f"B0BOOL{column[:5].upper()}"
    await upsert_book(db_session, _book(asin, **{field: True}))

    await upsert_book(db_session, _book(asin, **{field: False}))

    db_session.expire_all()
    assert getattr(await _stored(db_session, asin), column) is False


@pytest.mark.integration
@pytest.mark.parametrize(
    "field, column, asserted",
    [
        ("isListenable", "is_listenable", False),
        ("isBuyable", "is_buyable", False),
        ("isVvab", "is_vvab", True),
    ],
)
@pytest.mark.asyncio
async def test_silence_keeps_the_stored_answer(db_session, field, column, asserted):
    """A response that omits the field entirely must leave the stored answer
    standing. Each case is seeded with the value OPPOSITE to the column
    default, so a merge that quietly substituted either default would be
    caught rather than agreeing with the seed by luck."""
    asin = f"B0SILNT{column[:4].upper()}"
    await upsert_book(db_session, _book(asin, **{field: asserted}))

    await upsert_book(db_session, _book(asin, title="Thin Response"))

    db_session.expire_all()
    assert getattr(await _stored(db_session, asin), column) is asserted


@pytest.mark.integration
@pytest.mark.parametrize(
    "field, column, expected",
    [
        ("isListenable", "is_listenable", True),
        ("isBuyable", "is_buyable", True),
        ("isVvab", "is_vvab", False),
    ],
)
@pytest.mark.asyncio
async def test_a_first_write_without_the_field_takes_the_column_default(db_session, field, column, expected):
    """With nothing stored there is no answer to keep, so the insert supplies
    the column's own default — and the bind carrying None never reaches the
    NOT NULL column, which would abort the whole chunk's statement."""
    asin = f"B0DFLT{column[:5].upper()}"
    await upsert_book(db_session, _book(asin))

    assert getattr(await _stored(db_session, asin), column) is expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_explicit_null_is_read_as_silence_not_as_false(db_session):
    """The exact live bug: isListenable arriving as null used to insert true
    and update to false, so the same payload told two different stories
    depending on whether the book had been seen before. Null is Audible
    declining to answer, and the stored answer stands."""
    await upsert_book(db_session, _book("B0NULLBOOL1", isListenable=True))

    await upsert_book(db_session, _book("B0NULLBOOL1", isListenable=None))

    db_session.expire_all()
    assert (await _stored(db_session, "B0NULLBOOL1")).is_listenable is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_normalized_thin_product_does_not_flip_a_stored_boolean(db_session):
    """
    Every asserted-versus-silent case above hand-builds its payload with the
    field already at Libex's own tri-state contract (True/False/absent) --
    which cannot catch a normalizer defect that flattens Audible's own absence
    to a concrete default before the writer ever sees it, since a hand-built
    dict never routes through that flattening step at all.

    Built through the real normalizer instead: a rich raw product asserts all
    three fields, a later raw product simply doesn't carry those keys (a
    thinner response group, not Audible answering false), and the persisted
    values must survive exactly as the hand-built asserted-vs-silent tests
    above already prove they do when the None reaches the writer honestly.
    This is the same guarantee, pinned against what the normalizer, not the
    test author, actually emits.
    """
    rich_product = {
        "asin": "B0NORMBOOL1",
        "is_listenable": False, "is_buyable": False, "is_vvab": True,
    }
    thin_product = {"asin": "B0NORMBOOL1"}

    rich = _normalize_product(rich_product, REGION)
    thin = _normalize_product(thin_product, REGION)
    assert (rich["isListenable"], rich["isBuyable"], rich["isVvab"]) == (False, False, True)
    assert (thin["isListenable"], thin["isBuyable"], thin["isVvab"]) == (None, None, None)

    await upsert_book(db_session, rich)
    await upsert_book(db_session, thin)

    db_session.expire_all()
    stored = await _stored(db_session, "B0NORMBOOL1")
    assert (stored.is_listenable, stored.is_buyable, stored.is_vvab) == (False, False, True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_is_vvab_reaches_the_column(db_session):
    """isVvab is surfaced by the normalizer, has a column, has a reader and
    has an endpoint, and was written by nothing — so /db/vvab could only ever
    serve rows no fetch had produced. The first assertion this needs is simply
    that the value now arrives at all."""
    await upsert_book(db_session, _book("B0VVAB00001", isVvab=True))

    assert (await _stored(db_session, "B0VVAB00001")).is_vvab is True


# ============================================================
# A CHUNK CARRYING THE SAME ASIN TWICE
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_same_asin_twice_in_one_chunk_merges_rather_than_raising(db_session):
    """An author walk pages a catalog whose sort window shifts under it, so a
    chunk holding one ASIN twice is ordinary, and must merge rather than raise.

    This asserts the outcome, not the reason for it. What keeps a multi-row
    VALUES rewrite (and with it a cardinality_violation on the repeat) off
    this statement is the absence of a RETURNING clause, and that is pinned
    directly in tests/test_db_writer.py — adding one back would not fail this
    test, because the shared bind parameters in the set_ happen to defeat the
    rewrite on their own. Two independent guards, only one of them deliberate,
    so each is checked where it can actually be seen."""
    await write_books(db_session, [
        _book("B0DUPE00001", description="short"),
        _book("B0DUPE00001", description="a much longer description than the first"),
    ])
    await db_session.commit()

    stored = await _stored(db_session, "B0DUPE00001")
    assert stored.description == "a much longer description than the first"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_chunk_merges_a_book_it_already_holds_stored(db_session):
    """The repeat is merged, not overwritten: the second copy in the chunk is
    treated exactly as a later request would be, so a thinner duplicate cannot
    undo the richer one that shares its chunk."""
    await write_books(db_session, [
        _book("B0DUPE00002", description="a much longer description than the second"),
        _book("B0DUPE00002"),
    ])
    await db_session.commit()

    stored = await _stored(db_session, "B0DUPE00002")
    assert stored.description == "a much longer description than the second"


# ============================================================
# THE BATCH AND THE SINGLE BOOK AGREE
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_batched_write_merges_exactly_as_a_single_one_does(db_session):
    """The whole safety of batching rests on the two paths applying the same
    merge. Both are handed the same rich book and then the same thin one, and
    have to end up holding the same row."""
    rich = {"description": "a long stored description", "summary": "a stored summary",
            "publisher": "Stored Publisher", "isListenable": False, "isVvab": True}
    thin = {"title": "Thin Response"}

    await write_books(db_session, [_book("B0BATCH0001", **rich)])
    await db_session.commit()
    await write_books(db_session, [_book("B0BATCH0001", **thin)])
    await db_session.commit()

    await upsert_book(db_session, _book("B0SINGLE001", **rich))
    await upsert_book(db_session, _book("B0SINGLE001", **thin))

    db_session.expire_all()
    batched = await _stored(db_session, "B0BATCH0001")
    single = await _stored(db_session, "B0SINGLE001")
    compared = ("title", "description", "summary", "publisher", "region",
                "is_listenable", "is_buyable", "is_vvab", "plans")
    assert {c: getattr(batched, c) for c in compared} == {c: getattr(single, c) for c in compared}


# ============================================================
# plans — A JSONB COLUMN THROUGH A BOUND PARAMETER
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_plans_round_trips_as_a_json_array(db_session):
    """plans is the one column whose bind needs an explicit type: an untyped
    bind parameter reaches asyncpg as a Python list and raises, and one typed
    as text would store the list's repr and read back as a string that every
    consumer would treat as a one-element array of gibberish."""
    await upsert_book(db_session, _book("B0PLANS0001", plans=["US Minerva", "US Radio"]))

    assert (await _stored(db_session, "B0PLANS0001")).plans == ["US Minerva", "US Radio"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_response_without_plans_keeps_the_stored_array(db_session):
    """Same coalesce rule as every other nullable column: silence does not
    empty what is stored."""
    await upsert_book(db_session, _book("B0PLANS0002", plans=["US Minerva"]))

    await upsert_book(db_session, _book("B0PLANS0002", title="Thin Response"))

    db_session.expire_all()
    assert (await _stored(db_session, "B0PLANS0002")).plans == ["US Minerva"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_response_with_an_empty_plans_array_replaces_the_stored_one(db_session):
    """
    An explicit `plans: []` is Audible asserting the title left every plan,
    not silence -- unlike every coalesced column above, plans is commercial
    state that changes, and refusing the empty answer would leave a book
    permanently stuck in a plan it dropped out of. Built through the real
    normalizer so a regression in what _parse_plans returns is what this
    catches, not what a test author assumed it returns.
    """
    rich_product = {"asin": "B0PLANS0003", "plans": [{"plan_name": "US Minerva"}]}
    later_product = {"asin": "B0PLANS0003", "plans": []}

    rich = _normalize_product(rich_product, REGION)
    later = _normalize_product(later_product, REGION)
    assert rich["plans"] == ["US Minerva"]
    assert later["plans"] == []

    await upsert_book(db_session, rich)
    await upsert_book(db_session, later)

    db_session.expire_all()
    assert (await _stored(db_session, "B0PLANS0003")).plans == []


# ============================================================
# EVERY COLUMN AT ONCE — THE BOUND-NULL TRAP
# ============================================================

_RICH_BOOK = {
    "title": "A Full Title", "subtitle": "A Subtitle", "description": "A long description",
    "summary": "A summary", "publisher": "A Publisher", "copyright": "(c) 2020",
    "isbn": "9781234567897", "language": "english", "rating": 4.5,
    "releaseDate": "2020-01-01T00:00:00+00:00", "lengthMinutes": 600,
    "explicit": True, "whisperSync": True, "hasPdf": True,
    "imageUrl": "https://example.com/i.jpg", "bookFormat": "unabridged",
    "contentType": "Product", "contentDeliveryType": "SinglePartBook",
    "episodeNumber": "3", "episodeType": "full", "sku": "SKU123", "skuGroup": "SG123",
    "isListenable": False, "isBuyable": False, "isVvab": True,
    "plans": ["US Minerva"],
}

_MERGED_COLUMNS = [
    "title", "subtitle", "region", "description", "summary", "publisher", "copyright",
    "isbn", "language", "rating", "release_date", "length_minutes", "image",
    "book_format", "content_type", "content_delivery_type", "episode_number",
    "episode_type", "sku", "sku_group", "is_listenable", "is_buyable", "is_vvab", "plans",
]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_column_is_emptied_by_a_response_that_omits_everything(db_session):
    """
    Every merged column at once, because the way this breaks is per column and
    per type rather than per rule.

    Moving the values clause from compiled literals to bound parameters changes
    who handles a Python None: a literal None is coerced to SQL NULL before any
    type sees it, while a bound None runs through the column type's bind
    processor first. Most types pass it through — but JSONB's default turns it
    into the JSON value null, which is a value, wins a coalesce against the
    stored array, and empties the column on every thin response. It was found
    by a test like this one and would not have been found by reading, so the
    check is kept over all columns rather than the one that failed.
    """
    await upsert_book(db_session, _book("B0EVERY0001", **_RICH_BOOK))
    db_session.expire_all()
    rich = {c: getattr(await _stored(db_session, "B0EVERY0001"), c) for c in _MERGED_COLUMNS}

    await upsert_book(db_session, {"asin": "B0EVERY0001", "region": REGION})

    db_session.expire_all()
    thin = {c: getattr(await _stored(db_session, "B0EVERY0001"), c) for c in _MERGED_COLUMNS}
    assert thin == rich


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_column_is_emptied_by_a_response_that_sends_explicit_nulls(db_session):
    """
    The same sweep with every field present and null rather than absent.

    dict.get cannot tell the two apart, so most of the writer treats them
    alike — but the boolean merge deliberately does not use dict.get's default,
    and this is the shape that used to make it disagree with itself. A field
    Audible sends as null is Audible declining to answer, not answering false.
    """
    await upsert_book(db_session, _book("B0EVERY0002", **_RICH_BOOK))
    db_session.expire_all()
    rich = {c: getattr(await _stored(db_session, "B0EVERY0002"), c) for c in _MERGED_COLUMNS}

    await upsert_book(
        db_session,
        {"asin": "B0EVERY0002", "region": REGION, **{k: None for k in _RICH_BOOK}},
    )

    db_session.expire_all()
    thin = {c: getattr(await _stored(db_session, "B0EVERY0002"), c) for c in _MERGED_COLUMNS}
    assert thin == rich
