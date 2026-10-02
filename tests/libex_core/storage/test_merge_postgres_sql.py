"""
The Postgres SQL of every merge builder is the SQL the hosted writer emits,
character for character. The hosted statements are the contract; these tests
fail the moment a builder moves them.
"""

import os

os.environ["AXIOM_TOKEN"] = ""
os.environ["AXIOM_DATASET"] = ""

import pytest
from sqlalchemy import bindparam, cast, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, asyncpg, insert

import app.services.db.writer as hosted
from libex_core.storage import merge
from libex_core.storage.models import Book, Track
from libex_core.storage.types import JSONDocument

TEXT = bindparam("v")
DOC = bindparam("v", type_=JSONDocument)

GOLDEN = {
    "coalesce": (
        merge.coalesce, TEXT, Book.title,
        "coalesce(%(v)s, books.title)",
    ),
    "answered": (
        merge.answered, TEXT, Book.title,
        "CASE WHEN (btrim(%(v)s, %(btrim_1)s) != %(btrim_2)s) THEN %(v)s ELSE books.title END",
    ),
    "longer_wins": (
        merge.longer_wins, TEXT, Book.description,
        "CASE WHEN (coalesce(length(nullif(btrim(%(v)s, %(btrim_1)s), %(nullif_1)s)), "
        "%(coalesce_1)s) > coalesce(length(books.description), %(coalesce_2)s)) "
        "THEN %(v)s ELSE books.description END",
    ),
    "chaptered_wins": (
        merge.chaptered_wins, DOC, Track.chapters,
        "CASE WHEN (jsonb_array_length(CASE WHEN (jsonb_typeof(%(v)s::JSONB[%(param_1)s]) = "
        "%(jsonb_typeof_1)s) THEN %(v)s::JSONB[%(param_1)s] ELSE jsonb_build_array() END) > "
        "%(jsonb_array_length_1)s) THEN %(v)s::JSONB WHEN (jsonb_array_length(CASE WHEN "
        "(jsonb_typeof(tracks.chapters[%(chapters_1)s]) = %(jsonb_typeof_2)s) THEN "
        "tracks.chapters[%(chapters_1)s] ELSE jsonb_build_array() END) > "
        "%(jsonb_array_length_2)s) THEN tracks.chapters ELSE %(v)s::JSONB END",
    ),
    "extras_union": (
        merge.extras_union, DOC, Book.audible_extras,
        "CASE WHEN (%(v)s::JSONB IS NULL) THEN books.audible_extras WHEN "
        "(books.audible_extras IS NULL) THEN %(v)s::JSONB WHEN (books.audible_extras @> "
        "%(v)s::JSONB) THEN books.audible_extras ELSE books.audible_extras || %(v)s::JSONB END",
    ),
}


def _compiled(expression, dialect):
    return expression.compile(dialect=dialect)


@pytest.mark.parametrize("name", GOLDEN)
def test_builder_compiles_to_the_golden_postgres_sql(name):
    builder, new_value, column, golden = GOLDEN[name]
    assert str(_compiled(builder(new_value, column), postgresql.dialect())) == golden


@pytest.mark.parametrize("name", GOLDEN)
@pytest.mark.parametrize("dialect", [postgresql.dialect, asyncpg.dialect], ids=["pyformat", "asyncpg"])
def test_builder_matches_the_hosted_builder(name, dialect):
    builder, new_value, column, _ = GOLDEN[name]
    hosted_builder = getattr(hosted, f"_{name}")
    ours = _compiled(builder(new_value, column), dialect())
    theirs = _compiled(hosted_builder(new_value, column), dialect())
    assert str(ours) == str(theirs)
    assert ours.params == theirs.params


@pytest.mark.parametrize(
    "build", ["_build_book_upsert", "_build_series_upsert", "_build_book_series_upsert"]
)
def test_whole_hosted_statements_are_unchanged_when_built_from_the_core_builders(monkeypatch, build):
    dialect = asyncpg.dialect()
    before = getattr(hosted, build)().compile(dialect=dialect)
    for name in GOLDEN:
        monkeypatch.setattr(hosted, f"_{name}", GOLDEN[name][0])
    after = getattr(hosted, build)().compile(dialect=dialect)
    assert str(after) == str(before)
    assert after.params == before.params
    assert after.positiontup == before.positiontup


def test_blank_set_is_the_hosted_one():
    assert merge.BLANK_CHARS == hosted._BLANK_CHARS


@pytest.mark.parametrize("dialect", [postgresql.dialect, asyncpg.dialect], ids=["pyformat", "asyncpg"])
def test_json_bind_is_the_hosted_cast_bind(dialect):
    hosted_form = insert(Book).values(
        asin=bindparam("asin"), plans=cast(bindparam("plans", type_=JSONB(none_as_null=True)), JSONB)
    )
    core_form = insert(Book).values(asin=bindparam("asin"), plans=merge.json_bind("plans"))
    ours = core_form.compile(dialect=dialect())
    theirs = hosted_form.compile(dialect=dialect())
    assert str(ours) == str(theirs)
    assert ours.params == theirs.params


def test_json_bind_keeps_none_as_sql_null_on_postgres():
    compiled = insert(Book).values(asin=bindparam("asin"), plans=merge.json_bind("plans")).compile(dialect=asyncpg.dialect())
    bind_type = compiled.binds["plans"].type
    assert bind_type.dialect_impl(asyncpg.dialect()).none_as_null is True


def test_no_sqlite_spelling_reaches_a_postgres_statement():
    stmt = update(Book).values(
        description=merge.longer_wins(bindparam("d"), Book.description),
        audible_extras=merge.extras_union(bindparam("e", type_=JSONDocument), Book.audible_extras),
    )
    text = str(stmt.compile(dialect=postgresql.dialect()))
    assert "libex_" not in text
    assert "json_type" not in text and "json_array_length(" not in text.replace("jsonb_array_length(", "")
    assert " trim(" not in text.replace("btrim(", "")
