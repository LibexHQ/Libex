"""
The partial index behind the delisted list, on the package's own chain: the head
revision builds it over the listed order and predicate, the one before it does
not, and a downgrade removes it without touching a row.
"""

# Third party
import pytest
from alembic import command
from sqlalchemy import create_engine, inspect

# Local
from libex_core.storage.models import Book
from libex_core.storage.dialect import configure_sqlite
from libex_core.storage.upgrade import _config, set_foreign_keys

HEAD = "f2b8d6a40c17"
BEFORE = "d3a8f1b65e90"
NAME = "ix_books_manual_entry"


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'store.db'}")
    configure_sqlite(eng, wal=False)
    yield eng
    eng.dispose()


def _run(engine, revision, *, down=False):
    with engine.connect() as connection:
        set_foreign_keys(connection, False)
        try:
            with connection.begin():
                (command.downgrade if down else command.upgrade)(_config(connection), revision)
        finally:
            set_foreign_keys(connection, True)


def _index_sql(engine):
    with engine.connect() as c:
        return c.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (NAME,)
        ).scalar()


def test_the_head_builds_the_partial_index_and_the_revision_before_it_does_not(engine):
    _run(engine, BEFORE)
    assert _index_sql(engine) is None
    _run(engine, HEAD)
    sql = _index_sql(engine)
    assert sql is not None
    assert "manual_entry_at DESC, asin, region" in sql
    assert "WHERE manual_entry_at IS NOT NULL" in sql


def test_the_downgrade_removes_the_index_keeps_the_rows_and_reruns_cleanly(engine):
    _run(engine, BEFORE)
    with engine.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO books (asin, title, region, explicit, whisper_sync, has_pdf, is_listenable, "
            "is_buyable, is_vvab, is_primary, created_at, updated_at, manual_entry_at) VALUES "
            "('B1', 'one', 'us', 0, 0, 0, 0, 0, 0, 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00')"
        )
    _run(engine, HEAD)
    assert _index_sql(engine) is not None
    _run(engine, BEFORE, down=True)
    assert _index_sql(engine) is None
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT asin FROM books").all() == [("B1",)]
    _run(engine, HEAD)
    assert _index_sql(engine) is not None


def test_the_model_declares_the_index_the_chain_builds(engine):
    declared = {i.name: i for i in Book.__table__.indexes}[NAME]
    assert [c.name for c in declared.columns] == ["manual_entry_at", "asin", "region"]
    _run(engine, HEAD)
    assert NAME in {i["name"] for i in inspect(engine).get_indexes("books")}
