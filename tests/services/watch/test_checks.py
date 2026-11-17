"""
Tests for the operational checks: every threshold on both sides, a threshold of
0 turning its check off, "could not check" kept apart from "stale", one alert
per incident with exactly one "cleared", the restart-loop line matcher against
the real log formatter, the database access rules, and that no
secret or log line reaches a log record or an alert.

Seams: the notifier is a recorder (or a real Notifier over MockTransport where
the wire matters), HTTP is httpx.MockTransport, the engine is patched at
checks' import location, and the clock and statvfs are patched. No network and
no database.
"""

# Standard library
import asyncio
import inspect
import json
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace

# Third party
import httpx
import pytest
from sqlalchemy.exc import OperationalError

# Core
from app.core.config import get_settings
from app.core.logging import ContextFormatter, setup_logging

# Services
import app.services.watch as watch_pkg
import app.services.watch.checks as checks_mod
from app.services.watch.checks import (
    Alerts,
    OpsChecks,
    OpsConfig,
    SQL_STATEMENTS,
    boot_count,
    boots_in_logs,
    log_decision,
    disk_percent,
    load_ops_config,
    run_ops,
)
from app.services.watch.gotify import Notifier
from app.services.watch.heartbeat import Heartbeat

# Scripts
import scripts.stack_watch as stack_watch

VERSION = get_settings().app_version
HOUR = 3600


class Rec:
    """A notifier that records instead of sending."""

    def __init__(self):
        self.sent = []

    async def send(self, what, happened, did, needs_you):
        self.sent.append((what, happened, did, needs_you))
        return True

    @property
    def titles(self):
        return [s[0] for s in self.sent]


def make(**kw):
    rec = Rec()
    return OpsChecks(OpsConfig(**kw), rec, "api"), rec


def client(handler, base="http://api"):
    return httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(handler))


# --- config: a threshold of 0 turns its check off ------------------------------

def test_defaults_are_everything_off():
    cfg = load_ops_config({})
    assert not cfg.any_on
    assert not any(cfg.states().values())


def test_defaults_carry_the_documented_thresholds():
    cfg = load_ops_config({})
    assert (cfg.backup_max_age_h, cfg.stats_zero_s, cfg.health_fails) == (26.0, 1800.0, 3)
    assert (cfg.disk_pct, cfg.dead_tuple_pct, cfg.vacuum_max_age_h, cfg.vacuum_run_h, cfg.db_fails) == (85.0, 20.0, 24.0, 6.0, 3)
    assert (cfg.restart_boots, cfg.restart_window_s, cfg.heartbeat_dead_s, cfg.interval_s) == (2, 1800.0, 300.0, 300.0)


def test_restart_boots_default_scales_with_web_concurrency():
    assert load_ops_config({"WEB_CONCURRENCY": "4"}).restart_boots == 8
    assert load_ops_config({"WEB_CONCURRENCY": "4", "WATCH_RESTART_BOOTS": "5"}).restart_boots == 5


@pytest.mark.parametrize("env,key", [
    ({"BACKUP_SPOOL_DIR": "/s"}, "backup_stale"),
    ({"WATCH_LIBEX_URL": "http://a"}, "stats_badges"),
    ({"WATCH_LIBEX_URL": "http://a"}, "api_health"),
    ({"WATCH_LOG_DIR": "/l"}, "restart_loop"),
    ({"WATCH_HEARTBEAT_NAME": "seeder"}, "worker_heartbeat"),
    ({"WATCH_DISK_PATH": "/d"}, "db_disk"),
    ({"WATCH_DATABASE_URL": "postgresql://u:p@h/d"}, "books_vacuum"),
])
def test_each_check_turns_on_with_its_own_env_and_only_that_one(env, key):
    states = load_ops_config(env).states()
    assert states[key] is True
    assert [k for k, on in states.items() if on and k != key and k not in ("stats_badges", "api_health")] == []


@pytest.mark.parametrize("zero_env,key", [
    ("WATCH_BACKUP_MAX_AGE_H", "backup_stale"),
    ("WATCH_STATS_ZERO_S", "stats_badges"),
    ("WATCH_HEALTH_FAILS", "api_health"),
    ("WATCH_RESTART_WINDOW_S", "restart_loop"),
    ("WATCH_DISK_PCT", "db_disk"),
])
def test_a_threshold_of_zero_turns_its_check_off(zero_env, key):
    on = {"BACKUP_SPOOL_DIR": "/s", "WATCH_LIBEX_URL": "http://a", "WATCH_LOG_DIR": "/l", "WATCH_DISK_PATH": "/d"}
    assert load_ops_config(on).states()[key] is True  # control: it is on without the zero
    cfg = load_ops_config({**on, zero_env: "0"})
    assert cfg.states()[key] is False
    assert sum(cfg.states().values()) == sum(load_ops_config(on).states().values()) - 1  # and only that one


@pytest.mark.parametrize("level,visible", [
    ("INFO", True), ("DEBUG", True), ("info", True), ("", True),
    ("WARNING", False), ("ERROR", False), ("CRITICAL", False),
])
def test_restart_loop_is_off_when_log_level_is_above_info(level, visible):
    cfg = load_ops_config({"WATCH_LOG_DIR": "/l", "LOG_LEVEL": level})
    assert cfg.states()["restart_loop"] is visible


def test_restart_loop_is_on_when_log_level_unset():
    assert load_ops_config({"WATCH_LOG_DIR": "/l"}).states()["restart_loop"] is True


@pytest.mark.parametrize("env", [
    {"WATCH_HEARTBEAT_NAME": "../x"}, {"WATCH_HEARTBEAT_NAME": "Seeder"}, {"WATCH_DISK_PCT": "lots"},
])
def test_bad_config_refuses_to_start(env):
    with pytest.raises(SystemExit):
        load_ops_config(env)


def test_report_ops_says_on_or_off_for_every_check(caplog):
    cfg = load_ops_config({"WATCH_DISK_PATH": "/d"})
    with caplog.at_level(logging.INFO, logger="libex"):
        checks_mod.report_ops(cfg, "api")
    seen = {r.check: r.msg for r in caplog.records if hasattr(r, "check")}
    assert seen["db_disk"] == "Watch: check on"
    assert seen["backup_stale"] == "Watch: check off"
    assert set(seen) == set(cfg.states())


# --- Alerts: once per incident, one "cleared", blind once per episode ---------------

async def test_incident_sends_once_then_one_cleared():
    rec = Rec()
    alerts = Alerts(rec)
    for _ in range(3):
        await alerts.incident("k", True, "thing", "h", "d", True)
    assert rec.titles == ["thing"]
    await alerts.incident("k", False, "thing", "h", "d", True)
    await alerts.incident("k", False, "thing", "h", "d", True)
    assert rec.titles == ["thing", "cleared: thing"]
    assert rec.sent[-1][3] is False  # a cleared message does not need the operator


async def test_not_bad_without_an_open_incident_sends_nothing():
    rec = Rec()
    await Alerts(rec).incident("k", False, "thing", "h", "d", True)
    assert rec.sent == []


async def test_incident_reopens_after_clearing():
    rec = Rec()
    alerts = Alerts(rec)
    for bad in (True, False, True):
        await alerts.incident("k", bad, "thing", "h", "d", True)
    assert rec.titles == ["thing", "cleared: thing", "thing"]


