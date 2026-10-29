"""
Strict input models for a hand-entered book: every accepted key is one the
writer stores, every other key is refused loudly at every level, and every
cap and format check turns a bad value into an error carrying only a location
and a type.
"""

# Standard library
import copy
import json
from datetime import datetime, timezone

# Third party
import pytest
from pydantic import ValidationError

# Local
from app.core.ingest_models import (
    AUTHOR_FIELDS,
    BOOK_FIELDS,
    CHAPTER_ITEM_FIELDS,
    CHAPTERS_FIELDS,
    GENRE_FIELDS,
    NARRATOR_FIELDS,
    SERIES_FIELDS,
    IngestAuthor,
    IngestBody,
    IngestBook,
    IngestChapterItem,
    IngestChapters,
    IngestGenre,
    IngestNarrator,
    IngestSeries,
    error_list,
)
from libex_core.models import BookResponse
from libex_core.storage.write.params import book_params
from tests.fixtures.manual_entry import FULL_BOOK, FULL_CHAPTERS

ASIN = "B0MANUAL01"

MINIMAL_BOOK = {
    "asin": ASIN, "region": "us", "title": "T", "language": "english",
    "releaseDate": "2020-01-02", "authors": [{"name": "A"}],
}
MINIMAL_CHAPTERS = {
    "runtimeLengthMs": 1000,
    "chapters": [{"title": "One", "lengthMs": 1000, "startOffsetMs": 0}],
}


def _book(**over):
    return {**copy.deepcopy(MINIMAL_BOOK), **over}


def _errors(payload) -> list[dict]:
    with pytest.raises(ValidationError) as caught:
        IngestBody.model_validate_json(json.dumps(payload))
    return error_list(caught.value)


def _parse(payload) -> IngestBody:
    return IngestBody.model_validate_json(json.dumps(payload))


# ============================================================
# ACCEPTED KEYS ARE STORED KEYS
# ============================================================


def test_the_full_body_parses_and_round_trips_every_key():
    body = _parse({"book": FULL_BOOK, "chapters": FULL_CHAPTERS})
    assert body.book.model_dump(exclude_unset=True) == FULL_BOOK
    assert body.chapters.model_dump(exclude_unset=True) == FULL_CHAPTERS


@pytest.mark.parametrize(
    "model,fields",
    [
        (IngestBook, BOOK_FIELDS), (IngestAuthor, AUTHOR_FIELDS),
        (IngestNarrator, NARRATOR_FIELDS), (IngestSeries, SERIES_FIELDS),
        (IngestGenre, GENRE_FIELDS), (IngestChapters, CHAPTERS_FIELDS),
        (IngestChapterItem, CHAPTER_ITEM_FIELDS),
    ],
)
def test_each_model_accepts_exactly_the_keys_its_map_lists(model, fields):
    assert set(model.model_fields) == set(fields)


def test_the_full_book_uses_every_accepted_key():
    assert set(FULL_BOOK) == set(BOOK_FIELDS)
    for key, model in (("authors", AUTHOR_FIELDS), ("narrators", NARRATOR_FIELDS),
                       ("series", SERIES_FIELDS), ("genres", GENRE_FIELDS)):
        assert set(FULL_BOOK[key][0]) == set(model)
    assert set(FULL_CHAPTERS) == set(CHAPTERS_FIELDS)
    assert set(FULL_CHAPTERS["chapters"][0]) == set(CHAPTER_ITEM_FIELDS)


def test_every_accepted_scalar_book_key_changes_what_the_writer_binds():
    # An accepted key the writer ignores would be dropped silently.
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    seed = {"asin": ASIN, "region": "us", "title": "t"}
    base = book_params(seed, now)
    unbound = [
        key for key, target in BOOK_FIELDS.items()
        if target.startswith("books.") and key not in seed
        and book_params({**seed, key: FULL_BOOK[key]}, now) == base
    ]
    assert unbound == []


def test_every_column_the_writer_binds_from_a_book_key_has_an_accepted_key():
    bound_columns = set(book_params({"asin": ASIN}, datetime(2026, 1, 1, tzinfo=timezone.utc)))
    stamped = {"confirmed_at", "manual_entry_at", "created_at", "updated_at"}
    mapped = {v.split(".", 1)[1] for v in BOOK_FIELDS.values() if v.startswith("books.")}
    assert bound_columns - stamped - mapped == set()
    assert mapped - bound_columns == set()


def test_the_writer_stamps_are_not_accepted_keys():
    for refused in ("manualEntry", "manual_entry_at", "confirmedAt", "createdAt"):
        assert refused not in IngestBook.model_fields


# ============================================================
# EVERY OTHER KEY IS REFUSED
# ============================================================

