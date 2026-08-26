"""
Standalone seeder entry point: proxy containment, shutdown ordering, exit
codes, and CLI wiring.

scripts/seed.py runs unattended in its own container with nobody watching it
live -- a bad proxy check lets its traffic egress from the live service's own
address, a swapped drain/dispose order silently abandons in-flight persist
writes, and a wrong exit code hides "a worker broke" behind "finished clean"
from whatever supervises the container. Scoped to what is pure and testable
without a live Audible/Postgres pair: _proxy_host_for_log and
_verify_dedicated_proxy (mirroring scripts/backfill_chapters.py and
scripts/refresh_corpus.py's own tests for the identical pair, adjusted for
this script's own "seeder" hostname convention), _env_float, _Stopper's
cancel-on-request behavior, _drain_persist_queue's timeout/success return, and
_run's ordering and exit-code logic with both workers and the drain/dispose
calls replaced by fakes. The dispatch loops themselves (run_seeder,
run_new_releases_seeder) are app/services/seeder.py's own content and covered
there, not re-tested here.
"""

# Standard library
import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from scripts.seed import (
    DRAIN_TIMEOUT_SECONDS,
    _drain_persist_queue,
    _env_float,
    _proxy_host_for_log,
    _run,
    _Stopper,
    _verify_dedicated_proxy,
    main,
)
import scripts.seed as seed


# ============================================================
# _env_float
# ============================================================

def test_env_float_reads_a_set_value(monkeypatch):
    monkeypatch.setenv("SOME_TIMEOUT", "12.5")
    assert _env_float("SOME_TIMEOUT", 300.0) == 12.5


def test_env_float_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_TIMEOUT", raising=False)
    assert _env_float("SOME_TIMEOUT", 300.0) == 300.0


def test_env_float_falls_back_to_default_on_unparseable_value(monkeypatch):
    """A Portainer env field is free text -- a typo must not crash the
    module import, it must fall back exactly like unset."""
    monkeypatch.setenv("SOME_TIMEOUT", "not-a-number")
    assert _env_float("SOME_TIMEOUT", 300.0) == 300.0


def test_drain_timeout_seconds_default_matches_the_documented_value():
    """Pins the module-level constant's default to the value the module
    docstring's ENVIRONMENT section documents (300.0) -- this constant is
    computed once at import time from SEEDER_DRAIN_TIMEOUT_SECONDS, so this
    is the only way to check the default without reloading the module."""
    assert DRAIN_TIMEOUT_SECONDS == 300.0


# ============================================================
# _proxy_host_for_log -- containment: no full proxy value in any log record
# ============================================================

CREDENTIALED_PROXY = "http://opsuser:s3cr3t-token@libex-seeder-vpn:8888"


def test_proxy_host_for_log_strips_credentials():
    host = _proxy_host_for_log(CREDENTIALED_PROXY)
    assert host == "libex-seeder-vpn"
    assert "opsuser" not in host
    assert "s3cr3t-token" not in host


def test_proxy_host_for_log_direct_when_unset():
    assert _proxy_host_for_log(None) == "direct"
    assert _proxy_host_for_log("") == "direct"


def test_proxy_host_for_log_never_raises_on_malformed_value():
    """httpx.URL raises InvalidURL on some malformed strings -- confirmed
    directly against the installed httpx: 'http://[::1' is one of them -- so
    this must catch it and return a sentinel, not propagate."""
    assert _proxy_host_for_log("http://[::1") == "(unparseable)"


# ============================================================
# _verify_dedicated_proxy -- containment: refuse to egress from the host
# ============================================================

def test_verify_dedicated_proxy_raises_when_unset(monkeypatch):
    monkeypatch.delenv("AUDIBLE_PROXY_URL", raising=False)
    with pytest.raises(SystemExit, match="unset"):
        _verify_dedicated_proxy()


def test_verify_dedicated_proxy_raises_on_non_seeder_hostname(monkeypatch):
    """A hostname naming some OTHER exit -- the shared production proxy, or
    backfill_chapters's/refresh_corpus's own dedicated ones -- must fail
    exactly like unset. This is what stops a copy-pasted exit from being
    reused here."""
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-backfill-vpn:8888")
    with pytest.raises(SystemExit, match="libex-backfill-vpn"):
        _verify_dedicated_proxy()


def test_verify_dedicated_proxy_failure_never_names_credentials(monkeypatch):
    monkeypatch.setenv(
        "AUDIBLE_PROXY_URL", "http://opsuser:s3cr3t-token@libex-backfill-vpn:8888"
    )
    with pytest.raises(SystemExit) as exc_info:
        _verify_dedicated_proxy()
    assert "s3cr3t-token" not in str(exc_info.value)
    assert "opsuser" not in str(exc_info.value)


