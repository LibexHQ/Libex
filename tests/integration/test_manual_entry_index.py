"""
The migration behind the delisted list's index, on real Postgres: it builds
CONCURRENTLY, replaces an INVALID leftover from a killed build, and a downgrade
removes it.
"""

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

BEFORE = "b7e2d94c1a68"
NAME = "ix_books_manual_entry"


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
        c.execute(text(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:n)"
        ), {"n": NAME})
    eng.dispose()
    assert _index(sync_url)[0] is False
    eng = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
    eng.dispose()
    run_alembic(async_url, "upgrade", "head")
    assert _index(sync_url)[0] is True


def test_downgrade_removes_the_index(db):
    sync_url, async_url = db
    run_alembic(async_url, "upgrade", "head")
    run_alembic(async_url, "downgrade", BEFORE)
    assert _index(sync_url) is None