async def test_separate_keys_are_separate_incidents():
    rec = Rec()
    alerts = Alerts(rec)
    await alerts.incident("a", True, "A", "h", "d", True)
    await alerts.incident("b", True, "B", "h", "d", True)
    assert rec.titles == ["A", "B"]


async def test_blind_says_so_once_per_episode_and_again_after_seen():
    rec = Rec()
    alerts = Alerts(rec)
    await alerts.blind("k", "why")
    await alerts.blind("k", "why")
    assert rec.titles == ["could not check k"]
    alerts.seen("k")
    await alerts.blind("k", "why")
    assert rec.titles == ["could not check k"] * 2


async def test_quiet_closes_an_incident_without_a_message():
    rec = Rec()
    alerts = Alerts(rec)
    await alerts.incident("k", True, "thing", "h", "d", True)
    alerts.quiet("k")
    await alerts.incident("k", False, "thing", "h", "d", True)
    assert rec.titles == ["thing"]


class FlakyRec(Rec):
    """A notifier whose sends fail until `ok` is set."""

    def __init__(self):
        super().__init__()
        self.ok = False

    async def send(self, what, happened, did, needs_you):
        await super().send(what, happened, did, needs_you)
        return self.ok


async def test_a_failed_send_opens_no_incident_and_sends_no_cleared():
    rec = FlakyRec()
    alerts = Alerts(rec)
    await alerts.incident("k", True, "thing", "h", "d", True)
    assert rec.titles == ["thing"]  # attempted
    await alerts.incident("k", False, "thing", "h", "d", True)
    assert rec.titles == ["thing"]  # nothing was delivered, so there is nothing to clear


async def test_a_failed_send_is_retried_on_the_next_pass_then_opens_once():
    rec = FlakyRec()
    alerts = Alerts(rec)
    await alerts.incident("k", True, "thing", "h", "d", True)
    await alerts.incident("k", True, "thing", "h", "d", True)
    assert rec.titles == ["thing", "thing"]  # retried
    rec.ok = True
    await alerts.incident("k", True, "thing", "h", "d", True)
    await alerts.incident("k", True, "thing", "h", "d", True)
    assert rec.titles == ["thing"] * 3  # delivered once, then quiet
    await alerts.incident("k", False, "thing", "h", "d", True)
    assert rec.titles == ["thing"] * 3 + ["cleared: thing"]


# --- log_decision: one record per decision, title only ---------------------------

def _decisions(caplog):
    return [r for r in caplog.records if r.getMessage().startswith("Watch: incident")]


@pytest.mark.parametrize("event,needs_you,level", [
    ("opened", True, logging.WARNING),
    ("opened", False, logging.INFO),
    ("cleared", False, logging.INFO),
    ("cleared", True, logging.INFO),  # a clear is never a warning, whatever the incident needed
])
def test_log_decision_level_and_extra(caplog, event, needs_you, level):
    with caplog.at_level(logging.DEBUG, logger="libex"):
        log_decision("api", event, "thing", needs_you)
    (record,) = _decisions(caplog)
    assert record.levelno == level
    assert record.getMessage() == f"Watch: incident {event}"
    assert (record.stack, record.alert, record.needs_you) == ("api", "thing", needs_you)


async def test_alerts_log_opened_and_cleared_once_each_and_not_a_failed_send(caplog):
    rec = FlakyRec()
    alerts = Alerts(rec, "api")
    with caplog.at_level(logging.DEBUG, logger="libex"):
        await alerts.incident("k", True, "thing", "h", "d", True)
        assert _decisions(caplog) == []  # not delivered, not decided
        rec.ok = True
        await alerts.incident("k", True, "thing", "h", "d", True)
        await alerts.incident("k", True, "thing", "h", "d", True)
        await alerts.incident("k", False, "thing", "h", "d", True)
    assert [r.getMessage() for r in _decisions(caplog)] == ["Watch: incident opened", "Watch: incident cleared"]
    assert [r.levelno for r in _decisions(caplog)] == [logging.WARNING, logging.INFO]


async def test_a_decision_record_carries_no_body_text(caplog):
    secret = "tok-Zq9Xw8Vb7"
    alerts = Alerts(Rec(), "api")
    with caplog.at_level(logging.DEBUG, logger="libex"):
        await alerts.incident("k", True, "thing", f"happened {secret}", f"did {secret}", True)
        await alerts.incident("k", False, "thing", "h", "d", True)
    assert len(_decisions(caplog)) == 2
    assert secret not in caplog.text + " ".join(str(r.__dict__) for r in caplog.records)


# --- backup: could not check is not stale ----------------------------------------

def _status(tmp_path, content):
    (tmp_path / "status.json").write_text(content if isinstance(content, str) else json.dumps(content))


def _ago(hours):
    return datetime.fromtimestamp(time.time() - hours * HOUR, timezone.utc).isoformat()


def _backup(tmp_path, **kw):
    return make(backup_spool_dir=str(tmp_path), **kw)


@pytest.mark.parametrize("hours,alerts", [(1, False), (25, False), (25.9, False), (26.1, True), (27, True), (400, True)])
async def test_backup_threshold_both_sides(tmp_path, hours, alerts):
    _status(tmp_path, {"last_success_at": _ago(hours)})
    checks, rec = _backup(tmp_path)
    await checks.run_once(None)
    assert rec.titles == (["backup stale"] if alerts else [])


async def test_backup_threshold_follows_the_configured_hours(tmp_path):
    _status(tmp_path, {"last_success_at": _ago(10)})
    checks, rec = _backup(tmp_path, backup_max_age_h=8)
    await checks.run_once(None)
    assert rec.titles == ["backup stale"]
    checks, rec = _backup(tmp_path, backup_max_age_h=12)
    await checks.run_once(None)
    assert rec.titles == []


async def test_backup_zero_threshold_is_off_and_never_reads(tmp_path):
    _status(tmp_path, {"last_success_at": _ago(500)})
    checks, rec = _backup(tmp_path, backup_max_age_h=0)
    await checks.run_once(None)
    assert rec.sent == []


async def test_backup_missing_status_is_could_not_check_never_stale(tmp_path):
    checks, rec = _backup(tmp_path)
    await checks.run_once(None)
    assert rec.titles == ["could not check backup_stale"]
    assert "stale" not in rec.sent[0][0].replace("could not check backup_stale", "")


@pytest.mark.parametrize("content", ["", "{not json", "\x00\x01", "[1, 2"])
async def test_backup_unparseable_status_is_could_not_check_never_stale(tmp_path, content):
    _status(tmp_path, content)
    checks, rec = _backup(tmp_path)
    await checks.run_once(None)
    assert rec.titles == ["could not check backup_stale"]


async def test_backup_unreadable_spool_dir_is_could_not_check(tmp_path):
    checks, rec = make(backup_spool_dir=str(tmp_path / "no-such-dir"))
    await checks.run_once(None)
    assert rec.titles == ["could not check backup_stale"]


