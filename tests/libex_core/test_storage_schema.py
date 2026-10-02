"""
libex_core.storage: the opt-in schema package.

Covers the import contract (importing the package loads no database library;
the extra's absence is reported by name), the cross-backend column types
against an in-memory SQLite database, and the Postgres DDL, which is compared
to a golden captured from the hosted app's models as they stood the commit
before they moved, so the move is proven byte-identical rather than assumed.
"""

# Standard library
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Third party
import pytest
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.base import CreateEnumType
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

# Local
import app.db.models as app_models
import libex_core.storage as storage
from app.db.base import Base as app_base
from libex_core.storage import models as core_models
from libex_core.storage import store as store_module
from libex_core.storage.base import Base
from libex_core.storage.types import JSONDocument, UTCDateTime, _as_utc

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden_postgres_ddl.json"

TABLES = [
    "books", "authors", "series", "narrators", "genres", "tracks",
    "author_book", "book_narrator", "book_series", "book_genre",
    "author_genre", "series_author",
]

_EXPORTS = {
    "Base": Base,
    "UTCDateTime": UTCDateTime,
    "JSONDocument": JSONDocument,
    "Book": core_models.Book,
    "Author": core_models.Author,
    "Series": core_models.Series,
    "Narrator": core_models.Narrator,
    "Genre": core_models.Genre,
    "Track": core_models.Track,
    "LocalStore": store_module.LocalStore,
    "StoreError": store_module.StoreError,
    "StoreConfigError": store_module.StoreConfigError,
    "StoreConnectionError": store_module.StoreConnectionError,
    "StoreNotInitialised": store_module.StoreNotInitialised,
    "StoreOutdated": store_module.StoreOutdated,
    "ForeignDatabase": store_module.ForeignDatabase,
    "StoreClosed": store_module.StoreClosed,
    "StoreMigrationError": store_module.StoreMigrationError,
}


def _golden() -> dict:
    return json.loads(GOLDEN.read_text())


def _child(script: str, tmp_path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        # Only the library's tables: when the hosted app is imported in the same
        # process its Postgres-only tables share this metadata.
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[Base.metadata.tables[n] for n in TABLES]
            )
        )
    yield eng
    await eng.dispose()


# ============================================================
# IMPORT CONTRACT
# ============================================================

def test_importing_storage_does_not_load_sqlalchemy(tmp_path):
    result = _child(
        "import sys, libex_core.storage\n"
        "print('LOADED:' + str(any(n == 'sqlalchemy' or n.startswith('sqlalchemy.') "
        "for n in sys.modules)))\n"
        "print('MODELS:' + str('libex_core.storage.models' in sys.modules))\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "LOADED:False" in result.stdout
    assert "MODELS:False" in result.stdout


def test_require_storage_passes_when_the_libraries_are_installed():
    storage.require_storage()


def test_require_storage_names_the_extra_when_a_library_is_missing():
    with patch("libex_core.storage.importlib.util.find_spec", return_value=None):
        with pytest.raises(storage.StorageUnavailable) as raised:
            storage.require_storage()

    assert "libex-core[storage]" in str(raised.value)
    assert "sqlalchemy" in str(raised.value)


def test_storage_unavailable_is_an_import_error():
    assert issubclass(storage.StorageUnavailable, ImportError)


def test_require_storage_names_only_what_is_missing():
    def spec(name):
        return None if name == "aiosqlite" else object()

    with patch("libex_core.storage.importlib.util.find_spec", side_effect=spec):
        with pytest.raises(storage.StorageUnavailable) as raised:
            storage.require_storage()

    assert "missing: aiosqlite" in str(raised.value)


def test_lazy_attribute_access_raises_the_same_error_when_missing():
    with patch("libex_core.storage.importlib.util.find_spec", return_value=None):
        with pytest.raises(storage.StorageUnavailable, match=r"libex-core\[storage\]"):
            storage.Book


@pytest.mark.parametrize("name", sorted(_EXPORTS))
def test_each_lazy_export_is_the_models_object(name):
    assert getattr(storage, name) is _EXPORTS[name]


def test_the_export_list_matches_what_is_tested():
    assert set(storage.__all__) == {"StorageUnavailable", "require_storage", *_EXPORTS}


def test_an_unknown_attribute_is_an_attribute_error():
    with pytest.raises(AttributeError, match="no_such_name"):
        storage.no_such_name


def test_an_unknown_attribute_does_not_need_the_extra():
    with patch("libex_core.storage.importlib.util.find_spec", return_value=None):
        with pytest.raises(AttributeError):
            storage.no_such_name


def test_the_hosted_app_re_exports_the_same_objects():
    assert app_base is Base
    for name in ("REGION_ENUM", "Book", "Author", "Series", "Narrator", "Genre",
                 "Track", "author_book", "book_narrator", "book_series",
                 "book_genre", "author_genre", "series_author"):
        assert getattr(app_models, name) is getattr(core_models, name), name


# ============================================================
# SQLITE -- schema creation and column types
# ============================================================

async def test_create_all_makes_every_table(engine):
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))

    assert set(TABLES) <= names


async def test_a_timestamp_with_an_offset_comes_back_as_the_same_utc_instant(engine):
    plus_five = timezone(timedelta(hours=5))
    written = datetime(2024, 3, 1, 12, 0, tzinfo=plus_five)
    async with engine.begin() as conn:
        await conn.execute(insert(core_models.Book).values(
            asin="B000000001", title="t", region="us", release_date=written))
        row = (await conn.execute(select(core_models.Book.release_date))).scalar_one()

    assert row.tzinfo is not None
    assert row.utcoffset() == timedelta(0)
    assert row == written
    assert row == datetime(2024, 3, 1, 7, 0, tzinfo=timezone.utc)


