"""A connection is closed only by the task that made it, a write session
closes its own failed connection at once, and a failed SQLite connect leaves
no worker thread running behind it."""

# Standard library
import asyncio

# Third party
import pytest
from sqlalchemy import event, text

# Local
from libex_core.storage import store as store_module
from libex_core.storage.store import LocalStore, StoreConnectionError

# Skips the whole module when the storage extra is absent; everything below may
# use aiosqlite.
aiosqlite = pytest.importorskip("aiosqlite")


class _Conn:
    def __init__(self, name, closed):
        self.name = name
        self._closed = closed

    async def close(self):
        self._closed.append(self.name)


async def test_one_tasks_reclaim_leaves_another_tasks_pending_connection_alone():
    handoff = store_module._Handoff()
    closed = []
    mine, theirs = _Conn("mine", closed), _Conn("theirs", closed)
    held = asyncio.Event()
    release = asyncio.Event()

    async def other():
        handoff.hold(theirs)
        held.set()
        await release.wait()

    task = asyncio.ensure_future(other())
    await held.wait()
    handoff.hold(mine)
    await handoff.reclaim()
    assert closed == ["mine"]
    await handoff.reclaim(everything=True)
    assert closed == ["mine", "theirs"]
    release.set()
    await task


async def test_a_write_session_connection_that_fails_the_pool_setup_is_closed_at_once(tmp_path, monkeypatch):
    store = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    await store.upgrade()
    await store.open()
    armed = {"on": False}
    closed = []

    engine = store._engine.sync_engine

    def fail(dbapi_connection, record):
        if armed["on"]:
            raise RuntimeError("secret-password")

    # Setup that fails runs after the hold and before the confirming listener.
    event.remove(engine, "connect", store._handoff.claim)
    event.listen(engine, "connect", fail)
    event.listen(engine, "connect", store._handoff.claim)

    real_close = aiosqlite.Connection.close

    async def close(self):
        closed.append(self)
        return await real_close(self)

    monkeypatch.setattr(aiosqlite.Connection, "close", close)
    try:
        async with store.session() as first:
            await first.execute(text("SELECT 1"))
            armed["on"] = True
            with pytest.raises(Exception):
                async with store.write() as second:
                    await second.execute(text("SELECT 1"))
            assert len(closed) == 1
            armed["on"] = False
    finally:
        await store.close()


async def test_a_failed_hook_connect_leaves_no_worker_thread_running(tmp_path, monkeypatch):

    made = []
    real_init = aiosqlite.Connection.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        made.append(self)

    monkeypatch.setattr(aiosqlite.Connection, "__init__", init)

    def hook(path):
        raise OSError("secret-password")

    store = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", connect=hook)
    with pytest.raises(StoreConnectionError):
        await store.upgrade()
    await store.close()
    assert made and not any(c._thread.is_alive() for c in made)