@pytest.mark.parametrize("content", [{}, {"last_success_at": ""}, {"last_success_at": "not a date"}, {"other": "x"}])
async def test_backup_readable_status_without_a_success_is_stale(tmp_path, content):
    _status(tmp_path, content)
    checks, rec = _backup(tmp_path)
    await checks.run_once(None)
    assert rec.titles == ["backup stale"]
    assert "no successful backup is recorded" in rec.sent[0][1]


async def test_backup_naive_timestamp_is_read_as_utc(tmp_path):
    naive = datetime.fromtimestamp(time.time() - 27 * HOUR, timezone.utc).replace(tzinfo=None).isoformat()
    _status(tmp_path, {"last_success_at": naive})
    checks, rec = _backup(tmp_path)
    await checks.run_once(None)
    assert rec.titles == ["backup stale"]


async def test_backup_lifecycle_once_then_one_cleared(tmp_path):
    checks, rec = _backup(tmp_path)
    _status(tmp_path, {"last_success_at": _ago(30)})
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.titles == ["backup stale"]
    _status(tmp_path, {"last_success_at": _ago(1)})
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.titles == ["backup stale", "cleared: backup stale"]


async def test_backup_could_not_check_once_per_episode_then_again_after_recovery(tmp_path):
    checks, rec = _backup(tmp_path)
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.titles == ["could not check backup_stale"]
    _status(tmp_path, {"last_success_at": _ago(1)})
    await checks.run_once(None)
    (tmp_path / "status.json").unlink()
    await checks.run_once(None)
    assert rec.titles == ["could not check backup_stale"] * 2


# --- API health: three in a row -------------------------------------------------

def _health_client(codes):
    seq = iter(codes)

    def handler(request):
        code = next(seq)
        if isinstance(code, Exception):
            raise code
        return httpx.Response(code, json={})

    return client(handler)


async def test_health_alerts_on_the_third_consecutive_failure_not_before():
    checks, rec = make(libex_url="http://api", stats_zero_s=0)
    http = _health_client([500, 500, 500, 500])
    await checks.run_once(http)
    await checks.run_once(http)
    assert rec.sent == []
    await checks.run_once(http)
    assert rec.titles == ["API unhealthy"]
    await checks.run_once(http)
    assert rec.titles == ["API unhealthy"]  # the fourth failure is the same incident


async def test_a_success_resets_the_health_streak():
    checks, rec = make(libex_url="http://api", stats_zero_s=0)
    http = _health_client([500, 500, 200, 500, 500])
    for _ in range(5):
        await checks.run_once(http)
    assert rec.sent == []
    assert checks.health_streak == 2


async def test_health_recovery_sends_exactly_one_cleared():
    checks, rec = make(libex_url="http://api", stats_zero_s=0)
    http = _health_client([500, 500, 500, 200, 200])
    for _ in range(5):
        await checks.run_once(http)
    assert rec.titles == ["API unhealthy", "cleared: API unhealthy"]


async def test_health_counts_a_connection_error_as_a_failure_and_logs_the_type_only(caplog):
    checks, rec = make(libex_url="http://api", stats_zero_s=0)
    http = _health_client([httpx.ConnectError("secret-host-9.example"), httpx.ReadTimeout("t"), httpx.ConnectError("x")])
    with caplog.at_level(logging.DEBUG, logger="libex"):
        for _ in range(3):
            await checks.run_once(http)
    assert rec.titles == ["API unhealthy"]
    assert "secret-host-9" not in caplog.text
    assert {r.error_type for r in caplog.records if hasattr(r, "error_type")} == {"ConnectError", "ReadTimeout"}


@pytest.mark.parametrize("fails,after,alerts", [(1, 1, True), (2, 1, False), (2, 2, True), (5, 4, False), (5, 5, True)])
async def test_health_threshold_follows_the_configured_count(fails, after, alerts):
    checks, rec = make(libex_url="http://api", health_fails=fails, stats_zero_s=0)
    http = _health_client([500] * after)
    for _ in range(after):
        await checks.run_once(http)
    assert bool(rec.sent) is alerts


async def test_health_zero_is_off_and_makes_no_request():
    seen = []
    checks, rec = make(libex_url="http://api", health_fails=0, stats_zero_s=0)
    await checks.run_once(client(lambda r: seen.append(r) or httpx.Response(500)))
    assert seen == [] and rec.sent == []


async def test_health_with_no_client_counts_as_failing():
    checks, rec = make(libex_url="http://api", health_fails=1, stats_zero_s=0)
    await checks.run_once(None)
    assert rec.titles == ["API unhealthy"]


# --- stats badges: zero for more than the window ---------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(checks_mod, "time", c)
    return c


def _stats_client(state, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={})
        return httpx.Response(200, json=state["stats"])

    return client(handler)


GOOD_STATS = {"books": 10, "authors": 5, "narrators": 3, "series": 2}
BADGE_TITLE = "books badge at zero"


async def test_books_zero_threshold_both_sides(clock):
    state = {"stats": {**GOOD_STATS, "books": 0}}
    checks, rec = make(libex_url="http://api", health_fails=0)
    http = _stats_client(state)
    await checks.run_once(http)
    clock.now += 1800  # exactly the window is not "more than"
    await checks.run_once(http)
    assert rec.sent == []
    clock.now += 1
    await checks.run_once(http)
    assert rec.titles == [BADGE_TITLE]
    assert rec.sent[0][1] == "/db/stats has reported zero books for over 30 minutes"
    clock.now += 600
    await checks.run_once(http)
    assert rec.titles == [BADGE_TITLE]  # once


@pytest.mark.parametrize("field", ["authors", "narrators", "series"])
async def test_a_zero_in_any_other_counter_never_alerts(clock, field):
    checks, rec = make(libex_url="http://api", health_fails=0)
    http = _stats_client({"stats": {**GOOD_STATS, field: 0}})
    for _ in range(3):
        clock.now += 5000
        await checks.run_once(http)
    assert rec.sent == []


@pytest.mark.parametrize("extra", [
    {"authors": "x"}, {"narrators": None}, {"series": True}, {"authors": -4, "series": 1.5},
    {"authors": [], "narrators": {}, "series": "0"}, {"unrelated": 0},
])
async def test_odd_other_counters_are_ignored_not_could_not_check(clock, extra):
    checks, rec = make(libex_url="http://api", health_fails=0)
    http = _stats_client({"stats": {"books": 10, **extra}})
    for _ in range(2):
        clock.now += 5000
        await checks.run_once(http)
    assert rec.sent == []
    zero = _stats_client({"stats": {"books": 0, **extra}})
    clock.now += 5000
    await checks.run_once(zero)
    clock.now += 1801
    await checks.run_once(zero)
    assert rec.titles == [BADGE_TITLE]  # a real zero in books still alerts beside the odd fields


async def test_books_recovery_resets_the_window_and_sends_one_cleared(clock):
    state = {"stats": {**GOOD_STATS, "books": 0}}
    checks, rec = make(libex_url="http://api", health_fails=0)
    http = _stats_client(state)
    await checks.run_once(http)
    clock.now += 1801
    await checks.run_once(http)
    state["stats"] = GOOD_STATS
    await checks.run_once(http)
    await checks.run_once(http)
    assert rec.titles == [BADGE_TITLE, "cleared: " + BADGE_TITLE]
    state["stats"] = {**GOOD_STATS, "books": 0}
    clock.now += 100
    await checks.run_once(http)
    clock.now += 1700  # a fresh zero starts a fresh window, it does not inherit the old one
    await checks.run_once(http)
    assert len(rec.sent) == 2


