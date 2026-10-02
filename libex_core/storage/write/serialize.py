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
from libex_core.storage.write.support import SQLITE

_locks: "weakref.WeakKeyDictionary[object, asyncio.Lock]" = weakref.WeakKeyDictionary()


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
    if session.get_bind().dialect.name != SQLITE:
        yield
        return

    engine = _engine_of(session)
    lock = _locks.get(engine)
    if lock is None:
        lock = _locks[engine] = asyncio.Lock()
    async with lock:
        await session.connection(execution_options={WRITE_OPTION: True})
        yield
