"""
Structural parity across the four hand-maintained lists that describe a book.

A book's shape is written out field by field in four separate places, and
nothing makes any of them agree with the others:

  _normalize_product   (app/services/audible/books.py) builds the dict a
                       freshly fetched Audible product becomes.
  _book_params         (app/services/db/writer.py) binds that dict to the
                       book upsert, column by column.
  _book_to_dict        (app/services/db/reader.py) rebuilds the same dict
                       from a stored row.
  BookResponse         (libex_core/models.py) declares what a caller is
                       handed.

Each is a literal a person types. Widening the row means editing all four,
and every subset of that edit is silent in its own way:

  builder only         The live path returns a field; the database-backed
                       path omits it. A caller sees it when the book is
                       fresh and not when it is served from the row, with
                       nothing failing either time.
  reader only          The field is null on every response that carries it,
                       forever, because nothing ever writes it.
  model only           Pydantic supplies the declared default, so the field
                       is present and empty on every response. That reads
                       as "Audible has nothing for this book" rather than
                       as a missing wire-up.
  neither model        The builder's extra key is dropped by
                       response_model serialisation without a word -- the
                       one that leaks a field Audible actually sent.

Asserted as a set identity, never as a count. The count is not a stable
fact about this row: it was quoted as 34 in two places while it was
something else, because a number is easy to copy and impossible to
re-derive from the thing it describes. A set says which field is missing
and from where; a count says only that somebody's arithmetic no longer
holds.

One key is deliberately not part of a flat identity, and pretending
otherwise would make this file assert something false.
_normalize_product adds extrasWithheld only when something actually was
withheld, so the builder emits one of two key sets rather than one. That
optionality is a documented decision, so the identity is stated over the
maximal set and the one permitted absence is named: every other key is
unconditional, and a second key going conditional fails here rather than
widening the exemption by accident.

The upsert half reads a SQLAlchemy private attribute:
_BOOK_UPSERT._post_values_clause.update_values_to_set is the compiled
ON CONFLICT DO UPDATE SET clause, and there is no public accessor for it.
A SQLAlchemy upgrade that renames or restructures it breaks this file
loudly -- an AttributeError naming the attribute, at collection -- rather
than quietly asserting nothing, which is why the dependency is acceptable
here. It is recorded rather than hidden so that the fix on that day is
"find the new accessor", not "delete the test".

In-repo precedent for the approach: tests/test_compose_env_parity.py does
the same job for the three files that must agree on which settings exist.
"""

# Standard library
from datetime import datetime, timezone

# Local
from app.db.models import Book
from app.services.audible.books import _normalize_product
from app.services.db.reader import _book_to_dict
from app.services.db.writer import _BOOK_UPSERT, _book_params
from libex_core.models import BookResponse
from tests.fixtures.audible_product import AUDIBLE_PRODUCT


REGION = "us"

# A product carrying every top-level key the normalizer reads, so the keys
# it derives conditionally are all exercised rather than defaulting out.
# Shared with tests/integration/test_book_widening_roundtrip.py, which
# asserts that the fields identified here survive a round trip -- the two
# are only measuring the same row while they are reading the same product.
_FIXTURE_PRODUCT = {**AUDIBLE_PRODUCT, "asin": "B0PARITY001"}

# The same product with one episode relationship added, which is the
# cheapest thing that makes the normalizer withhold something and so emit
# extrasWithheld. Kept as a separate fixture rather than a flag on the one
# above, so both key sets are named and neither is implied.
_FIXTURE_PRODUCT_WITH_SOMETHING_WITHHELD = {
    **_FIXTURE_PRODUCT,
    "relationships": [
        {"asin": "B0SERIES01", "relationship_type": "series", "sort": "1", "title": "A Series"},
        {"asin": "B0EPISODE1", "relationship_type": "episode", "sort": "1"},
    ],
}

# The one key _normalize_product emits conditionally. It is absent when the
# blob came through whole, because an empty record would read as a claim
# that something was examined and found clean on every single book, rather
# than as nothing having been withheld.
_CONDITIONAL_BUILDER_KEYS = {"extrasWithheld"}


def _stored_row() -> Book:
    """A Book instance with no session behind it.

    _book_to_dict reads columns and relationship collections only, and an
    unattached instance answers both -- the collections as empty lists --
    so the key set it produces needs no database to obtain.

    extras_withheld is populated rather than left NULL because the reader
    makes extrasWithheld conditional on it, exactly as the builder makes
    that key conditional on something having been withheld. A row without
    it yields the reader's minimal key set, which would be compared against
    the builder's maximal one and fail on a difference that is the two
    fixtures disagreeing rather than the two functions disagreeing. Both
    sides are stated at their maximum so the identity below is over the
    same thing on both sides of the equals sign.
    """
    return Book(
        asin="B0PARITY001",
        region=REGION,
        title="A Parity Title",
        extras_withheld={"episodes": "not modelled"},
    )


