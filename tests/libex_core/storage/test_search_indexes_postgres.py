"""
The folded search indexes as the core chain builds them on a real Postgres: the
function and the nine indexes have the pinned definitions and the models build
the same ones, an up, down and up on a populated database keeps the rows and
both extensions, a database that already has the objects is left alone, and a
function or index that differs from the expected one is refused rather than
replaced. Needs Docker, through the container fixture in conftest.
"""

# Standard library
import uuid

# Third party
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage import models
from libex_core.storage.base import Base
from libex_core.storage.migrations.versions import c8d2a5e71f39_folded_search_indexes as revision
from libex_core.storage.models import CORE_TABLES, FOLD_FUNCTION_BODY, FOLD_FUNCTION_DDL, FOLDED_INDEX_NAMES
from libex_core.storage.store import LocalStore
from libex_core.storage.upgrade import VERSION_TABLE
from tests.libex_core.storage._support import core_tables, create_extensions
from tests.libex_core.storage.test_trigram_indexes_postgres import (
    NOT_NULL,
    NOT_NULL_VALUES,
    TRIGRAM_HEAD,
    _downgrade_to,
    _new_database,
    _query,
    _upgrade_store,
    _upgrade_to,
)

pytestmark = pytest.mark.integration

# What Postgres reports for each index, with the public qualifier removed.
# Written out here rather than read from the revision, so a change to the
# revision's SQL has to change this too.
PINNED = {
    "ix_books_fold_title_gin": (
        "CREATE INDEX ix_books_fold_title_gin ON books USING gin "
        "(libex_fold(((title || ' '::text) || COALESCE(subtitle, ''::text))) gin_trgm_ops)"
    ),
    "ix_authors_fold_name_gin": "CREATE INDEX ix_authors_fold_name_gin ON authors USING gin (libex_fold(name) gin_trgm_ops)",
    "ix_series_fold_title_gin": "CREATE INDEX ix_series_fold_title_gin ON series USING gin (libex_fold(title) gin_trgm_ops)",
    "ix_narrators_fold_name_gin": "CREATE INDEX ix_narrators_fold_name_gin ON narrators USING gin (libex_fold(name) gin_trgm_ops)",
    "ix_authors_fold_name_bt": "CREATE INDEX ix_authors_fold_name_bt ON authors USING btree (libex_fold(name))",
    "ix_series_fold_title_bt": "CREATE INDEX ix_series_fold_title_bt ON series USING btree (libex_fold(title))",
    "ix_books_fold_pop": (
        "CREATE INDEX ix_books_fold_pop ON books USING btree (num_ratings DESC NULLS LAST, asin, region) WHERE is_primary"
    ),
    "ix_books_fold_region_pop": (
        "CREATE INDEX ix_books_fold_region_pop ON books USING btree (region, num_ratings DESC NULLS LAST, asin)"
    ),
    "ix_book_narrator_fold_narrator_name": (
        "CREATE INDEX ix_book_narrator_fold_narrator_name ON book_narrator USING btree (narrator_name)"
    ),
}
BEFORE = TRIGRAM_HEAD
NAMES_SQL = ", ".join(f"'{n}'" for n in sorted(PINNED))
FUNCTION_SQL = (
    "SELECT p.prosrc, l.lanname, p.provolatile::text, p.proisstrict, p.proparallel::text, "
    "p.prorettype::regtype::text, p.prosecdef, p.proconfig IS NULL, pg_get_userbyid(p.proowner), "
    "pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid = 'public.books'::regclass)) "
    "FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang WHERE p.proname = 'libex_fold'"
)


async def _defs(url) -> dict:
    rows = await _query(url, f"SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN ({NAMES_SQL})")
    return {name: definition.replace("public.", "") for name, definition in rows}


async def _functions(url):
    return await _query(url, FUNCTION_SQL)


@pytest.fixture
async def url(postgres_url):
    return await _new_database(postgres_url)


def test_the_pins_cover_exactly_the_models_index_names():
    assert set(PINNED) == FOLDED_INDEX_NAMES


