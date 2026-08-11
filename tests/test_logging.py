"""
Unit tests for logging configuration: the context formatter that surfaces
structured `extra` fields, the LOG_LEVEL resolution, and the Axiom handler's
off-loop delivery (queue + listener), bounded queue, and circuit breaker.
"""

# Standard library
import logging
import queue
import time
from types import SimpleNamespace

# Third party
import pytest

# Local
import app.core.logging as logging_module
from app.core.logging import (
    ContextFormatter,
    DirectAxiomHandler,
    MaxLevelFilter,
    _DroppingQueueHandler,
    _extra_fields,
    _resolve_level,
)


def _record(msg, level=logging.INFO, **extra):
    record = logging.LogRecord(
        name="libex", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_formatter_appends_extra_fields():
    fmt = ContextFormatter("%(levelname)s - %(message)s")
    out = fmt.format(_record("Scan complete", total_found=1000, new_books=0))
    assert "Scan complete" in out
    assert "total_found: 1000" in out
    assert "new_books: 0" in out


def test_formatter_clean_without_extra():
    fmt = ContextFormatter("%(levelname)s - %(message)s")
    out = fmt.format(_record("Plain message"))
    assert out == "INFO - Plain message"
    assert "(" not in out


def test_extra_fields_excludes_standard_attrs():
    record = _record("hi", found=5)
    extras = _extra_fields(record)
    assert extras == {"found": 5}
    assert "levelname" not in extras
    assert "msg" not in extras


def test_max_level_filter_allows_at_or_below():
    f = MaxLevelFilter(logging.INFO)
    assert f.filter(_record("info", level=logging.INFO)) is True
    assert f.filter(_record("debug", level=logging.DEBUG)) is True


def test_max_level_filter_blocks_above():
    f = MaxLevelFilter(logging.INFO)
    assert f.filter(_record("warn", level=logging.WARNING)) is False
    assert f.filter(_record("error", level=logging.ERROR)) is False


def test_resolve_level_uses_log_level():
    s = SimpleNamespace(debug=False, log_level="WARNING")
    assert _resolve_level(s) == logging.WARNING


def test_resolve_level_debug_mode_forces_debug():
    s = SimpleNamespace(debug=True, log_level="ERROR")
    assert _resolve_level(s) == logging.DEBUG


def test_resolve_level_is_case_insensitive():
    s = SimpleNamespace(debug=False, log_level="debug")
    assert _resolve_level(s) == logging.DEBUG


def test_resolve_level_unknown_falls_back_to_info():
    s = SimpleNamespace(debug=False, log_level="NONSENSE")
    assert _resolve_level(s) == logging.INFO


def test_resolve_level_defaults_to_info_when_empty():
    s = SimpleNamespace(debug=False, log_level="")
    assert _resolve_level(s) == logging.INFO


# ============================================================
# Axiom: off-loop delivery, bounded queue, circuit breaker
# ============================================================

class _SlowClient:
    """Stands in for axiom_py's Client: ingest_events blocks for `delay`
    seconds, the way an untimed, stalled HTTPS call would."""

    def __init__(self, delay):
        self.delay = delay
        self.calls = 0

    def ingest_events(self, dataset, events):
        self.calls += 1
        time.sleep(self.delay)


class _FailingClient:
    def __init__(self):
        self.calls = 0

    def ingest_events(self, dataset, events):
        self.calls += 1
        raise RuntimeError("simulated Axiom outage")


@pytest.fixture(autouse=True)
def _reset_libex_logger():
    """
    Isolates the shared "libex" logger and any Axiom queue listener across
    these tests. app.main attaches handlers to "libex" at import time (real
    settings, no Axiom token in CI), so tests that call setup_logging()
    directly need a clean slate rather than accumulating onto whatever is
    already attached.
    """
    logger = logging.getLogger("libex")
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    logger.handlers = []
    yield
    logging_module.stop_axiom_listener()
    for h in logger.handlers:
        h.close()
    logger.handlers = saved_handlers
    logger.level = saved_level


def _fake_settings(**overrides):
    base = dict(
        debug=False, log_level="INFO", log_retention_days=7,
        axiom_token="", axiom_dataset="libex",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_axiom_handler_emit_does_not_block_the_caller():
    client = _SlowClient(delay=0.3)
    direct_handler = DirectAxiomHandler(client=client, dataset="test")
    queue_handler = logging_module._start_axiom_listener(direct_handler)

    logger = logging.getLogger("libex.test_nonblocking")
    logger.handlers = [queue_handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    start = time.monotonic()
    logger.info("hello")
    elapsed = time.monotonic() - start

    assert elapsed < 0.1, "logging call blocked on the simulated network delay"

    # The background thread does eventually make the call.
    deadline = time.monotonic() + 2
    while client.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.calls == 1

    logger.handlers = []


def test_full_axiom_queue_drops_rather_than_blocks():
    record_queue = queue.Queue(maxsize=1)
    handler = _DroppingQueueHandler(record_queue)
    record_queue.put_nowait("placeholder")  # queue is now full

    record = logging.LogRecord(
        name="libex", level=logging.INFO, pathname=__file__, lineno=1,
        msg="dropped", args=(), exc_info=None,
    )

    start = time.monotonic()
    handler.emit(record)  # must not raise and must not block on the full queue
    elapsed = time.monotonic() - start

    assert elapsed < 0.1
    assert record_queue.qsize() == 1
    assert record_queue.get_nowait() == "placeholder"


def test_axiom_circuit_breaker_stops_calling_ingest_after_failure():
    client = _FailingClient()
    handler = DirectAxiomHandler(client=client, dataset="test")

    for i in range(3):
        record = logging.LogRecord(
            name="libex", level=logging.INFO, pathname=__file__, lineno=1,
            msg=f"record {i}", args=(), exc_info=None,
        )
        handler.emit(record)

    assert client.calls == 1, "the breaker should stop calling ingest_events after the first failure"


def test_apply_default_timeout_sets_timeout_when_caller_did_not():
    calls = []

    class _FakeSession:
        def request(self, method, url, **kwargs):
            calls.append(kwargs)

    class _FakeClient:
        def __init__(self):
            self.session = _FakeSession()

    client = _FakeClient()
    original_request = client.session.request
    logging_module._apply_default_timeout(client)

    assert client.session.request is not original_request
    client.session.request("POST", "http://example.invalid")
    assert calls[0]["timeout"] == logging_module._AXIOM_TIMEOUT_SECONDS

    client.session.request("POST", "http://example.invalid", timeout=1)
    assert calls[1]["timeout"] == 1


def test_setup_logging_without_axiom_token_skips_axiom_handler(monkeypatch):
    monkeypatch.setattr(logging_module, "get_settings", lambda: _fake_settings())

    logger = logging_module.setup_logging()

    assert not any(isinstance(h, _DroppingQueueHandler) for h in logger.handlers)
    # Logging still works with no Axiom handler attached.
    logger.info("still works with axiom disabled")


def test_setup_logging_with_axiom_token_attaches_queue_handler(monkeypatch):
    class _FakeClientClass:
        def __init__(self, token):
            self.session = None  # exercises _apply_default_timeout's no-session branch

        def ingest_events(self, dataset, events):
            return None

    monkeypatch.setattr(
        logging_module, "get_settings",
        lambda: _fake_settings(axiom_token="secret", axiom_dataset="libex"),
    )
    monkeypatch.setattr(logging_module, "AXIOM_AVAILABLE", True)
    monkeypatch.setattr(logging_module, "Client", _FakeClientClass)

    logger = logging_module.setup_logging()

    assert any(isinstance(h, _DroppingQueueHandler) for h in logger.handlers)