def test_verify_dedicated_proxy_passes_on_seeder_hostname(monkeypatch):
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-seeder-vpn:8888")
    _verify_dedicated_proxy()  # must not raise


def test_verify_dedicated_proxy_logs_error_before_raising_on_unset(monkeypatch, caplog):
    monkeypatch.delenv("AUDIBLE_PROXY_URL", raising=False)
    with caplog.at_level(logging.ERROR, logger="libex"):
        with pytest.raises(SystemExit):
            _verify_dedicated_proxy()

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    record = error_records[0]
    assert "AUDIBLE_PROXY_URL" in record.getMessage()
    assert record.proxy_host == "unset"
    assert record.proxy_configured is False


def test_verify_dedicated_proxy_logs_error_with_hostname_when_wrongly_named(monkeypatch, caplog):
    monkeypatch.setenv(
        "AUDIBLE_PROXY_URL", "http://opsuser:s3cr3t-token@libex-backfill-vpn:8888"
    )
    with caplog.at_level(logging.ERROR, logger="libex"):
        with pytest.raises(SystemExit):
            _verify_dedicated_proxy()

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    record = error_records[0]
    assert record.proxy_host == "libex-backfill-vpn"
    assert record.proxy_configured is True
    full_text = record.getMessage() + " ".join(
        str(v) for v in vars(record).values() if isinstance(v, str)
    )
    assert "s3cr3t-token" not in full_text
    assert "opsuser" not in full_text


def test_verify_dedicated_proxy_logs_nothing_at_error_when_correctly_named(monkeypatch, caplog):
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-seeder-vpn:8888")
    with caplog.at_level(logging.ERROR, logger="libex"):
        _verify_dedicated_proxy()
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


# ============================================================
# _Stopper -- SIGTERM/SIGINT cancel every tracked task, once, idempotently
# ============================================================

def test_stopper_cancels_every_tracked_task_not_already_done():
    stopper = _Stopper()
    live_task = MagicMock()
    live_task.done.return_value = False
    stopper.track(live_task)

    stopper.request()

    live_task.cancel.assert_called_once()
    assert stopper.requested is True


def test_stopper_does_not_cancel_a_task_already_done():
    """A task that finished on its own (the --once path) must not be handed
    a spurious cancel()."""
    stopper = _Stopper()
    done_task = MagicMock()
    done_task.done.return_value = True
    stopper.track(done_task)

    stopper.request()

    done_task.cancel.assert_not_called()


def test_stopper_second_request_logs_nothing_further(caplog):
    """Idempotent: a second signal while the first is still being handled
    must not log the stop-requested line again."""
    stopper = _Stopper()
    task = MagicMock()
    task.done.return_value = False
    stopper.track(task)

    with caplog.at_level(logging.INFO, logger="libex"):
        stopper.request()
        stopper.request()

    stop_lines = [r for r in caplog.records if "stop requested" in r.getMessage()]
    assert len(stop_lines) == 1


# ============================================================
# _drain_persist_queue -- bounded wait for persist_queue to empty
# ============================================================

@pytest.mark.asyncio
async def test_drain_persist_queue_returns_true_once_the_backlog_empties():
    calls = {"n": 0}

    def _queued_books():
        calls["n"] += 1
        return 0 if calls["n"] > 1 else 3

    with patch("scripts.seed.persist_queue.queued_books", side_effect=_queued_books), \
         patch("scripts.seed.asyncio.sleep", new=AsyncMock()):
        drained = await _drain_persist_queue(timeout=10.0)

    assert drained is True


@pytest.mark.asyncio
async def test_drain_persist_queue_returns_false_past_the_timeout():
    """A backlog that never empties must not hang the shutdown forever --
    bounded by `timeout`, reported as not drained rather than blocking."""
    times = iter([0.0, 0.0, 100.0])  # started, first check, past the 10s timeout

    with patch("scripts.seed.persist_queue.queued_books", return_value=5), \
         patch("scripts.seed.time.monotonic", side_effect=lambda: next(times)), \
         patch("scripts.seed.asyncio.sleep", new=AsyncMock()):
        drained = await _drain_persist_queue(timeout=10.0)

    assert drained is False


