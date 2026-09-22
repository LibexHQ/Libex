"""
normalize -> write -> read, against real Postgres.

The response-shape parity in tests/test_book_shape_parity.py catches a
builder, a reader or a model left behind by a widening. It cannot catch the
defect this file exists for: a column added to the model AND mirrored in the
reader AND declared on the response, but never bound in the writer. That
column stores NULL for the life of every row, and every shape check in the
suite passes, because all three places name the field and the field is
genuinely there -- empty.

It is invisible in review too, and this is why. The fresh path looks
perfect: the very response that normalized the product carries the value,
because it is handed back from the dict the normalizer built rather than
read from the row it was written to. Only the second request for the same
book -- the one served from the database, which is almost every request --
comes back empty, and by then nothing connects the two.

So this walks a product the whole way round: through the real normalizer,
into a real table by the real writer, back out through the real reader, and
asserts the value that comes back is the value that went in. Nothing is
mocked at any step, because a mock at any one of them is a mock of exactly
the step that fails.

Two things are compared loosely, both named rather than skipped:

  the relationship collections   authors, narrators, genres and series come
                                 back carrying a DB-assigned id and
                                 updatedAt that the normalizer could not
                                 have known. Everything else about them is
                                 compared exactly.
  updatedAt on the book itself   the normalizer emits None by contract and
                                 the row holds the write's own timestamp,
                                 so the sweep's non-None rule passes over
                                 it on its own.
"""

# Standard library
import json

# Third party
import pytest
from sqlalchemy import text

# Local
from app.services.audible.books import _normalize_product
from app.services.db.reader import get_book_from_db
from app.services.db.writer import write_books
from tests.fixtures.audible_product import AUDIBLE_PRODUCT


REGION = "us"

# The literal Audible sends for a title with no real publication date, and
# the value publicationDatetime has to survive character for character.
_UNRELEASED = "2200-01-01T00:00:00Z"

# A product with every first-class field answered, so the sweep below is
# comparing real values rather than passing over Nones. Shared with
# tests/test_book_shape_parity.py, which decides from the same product which
# fields a response is meant to carry -- a round trip over a different
# product would be asserting survival for a different set of fields than the
# one that was checked for completeness.
_PRODUCT = {**AUDIBLE_PRODUCT, "asin": "B0ROUND0001"}

_RELATIONSHIP_COLLECTIONS = {"authors", "narrators", "genres", "series"}

# Fields on a nested object that only the database can supply.
_DB_ASSIGNED = {"id", "updatedAt"}


def _without_db_assigned(entries):
    return [{k: v for k, v in entry.items() if k not in _DB_ASSIGNED} for entry in entries]


async def _round_trip(session, product):
    """Normalizes, writes and reads back, returning both sides."""
    normalized = _normalize_product(product, REGION)
    await write_books(session, [normalized])
    await session.commit()
    session.expire_all()
    stored = await get_book_from_db(session, product["asin"])
    assert stored is not None, f"{product['asin']} was not written at all."
    return normalized, stored


# ============================================================
# EVERY ANSWERED FIELD SURVIVES THE DATABASE
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_answered_field_comes_back_from_the_row(db_session):
    """The whole row at once, value by value.

    Not "the field is populated" -- the value. A sweep that only checked
    for non-None would pass a writer that bound every column to the same
    string, and pass a reader that returned the wrong column for a field.
    """
    normalized, stored = await _round_trip(db_session, _PRODUCT)

    missing, changed = [], []
    for key, sent in normalized.items():
        if sent is None:
            continue
        if key not in stored:
            missing.append(key)
            continue

        got = stored[key]
        if key in _RELATIONSHIP_COLLECTIONS:
            sent, got = _without_db_assigned(sent), _without_db_assigned(got)
        if got is None:
            missing.append(key)
        elif got != sent:
            changed.append(f"{key}: sent {sent!r}, read back {got!r}")

    assert missing == [], (
        f"{missing} were answered by Audible and come back null or absent "
        "from the row. A column mirrored in the reader but never bound in "
        "the writer looks exactly like this."
    )
    assert changed == [], "; ".join(changed)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_blob_comes_back_key_for_key(db_session):
    """audibleExtras specifically, compared whole rather than sampled.

    A jsonb column bound without its type, or bound through a serializer
    that stringifies, reads back as a string that looks populated in every
    check short of this one.
    """
    normalized, stored = await _round_trip(db_session, _PRODUCT)

    assert isinstance(stored["audibleExtras"], dict), (
        f"audibleExtras read back as {type(stored['audibleExtras']).__name__}, "
        "not an object."
    )
    assert stored["audibleExtras"] == normalized["audibleExtras"]