# Derived or foreign fields BookResponse serves that the writer does not store.
DERIVED = ["link", "regions", "betterType", "isAvailable", "manualEntry", "bookRegion",
           "createdAt", "updatedAt", "id", "unknownKey"]


@pytest.mark.parametrize("key", DERIVED)
def test_a_book_level_refused_key_is_extra_forbidden(key):
    assert _errors({"book": {**_book(), key: "x"}}) == [
        {"loc": ["book", key], "type": "extra_forbidden"}
    ]


@pytest.mark.parametrize("key", DERIVED)
def test_a_body_level_refused_key_is_extra_forbidden(key):
    assert _errors({"book": _book(), key: "x"}) == [{"loc": [key], "type": "extra_forbidden"}]


@pytest.mark.parametrize("key", DERIVED + ["bookAsin"])
@pytest.mark.parametrize(
    "where,value,path",
    [
        ("authors", {"name": "A"}, ["book", "authors", 0]),
        ("narrators", {"name": "N"}, ["book", "narrators", 0]),
        ("series", {"asin": "B00SERIES1", "name": "S"}, ["book", "series", 0]),
        ("genres", {"asin": "123", "name": "G", "type": "Tags"}, ["book", "genres", 0]),
    ],
)
def test_a_nested_refused_key_is_extra_forbidden(key, where, value, path):
    assert _errors({"book": _book(**{where: [{**value, key: "x"}]})}) == [
        {"loc": path + [key], "type": "extra_forbidden"}
    ]


@pytest.mark.parametrize("key", DERIVED)
def test_a_chapters_and_chapter_item_refused_key_is_extra_forbidden(key):
    on_chapters = _errors({"book": _book(), "chapters": {**MINIMAL_CHAPTERS, key: 1}})
    assert on_chapters == [{"loc": ["chapters", key], "type": "extra_forbidden"}]

    item = {**MINIMAL_CHAPTERS["chapters"][0], key: 1}
    on_item = _errors({"book": _book(), "chapters": {**MINIMAL_CHAPTERS, "chapters": [item]}})
    assert on_item == [{"loc": ["chapters", "chapters", 0, key], "type": "extra_forbidden"}]

    sub = {**item, "chapters": [{**MINIMAL_CHAPTERS["chapters"][0], key: 1}]}
    deep = _errors({"book": _book(), "chapters": {**MINIMAL_CHAPTERS, "chapters": [{**MINIMAL_CHAPTERS["chapters"][0], "chapters": sub["chapters"]}]}})
    assert deep == [{"loc": ["chapters", "chapters", 0, "chapters", 0, key], "type": "extra_forbidden"}]


def test_every_book_response_key_is_either_accepted_or_refused_never_ignored():
    for key in BookResponse.model_fields:
        accepted = key in IngestBook.model_fields
        if accepted:
            continue
        errors = _errors({"book": {**_book(), key: "x"}})
        assert errors == [{"loc": ["book", key], "type": "extra_forbidden"}], key


# ============================================================
# DEFAULTS
# ============================================================


def test_the_minimal_book_parses_and_the_three_flags_default_false():
    body = _parse({"book": MINIMAL_BOOK})
    assert (body.book.isListenable, body.book.isBuyable, body.book.isVvab) == (False, False, False)
    assert body.chapters is None


# ============================================================
# REQUIRED FIELDS, TYPES AND CAPS
# ============================================================


@pytest.mark.parametrize("missing", ["asin", "region", "title", "language", "releaseDate", "authors"])
def test_a_missing_required_book_key_is_refused(missing):
    book = _book()
    del book[missing]
    assert _errors({"book": book}) == [{"loc": ["book", missing], "type": "missing"}]


def test_the_body_without_a_book_is_refused():
    assert _errors({}) == [{"loc": ["book"], "type": "missing"}]


