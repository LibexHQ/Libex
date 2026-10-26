"""
books.manual_entry_at on real Postgres, through the hosted Alembic chain.

The shared container is built by `alembic upgrade head` (conftest), so the
column existing there is the proof that the hosted revision adds it. The same
revision is also run up and down on a scratch database cloned from the revision
before it. The writer is the hosted one, so the relist CASE is executed against
the schema the chain produced, not one built from the models.
"""

# Third party
import pytest
from sqlalchemy import create_engine, text

# Local
from app.db.models import Book
from app.services.db.writer import upsert_book, write_books
from tests.integration._scratch import build_template, clone, drop_database, run_alembic

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

BEFORE = "3c8e5a1f7d94"
REVISION = "b7e2d94c1a68"

ASIN = "B0MANUAL01"
LONG = "typed in by hand, " * 20
MEDIUM = "what Audible lists once it relists the book"


def _book(**fields):
    return {"asin": ASIN, "region": "us", "title": "Delisted", **fields}


async def _row(session, asin=ASIN):
    result = await session.execute(
        text("SELECT manual_entry_at, confirmed_at, description FROM books WHERE asin = :a"),
        {"a": asin},
    )
    return result.one()


# ============================================================
# THE HOSTED CHAIN
# ============================================================

@pytest.mark.asyncio
async def test_the_chain_at_head_has_a_nullable_undefaulted_timestamptz_column(db_session):
    """DB-3: the only guard on the hosted revision at the shared head."""
    result = await db_session.execute(text(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'books' AND column_name = 'manual_entry_at'"
    ))
    assert result.one() == ("timestamp with time zone", "YES", None)


@pytest.mark.asyncio
async def test_a_manual_insert_through_the_migrated_schema_sets_the_stamp(db_session):
    await upsert_book(db_session, _book(manualEntry=True))
    await upsert_book(db_session, _book(asin="B0PLAIN001"))
    await upsert_book(db_session, _book(asin="B0PLAIN002", manualEntry=False))

    assert (await _row(db_session))[0] is not None
    assert (await _row(db_session, "B0PLAIN001"))[0] is None
    assert (await _row(db_session, "B0PLAIN002"))[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False, None])
async def test_an_update_never_moves_the_stamp(db_session, flag):
    await upsert_book(db_session, _book(manualEntry=True))
    first = (await _row(db_session))[0]
    await upsert_book(db_session, _book(title="Retitled", manualEntry=flag), confirm=True)

    assert (await _row(db_session))[0] == first
    result = await db_session.execute(text("SELECT title FROM books WHERE asin = :a"), {"a": ASIN})
    assert result.scalar() == "Retitled"

    await upsert_book(db_session, _book(asin="B0PLAIN001"))
    await upsert_book(db_session, _book(asin="B0PLAIN001", manualEntry=True))
    assert (await _row(db_session, "B0PLAIN001"))[0] is None


@pytest.mark.asyncio
async def test_the_relist_runs_on_the_hosted_writer_and_postgres(db_session):
    await write_books(db_session, [
        _book(manualEntry=True, description=LONG),
        _book(asin="B0PLAIN001", description=LONG),
    ])
    await db_session.commit()

    # not Audible's answer: nothing changes
    await write_books(db_session, [_book(description=MEDIUM)])
    await db_session.commit()
    assert (await _row(db_session))[2] == LONG

    # Audible's answer: the manual row takes it, the plain row keeps the longer text
    await write_books(db_session, [
        _book(description=MEDIUM), _book(asin="B0PLAIN001", description=MEDIUM),
    ], confirm=True)
    await db_session.commit()
    manual, plain = await _row(db_session), await _row(db_session, "B0PLAIN001")
    assert manual[2] == MEDIUM and manual[0] is not None and manual[1] is not None
    assert plain[2] == LONG

    # a blank confirmed answer on a fresh manual row keeps what was typed
    await write_books(db_session, [_book(asin="B0BLANK001", manualEntry=True, description=LONG)])
    await write_books(db_session, [_book(asin="B0BLANK001", description="  ")], confirm=True)
    await db_session.commit()
    assert (await _row(db_session, "B0BLANK001"))[2] == LONG


def test_the_model_declares_the_column_the_chain_adds():
    assert "manual_entry_at" in Book.__table__.columns


# ============================================================
# THE REVISION UP AND DOWN
# ============================================================

@pytest.fixture(scope="module")
def template():
    name = build_template(BEFORE)
    yield name
    drop_database(name)


@pytest.fixture
def scratch(template):
    name, sync_url, async_url = clone(template)
    engine = create_engine(sync_url)
    yield engine, async_url
    engine.dispose()
    drop_database(name)


def _has_column(engine):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'books' AND column_name = 'manual_entry_at'"
        )).scalar() == 1


def _revision(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_the_revision_adds_the_column_keeps_rows_and_downgrades_cleanly(scratch):
    engine, url = scratch
    assert not _has_column(engine)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO books (asin, title, region, explicit, whisper_sync, has_pdf, "
            "is_listenable, is_buyable, is_vvab, created_at, updated_at) VALUES "
            "('B1', 'one', 'us', false, false, false, true, true, false, now(), now())"
        ))

    run_alembic(url, "upgrade", REVISION)
    assert _revision(engine) == REVISION
    assert _has_column(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT manual_entry_at FROM books WHERE asin = 'B1'")).scalar() is None

    run_alembic(url, "downgrade", BEFORE)
    assert _revision(engine) == BEFORE
    assert not _has_column(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM books")).scalar() == 1

    run_alembic(url, "upgrade", REVISION)  # a rerun after a downgrade is harmless
    assert _has_column(engine)
