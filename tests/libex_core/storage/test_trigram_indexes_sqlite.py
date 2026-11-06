"""
The trigram revision on SQLite: nothing is built and the chain still reaches
its head, and the models' Postgres-only indexes do not read as missing there.
"""

# Third party
import pytest
from sqlalchemy import text

# Local
from libex_core.storage.base import Base
from libex_core.storage.models import FOLDED_INDEX_NAMES
from libex_core.storage.store import LocalStore
from libex_core.storage.upgrade import include_object_for


@pytest.fixture
def url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'libex.db'}"


async def test_the_revision_builds_no_index_on_sqlite_and_the_chain_reaches_its_head(url):
    store = LocalStore(url)
    try:
        head = await store.upgrade()
        async with store._engine.connect() as connection:
            names = (await connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'"))).scalars().all()
            stamped = (await connection.execute(text("SELECT version_num FROM libex_core_alembic_version"))).scalar()
    finally:
        await store.close()
    assert stamped == head == "c8d2a5e71f39"
    assert not [n for n in names if n.endswith("_trgm")]
    assert not [n for n in names if "_fold_" in n]
    # Not vacuous: the other indexes are there.
    assert "ix_books_manual_entry" in names


def test_the_models_declare_the_trigram_indexes_for_postgres_only():
    books = Base.metadata.tables["books"]
    index = next(i for i in books.indexes if i.name == "ix_books_title_trgm")
    hook = {d: include_object_for(d) for d in ("sqlite", "postgresql")}
    assert hook["postgresql"](index, index.name, "index", False, None) is True
    assert hook["sqlite"](index, index.name, "index", False, None) is False
    # A reflected index is always kept, and an ordinary model index is kept on both.
    assert hook["sqlite"](index, index.name, "index", True, None) is True
    plain = next(i for i in books.indexes if i.name == "ix_books_manual_entry")
    assert hook["sqlite"](plain, plain.name, "index", False, None) is True


def test_the_folded_search_indexes_are_left_out_of_a_compare_on_both_sides():
    """Alembic cannot compare an expression index, so those the revisions own
    are skipped whether the model declares them or the database reflects them;
    any other index name is still compared."""
    tables = Base.metadata.tables
    for dialect in ("sqlite", "postgresql"):
        hook = include_object_for(dialect)
        for name in FOLDED_INDEX_NAMES:
            index = next(i for t in tables.values() for i in t.indexes if i.name == name)
            assert hook(index, name, "index", False, None) is False, (dialect, name)
            assert hook(index, name, "index", True, None) is False, (dialect, name)
        other = next(i for i in tables["books"].indexes if i.name == "ix_books_title_trgm")
        assert hook(other, "ix_books_title_trgm", "index", True, None) is True
        assert hook(other, "ix_books_fold_title_gin_copy", "index", True, None) is True
