"""
The live book-list query surface (filters, sort, order) is built from
libex_core.shaping. These tests pin that the HTTP layer neither drifts from the
specs nor changed against the published OpenAPI document.
"""

# Standard library
import inspect
import json
from pathlib import Path
from typing import get_args

# Local
from app.api.routes.filter_params import LiveBookFilters
from app.api.routes.sort_params import BookSortField, SortOrder
from app.main import app
from libex_core.shaping import BOOK_FILTER_SPECS, BOOK_SORT_FIELDS

GOLDEN = Path(__file__).resolve().parent.parent / "goldens" / "live_list_query_params.json"


def test_live_book_filters_signature_equals_specs():
    params = list(inspect.signature(LiveBookFilters).parameters.values())
    assert [p.name for p in params] == [s.name for s in BOOK_FILTER_SPECS]
    for p, spec in zip(params, BOOK_FILTER_SPECS):
        assert p.default is None
        base, query = get_args(p.annotation)[0], get_args(p.annotation)[1]
        assert base == spec.type | None
        assert query.description == spec.description


def test_live_book_filters_stores_values_and_defaults_none():
    f = LiveBookFilters(language="en", longer_than=5)
    kwargs = f.as_kwargs()
    assert list(kwargs) == [s.name for s in BOOK_FILTER_SPECS]
    assert kwargs["language"] == "en"
    assert kwargs["longer_than"] == 5
    assert kwargs["genre"] is None


def test_sort_enums_follow_shaping():
    assert [m.value for m in BookSortField] == list(BOOK_SORT_FIELDS)
    assert [m.value for m in SortOrder] == ["asc", "desc"]


def test_live_list_openapi_query_params_match_main_snapshot():
    """Query params of every live list route equal the snapshot taken from main."""
    expected = json.loads(GOLDEN.read_text())
    spec = app.openapi()
    actual = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            key = f"{method.upper()} {path}"
            if key in expected:
                actual[key] = [p for p in op.get("parameters", []) if p["in"] == "query"]
    assert set(actual) == set(expected)
    for key in expected:
        assert actual[key] == expected[key], key
