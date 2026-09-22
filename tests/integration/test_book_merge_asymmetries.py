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

The boolean pair also guards that an explicit null is read the same way on
both sides of the merge: isListenable: null must count as silence rather
than as Audible asserting false whether the statement is inserting or
updating, so a stored true is never flipped to false by a response that
later comes back null.
"""

# Standard library
import time
from datetime import datetime, timezone

# Third party
import pytest
from sqlalchemy import select

# Local
from app.db.models import Book
from app.services.audible.books import _normalize_product
from app.services.db.writer import _BOOK_UPSERT, _book_params, upsert_book, write_books


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
    failing to update anything: updated_at is meant to move and does.

    _now() (app/services/db/writer.py) is Python's
    datetime.now(timezone.utc), computed independently on each call rather
    than read from a single upstream clock. Its documented resolution is a
    microsecond, but two calls back to back with negligible real work
    between them can still land on the identical microsecond: measured
    directly in this environment, a tight loop of consecutive
    datetime.now(timezone.utc) calls with no I/O between them ties roughly
    38% of the time, even though time.get_clock_info reports a nanosecond
    resolution -- the clock source backing it does not actually update that
    often. The two real round trips to Postgres this test makes between the
    first _now() and the second are normally enough real elapsed time to
    clear that on their own, which is exactly why this was a flake and not
    a deterministic failure: a fast enough connection (a warmed pool, a
    unix socket, a quiet CI runner) can complete both round trips inside
    the same microsecond the underlying clock hasn't moved past yet. A
    short, real, synchronous sleep -- not a mocked clock -- is what forces
    actual wall-clock time to pass before the second write reads it,
    independent of how fast the two round trips themselves happen to be.
    """
    await upsert_book(db_session, _book("B0UPDATED01"))
    first = (await _stored(db_session, "B0UPDATED01")).updated_at

    time.sleep(0.01)
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
    [
        ("isListenable", "is_listenable"),
        ("isBuyable", "is_buyable"),
        ("isVvab", "is_vvab"),
        ("explicit", "explicit"),
        ("whisperSync", "whisper_sync"),
        ("hasPdf", "has_pdf"),
    ],
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
        ("explicit", "explicit", True),
        ("whisperSync", "whisper_sync", True),
        ("hasPdf", "has_pdf", True),
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
        ("explicit", "explicit", False),
        ("whisperSync", "whisper_sync", False),
        ("hasPdf", "has_pdf", False),
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
async def test_a_normalized_thin_product_does_not_flip_a_stored_flag(db_session):
    """
    The same normalizer-sourced guarantee as
    test_a_normalized_thin_product_does_not_flip_a_stored_boolean, for
    explicit/hasPdf/whisperSync -- whose defaults are left for the writer's
    merge to settle rather than baked into the normalizer itself. A
    hand-built payload that starts life already at Libex's tri-state
    contract (True/False/absent) cannot exercise that: it never routes
    through the normalizer's own None-preserving read at all. Built through
    the real normalizer instead, so a regression in either half -- the
    normalizer defaulting a missing key, or the writer reading stmt.excluded
    instead of the bind -- fails this the same way.
    """
    rich_product = {
        "asin": "B0NORMFLAG1",
        "is_adult_product": True, "is_pdf_url_available": True, "read_along_support": True,
    }
    thin_product = {"asin": "B0NORMFLAG1"}

    rich = _normalize_product(rich_product, REGION)
    thin = _normalize_product(thin_product, REGION)
    assert (rich["explicit"], rich["hasPdf"], rich["whisperSync"]) == (True, True, True)
    assert (thin["explicit"], thin["hasPdf"], thin["whisperSync"]) == (None, None, None)

    await upsert_book(db_session, rich)
    await upsert_book(db_session, thin)

    db_session.expire_all()
    stored = await _stored(db_session, "B0NORMFLAG1")
    assert (stored.explicit, stored.has_pdf, stored.whisper_sync) == (True, True, True)


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
# audible_extras AND extras_withheld — MERGED KEY BY KEY
# ============================================================
# The only two columns on the row whose merge combines its two inputs
# instead of choosing between them, and the only ones whose rule cannot be
# checked by a sweep that proves a thin response empties nothing. A merge
# that took the incoming blob whole passes every such sweep: a NULL incoming
# blob is not a thin blob, so the arm the sweeps exercise is the one arm
# that was never in question.
#
# Each of the three remaining arms is exercised here on the value it
# produces, because they are only distinguishable by what comes back.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_later_blob_adds_its_keys_to_the_stored_one(db_session):
    """Two responses carrying different keys leave a row holding both.

    This is what the column is for. Audible answers a different set of
    top-level keys per response group -- the same ASIN returns a wide blob
    while it is purchasable and a much narrower one once it is not -- so a
    merge that chose between the two blobs would discard whichever set the
    latest fetch did not happen to carry, and no later fetch would bring it
    back.
    """
    await upsert_book(db_session, _book("B0BLOB00001", audibleExtras={"first_key": "first"}))

    await upsert_book(db_session, _book("B0BLOB00001", audibleExtras={"second_key": "second"}))

    db_session.expire_all()
    assert (await _stored(db_session, "B0BLOB00001")).audible_extras == {
        "first_key": "first",
        "second_key": "second",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_blob_the_stored_one_already_holds_changes_nothing(db_session):
    """An incoming blob the stored one already contains leaves it exactly as
    it was, down a level as well as at the top.

    Containment in jsonb is not key-level equality: a stored array contains
    an incoming array that holds a subset of its entries. So the repeat
    fetch below is contained by what is stored even though its
    relationships array is one entry long, and the whole three-entry array
    stands. Concatenation alone would have replaced it with the one entry,
    because the top-level keys are merged and whatever hangs under a key is
    not -- the shrinkage would happen one level down, where nothing else on
    the row is watching.

    Every ordinary refresh of an unchanged book takes this path, which is
    the other half of why it is worth a test: it is the common case, not
    the edge one.
    """
    series_entry = {"asin": "B0SERIES01", "relationship_type": "series", "sort": "1"}
    component_entries = [
        {"asin": "B0PART0001", "relationship_type": "component", "sort": "1"},
        {"asin": "B0PART0002", "relationship_type": "component", "sort": "2"},
    ]
    stored = {
        "relationships": [series_entry, *component_entries],
        "platinum_keywords": ["fantasy", "epic"],
    }
    await upsert_book(db_session, _book("B0BLOB00002", audibleExtras=stored))

    await upsert_book(
        db_session, _book("B0BLOB00002", audibleExtras={"relationships": [series_entry]})
    )

    db_session.expire_all()
    assert (await _stored(db_session, "B0BLOB00002")).audible_extras == stored


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_changed_value_under_a_key_replaces_what_was_under_it(db_session):
    """The accepted limit of the merge, pinned so it is a known cost rather
    than a surprise.

    The combination is one level deep. A key whose incoming value is
    neither absent nor already contained replaces what was stored under it
    whole, so an object one level down can lose entries even though no
    top-level key can. Postgres has no deep merge of its own and writing
    one would be a substantial piece of machinery, so this is the shape the
    column has -- and a consumer reading a nested object out of the blob
    has to know it is one response's version of it rather than the union
    every top-level key enjoys.

    Asserted rather than left implicit because the day someone builds the
    deep merge, this test failing is how they learn it worked.
    """
    await upsert_book(
        db_session,
        _book("B0BLOB00003", audibleExtras={"product_images": {"500": "a", "1000": "b"}}),
    )

    await upsert_book(
        db_session,
        _book("B0BLOB00003", audibleExtras={"product_images": {"500": "c"}}),
    )

    db_session.expire_all()
    assert (await _stored(db_session, "B0BLOB00003")).audible_extras == {
        "product_images": {"500": "c"}
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_second_withholding_does_not_erase_the_first(db_session):
    """Two responses that each withheld something different leave a row
    recording both.

    extras_withheld has several independent producers -- the podcast strip
    writes relationships, the jsonb sanitizer writes sanitized, the size,
    depth and encoding checks write audibleExtras -- and a given response
    fires whichever of them its own content triggers. Choosing between two
    records would therefore make the column mean "whatever the most recent
    fetch that withheld anything happened to withhold", so the podcast
    episode count below would be gone the moment a later fetch stripped a
    single stray character, while the relationships key it describes is
    still sitting in the blob beside it. The two columns are read as one
    picture and cannot be unless they span the same responses.

    Built through the real normalizer, so what is being merged is what the
    producers actually emit rather than a hand-written guess at their
    shape -- a change to either record's spelling has to fail here.
    """
    podcast = _normalize_product(
        {
            "asin": "B0BLOB00004",
            "title": "A Podcast",
            "content_type": "Podcast",
            "relationships": [
                {"asin": "B0EPISODE1", "relationship_type": "episode"},
                {"asin": "B0SEASON01", "relationship_type": "season"},
            ],
        },
        REGION,
    )
    with_a_nul = _normalize_product(
        {"asin": "B0BLOB00004", "title": "A Podcast", "odd_field": "before\x00after"},
        REGION,
    )
    assert podcast["extrasWithheld"] == {"relationships": {"episode": 1, "season": 1}}
    assert with_a_nul["extrasWithheld"] == {"sanitized": {"nulCharacters": 1}}

    await upsert_book(db_session, podcast)
    await upsert_book(db_session, with_a_nul)

    db_session.expire_all()
    assert (await _stored(db_session, "B0BLOB00004")).extras_withheld == {
        "relationships": {"episode": 1, "season": 1},
        "sanitized": {"nulCharacters": 1},
    }


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
    "numRatings": 1234, "numReviews": 56,
    "publicationName": "A Publication",
    "publicationDatetime": "2020-01-02T03:04:05Z",
    "extendedProductDescription": "A much longer extended product description",
    "productState": "AVAILABLE",
    "audibleExtras": {"relationships": [{"relationship_type": "series"}], "isbn_extra": "z"},
    # The counts the podcast strip recorded for the sampled show B08JJND27B,
    # where the season is counted separately from the episodes rather than
    # being one of them.
    "extrasWithheld": {"relationships": {"episode": 4412, "season": 1}},
}

_MERGED_COLUMNS = [
    "title", "subtitle", "region", "description", "summary", "publisher", "copyright",
    "isbn", "language", "rating", "release_date", "length_minutes", "image",
    "book_format", "content_type", "content_delivery_type", "episode_number",
    "episode_type", "sku", "sku_group", "is_listenable", "is_buyable", "is_vvab",
    "explicit", "whisper_sync", "has_pdf", "plans",
    "num_ratings", "num_reviews", "publication_name", "publication_datetime",
    "extended_product_description", "product_state", "audible_extras",
    "extras_withheld",
]

# The one column the upsert merges that the sweeps deliberately leave out.
# It is the column that has to move on a second write, so including it in a
# "nothing was emptied" comparison would assert the opposite of what
# test_updated_at_does_move_on_a_later_write holds.
_MERGED_BUT_NOT_SWEPT = {"updated_at"}


def _assert_every_merged_column_is_swept():
    """
    _MERGED_COLUMNS names every column the upsert actually merges.

    The two guards below check the fixture and the stored row against this
    list. Nothing checked the list itself, and that is the gap a widening
    walks into from the other side: columns were added to the statement and
    not to the list, and neither guard can see a column it was never asked
    about. Both sweeps stayed green over a shrinking fraction of the row.

    Read from the compiled ON CONFLICT DO UPDATE clause rather than from a
    second hand-written list, so what is compared against is the statement
    that runs. tests/test_book_shape_parity.py reads the same attribute and
    records why depending on it is acceptable.
    """
    merged = {
        name for name, _ in _BOOK_UPSERT._post_values_clause.update_values_to_set
    }
    unswept = sorted(merged - set(_MERGED_COLUMNS) - _MERGED_BUT_NOT_SWEPT)
    assert unswept == [], (
        f"The upsert merges {unswept}, which the sweeps below never look at. "
        "Add them to _MERGED_COLUMNS with a value in _RICH_BOOK, or to "
        "_MERGED_BUT_NOT_SWEPT with the reason."
    )

    unmerged = sorted(set(_MERGED_COLUMNS) - merged)
    assert unmerged == [], (
        f"_MERGED_COLUMNS names {unmerged}, which the upsert does not merge "
        "at all, so sweeping them proves nothing about the merge rules."
    )


def _assert_the_fixture_supplies_every_merged_column():
    """
    _RICH_BOOK actually answers every column the two sweeps below compare.

    Without this the sweeps have a silent hole, and it is the one a widening
    of the row walks straight into: `rich` is produced by the same upsert
    path as `thin`, so a column added to _MERGED_COLUMNS but never added to
    _RICH_BOOK is NULL on both sides and compares equal. The sweep goes
    green while covering nothing at all for that column.

    Checked through _book_params rather than against a second hand-written
    field-name list, so this reads the writer's own response-key-to-column
    mapping. A column whose bind is None is one the fixture never answered,
    whatever it is spelled in the response.
    """
    bound = _book_params(_book("B0FIXTURE01", **_RICH_BOOK), datetime.now(timezone.utc))
    unanswered = sorted(c for c in _MERGED_COLUMNS if bound.get(c) is None)
    assert unanswered == [], (
        f"_RICH_BOOK supplies nothing for {unanswered}, so the sweeps below "
        "would compare NULL against NULL and pass without testing them."
    )


async def _assert_every_merged_column_is_populated(session, asin):
    """
    The stored row answers every merged column before anything is compared
    against it.

    The other half of the same hole: a column bound by the fixture but never
    bound in the upsert statement stores NULL, reads NULL on both the rich
    and the thin side, and compares equal. Only a positive check on the rich
    row tells "survived the thin response" from "was never written".
    """
    stored = await _stored(session, asin)
    empty = sorted(c for c in _MERGED_COLUMNS if getattr(stored, c) is None)
    assert empty == [], (
        f"{empty} are NULL on the rich row, so comparing it against the thin "
        "row proves nothing about them. Either the upsert never binds them or "
        "_RICH_BOOK never answers them."
    )


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

    The three guards run first because thin == rich is satisfied by two
    columns that are both NULL just as readily as by two that both hold the
    rich value, and by a column this sweep never names at all -- see each
    guard for the way a widened row reaches those states without anything
    failing.
    """
    _assert_every_merged_column_is_swept()
    _assert_the_fixture_supplies_every_merged_column()

    await upsert_book(db_session, _book("B0EVERY0001", **_RICH_BOOK))
    db_session.expire_all()
    await _assert_every_merged_column_is_populated(db_session, "B0EVERY0001")
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

    Same three guards as the sweep above, for the same reason.
    """
    _assert_every_merged_column_is_swept()
    _assert_the_fixture_supplies_every_merged_column()

    await upsert_book(db_session, _book("B0EVERY0002", **_RICH_BOOK))
    db_session.expire_all()
    await _assert_every_merged_column_is_populated(db_session, "B0EVERY0002")
    rich = {c: getattr(await _stored(db_session, "B0EVERY0002"), c) for c in _MERGED_COLUMNS}

    await upsert_book(
        db_session,
        {"asin": "B0EVERY0002", "region": REGION, **{k: None for k in _RICH_BOOK}},
    )

    db_session.expire_all()
    thin = {c: getattr(await _stored(db_session, "B0EVERY0002"), c) for c in _MERGED_COLUMNS}
    assert thin == rich