async def test_the_chain_builds_the_function_and_every_index_with_the_pinned_definition(url):
    await _upgrade_store(url)
    assert await _defs(url) == PINNED
    [row] = await _functions(url)
    body, language, volatile, strict, parallel, returns, definer, no_config, owner, table_owner = row
    assert (body, language, volatile, strict, parallel, returns) == (FOLD_FUNCTION_BODY, "sql", "i", True, "s", "text")
    assert (definer, no_config, owner == table_owner) == (False, True, True)
    assert await _query(url, "SELECT count(*) FROM pg_index WHERE NOT indisvalid") == [(0,)]
    # The raw-column indexes the /db filters read are still there.
    assert await _query(url, "SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'ix\\_%\\_trgm'") == [(7,)]


async def test_a_pin_that_does_not_match_is_noticed(url):
    await _upgrade_store(url)
    await _query(url, "DROP INDEX ix_authors_fold_name_gin")
    await _query(url, "CREATE INDEX ix_authors_fold_name_gin ON authors USING gin (name public.gin_trgm_ops)")
    defs = await _defs(url)
    assert defs["ix_authors_fold_name_gin"] != PINNED["ix_authors_fold_name_gin"]
    assert defs != PINNED


async def test_the_models_build_the_same_definitions_as_the_migration(url):
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await create_extensions(conn)
            await conn.run_sync(Base.metadata.create_all, tables=core_tables())
    finally:
        await engine.dispose()
    assert await _defs(url) == PINNED
    [row] = await _functions(url)
    assert row[0] == FOLD_FUNCTION_BODY and row[1:6] == ("sql", "i", True, "s", "text")


async def test_create_all_on_a_database_that_has_the_function_does_not_fail_or_replace_it(url):
    await _upgrade_store(url)
    before = await _query(url, "SELECT 'public.libex_fold(text)'::regprocedure::oid")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=core_tables())
    finally:
        await engine.dispose()
    assert await _query(url, "SELECT 'public.libex_fold(text)'::regprocedure::oid") == before


async def test_dropping_every_table_drops_the_function(url):
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await create_extensions(conn)
            await conn.run_sync(Base.metadata.create_all, tables=core_tables())
        assert len(await _functions(url)) == 1
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=core_tables())
    finally:
        await engine.dispose()
    assert await _query(url, "SELECT count(*) FROM pg_proc WHERE proname = 'libex_fold'") == [(0,)]


async def test_the_drop_listener_leaves_the_function_while_an_index_still_uses_it(url):
    await _upgrade_store(url)
    await _query(url, models._DROP_FOLD_FUNCTION.statement)
    assert len(await _functions(url)) == 1
    assert await _defs(url) == PINNED


async def test_up_down_up_on_a_populated_database_keeps_the_rows_and_both_extensions(url):
    await _upgrade_to(url, BEFORE)
    await _query(url, f"INSERT INTO books (asin, region, title, {NOT_NULL}) VALUES ('B0FOLD0001', 'us', 'Kept', {NOT_NULL_VALUES})")
    head = await _upgrade_store(url)
    assert await _defs(url) == PINNED

    await _downgrade_to(url, BEFORE)
    assert await _defs(url) == {}
    assert await _functions(url) == []
    extensions = await _query(url, "SELECT extname FROM pg_extension WHERE extname IN ('pg_trgm', 'unaccent') ORDER BY 1")
    assert extensions == [("pg_trgm",), ("unaccent",)]
    assert await _query(url, f"SELECT version_num FROM {VERSION_TABLE}") == [(BEFORE,)]
    assert await _query(url, "SELECT title FROM books") == [("Kept",)]

    assert await _upgrade_store(url) == head
    assert await _defs(url) == PINNED
    assert await _query(url, "SELECT count(*) FROM pg_index WHERE NOT indisvalid") == [(0,)]
    assert await _query(url, "SELECT title FROM books") == [("Kept",)]


