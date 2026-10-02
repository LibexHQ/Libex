"""
The merge rules that keep Libex from ever accepting less than it holds, as SQL
expression builders that run on Postgres and SQLite.

Each builder takes the incoming value and the stored column and returns the
expression that settles which one the row ends up with. They are the field
level mechanism behind the rule that when Audible returns less than is stored,
the stored value stays: never a whole-response accept or reject.

On Postgres each builder compiles to exactly the SQL the hosted writer emits;
that is the contract, and the tests hold it to the character. On SQLite the
pieces Postgres has and SQLite lacks are swapped for their equivalents (see
`libex_core.storage.dialect`), and the result is the same row.
"""

# Third party
from sqlalchemy import JSON, bindparam, case, cast, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import ColumnElement
from sqlalchemy.types import Boolean

# Local
from libex_core.storage.dialect import JSON_CONTAINS, JSON_MERGE, DialectVariant
from libex_core.storage.types import JSONDocument

# Every character Unicode gives the White_Space property, for the trim's
# second argument. A trim with no second argument takes U+0020 and nothing
# else -- not a tab, not a newline, not the U+00A0 a copied web page leaves
# behind, not the U+3000 ideographic space that is ordinary in the Japanese
# catalogue -- so a value made of any of those would pass for a real answer.
# Naming the set is what makes "blank" mean blank, and it is the same set on
# both backends.
#
# Written as escapes rather than as the characters themselves: most are
# invisible and several are indistinguishable from a plain space, so a literal
# set could be corrupted by an ordinary edit with nothing to show it.
#
# The set is exactly White_Space and stops there. Zero-width format characters
# -- U+200B, U+FEFF and their neighbours -- are not whitespace in Unicode and
# are not trimmed, so a value made only of those still reads as an answer.
BLANK_CHARS = (
    "\t\n\v\f\r "  # U+0009..U+000D, U+0020
    "\u0085"  # next line
    " "  # no-break space
    " "  # ogham space mark
    "           "
    "  "  # line separator, paragraph separator
    " "  # narrow no-break space
    " "  # medium mathematical space
    "　"  # ideographic space
)

# The bind type for a JSON column that must be able to say "no value". A
# Python None serialises to the JSON value null by default, which is a value
# and not SQL NULL, so a coalesce would take it in preference to the stored
# one and empty the column. none_as_null makes None bind as SQL NULL.
_JSON_BIND = JSONB(none_as_null=True).with_variant(JSON(none_as_null=True), "sqlite")


def json_bind(name: str) -> ColumnElement:
    """A named bind for a JSON column, SQL NULL when the value is None.

    Postgres casts the bind to jsonb, as the hosted writer does. SQLite gets
    the bare bind: its CAST(? AS JSON) does not parse the text, it reads the
    leading digits and returns the number 0, which would overwrite the column.
    """
    parameter = bindparam(name, type_=_JSON_BIND)
    return DialectVariant(cast(parameter, JSONB), bindparam(name, type_=_JSON_BIND))


def coalesce(new_value, existing_col):
    """Returns new_value if not null, otherwise keeps the existing column value."""
    return func.coalesce(new_value, existing_col)


def _trimmed(value) -> ColumnElement:
    # btrim is Postgres's spelling; SQLite's trim takes the same set argument
    # and reads it as characters, not bytes.
    return DialectVariant(func.btrim(value, BLANK_CHARS), func.trim(value, BLANK_CHARS))


def answered(new_value, existing_col):
    """
    Keeps the incoming value only where Audible actually answered, so a blank
    response cannot blank a stored one.

    coalesce is the right merge wherever "no answer" reaches SQL as NULL. It is
    the wrong merge for text columns, because a blank from Audible is not
    always a null: a response group that carries a field with nothing to put in
    it sends an empty string, and coalesce('', stored) is '' -- SQL sees a value
    and takes it, so a stored publisher is replaced by nothing. That is the
    shrinkage rule failing inside the merge written to enforce it.

    Emptiness is measured after trimming every character BLANK_CHARS names, so
    an all-whitespace value counts as no answer. Only the measurement is
    trimmed; what is written is the value received, verbatim, because cleaning
    what Audible sends belongs to the fetch layer and this function chooses
    between two values and alters neither.

    NULL keeps behaving as it always did: the trim of NULL is NULL, NULL != ''
    is NULL rather than true, and the CASE falls to its ELSE, the stored value.

    Not a rule for every column. It fits only where a blank cannot be an
    assertion; where Audible could mean "none" by sending nothing, swallowing
    the blank would pin a stale value in place forever.
    """
    return case(
        (_trimmed(new_value) != "", new_value),
        else_=existing_col,
    )


