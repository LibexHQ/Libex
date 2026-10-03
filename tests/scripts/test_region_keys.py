"""
scripts/region_keys.py entry point and guards that need no database.

What only the entry point can get wrong: setup_logging running before
anything else (a standalone script emits nothing without it), finalize and
unfinalize refusing to start without their maintenance-window flag, and the script refusing
any backend but Postgres before it opens a connection. The behaviour against
a real schema lives in tests/integration/test_region_keys.py.
"""

# Standard library
import asyncio
import contextlib
import time
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


async def test_retry_retries_a_deadlock(monkeypatch):
    monkeypatch.setattr(rk, "BACKOFF_BASE_SECONDS", 0)
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _dbapi_error("40P01")
        return "done"

    assert await rk._retry("x", op, rk._Stop()) == "done"
    assert calls["n"] == 2


async def test_a_stop_during_backoff_raises_stop_requested_not_the_db_error(monkeypatch):
    monkeypatch.setattr(rk, "BACKOFF_BASE_SECONDS", 0)
    stop = rk._Stop()
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        stop.request()
        raise _dbapi_error("55P03")

    with pytest.raises(rk.StopRequested):
        await rk._retry("x", op, stop)
    assert calls["n"] == 1


async def test_a_stop_wakes_a_long_backoff_early(monkeypatch):
    monkeypatch.setattr(rk, "BACKOFF_BASE_SECONDS", 30)
    monkeypatch.setattr(rk, "STOP_POLL_SECONDS", 0.01)
    stop = rk._Stop()

    async def op():
        asyncio.get_running_loop().call_later(0.05, stop.request)
        raise _dbapi_error("55P03")

    started = time.monotonic()
    with pytest.raises(rk.StopRequested):
        await rk._retry("x", op, stop)
    assert time.monotonic() - started < 2


async def test_a_failed_unlock_is_a_warning_not_a_failed_run(monkeypatch):
    class Conn:
        async def execution_options(self, **kw):
            return self

        async def execute(self, stmt, params=None):
            if "unlock" in str(stmt):
                raise _dbapi_error("08006")
            return SimpleNamespace(scalar_one=lambda: True)

    class Ctx:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *a):
            return False

    eng = MagicMock()
    eng.connect = lambda: Ctx()
    warned: list[str] = []
    monkeypatch.setattr(rk.logger, "warning", lambda msg, *a, **k: warned.append(msg))
    async with rk._single_run_lock(eng):
        pass
    assert warned == ["RegionKeys: could not release the run lock"]


def _args(mode: str):
    return SimpleNamespace(mode=mode, batch_size=1, pre_window=False)


def _no_lock(monkeypatch):
    @contextlib.asynccontextmanager
    async def free(engine):
        yield

    monkeypatch.setattr(rk, "_single_run_lock", free)


@pytest.fixture
def engine():
    eng = MagicMock()
    eng.dispose = AsyncMock()
    return eng


async def test_run_exits_stopped_when_a_stop_lands_during_backoff(monkeypatch, engine):
    _no_lock(monkeypatch)
    monkeypatch.setattr(rk, "expand", AsyncMock(side_effect=rk.StopRequested("add x")))
    assert await rk._run(_args("expand"), engine) == rk.EXIT_STOPPED
    engine.dispose.assert_awaited()


async def test_run_logs_type_and_sqlstate_never_the_error_text(monkeypatch, engine):
    _no_lock(monkeypatch)
    secret = "Key (asin)=(B0SECRET01) already exists"
    err = DBAPIError("stmt", None, _FakeOrig("23505"))
    err.args = (secret,)
    monkeypatch.setattr(rk, "backfill", AsyncMock(side_effect=err))
    logged: list[dict] = []
    monkeypatch.setattr(rk.logger, "error", lambda msg, *a, extra=None, **k: logged.append({"msg": msg, **(extra or {})}))
    assert await rk._run(_args("backfill"), engine) == rk.EXIT_FAILED
    (entry,) = logged
    assert entry["error_type"] == "DBAPIError"
    assert entry["sqlstate"] == "23505"
    assert "error" not in entry
    assert secret not in repr(entry)


async def test_run_refuses_when_another_run_holds_the_lock(monkeypatch, engine):
    @contextlib.asynccontextmanager
    async def held(engine):
        raise rk.AlreadyRunning()
        yield  # pragma: no cover

    monkeypatch.setattr(rk, "_single_run_lock", held)
    expand = AsyncMock()
    monkeypatch.setattr(rk, "expand", expand)
    assert await rk._run(_args("expand"), engine) == rk.EXIT_FAILED
    expand.assert_not_called()


async def test_verify_takes_no_lock(monkeypatch, engine):
    @contextlib.asynccontextmanager
    async def boom(engine):
        raise AssertionError("verify must not take the run lock")
        yield  # pragma: no cover

    monkeypatch.setattr(rk, "_single_run_lock", boom)
    monkeypatch.setattr(rk, "verify", AsyncMock(return_value=rk.EXIT_OK))
    assert await rk._run(_args("verify"), engine) == rk.EXIT_OK


def test_unfinalize_refuses_without_the_writers_stopped_flag(monkeypatch):
    monkeypatch.setattr(rk, "setup_logging", lambda: None)
    run = AsyncMock()
    monkeypatch.setattr(rk, "_run", run)
    with pytest.raises(SystemExit) as exc:
        rk.main(["unfinalize"])
    assert exc.value.code == rk.EXIT_USAGE
    run.assert_not_called()


def test_the_two_books_and_series_indexes_are_the_window_indexes():
    assert {s.name for s in rk.WINDOW_INDEXES} == {"uq_books_asin_region", "uq_series_asin_region"}
    assert {s.name for s in rk.ONLINE_INDEXES} | {s.name for s in rk.WINDOW_INDEXES} == {s.name for s in rk.INDEXES}


async def test_retry_is_bounded_on_a_persistent_lock_wait(monkeypatch):
    monkeypatch.setattr(rk, "BACKOFF_BASE_SECONDS", 0)
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise _dbapi_error("55P03")

    with pytest.raises(DBAPIError):
        await rk._retry("x", op, rk._Stop())
    assert calls["n"] == rk.LOCK_ATTEMPTS
