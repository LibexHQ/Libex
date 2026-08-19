"""
Chapter backfill classification and proxy-containment tests.

Covers two independent slices:
- Classification: _PERMANENT_UPSTREAM_STATUSES, _is_backoff_signal, and
  _process_one's (outcome, is_backoff_signal) pairs.
- Containment: _proxy_host_for_log never letting a full (possibly
  credentialed) AUDIBLE_PROXY_URL reach a log record, and
  _verify_dedicated_proxy refusing to start against anything but this
  script's own dedicated exit.

Nothing here exercises the run loop's pacing/pause machinery or the DB
queue beyond what proves the proxy check runs before either is touched --
those remain operational concerns of a one-off script, not the content of
either fix.
"""

# Standard library
import logging
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.core.exceptions import AudibleAPIException, NotFoundException
from scripts.backfill_chapters import (
    _Outcome,
    _PERMANENT_UPSTREAM_STATUSES,
    _is_backoff_signal,
    _log_exit_ip,
    _process_one,
    _proxy_host_for_log,
    _run,
    _verify_dedicated_proxy,
)


# ============================================================
# _PERMANENT_UPSTREAM_STATUSES
# ============================================================

def test_permanent_upstream_statuses_contains_only_400():
    """Limited to exactly {400} -- the only status actually observed in
    production. Anything wider is a speculative expansion of what gets
    marked dead forever without evidence."""
    assert _PERMANENT_UPSTREAM_STATUSES == {400}


# ============================================================
# _is_backoff_signal
# ============================================================

@pytest.mark.parametrize("upstream_status", [None, 401, 403, 429, 500, 503, 599])
def test_is_backoff_signal_true_for_plausible_ip_trouble(upstream_status):
    """No response at all, 401/403, 429, and every 5xx all plausibly mean
    the exit IP itself is in trouble -- these must feed the back-off window."""
    assert _is_backoff_signal(upstream_status) is True


@pytest.mark.parametrize("upstream_status", [400, 404, 405, 418])
def test_is_backoff_signal_false_for_non_ip_statuses(upstream_status):
    """400 (confirmed-permanent), 404 (confirmed-terminal), and any status
    nobody anticipated (405, 418) must NOT feed the back-off window -- only
    the explicit set above plausibly indicates IP trouble."""
    assert _is_backoff_signal(upstream_status) is False


# ============================================================
# _process_one classification
# ============================================================

@pytest.mark.asyncio
async def test_process_one_400_is_permanent_and_marked_checked_not_backoff():
    """A confirmed-permanent 400 is terminal: PERMANENT outcome, marked
    checked so it leaves the queue for good, and does NOT feed the back-off
    window. This is the exact bug this branch fixes -- these ASINs were
    previously classified ERROR, never marked, and re-selected forever."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("bad request", upstream_status=400)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff_signal = await _process_one(session, "B00TEST0400", "us")

    assert outcome == _Outcome.PERMANENT
    assert is_backoff_signal is False
    mark_checked.assert_awaited_once_with(session, "B00TEST0400")


@pytest.mark.asyncio
async def test_process_one_404_is_not_found_and_marked_checked_not_backoff():
    """A 404 stays terminal exactly as before this fix: NOT_FOUND, marked
    checked, and never a back-off signal."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=NotFoundException()),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff_signal = await _process_one(session, "B00TEST0404", "us")

    assert outcome == _Outcome.NOT_FOUND
    assert is_backoff_signal is False
    mark_checked.assert_awaited_once_with(session, "B00TEST0404")