async def test_stats_nonzero_never_alerts(clock):
    checks, rec = make(libex_url="http://api", health_fails=0)
    http = _stats_client({"stats": GOOD_STATS})
    for _ in range(3):
        clock.now += 5000
        await checks.run_once(http)
    assert rec.sent == []


async def test_stats_zero_window_zero_is_off(clock):
    seen = []
    checks, rec = make(libex_url="http://api", health_fails=0, stats_zero_s=0)
    http = _stats_client({"stats": {"books": 0, "authors": 0, "narrators": 0, "series": 0}}, seen)
    clock.now += 99999
    await checks.run_once(http)
    assert seen == [] and rec.sent == []


@pytest.mark.parametrize("stats", [
    {}, {"authors": 5, "narrators": 3, "series": 2},  # books missing
    {"books": "10"}, {"books": True}, {"books": False}, {"books": None}, {"books": 1.5}, {"books": 0.0},
    {"books": [0]}, {"books": {}},
])
async def test_a_missing_or_non_int_books_is_could_not_check_never_ok_or_zero(clock, stats):
    checks, rec = make(libex_url="http://api", health_fails=0)
    http = _stats_client({"stats": stats})
    await checks.run_once(http)
    clock.now += 5000
    await checks.run_once(http)
    assert rec.titles == ["could not check stats_badges"]  # once per episode, no zero alert, no cleared


async def test_stats_http_error_is_could_not_check():
    checks, rec = make(libex_url="http://api", health_fails=0)
    http = client(lambda r: httpx.Response(200) if r.url.path == "/health" else httpx.Response(500))
    await checks.run_once(http)
    assert rec.titles == ["could not check stats_badges"]


# --- stats is skipped while health is failing ------------------------------------

async def test_stats_is_skipped_while_health_is_failing():
    paths = []

    def api(request):
        paths.append(request.url.path)
        return httpx.Response(503, json={})

    checks, rec = make(libex_url="http://api")
    await checks.run_once(client(api))
    assert paths == ["/health"]  # /db/stats never asked
    assert rec.sent == []  # a blind or zero alert here would be noise on top of the health alert


async def test_stats_runs_again_once_health_recovers():
    state = {"code": 503}
    paths = []

    def api(request):
        paths.append(request.url.path)
        return httpx.Response(state["code"] if request.url.path == "/health" else 200, json=GOOD_STATS)

    checks, rec = make(libex_url="http://api")
    await checks.run_once(client(api))
    assert "/db/stats" not in paths
    state["code"] = 200
    await checks.run_once(client(api))
    assert "/db/stats" in paths


# --- no Axiom name remains in the watch modules ------------------------------------

def test_no_axiom_name_remains_in_the_watch_modules():
    names = [n for m in (checks_mod, watch_pkg, stack_watch) for n in vars(m)]
    assert [n for n in names if "axiom" in n.lower()] == []
    assert [f for f in OpsConfig.__dataclass_fields__ if "axiom" in f.lower()] == []
    on = load_ops_config({"AXIOM_QUERY_TOKEN": "x"})
    assert [k for k in on.states() if "axiom" in k.lower()] == []
    assert not on.any_on  # the old env var turns nothing on
    assert list(inspect.signature(OpsChecks.run_once).parameters) == ["self", "http"]


# --- restart loop: the real formatter, anchored, whole lines ---------------------

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - pid=%(process)d - %(message)s"
FMT = ContextFormatter(LOG_FORMAT)


def _line(msg, pid=101, age_s=60, level=logging.INFO, **extra):
    record = logging.LogRecord("libex", level, __file__, 1, msg, None, None)
    record.process = pid
    record.created = time.time() - age_s
    for key, value in extra.items():
        setattr(record, key, value)
    return FMT.format(record)


def _boot(pid=101, age_s=60, version=None):
    return _line(f"Libex {version or VERSION} starting up", pid, age_s)


def test_the_formatter_string_here_is_the_one_setup_logging_uses():
    assert LOG_FORMAT in inspect.getsource(setup_logging)


def test_the_real_startup_line_has_the_shape_the_matcher_expects():
    line = _boot(77)
    assert line.endswith(f" - libex - INFO - pid=77 - Libex {VERSION} starting up")


def _since(seconds=1800):
    return datetime.fromtimestamp(time.time() - seconds)


def test_boot_count_counts_a_real_startup_line():
    assert boot_count(_boot(101), VERSION, _since()) == 1


def test_a_pid_that_boots_again_is_counted_each_time():
    # A restarted container reuses its pids (a lone worker is pid 1 every time).
    body = "\n".join([_boot(1, age_s=600), _boot(1, age_s=400), _boot(1, age_s=200), _boot(1, age_s=60)])
    assert boot_count(body, VERSION, _since()) == 4


def test_a_non_boot_line_for_the_same_pid_is_not_counted():
    body = "\n".join([_boot(1, age_s=400), _line("Request completed", 1, age_s=300), _boot(1, age_s=60)])
    assert boot_count(body, VERSION, _since()) == 2


def test_a_request_line_quoting_the_startup_text_is_not_a_boot():
    quoted = f"Libex {VERSION} starting up"
    prefix = f"2026-11-16 12:00:00,000 - libex - INFO - pid=9 - {quoted}"
    lines = [
        _line("Request completed", 9, url=f"/x?q={quoted}", userAgent=quoted, status=200),
        _line("Request completed", 9, url=prefix),
        _line("Request completed", 9, userAgent=f"pid=9 - {quoted}"),
        _line(f"Request completed - {quoted}", 9),
        f"{_boot(9)} (extra: 1)",  # trailing context: not the bare startup record
        f"junk {_boot(9)}",
        f"{_boot(9)} ",
    ]
    assert boot_count("\n".join(lines), VERSION, _since()) == 0


def test_the_anchoring_negative_has_a_positive_control():
    body = "\n".join([_line("Request completed", 9, userAgent=f"Libex {VERSION} starting up"), _boot(9)])
    assert boot_count(body, VERSION, _since()) == 1


@pytest.mark.parametrize("make_line", [
    lambda: _boot(1, version="0.0.0-other"),
    lambda: _line(f"Libex {VERSION} starting up", 1, level=logging.WARNING),
    lambda: _line(f"Libex {VERSION} starting up", 1).replace("pid=1", "pid=abc"),
    lambda: _line(f"Libex {VERSION} starting up", 1).replace(" - libex - ", " - other - "),
    lambda: _line(f"Libex {VERSION} shutting down", 1),
    lambda: _boot(1).replace(VERSION, VERSION + "x"),
])
def test_other_lines_are_not_boots(make_line):
    assert boot_count(make_line(), VERSION, _since()) == 0


def test_boots_outside_the_window_are_not_counted():
    body = "\n".join([_boot(1, age_s=3600), _boot(2, age_s=60)])
    assert boot_count(body, VERSION, _since(1800)) == 1


def test_version_is_matched_literally_not_as_a_pattern():
    other = "2x6y1"
    assert boot_count(_boot(1, version=other), "2.6.1", _since()) == 0