async def test_a_naive_timestamp_is_taken_as_utc(engine):
    async with engine.begin() as conn:
        await conn.execute(insert(core_models.Book).values(
            asin="B000000002", title="t", region="us",
            release_date=datetime(2024, 3, 1, 12, 0)))
        row = (await conn.execute(select(core_models.Book.release_date))).scalar_one()
        raw = (await conn.execute(text("select release_date from books"))).scalar_one()

    assert row == datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
    assert row.utcoffset() == timedelta(0)
    assert raw.startswith("2024-03-01 12:00:00")


async def test_a_null_timestamp_stays_null(engine):
    async with engine.begin() as conn:
        await conn.execute(insert(core_models.Book).values(
            asin="B000000003", title="t", region="us"))
        row = (await conn.execute(select(core_models.Book.release_date))).scalar_one()

    assert row is None


async def test_none_in_a_json_document_is_stored_as_sql_null(engine):
    async with engine.begin() as conn:
        await conn.execute(insert(core_models.Book).values(
            asin="B000000004", title="t", region="us", plans=None))
        raw_null = (await conn.execute(
            text("select plans is null from books"))).scalar_one()
        back = (await conn.execute(select(core_models.Book.plans))).scalar_one()

    assert raw_null == 1
    assert back is None


async def test_a_json_document_round_trips(engine):
    plans = ["US Minerva", {"a": 1}]
    async with engine.begin() as conn:
        await conn.execute(insert(core_models.Book).values(
            asin="B000000005", title="t", region="us", plans=plans))
        back = (await conn.execute(select(core_models.Book.plans))).scalar_one()

    assert back == plans


async def test_two_authors_with_a_null_asin_and_the_same_name_region_are_rejected(engine):
    authors = core_models.Author.__table__
    async with engine.begin() as conn:
        await conn.execute(insert(authors).values(name="Same", region="us", asin=None))
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(insert(authors).values(name="Same", region="us", asin=None))


async def test_null_asin_authors_in_different_regions_are_allowed(engine):
    authors = core_models.Author.__table__
    async with engine.begin() as conn:
        await conn.execute(insert(authors).values(name="Same", region="us", asin=None))
        await conn.execute(insert(authors).values(name="Same", region="uk", asin=None))


async def test_authors_with_a_non_null_asin_are_not_held_to_the_partial_index(engine):
    authors = core_models.Author.__table__
    async with engine.begin() as conn:
        await conn.execute(insert(authors).values(name="Twin", region="us", asin="A1"))
        await conn.execute(insert(authors).values(name="Twin", region="us", asin="A2"))
        await conn.execute(insert(authors).values(name="Twin", region="us", asin=None))
        count = (await conn.execute(
            text("select count(*) from authors where name = 'Twin'"))).scalar_one()

    assert count == 3


async def test_a_date_bound_to_a_utc_column_stores_and_reads_back(engine):
    """A plain DateTime column lets the driver take a date; the UTC type must
    not turn that into an AttributeError on the way in or out."""
    async with engine.begin() as conn:
        await conn.execute(insert(core_models.Book).values(
            asin="B000000006", title="t", region="us", release_date=date(2024, 3, 1)))
        row = (await conn.execute(select(core_models.Book.release_date))).scalar_one()

    assert row is not None
    assert (row.year, row.month, row.day) == (2024, 3, 1)


# ============================================================
# _as_utc
# ============================================================

def test_as_utc_returns_a_date_unchanged():
    value = date(2024, 3, 1)

    assert _as_utc(value) is value


def test_as_utc_returns_none_unchanged():
    assert _as_utc(None) is None


def test_as_utc_attaches_utc_to_a_naive_datetime():
    result = _as_utc(datetime(2024, 3, 1, 12, 0))

    assert result == datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
    assert result.utcoffset() == timedelta(0)


def test_as_utc_converts_an_aware_datetime_to_utc():
    result = _as_utc(datetime(2024, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=5))))

    assert result == datetime(2024, 3, 1, 7, 0, tzinfo=timezone.utc)
    assert result.utcoffset() == timedelta(0)
    assert result.tzinfo is timezone.utc


# ============================================================
# POSTGRES DDL -- byte-identical to the hosted schema
# ============================================================

def _ddl(name: str) -> str:
    dialect = postgresql.dialect()
    table = Base.metadata.tables[name]
    parts = [str(CreateTable(table).compile(dialect=dialect)).strip()]
    for index in sorted(table.indexes, key=lambda i: i.name):
        parts.append(str(CreateIndex(index).compile(dialect=dialect)).strip())
    return "\n".join(parts)


def test_the_golden_covers_every_table():
    golden = _golden()

    assert set(golden) == {*TABLES, "region_enum"}


@pytest.mark.parametrize("name", TABLES)
def test_the_postgres_ddl_matches_the_golden_captured_before_the_move(name):
    golden = _golden()

    assert _ddl(name) == golden[name]


def test_the_postgres_region_enum_matches_the_golden():
    golden = _golden()
    ddl = str(CreateEnumType(core_models.REGION_ENUM).compile(dialect=postgresql.dialect()))

    assert ddl.strip() == golden["region_enum"]


def test_the_golden_holds_the_partial_index_predicate():
    """A golden that lost the WHERE clause would bless a widened rule."""
    golden = _golden()

    assert "WHERE asin IS NULL" in golden["authors"]