@pytest.mark.parametrize("upstream_status", [401, 403, 429, 500, 503])
@pytest.mark.asyncio
async def test_process_one_ip_trouble_statuses_are_error_and_feed_backoff_unmarked(upstream_status):
    """401/403/429/5xx are all ERROR (retried later -- never marked checked)
    and DO feed the back-off window, since each plausibly means the exit IP
    is in trouble rather than a fact about this specific ASIN."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("upstream trouble", upstream_status=upstream_status)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff_signal = await _process_one(session, "B00TESTSTAT", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff_signal is True
    mark_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_none_upstream_status_is_error_and_feeds_backoff():
    """A timeout/connection failure arrives as AudibleAPIException with
    upstream_status None -- ERROR, unmarked, and DOES feed the back-off
    window. This is the load-bearing None case: it must not be confused
    with a confirmed-permanent per-ASIN fact."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("timed out", upstream_status=None)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff_signal = await _process_one(session, "B00TESTNONE", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff_signal is True
    mark_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_unanticipated_status_is_error_not_permanent_and_no_backoff():
    """The safety property: a status nobody anticipated (405) is ERROR (so
    it's retried, never silently marked permanently dead) but does NOT feed
    the back-off window (since it isn't one of the plausible-IP-trouble
    statuses either). An explicit permanent set means unknowns default to
    'retry', never to 'give up on this ASIN forever'."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("method not allowed", upstream_status=405)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff_signal = await _process_one(session, "B00TEST0405", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff_signal is False
    mark_checked.assert_not_awaited()


# ============================================================
# _proxy_host_for_log -- containment: no full proxy value in any log record
# ============================================================

CREDENTIALED_PROXY = "http://opsuser:s3cr3t-token@libex-backfill-vpn:8888"


def test_proxy_host_for_log_strips_credentials():
    """The hostname is what a log line needs to say which exit is in use;
    the embedded user:pass must never survive into it."""
    host = _proxy_host_for_log(CREDENTIALED_PROXY)
    assert host == "libex-backfill-vpn"
    assert "opsuser" not in host
    assert "s3cr3t-token" not in host


def test_proxy_host_for_log_direct_when_unset():
    assert _proxy_host_for_log(None) == "direct"
    assert _proxy_host_for_log("") == "direct"


def test_proxy_host_for_log_never_raises_on_malformed_value():
    """A logging call must never take the run down over an unparseable env
    var. httpx.URL raises InvalidURL on some malformed strings -- confirmed
    directly against the installed httpx: 'http://[::1' is one of them --
    so this must catch it and return a sentinel, not propagate."""
    assert _proxy_host_for_log("http://[::1") == "(unparseable)"


@pytest.mark.asyncio
async def test_log_exit_ip_never_logs_the_full_credentialed_proxy(monkeypatch, caplog):
    """The exit-IP startup probe logs 'via <host>'. Proves the credentialed
    value never reaches the record that would otherwise go to stdout and,
    with AXIOM_TOKEN set, to Axiom."""
    monkeypatch.setenv("AUDIBLE_PROXY_URL", CREDENTIALED_PROXY)

    class _FakeResponse:
        text = "203.0.113.9"

    class _FakeAsyncClient:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, *_a, **_kw):
            return _FakeResponse()

    with patch("scripts.backfill_chapters.httpx.AsyncClient", _FakeAsyncClient):
        with caplog.at_level(logging.INFO, logger="libex"):
            await _log_exit_ip()

    full_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cr3t-token" not in full_text
    assert "opsuser" not in full_text
    assert "libex-backfill-vpn" in full_text


# ============================================================
# _verify_dedicated_proxy -- containment: refuse to egress from the host
# ============================================================

def test_verify_dedicated_proxy_raises_when_unset(monkeypatch):
    """Unset means httpx.AsyncClient would get proxy=None and this script's
    traffic would egress direct from the container -- the production
    host's own address. Must refuse before anything else runs."""
    monkeypatch.delenv("AUDIBLE_PROXY_URL", raising=False)
    with pytest.raises(SystemExit, match="unset"):
        _verify_dedicated_proxy()


def test_verify_dedicated_proxy_raises_on_non_backfill_hostname(monkeypatch):
    """A hostname naming some OTHER exit -- the shared production proxy, or
    refresh_corpus's own dedicated one -- must fail exactly like unset. This
    is what stops a copy-pasted refresh exit from being reused here."""
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-refresh-vpn:8888")
    with pytest.raises(SystemExit, match="libex-refresh-vpn"):
        _verify_dedicated_proxy()


def test_verify_dedicated_proxy_failure_never_names_credentials(monkeypatch):
    """The failure message names the hostname only, proving a credentialed
    but wrongly-named value can't leak into the SystemExit text either."""
    monkeypatch.setenv(
        "AUDIBLE_PROXY_URL", "http://opsuser:s3cr3t-token@libex-refresh-vpn:8888"
    )
    with pytest.raises(SystemExit) as exc_info:
        _verify_dedicated_proxy()
    assert "s3cr3t-token" not in str(exc_info.value)
    assert "opsuser" not in str(exc_info.value)


def test_verify_dedicated_proxy_passes_on_backfill_hostname(monkeypatch):
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-backfill-vpn:8888")
    _verify_dedicated_proxy()  # must not raise


