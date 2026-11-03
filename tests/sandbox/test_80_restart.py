"""
Killing, stopping and restarting things in the middle of a write. Each case
has its own ASIN, trigger and entity names, and they run one after another:
a stalled write holds a connection for 15s.
"""

# Standard library
import threading
import time

# Third party
import httpx
import pytest

ZERO = (0, 0, 0, 0, 0, 0)
FULL = (1, 1, 1, 0, 0, 0)
UP = ("up", "-d", "--wait", "--wait-timeout", "300")


def _body(sbx, asin, tag):
    return sbx.book(asin=asin, authors=[{"name": f"SBX Stall Author {tag}"}],
                    narrators=[{"name": f"SBX Stall Narrator {tag}"}])


def _stalled(sbx):
    return sbx.psql("SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = 'libex' AND wait_event = 'PgSleep'") != "0"


def _start(sbx, body):
    """Commits in a thread (paced first, from here) and returns the thread and its result list."""
    out = []
    sbx.audible("probe")

    def run():
        try:
            out.append(sbx.post(body, dry_run=False, audible=False))
        except httpx.HTTPError as e:
            out.append(e)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


def _version(sbx):
    return sbx.psql("SELECT version_num FROM alembic_version")


def _api_start(sbx):
    return sbx.compose("exec", "-T", "api", "sh", "-c", "cut -d' ' -f22 /proc/1/stat").stdout.strip()


@pytest.mark.timeout(600)
def test_api_killed_mid_write_leaves_nothing_and_comes_back(sbx):
    asin = "B0SBXSTLA0"
    body = _body(sbx, asin, "A")
    version = _version(sbx)
    with sbx.trigger("sbx_stall_a", asin, sbx.STALL_BODY):
        thread, out = _start(sbx, body)
        assert sbx.wait_for(lambda: _stalled(sbx), 30)
        killed = time.monotonic()
        sbx.compose("kill", "api")
        thread.join(60)
        assert not (isinstance(out[0], httpx.Response) and out[0].status_code == 200)
        sbx.compose(*UP, "api")
        assert sbx.wait_for(lambda: not _stalled(sbx), 40)
        assert sbx.rows_of(asin) == ZERO
        time.sleep(max(0, 90 - (time.monotonic() - killed)))
        # The api sets idle_in_transaction_session_timeout=60s on every connection.
        idle = sbx.psql("SELECT count(*) FROM pg_stat_activity WHERE datname = 'libex' "
                        "AND state LIKE 'idle in transaction%'")
        assert idle == "0"
    assert sbx.post(body, dry_run=False).status_code == 200
    assert sbx.rows_of(asin) == FULL
    assert _version(sbx) == version
    assert "(head)" in sbx.compose("exec", "-T", "api", "alembic", "current").stdout


@pytest.mark.timeout(600)
def test_db_restarted_mid_write_is_a_500_and_api_recovers_by_itself(sbx):
    asin = "B0SBXSTLB0"
    body = _body(sbx, asin, "B")
    started = _api_start(sbx)
    with sbx.trigger("sbx_stall_b", asin, sbx.STALL_BODY):
        thread, out = _start(sbx, body)
        assert sbx.wait_for(lambda: _stalled(sbx), 30)
        sbx.compose("restart", "db")
        thread.join(120)
        assert isinstance(out[0], httpx.Response)
        assert out[0].status_code == 500
        assert out[0].json() == {"error": "Internal Server Error"}
        sbx.compose(*UP, "db")
        assert sbx.rows_of(asin) == ZERO
    assert sbx.http.get("/health").status_code == 200
    assert sbx.post(body, dry_run=False).status_code == 200
    assert sbx.rows_of(asin) == FULL
    assert _api_start(sbx) == started  # api was not restarted


@pytest.mark.timeout(600)
def test_sigterm_mid_write_is_all_or_nothing(sbx):
    asin = "B0SBXSTLC0"
    body = _body(sbx, asin, "C")
    with sbx.trigger("sbx_stall_c", asin, sbx.STALL_BODY):
        thread, out = _start(sbx, body)
        assert sbx.wait_for(lambda: _stalled(sbx), 30)
        # 30s, not the default 10: the stall is 15s, and a shorter grace period would be a SIGKILL.
        sbx.compose("stop", "-t", "30", "api")
        thread.join(60)
        rows = sbx.rows_of(asin)
        assert rows in (ZERO, FULL)
        answered = isinstance(out[0], httpx.Response) and out[0].status_code == 200
        assert answered == (rows == FULL)
        sbx.compose(*UP, "api")
    again = sbx.post(body, dry_run=False)
    assert again.status_code == (409 if rows == FULL else 200)
    assert sbx.rows_of(asin) == FULL
