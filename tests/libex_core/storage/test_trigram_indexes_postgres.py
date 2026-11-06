"""
The trigram indexes behind the ILIKE filters, as the core chain builds them on
a real Postgres: the right index method and operator class under every name,
a round trip through up, down and up on a populated database, the refusal of a
role that cannot create the extension, and no effect at all on SQLite.
Everything here needs Docker, through the container fixture in conftest.
"""

# Standard library
import uuid

# Third party
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.models import CORE_TABLES
from libex_core.storage.store import LocalStore
from libex_core.storage.upgrade import VERSION_TABLE, _config

pytestmark = pytest.mark.integration

# The NOT NULL columns of books that have no database default.
NOT_NULL = "explicit, whisper_sync, has_pdf, is_listenable, is_buyable, is_vvab, created_at, updated_at"
NOT_NULL_VALUES = "false, false, false, true, true, false, now(), now()"
PRE = "f2b8d6a40c17"
# The last revision that builds trigram indexes; the folded-index revision after
# it needs pg_trgm in public, which this test deliberately avoids.
TRIGRAM_HEAD = "b5e9c3a7f184"
TRIGRAM_INDEXES = {
    "ix_books_title_trgm": "books",
    "ix_books_subtitle_trgm": "books",
    "ix_books_publisher_trgm": "books",
    "ix_books_isbn_trgm": "books",
    "ix_authors_name_trgm": "authors",
    "ix_series_title_trgm": "series",
    "ix_narrators_name_trgm": "narrators",
}


def is_trigram(indexdef: str) -> bool:
    return " USING gin " in indexdef and "gin_trgm_ops" in indexdef


async def _admin(base_url, sql, **params):
    engine = create_async_engine(base_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(sql), params)
            return result.all() if result.returns_rows else None
    finally:
        await engine.dispose()


async def _new_database(base_url) -> str:
    name = "t_" + uuid.uuid4().hex[:12]
    await _admin(base_url, f'CREATE DATABASE "{name}"')
    return make_url(base_url).set(database=name).render_as_string(hide_password=False)


async def _query(url, sql, **params):
    return await _admin(url, sql, **params)


async def _indexdefs(url) -> dict:
    rows = await _query(
        url, "SELECT indexname, indexdef FROM pg_indexes WHERE indexname LIKE 'ix\\_%\\_trgm'"
    )
    return dict(rows)


async def _upgrade_to(url, revision):
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda c: command.upgrade(_config(c), revision))
    finally:
        await engine.dispose()


async def _downgrade_to(url, revision):
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda c: command.downgrade(_config(c), revision))
    finally:
        await engine.dispose()


async def _upgrade_store(url):
    store = LocalStore(url)
    try:
        return await store.upgrade()
    finally:
        await store.close()


@pytest.fixture
async def url(postgres_url):
    return await _new_database(postgres_url)


async def test_every_trigram_index_is_gin_over_the_trigram_operator_class(url):
    await _upgrade_store(url)
    defs = await _indexdefs(url)
    assert set(defs) == set(TRIGRAM_INDEXES)
    for name, table in TRIGRAM_INDEXES.items():
        assert is_trigram(defs[name]), defs[name]
        assert f" ON public.{table} " in defs[name]


async def test_the_check_rejects_a_btree_and_a_gin_with_the_default_class(url):
    await _upgrade_store(url)
    await _query(url, "CREATE INDEX plain_btree ON books (title)")
    await _query(url, "CREATE INDEX plain_gin ON books USING gin (to_tsvector('simple', title))")
    defs = dict(await _query(url, "SELECT indexname, indexdef FROM pg_indexes WHERE indexname LIKE 'plain\\_%'"))
    assert not is_trigram(defs["plain_btree"])
    assert not is_trigram(defs["plain_gin"])


async def test_the_planner_uses_the_index_for_an_unescaped_ilike(url):
    await _upgrade_store(url)
    await _query(url, f"INSERT INTO books (asin, region, title, {NOT_NULL}) VALUES ('B0TRGM0001', 'us', 'The Hobbit', {NOT_NULL_VALUES})")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET enable_seqscan = off"))
            plan = "\n".join(
                row[0] for row in await connection.execute(
                    text("EXPLAIN SELECT asin FROM books WHERE title ILIKE '%hobb%'")
                )
            )
    finally:
        await engine.dispose()
    assert "ix_books_title_trgm" in plan


