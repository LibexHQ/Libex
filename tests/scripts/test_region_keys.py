"""
scripts/region_keys.py entry point and guards that need no database.

What only the entry point can get wrong: setup_logging running before
anything else (a standalone script emits nothing without it), finalize
refusing to start without its maintenance-window flag, and the script refusing
any backend but Postgres before it opens a connection. The behaviour against
a real schema lives in tests/integration/test_region_keys.py.
"""

# Standard library
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Third party
import pytest
from sqlalchemy.exc import DBAPIError

# Local
import scripts.region_keys as rk


def _settings(url: str):
    return SimpleNamespace(database_url=url)


def test_setup_logging_runs_before_anything_else(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(rk, "setup_logging", lambda: order.append("logging"))
    monkeypatch.setattr(rk, "get_settings", lambda: (order.append("settings"), _settings("postgresql+asyncpg://x/y"))[1])
    monkeypatch.setattr(rk, "_make_engine", lambda url: MagicMock())

    async def fake_run(args, engine):
        order.append("run")
        return 0

    monkeypatch.setattr(rk, "_run", fake_run)
    rk.main(["verify"])
    assert order == ["logging", "settings", "run"]


def test_finalize_refuses_without_the_writers_stopped_flag(monkeypatch):
    monkeypatch.setattr(rk, "setup_logging", lambda: None)
    run = AsyncMock()
    monkeypatch.setattr(rk, "_run", run)
    with pytest.raises(SystemExit) as exc:
        rk.main(["finalize"])
    assert exc.value.code == rk.EXIT_USAGE
    run.assert_not_called()


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///x.db", "mysql+aiomysql://u@h/d"])
def test_refuses_a_non_postgres_backend_before_connecting(monkeypatch, url):
    monkeypatch.setattr(rk, "setup_logging", lambda: None)
    monkeypatch.setattr(rk, "get_settings", lambda: _settings(url))
    make_engine = MagicMock()
    monkeypatch.setattr(rk, "_make_engine", make_engine)
    with pytest.raises(SystemExit) as exc:
        rk.main(["expand"])
    assert exc.value.code == rk.EXIT_USAGE
    make_engine.assert_not_called()


def test_a_non_positive_batch_size_is_a_usage_error(monkeypatch):
    monkeypatch.setattr(rk, "setup_logging", lambda: None)
    with pytest.raises(SystemExit) as exc:
        rk.main(["backfill", "--batch-size", "0"])
    assert exc.value.code == 2


def test_nonzero_result_becomes_the_exit_code(monkeypatch):
    monkeypatch.setattr(rk, "setup_logging", lambda: None)
    monkeypatch.setattr(rk, "get_settings", lambda: _settings("postgresql+asyncpg://x/y"))
    monkeypatch.setattr(rk, "_make_engine", lambda url: MagicMock())
    monkeypatch.setattr(rk, "_run", AsyncMock(return_value=rk.EXIT_STOPPED))
    with pytest.raises(SystemExit) as exc:
        rk.main(["backfill"])
    assert exc.value.code == rk.EXIT_STOPPED


def test_index_names_are_unique_and_every_column_is_known():
    names = [spec.name for spec in rk.INDEXES]
    assert len(names) == len(set(names))
    added = {(c.table, c.column) for c in rk.REGION_COLUMNS}
    for spec in rk.INDEXES:
        for column in spec.columns:
            if column.endswith("region") and column not in ("region",):
                assert (spec.table, column) in added, (spec.name, column)


class _FakeOrig(Exception):
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate


def _dbapi_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("stmt", None, _FakeOrig(sqlstate))


async def test_retry_gives_up_on_a_non_lock_error_at_once(monkeypatch):
    monkeypatch.setattr(rk, "BACKOFF_BASE_SECONDS", 0)
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise _dbapi_error("42P01")

    with pytest.raises(DBAPIError):
        await rk._retry("x", op, rk._Stop())
    assert calls["n"] == 1


async def test_retry_is_bounded_on_a_persistent_lock_wait(monkeypatch):
    monkeypatch.setattr(rk, "BACKOFF_BASE_SECONDS", 0)
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise _dbapi_error("55P03")

    with pytest.raises(DBAPIError):
        await rk._retry("x", op, rk._Stop())
    assert calls["n"] == rk.LOCK_ATTEMPTS