@pytest.mark.parametrize(
    "field,value",
    [
        ("asin", "nope"), ("asin", ""), ("asin", "B0 MANUAL1"),
        ("region", "xx"), ("region", ""),
        ("title", ""), ("title", "   "), ("title", "x" * 1001), ("title", "a\x00b"),
        ("language", " "), ("language", "x" * 51),
        ("releaseDate", "not a date"), ("releaseDate", "2020-13-45"),
        ("publicationDatetime", "yesterday"),
        ("rating", 5.1), ("rating", -0.1), ("rating", "4"),
        ("numRatings", -1), ("numRatings", 10**9 + 1), ("numReviews", -1),
        ("lengthMinutes", -1), ("lengthMinutes", 10**7 + 1),
        ("explicit", "yes"), ("explicit", 1), ("isBuyable", "true"),
        ("subtitle", "x" * 1001), ("description", "x" * 65537),
        ("publisher", "x" * 2001), ("isbn", "x" * 17), ("sku", "x" * 21),
        ("episodeNumber", "x" * 21), ("bookFormat", "x" * 51),
        ("plans", ["x"] * 51), ("plans", [1]),
        ("authors", []), ("authors", [{"name": "A"}] * 51),
        ("narrators", [{"name": "N"}] * 51), ("series", [{"asin": "B00SERIES1", "name": "S"}] * 51),
        ("genres", [{"asin": "1", "name": "G", "type": "Tags"}] * 201),
    ],
)
def test_a_bad_book_value_is_refused_at_its_own_location(field, value):
    errors = _errors({"book": {**_book(), field: value}})
    assert errors, (field, value)
    assert all(e["loc"][:2] == ["book", field] for e in errors)
    assert all(set(e) == {"loc", "type"} for e in errors)


@pytest.mark.parametrize(
    "field,value",
    [
        ("rating", 0), ("rating", 5), ("numRatings", 0), ("numRatings", 10**9),
        ("lengthMinutes", 10**7), ("title", "x" * 1000), ("description", "x" * 65536),
        ("publicationDatetime", "2020-01-02T03:04:05+02:00"),
        ("releaseDate", "2020-01-02T00:00:00"),
    ],
)
def test_the_boundary_value_is_accepted(field, value):
    assert _parse({"book": {**_book(), field: value}}).book is not None


def test_ints_are_not_coerced_from_strings_or_floats():
    assert _errors({"book": {**_book(), "numRatings": "5"}})
    assert _errors({"book": {**_book(), "lengthMinutes": 5.5}})


@pytest.mark.parametrize(
    "image",
    [
        "http://img.example/a.jpg", "ftp://x/y", "//img.example/a.jpg", "javascript:alert(1)",
        "https://", "https://user:pw@img.example/a.jpg", "https://img.example:8443/a.jpg",
        "https://img.example/a b.jpg", "https://img.example/a\n.jpg", "https://img.example/\x7f",
        "https://img.example/" + "a" * 2050, "", "relative/path.jpg", "https://[::1/a",
    ],
)
def test_a_bad_image_url_is_refused_on_book_and_author(image):
    assert _errors({"book": {**_book(), "imageUrl": image}})[0]["loc"] == ["book", "imageUrl"]
    author = {"name": "A", "image": image}
    assert _errors({"book": _book(authors=[author])})[0]["loc"] == ["book", "authors", 0, "image"]


def test_a_good_image_url_is_accepted_with_the_default_port():
    assert _parse({"book": {**_book(), "imageUrl": "https://img.example:443/a.jpg"}})


# ============================================================
# NESTED VALUES
# ============================================================


@pytest.mark.parametrize(
    "where,entry,loc",
    [
        ("authors", {"name": " "}, ["book", "authors", 0, "name"]),
        ("authors", {"name": "A", "asin": "bad"}, ["book", "authors", 0, "asin"]),
        ("authors", {"name": "A", "description": "x" * 65537}, ["book", "authors", 0, "description"]),
        ("authors", {"name": "A\x00"}, ["book", "authors", 0, "name"]),
        ("narrators", {"name": ""}, ["book", "narrators", 0, "name"]),
        ("series", {"asin": "bad", "name": "S"}, ["book", "series", 0, "asin"]),
        ("series", {"asin": "B00SERIES1", "name": ""}, ["book", "series", 0, "name"]),
        ("series", {"asin": "B00SERIES1", "name": "S", "position": "x" * 101}, ["book", "series", 0, "position"]),
        ("series", {"asin": "B00SERIES1", "name": "S", "region": "xx"}, ["book", "series", 0, "region"]),
        ("genres", {"asin": "", "name": "G", "type": "Tags"}, ["book", "genres", 0, "asin"]),
        ("genres", {"asin": "1" * 13, "name": "G", "type": "Tags"}, ["book", "genres", 0, "asin"]),
        ("genres", {"asin": "12-3", "name": "G", "type": "Tags"}, ["book", "genres", 0, "asin"]),
        ("genres", {"asin": "123", "name": "G", "type": "Other"}, ["book", "genres", 0, "type"]),
        ("genres", {"asin": "123", "name": "G"}, ["book", "genres", 0, "type"]),
    ],
)
def test_a_bad_nested_value_is_refused_at_its_location(where, entry, loc):
    errors = _errors({"book": _book(**{where: [entry]})})
    assert errors[0]["loc"] == loc
    assert set(errors[0]) == {"loc", "type"}


