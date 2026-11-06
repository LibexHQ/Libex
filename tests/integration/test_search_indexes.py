"""
The hosted revision behind the ranked search, on real Postgres: the function
and every index have the pinned definition, an up, down and up on a populated
database keeps the rows and both extensions, a database prepared ahead of the
deploy is left alone, an invalid leftover from a killed build is rebuilt, and a
function or index that differs from the expected one is refused, not replaced.
"""

# Standard library
import uuid

# Third party
import pytest
from sqlalchemy import create_engine, text

# Local
from libex_core.storage.models import FOLD_FUNCTION_BODY, FOLD_FUNCTION_DDL
from migrations.versions import e6a1d4f93b27_folded_search_indexes as revision
from tests.integration._scratch import build_template, clone, drop_database, run_alembic
from tests.libex_core.storage.test_search_indexes_postgres import FUNCTION_SQL, NAMES_SQL, PINNED

pytestmark = [pytest.mark.integration, pytest.mark.timeout(600)]

BEFORE = "a8c3e51f7d29"
HEAD = "e6a1d4f93b27"
NOT_NULL = "explicit, whisper_sync, has_pdf, is_listenable, is_buyable, is_vvab, created_at, updated_at"
NOT_NULL_VALUES = "false, false, false, true, true, false, now(), now()"


@pytest.fixture(scope="module")
def template():
    name = build_template(BEFORE)
    yield name
    drop_database(name)


@pytest.fixture
def db(template):
    name, sync_url, async_url = clone(template)
    yield sync_url, async_url
    drop_database(name)


def _sql(sync_url, sql, **params):
    eng = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            result = c.execute(text(sql), params)
            return result.all() if result.returns_rows else None
    finally:
        eng.dispose()


def _defs(sync_url):
    rows = _sql(sync_url, f"SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN ({NAMES_SQL})")
    return {name: definition.replace("public.", "") for name, definition in rows}


def _version(sync_url):
    return _sql(sync_url, "SELECT version_num FROM alembic_version")


