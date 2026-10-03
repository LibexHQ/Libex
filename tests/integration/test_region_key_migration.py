"""
The hosted revision that keys books, series and tracks by (asin, region).

It runs against real Postgres, in scratch databases cloned from one template
built at the revision before it, because everything it does is catalog work
that only a real server can judge: primary keys adopted from indexes built
ahead of time, composite foreign keys added NOT VALID, and a refusal to run
when that preparation is missing or half done.

The preparation is done here by hand, in the same statements and under the
same index names as scripts/region_keys.py, which is what the revision assumes
has already happened on the hosted database.
"""

# Standard library
import asyncio
import importlib.util
import uuid

# Third party
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Local
import scripts.region_keys as rk
from libex_core.storage.base import Base
from libex_core.storage.models import CORE_TABLES
from tests.integration._scratch import (
    ROOT,
    admin,
    build_template,
    clone,
    drop_database,
    run_alembic,
    urls,
)

# The template fixture below upgrades the whole chain inside whichever test
# asks for it first, and pytest-timeout counts fixture setup against that
# test's budget. Idle that takes about 20 seconds; under the machine-wide load
# of a full run it measured past the 30 second default, so the module carries
# its own, still a tripwire for a hang.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

PRE = "b8d2e5a71c46"
POST = "e7c2a94d1f58"

STAMP = "now()"
BOOK_COLUMNS = (
    "explicit, whisper_sync, has_pdf, is_listenable, is_buyable, is_vvab, created_at, updated_at"
)
BOOK_VALUES = f"false, false, false, true, true, false, {STAMP}, {STAMP}"

COUNTED = (
    "books", "series", "tracks", "author_book", "book_narrator", "book_genre",
    "book_series", "series_author",
)

_spec = importlib.util.spec_from_file_location(
    "region_key_revision",
    next((ROOT / "migrations" / "versions").glob("e7c2a94d1f58_*.py")),
)
revision = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(revision)


# ============================================================
# SCRATCH DATABASES
# ============================================================

_alembic = run_alembic


@pytest.fixture(scope="module")
def template():
    name = build_template(PRE)
    yield name
    drop_database(name)


@pytest.fixture
def scratch(template):
    """A fresh database at the revision before the swap."""
    name, sync_url, async_url = clone(template)
    engine = create_engine(sync_url)
    yield engine, async_url
    engine.dispose()
    drop_database(name)


# ============================================================
# A CATALOGUE, AND THE PREPARATION THE SCRIPT DOES
# ============================================================

def _seed(engine) -> None:
    with engine.begin() as c:
        c.execute(text(
            f"INSERT INTO books (asin, title, region, {BOOK_COLUMNS}) VALUES "
            f"('B1', 'one', 'uk', {BOOK_VALUES}), ('B2', 'two', 'us', {BOOK_VALUES}), "
            f"('B3', 'three', 'de', {BOOK_VALUES})"
        ))
        c.execute(text(
            "INSERT INTO series (asin, title, region, fetched_description, created_at, updated_at) "
            f"VALUES ('S1', 's one', 'uk', false, {STAMP}, {STAMP}), "
            f"('S2', 's two', 'us', false, {STAMP}, {STAMP})"
        ))
        c.execute(text(
            "INSERT INTO authors (id, asin, name, region, fetched_description, created_at, updated_at) "
            f"VALUES (1, 'A1', 'ann', 'uk', false, {STAMP}, {STAMP})"
        ))
        c.execute(text(
            "INSERT INTO narrators (name, created_at, updated_at) "
            f"VALUES ('nina', {STAMP}, {STAMP})"
        ))
        c.execute(text(
            "INSERT INTO genres (asin, name, type, created_at, updated_at) "
            f"VALUES ('G1', 'g', 'Genres', {STAMP}, {STAMP})"
        ))
        c.execute(text("INSERT INTO author_book VALUES (1, 'B1'), (1, 'B2')"))
        c.execute(text("INSERT INTO book_narrator VALUES ('nina', 'B1'), ('nina', 'B3')"))
        c.execute(text("INSERT INTO book_genre VALUES ('B1', 'G1'), ('B2', 'G1')"))
        c.execute(text("INSERT INTO book_series VALUES ('B1', 'S1', '1'), ('B2', 'S2', '4')"))
        c.execute(text("INSERT INTO series_author VALUES ('S1', 1)"))
        c.execute(text(
            "INSERT INTO tracks (asin, chapters, created_at, updated_at) "
            f"VALUES ('B1', '[]', {STAMP}, {STAMP}), ('B3', '[]', {STAMP}, {STAMP})"
        ))