def test_a_series_region_other_than_the_books_is_refused():
    series = [{"asin": "B00SERIES1", "name": "S", "region": "uk"}]
    errors = _errors({"book": _book(series=series)})
    assert errors == [{"loc": ["book"], "type": "value_error"}]
    ok = [{"asin": "B00SERIES1", "name": "S", "region": "us"}, {"asin": "B00SERIES2", "name": "T"}]
    assert _parse({"book": _book(series=ok)})


def test_asins_are_normalised():
    body = _parse({"book": _book(asin=ASIN.lower())})
    assert body.book.asin == ASIN


# ============================================================
# EXTRAS AND CHAPTERS
# ============================================================


@pytest.mark.parametrize(
    "extras",
    [{"k": "x" * 70000}, {"k": "\x00"}, {"k": float("nan")}, {"k": float("inf")}],
    ids=["too-large", "nul", "nan", "infinity"],
)
def test_unstorable_extras_are_refused(extras):
    raw = json.dumps({"book": {**_book(), "audibleExtras": extras}}, allow_nan=True)
    with pytest.raises(ValidationError) as caught:
        IngestBody.model_validate_json(raw)
    assert error_list(caught.value)[0]["loc"] == ["book", "audibleExtras"]


def test_extras_must_be_an_object():
    assert _errors({"book": {**_book(), "audibleExtras": [1]}})


@pytest.mark.parametrize(
    "chapters",
    [
        {**MINIMAL_CHAPTERS, "chapters": []},
        {**MINIMAL_CHAPTERS, "runtimeLengthMs": -1},
        {**MINIMAL_CHAPTERS, "runtimeLengthMs": 10**10 + 1},
        {**MINIMAL_CHAPTERS, "isAccurate": "yes"},
        {"chapters": MINIMAL_CHAPTERS["chapters"]},
        {**MINIMAL_CHAPTERS, "chapters": [{"title": "t", "lengthMs": -1, "startOffsetMs": 0}]},
        {**MINIMAL_CHAPTERS, "chapters": [{"title": "t" * 2001, "lengthMs": 1, "startOffsetMs": 0}]},
        {**MINIMAL_CHAPTERS, "chapters": [{"title": "t", "lengthMs": 1}]},
        {**MINIMAL_CHAPTERS, "chapters": [MINIMAL_CHAPTERS["chapters"][0]] * 2001},
    ],
    ids=["empty", "negative-runtime", "huge-runtime", "bool-string", "no-runtime", "negative-length",
         "long-title", "no-offset", "too-many"],
)
def test_bad_chapters_are_refused(chapters):
    errors = _errors({"book": _book(), "chapters": chapters})
    assert errors and all(e["loc"][0] == "chapters" for e in errors)


def _nested(depth):
    node = {"title": "t", "lengthMs": 1, "startOffsetMs": 0}
    for _ in range(depth - 1):
        node = {"title": "t", "lengthMs": 1, "startOffsetMs": 0, "chapters": [node]}
    return node


def test_sub_chapter_depth_is_bounded():
    ok = {"runtimeLengthMs": 1, "chapters": [_nested(4)]}
    assert _parse({"book": _book(), "chapters": ok}).chapters is not None
    too_deep = {"runtimeLengthMs": 1, "chapters": [_nested(5)]}
    assert _errors({"book": _book(), "chapters": too_deep}) == [
        {"loc": ["chapters"], "type": "value_error"}
    ]


def test_chapters_default_when_only_the_required_keys_are_sent():
    chapters = _parse({"book": _book(), "chapters": MINIMAL_CHAPTERS}).chapters
    assert (chapters.isAccurate, chapters.brandIntroDurationMs, chapters.runtimeLengthSec) == (False, 0, 0)


# ============================================================
# ERROR BODIES
# ============================================================


def test_an_error_carries_only_a_location_and_a_type_and_never_the_value():
    secret = "SECRET-VALUE-9f3a"
    payload = {"book": {**_book(), "rating": secret, "unknown": secret, "region": secret,
                        "authors": [{"name": "A", "image": secret}]}}
    with pytest.raises(ValidationError) as caught:
        IngestBody.model_validate_json(json.dumps(payload))
    errors = error_list(caught.value)
    assert len(errors) == 4
    assert all(set(e) == {"loc", "type"} for e in errors)
    assert secret not in repr(errors)
    # the raw exception does echo it, which is why error_list exists
    assert secret in str(caught.value)


def test_malformed_json_is_a_validation_error_not_a_crash():
    with pytest.raises(ValidationError) as caught:
        IngestBody.model_validate_json(b"{not json")
    assert set(error_list(caught.value)[0]) == {"loc", "type"}
    with pytest.raises(ValidationError):
        IngestBody.model_validate_json(b"[]")
