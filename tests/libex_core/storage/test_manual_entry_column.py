"""
books.manual_entry_at: stamped once, on insert, for a row that began as a manual
entry, and never changed or cleared by any later write. On a row that carries
it and has not been confirmed, the first Audible answer to confirm it replaces
the typed-in description, summary and extended description (the relist), but
only the first, only with text, and only for those three columns. Every other
row keeps the longer-wins merge exactly.

Run on SQLite and, when Docker is there, Postgres, since the relist is a CASE
inside the upsert's update clause and the two backends execute it separately.
"""

# Standard library
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.base import Base
from libex_core.storage.models import Book
from libex_core.storage.read.books import get_book
from libex_core.storage.read.shapes import book_to_dict
from libex_core.storage.store import LocalStore
from libex_core.storage.write import write_books
from libex_core.storage.write.params import book_params
from tests.libex_core.storage._support import core_tables

pytest.importorskip("aiosqlite")

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)

ASIN = "B0MANUAL01"
LONG = "typed in by hand, " * 20
MEDIUM = "what Audible lists once it relists the book"
THIN = "thin"

# (column, key in the writer dict)
TEXT_COLUMNS = [
    pytest.param("description", "description", id="description"),
    pytest.param("summary", "summary", id="summary"),
    pytest.param(
        "extended_product_description", "extendedProductDescription",
        id="extended_product_description",
    ),
]


class _Backend:
    def __init__(self, write, read):
        self.write = write
        self.read = read

    async def row(self, asin=ASIN, region="us"):
        async with self.read() as session:
            rows = (await session.execute(
                select(Book).filter_by(asin=asin, region=region)
            )).scalars().all()
            assert len(rows) == 1
            return rows[0]


@pytest.fixture
def clock(monkeypatch):
    """Sets the instant every writer stamps with."""
    now = {"value": T0}
    for module in ("books", "entities"):
        monkeypatch.setattr(
            f"libex_core.storage.write.{module}.utc_now", lambda: now["value"]
        )
    return lambda value: now.update(value=value)


@pytest_asyncio.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
async def backend(request):
    if request.param == "sqlite":
        store = LocalStore("sqlite+aiosqlite://")
        await store.upgrade()
        await store.open()
        yield _Backend(store.write, store.session)
        await store.close()
        return
    url = request.getfixturevalue("postgres_url")
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=core_tables())
        await conn.run_sync(Base.metadata.create_all, tables=core_tables())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def write():
        async with factory() as session:
            yield session
            await session.commit()

    @asynccontextmanager
    async def read():
        async with factory() as session:
            yield session

    yield _Backend(write, read)
    await engine.dispose()


def _book(**fields):
    return {"asin": ASIN, "region": "us", "title": "Delisted", **fields}


async def _write(backend, *books, confirm=False):
    async with backend.write() as session:
        await write_books(session, list(books), confirm=confirm)


# ============================================================
# THE COLUMN FROM THE WRITER'S SIDE
# ============================================================

def test_book_params_stamps_only_for_an_exact_true():
    stamped = book_params(_book(manualEntry=True), T0)
    assert stamped["manual_entry_at"] == T0
    for refused in (False, None, 1, "yes", "true", [True]):
        assert book_params(_book(manualEntry=refused), T0)["manual_entry_at"] is None
    assert book_params(_book(), T0)["manual_entry_at"] is None


def test_every_row_of_a_batch_binds_the_key():
    """An executemany needs uniform keys across rows, so a book without the
    flag still carries the bind."""
    assert "manual_entry_at" in book_params(_book(), T0)
    assert "manual_entry_at" in book_params(_book(manualEntry=True), T0)


# ============================================================
# STAMPING
# ============================================================

async def test_a_manual_entry_is_stamped_on_insert_and_nothing_else_is(backend, clock):
    clock(T0)
    await _write(backend, _book(manualEntry=True))
    row = await backend.row()
    assert row.manual_entry_at == T0
    assert row.confirmed_at is None


