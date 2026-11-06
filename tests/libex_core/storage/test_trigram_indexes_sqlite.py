"""
The trigram revision on SQLite: nothing is built and the chain still reaches
its head, and the models' Postgres-only indexes do not read as missing there.
"""

# Third party
import pytest
from sqlalchemy import text

# Local
from libex_core.storage.base import Base
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
    assert stamped == head == "b5e9c3a7f184"
    assert not [n for n in names if n.endswith("_trgm")]
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
