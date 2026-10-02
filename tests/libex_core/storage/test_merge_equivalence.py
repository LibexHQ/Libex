"""
Each merge builder, run against real Postgres and against SQLite, must leave
the same row. The table of cases is in _merge_cases; every row also states the
expected result, so two backends agreeing on a wrong answer still fail.

Both backends run the builders inside an UPDATE and inside the INSERT ... ON
CONFLICT DO UPDATE the writer uses, since the second is where excluded is
read and a registered function must work inside SET.
"""

import asyncio
import json
import random
import unicodedata
from decimal import Decimal

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    bindparam,
    cast,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from libex_core.storage import merge
from libex_core.storage.dialect import (
    DialectVariant,
    configure_sqlite,
    sqlite_json_contains,
    sqlite_json_merge,
    sqlite_lower,
)
from libex_core.storage.types import JSONDocument
from tests.libex_core.storage import _merge_cases as cases

pytestmark = pytest.mark.integration

docker = pytest.importorskip("docker")
postgres_container = pytest.importorskip("testcontainers.postgres")


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


if not _docker_available():
    pytest.skip("Docker daemon not available", allow_module_level=True)

METADATA = MetaData()
TEXT_TABLE = Table("eq_text", METADATA, Column("id", Integer, primary_key=True), Column("val", Text))
EXTRAS_TABLE = Table("eq_extras", METADATA, Column("id", Integer, primary_key=True), Column("val", JSONDocument))
CHAPTER_TABLE = Table(
    "eq_chapters", METADATA, Column("id", Integer, primary_key=True), Column("val", JSONDocument, nullable=False)
)
NAMES_TABLE = Table("eq_names", METADATA, Column("n", String))


@pytest.fixture(scope="module")
def postgres_url():
    container = postgres_container.PostgresContainer("postgres:16")
    container.start()
    try:
        url = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql+asyncpg://")

        async def prepare():
            engine = create_async_engine(url, poolclass=NullPool)
            async with engine.begin() as connection:
                await connection.run_sync(METADATA.create_all)
            await engine.dispose()

        asyncio.run(prepare())
        yield url
    finally:
        container.stop()


@pytest.fixture
async def backends(postgres_url):
    pg = create_async_engine(postgres_url, poolclass=NullPool)
    lite = create_async_engine("sqlite+aiosqlite://")
    configure_sqlite(lite)
    async with lite.begin() as connection:
        await connection.run_sync(METADATA.create_all)
    yield {"postgresql": pg, "sqlite": lite}
    await pg.dispose()
    await lite.dispose()


def raw_json(name):
    """A JSON value bound as text: cast to jsonb on Postgres, bare on SQLite."""
    return DialectVariant(cast(bindparam(name, type_=Text), JSONB), bindparam(name, type_=Text))


def _encode(value):
    if value is cases.NULL:
        return None
    if value is cases.JNULL:
        return "null"
    return json.dumps(value)


def canon(value):
    """A comparable form that keeps true, 1 and "1" apart and 1 equal to 1.0."""
    if isinstance(value, bool):
        return ("bool", value)
    if value is None:
        return ("null",)
    if isinstance(value, (int, float, Decimal)):
        return ("num", Decimal(str(value)).normalize())
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, list):
        return ("arr", [canon(v) for v in value])
    return ("obj", sorted((k, canon(v)) for k, v in value.items()))


def _insert_for(name, table):
    return (postgresql if name == "postgresql" else sqlite).insert(table)


async def _merge(engine, table, builder, stored, incoming, mode, encode, new_expression):
    """Applies the builder to one stored row on one backend; returns the row."""
    name = engine.dialect.name
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                insert(table).values(id=1, val=new_expression("stored")), {"stored": encode(stored)}
            )
            if mode == "update":
                await connection.execute(
                    update(table).where(table.c.id == 1).values(val=builder(new_expression("incoming"), table.c.val)),
                    {"incoming": encode(incoming)},
                )
            else:
                statement = _insert_for(name, table).values(id=1, val=new_expression("incoming"))
                await connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["id"],
                        set_={"val": builder(statement.excluded.val, table.c.val)},
                    ),
                    {"incoming": encode(incoming)},
                )
            if table is TEXT_TABLE:
                return (await connection.execute(select(table.c.val))).scalar()
            row = (await connection.execute(select(table.c.val.is_(None), cast(table.c.val, Text)))).one()
            return None if row[0] else json.loads(row[1], parse_float=Decimal)
        finally:
            await transaction.rollback()


def _text_new(name):
    return bindparam(name, type_=Text)


def _expected_json(expected):
    return None if expected is cases.NULL else expected