@pytest.mark.parametrize("flag", [False, None, 1, "yes"], ids=repr)
async def test_anything_but_true_leaves_a_new_row_unstamped(backend, clock, flag):
    await _write(backend, _book(manualEntry=flag))
    assert (await backend.row()).manual_entry_at is None


async def test_an_audible_write_binds_null(backend, clock):
    await _write(backend, _book(), confirm=True)
    row = await backend.row()
    assert row.manual_entry_at is None
    assert row.confirmed_at == T0


@pytest.mark.parametrize("flag", [True, False, None, "absent"], ids=repr)
@pytest.mark.parametrize("confirm", [False, True])
async def test_no_update_changes_or_clears_the_stamp(backend, clock, flag, confirm):
    clock(T0)
    await _write(backend, _book(manualEntry=True))
    clock(T2)
    update = _book(title="Retitled") if flag == "absent" else _book(title="Retitled", manualEntry=flag)
    await _write(backend, update, confirm=confirm)
    row = await backend.row()
    assert row.manual_entry_at == T0
    assert row.title == "Retitled"  # the update did run


@pytest.mark.parametrize("flag", [True, False], ids=repr)
async def test_an_update_never_stamps_a_row_that_was_not_manual(backend, clock, flag):
    await _write(backend, _book())
    clock(T2)
    await _write(backend, _book(title="Retitled", manualEntry=flag))
    row = await backend.row()
    assert row.manual_entry_at is None
    assert row.title == "Retitled"


async def test_a_mixed_batch_stamps_only_the_manual_book(backend, clock):
    await _write(
        backend,
        _book(manualEntry=True),
        _book(asin="B0PLAIN001"),
        _book(asin="B0PLAIN002", manualEntry=False),
    )
    assert (await backend.row()).manual_entry_at == T0
    assert (await backend.row("B0PLAIN001")).manual_entry_at is None
    assert (await backend.row("B0PLAIN002")).manual_entry_at is None


async def test_the_stamp_is_per_region(backend, clock):
    await _write(backend, _book(manualEntry=True), _book(region="uk"))
    assert (await backend.row(region="us")).manual_entry_at == T0
    assert (await backend.row(region="uk")).manual_entry_at is None


# ============================================================
# THE READER
# ============================================================

def test_book_to_dict_reports_the_stamp_as_a_bool():
    stamped = book_to_dict(Book(asin=ASIN, region="us", title="T", manual_entry_at=T0), {})
    unstamped = book_to_dict(Book(asin=ASIN, region="us", title="T"), {})
    assert stamped["manualEntry"] is True
    assert unstamped["manualEntry"] is False
    assert "manual_entry_at" not in stamped  # no timestamp is exposed


async def test_a_stored_row_reads_back_with_its_provenance(backend, clock):
    await _write(backend, _book(manualEntry=True), _book(asin="B0PLAIN001"))
    async with backend.read() as session:
        manual = await get_book(session, ASIN, region="us")
        plain = await get_book(session, "B0PLAIN001", region="us")
    assert manual["manualEntry"] is True
    assert plain["manualEntry"] is False


# ============================================================
# THE RELIST
# ============================================================

@pytest.mark.parametrize("column, key", TEXT_COLUMNS)
async def test_audible_text_replaces_a_longer_manual_text(backend, clock, column, key):
    clock(T0)
    await _write(backend, _book(manualEntry=True, **{key: LONG}))
    clock(T1)
    await _write(backend, _book(**{key: MEDIUM}), confirm=True)
    row = await backend.row()
    assert getattr(row, column) == MEDIUM  # longer_wins alone would keep LONG
    assert row.manual_entry_at == T0
    assert row.confirmed_at == T1


