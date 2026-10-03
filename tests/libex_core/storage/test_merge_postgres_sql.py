"""
The Postgres SQL of every merge builder, and of every statement the writer
builds from them, is the SQL the hosted writer emitted before the writer moved
into the package, character for character. The golden strings here, and the
golden file beside the schema one, were captured from that writer; these tests
fail the moment a builder or a statement moves them.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import bindparam, cast, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, asyncpg, insert

from libex_core.storage import merge
from libex_core.storage.models import Book, Track
from libex_core.storage.types import JSONDocument
from libex_core.storage.write.statements import statements_for

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


GOLDEN_STATEMENTS = json.loads(
    (Path(__file__).resolve().parent.parent / "golden_postgres_statements.json").read_text()
)


@pytest.mark.parametrize("name", GOLDEN_STATEMENTS)
def test_every_writer_statement_compiles_to_the_hosted_postgres_sql(name):
    golden = GOLDEN_STATEMENTS[name]
    statement = getattr(statements_for("postgresql"), name)
    on_asyncpg = statement.compile(dialect=asyncpg.dialect())
    assert str(on_asyncpg) == golden["asyncpg"]
    assert list(on_asyncpg.positiontup) == golden["asyncpg_positions"]
    assert str(statement.compile(dialect=postgresql.dialect())) == golden["pyformat"]


def test_blank_set_is_exactly_unicode_white_space():
    codepoints = [
        *range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B),
        0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
    ]
    assert merge.BLANK_CHARS == "".join(chr(c) for c in codepoints)


@pytest.mark.parametrize("dialect", [postgresql.dialect, asyncpg.dialect], ids=["pyformat", "asyncpg"])
def test_json_bind_is_the_hosted_cast_bind(dialect):
    hosted_form = insert(Book).values(
        asin=bindparam("asin"), region=bindparam("region"),
        plans=cast(bindparam("plans", type_=JSONB(none_as_null=True)), JSONB),
    )
    core_form = insert(Book).values(
        asin=bindparam("asin"), region=bindparam("region"), plans=merge.json_bind("plans")
    )
    ours = core_form.compile(dialect=dialect())
    theirs = hosted_form.compile(dialect=dialect())
    assert str(ours) == str(theirs)
    assert ours.params == theirs.params


def test_json_bind_keeps_none_as_sql_null_on_postgres():
    compiled = insert(Book).values(
        asin=bindparam("asin"), region=bindparam("region"), plans=merge.json_bind("plans")
    ).compile(dialect=asyncpg.dialect())
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