@pytest.mark.asyncio
async def test_drain_persist_queue_returns_true_immediately_when_already_empty():
    with patch("scripts.seed.persist_queue.queued_books", return_value=0), \
         patch("scripts.seed.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        drained = await _drain_persist_queue(timeout=10.0)

    assert drained is True
    sleep_mock.assert_not_awaited()


# ============================================================
# _run -- drain-before-dispose ordering, --once threading, exit codes
# ============================================================

def _patched_run(monkeypatch, *, seeder=None, releases=None, drained=True):
    """Sets a valid seeder proxy and patches both workers, the drain, and the
    engine so `_run` can be exercised without asyncio.gather touching real
    coroutines or a real database engine."""
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-seeder-vpn:8888")
    seeder = seeder or AsyncMock(return_value=None)
    releases = releases or AsyncMock(return_value=None)
    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()
    return patch.multiple(
        seed,
        run_seeder=seeder,
        run_new_releases_seeder=releases,
        _drain_persist_queue=AsyncMock(return_value=drained),
        engine=fake_engine,
    )


@pytest.mark.asyncio
async def test_run_drains_the_persist_queue_before_disposing_the_engine(monkeypatch):
    """Ordering is the point: a drain that runs AFTER dispose would abandon
    whatever persist_queue's fire-and-forget tasks were still writing through
    a connection nothing is driving anymore."""
    order = []
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-seeder-vpn:8888")

    async def _fake_drain(_timeout):
        order.append("drain")
        return True

    fake_engine = MagicMock()

    async def _fake_dispose():
        order.append("dispose")

    fake_engine.dispose = _fake_dispose

    with patch.multiple(
        seed,
        run_seeder=AsyncMock(return_value=None),
        run_new_releases_seeder=AsyncMock(return_value=None),
        _drain_persist_queue=_fake_drain,
        engine=fake_engine,
    ):
        exit_code = await _run(once=True)

    assert order == ["drain", "dispose"]
    assert exit_code == 0


@pytest.mark.asyncio
async def test_run_passes_once_through_to_both_workers(monkeypatch):
    seeder = AsyncMock(return_value=None)
    releases = AsyncMock(return_value=None)
    with _patched_run(monkeypatch, seeder=seeder, releases=releases):
        await _run(once=True)

    seeder.assert_awaited_once_with(once=True)
    releases.assert_awaited_once_with(once=True)


@pytest.mark.asyncio
async def test_run_passes_once_false_through_when_not_requested(monkeypatch):
    seeder = AsyncMock(return_value=None)
    releases = AsyncMock(return_value=None)
    with _patched_run(monkeypatch, seeder=seeder, releases=releases):
        await _run(once=False)

    seeder.assert_awaited_once_with(once=False)
    releases.assert_awaited_once_with(once=False)


@pytest.mark.asyncio
async def test_run_returns_0_when_clean_and_drained(monkeypatch):
    with _patched_run(monkeypatch, drained=True):
        exit_code = await _run(once=True)
    assert exit_code == 0


@pytest.mark.asyncio
async def test_run_returns_1_when_a_worker_raises(monkeypatch):
    async def _boom(once):
        raise RuntimeError("boom")

    with _patched_run(monkeypatch, seeder=_boom, drained=True):
        exit_code = await _run(once=True)

    assert exit_code == 1


@pytest.mark.asyncio
async def test_run_returns_2_when_drain_times_out_with_no_worker_failures(monkeypatch):
    with _patched_run(monkeypatch, drained=False):
        exit_code = await _run(once=True)
    assert exit_code == 2


@pytest.mark.asyncio
async def test_run_failure_exit_code_takes_priority_over_a_drain_timeout(monkeypatch):
    """Both a worker failure and an undrained queue at once must report 1,
    not 2 -- a supervisor needs to know "a worker broke" even if the queue
    also failed to drain, not have that fact hidden behind the drain's own
    weaker signal."""
    async def _boom(once):
        raise RuntimeError("boom")

    with _patched_run(monkeypatch, seeder=_boom, drained=False):
        exit_code = await _run(once=True)

    assert exit_code == 1


@pytest.mark.asyncio
async def test_run_still_drains_and_disposes_when_a_worker_raises(monkeypatch):
    """A worker exception must not skip the shutdown sequence -- the drain
    and dispose still have to run so nothing gets abandoned just because one
    of the two loops broke."""
    async def _boom(once):
        raise RuntimeError("boom")

    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()
    drain = AsyncMock(return_value=True)
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-seeder-vpn:8888")

    with patch.multiple(
        seed,
        run_seeder=_boom,
        run_new_releases_seeder=AsyncMock(return_value=None),
        _drain_persist_queue=drain,
        engine=fake_engine,
    ):
        await _run(once=True)

    drain.assert_awaited_once()
    fake_engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_dies_before_starting_any_worker_when_proxy_unset(monkeypatch):
    """The proxy check is the first thing _run does -- neither worker may
    ever be started against an unverified exit."""
    monkeypatch.delenv("AUDIBLE_PROXY_URL", raising=False)
    seeder = AsyncMock(return_value=None)
    releases = AsyncMock(return_value=None)

    with patch.multiple(seed, run_seeder=seeder, run_new_releases_seeder=releases):
        with pytest.raises(SystemExit):
            await _run(once=True)

    seeder.assert_not_awaited()
    releases.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_creates_and_tracks_both_tasks_before_registering_signal_handlers(monkeypatch):
    """Ordering is the point, not mere presence: a test asserting only that
    both tasks end up tracked and both handlers end up registered would pass
    under the old, broken order too (register handlers -> create tasks ->
    track). A signal landing in that old window called stopper.request()
    against an empty task list -- requested=True got set, but there was
    nothing yet to cancel, and the tasks created afterward were never told."""
    order = []
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-seeder-vpn:8888")

    real_create_task = asyncio.create_task

    def _tracking_create_task(coro, **kwargs):
        order.append("create_task")
        return real_create_task(coro, **kwargs)

    real_track = _Stopper.track

    def _tracking_track(self, task):
        order.append("track")
        return real_track(self, task)

    def _tracking_signal(sig, handler):
        order.append("signal.signal")

    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()

    with patch.multiple(
        seed,
        run_seeder=AsyncMock(return_value=None),
        run_new_releases_seeder=AsyncMock(return_value=None),
        _drain_persist_queue=AsyncMock(return_value=True),
        engine=fake_engine,
    ), \
         patch("scripts.seed.asyncio.create_task", side_effect=_tracking_create_task), \
         patch.object(_Stopper, "track", _tracking_track), \
         patch("scripts.seed.signal.signal", side_effect=_tracking_signal):
        exit_code = await _run(once=True)

    assert order == [
        "create_task", "create_task", "track", "track",
        "signal.signal", "signal.signal",
    ]
    assert exit_code == 0


# ============================================================
# main -- setup_logging ordering, --once wiring, exit-code propagation
# ============================================================

def test_main_calls_setup_logging_before_running(monkeypatch):
    order = []
    monkeypatch.setattr(sys, "argv", ["seed.py"])

    def _fake_run(_once):
        order.append("_run-created")
        return "sentinel-coro"

    def _fake_asyncio_run(coro):
        order.append("asyncio.run")
        assert coro == "sentinel-coro"
        return 0

    with patch("scripts.seed.setup_logging", side_effect=lambda: order.append("setup_logging")), \
         patch("scripts.seed._run", new=MagicMock(side_effect=_fake_run)), \
         patch("scripts.seed.asyncio.run", side_effect=_fake_asyncio_run):
        main()

    assert order == ["setup_logging", "_run-created", "asyncio.run"]


def test_main_calls_check_retired_env_vars_after_logging_and_before_the_run(monkeypatch):
    """Pins the new call's exact position, not merely its presence: it must
    land after setup_logging() -- a warning logged before handlers are
    attached goes nowhere -- and before the run starts, so a stale
    SEEDER_ENABLED pasted into this container's environment is actually
    reported somewhere."""
    order = []
    monkeypatch.setattr(sys, "argv", ["seed.py"])

    def _fake_run(_once):
        order.append("_run-created")
        return "sentinel-coro"

    def _fake_asyncio_run(coro):
        order.append("asyncio.run")
        assert coro == "sentinel-coro"
        return 0

    with patch("scripts.seed.setup_logging", side_effect=lambda: order.append("setup_logging")), \
         patch(
             "scripts.seed.check_retired_env_vars",
             side_effect=lambda: order.append("check_retired_env_vars"),
         ), \
         patch("scripts.seed._run", new=MagicMock(side_effect=_fake_run)), \
         patch("scripts.seed.asyncio.run", side_effect=_fake_asyncio_run):
        main()

    assert order == [
        "setup_logging", "check_retired_env_vars", "_run-created", "asyncio.run",
    ]


def test_main_defaults_once_to_false(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["seed.py"])
    run_mock = MagicMock(return_value="sentinel-coro")

    with patch("scripts.seed.setup_logging"), \
         patch("scripts.seed._run", run_mock), \
         patch("scripts.seed.asyncio.run", return_value=0):
        main()

    run_mock.assert_called_once_with(False)


def test_main_passes_once_true_when_the_flag_is_given(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["seed.py", "--once"])
    run_mock = MagicMock(return_value="sentinel-coro")

    with patch("scripts.seed.setup_logging"), \
         patch("scripts.seed._run", run_mock), \
         patch("scripts.seed.asyncio.run", return_value=0):
        main()

    run_mock.assert_called_once_with(True)


def test_main_raises_systemexit_with_the_run_exit_code(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["seed.py"])

    with patch("scripts.seed.setup_logging"), \
         patch("scripts.seed._run", new=MagicMock(return_value="sentinel-coro")), \
         patch("scripts.seed.asyncio.run", return_value=1):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1


def test_main_does_not_raise_on_a_clean_exit_code(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["seed.py"])

    with patch("scripts.seed.setup_logging"), \
         patch("scripts.seed._run", new=MagicMock(return_value="sentinel-coro")), \
         patch("scripts.seed.asyncio.run", return_value=0):
        main()  # must not raise