def _prebuild(sync_url):
    """What the production pre-build does ahead of the deploy: the extension, the
    function and every index, built concurrently."""
    _sql(sync_url, "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public")
    _sql(sync_url, FOLD_FUNCTION_DDL)
    for name, table, tail, _ in revision.INDEXES:
        _sql(sync_url, f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {tail}")


def _oids(sync_url):
    return _sql(sync_url, f"SELECT indexrelid::regclass::text, indexrelid::oid FROM pg_index WHERE indexrelid::regclass::text IN ({NAMES_SQL}) ORDER BY 1")


def test_the_revision_builds_the_function_and_every_index_with_the_pinned_definition(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    assert _defs(sync_url) == PINNED
    [row] = _sql(sync_url, FUNCTION_SQL)
    assert row[:6] == (FOLD_FUNCTION_BODY, "sql", "i", True, "s", "text")
    assert (row[6], row[7], row[8] == row[9]) == (False, True, True)
    assert _sql(sync_url, "SELECT count(*) FROM pg_index WHERE NOT indisvalid") == [(0,)]
    assert _version(sync_url) == [(HEAD,)]
    # The S1 indexes are untouched.
    assert _sql(sync_url, "SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'ix\\_%\\_trgm'") == [(7,)]


def test_up_down_up_on_a_populated_database_keeps_the_rows_and_both_extensions(db):
    sync_url, async_url = db
    _sql(sync_url, f"INSERT INTO books (asin, region, title, {NOT_NULL}) VALUES ('B0FOLD0002', 'us', 'Kept', {NOT_NULL_VALUES})")
    run_alembic(async_url, "upgrade", "head")
    assert _defs(sync_url) == PINNED

    run_alembic(async_url, "downgrade", BEFORE)
    assert _defs(sync_url) == {}
    assert _sql(sync_url, "SELECT count(*) FROM pg_proc WHERE proname = 'libex_fold'") == [(0,)]
    assert _sql(sync_url, "SELECT extname FROM pg_extension WHERE extname IN ('pg_trgm', 'unaccent') ORDER BY 1") == [("pg_trgm",), ("unaccent",)]
    assert _version(sync_url) == [(BEFORE,)]
    assert _sql(sync_url, "SELECT title FROM books") == [("Kept",)]

    run_alembic(async_url, "upgrade", "head")
    assert _defs(sync_url) == PINNED
    assert _sql(sync_url, "SELECT count(*) FROM pg_index WHERE NOT indisvalid") == [(0,)]
    assert _sql(sync_url, "SELECT title FROM books") == [("Kept",)]


def test_a_database_prepared_ahead_of_the_deploy_is_left_alone(db):
    sync_url, async_url = db
    _prebuild(sync_url)
    oids = _oids(sync_url)
    function = _sql(sync_url, "SELECT 'public.libex_fold(text)'::regprocedure::oid")
    assert len(oids) == len(PINNED)

    run_alembic(async_url, "upgrade", "head")

    assert _oids(sync_url) == oids
    assert _sql(sync_url, "SELECT 'public.libex_fold(text)'::regprocedure::oid") == function
    assert _defs(sync_url) == PINNED
    assert _version(sync_url) == [(HEAD,)]


def test_an_invalid_leftover_from_a_killed_build_is_rebuilt(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    victim = "ix_books_fold_title_gin"
    before = dict(_oids(sync_url))
    # What a killed CONCURRENTLY build leaves behind.
    _sql(sync_url, "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:n)", n=victim)
    assert _sql(sync_url, "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)", n=victim) == [(False,)]
    _sql(sync_url, "UPDATE alembic_version SET version_num = :v", v=BEFORE)

    run_alembic(async_url, "upgrade", "head")

    assert _sql(sync_url, "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)", n=victim) == [(True,)]
    after = dict(_oids(sync_url))
    assert after[victim] != before[victim], "the invalid index was kept rather than rebuilt"
    # Valid ones were not touched.
    assert {k: v for k, v in after.items() if k != victim} == {k: v for k, v in before.items() if k != victim}
    assert _defs(sync_url) == PINNED


def _variant(body=FOLD_FUNCTION_BODY, attributes="IMMUTABLE STRICT PARALLEL SAFE", extra=""):
    return f"CREATE FUNCTION public.libex_fold(text) RETURNS text LANGUAGE sql {attributes} {extra} AS $${body}$$"


@pytest.mark.parametrize(
    "ddl,what",
    [
        (_variant(body="SELECT pg_catalog.upper(public.unaccent('public.unaccent'::regdictionary, $1))"), "body"),
        (_variant(attributes="STABLE STRICT PARALLEL SAFE"), "volatility"),
        (_variant(extra="SECURITY DEFINER"), "security definer"),
        (_variant(extra="SET search_path = public"), "a SET clause"),
    ],
    ids=["body", "volatility", "security-definer", "set-clause"],
)
def test_an_existing_function_that_differs_is_refused_and_kept(db, ddl, what):
    sync_url, async_url = db
    _sql(sync_url, "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public")
    _sql(sync_url, ddl)
    before = _sql(sync_url, FUNCTION_SQL)

    with pytest.raises(Exception) as raised:
        run_alembic(async_url, "upgrade", "head")

    assert "does not match the definition" in str(raised.value) and what in str(raised.value)
    assert _sql(sync_url, FUNCTION_SQL) == before
    assert _defs(sync_url) == {}
    assert _version(sync_url) == [(BEFORE,)]


def test_a_function_owned_by_another_role_or_overloaded_is_refused(db):
    sync_url, async_url = db
    _sql(sync_url, "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public")
    _sql(sync_url, FOLD_FUNCTION_DDL)
    role = "o_" + uuid.uuid4().hex[:10]
    _sql(sync_url, f"CREATE ROLE {role}")
    _sql(sync_url, f"ALTER FUNCTION public.libex_fold(text) OWNER TO {role}")
    with pytest.raises(Exception) as raised:
        run_alembic(async_url, "upgrade", "head")
    assert "owner" in str(raised.value) and "does not match the definition" in str(raised.value)

    _sql(sync_url, "ALTER FUNCTION public.libex_fold(text) OWNER TO CURRENT_USER")
    _sql(sync_url, "CREATE FUNCTION public.libex_fold(text, text) RETURNS text LANGUAGE sql IMMUTABLE AS $$SELECT $1$$")
    with pytest.raises(Exception) as raised:
        run_alembic(async_url, "upgrade", "head")
    assert "2 functions named libex_fold exist, expected exactly one" in str(raised.value)
    assert _defs(sync_url) == {} and _version(sync_url) == [(BEFORE,)]


def test_an_index_with_the_right_name_and_another_definition_is_refused_then_built_once_removed(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    victim = "ix_authors_fold_name_gin"
    _sql(sync_url, f"DROP INDEX {victim}")
    _sql(sync_url, f"CREATE INDEX {victim} ON authors USING gin (name public.gin_trgm_ops)")
    wrong = _defs(sync_url)[victim]
    _sql(sync_url, "UPDATE alembic_version SET version_num = :v", v=BEFORE)

    with pytest.raises(Exception) as raised:
        run_alembic(async_url, "upgrade", "head")

    assert f"{victim} already exists with a different definition" in str(raised.value)
    assert _defs(sync_url)[victim] == wrong != PINNED[victim]
    assert _version(sync_url) == [(BEFORE,)]

    _sql(sync_url, f"DROP INDEX {victim}")
    run_alembic(async_url, "upgrade", "head")
    assert _defs(sync_url) == PINNED and _version(sync_url) == [(HEAD,)]


def test_unaccent_outside_public_is_refused_before_anything_is_built(db):
    sync_url, async_url = db
    _sql(sync_url, "CREATE SCHEMA ext")
    _sql(sync_url, "CREATE EXTENSION unaccent WITH SCHEMA ext")

    with pytest.raises(Exception) as raised:
        run_alembic(async_url, "upgrade", "head")

    assert "unaccent is installed in the ext schema" in str(raised.value)
    assert _defs(sync_url) == {}
    assert _sql(sync_url, "SELECT count(*) FROM pg_proc WHERE proname = 'libex_fold'") == [(0,)]
    assert _version(sync_url) == [(BEFORE,)]