def test_verify_dedicated_proxy_logs_error_before_raising_on_unset(monkeypatch, caplog):
    """SystemExit alone never reaches the libex logger -- it propagates
    straight out of the process, so unattended it would survive only as
    stderr text. The refusal must also land as a structured ERROR record
    (rotating file handler, Axiom) before the raise, not instead of it."""
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
    """A wrongly-named but set value logs proxy_configured=True and the
    actual (safe) hostname it resolved to -- distinct from the unset case,
    and still never the raw, possibly-credentialed value."""
    monkeypatch.setenv(
        "AUDIBLE_PROXY_URL", "http://opsuser:s3cr3t-token@libex-refresh-vpn:8888"
    )
    with caplog.at_level(logging.ERROR, logger="libex"):
        with pytest.raises(SystemExit):
            _verify_dedicated_proxy()

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    record = error_records[0]
    assert record.proxy_host == "libex-refresh-vpn"
    assert record.proxy_configured is True
    full_text = record.getMessage() + " ".join(
        str(v) for v in vars(record).values() if isinstance(v, str)
    )
    assert "s3cr3t-token" not in full_text
    assert "opsuser" not in full_text


def test_verify_dedicated_proxy_logs_error_on_malformed_value(monkeypatch, caplog):
    """A value that is set but unparseable (a typo'd port is the realistic
    case -- a Portainer env field is free text) must take the exact same
    logged-then-refused path as unset or wrongly-named, not escape as an
    uncaught httpx.InvalidURL that skips both the log line and the
    deliberate SystemExit message. Live-reproduced by the security review
    against pinned httpx 0.28.1: 'notaport' raises InvalidURL."""
    monkeypatch.setenv(
        "AUDIBLE_PROXY_URL",
        "http://opsuser:s3cr3t-token@libex-backfill-vpn:notaport",
    )
    with caplog.at_level(logging.ERROR, logger="libex"):
        with pytest.raises(SystemExit) as exc_info:
            _verify_dedicated_proxy()

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    record = error_records[0]
    assert record.proxy_host == "(unparseable)"
    assert record.proxy_configured is True
    full_text = record.getMessage() + " ".join(
        str(v) for v in vars(record).values() if isinstance(v, str)
    ) + str(exc_info.value)
    assert "s3cr3t-token" not in full_text
    assert "opsuser" not in full_text


@pytest.mark.asyncio
async def test_run_dies_before_touching_the_db_when_proxy_malformed(monkeypatch):
    """The malformed case must die in _run before the DB engine exists,
    exactly like the unset and wrongly-named cases -- not merely log
    correctly and then still blow up somewhere else uncaught."""
    monkeypatch.setenv(
        "AUDIBLE_PROXY_URL",
        "http://opsuser:s3cr3t-token@libex-backfill-vpn:notaport",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("scripts.backfill_chapters.create_async_engine") as create_engine:
        with pytest.raises(SystemExit):
            await _run(limit=1)
    create_engine.assert_not_called()


def test_verify_dedicated_proxy_logs_nothing_at_error_when_correctly_named(monkeypatch, caplog):
    """The success path must not also emit the refusal record -- proves the
    new logger.error call is gated on the same condition as the raise, not
    unconditional."""
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-backfill-vpn:8888")
    with caplog.at_level(logging.ERROR, logger="libex"):
        _verify_dedicated_proxy()
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


# ============================================================
# _run -- the check fires before a single request or DB touch
# ============================================================

@pytest.mark.asyncio
async def test_run_dies_before_touching_the_db_when_proxy_unset(monkeypatch):
    """Proves the guard runs first in startup, not merely somewhere: with
    DATABASE_URL also absent, an unset proxy must still surface as the
    proxy's own SystemExit, never as a KeyError from reading DATABASE_URL,
    and create_async_engine must never be reached at all -- exactly the
    'dies before a single request goes out' requirement."""
    monkeypatch.delenv("AUDIBLE_PROXY_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("scripts.backfill_chapters.create_async_engine") as create_engine:
        with pytest.raises(SystemExit):
            await _run(limit=1)
    create_engine.assert_not_called()


@pytest.mark.asyncio
async def test_run_proceeds_past_the_check_when_correctly_named(monkeypatch):
    """A correctly-named proxy lets the run past the guard -- proven by the
    failure moving on to the next real requirement (DATABASE_URL) instead of
    the proxy check itself. This is also the --limit trial path: a trial
    still calls Audible for real, so it must clear the same guard as the
    unlimited run, with no bypass."""
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-backfill-vpn:8888")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("scripts.backfill_chapters._log_exit_ip", new=AsyncMock()):
        with pytest.raises(KeyError, match="DATABASE_URL"):
            await _run(limit=1)
