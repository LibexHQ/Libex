"""
Serialising writers on SQLite.

SQLite allows one writer at a time. A second one does not fail at once: it
waits out the connection's busy timeout and then gives up, so a burst of
concurrent writers queues in the driver's thread and the ones at the back can
time out while the front ones are still working. Postgres takes row locks and
needs none of this.

`exclusive_write` is the one place that is dealt with. It holds a lock per
engine for the length of the block, so writers queue in the event loop instead
of the driver, and it opens the transaction as a write transaction
(BEGIN IMMEDIATE, see `libex_core.storage.dialect.WRITE_OPTION`) so a
transaction that reads and then writes cannot find the write lock taken from
under it. On any other database it does nothing.
"""

# Standard library
import asyncio
import weakref
from contextlib import asynccontextmanager

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from libex_core.storage.dialect import WRITE_OPTION
from libex_core.storage.write.support import SQLITE, dialect_of

# One lock per (engine, running event loop). An asyncio.Lock binds to the loop
# it is first contended on and raises if used from another, so a lock shared
# by an engine that outlives its loop (asyncio.run called twice, a test per
# loop) would fail on the second. Keying by loop as well gives each loop its
# own lock; loops do not share an event loop's tasks, and SQLite's own file
# lock, taken by BEGIN IMMEDIATE, still orders writers from different loops
# or threads. Both levels are weak, so neither an engine nor a finished loop
# is kept alive by its lock.
_locks: "weakref.WeakKeyDictionary[object, weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _engine_of(session: AsyncSession):
    bind = session.get_bind()
    return getattr(bind, "engine", bind)


@asynccontextmanager
async def exclusive_write(session: AsyncSession):
    """
    Holds the engine's write lock, and opens a write transaction, for the
    length of the block.

    Wrap the whole unit of work, commit included: the lock is released when
    the block ends, and a lock released before the commit would let the next
    writer start while this one still holds the database. Enter it before the
    session has run anything -- a transaction already begun cannot be turned
    into a write transaction after the fact, and the session raises when asked
    to.
    """
    if dialect_of(session) != SQLITE:
        yield
        return

    engine = _engine_of(session)
    loop = asyncio.get_running_loop()
    by_loop = _locks.get(engine)
    if by_loop is None:
        by_loop = _locks[engine] = weakref.WeakKeyDictionary()
    lock = by_loop.get(loop)
    if lock is None:
        lock = by_loop[loop] = asyncio.Lock()
    async with lock:
        await session.connection(execution_options={WRITE_OPTION: True})
        yield