def _upsert_set_columns() -> set[str]:
    """Column names the book upsert's ON CONFLICT DO UPDATE clause sets.

    update_values_to_set is a list of (name, expression) pairs, not a
    mapping -- taking set() of it directly yields tuples and compares
    unequal to everything, which passes nothing and fails everything.
    """
    return {name for name, _ in _BOOK_UPSERT._post_values_clause.update_values_to_set}


# Fields the fixture cannot answer, each for a reason in the normalizer
# rather than in the fixture. episodeNumber and episodeType are read only
# when the product is a podcast, and this one is an ordinary title;
# updatedAt is emitted as None by contract, the value being the row's, not
# Audible's.
_FIELDS_THE_FIXTURE_CANNOT_ANSWER = {"episodeNumber", "episodeType", "updatedAt"}


# ============================================================
# THE FIXTURE ITSELF
# ============================================================

def test_the_fixture_answers_every_field_it_describes():
    """The product really is shaped the way the normalizer reads it.

    Everything else in this file compares key sets, and a key set is blind
    to the value under the key: a fixture that puts an upstream key at the
    wrong depth still produces the full set of response keys, with the
    field that should have carried the value null and the misplaced key
    flowing into audibleExtras instead. Every assertion stays green and
    the field is covered by nothing.

    That is not hypothetical. num_reviews was written at the product top
    level here while the normalizer reads it at rating.num_reviews, and
    nothing in this file or the round trip that shares this product could
    see it, because both were measuring shape rather than content.

    The exemptions are named individually so that a field going quiet has
    to be explained rather than absorbed.
    """
    book = _normalize_product(_FIXTURE_PRODUCT, REGION)

    unanswered = sorted(
        key for key, value in book.items()
        if key not in _FIELDS_THE_FIXTURE_CANNOT_ANSWER
        and (value is None or value == [] or value == {})
    )
    assert unanswered == [], (
        f"The fixture leaves {unanswered} empty, so every check that reads "
        "them is comparing nothing against nothing. Either the product is "
        "missing the upstream key, or it carries it somewhere the "
        "normalizer does not look."
    )


# ============================================================
# THE THREE RESPONSE SHAPES
# ============================================================

def test_the_builder_the_reader_and_the_model_hold_the_same_keys():
    """The identity that makes a widening complete rather than partial.

    All three compared here rather than two, because the pairwise version
    has a blind spot the three-way one does not: builder-against-model and
    reader-against-model can each pass while builder and reader disagree,
    if the model happens to be ahead of one and behind the other.

    The builder is read from the fixture that withholds something, which is
    its maximal key set. The permitted absence is held separately by
    test_the_builder_omits_only_the_one_conditional_key, so widening the
    exemption takes two edits in two places rather than one.
    """
    from_audible = set(_normalize_product(_FIXTURE_PRODUCT_WITH_SOMETHING_WITHHELD, REGION))
    from_the_database = set(_book_to_dict(_stored_row(), {}))
    from_the_model = set(BookResponse.model_fields)

    assert from_audible == from_the_model, (
        "_normalize_product and BookResponse disagree. "
        f"Only in the builder: {sorted(from_audible - from_the_model)}. "
        f"Only in the model: {sorted(from_the_model - from_audible)}."
    )
    assert from_the_database == from_the_model, (
        "_book_to_dict and BookResponse disagree. "
        f"Only in the reader: {sorted(from_the_database - from_the_model)}. "
        f"Only in the model: {sorted(from_the_model - from_the_database)}."
    )
    assert from_audible == from_the_database, (
        "_normalize_product and _book_to_dict disagree. "
        f"Only in the builder: {sorted(from_audible - from_the_database)}. "
        f"Only in the reader: {sorted(from_the_database - from_audible)}."
    )


def test_the_builder_omits_only_the_one_conditional_key():
    """Everything except extrasWithheld is present whatever the product was.

    A field that starts appearing only when it has a value is a shape
    change a caller cannot code against: absent and null are different
    responses, and a consumer written against the product that had one
    breaks on the product that did not. The exemption is one key, named,
    for a documented reason -- this is what keeps it at one.
    """
    nothing_withheld = set(_normalize_product(_FIXTURE_PRODUCT, REGION))
    something_withheld = set(_normalize_product(
        _FIXTURE_PRODUCT_WITH_SOMETHING_WITHHELD, REGION
    ))

    assert something_withheld - nothing_withheld == _CONDITIONAL_BUILDER_KEYS, (
        "A key other than extrasWithheld now appears only on some products: "
        f"{sorted((something_withheld - nothing_withheld) - _CONDITIONAL_BUILDER_KEYS)}."
    )
    assert nothing_withheld - something_withheld == set(), (
        "Withholding something dropped a key that was otherwise present: "
        f"{sorted(nothing_withheld - something_withheld)}."
    )


