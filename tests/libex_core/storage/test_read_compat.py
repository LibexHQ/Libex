"""
The backend-specific constructs compile to exactly the SQL the hosted app has
always sent Postgres, and to something SQLite can run.
"""

# Third party
import pytest
from sqlalchemy import Float, case, cast, select
from sqlalchemy.dialects import postgresql, sqlite

# Local
from libex_core.storage.filtering import apply_book_filters, apply_narrator_filters
from libex_core.storage.models import Book, Narrator, book_series
from libex_core.storage.read._compat import (
    AscNullsLast,
    ILike,
    JsonHasKey,
    JsonListContains,
    NumericPosition,
    dialect_name,
)

PG = postgresql.dialect()
LITE = sqlite.dialect()


def _same_on_postgres(wrapped, original):
    a, b = wrapped.compile(dialect=PG), original.compile(dialect=PG)
    assert str(a) == str(b)
    assert a.params == b.params


def test_ilike_is_the_original_expression_on_postgres():
    _same_on_postgres(ILike(Book.title, "%x%"), Book.title.ilike("%x%"))


def test_list_contains_is_the_original_expression_on_postgres():
    _same_on_postgres(JsonListContains(Book.plans, "Plus"), Book.plans.contains(["Plus"]))


def test_has_key_is_the_original_expression_on_postgres():
    _same_on_postgres(JsonHasKey(Narrator.languages, "English"), Narrator.languages.has_key("English"))


def test_numeric_position_is_the_original_expression_on_postgres():
    position = book_series.c.position
    original = case((position.op("~")(r"^\d+(\.\d+)?$"), cast(position, Float)), else_=None)
    _same_on_postgres(NumericPosition(position), original)


def test_ascending_order_is_left_bare_on_postgres():
    _same_on_postgres(AscNullsLast(book_series.c.position), book_series.c.position.asc())


def test_filters_issue_the_same_postgres_sql_as_the_plain_expressions():
    stmt = apply_book_filters(select(Book), title="a", publisher="b", plan_name="Plus", series_name="s")
    sql = str(stmt.compile(dialect=PG))
    assert "books.title ILIKE" in sql
    assert "books.publisher ILIKE" in sql
    assert "books.plans @>" in sql
    narrators = str(apply_narrator_filters(select(Narrator), gender="g", language="en").compile(dialect=PG))
    assert "narrators.gender ILIKE" in narrators
    assert "narrators.languages ?" in narrators


@pytest.mark.parametrize(
    "build",
    [
        lambda: apply_book_filters(select(Book), title="a", plan_name="Plus", genre="g", category="1,2"),
        lambda: apply_narrator_filters(select(Narrator), gender="g", language="en", source="s"),
        lambda: select(Book).order_by(NumericPosition(book_series.c.position).asc().nulls_last(),
                                      AscNullsLast(book_series.c.position)),
    ],
)
def test_sqlite_gets_no_postgres_only_syntax(build):
    sql = str(build().compile(dialect=LITE))
    for postgres_only in ("ILIKE", "@>", "::JSONB", " ? ", " ~ "):
        assert postgres_only not in sql


def test_sqlite_lowercases_both_sides_and_states_the_escape_character():
    sql = str(select(Book).where(ILike(Book.title, "%x%")).compile(dialect=LITE))
    assert "lower(books.title) LIKE lower(?) ESCAPE '\\'" in sql


def test_a_statement_is_not_cached_across_dialects():
    stmt = select(Book).where(ILike(Book.title, "%x%"))
    assert "ILIKE" in str(stmt.compile(dialect=PG))
    assert "ILIKE" not in str(stmt.compile(dialect=LITE))
    assert "ILIKE" in str(stmt.compile(dialect=PG))


def test_dialect_name_reads_the_bind_and_tolerates_a_session_without_one():
    class Bound:
        class bind:  # noqa: N801 - stand-in for an engine
            dialect = LITE

    assert dialect_name(Bound()) == "sqlite"
    assert dialect_name(object()) == "unknown"
