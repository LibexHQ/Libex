"""A connect hook's connection: closed when refused, and the interpreter exits
once the store is closed."""

# Standard library
import subprocess
import sys
import textwrap

# Third party
import pytest

# Local
from libex_core.storage.store import LocalStore, StoreConnectionError

pytest.importorskip("aiosqlite")

_SCRIPT = textwrap.dedent(
    """
    import asyncio, sys
    import aiosqlite
    from libex_core.storage.store import LocalStore

    async def hook(path):
        return await aiosqlite.connect(path)

    async def main():
        url = "sqlite+aiosqlite:///" + sys.argv[1]
        store = LocalStore(url, connect=hook)
        await store.upgrade()
        {body}

    asyncio.run(main())
    """
)


_BODIES = {
    "async_with": (
        "async with store:\n"
        "            async with store.session():\n"
        "                pass"
    ),
    "close": "await store.close()",
}


@pytest.mark.parametrize("body", _BODIES.values(), ids=_BODIES.keys())
def test_closed_hook_store_exits(tmp_path, body):
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT.format(body=body), str(tmp_path / "x.db")],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode()


class _Wrong:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _Unclosable:
    async def close(self):
        raise RuntimeError("secret-password")


async def _first_use(store):
    with pytest.raises(StoreConnectionError) as caught:
        await store.upgrade()
    return caught.value


async def test_wrong_type_sqlite_connection_is_closed(tmp_path):
    wrong = _Wrong()

    async def hook(path):
        return wrong

    store = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", connect=hook)
    await _first_use(store)
    assert wrong.closed


async def test_wrong_type_postgres_connection_is_closed():
    pytest.importorskip("asyncpg")
    wrong = _Wrong()

    async def hook():
        return wrong

    store = LocalStore("postgresql+asyncpg://", connect=hook)
    await _first_use(store)
    assert wrong.closed


async def test_failure_to_close_a_refused_connection_is_not_reported(tmp_path):
    async def hook(path):
        return _Unclosable()

    store = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", connect=hook)
    error = await _first_use(store)
    assert "secret-password" not in str(error)
    assert "RuntimeError" not in str(error)