def _logdir(tmp_path, current=(), rotated=None):
    (tmp_path / "libex.log").write_text("\n".join(current) + "\n")
    for name, lines in (rotated or {}).items():
        (tmp_path / name).write_text("\n".join(lines) + "\n")
    return OpsConfig(log_dir=str(tmp_path))


def test_boots_in_logs_reads_the_current_file_and_the_newest_rotated_one_only(tmp_path):
    cfg = _logdir(tmp_path, [_boot(1)], {
        "libex.log.2026-11-14": [_boot(3), _boot(4), _boot(5)],
        "libex.log.2026-11-15": [_boot(2)],
    })
    assert boots_in_logs(cfg, VERSION) == 2  # the current file and the newest rotation; the older one (3 boots) is not read


def test_boots_in_logs_counts_a_boot_in_each_file(tmp_path):
    cfg = _logdir(tmp_path, [_boot(1)], {"libex.log.2026-11-15": [_boot(1)]})
    assert boots_in_logs(cfg, VERSION) == 2


def test_boots_in_logs_ignores_files_that_only_resemble_rotations(tmp_path):
    cfg = _logdir(tmp_path, [], {"libex.log.lock": [_boot(8)], "libex.log.2026-11-15.bak": [_boot(9)], "other.log": [_boot(7)]})
    assert boots_in_logs(cfg, VERSION) == 0


def test_boots_in_logs_tolerates_a_missing_current_file(tmp_path):
    (tmp_path / "libex.log.2026-11-15").write_text(_boot(4) + "\n")
    assert boots_in_logs(OpsConfig(log_dir=str(tmp_path)), VERSION) == 1


def test_boots_in_logs_reads_only_the_tail(tmp_path):
    filler = [_line("Request completed", 1, status=200)] * 400
    cfg = _logdir(tmp_path, [_boot(1)] + filler + [_boot(2)])
    cfg = OpsConfig(log_dir=str(tmp_path), log_tail_mib=0.01)  # about 10 KiB
    assert boots_in_logs(cfg, VERSION) == 1  # only the boot near the end


def test_boots_in_logs_discards_the_half_line_a_tail_starts_in(tmp_path):
    whole = _boot(2)
    pad = "x" * 5000
    (tmp_path / "libex.log").write_text(f"{pad}{_boot(1)}\n{whole}\n")
    cfg = OpsConfig(log_dir=str(tmp_path), log_tail_mib=(len(whole) + 40) / (1024 * 1024))
    assert boots_in_logs(cfg, VERSION) == 1


def test_boots_in_logs_survives_undecodable_bytes(tmp_path):
    (tmp_path / "libex.log").write_bytes(b"\xff\xfe\x80garbage\n" + _boot(3).encode() + b"\n")
    assert boots_in_logs(OpsConfig(log_dir=str(tmp_path)), VERSION) == 1


@pytest.mark.parametrize("pids,alerts", [(0, False), (1, False), (2, False), (3, True), (9, True)])
async def test_restart_loop_threshold_both_sides(tmp_path, pids, alerts):
    cfg = _logdir(tmp_path, [_boot(100 + n) for n in range(pids)])
    rec = Rec()
    checks = OpsChecks(OpsConfig(log_dir=cfg.log_dir, restart_boots=2), rec, "api")
    await checks.run_once(None)
    assert rec.titles == (["API restarting"] if alerts else [])


async def test_restart_loop_alerts_when_one_pid_boots_over_and_over(tmp_path):
    # A single worker is pid 1 on every container restart.
    cfg = _logdir(tmp_path, [_boot(1, age_s=600 - 50 * n) for n in range(10)])
    rec = Rec()
    await OpsChecks(OpsConfig(log_dir=cfg.log_dir), rec, "api").run_once(None)
    assert rec.titles == ["API restarting"]


async def test_restart_loop_stays_quiet_for_one_boot_of_a_pid(tmp_path):
    cfg = _logdir(tmp_path, [_boot(1)])
    rec = Rec()
    await OpsChecks(OpsConfig(log_dir=cfg.log_dir), rec, "api").run_once(None)
    assert rec.sent == []


async def test_restart_loop_lifecycle(tmp_path):
    cfg = _logdir(tmp_path, [_boot(n) for n in range(10, 14)])
    rec = Rec()
    checks = OpsChecks(OpsConfig(log_dir=cfg.log_dir), rec, "api")
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.titles == ["API restarting"]
    (tmp_path / "libex.log").write_text("")
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.titles == ["API restarting", "cleared: API restarting"]


async def test_restart_loop_off_when_window_is_zero_or_level_above_info(tmp_path):
    lines = [_boot(n) for n in range(10, 20)]
    for env in ({"WATCH_RESTART_WINDOW_S": "0"}, {"LOG_LEVEL": "WARNING"}):
        _logdir(tmp_path, lines)
        cfg = load_ops_config({"WATCH_LOG_DIR": str(tmp_path), **env})
        rec = Rec()
        await OpsChecks(cfg, rec, "api").run_once(None)
        assert rec.sent == []


async def test_restart_loop_missing_log_dir_is_could_not_check(tmp_path):
    rec = Rec()
    await OpsChecks(OpsConfig(log_dir=str(tmp_path / "nope")), rec, "api").run_once(None)
    assert rec.titles == ["could not check restart_loop"]


async def test_restart_loop_line_content_and_pids_never_reach_a_log_or_an_alert(tmp_path, caplog):
    marker = "marker-user-agent-7f3a"
    body = [_boot(424240 + n) for n in range(5)]
    body.append(_line("Request completed", 424299, url="/private/path?token=abc123", userAgent=marker))
    cfg = _logdir(tmp_path, body)
    rec = Rec()
    with caplog.at_level(logging.DEBUG, logger="libex"):
        await OpsChecks(OpsConfig(log_dir=cfg.log_dir), rec, "api").run_once(None)
    assert rec.titles == ["API restarting"]  # control: it ran and alerted
    everything = " ".join(" ".join(s[:3]) for s in rec.sent) + caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    for leaked in (marker, "abc123", "/private/path", "42424", "424299"):
        assert leaked not in everything


# --- worker heartbeat: dead, stalled, stopped, done --------------------------------

def _beat(tmp_path, alive_ago=0, progress_ago=0, due=7200, state="running", name="seeder"):
    now = time.time()
    (tmp_path / f"{name}.json").write_text(json.dumps({
        "alive_at": now - alive_ago, "progress_at": now - progress_ago, "progress_due_s": due, "state": state,
    }))


def _hb(tmp_path, **kw):
    return make(heartbeat_name="seeder", heartbeat_dir=str(tmp_path), **kw)


async def test_heartbeat_healthy_is_silent(tmp_path):
    _beat(tmp_path, alive_ago=10, progress_ago=100)
    checks, rec = _hb(tmp_path)
    await checks.run_once(None)
    assert rec.sent == []


@pytest.mark.parametrize("alive_ago,alerts", [(299, False), (301, True), (5000, True)])
async def test_heartbeat_dead_threshold_both_sides(tmp_path, alive_ago, alerts):
    _beat(tmp_path, alive_ago=alive_ago)
    checks, rec = _hb(tmp_path)
    await checks.run_once(None)
    assert rec.titles == (["seeder dead"] if alerts else [])


