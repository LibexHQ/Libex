"""
SQLite connection setup: what configure_sqlite does to every connection, and
the functions it registers, proven through the real aiosqlite adapter.
"""

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, insert, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from libex_core.storage import dialect
from libex_core.storage.dialect import (
    WRITE_OPTION,
    SQLiteTooOld,
    configure_sqlite,
    require_sqlite_version,
    sqlite_json_contains,
    sqlite_json_merge,
    sqlite_lower,
)
from libex_core.storage.merge import BLANK_CHARS, json_bind

MEMORY = "sqlite+aiosqlite://"


@pytest.fixture
async def memory_engine():
    engine = create_async_engine(MEMORY)
    configure_sqlite(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
async def file_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    configure_sqlite(engine, busy_timeout_ms=50)
    yield engine
    await engine.dispose()


async def scalar(engine, sql, **params):
    async with engine.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar()


# ---------------------------------------------------------------- version floor

def test_a_sqlite_older_than_3_35_is_refused_naming_both_versions():
    with pytest.raises(SQLiteTooOld) as caught:
        require_sqlite_version("3.34.1")
    assert "3.35.0" in str(caught.value) and "3.34.1" in str(caught.value)


@pytest.mark.parametrize("version", ["3.35.0", "3.35.5", "3.45.1", "4.0.0"])
def test_the_floor_and_anything_newer_is_accepted(version):
    require_sqlite_version(version)


def test_configure_refuses_to_run_when_the_linked_sqlite_is_too_old(monkeypatch):
    monkeypatch.setattr(dialect.sqlite3, "sqlite_version", "3.30.0")
    with pytest.raises(SQLiteTooOld, match="3.30.0"):
        configure_sqlite(create_async_engine(MEMORY))


def test_configure_refuses_an_engine_that_is_not_sqlite():
    with pytest.raises(ValueError, match="SQLite"):
        configure_sqlite(create_async_engine("postgresql+asyncpg://user@localhost/db"))


# ---------------------------------------------------------------- pragmas

async def test_in_memory_connection_pragmas(memory_engine):
    assert await scalar(memory_engine, "PRAGMA foreign_keys") == 1
    assert await scalar(memory_engine, "PRAGMA busy_timeout") == dialect.BUSY_TIMEOUT_MS
    assert await scalar(memory_engine, "PRAGMA synchronous") == 1
    assert await scalar(memory_engine, "PRAGMA journal_mode") == "memory"


async def test_file_connection_runs_in_wal_with_the_given_busy_timeout(file_engine):
    assert await scalar(file_engine, "PRAGMA journal_mode") == "wal"
    assert await scalar(file_engine, "PRAGMA busy_timeout") == 50
    assert await scalar(file_engine, "PRAGMA foreign_keys") == 1


async def test_wal_can_be_declined_for_a_file(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'plain.db'}")
    configure_sqlite(engine, wal=False)
    try:
        assert await scalar(engine, "PRAGMA journal_mode") == "delete"
    finally:
        await engine.dispose()


async def test_configuring_twice_does_not_stack_a_second_begin(memory_engine):
    configure_sqlite(memory_engine)
    async with memory_engine.begin() as connection:
        await connection.execute(text("create table t (a)"))
    assert await scalar(memory_engine, "select count(*) from t") == 0


async def test_foreign_keys_are_enforced(memory_engine):
    metadata = MetaData()
    parent = Table("parent", metadata, Column("id", Integer, primary_key=True))
    child = Table("child", metadata, Column("parent_id", Integer, ForeignKey("parent.id")))
    async with memory_engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(insert(parent).values(id=1))
        await connection.execute(insert(child).values(parent_id=1))
    with pytest.raises(IntegrityError):
        async with memory_engine.begin() as connection:
            await connection.execute(insert(child).values(parent_id=99))


# ---------------------------------------------------------------- transactions

async def _release_then_roll_back_outer(engine):
    async with engine.connect() as connection:
        await connection.execute(text("create table t (a)"))
        await connection.commit()
    async with engine.connect() as connection:
        savepoint = await connection.begin_nested()
        await connection.execute(text("insert into t values (2)"))
        await savepoint.commit()
        await connection.rollback()
    async with engine.connect() as connection:
        return [row[0] for row in await connection.execute(text("select a from t"))]


async def test_begin_nested_honours_the_outer_rollback(memory_engine):
    assert await _release_then_roll_back_outer(memory_engine) == []


async def test_an_unconfigured_engine_commits_a_released_savepoint_early():
    # The control: without the engine-emitted BEGIN, RELEASE of the first
    # savepoint is the outermost transaction and commits it.
    engine = create_async_engine(MEMORY)
    try:
        assert await _release_then_roll_back_outer(engine) == [2]
    finally:
        await engine.dispose()


async def test_a_rolled_back_savepoint_undoes_only_its_own_work(memory_engine):
    async with memory_engine.connect() as connection:
        await connection.execute(text("create table t (a)"))
        await connection.execute(text("insert into t values (1)"))
        savepoint = await connection.begin_nested()
        await connection.execute(text("insert into t values (2)"))
        await savepoint.rollback()
        await connection.commit()
    assert await scalar(memory_engine, "select group_concat(a) from t") == "1"


async def test_a_write_transaction_takes_the_write_lock_up_front(file_engine, tmp_path):
    async with file_engine.begin() as connection:
        await connection.execute(text("create table t (a)"))
    other = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    configure_sqlite(other, busy_timeout_ms=50)
    try:
        holder = file_engine.execution_options(**{WRITE_OPTION: True})
        contender = other.execution_options(**{WRITE_OPTION: True})
        async with holder.connect() as held:
            await held.begin()
            with pytest.raises(OperationalError, match="locked"):
                async with contender.connect() as blocked:
                    await blocked.begin()
            # A plain read neither waits for the writer nor sees its work.
            async with other.connect() as reader:
                assert (await reader.execute(text("select count(*) from t"))).scalar() == 0
            await held.rollback()
        async with contender.begin() as connection:
            await connection.execute(text("insert into t values (1)"))
    finally:
        await other.dispose()


async def test_a_plain_transaction_begins_deferred(file_engine, tmp_path):
    other = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    configure_sqlite(other, busy_timeout_ms=50)
    try:
        holder = file_engine.execution_options(**{WRITE_OPTION: True})
        async with holder.connect() as held:
            await held.begin()
            async with other.connect() as other_connection:
                await other_connection.begin()
            await held.rollback()
    finally:
        await other.dispose()


# ---------------------------------------------------------------- functions

async def test_registered_functions_are_deterministic(memory_engine):
    # SQLite refuses a non-deterministic function in an index expression.
    async with memory_engine.begin() as connection:
        await connection.execute(text("create table t (a text, j text)"))
        await connection.execute(text("create index ix_lower on t (lower(a))"))
        await connection.execute(text("create index ix_contains on t (libex_json_contains(j, j))"))
        await connection.execute(text("create index ix_merge on t (libex_json_merge(j, j))"))


@pytest.mark.parametrize(
    ("value", "lowered"),
    [
        ("Müller", "müller"),
        ("MÜLLER", "müller"),
        ("ÉLAN", "élan"),
        ("ÑANDÚ", "ñandú"),
        ("SÃO", "são"),
        ("Œuvre", "œuvre"),
        ("ΣΊΣΥΦΟΣ", "σίσυφοσ"),
        ("ß", "ß"),
        ("ẞ", "ß"),
        ("İ", "i"),
        ("I", "i"),
        ("ＡＢ", "ａｂ"),
        ("ДОМ", "дом"),
        ("日本語", "日本語"),
        ("", ""),
    ],
)
async def test_lower_is_the_postgres_one_character_simple_lowercase(memory_engine, value, lowered):
    assert sqlite_lower(value) == lowered
    assert await scalar(memory_engine, "select lower(:v)", v=value) == lowered


def test_lower_passes_null_through():
    assert sqlite_lower(None) is None


async def test_ilike_folds_beyond_ascii(memory_engine):
    metadata = MetaData()
    names = Table("names", metadata, Column("n", String))
    async with memory_engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(insert(names), [{"n": "Müller"}, {"n": "ÉLAN"}, {"n": "straße"}])
        found = (await connection.execute(select(names.c.n).where(names.c.n.ilike("%müller%")))).scalars().all()
        assert found == ["Müller"]
        found = (await connection.execute(select(names.c.n).where(names.c.n.ilike("%élan%")))).scalars().all()
        assert found == ["ÉLAN"]
        # Not casefold: the sharp s does not match ss, as on Postgres.
        found = (await connection.execute(select(names.c.n).where(names.c.n.ilike("%strasse%")))).scalars().all()
        assert found == []


@pytest.mark.parametrize("char", list(BLANK_CHARS), ids=lambda c: f"U+{ord(c):04X}")
async def test_trim_removes_every_blank_character(memory_engine, char):
    value = f"{char}x{char}"
    assert await scalar(memory_engine, "select trim(:v, :blank)", v=value, blank=BLANK_CHARS) == "x"
    assert await scalar(memory_engine, "select trim(:v, :blank)", v=char * 3, blank=BLANK_CHARS) == ""


@pytest.mark.parametrize("char", ["​", "﻿", "\u001c", "᠎", "⁠"], ids=lambda c: f"U+{ord(c):04X}")
async def test_trim_leaves_zero_width_and_control_characters(memory_engine, char):
    assert await scalar(memory_engine, "select trim(:v, :blank)", v=char, blank=BLANK_CHARS) == char


def test_blank_set_is_exactly_unicode_white_space():
    import sys

    expected = "".join(chr(c) for c in range(sys.maxunicode + 1) if chr(c).isspace() and c not in range(0x1C, 0x20))
    assert sorted(BLANK_CHARS) == sorted(expected)


# ---------------------------------------------------------------- json bind

async def test_json_bind_stores_the_document_and_none_as_sql_null(memory_engine):
    async with memory_engine.begin() as connection:
        await connection.execute(text("create table t (id integer primary key, j json)"))
        table = Table("t", MetaData(), Column("id", Integer, primary_key=True), Column("j", String))
        await connection.execute(
            table.insert().values(id=1, j=json_bind("doc")), {"doc": {"a": [1, 2]}}
        )
        await connection.execute(table.insert().values(id=2, j=json_bind("doc")), {"doc": None})
        rows = (await connection.execute(text("select id, j, j is null from t order by id"))).all()
    assert rows[0] == (1, '{"a": [1, 2]}', 0)
    assert rows[1] == (2, None, 1)


async def test_cast_to_json_is_what_json_bind_avoids(memory_engine):
    # SQLite reads the leading number of the text and returns it: the column
    # would be overwritten with 0.
    assert await scalar(memory_engine, "select cast(:v as json)", v='{"a": 1}') == 0


# ---------------------------------------------------------------- json functions

def test_containment_keeps_nested_and_array_subsets():
    assert sqlite_json_contains('{"a":{"x":1,"y":2}}', '{"a":{"x":1}}') == 1
    assert sqlite_json_contains('{"a":[1,2,3]}', '{"a":[3,1]}') == 1
    assert sqlite_json_contains('{"a":{"x":1}}', '{"a":{"x":1,"y":2}}') == 0
    assert sqlite_json_contains('{"a":true}', '{"a":1}') == 0
    assert sqlite_json_contains('{"a":1}', '{"a":1.0}') == 1
    assert sqlite_json_contains(None, "{}") is None


def test_merge_is_shallow_and_the_incoming_value_wins_a_clash():
    assert sqlite_json_merge('{"a":{"x":1,"y":2},"b":1}', '{"a":{"x":9}}') == '{"a":{"x":9},"b":1}'
    assert sqlite_json_merge('{"a":1}', '{"b":2}') == '{"a":1,"b":2}'
    assert sqlite_json_merge(None, "{}") is None


def test_merge_keeps_number_digits_it_was_given():
    merged = sqlite_json_merge('{"n":12345678901234567890123}', '{"f":0.1000000000000000055511151231257827}')
    assert merged == '{"n":12345678901234567890123,"f":0.1000000000000000055511151231257827}'


@pytest.mark.parametrize("bad", ['{"a": NaN}', '{"a": Infinity}', "not json"])
def test_functions_reject_what_jsonb_would_reject(bad):
    with pytest.raises(ValueError):
        sqlite_json_contains(bad, "{}")