@pytest.mark.parametrize("column, key", TEXT_COLUMNS)
@pytest.mark.parametrize("blank", ["", "   ", "\t\n", None], ids=repr)
async def test_a_blank_audible_text_keeps_the_manual_text(backend, clock, column, key, blank):
    await _write(backend, _book(manualEntry=True, **{key: LONG}))
    clock(T1)
    await _write(backend, _book(**{key: blank}), confirm=True)
    row = await backend.row()
    assert getattr(row, column) == LONG
    assert row.manual_entry_at == T0


@pytest.mark.parametrize("column, key", TEXT_COLUMNS)
async def test_a_second_thinner_confirmed_write_does_not_shrink_the_relist(
    backend, clock, column, key
):
    await _write(backend, _book(manualEntry=True, **{key: LONG}))
    clock(T1)
    await _write(backend, _book(**{key: MEDIUM}), confirm=True)
    clock(T2)
    await _write(backend, _book(**{key: THIN}), confirm=True)
    assert getattr(await backend.row(), column) == MEDIUM


@pytest.mark.parametrize("column, key", TEXT_COLUMNS)
async def test_the_window_is_one_shot_and_longer_wins_after_it(backend, clock, column, key):
    await _write(backend, _book(manualEntry=True, **{key: LONG}))
    clock(T1)
    await _write(backend, _book(**{key: MEDIUM}), confirm=True)
    clock(T2)
    longer = MEDIUM + " and then some more words"
    await _write(backend, _book(**{key: longer}), confirm=True)
    assert getattr(await backend.row(), column) == longer


@pytest.mark.parametrize("column, key", TEXT_COLUMNS)
async def test_an_unconfirmed_write_does_not_trigger_the_relist(backend, clock, column, key):
    """Seed, ingest and cache writes are not Audible's answer for the book."""
    await _write(backend, _book(manualEntry=True, **{key: LONG}))
    clock(T1)
    await _write(backend, _book(**{key: MEDIUM}))
    row = await backend.row()
    assert getattr(row, column) == LONG
    assert row.confirmed_at is None

    # and the window was not spent: the first confirmed write still relists
    clock(T2)
    await _write(backend, _book(**{key: MEDIUM}), confirm=True)
    assert getattr(await backend.row(), column) == MEDIUM


@pytest.mark.parametrize("column, key", TEXT_COLUMNS)
async def test_a_row_that_was_not_a_manual_entry_stays_on_longer_wins(backend, clock, column, key):
    # confirmed_at is still NULL here, so manual_entry_at is the only thing
    # that tells this row from a manual one.
    await _write(backend, _book(**{key: LONG}))
    clock(T1)
    await _write(backend, _book(**{key: MEDIUM}), confirm=True)
    assert getattr(await backend.row(), column) == LONG

    longer = LONG + " plus more"
    clock(T2)
    await _write(backend, _book(**{key: longer}), confirm=True)
    assert getattr(await backend.row(), column) == longer


async def test_the_relist_is_judged_column_by_column(backend, clock):
    await _write(backend, _book(
        manualEntry=True, description=LONG, summary=LONG, extendedProductDescription=LONG,
    ))
    clock(T1)
    await _write(backend, _book(
        description=MEDIUM, summary=None, extendedProductDescription="  ",
    ), confirm=True)
    row = await backend.row()
    assert row.description == MEDIUM
    assert row.summary == LONG
    assert row.extended_product_description == LONG


async def test_a_relist_in_one_region_leaves_the_other_regions_manual_text(backend, clock):
    await _write(backend, _book(manualEntry=True, description=LONG),
                 _book(region="uk", manualEntry=True, description=LONG))
    clock(T1)
    await _write(backend, _book(region="uk", description=MEDIUM), confirm=True)
    assert (await backend.row(region="uk")).description == MEDIUM
    assert (await backend.row(region="us")).description == LONG


async def test_the_relist_does_not_reach_columns_outside_the_three(backend, clock):
    await _write(backend, _book(manualEntry=True, publisher="Typed Publisher Name"))
    clock(T1)
    await _write(backend, _book(publisher="   "), confirm=True)
    assert (await backend.row()).publisher == "Typed Publisher Name"