async def test_a_database_with_the_objects_already_in_place_is_left_alone(url):
    await _upgrade_to(url, BEFORE)
    await _query(url, "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public")
    await _query(url, FOLD_FUNCTION_DDL)
    for name, table, tail, _ in revision.INDEXES:
        await _query(url, f"CREATE INDEX {name} ON {table} {tail}")
    oids = await _query(url, f"SELECT indexrelid::regclass::text, indexrelid::oid FROM pg_index WHERE indexrelid::regclass::text IN ({NAMES_SQL}) ORDER BY 1")
    function = await _query(url, "SELECT 'public.libex_fold(text)'::regprocedure::oid")
    assert len(oids) == len(PINNED)

    await _upgrade_store(url)

    assert await _query(url, f"SELECT indexrelid::regclass::text, indexrelid::oid FROM pg_index WHERE indexrelid::regclass::text IN ({NAMES_SQL}) ORDER BY 1") == oids
    assert await _query(url, "SELECT 'public.libex_fold(text)'::regprocedure::oid") == function
    assert await _defs(url) == PINNED


# ============================================================
# A FUNCTION OR INDEX THAT IS NOT THE EXPECTED ONE IS REFUSED
# ============================================================

def _variant(body=FOLD_FUNCTION_BODY, attributes="IMMUTABLE STRICT PARALLEL SAFE", extra=""):
    return f"CREATE FUNCTION public.libex_fold(text) RETURNS text LANGUAGE sql {attributes} {extra} AS $${body}$$"


MISMATCHES = {
    "body": (_variant(body="SELECT pg_catalog.upper(public.unaccent('public.unaccent'::regdictionary, $1))"), "body"),
    "volatility": (_variant(attributes="STABLE STRICT PARALLEL SAFE"), "volatility"),
    "strictness": (_variant(attributes="IMMUTABLE PARALLEL SAFE"), "strict"),
    "parallel safety": (_variant(attributes="IMMUTABLE STRICT"), "parallel"),
    "security definer": (_variant(extra="SECURITY DEFINER"), "security definer"),
    "a SET clause": (_variant(extra="SET search_path = public"), "a SET clause"),
}


@pytest.mark.parametrize("name", sorted(MISMATCHES))
async def test_an_existing_function_that_differs_is_refused_and_kept(url, name):
    ddl, what = MISMATCHES[name]
    await _upgrade_to(url, BEFORE)
    await _query(url, "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public")
    await _query(url, ddl)
    before = await _functions(url)

    with pytest.raises(Exception) as raised:
        await _upgrade_store(url)

    assert "does not match the definition" in str(raised.value) and what in str(raised.value)
    assert await _functions(url) == before
    assert await _defs(url) == {}
    assert await _query(url, f"SELECT version_num FROM {VERSION_TABLE}") == [(BEFORE,)]


async def test_a_second_function_of_the_name_is_refused(url):
    await _upgrade_to(url, BEFORE)
    await _query(url, "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public")
    await _query(url, FOLD_FUNCTION_DDL)
    await _query(url, "CREATE FUNCTION public.libex_fold(text, text) RETURNS text LANGUAGE sql IMMUTABLE AS $$SELECT $1$$")

    with pytest.raises(Exception) as raised:
        await _upgrade_store(url)

    assert "2 functions named libex_fold exist, expected exactly one" in str(raised.value)
    assert await _defs(url) == {}


async def test_a_function_owned_by_another_role_is_refused(url):
    await _upgrade_to(url, BEFORE)
    await _query(url, "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public")
    await _query(url, FOLD_FUNCTION_DDL)
    role = "o_" + uuid.uuid4().hex[:10]
    await _query(url, f"CREATE ROLE {role}")
    await _query(url, f"ALTER FUNCTION public.libex_fold(text) OWNER TO {role}")

    with pytest.raises(Exception) as raised:
        await _upgrade_store(url)

    assert "does not match the definition" in str(raised.value) and "owner" in str(raised.value)
    assert await _defs(url) == {}
    assert await _query(url, f"SELECT version_num FROM {VERSION_TABLE}") == [(BEFORE,)]