@pytest.mark.parametrize("progress_ago,alerts", [(7199, False), (7201, True)])
async def test_heartbeat_stalled_threshold_both_sides_uses_its_own_due(tmp_path, progress_ago, alerts):
    _beat(tmp_path, alive_ago=5, progress_ago=progress_ago, due=7200)
    checks, rec = _hb(tmp_path)
    await checks.run_once(None)
    assert rec.titles == (["seeder stalled"] if alerts else [])


async def test_a_deferred_pause_is_not_a_stall_but_a_real_stall_after_it_is(tmp_path, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    paused = Heartbeat("seeder", 1800, str(tmp_path))
    unpaused = Heartbeat("backfill", 1800, str(tmp_path))
    paused.defer(7200)  # a deliberate sleep longer than progress_due
    checks, rec = _hb(tmp_path)
    other, other_rec = make(heartbeat_name="backfill", heartbeat_dir=str(tmp_path))

    clock[0] += 3600  # mid-pause, twice the due time
    for beat in (paused, unpaused):
        beat._write("running")
    await checks.run_once(None)
    await other.run_once(None)
    assert rec.sent == []  # the pause is not a stall
    assert other_rec.titles == ["backfill stalled"]  # control: without defer the same silence is a stall

    clock[0] += 3600 + 1800 - 1  # one second short of the due time after the pause ends
    paused._write("running")
    await checks.run_once(None)
    assert rec.sent == []
    clock[0] += 2  # now past it
    paused._write("running")
    await checks.run_once(None)
    assert rec.titles == ["seeder stalled"]


async def test_heartbeat_stalled_follows_the_beats_own_progress_due(tmp_path):
    _beat(tmp_path, alive_ago=5, progress_ago=2000, due=1800)  # a backfill-sized due
    checks, rec = _hb(tmp_path)
    await checks.run_once(None)
    assert rec.titles == ["seeder stalled"]
    _beat(tmp_path, alive_ago=5, progress_ago=2000, due=7200)
    checks, rec = _hb(tmp_path)
    await checks.run_once(None)
    assert rec.sent == []


async def test_heartbeat_dead_worker_reports_dead_not_also_stalled(tmp_path):
    _beat(tmp_path, alive_ago=9999, progress_ago=99999)
    checks, rec = _hb(tmp_path)
    await checks.run_once(None)
    assert rec.titles == ["seeder dead"]


@pytest.mark.parametrize("state", ["stopped", "done"])
async def test_heartbeat_stopped_and_done_are_silent_even_when_stale(tmp_path, state):
    _beat(tmp_path, alive_ago=99999, progress_ago=99999, state=state)
    checks, rec = _hb(tmp_path)
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.sent == []


async def test_heartbeat_going_to_stopped_closes_a_dead_incident_without_a_message(tmp_path):
    _beat(tmp_path, alive_ago=9999)
    checks, rec = _hb(tmp_path)
    await checks.run_once(None)
    _beat(tmp_path, alive_ago=9999, state="stopped")
    await checks.run_once(None)
    assert rec.titles == ["seeder dead"]


async def test_heartbeat_dead_lifecycle_once_then_one_cleared(tmp_path):
    checks, rec = _hb(tmp_path)
    _beat(tmp_path, alive_ago=9999)
    await checks.run_once(None)
    await checks.run_once(None)
    _beat(tmp_path, alive_ago=1)
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.titles == ["seeder dead", "cleared: seeder dead"]


async def test_heartbeat_missing_is_could_not_check_never_healthy(tmp_path):
    checks, rec = _hb(tmp_path)
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.titles == ["could not check worker_heartbeat"]  # once per episode


@pytest.mark.parametrize("content", [
    "{not json", "[]", json.dumps({"alive_at": time.time(), "progress_at": time.time(), "progress_due_s": 1, "state": "running", "x": 1}),
    json.dumps({"alive_at": "now", "progress_at": 1, "progress_due_s": 1, "state": "running"}),
    json.dumps({"alive_at": 1, "progress_at": 1, "progress_due_s": 1, "state": "paused"}),
    "x" * 5000,
])
async def test_heartbeat_invalid_file_is_could_not_check_never_healthy(tmp_path, content):
    (tmp_path / "seeder.json").write_text(content)
    checks, rec = _hb(tmp_path)
    await checks.run_once(None)
    assert rec.titles == ["could not check worker_heartbeat"]


# --- DB disk -------------------------------------------------------------------------

def _statvfs(monkeypatch, blocks, bfree, bavail):
    monkeypatch.setattr(
        checks_mod.os, "statvfs",
        lambda path: SimpleNamespace(f_blocks=blocks, f_bfree=bfree, f_bavail=bavail),
    )


def test_disk_percent_is_df_style_used_over_used_plus_available(monkeypatch):
    _statvfs(monkeypatch, 100, 20, 10)  # 80 used, 10 available to the caller: 80/90
    assert disk_percent("/x") == pytest.approx(88.888, abs=0.01)


def test_disk_percent_of_an_empty_filesystem_is_zero(monkeypatch):
    _statvfs(monkeypatch, 0, 0, 0)
    assert disk_percent("/x") == 0.0


@pytest.mark.parametrize("used,alerts", [(70, False), (84, False), (85, False), (86, True), (99, True)])
async def test_disk_threshold_both_sides(monkeypatch, used, alerts):
    _statvfs(monkeypatch, 100, 100 - used, 100 - used)
    checks, rec = make(disk_path="/data")
    await checks.run_once(None)
    assert rec.titles == (["database disk filling"] if alerts else [])


async def test_disk_lifecycle_once_then_one_cleared(monkeypatch):
    checks, rec = make(disk_path="/data")
    _statvfs(monkeypatch, 100, 5, 5)
    await checks.run_once(None)
    await checks.run_once(None)
    _statvfs(monkeypatch, 100, 50, 50)
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.titles == ["database disk filling", "cleared: database disk filling"]


async def test_disk_zero_threshold_is_off(monkeypatch):
    _statvfs(monkeypatch, 100, 0, 0)
    checks, rec = make(disk_path="/data", disk_pct=0)
    await checks.run_once(None)
    assert rec.sent == []


async def test_disk_unreadable_path_is_could_not_check(tmp_path):
    checks, rec = make(disk_path=str(tmp_path / "missing"))
    await checks.run_once(None)
    assert rec.titles == ["could not check db_disk"]


# --- Postgres: engine, statements, thresholds -------------------------------------------

DB_URL = "postgresql://libex_watch:s3cretPASSWORD-0a1b2c3d4e5f@db.internal:5432/libex"


class FakeConn:
    def __init__(self, state):
        self.state = state

    async def execute(self, stmt):
        sql = str(stmt)
        self.state["sql"].append(sql)
        if sql == checks_mod.SQL_BOOKS_TUPLES:
            return SimpleNamespace(one_or_none=lambda: self.state["tuples"])
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.state["running"]))


class FakeEngine:
    def __init__(self, state):
        self.state = state
        self.disposed = False

    def connect(self):
        state = self.state

        class _Ctx:
            async def __aenter__(self_inner):
                if state["fail"] is not None:
                    raise state["fail"]
                return FakeConn(state)

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()

    async def dispose(self):
        self.disposed = True