# column, the column holding the ASIN, the parent it copies the region from
_BACKFILLS = (
    ("author_book", "book_region", "book_asin", "books"),
    ("book_narrator", "book_region", "book_asin", "books"),
    ("book_genre", "book_region", "book_asin", "books"),
    ("book_series", "book_region", "book_asin", "books"),
    ("book_series", "series_region", "series_asin", "series"),
    ("series_author", "series_region", "series_asin", "series"),
    ("tracks", "region", "asin", "books"),
)


def _prepare(engine, *, skip_index=None, leave_nullable=None, null_series=False) -> None:
    """The expand, backfill, index and finalize steps, as the script runs them.
    The keyword arguments leave one piece undone, to see the revision notice."""
    with engine.begin() as c:
        for table, column, key, parent in _BACKFILLS:
            c.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} region_enum"))
            c.execute(text(
                f"UPDATE {table} SET {column} = p.region FROM {parent} p "
                f"WHERE p.asin = {table}.{key}"
            ))
        if null_series:
            c.execute(text(
                "INSERT INTO series (asin, title, region, fetched_description, created_at, updated_at) "
                f"VALUES ('S9', 'no region', NULL, false, {STAMP}, {STAMP})"
            ))
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        for name, (table, columns) in revision._INDEXES.items():
            if name != skip_index:
                c.execute(text(f"CREATE UNIQUE INDEX {name} ON {table} ({', '.join(columns)})"))
        for name, (table, columns) in revision._WIDENED.items():
            if name != skip_index:
                c.execute(text(f"CREATE INDEX {name} ON {table} ({', '.join(columns)})"))
    with engine.begin() as c:
        for table, column in revision._NOT_NULL:
            if (table, column) == leave_nullable or (null_series and table == "series"):
                continue
            c.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL"))


def _counts(engine):
    with engine.connect() as c:
        return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in COUNTED}


def _head(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


# ============================================================
# THE REVISION, ON A PREPARED DATABASE
# ============================================================

def test_the_revision_swaps_the_keys_and_keeps_every_row(scratch):
    engine, url = scratch
    _seed(engine)
    _prepare(engine)
    before = _counts(engine)

    _alembic(url, "upgrade", POST)

    assert _head(engine) == POST
    assert _counts(engine) == before
    inspector = inspect(engine)
    for table in ("books", "series", "tracks"):
        assert inspector.get_pk_constraint(table)["constrained_columns"] == ["asin", "region"]
        assert inspector.get_pk_constraint(table)["name"] == f"{table}_pkey"
    with engine.connect() as c:
        # The script's indexes became the keys, under the constraints' names.
        names = {r[0] for r in c.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        ))}
        assert not names & set(revision._INDEXES)
        assert not names & set(revision._WIDENED)
        # The wider index took the narrower one's name.
        assert c.execute(text(
            "SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = 'genre_book_index'::regclass"
        )).scalar().endswith("(genre_asin, book_asin, book_region)")
        assert {"uq_author_book", "uq_book_narrator", "uq_book_genre", "uq_book_series",
                "uq_series_author", "books_pkey", "series_pkey", "tracks_pkey"} <= names
        # The indexes the new keys make redundant are gone.
        assert not names & {name for name, _t, _c in revision._DROPPED_INDEXES}
        # Added NOT VALID: enforced for every new row, not yet scanned.
        validated = dict(c.execute(text(
            "SELECT conname, convalidated FROM pg_constraint WHERE contype = 'f' "
            "AND conname LIKE '%region%fkey'"
        )).all())
        assert set(validated) == {name for _t, name, *_ in revision._COMPOSITE_FKS}
        assert not any(validated.values())
        # And every link kept the region of its parent.
        assert c.execute(text(
            "SELECT book_asin, book_region FROM author_book ORDER BY 1"
        )).all() == [("B1", "uk"), ("B2", "us")]