@pytest.mark.parametrize(
    "name,wrong",
    [
        ("ix_authors_fold_name_gin", "CREATE INDEX ix_authors_fold_name_gin ON authors USING gin (name public.gin_trgm_ops)"),
        ("ix_authors_fold_name_bt", "CREATE INDEX ix_authors_fold_name_bt ON authors (name)"),
        ("ix_books_fold_pop", "CREATE INDEX ix_books_fold_pop ON books (num_ratings, asin, region)"),
        ("ix_series_fold_title_gin", "CREATE INDEX ix_series_fold_title_gin ON series USING gin (public.libex_fold(title) public.gin_trgm_ops) WHERE is_primary"),
    ],
)
async def test_an_index_with_the_right_name_and_a_different_definition_is_refused_and_nothing_is_built(url, name, wrong):
    await _upgrade_to(url, BEFORE)
    await _query(url, "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public")
    await _query(url, FOLD_FUNCTION_DDL)
    await _query(url, wrong)
    before = await _defs(url)

    with pytest.raises(Exception) as raised:
        await _upgrade_store(url)

    assert f"{name} already exists with a different definition" in str(raised.value)
    assert await _defs(url) == before
    # The indexes ahead of it in the revision were rolled back with it.
    assert set(await _defs(url)) == {name}
    assert await _query(url, f"SELECT version_num FROM {VERSION_TABLE}") == [(BEFORE,)]


async def _plant_every_index(url):
    await _upgrade_to(url, BEFORE)
    await _query(url, "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public")
    await _query(url, FOLD_FUNCTION_DDL)
    for name, table, tail, _ in revision.INDEXES:
        await _query(url, f"CREATE INDEX {name} ON {table} {tail}")


@pytest.mark.parametrize("flag", ["indisvalid", "indisready"])
async def test_a_correctly_defined_index_that_is_not_valid_and_ready_is_refused_and_nothing_changes(url, flag):
    await _plant_every_index(url)
    # Missing ahead of the broken one, so a build before the refusal would show.
    await _query(url, "DROP INDEX ix_authors_fold_name_bt")
    await _query(url, f"UPDATE pg_index SET {flag} = false WHERE indexrelid = 'ix_series_fold_title_bt'::regclass")
    before = await _defs(url)
    assert before["ix_series_fold_title_bt"] == PINNED["ix_series_fold_title_bt"]
    assert "ix_authors_fold_name_bt" not in before

    with pytest.raises(Exception) as raised:
        await _upgrade_store(url)

    assert "ix_series_fold_title_bt already exists but is not valid and ready" in str(raised.value)
    assert await _defs(url) == before
    assert await _query(url, f"SELECT version_num FROM {VERSION_TABLE}") == [(BEFORE,)]


async def test_the_same_index_valid_and_ready_is_accepted_as_a_no_op(url):
    await _plant_every_index(url)
    sql = f"SELECT indexrelid::regclass::text, indexrelid::oid FROM pg_index WHERE indexrelid::regclass::text IN ({NAMES_SQL}) ORDER BY 1"
    oids = await _query(url, sql)

    await _upgrade_store(url)

    assert await _query(url, sql) == oids
    assert await _query(url, "SELECT count(*) FROM pg_index WHERE NOT indisvalid OR NOT indisready") == [(0,)]


async def test_pg_trgm_outside_public_is_refused_at_this_revision(url):
    await _query(url, "CREATE SCHEMA ext")
    await _query(url, "CREATE EXTENSION pg_trgm WITH SCHEMA ext")
    await _upgrade_to(url, BEFORE)

    with pytest.raises(Exception) as raised:
        await _upgrade_store(url)

    assert "pg_trgm is installed in the ext schema" in str(raised.value)
    assert await _functions(url) == [] and await _defs(url) == {}