@pytest.fixture
def db(monkeypatch):
    state = {"fail": None, "tuples": (1000, 0, 100.0), "running": [], "sql": [], "created": []}
    real_create = checks_mod.create_async_engine

    def factory(url, **kw):
        real = real_create(url, **kw)  # build the real thing to inspect it; nothing connects
        state["created"].append({"url": str(url), "kw": kw, "pool": real.pool.size(), "echo": real.echo})
        state["engine"] = FakeEngine(state)
        return state["engine"]

    monkeypatch.setattr(checks_mod, "create_async_engine", factory)
    return state


def _vac(**kw):
    return make(database_url=DB_URL, **kw)


async def test_engine_is_built_once_with_a_pool_of_one_and_echo_off(db):
    checks, rec = _vac()
    for _ in range(3):
        await checks.run_once(None)
    assert len(db["created"]) == 1
    built = db["created"][0]
    assert built["pool"] == 1 and built["echo"] is False
    assert built["kw"]["pool_size"] == 1 and built["kw"]["max_overflow"] == 0 and built["kw"]["echo"] is False
    assert built["url"].startswith("postgresql+asyncpg://libex_watch:")  # the async driver, same credentials


async def test_engine_connects_and_queries_under_a_ten_second_timeout(db):
    checks, _ = _vac()
    await checks.run_once(None)
    args = db["created"][0]["kw"]["connect_args"]
    assert args == {"timeout": 10.0, "command_timeout": 10.0}  # a hung Postgres must not hang the loop


def test_the_autovacuum_query_pins_the_public_schema():
    autovac = SQL_STATEMENTS[1]
    assert "'public.books'::regclass" in autovac
    assert "'books'::regclass" not in autovac.replace("'public.books'::regclass", "")