# ============================================================
# publicationDatetime KEEPS ITS EXACT SPELLING
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_publication_datetime_round_trips_byte_identical(db_session):
    """Audible sends a UTC instant with a literal Z, and that is what a
    caller gets back.

    The column is timestamptz, so the value does not survive by being left
    alone -- it is parsed on the way in and rendered on the way out, and
    the obvious rendering is datetime.isoformat(), which writes
    +00:00 rather than Z. Both spell the same instant and they are not the
    same string, which is the whole of the problem: a consumer matching on
    the value, caching on it, or comparing it to what Audible returns sees
    a change that never happened.

    The unreleased sentinel is used as the value because it is the one
    literal this repo knows Audible sends verbatim, and because a year of
    2200 catches a round trip that quietly clamps or truncates.
    """
    product = {**_PRODUCT, "asin": "B0ROUND0002", "publication_datetime": _UNRELEASED}

    normalized, stored = await _round_trip(db_session, product)

    assert normalized["publicationDatetime"] == _UNRELEASED, (
        "The normalizer changed the value before it ever reached the database."
    )
    assert stored["publicationDatetime"] == _UNRELEASED, (
        f"publicationDatetime came back as {stored['publicationDatetime']!r}, "
        f"not {_UNRELEASED!r}."
    )


# ============================================================
# THE SANITIZER, AGAINST THE COLUMN THAT REFUSES THESE VALUES
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_really_does_refuse_an_unsanitized_value(db_session):
    """The premise the sanitizer rests on, checked rather than assumed.

    If jsonb accepted a NUL the sanitizer would be removing data for no
    reason, and every test of it would be asserting a made-up requirement.
    This is the one place that can say the requirement is real, because
    only a real Postgres can refuse it.
    """
    with pytest.raises(Exception) as caught:
        await db_session.execute(
            text("SELECT CAST(:raw AS jsonb)"),
            {"raw": json.dumps({"odd_field": "before\x00after"})},
        )
        await db_session.commit()

    assert "\\u0000" in str(caught.value) or "u0000" in str(caught.value).lower(), (
        f"Postgres refused the value for some other reason: {caught.value}"
    )
    await db_session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_product_carrying_every_refused_value_still_writes(db_session):
    """One of these anywhere in a product fails that book's write for good.

    Not a degraded write -- no row at all, and on a batched chunk the other
    forty-nine books in it go down with it. So the assertion is that the
    book is written, that the sanitized values are what the row holds, and
    that the record of what was changed came back with it.
    """
    product = {
        **_PRODUCT,
        "asin": "B0ROUND0003",
        "nul_field": "before\x00after",
        "nan_field": float("nan"),
        "inf_field": 1e999,
        "wide_int_field": 10 ** 5000,
    }

    normalized, stored = await _round_trip(db_session, product)

    blob = stored["audibleExtras"]
    assert blob["nul_field"] == "beforeafter"
    assert blob["nan_field"] is None
    assert blob["inf_field"] is None
    assert blob["wide_int_field"] is None
    assert stored["extrasWithheld"]["sanitized"] == {
        "nulCharacters": 1,
        "nonFiniteNumbers": 2,
        "oversizedNumbers": 1,
    }


# ============================================================
# A THIN RESPONSE DOES NOT EMPTY WHAT IS STORED
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_later_thin_response_leaves_the_widened_fields_alone(db_session):
    """The shrinkage rule applied to the Audible product fields below.

    One outcome, four merge rules, which is why this is asserted over the
    whole group rather than per column: a plain coalesce is right for some
    of them and actively wrong for others, so a reader who assumes one rule
    covers the group will pick the wrong one for the next column added.

      numRatings, numReviews,        plain NULL merge. Nothing but a NULL
      publicationDatetime            can be read as no answer here.
      publicationName, productState  a thin response group sends these as
                                     '' rather than omitting them, and
                                     coalesce('', stored) is '' -- so the
                                     blank has to be measured, not
                                     coalesced.
      extendedProductDescription     longest wins, as the other long-form
                                     text columns do, because the field
                                     fills in as response groups widen and
                                     a shorter later answer is not a newer
                                     truth.
      audibleExtras                  merged key by key. Taking an incoming
                                     blob whole would drop every key the
                                     narrower response did not carry.

    What they share is the outcome asserted here, and it is the outcome a
    widening breaks: a new column bound with excluded.<column> and no merge
    rule at all passes every test above and empties on the next thin fetch.
    """
    product = {**_PRODUCT, "asin": "B0ROUND0004"}
    rich, _ = await _round_trip(db_session, product)

    thin = {"asin": "B0ROUND0004", "title": _PRODUCT["title"], "region": REGION}
    await write_books(db_session, [thin])
    await db_session.commit()
    db_session.expire_all()

    stored = await get_book_from_db(db_session, "B0ROUND0004")
    widened = [
        "numRatings", "numReviews", "publicationName", "publicationDatetime",
        "extendedProductDescription", "productState", "audibleExtras",
    ]
    emptied = [key for key in widened if stored.get(key) != rich[key]]

    assert emptied == [], (
        f"{emptied} were emptied by a response that said nothing about them."
    )
