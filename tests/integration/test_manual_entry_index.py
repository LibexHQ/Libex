"""
The migration behind the delisted list's index, on real Postgres: it builds
CONCURRENTLY, replaces an INVALID leftover from a killed build, leaves a valid
index alone, keeps books readable while the build waits, commits on success and
rolls back on failure, and a downgrade removes it.
"""

# Standard library
import threading
import time

# Third party
import pytest
from sqlalchemy import create_engine, text

# Local
from tests.integration._scratch import (
    build_template,
    clone,
    drop_database,
    run_alembic,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

PARENT = "3c8e5a1f7d94"
BEFORE = "b7e2d94c1a68"
NAME = "ix_books_manual_entry"


@pytest.fixture(scope="module")
def template():
    name = build_template(BEFORE)
    yield name
    drop_database(name)


@pytest.fixture(scope="module")
def parent_template():
    name = build_template(PARENT)
    yield name
    drop_database(name)


@pytest.fixture
def parent_db(parent_template):
    name, sync_url, async_url = clone(parent_template)
    yield sync_url, async_url
    drop_database(name)


@pytest.fixture
def db(template):
    name, sync_url, async_url = clone(template)
    yield sync_url, async_url
    drop_database(name)


def _index(sync_url):
    eng = create_engine(sync_url)
    try:
        with eng.connect() as c:
            return c.execute(
                text(
                    "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i "
                    "WHERE i.indexrelid = to_regclass(:n)"
                ),
                {"n": NAME},
            ).first()
    finally:
        eng.dispose()


def test_upgrade_builds_a_valid_partial_index(db):
    sync_url, async_url = db
    assert _index(sync_url) is None
    run_alembic(async_url, "upgrade", "head")
    valid, definition = _index(sync_url)
    assert valid
    assert "(manual_entry_at DESC, asin, region)" in definition
    assert "WHERE (manual_entry_at IS NOT NULL)" in definition


def test_an_invalid_leftover_is_replaced_on_rerun(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    eng = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        # What a killed CONCURRENTLY build leaves behind.
        c.execute(text(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:n)"
        ), {"n": NAME})
    eng.dispose()
    assert _index(sync_url)[0] is False
    eng = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        # Puts the chain back before the revision so upgrade runs it again.
        c.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
    eng.dispose()
    run_alembic(async_url, "upgrade", "head")
    assert _index(sync_url)[0] is True


def test_downgrade_removes_the_index(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    run_alembic(async_url, "downgrade", BEFORE)
    assert _index(sync_url) is None


def _oid(sync_url):
    eng = create_engine(sync_url)
    try:
        with eng.connect() as c:
            return c.execute(text("SELECT to_regclass(:n)::oid"), {"n": NAME}).scalar()
    finally:
        eng.dispose()


def test_a_valid_index_is_left_in_place_on_rerun(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    before = _oid(sync_url)
    assert before is not None
    eng = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        # Puts the chain back before the revision so upgrade runs it again.
        c.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
    eng.dispose()
    run_alembic(async_url, "upgrade", "head")
    assert _oid(sync_url) == before


def test_books_stays_readable_while_the_build_waits_on_an_older_snapshot(parent_db):
    sync_url, async_url = parent_db
    holder = create_engine(sync_url, isolation_level="REPEATABLE READ").connect()
    reader = create_engine(sync_url, isolation_level="AUTOCOMMIT").connect()
    watcher = create_engine(sync_url, isolation_level="AUTOCOMMIT").connect()
    errors = []
    try:
        # An open snapshot older than the build, which the concurrent build must wait out.
        holder.execute(text("SELECT 1 FROM authors LIMIT 1"))
        thread = threading.Thread(
            target=lambda: _capture(errors, run_alembic, async_url, "upgrade", "head")
        )
        thread.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            waiting = watcher.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE query LIKE 'CREATE INDEX CONCURRENTLY%' AND wait_event_type = 'Lock'"
            )).scalar()
            if waiting:
                break
            time.sleep(0.2)
        assert waiting, "the build never reached its wait"
        reader.execute(text("SET statement_timeout = '2s'"))
        assert reader.execute(text("SELECT 1 FROM books LIMIT 1")).all() == []
        holder.rollback()
        thread.join(60)
        assert not thread.is_alive()
        assert errors == []
        assert _index(sync_url)[0] is True
    finally:
        for c in (holder, reader, watcher):
            c.close()


def _capture(errors, fn, *args):
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - handed back to the test thread
        errors.append(exc)


def test_a_failed_upgrade_rolls_back_and_a_later_one_commits(parent_db):
    sync_url, async_url = parent_db
    blocker = create_engine(sync_url).connect()
    try:
        blocker.execute(text("LOCK TABLE books IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(Exception, match="lock timeout"):
            run_alembic(async_url, "upgrade", "head")
    finally:
        blocker.rollback()
        blocker.close()
    eng = create_engine(sync_url)
    with eng.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == PARENT
        assert c.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'books' AND column_name = 'manual_entry_at'"
        )).scalar() == 0
        assert c.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction' "
            "AND pid <> pg_backend_pid() AND datname = current_database()"
        )).scalar() == 0
    run_alembic(async_url, "upgrade", "head")
    with eng.connect() as c:
        head = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert head not in (PARENT, BEFORE)
    eng.dispose()
    assert _index(sync_url)[0] is True