async def test_unaccent_outside_public_is_refused(url):
    await _upgrade_to(url, BEFORE)
    await _query(url, "CREATE SCHEMA ext")
    await _query(url, "CREATE EXTENSION unaccent WITH SCHEMA ext")

    with pytest.raises(Exception) as raised:
        await _upgrade_store(url)

    assert "unaccent is installed in the ext schema" in str(raised.value)
    assert await _functions(url) == [] and await _defs(url) == {}
    assert await _query(url, f"SELECT version_num FROM {VERSION_TABLE}") == [(BEFORE,)]


# ============================================================
# A ROLE THAT CANNOT CREATE WHAT THE REVISION NEEDS
# ============================================================

async def _role_database(postgres_url, *, create_on_database, create_on_schema, unaccent_installed):
    """A database at the revision before this one, whose tables belong to a
    plain role that has only the rights granted here."""
    url = await _new_database(postgres_url)
    role = "r_" + uuid.uuid4().hex[:10]
    database = make_url(url).database
    await _upgrade_to(url, BEFORE)
    if unaccent_installed:
        await _query(url, "CREATE EXTENSION unaccent WITH SCHEMA public")
    await _query(url, f"CREATE ROLE {role} LOGIN PASSWORD 'pw'")
    for table in sorted(CORE_TABLES) + [VERSION_TABLE]:
        await _query(url, f'ALTER TABLE "{table}" OWNER TO {role}')
    await _query(url, "REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    await _query(url, f"REVOKE CREATE ON DATABASE {database} FROM PUBLIC")
    await _query(url, f"GRANT USAGE ON SCHEMA public TO {role}")
    if create_on_schema:
        await _query(url, f"GRANT CREATE ON SCHEMA public TO {role}")
    if create_on_database:
        await _query(url, f"GRANT CREATE ON DATABASE {database} TO {role}")
    role_url = make_url(url).set(username=role, password="pw").render_as_string(hide_password=False)
    return url, role_url


async def _upgrade_refused(role_url):
    store = LocalStore(role_url)
    try:
        with pytest.raises(Exception) as raised:
            await store.upgrade()
    finally:
        await store.close()
    return str(raised.value)


async def test_a_role_that_cannot_create_unaccent_is_refused_with_the_command_and_nothing_is_stamped(postgres_url):
    url, role_url = await _role_database(postgres_url, create_on_database=False, create_on_schema=True, unaccent_installed=False)
    message = await _upgrade_refused(role_url)
    assert "a privileged role must run: CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public" in message
    assert await _query(url, f"SELECT version_num FROM {VERSION_TABLE}") == [(BEFORE,)]
    assert await _query(url, "SELECT 1 FROM pg_extension WHERE extname = 'unaccent'") == []
    assert await _functions(url) == [] and await _defs(url) == {}


async def test_a_role_that_cannot_create_the_function_is_refused_with_the_command(postgres_url):
    url, role_url = await _role_database(postgres_url, create_on_database=False, create_on_schema=False, unaccent_installed=True)
    message = await _upgrade_refused(role_url)
    assert "the role that owns the tables must run: CREATE FUNCTION public.libex_fold(text)" in message
    assert FOLD_FUNCTION_BODY in message
    assert await _query(url, f"SELECT version_num FROM {VERSION_TABLE}") == [(BEFORE,)]
    assert await _functions(url) == [] and await _defs(url) == {}


async def test_the_same_role_with_the_privileges_builds_everything_and_owns_the_function(postgres_url):
    url, role_url = await _role_database(postgres_url, create_on_database=True, create_on_schema=True, unaccent_installed=False)
    await _upgrade_store(role_url)
    assert await _defs(url) == PINNED
    [row] = await _functions(url)
    assert row[8] == row[9] != "test"


async def test_the_function_made_by_another_role_is_refused_even_when_the_text_is_right(postgres_url):
    url, role_url = await _role_database(postgres_url, create_on_database=False, create_on_schema=True, unaccent_installed=True)
    await _query(url, FOLD_FUNCTION_DDL)  # as the superuser, so the tables' owner does not own it
    message = await _upgrade_refused(role_url)
    assert "owner" in message and "does not match the definition" in message
    assert await _defs(url) == {}
