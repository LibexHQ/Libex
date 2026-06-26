"""
Unit tests for the shared DB filter helpers.

apply_category_filter accepts one or more Audible category ids and narrows a Book
query to books in any of them (a union). These tests compile the statement and
inspect the SQL rather than hitting a database, so they verify the filter shape
directly: single id, a comma-separated list, whitespace handling, and the no-op
cases where nothing usable was passed.
"""

# Third party
from sqlalchemy import select

# Database
from app.db.models import Book

# Services
from app.services.db.filtering import apply_category_filter


def _sql(stmt) -> str:
    """Compiles a statement to SQL with literals inlined, for substring checks."""
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_single_category_filters_by_that_id():
    """A single id adds an IN filter naming that id."""
    base = select(Book)
    out = apply_category_filter(base, "18580628011")
    sql = _sql(out)
    assert "18580628011" in sql
    assert "IN" in sql.upper()
    # the filter actually changed the statement
    assert _sql(out) != _sql(base)


def test_multiple_categories_union_all_ids():
    """A comma-separated list adds an IN filter naming every id (a union)."""
    base = select(Book)
    out = apply_category_filter(base, "18580628011,18573212011")
    sql = _sql(out)
    assert "18580628011" in sql
    assert "18573212011" in sql


def test_whitespace_around_ids_is_stripped():
    """Surrounding whitespace on each id is ignored."""
    base = select(Book)
    out = apply_category_filter(base, " id1 , id2 ")
    sql = _sql(out)
    assert "'id1'" in sql
    assert "'id2'" in sql
    assert "' id1 '" not in sql


def test_empty_entries_are_dropped():
    """Empty entries between commas are ignored, leaving only real ids."""
    base = select(Book)
    out = apply_category_filter(base, "id1,,id2,")
    sql = _sql(out)
    assert "'id1'" in sql
    assert "'id2'" in sql


def test_none_leaves_statement_unchanged():
    """None applies no filter — the statement is returned as-is."""
    base = select(Book)
    out = apply_category_filter(base, None)
    assert _sql(out) == _sql(base)


def test_empty_string_leaves_statement_unchanged():
    """An empty string applies no filter."""
    base = select(Book)
    out = apply_category_filter(base, "")
    assert _sql(out) == _sql(base)


def test_only_commas_and_spaces_leaves_statement_unchanged():
    """A value with no real ids (just commas/whitespace) applies no filter."""
    base = select(Book)
    out = apply_category_filter(base, " , , ")
    assert _sql(out) == _sql(base)