async def _run_both(backends, table, builder, row, mode, encode, new_expression):
    _, stored, incoming, expected = row
    results = {
        name: await _merge(engine, table, builder, stored, incoming, mode, encode, new_expression)
        for name, engine in backends.items()
    }
    return results, expected


def _ids(rows):
    return [row[0] for row in rows]


@pytest.mark.parametrize("mode", ["update", "upsert"])
@pytest.mark.parametrize("row", cases.COALESCE, ids=_ids(cases.COALESCE))
async def test_coalesce_agrees(backends, mode, row):
    results, expected = await _run_both(backends, TEXT_TABLE, merge.coalesce, row, mode, lambda v: v, _text_new)
    assert results["postgresql"] == results["sqlite"] == expected


@pytest.mark.parametrize("mode", ["update", "upsert"])
@pytest.mark.parametrize("row", cases.ANSWERED, ids=_ids(cases.ANSWERED))
async def test_answered_agrees(backends, mode, row):
    results, expected = await _run_both(backends, TEXT_TABLE, merge.answered, row, mode, lambda v: v, _text_new)
    assert results["postgresql"] == results["sqlite"] == expected


@pytest.mark.parametrize("mode", ["update", "upsert"])
@pytest.mark.parametrize("row", cases.LONGER_WINS, ids=_ids(cases.LONGER_WINS))
async def test_longer_wins_agrees(backends, mode, row):
    results, expected = await _run_both(backends, TEXT_TABLE, merge.longer_wins, row, mode, lambda v: v, _text_new)
    assert results["postgresql"] == results["sqlite"] == expected


@pytest.mark.parametrize("mode", ["update", "upsert"])
@pytest.mark.parametrize("row", cases.EXTRAS, ids=_ids(cases.EXTRAS))
async def test_extras_union_agrees(backends, mode, row):
    results, expected = await _run_both(backends, EXTRAS_TABLE, merge.extras_union, row, mode, _encode, raw_json)
    assert canon(results["postgresql"]) == canon(results["sqlite"]) == canon(_expected_json(expected))


# Only the upsert form: the payload is subscripted, and Postgres does not
# subscript a cast expression, so the incoming side must be a column, which in
# the writer it is (excluded.chapters).
@pytest.mark.parametrize("row", cases.CHAPTERED, ids=_ids(cases.CHAPTERED))
async def test_chaptered_wins_agrees(backends, row):
    results, expected = await _run_both(backends, CHAPTER_TABLE, merge.chaptered_wins, row, "upsert", _encode, raw_json)
    expected = None if expected is cases.JNULL else expected
    assert canon(results["postgresql"]) == canon(results["sqlite"]) == canon(expected)


async def test_chapter_count_never_raises_on_either_backend(backends):
    payloads = [{"chapters": [1, 2]}, {"chapters": "x"}, {"chapters": None}, {}, "null", "[1]", "3", '"x"']
    for name, engine in backends.items():
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for index, payload in enumerate(payloads):
                    payload = payload if isinstance(payload, str) else json.dumps(payload)
                    await connection.execute(
                        insert(CHAPTER_TABLE).values(id=index, val=raw_json("p")), {"p": payload}
                    )
                count = merge.chapter_count(CHAPTER_TABLE.c.val)
                listed = (await connection.execute(select(count).order_by(CHAPTER_TABLE.c.id))).scalars().all()
                # The WHERE position is where a planner may reorder quals, and
                # where a bare jsonb_array_length would raise on a non-array.
                kept = (await connection.execute(select(CHAPTER_TABLE.c.id).where(count > 0))).scalars().all()
            finally:
                await transaction.rollback()
        assert listed == [2, 0, 0, 0, 0, 0, 0, 0], name
        assert kept == [0], name


async def test_json_bind_means_the_same_on_both_backends(backends):
    table = Table("eq_bind", MetaData(), Column("id", Integer, primary_key=True), Column("val", JSONDocument))
    for name, engine in backends.items():
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.run_sync(table.create)
                await connection.execute(
                    insert(table).values(id=1, val=merge.json_bind("v")), {"v": {"a": [1, "é"]}}
                )
                await connection.execute(insert(table).values(id=2, val=merge.json_bind("v")), {"v": None})
                rows = (await connection.execute(select(table.c.id, table.c.val.is_(None), cast(table.c.val, Text)).order_by(table.c.id))).all()
            finally:
                await transaction.rollback()
        assert rows[0][1] is False and json.loads(rows[0][2]) == {"a": [1, "é"]}, name
        assert rows[1][1] is True, name