def test_every_row_that_exists_is_primary_and_the_column_is_catalog_only(scratch):
    engine, url = scratch
    _seed(engine)
    _prepare(engine)
    _alembic(url, "upgrade", POST)

    with engine.connect() as c:
        for table in ("books", "series"):
            assert c.execute(text(
                f"SELECT count(*) FROM {table} WHERE NOT is_primary"
            )).scalar() == 0
            column = c.execute(text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                f"WHERE table_name = '{table}' AND column_name = 'is_primary'"
            )).one()
            assert column == ("NO", "true")
    # Inserted without naming it, as any older caller does: primary.
    with engine.begin() as c:
        c.execute(text(
            f"INSERT INTO books (asin, title, region, {BOOK_COLUMNS}) "
            f"VALUES ('B7', 'seven', 'fr', {BOOK_VALUES})"
        ))
        assert c.execute(text("SELECT is_primary FROM books WHERE asin = 'B7'")).scalar() is True


def test_after_the_swap_the_same_asin_in_two_regions_is_two_books(scratch):
    engine, url = scratch
    _seed(engine)
    _prepare(engine)
    _alembic(url, "upgrade", POST)

    with engine.begin() as c:
        c.execute(text(
            f"INSERT INTO books (asin, title, region, {BOOK_COLUMNS}) "
            f"VALUES ('B1', 'one in the us', 'us', {BOOK_VALUES})"
        ))
        c.execute(text("INSERT INTO author_book (author_id, book_asin, book_region) VALUES (1, 'B1', 'us')"))
        c.execute(text("INSERT INTO book_genre (book_asin, book_region, genre_asin) VALUES ('B1', 'us', 'G1')"))
        c.execute(text(
            "INSERT INTO tracks (asin, region, chapters, created_at, updated_at) "
            f"VALUES ('B1', 'us', '[]', {STAMP}, {STAMP})"
        ))
        c.execute(text("DELETE FROM books WHERE asin = 'B1' AND region = 'uk'"))
        assert c.execute(text(
            "SELECT book_region FROM author_book WHERE book_asin = 'B1'"
        )).all() == [("us",)]
        assert c.execute(text("SELECT region FROM tracks WHERE asin = 'B1'")).all() == [("us",)]

    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO author_book (author_id, book_asin, book_region) VALUES (1, 'B2', 'fr')"
            ))


def test_the_revision_keeps_the_index_names_the_expand_script_builds():
    scripts = ROOT / "scripts" / "region_keys.py"
    if not scripts.exists():
        pytest.skip("scripts/region_keys.py is not in this tree yet")
    spec = importlib.util.spec_from_file_location("region_keys_script", scripts)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    unique = {i.name: (i.table, tuple(i.columns)) for i in module.INDEXES if i.unique}
    plain = {i.name: (i.table, tuple(i.columns)) for i in module.INDEXES if not i.unique}
    assert unique == revision._INDEXES
    assert plain == revision._WIDENED


def test_the_validate_mode_names_the_foreign_keys_the_revision_adds():
    assert set(rk.FOREIGN_KEYS) == {(table, name) for table, name, *_ in revision._COMPOSITE_FKS}


def _validate(url):
    async def run():
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            return await rk.validate(engine, rk._Stop())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _validated(engine):
    with engine.connect() as c:
        return dict(c.execute(text(
            "SELECT conname, convalidated FROM pg_constraint WHERE contype = 'f' "
            "AND conname LIKE '%region%fkey'"
        )).all())


def test_validate_checks_every_added_key_online_and_can_run_again(scratch):
    engine, url = scratch
    _seed(engine)
    _prepare(engine)
    _alembic(url, "upgrade", POST)
    assert not any(_validated(engine).values())

    assert _validate(url) == rk.EXIT_OK

    state = _validated(engine)
    assert set(state) == {name for _t, name in rk.FOREIGN_KEYS}
    assert all(state.values())
    assert _validate(url) == rk.EXIT_OK  # already validated: skipped, not an error


