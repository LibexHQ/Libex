"""
The migrations behind the search indexes, on real Postgres: every index is GIN
over gin_trgm_ops, an up, down and up on a populated database keeps the rows
and the extension, an INVALID leftover from a killed build is replaced, a valid
index is left alone, and an extension in another schema is refused.
"""

# Third party
import pytest
from sqlalchemy import create_engine, text

# Local
from tests.integration._scratch import build_template, clone, drop_database, run_alembic

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

# The NOT NULL columns of books that have no database default.
NOT_NULL = "explicit, whisper_sync, has_pdf, is_listenable, is_buyable, is_vvab, created_at, updated_at"
NOT_NULL_VALUES = "false, false, false, true, true, false, now(), now()"
BEFORE = "c4f7a2e9d1b3"
SHORT = "a8c3e51f7d29"
SHORT_NAMES = {
    "ix_books_title_trgm": "books",
    "ix_books_subtitle_trgm": "books",
    "ix_books_publisher_trgm": "books",
    "ix_books_isbn_trgm": "books",
    "ix_authors_name_trgm": "authors",
    "ix_series_title_trgm": "series",
    "ix_narrators_name_trgm": "narrators",
}


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
    return dict(_sql(sync_url, "SELECT indexname, indexdef FROM pg_indexes WHERE indexname LIKE 'ix\\_%\\_trgm'"))


def _is_trigram(indexdef):
    return " USING gin " in indexdef and "gin_trgm_ops" in indexdef


def _check(defs, names):
    assert set(defs) == set(names)
    for name, table in names.items():
        assert _is_trigram(defs[name]), defs[name]
        assert f" ON public.{table} " in defs[name]


def test_every_index_is_gin_over_the_trigram_class(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    _check(_defs(sync_url), SHORT_NAMES)


def test_the_check_rejects_a_btree_and_a_gin_with_the_default_class(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    _sql(sync_url, "CREATE INDEX plain_btree ON books (title)")
    _sql(sync_url, "CREATE INDEX plain_gin ON books USING gin (to_tsvector('simple', title))")
    defs = dict(_sql(sync_url, "SELECT indexname, indexdef FROM pg_indexes WHERE indexname LIKE 'plain\\_%'"))
    assert not _is_trigram(defs["plain_btree"]) and not _is_trigram(defs["plain_gin"])


def test_the_head_revision_builds_only_the_short_columns(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", SHORT)
    _check(_defs(sync_url), SHORT_NAMES)


def test_the_planner_uses_the_index_for_an_unescaped_ilike(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    _sql(sync_url, f"INSERT INTO books (asin, region, title, subtitle, {NOT_NULL}) VALUES ('B0TRGM0001', 'us', 'The Hobbit', 'There and back', {NOT_NULL_VALUES})")
    eng = create_engine(sync_url)
    try:
        with eng.connect() as c:
            c.execute(text("SET enable_seqscan = off"))
            plans = {
                col: "\n".join(r[0] for r in c.execute(text(f"EXPLAIN SELECT asin FROM books WHERE {col} ILIKE '%hobb%'")))
                for col in ("title", "subtitle")
            }
    finally:
        eng.dispose()
    assert "ix_books_title_trgm" in plans["title"]
    assert "ix_books_subtitle_trgm" in plans["subtitle"]


def test_up_down_up_on_a_populated_database_keeps_the_rows_and_the_extension(db):
    sync_url, async_url = db
    _sql(sync_url, f"INSERT INTO books (asin, region, title, {NOT_NULL}) VALUES ('B0TRGM0002', 'us', 'Kept', {NOT_NULL_VALUES})")
    run_alembic(async_url, "upgrade", "head")
    _check(_defs(sync_url), SHORT_NAMES)

    run_alembic(async_url, "downgrade", BEFORE)
    assert _defs(sync_url) == {}
    assert _sql(sync_url, "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'") == [(1,)]
    assert _sql(sync_url, "SELECT version_num FROM alembic_version") == [(BEFORE,)]
    assert _sql(sync_url, "SELECT title FROM books") == [("Kept",)]

    run_alembic(async_url, "upgrade", "head")
    _check(_defs(sync_url), SHORT_NAMES)
    assert _sql(sync_url, "SELECT count(*) FROM pg_index WHERE NOT indisvalid") == [(0,)]
    assert _sql(sync_url, "SELECT title FROM books") == [("Kept",)]


def test_an_invalid_leftover_is_replaced_on_rerun(db):
    sync_url, async_url = db
    target, victim, names = "head", "ix_books_title_trgm", SHORT_NAMES
    run_alembic(async_url, "upgrade", target)
    # What a killed CONCURRENTLY build leaves behind.
    _sql(sync_url, "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:n)", n=victim)
    assert _sql(sync_url, "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)", n=victim) == [(False,)]
    # Puts the chain back before the revisions so upgrade runs them again.
    _sql(sync_url, "UPDATE alembic_version SET version_num = :v", v=BEFORE)
    run_alembic(async_url, "upgrade", target)
    assert _sql(sync_url, "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)", n=victim) == [(True,)]
    _check(_defs(sync_url), names)


def test_a_valid_index_is_left_in_place_on_rerun(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    before = _sql(sync_url, "SELECT to_regclass('ix_books_title_trgm')::oid")
    assert before[0][0] is not None
    _sql(sync_url, "UPDATE alembic_version SET version_num = :v", v=BEFORE)
    run_alembic(async_url, "upgrade", "head")
    assert _sql(sync_url, "SELECT to_regclass('ix_books_title_trgm')::oid") == before


def test_an_extension_in_another_schema_is_refused_and_nothing_is_built(db):
    sync_url, async_url = db
    _sql(sync_url, "CREATE SCHEMA ext")
    _sql(sync_url, "CREATE EXTENSION pg_trgm WITH SCHEMA ext")
    with pytest.raises(RuntimeError, match="pg_trgm is installed in the ext schema"):
        run_alembic(async_url, "upgrade", "head")
    assert _defs(sync_url) == {}
    assert _sql(sync_url, "SELECT version_num FROM alembic_version") == [(BEFORE,)]