def test_db_url_only_rewrites_the_bare_postgresql_scheme():
    assert checks_mod._db_url("postgresql://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"
    assert checks_mod._db_url("postgresql+asyncpg://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"


async def test_aclose_disposes_the_engine(db):
    checks, _ = _vac()
    await checks.run_once(None)
    await checks.aclose()
    assert db["engine"].disposed is True


def test_no_sql_constant_selects_statement_text():
    assert len(SQL_STATEMENTS) == 2  # the control: the set is not empty or silently shrunk
    for sql in SQL_STATEMENTS:
        assert "query" not in sql.lower()
        assert sql.lstrip().upper().startswith("SELECT")
        assert "pg_stat" in sql


async def test_only_the_declared_statements_are_ever_executed(db):
    checks, _ = _vac()
    await checks.run_once(None)
    assert db["sql"] == list(SQL_STATEMENTS)
    assert all("query" not in s.lower() for s in db["sql"])


@pytest.mark.parametrize("dead,live,age,alerts", [
    (201, 1000, None, True),            # just over 20%, never vacuumed
    (200, 1000, None, False),           # exactly 20% is not over
    (100, 1000, None, False),
    (500, 1000, 23 * HOUR, False),      # bloated but vacuumed recently
    (500, 1000, 25 * HOUR, True),       # bloated and not vacuumed in over a day
    (500, 1000, 24 * HOUR, False),      # exactly the age limit is not over
    (5, 0, None, True),                 # dead rows and no live ones: fully dead
    (0, 0, None, False),                # an empty table is not bloat
])
async def test_dead_tuple_threshold_both_sides(db, dead, live, age, alerts):
    db["tuples"] = (live, dead, age)
    checks, rec = _vac()
    await checks.run_once(None)
    assert rec.titles == (["books table bloating"] if alerts else [])


async def test_dead_tuple_thresholds_follow_the_configuration(db):
    db["tuples"] = (1000, 150, None)  # 15%
    checks, rec = _vac(dead_tuple_pct=10)
    await checks.run_once(None)
    assert rec.titles == ["books table bloating"]
    db["tuples"] = (1000, 150, 5 * HOUR)
    checks, rec = _vac(dead_tuple_pct=10, vacuum_max_age_h=4)
    await checks.run_once(None)
    assert rec.titles == ["books table bloating"]
    checks, rec = _vac(dead_tuple_pct=10, vacuum_max_age_h=6)
    await checks.run_once(None)
    assert rec.sent == []


@pytest.mark.parametrize("running,alerts", [
    ([], False), ([6 * HOUR - 1], False), ([6 * HOUR], False), ([6 * HOUR + 1], True),
    ([100, 7 * HOUR], True), ([None], False), ([None, 7 * HOUR], True), ([100.0, 200.0], False),
])
async def test_stuck_autovacuum_threshold_both_sides(db, running, alerts):
    db["running"] = running
    checks, rec = _vac()
    await checks.run_once(None)
    assert rec.titles == (["books autovacuum stuck"] if alerts else [])


async def test_stuck_autovacuum_threshold_follows_the_configuration(db):
    db["running"] = [2 * HOUR]
    checks, rec = _vac(vacuum_run_h=1)
    await checks.run_once(None)
    assert rec.titles == ["books autovacuum stuck"]
    checks, rec = _vac(vacuum_run_h=3)
    await checks.run_once(None)
    assert rec.sent == []


async def test_dead_tuples_and_stuck_autovacuum_are_separate_incidents(db):
    db["tuples"] = (1000, 900, None)
    db["running"] = [9 * HOUR]
    checks, rec = _vac()
    await checks.run_once(None)
    await checks.run_once(None)
    assert sorted(rec.titles) == ["books autovacuum stuck", "books table bloating"]
    db["running"] = []
    await checks.run_once(None)
    assert rec.titles[-1] == "cleared: books autovacuum stuck"  # only the incident that ended is cleared
    assert len(rec.titles) == 3


async def test_dead_tuple_lifecycle_once_then_one_cleared(db):
    checks, rec = _vac()
    db["tuples"] = (1000, 900, None)
    await checks.run_once(None)
    await checks.run_once(None)
    db["tuples"] = (1000, 10, 60.0)
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.titles == ["books table bloating", "cleared: books table bloating"]


async def test_books_missing_from_the_stats_view_is_could_not_check_not_ok(db):
    db["tuples"] = None
    checks, rec = _vac()
    await checks.run_once(None)
    assert rec.titles == ["could not check books_vacuum"]


@pytest.mark.parametrize("failures,alerts", [(1, False), (2, False), (3, True), (4, True)])
async def test_postgres_unreachable_threshold_both_sides(db, failures, alerts):
    db["fail"] = OperationalError("SELECT 1", {}, Exception("boom"))
    checks, rec = _vac()
    for _ in range(failures):
        await checks.run_once(None)
    assert rec.titles == (["Postgres unreachable"] if alerts else [])


async def test_postgres_unreachable_alerts_once_then_one_cleared(db):
    db["fail"] = OSError("refused")
    checks, rec = _vac()
    for _ in range(5):
        await checks.run_once(None)
    assert rec.titles == ["Postgres unreachable"]
    db["fail"] = None
    await checks.run_once(None)
    await checks.run_once(None)
    assert rec.titles == ["Postgres unreachable", "cleared: Postgres unreachable"]


async def test_a_success_resets_the_postgres_failure_streak(db):
    checks, rec = _vac()
    for fail in (True, True, False, True, True):
        db["fail"] = OperationalError("s", {}, Exception("x")) if fail else None
        await checks.run_once(None)
    assert rec.sent == []


async def test_postgres_unreachable_threshold_follows_the_configuration(db):
    db["fail"] = OSError("x")
    checks, rec = _vac(db_fails=5)
    for _ in range(4):
        await checks.run_once(None)
    assert rec.sent == []
    await checks.run_once(None)
    assert rec.titles == ["Postgres unreachable"]


async def test_postgres_unreachable_logs_the_error_type_only(db, caplog):
    db["fail"] = OperationalError("SELECT 1", {}, Exception(DB_URL))
    checks, _ = _vac()
    with caplog.at_level(logging.DEBUG, logger="libex"):
        await checks.run_once(None)
    assert "s3cretPASSWORD" not in caplog.text
    assert any(getattr(r, "error_type", None) == "OperationalError" for r in caplog.records)


async def test_an_unexpected_database_error_is_could_not_check_not_unreachable(db):
    db["fail"] = ValueError("weird")
    checks, rec = _vac()
    await checks.run_once(None)
    assert rec.titles == ["could not check books_vacuum"]


async def test_unset_database_url_makes_no_engine(db):
    checks, rec = make()
    await checks.run_once(None)
    assert db["created"] == [] and rec.sent == []


def _run_ops_capture(monkeypatch, handler, **cfg):
    """Runs run_ops for one pass with every HTTP client over a MockTransport, capturing how each was built."""
    made = []
    real = httpx.AsyncClient
    stop = asyncio.Event()

    def wrapped(request):
        stop.set()  # one pass is enough: the loop exits after the first one
        return handler(request)

    def factory(**kw):
        made.append(kw)
        return real(**{**kw, "transport": httpx.MockTransport(wrapped)})

    monkeypatch.setattr(checks_mod.httpx, "AsyncClient", factory)
    rec = Rec()
    return made, rec, run_ops(OpsConfig(**cfg), rec, "api", stop)


async def test_the_libex_client_does_not_read_env_or_follow_redirects(monkeypatch):
    made, rec, coro = _run_ops_capture(
        monkeypatch, lambda r: httpx.Response(200, json=GOOD_STATS), libex_url="http://api:8000", stats_zero_s=0,
    )
    await coro
    assert made[0]["base_url"] == "http://api:8000"
    assert made[0]["trust_env"] is False and made[0]["follow_redirects"] is False
    assert made[0]["timeout"] is not None


# --- secrets: none reach a log record or an alert -----------------------------------------

GOTIFY_TOKEN = "gotify-Ab12Cd34Ef56Gh78"
LEAK_TOKEN = "xaat-0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
SECRETS = (DB_URL.split("@")[0].split(":")[2], LEAK_TOKEN, GOTIFY_TOKEN, "libex_watch:")


async def test_no_secret_reaches_a_log_record_or_a_gotify_request_body(tmp_path, monkeypatch, caplog, db):
    env = {
        "WATCH_LIBEX_URL": "http://api:8000", "WATCH_DATABASE_URL": DB_URL,
        "BACKUP_SPOOL_DIR": str(tmp_path / "spool"), "WATCH_DISK_PATH": "/data", "WATCH_LOG_DIR": str(tmp_path / "logs"),
        "WATCH_HEARTBEAT_NAME": "seeder", "WATCH_HEARTBEAT_DIR": str(tmp_path / "hb"), "GOTIFY_TOKEN": GOTIFY_TOKEN,
    }
    cfg = load_ops_config(env)
    assert all(cfg.states().values())  # control: every check is live

    posted = []

    def gotify(request):
        posted.append(request)
        return httpx.Response(200, json={})

    notifier = Notifier("https://gotify.example.com", GOTIFY_TOKEN, "api")
    notifier._http = httpx.AsyncClient(
        base_url="https://gotify.example.com", headers={"X-Gotify-Key": GOTIFY_TOKEN},
        transport=httpx.MockTransport(gotify),
    )
    mode = {"health": "raise"}

    def api(request):
        if request.url.path == "/health":
            if mode["health"] == "raise":
                raise httpx.ConnectError(f"cannot reach {LEAK_TOKEN} {DB_URL}")
            return httpx.Response(200, json={})
        raise httpx.ConnectError(f"stats {LEAK_TOKEN} {GOTIFY_TOKEN}")

    def statvfs(path):
        raise PermissionError(f"{DB_URL} {GOTIFY_TOKEN}")

    monkeypatch.setattr(checks_mod.os, "statvfs", statvfs)
    db["fail"] = OperationalError("SELECT 1", {}, Exception(f"{DB_URL} {LEAK_TOKEN}"))
    checks = OpsChecks(cfg, notifier, "api")
    with caplog.at_level(logging.DEBUG, logger="libex"):
        for _ in range(4):  # health failing: stats skipped; then up: they run and fail
            await checks.run_once(client(api))
        mode["health"] = "ok"
        for _ in range(2):
            await checks.run_once(client(api))
    await notifier.aclose()

    assert len(caplog.records) >= 5 and posted, "control: there were records and alerts to inspect"
    logged = " ".join(r.getMessage() + " " + str(r.__dict__) + (r.exc_text or "") for r in caplog.records)
    wire = " ".join(str(r.url) + r.content.decode() for r in posted)
    for secret in SECRETS:
        assert secret not in logged
        assert secret not in wire
    # the one legitimate place a gotify token goes is its own header
    assert all(r.headers["X-Gotify-Key"] == GOTIFY_TOKEN for r in posted)
    assert not any(LEAK_TOKEN in v or DB_URL in v for r in posted for v in r.headers.values())
    titles = " ".join(json.loads(r.content)["title"] for r in posted)
    assert "could not check" in titles and "Postgres unreachable" in titles


async def test_a_check_that_raises_does_not_stop_the_others_and_says_so_once(tmp_path):
    checks, rec = make(backup_spool_dir=str(tmp_path / "missing"), disk_path=str(tmp_path / "missing"), log_dir=str(tmp_path / "nope"))
    await checks.run_once(None)
    await checks.run_once(None)
    assert sorted(rec.titles) == [
        "could not check backup_stale", "could not check db_disk", "could not check restart_loop",
    ]


async def test_a_failed_blind_send_is_retried_next_pass_then_sent_once():
    rec = FlakyRec()
    alerts = Alerts(rec)
    await alerts.blind("k", "why")
    await alerts.blind("k", "why")
    assert rec.titles == ["could not check k"] * 2  # failed, so tried again
    rec.ok = True
    for _ in range(3):
        await alerts.blind("k", "why")
    assert rec.titles == ["could not check k"] * 3  # delivered once, then quiet


async def test_a_delivered_blind_send_does_not_repeat_while_the_episode_lasts():
    rec = FlakyRec()
    rec.ok = True
    alerts = Alerts(rec)
    for _ in range(4):
        await alerts.blind("k", "why")
    assert rec.titles == ["could not check k"]
    await alerts.blind("other", "why")  # a different key is its own episode
    assert rec.titles == ["could not check k", "could not check other"]