def longer_wins(new_value, existing_col):
    """
    Keeps whichever value carries more text, so a later, richer Audible
    response replaces a thinner stored one and never the reverse.

    Both lengths are floored to a sentinel rather than compared directly,
    because length(NULL) is NULL and a comparison against NULL is NULL, not
    false. A bare length(new) > length(existing) falls to the ELSE branch
    whenever the stored value is NULL and pins that NULL permanently: no
    incoming description, however long, could fill a column first written empty.

    An incoming value that is empty or entirely whitespace (BLANK_CHARS)
    measures as absent, so it cannot displace a stored NULL. Only the
    measurement is trimmed; the value written is the value received. The
    incoming side is trimmed and the stored side is not, and on a tie the
    stored value stays.
    """
    absent = -1
    new_length = func.coalesce(func.length(func.nullif(_trimmed(new_value), "")), absent)
    existing_length = func.coalesce(func.length(existing_col), absent)
    return case(
        (new_length > existing_length, new_value),
        else_=existing_col,
    )


def chapter_count(payload):
    """
    Counts the chapters carried by a TrackContentDto payload, without ever
    raising on one that carries none.

    The payload may be anything Audible answered, or anything an earlier
    version of the normalizer stored: an object with no chapters key, a json
    null, a string. Postgres's jsonb_array_length raises on anything that is
    not an array, so the inner CASE replaces the value rather than testing it:
    both of its arms are arrays, and the error is structurally unreachable. An
    AND of the type test and the length would survive in the positions used
    today and fail in a WHERE, where the planner reorders quals.

    SQLite's json_array_length does not raise on a non-array, but the same
    CASE shape is kept so the two backends read as one rule.
    """
    chapters = payload["chapters"]
    on_postgres = func.jsonb_array_length(
        case(
            (func.jsonb_typeof(chapters) == "array", chapters),
            else_=func.jsonb_build_array(),
        )
    )
    on_sqlite = case(
        (func.json_type(payload, "$.chapters") == "array", func.json_array_length(payload, "$.chapters")),
        else_=0,
    )
    return DialectVariant(on_postgres, on_sqlite)


def chaptered_wins(new_value, existing_col):
    """
    Keeps whichever chapter payload actually lists chapters, so a response
    that carries none cannot erase one that is already stored.

    The floor is emptiness and nothing finer, deliberately. An empty list
    asserts nothing, so preferring the stored payload loses no answer Audible
    gave. Two non-empty lists are two real answers, and the shorter one is not
    necessarily the poorer -- a reissue can re-cut a title into fewer, longer
    chapters, and a count floor would pin the first list ever seen. Merging the
    two is meaningless: chapters are an ordered whole.

    The payload is replaced whole, so everything riding in it is replaced by a
    later chaptered response, not merged with the stored one. When neither
    payload lists chapters the incoming one is taken.
    """
    return case(
        (chapter_count(new_value) > 0, new_value),
        (chapter_count(existing_col) > 0, existing_col),
        else_=new_value,
    )


def extras_union(new_value, existing_col):
    """
    Merges two extras blobs key by key, so a thin response adds to a rich
    stored blob and can never replace it.

    The blob is not all-or-nothing from Audible's side: the same ASIN returns
    49 top-level keys while it is purchasable and 32 once it is not. Taking the
    incoming blob whole would be the shrinkage rule failing inside the merge;
    comparing text length would let one long review outweigh ten dropped keys.
    Different response groups populate different top-level keys, so combining
    them is the only merge that keeps what each response contributed.

    Both NULL arms are load-bearing. NULL in a stored column means no response
    has written the row yet, and coalesce(stored, '{}') || coalesce(new, '{}')
    would erase that. A NULL incoming blob means the extras were dropped whole
    at normalization, so it must leave the stored value exactly as it was.

    The containment arm does two jobs. It is a write guard: returning the
    stored datum unchanged rewrites nothing out of line on Postgres, 368 bytes
    of WAL a row against 9,463. And it protects data: || is shallow, so an
    incoming object with fewer sub-keys would replace a richer stored
    sub-object whole; when the stored blob already contains the incoming one,
    in every nested key and array element, it is kept as it is. SQLite has
    neither operator, so the two are registered functions with jsonb's
    semantics (see `libex_core.storage.dialect`), not an approximation.

    The result is a union over time, never a snapshot: a key Audible stops
    sending is never removed.
    """
    contains = DialectVariant(
        existing_col.contains(new_value),
        getattr(func, JSON_CONTAINS)(existing_col, new_value, type_=Boolean),
    )
    merged = DialectVariant(
        existing_col.op("||", return_type=JSONB)(new_value),
        getattr(func, JSON_MERGE)(existing_col, new_value, type_=JSONDocument),
    )
    return case(
        (new_value.is_(None), existing_col),
        (existing_col.is_(None), new_value),
        (contains, existing_col),
        else_=merged,
    )