async def test_trim_agrees_on_every_blank_and_every_neighbour(backends):
    candidates = list(merge.BLANK_CHARS) + ["​", "﻿", "\u001c", "᠎", "⁠", "‌", "‍", "­"]
    results = {}
    for name, engine in backends.items():
        function = "btrim" if name == "postgresql" else "trim"
        async with engine.connect() as connection:
            results[name] = [
                (await connection.execute(text(f"select {function}(:v, :b)"), {"v": f"{c}x{c}", "b": merge.BLANK_CHARS})).scalar()
                for c in candidates
            ]
    assert results["postgresql"] == results["sqlite"]
    assert results["postgresql"][: len(merge.BLANK_CHARS)] == ["x"] * len(merge.BLANK_CHARS)


async def test_ilike_agrees_on_non_ascii_case_pairs(backends):
    results = {}
    for name, engine in backends.items():
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(insert(NAMES_TABLE), [{"n": n} for n in cases.NAMES])
                found = []
                for pattern, _ in cases.ILIKE:
                    rows = await connection.execute(select(NAMES_TABLE.c.n).where(NAMES_TABLE.c.n.ilike(pattern)))
                    found.append(sorted(rows.scalars().all()))
            finally:
                await transaction.rollback()
        results[name] = found
    assert results["postgresql"] == results["sqlite"]
    assert results["sqlite"] == [sorted(expected) for _, expected in cases.ILIKE]


async def test_lower_matches_postgres_for_every_code_point_python_assigns(backends):
    """Exhaustive over Unicode. A code point Python's tables do not yet assign
    is skipped: a newer Postgres or libc may lower-case a script added since,
    and no function Python can supply knows it."""
    async with backends["postgresql"].connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "select n, lower(chr(n)) from generate_series(1, 1114111) n "
                    "where n not between 55296 and 57343 and lower(chr(n)) <> chr(n)"
                )
            )
        ).all()
    postgres = dict(rows)
    mine = {
        n: sqlite_lower(chr(n))
        for n in range(1, 0x110000)
        if not 0xD800 <= n <= 0xDFFF and sqlite_lower(chr(n)) != chr(n)
    }
    assigned = {n: v for n, v in postgres.items() if unicodedata.category(chr(n)) != "Cn"}
    assert mine == assigned
    assert len(mine) > 1400


async def test_lower_differs_from_str_lower_exactly_where_postgres_does(backends):
    probes = ["İ", "ΑΣ", "ΑΣ ΑΣ", "ß", "ẞ", "Ǆ", "ǅ", "I"]
    async with backends["postgresql"].connect() as connection:
        postgres = [(await connection.execute(text("select lower(:v)"), {"v": p})).scalar() for p in probes]
    assert [sqlite_lower(p) for p in probes] == postgres
    differing = [p for p in probes if p.lower() != sqlite_lower(p)]
    assert differing == ["İ", "ΑΣ", "ΑΣ ΑΣ"]


def _random_json(rng, depth=0):
    roll = rng.random()
    if depth >= 3 or roll < 0.4:
        return rng.choice([None, True, False, 0, 1, 2, 1.0, 1.5, "a", "b", "1", "", "é"])
    if roll < 0.7:
        return {k: _random_json(rng, depth + 1) for k in rng.sample(["a", "b", "c", "d"], rng.randint(0, 3))}
    return [_random_json(rng, depth + 1) for _ in range(rng.randint(0, 3))]


async def test_containment_and_merge_agree_with_jsonb_on_random_documents(backends):
    rng = random.Random(20261001)
    pairs = [(json.dumps(_random_json(rng)), json.dumps(_random_json(rng))) for _ in range(2500)]
    lefts, rights = [p[0] for p in pairs], [p[1] for p in pairs]
    async with backends["postgresql"].connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "select o, a::jsonb @> b::jsonb, (a::jsonb || b::jsonb)::text "
                    "from unnest(cast(:a as text[]), cast(:b as text[])) with ordinality as t(a, b, o) order by o"
                ),
                {"a": lefts, "b": rights},
            )
        ).all()
    async with backends["sqlite"].connect() as connection:
        for (left, right), (_, contained, merged) in zip(pairs, rows, strict=True):
            sql = (
                await connection.execute(
                    text("select libex_json_contains(:a, :b), libex_json_merge(:a, :b)"), {"a": left, "b": right}
                )
            ).one()
            assert sqlite_json_contains(left, right) == int(contained) == sql[0], (left, right)
            expected = canon(json.loads(merged, parse_float=Decimal))
            assert canon(json.loads(sql[1], parse_float=Decimal)) == expected, (left, right)
            assert canon(json.loads(sqlite_json_merge(left, right), parse_float=Decimal)) == expected