def test_validate_fails_on_a_link_that_points_at_no_stored_book(scratch):
    engine, url = scratch
    _seed(engine)
    _prepare(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE author_book SET book_region = 'fr' WHERE book_asin = 'B1'"))
    _alembic(url, "upgrade", POST)

    with pytest.raises(DBAPIError):
        _validate(url)
    assert not _validated(engine)["author_book_book_asin_book_region_fkey"]


def test_validate_stops_when_the_revision_has_not_run(scratch):
    _engine, url = scratch
    with pytest.raises(rk.FinalizeAbort, match="has not run"):
        _validate(url)


# ============================================================
# AN UNPREPARED DATABASE IS REFUSED, NEVER FIXED
# ============================================================

def _refused(scratch, message, **undone):
    engine, url = scratch
    _seed(engine)
    if undone.get("unprepared"):
        pass
    else:
        _prepare(engine, **undone)
    before = _counts(engine)
    with pytest.raises(RuntimeError, match=message):
        _alembic(url, "upgrade", POST)
    # Nothing moved: the failed revision rolled back whole.
    assert _head(engine) == PRE
    assert _counts(engine) == before
    inspector = inspect(engine)
    assert inspector.get_pk_constraint("books")["constrained_columns"] == ["asin"]
    assert "walk_results" not in inspector.get_table_names()
    assert "confirmed_at" not in {c["name"] for c in inspector.get_columns("books")}


def test_a_database_that_was_never_prepared_is_refused(scratch):
    _refused(scratch, r"column author_book\.book_region does not exist", unprepared=True)


def test_a_missing_index_is_refused(scratch):
    _refused(scratch, "index uq_book_genre_region does not exist", skip_index="uq_book_genre_region")


def test_a_missing_widened_genre_index_is_refused(scratch):
    _refused(scratch, "index genre_book_region_index does not exist", skip_index="genre_book_region_index")


def test_a_column_still_nullable_is_refused(scratch):
    _refused(scratch, r"column tracks\.region is nullable", leave_nullable=("tracks", "region"))


def test_a_series_with_no_region_is_refused(scratch):
    _refused(scratch, "1 series rows have a NULL region", null_series=True)


def test_an_invalid_index_is_refused(scratch):
    engine, url = scratch
    _seed(engine)
    _prepare(engine)
    with engine.begin() as c:
        c.execute(text(
            "UPDATE pg_index SET indisvalid = false "
            "WHERE indexrelid = 'uq_series_author_region'::regclass"
        ))
    with pytest.raises(RuntimeError, match="index uq_series_author_region is invalid"):
        _alembic(url, "upgrade", POST)
    assert _head(engine) == PRE


# ============================================================
# DOWNGRADE
# ============================================================

def test_downgrade_refuses_once_an_asin_has_two_regions_and_deletes_nothing(scratch):
    engine, url = scratch
    _seed(engine)
    _prepare(engine)
    _alembic(url, "upgrade", POST)
    with engine.begin() as c:
        c.execute(text(
            f"INSERT INTO books (asin, title, region, {BOOK_COLUMNS}) "
            f"VALUES ('B1', 'one in the us', 'us', {BOOK_VALUES})"
        ))
    before = _counts(engine)
    with pytest.raises(RuntimeError, match=r"1 ASINs in books.*Nothing was changed or deleted"):
        _alembic(url, "downgrade", PRE)
    assert _head(engine) == POST
    assert _counts(engine) == before


def test_downgrade_restores_the_single_region_keys_and_keeps_every_row(scratch):
    engine, url = scratch
    _seed(engine)
    _prepare(engine)
    before = _counts(engine)
    _alembic(url, "upgrade", POST)
    _alembic(url, "downgrade", PRE)

    assert _head(engine) == PRE
    assert _counts(engine) == before
    inspector = inspect(engine)
    for table in ("books", "series", "tracks"):
        assert inspector.get_pk_constraint(table)["constrained_columns"] == ["asin"]
    assert {i["name"] for i in inspector.get_indexes("books")} == {
        "books_asin_index", "books_region_asin_index",
    }
    assert "walk_results" not in inspector.get_table_names()
    # Older code can insert without the region columns again.
    with engine.begin() as c:
        c.execute(text("INSERT INTO author_book (author_id, book_asin) VALUES (1, 'B3')"))
    # And the swap can be done again from there.
    with engine.begin() as c:
        c.execute(text("DELETE FROM author_book WHERE book_asin = 'B3'"))


# ============================================================
# AN EMPTY DATABASE PREPARES ITSELF
# ============================================================

def test_a_database_with_no_rows_builds_what_the_script_would_have(scratch):
    engine, url = scratch
    _alembic(url, "upgrade", POST)
    inspector = inspect(engine)
    for table in ("books", "series", "tracks"):
        assert inspector.get_pk_constraint(table)["constrained_columns"] == ["asin", "region"]
    assert not {c["name"]: c for c in inspector.get_columns("tracks")}["region"]["nullable"]
    assert not {c["name"]: c for c in inspector.get_columns("series")}["region"]["nullable"]


# ============================================================
# THE MIGRATED SCHEMA AGAINST THE MODELS, AND THE TWO CHAINS AGAINST EACH OTHER
# ============================================================

def _shape(engine) -> dict:
    """The core tables as the database reports them: columns, keys, indexes."""
    inspector = inspect(engine)
    shape = {}
    for table in sorted(CORE_TABLES):
        shape[table] = {
            "columns": {
                c["name"]: (str(c["type"]), bool(c["nullable"]))
                for c in inspector.get_columns(table)
            },
            "pk": inspector.get_pk_constraint(table)["constrained_columns"],
            "uniques": sorted(
                (u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(table)
            ),
            "fks": sorted(
                (tuple(f["constrained_columns"]), f["referred_table"],
                 tuple(f["referred_columns"]), (f.get("options") or {}).get("ondelete"))
                for f in inspector.get_foreign_keys(table)
            ),
            "indexes": sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in inspector.get_indexes(table)
                if not i.get("duplicates_constraint")
            ),
        }
    return shape


def _model_shape() -> dict:
    shape = {}
    for table in sorted(CORE_TABLES):
        t = Base.metadata.tables[table]
        fks = sorted(
            (tuple(c.name for c in fk.columns), fk.referred_table.name,
             tuple(e.column.name for e in fk.elements), fk.ondelete)
            for fk in t.foreign_key_constraints
        )
        shape[table] = {
            "pk": [c.name for c in t.primary_key.columns],
            "uniques": sorted(
                (u.name, tuple(c.name for c in u.columns))
                for u in t.constraints
                if u.__class__.__name__ == "UniqueConstraint"
            ),
            "fks": fks,
            "indexes": sorted(
                (i.name, tuple(c.name for c in i.columns), bool(i.unique)) for i in t.indexes
            ),
        }
    return shape


def test_the_hosted_head_has_the_keys_the_models_declare(scratch):
    engine, url = scratch
    _alembic(url, "upgrade", "head")
    actual, expected = _shape(engine), _model_shape()
    for table in expected:
        for part in ("pk", "uniques", "fks", "indexes"):
            assert actual[table][part] == expected[table][part], (table, part)


def test_the_package_chain_and_the_hosted_chain_build_the_same_schema(scratch):
    from libex_core.storage.upgrade import upgrade_to_head

    hosted, url = scratch
    _alembic(url, "upgrade", "head")

    name = f"rk_pkg_{uuid.uuid4().hex[:8]}"
    server = admin()
    with server.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    package = create_engine(urls(name)[0])
    try:
        with package.begin() as connection:
            upgrade_to_head(connection)
        theirs, ours = _shape(package), _shape(hosted)
    finally:
        package.dispose()
        with server.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        server.dispose()

    for table in ours:
        for part in ("columns", "pk", "uniques", "fks", "indexes"):
            assert theirs[table][part] == ours[table][part], (table, part)