def test_the_reader_omits_the_same_one_conditional_key():
    """The reader's optionality matches the builder's, in both directions.

    The identity above is stated over the maximal key set, so it is blind
    to this on its own: a reader that emitted extrasWithheld on every row,
    including the rows where nothing was withheld, produces the same
    maximal set and passes there untouched. What it would actually do is
    put an explicit null on the great majority of responses, which reads as
    "something was examined and nothing was withheld" on every row written
    before the column existed -- a claim nobody made.

    The opposite mistake is the one that matters more to a caller, and it
    fails here too: a reader that dropped the key even when the row holds a
    record of what was withheld serves a book whose response cannot say
    anything was ever left out of it.
    """
    with_a_record = set(_book_to_dict(_stored_row(), {}))
    without_a_record = set(_book_to_dict(
        Book(asin="B0PARITY002", region=REGION, title="A Parity Title"), {}
    ))

    assert with_a_record - without_a_record == _CONDITIONAL_BUILDER_KEYS, (
        "The reader's conditional keys are not the builder's: "
        f"{sorted(with_a_record - without_a_record)} against "
        f"{sorted(_CONDITIONAL_BUILDER_KEYS)}."
    )
    assert without_a_record - with_a_record == set(), (
        "A row that recorded a withholding lost a key a silent row keeps: "
        f"{sorted(without_a_record - with_a_record)}."
    )


# ============================================================
# THE WRITE PATH
# ============================================================

# chapters_checked_at is written by _mark_chapters_checked and the chapters
# backfill, on their own UPDATE, never by the book upsert -- a book fetch
# says nothing about whether its chapters have been looked for, and binding
# it here would stamp every book as checked on every write. It is the only
# column on this table the book upsert deliberately does not carry.
_COLUMNS_THE_BOOK_UPSERT_DOES_NOT_BIND = {"chapters_checked_at"}

# asin is the conflict key -- setting it in the update clause would assign a
# row its own primary key, which is either a no-op or a rewrite of the key
# being matched on. created_at is absent so a later write cannot reset a
# book's real creation time; tests/integration/test_book_merge_asymmetries.py
# holds that behaviour against a real database.
_BOUND_BUT_NEVER_UPDATED = {"asin", "created_at"}


def test_the_writer_binds_every_column_the_book_table_has():
    """A column on the model that the upsert never binds stores NULL for the
    life of the row.

    Nothing else in the suite sees this at unit level: the response-shape
    identity above is satisfied by a builder, a reader and a model that all
    name the field, and every one of them is intact while the writer drops
    it on the floor. What makes it hard to notice in review is that the
    fresh path looks perfect -- the field is populated on the response that
    normalized it, and null on every response served from the row
    afterwards.
    """
    bound = set(_book_params({"asin": "B0PARITY001"}, datetime.now(timezone.utc)))
    columns = set(Book.__table__.columns.keys())

    unbound = sorted(columns - bound - _COLUMNS_THE_BOOK_UPSERT_DOES_NOT_BIND)
    assert unbound == [], (
        f"{unbound} are columns on books that _book_params never binds, so "
        "they stay NULL however complete the response was. Bind them, or add "
        "them to _COLUMNS_THE_BOOK_UPSERT_DOES_NOT_BIND with the reason."
    )

    unknown = sorted(bound - columns)
    assert unknown == [], (
        f"_book_params binds {unknown}, which are not columns on books. The "
        "statement will fail at execution against a real database."
    )


def test_every_bound_column_is_also_merged_on_conflict():
    """The second write, which is the one almost every book gets.

    A column bound on insert but missing from the update clause is written
    once and then frozen: the first fetch of a book fills it and no later
    fetch ever moves it again. That is invisible to any test that writes a
    book once, and invisible to the response-shape identity above, because
    the value is there -- just permanently the first one seen.
    """
    bound = set(_book_params({"asin": "B0PARITY001"}, datetime.now(timezone.utc)))
    merged = _upsert_set_columns()

    never_merged = sorted(bound - merged - _BOUND_BUT_NEVER_UPDATED)
    assert never_merged == [], (
        f"{never_merged} are bound on insert but absent from the upsert's "
        "ON CONFLICT DO UPDATE clause, so they are written once and never "
        "refreshed."
    )

    merged_but_unbound = sorted(merged - bound)
    assert merged_but_unbound == [], (
        f"{merged_but_unbound} are merged on conflict but never bound, so the "
        "update clause has no incoming value to merge."
    )