async def test_up_down_up_on_a_populated_database_keeps_the_rows_and_the_extension(url):
    await _upgrade_to(url, PRE)
    await _query(url, f"INSERT INTO books (asin, region, title, {NOT_NULL}) VALUES ('B0TRGM0002', 'us', 'Kept', {NOT_NULL_VALUES})")
    head = await _upgrade_store(url)
    assert set(await _indexdefs(url)) == set(TRIGRAM_INDEXES)

    await _downgrade_to(url, PRE)
    assert await _indexdefs(url) == {}
    assert await _query(url, "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'") == [(1,)]
    assert await _query(url, "SELECT title FROM books") == [("Kept",)]
    assert (await _query(url, f"SELECT version_num FROM {VERSION_TABLE}")) == [(PRE,)]

    assert await _upgrade_store(url) == head
    defs = await _indexdefs(url)
    assert set(defs) == set(TRIGRAM_INDEXES) and all(is_trigram(d) for d in defs.values())
    valid = await _query(url, "SELECT count(*) FROM pg_index WHERE indexrelid = ANY (SELECT oid FROM pg_class WHERE relname LIKE 'ix\\_%\\_trgm') AND indisvalid")
    assert valid == [(len(TRIGRAM_INDEXES),)]
    assert await _query(url, "SELECT title FROM books") == [("Kept",)]


async def test_an_existing_extension_in_another_schema_is_used_to_qualify_the_class(url):
    await _query(url, "CREATE SCHEMA ext")
    await _query(url, "CREATE EXTENSION pg_trgm WITH SCHEMA ext")
    await _upgrade_to(url, TRIGRAM_HEAD)
    defs = await _indexdefs(url)
    assert set(defs) == set(TRIGRAM_INDEXES)
    assert all("ext.gin_trgm_ops" in d for d in defs.values())
    assert await _query(url, "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE extname = 'pg_trgm'") == [("ext",)]


async def _role_database(postgres_url, grant_create: bool):
    """A database at the revision before the trigram one, whose tables belong to
    a plain role that has no extension rights unless `grant_create`."""
    url = await _new_database(postgres_url)
    role = "r_" + uuid.uuid4().hex[:10]
    await _upgrade_to(url, PRE)
    await _query(url, f"CREATE ROLE {role} LOGIN PASSWORD 'pw'")
    for table in sorted(CORE_TABLES) + [VERSION_TABLE]:
        await _query(url, f'ALTER TABLE "{table}" OWNER TO {role}')
    await _query(url, f"GRANT ALL ON SCHEMA public TO {role}")
    await _query(url, "REVOKE CREATE ON DATABASE " + make_url(url).database + " FROM PUBLIC")
    if grant_create:
        await _query(url, f"GRANT CREATE ON DATABASE {make_url(url).database} TO {role}")
    role_url = make_url(url).set(username=role, password="pw").render_as_string(hide_password=False)
    return url, role_url


async def test_a_role_that_cannot_create_the_extension_is_refused_and_nothing_is_stamped(postgres_url):
    url, role_url = await _role_database(postgres_url, grant_create=False)
    store = LocalStore(role_url)
    try:
        with pytest.raises(Exception) as raised:
            await store.upgrade()
    finally:
        await store.close()
    message = str(raised.value)
    # The migration's own message, not Postgres's permission error echoing the statement.
    assert "a privileged role must run: CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public" in message
    assert await _query(url, f"SELECT version_num FROM {VERSION_TABLE}") == [(PRE,)]
    assert await _indexdefs(url) == {}
    assert await _query(url, "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'") == []


async def test_the_same_role_with_the_privilege_creates_the_extension_and_the_indexes(postgres_url):
    url, role_url = await _role_database(postgres_url, grant_create=True)
    await _upgrade_store(role_url)
    assert set(await _indexdefs(url)) == set(TRIGRAM_INDEXES)
    assert await _query(url, "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'") == [(1,)]
