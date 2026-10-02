"""
Unit tests for libex_core.shaping: the in-memory filter and sort applied to live
book lists, and the field specs that define the filterable and sortable surface.
"""

# Standard library
import subprocess
import sys
from pathlib import Path

# Third party
import pytest

# Local
from libex_core import shaping
from libex_core.shaping import (
    BOOK_FILTER_FIELDS,
    BOOK_FILTER_SPECS,
    BOOK_SORT_FIELDS,
    BookSortField,
    FilterSpec,
    SortOrder,
    filter_dicts,
    sort_dicts,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Section: fixtures


def _book(asin, **fields):
    return {"asin": asin, **fields}


def _asins(books):
    return [b["asin"] for b in books]


# Section: specs

EXPECTED_FILTER_NAMES = [
    "language",
    "book_format",
    "explicit",
    "whisper_sync",
    "has_pdf",
    "is_vvab",
    "plan_name",
    "rating_better_than",
    "rating_worse_than",
    "longer_than",
    "shorter_than",
    "genre",
]


def test_filter_specs_names_and_order_are_the_published_surface():
    assert [s.name for s in BOOK_FILTER_SPECS] == EXPECTED_FILTER_NAMES


def test_filter_specs_types():
    types = {s.name: s.type for s in BOOK_FILTER_SPECS}
    assert types == {
        "language": str,
        "book_format": str,
        "explicit": bool,
        "whisper_sync": bool,
        "has_pdf": bool,
        "is_vvab": bool,
        "plan_name": str,
        "rating_better_than": float,
        "rating_worse_than": float,
        "longer_than": int,
        "shorter_than": int,
        "genre": str,
    }


def test_filter_specs_are_filterspecs_with_descriptions():
    for spec in BOOK_FILTER_SPECS:
        assert isinstance(spec, FilterSpec)
        assert spec.description.strip()


def test_filter_fields_derived_from_specs():
    assert BOOK_FILTER_FIELDS == {s.name for s in BOOK_FILTER_SPECS}
    assert len(BOOK_FILTER_FIELDS) == len(BOOK_FILTER_SPECS) == 12


def test_sort_fields_tuple_and_enum_agree():
    assert BOOK_SORT_FIELDS == (
        "title",
        "releaseDate",
        "rating",
        "lengthMinutes",
        "language",
        "publisher",
        "updatedAt",
    )
    assert [m.name for m in BookSortField] == list(BOOK_SORT_FIELDS)
    assert [m.value for m in BookSortField] == list(BOOK_SORT_FIELDS)


def test_sort_order_members():
    assert [m.value for m in SortOrder] == ["asc", "desc"]


# Section: filter_dicts


def test_no_active_filters_returns_input_unchanged():
    items = [_book("A"), _book("B")]
    assert filter_dicts(items, {}) is items
    assert filter_dicts(items, {"language": None, "genre": None}) is items


def test_unknown_filter_keys_are_ignored():
    items = [_book("A", language="en")]
    assert filter_dicts(items, {"nonsense": "x"}) is items
    assert _asins(filter_dicts(items, {"nonsense": "x", "language": "fr"})) == []


def test_returns_new_list_preserving_order_when_filtering():
    items = [_book("A", language="en"), _book("B", language="fr"), _book("C", language="en")]
    out = filter_dicts(items, {"language": "en"})
    assert _asins(out) == ["A", "C"]
    assert out is not items


@pytest.mark.parametrize(
    "name,key,match,miss",
    [
        ("language", "language", "english", "french"),
        ("book_format", "bookFormat", "unabridged", "abridged"),
        ("explicit", "explicit", True, False),
        ("whisper_sync", "whisperSync", True, False),
        ("has_pdf", "hasPdf", True, False),
        ("is_vvab", "isVvab", True, False),
    ],
)
def test_equality_filters(name, key, match, miss):
    items = [_book("HIT", **{key: match}), _book("MISS", **{key: miss}), _book("NONE")]
    assert _asins(filter_dicts(items, {name: match})) == ["HIT"]


def test_false_boolean_filter_is_active_and_excludes_missing():
    items = [_book("T", explicit=True), _book("F", explicit=False), _book("N")]
    assert _asins(filter_dicts(items, {"explicit": False})) == ["F"]


def test_plan_name_membership():
    items = [
        _book("A", plans=["US Minerva", "Other"]),
        _book("B", plans=["Other"]),
        _book("C", plans=None),
        _book("D"),
    ]
    assert _asins(filter_dicts(items, {"plan_name": "US Minerva"})) == ["A"]


def test_plan_name_is_exact_not_substring():
    items = [_book("A", plans=["US Minerva"])]
    assert filter_dicts(items, {"plan_name": "Minerva"}) == []


def test_rating_better_than_is_inclusive_and_drops_none():
    items = [
        _book("LOW", rating=3.9),
        _book("EQ", rating=4.0),
        _book("HI", rating=4.5),
        _book("NONE", rating=None),
        _book("ABSENT"),
    ]
    assert _asins(filter_dicts(items, {"rating_better_than": 4.0})) == ["EQ", "HI"]


def test_rating_worse_than_is_inclusive_and_drops_none():
    items = [
        _book("LOW", rating=3.9),
        _book("EQ", rating=4.0),
        _book("HI", rating=4.5),
        _book("NONE", rating=None),
        _book("ABSENT"),
    ]
    assert _asins(filter_dicts(items, {"rating_worse_than": 4.0})) == ["LOW", "EQ"]


def test_zero_threshold_is_active():
    items = [_book("Z", rating=0.0), _book("N", rating=None)]
    assert _asins(filter_dicts(items, {"rating_worse_than": 0.0})) == ["Z"]
    items = [_book("Z", lengthMinutes=0), _book("N")]
    assert _asins(filter_dicts(items, {"shorter_than": 0})) == ["Z"]


def test_longer_than_is_inclusive_and_drops_none():
    items = [
        _book("S", lengthMinutes=599),
        _book("EQ", lengthMinutes=600),
        _book("L", lengthMinutes=900),
        _book("NONE", lengthMinutes=None),
        _book("ABSENT"),
    ]
    assert _asins(filter_dicts(items, {"longer_than": 600})) == ["EQ", "L"]


def test_shorter_than_is_inclusive_and_drops_none():
    items = [
        _book("S", lengthMinutes=599),
        _book("EQ", lengthMinutes=600),
        _book("L", lengthMinutes=900),
        _book("NONE", lengthMinutes=None),
        _book("ABSENT"),
    ]
    assert _asins(filter_dicts(items, {"shorter_than": 600})) == ["S", "EQ"]


def test_genre_partial_case_insensitive_match():
    items = [
        _book("A", genres=[{"name": "Epic Fantasy"}]),
        _book("B", genres=[{"name": "Romance"}, {"name": "Urban FANTASY"}]),
        _book("C", genres=[{"name": "Thriller"}]),
        _book("D", genres=None),
        _book("E", genres=[{}]),
        _book("F"),
    ]
    assert _asins(filter_dicts(items, {"genre": "fantasy"})) == ["A", "B"]


def test_filters_combine_with_and():
    items = [
        _book("A", language="en", rating=4.5, lengthMinutes=700),
        _book("B", language="en", rating=3.0, lengthMinutes=700),
        _book("C", language="fr", rating=4.5, lengthMinutes=700),
        _book("D", language="en", rating=4.5, lengthMinutes=100),
    ]
    out = filter_dicts(
        items, {"language": "en", "rating_better_than": 4.0, "longer_than": 600}
    )
    assert _asins(out) == ["A"]


def test_every_spec_filter_is_applied_by_filter_dicts():
    """Each published filter, set alone, must change the result of some list."""
    book = {
        "asin": "X",
        "language": "english",
        "bookFormat": "unabridged",
        "explicit": True,
        "whisperSync": True,
        "hasPdf": True,
        "isVvab": True,
        "plans": ["P"],
        "rating": 4.0,
        "lengthMinutes": 600,
        "genres": [{"name": "Fantasy"}],
    }
    excluding = {
        "language": "klingon",
        "book_format": "nope",
        "explicit": False,
        "whisper_sync": False,
        "has_pdf": False,
        "is_vvab": False,
        "plan_name": "nope",
        "rating_better_than": 4.5,
        "rating_worse_than": 3.5,
        "longer_than": 700,
        "shorter_than": 500,
        "genre": "nope",
    }
    assert set(excluding) == BOOK_FILTER_FIELDS
    for name, value in excluding.items():
        assert filter_dicts([book], {name: value}) == [], name


# Section: sort_dicts

ALLOWED_FORMS = [BOOK_SORT_FIELDS, {f: None for f in BOOK_SORT_FIELDS}, set(BOOK_SORT_FIELDS)]


@pytest.mark.parametrize("allowed", ALLOWED_FORMS, ids=["tuple", "dict", "set"])
def test_sort_accepts_any_container_of_fields(allowed):
    items = [_book("B", title="b"), _book("A", title="a")]
    assert _asins(sort_dicts(items, "title", "asc", allowed)) == ["A", "B"]


def test_sort_dict_allowed_tests_keys_only():
    items = [_book("B", title="b"), _book("A", title="a")]
    assert sort_dicts(items, "title", "asc", {"other": 1}) is items


def test_sort_none_or_empty_or_unknown_returns_input_unchanged():
    items = [_book("B", title="b"), _book("A", title="a")]
    assert sort_dicts(items, None, "asc", BOOK_SORT_FIELDS) is items
    assert sort_dicts(items, "", "asc", BOOK_SORT_FIELDS) is items
    assert sort_dicts(items, "description", "asc", BOOK_SORT_FIELDS) is items


@pytest.mark.parametrize(
    "field,values",
    [
        ("title", ["a", "b", "c"]),
        ("releaseDate", ["2020-01-01", "2021-01-01", "2022-01-01"]),
        ("rating", [1.0, 2.5, 4.0]),
        ("lengthMinutes", [10, 20, 30]),
        ("language", ["a", "b", "c"]),
        ("publisher", ["a", "b", "c"]),
        ("updatedAt", ["2020-01-01", "2021-01-01", "2022-01-01"]),
    ],
)
def test_each_sort_field_asc_and_desc(field, values):
    items = [_book("MID", **{field: values[1]}), _book("HI", **{field: values[2]}), _book("LO", **{field: values[0]})]
    assert _asins(sort_dicts(items, field, "asc", BOOK_SORT_FIELDS)) == ["LO", "MID", "HI"]
    assert _asins(sort_dicts(items, field, "desc", BOOK_SORT_FIELDS)) == ["HI", "MID", "LO"]


def test_order_defaults_to_asc_and_is_case_insensitive():
    items = [_book("B", rating=2.0), _book("A", rating=1.0)]
    assert _asins(sort_dicts(items, "rating", None, BOOK_SORT_FIELDS)) == ["A", "B"]
    assert _asins(sort_dicts(items, "rating", "", BOOK_SORT_FIELDS)) == ["A", "B"]
    assert _asins(sort_dicts(items, "rating", "DESC", BOOK_SORT_FIELDS)) == ["B", "A"]
    assert _asins(sort_dicts(items, "rating", "bogus", BOOK_SORT_FIELDS)) == ["A", "B"]


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_none_and_missing_sort_last_in_either_direction(order):
    items = [
        _book("N1", rating=None),
        _book("MID", rating=2.0),
        _book("ABS"),
        _book("HI", rating=3.0),
        _book("LO", rating=1.0),
    ]
    out = _asins(sort_dicts(items, "rating", order, BOOK_SORT_FIELDS))
    head = ["LO", "MID", "HI"] if order == "asc" else ["HI", "MID", "LO"]
    assert out[:3] == head
    assert out[3:] == ["N1", "ABS"]  # input order kept among the missing


def test_zero_value_is_present_not_missing():
    items = [_book("N", lengthMinutes=None), _book("Z", lengthMinutes=0), _book("P", lengthMinutes=5)]
    assert _asins(sort_dicts(items, "lengthMinutes", "asc", BOOK_SORT_FIELDS)) == ["Z", "P", "N"]


def test_sort_does_not_mutate_input():
    items = [_book("B", title="b"), _book("A", title="a")]
    sort_dicts(items, "title", "asc", BOOK_SORT_FIELDS)
    assert _asins(items) == ["B", "A"]


# Section: isolation


def test_shaping_imports_no_web_framework_or_app():
    code = (
        "import sys, libex_core.shaping\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "('fastapi','starlette','pydantic','sqlalchemy','app')]\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={"PATH": "", "PYTHONPATH": str(REPO_ROOT)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_shaping_source_has_no_forbidden_imports():
    import ast

    tree = ast.parse(Path(shaping.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert not roots & {"fastapi", "starlette", "pydantic", "sqlalchemy", "app"}
