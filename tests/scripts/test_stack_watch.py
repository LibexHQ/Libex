"""
Tests for scripts/stack_watch.py: config, the decision table (Watcher.cycle),
the run loop's shutdown, and that no secret reaches a log or a Gotify body.

Seams: probes, rotate and the control client are patched at stack_watch's
import location; Gotify and gluetun are httpx.MockTransport. No network.
"""

# Standard library
import asyncio
import json
import logging
import os
import signal
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import httpx
import pytest

# Scripts
import scripts.stack_watch as sw

# Core
from libex_core.exceptions import AudibleAPIException

# Services
from app.services.watch import gluetun as gl
from app.services.watch import probes
from app.services.watch.gluetun import GluetunAuthError, RotationResult
from app.services.watch.gotify import Notifier
from app.services.watch.probes import ExitProbe

BASE = {"GOTIFY_URL": "https://gotify.example.com", "GOTIFY_TOKEN": "gtok"}
GOOD = ExitProbe({"us": "ok", "uk": "ok"})
BLOCKED = ExitProbe({"us": "block", "uk": "block"})
HALF = ExitProbe({"us": "block", "uk": "ok"})


def cfg_for(**extra):
    env = dict(BASE)
    env.update(extra)
    return sw.load_config(env)


# --- load_config -------------------------------------------------------------

@pytest.mark.parametrize("drop", ["GOTIFY_URL", "GOTIFY_TOKEN"])
def test_refuses_without_gotify_config(drop):
    env = dict(BASE)
    del env[drop]
    with pytest.raises(SystemExit):
        sw.load_config(env)


@pytest.mark.parametrize("env_extra", [
    {"GOTIFY_URL": "http://gotify.example.com"},
    {"GOTIFY_URL": "https://u:sekret@gotify.example.com"},
])
def test_refuses_bad_gotify_url_without_echo(env_extra):
    with pytest.raises(SystemExit) as err:
        cfg_for(**env_extra)
    assert "sekret" not in str(err.value)


@pytest.mark.parametrize("proxy", ["http://:8888", "gluetun:8888", "http://px:pw9@:8888"])
def test_refuses_hostless_proxy_without_echo(proxy):
    with pytest.raises(SystemExit) as err:
        cfg_for(AUDIBLE_PROXY_URL=proxy)
    assert "pw9" not in str(err.value)


def test_control_host_comes_from_the_proxy_hostname_only():
    cfg = cfg_for(AUDIBLE_PROXY_URL="http://user:pw@gluetun:8888", GLUETUN_API_KEY="k",
                  WATCH_CONTROL_PORT="9000")
    assert cfg.control_host == "gluetun"
    assert cfg.control_port == 9000
    assert cfg.rotation_on is True


def test_rotation_needs_both_proxy_and_key():
    assert cfg_for(AUDIBLE_PROXY_URL="http://gluetun:8888").rotation_on is False
    assert cfg_for(GLUETUN_API_KEY="k").rotation_on is False


@pytest.mark.parametrize("raw,expected", [
    ("1", 3), ("2", 3), ("3", 3), ("5", 5), ("0", 3), ("-4", 3), (None, 3),
])
def test_k_is_clamped_to_at_least_three(raw, expected):
    extra = {} if raw is None else {"WATCH_FAILS_BEFORE_ROTATE": raw}
    assert cfg_for(**extra).fails_before_rotate == expected


def test_non_integer_setting_is_refused():
    with pytest.raises(SystemExit):
        cfg_for(WATCH_INTERVAL_S="soon")


# --- helpers -----------------------------------------------------------------

class FakeControl:
    def __init__(self, status="running"):
        self._status = status
        self.we_stopped = False
        self.calls = []

    async def status(self):
        self.calls.append("status")
        if isinstance(self._status, Exception):
            raise self._status
        return self._status

    async def ensure_running(self):
        self.calls.append("ensure_running")

    async def aclose(self):
        pass


def make_watcher(control=None, with_rotation=True, **extra):
    env = {}
    if with_rotation:
        env.update(AUDIBLE_PROXY_URL="http://gluetun:8888", GLUETUN_API_KEY="k")
    env.update(extra)
    cfg = cfg_for(**env)
    notifier = MagicMock()
    notifier.send = AsyncMock(return_value=True)
    if control is None and with_rotation:
        control = FakeControl()
    return sw.Watcher(cfg, notifier, asyncio.Event(), control), notifier


def titles(notifier):
    return [c.args[0] for c in notifier.send.await_args_list]


async def run_cycles(w, n, exit_probe=GOOD, app_ok=True, rotate_result=None):
    """Runs n cycles with a fixed probe outcome; returns the rotate mock."""
    rot = AsyncMock(return_value=rotate_result or RotationResult("1.1.1.1", "2.2.2.2", True))
    with patch.object(sw, "probe_exit", AsyncMock(return_value=exit_probe)), \
         patch.object(sw, "probe_app", AsyncMock(return_value=app_ok)), \
         patch.object(sw, "rotate", rot):
        for _ in range(n):
            await w.cycle()
    return rot


# --- when to rotate ----------------------------------------------------------

async def test_one_region_failing_never_rotates():
    w, notifier = make_watcher()
    rot = await run_cycles(w, 10, exit_probe=HALF)
    rot.assert_not_awaited()
    notifier.send.assert_not_awaited()


async def test_other_class_failures_never_rotate():
    w, notifier = make_watcher()
    rot = await run_cycles(w, 10, exit_probe=ExitProbe({"us": "other", "uk": "other"}))
    rot.assert_not_awaited()
    notifier.send.assert_not_awaited()


async def test_k_minus_one_blocked_cycles_do_not_rotate_but_k_does():
    w, notifier = make_watcher()
    rot = await run_cycles(w, 2, exit_probe=BLOCKED)
    rot.assert_not_awaited()
    notifier.send.assert_not_awaited()
    rot = await run_cycles(w, 1, exit_probe=BLOCKED)
    rot.assert_awaited_once()


async def test_a_good_cycle_resets_the_streak():
    w, _ = make_watcher()
    await run_cycles(w, 2, exit_probe=BLOCKED)
    await run_cycles(w, 1, exit_probe=GOOD)
    rot = await run_cycles(w, 2, exit_probe=BLOCKED)
    rot.assert_not_awaited()


async def test_configured_k_above_three_is_honoured():
    w, _ = make_watcher(WATCH_FAILS_BEFORE_ROTATE="5")
    assert (await run_cycles(w, 4, exit_probe=BLOCKED)).await_count == 0
    assert (await run_cycles(w, 1, exit_probe=BLOCKED)).await_count == 1


# --- rotation outcomes -------------------------------------------------------

async def test_rotation_cap_then_cooldown_and_escalation_once():
    w, notifier = make_watcher(WATCH_COOLDOWN_S="1000")
    failed = RotationResult("1.1.1.1", "1.1.1.1", False)
    rot = await run_cycles(w, 3 + 8, exit_probe=BLOCKED, rotate_result=failed)
    # K=3 cycles to start, then one rotation per cycle up to the cap of 3, then none.
    assert rot.await_count == 3
    assert titles(notifier).count("still blocked") == 1
    assert w.cooldown_until is not None


async def test_cooldown_expiry_allows_rotation_again():
    w, notifier = make_watcher(WATCH_COOLDOWN_S="1000")
    failed = RotationResult("1.1.1.1", "1.1.1.1", False)
    clock = MagicMock()
    clock.monotonic.return_value = 100.0
    with patch.object(sw, "time", clock):
        rot = await run_cycles(w, 12, exit_probe=BLOCKED, rotate_result=failed)
        assert rot.await_count == 3
        clock.monotonic.return_value = 100.0 + 999
        assert (await run_cycles(w, 2, exit_probe=BLOCKED, rotate_result=failed)).await_count == 0
        clock.monotonic.return_value = 100.0 + 1001
        assert (await run_cycles(w, 1, exit_probe=BLOCKED, rotate_result=failed)).await_count == 1
    assert titles(notifier).count("still blocked") == 1  # alerts again only after re-escalating


async def test_failed_rotation_is_reported_as_failed_with_old_and_new_ip():
    w, notifier = make_watcher()
    await run_cycles(w, 3, exit_probe=BLOCKED, rotate_result=RotationResult("1.1.1.1", "1.1.1.1", False))
    assert titles(notifier) == ["exit blocked", "rotation failed"]
    assert "1.1.1.1 -> 1.1.1.1" in notifier.send.await_args_list[1].args[2]


async def test_successful_rotation_is_reported_as_rotated():
    w, notifier = make_watcher()
    await run_cycles(w, 3, exit_probe=BLOCKED)
    assert titles(notifier) == ["exit blocked", "rotated"]
    assert "1.1.1.1 -> 2.2.2.2" in notifier.send.await_args_list[1].args[2]


async def test_rotate_exception_is_reported_and_cycle_survives():
    w, notifier = make_watcher()
    with patch.object(sw, "probe_exit", AsyncMock(return_value=BLOCKED)), \
         patch.object(sw, "rotate", AsyncMock(side_effect=httpx.ReadError("x"))):
        for _ in range(3):
            await w.cycle()
    assert "rotation failed" in titles(notifier)


async def test_401_during_rotate_disables_rotation_and_alerts_once():
    w, notifier = make_watcher()
    rot = AsyncMock(side_effect=GluetunAuthError("401"))
    with patch.object(sw, "probe_exit", AsyncMock(return_value=BLOCKED)), \
         patch.object(sw, "rotate", rot):
        for _ in range(8):
            await w.cycle()
    assert rot.await_count == 1
    assert w.rotation_disabled is True
    assert titles(notifier).count("control key rejected") == 1


async def test_401_on_status_check_disables_rotation_and_alerts_once():
    control = FakeControl(status=GluetunAuthError("401"))
    w, notifier = make_watcher(control=control)
    rot = await run_cycles(w, 8, exit_probe=BLOCKED)
    rot.assert_not_awaited()
    assert control.calls == ["status"]  # not asked again once disabled
    # blocked but rotation unavailable: one "exit blocked" needs-you alert, plus the key one
    assert sorted(titles(notifier)) == ["control key rejected", "exit blocked"]


async def test_vpn_found_stopped_is_started_then_notified():
    control = FakeControl(status="stopped")
    w, notifier = make_watcher(control=control)
    order = []
    control.ensure_running = AsyncMock(side_effect=lambda: order.append("start"))
    notifier.send.side_effect = lambda *a: order.append("notify")
    await run_cycles(w, 1)
    assert order == ["start", "notify"]
    assert titles(notifier) == ["VPN was stopped"]


async def test_vpn_running_is_left_alone():
    control = FakeControl(status="running")
    w, notifier = make_watcher(control=control)
    await run_cycles(w, 3)
    assert "ensure_running" not in control.calls
    notifier.send.assert_not_awaited()


async def test_failed_start_does_not_claim_it_started():
    control = FakeControl(status="stopped")
    control.ensure_running = AsyncMock(side_effect=httpx.ReadError("x"))
    w, notifier = make_watcher(control=control)
    await run_cycles(w, 1)
    notifier.send.assert_not_awaited()


# --- decision table (with the app probe on) ----------------------------------

APP = {"WATCH_LIBEX_URL": "http://libex:3333"}


async def test_exit_ok_app_failing_k_cycles_is_stuck_with_no_gluetun_action():
    control = FakeControl()
    w, notifier = make_watcher(control=control, **APP)
    rot = await run_cycles(w, 2, exit_probe=GOOD, app_ok=False)
    notifier.send.assert_not_awaited()  # K-1: nothing yet
    rot = await run_cycles(w, 10, exit_probe=GOOD, app_ok=False)
    rot.assert_not_awaited()
    assert titles(notifier) == ["Libex stuck"]  # once per incident
    assert notifier.send.await_args.args[3] is True  # needs you
    assert set(control.calls) == {"status"}  # never started, stopped or rotated


async def test_exit_blocked_and_app_failing_rotates():
    w, notifier = make_watcher(**APP)
    rot = await run_cycles(w, 3, exit_probe=BLOCKED, app_ok=False)
    rot.assert_awaited_once()
    assert "exit blocked" in titles(notifier)
    assert "Libex stuck" not in titles(notifier)


async def test_exit_blocked_app_ok_alerts_once_and_never_rotates():
    w, notifier = make_watcher(**APP)
    rot = await run_cycles(w, 10, exit_probe=BLOCKED, app_ok=True)
    rot.assert_not_awaited()
    assert titles(notifier) == ["exit probe failing, Libex serving"]
    assert notifier.send.await_args.args[3] is False  # needs you: no


async def test_app_probe_flapping_does_not_alert_stuck():
    w, notifier = make_watcher(**APP)
    for ok in (False, False, True, False, False, True):
        await run_cycles(w, 1, exit_probe=GOOD, app_ok=ok)
    notifier.send.assert_not_awaited()


async def test_cleared_after_incident_exactly_once():
    w, notifier = make_watcher(**APP)
    await run_cycles(w, 3, exit_probe=GOOD, app_ok=False)
    assert titles(notifier) == ["Libex stuck"]
    await run_cycles(w, 5, exit_probe=GOOD, app_ok=True)
    assert titles(notifier) == ["Libex stuck", "cleared"]


async def test_no_cleared_without_an_incident():
    w, notifier = make_watcher(**APP)
    await run_cycles(w, 10, exit_probe=GOOD, app_ok=True)
    notifier.send.assert_not_awaited()


async def test_second_incident_alerts_again_after_cleared():
    w, notifier = make_watcher(**APP)
    await run_cycles(w, 3, exit_probe=GOOD, app_ok=False)
    await run_cycles(w, 1, exit_probe=GOOD, app_ok=True)
    await run_cycles(w, 3, exit_probe=GOOD, app_ok=False)
    assert titles(notifier) == ["Libex stuck", "cleared", "Libex stuck"]


async def test_cleared_after_rotation_incident_resets_the_cap():
    w, notifier = make_watcher()
    await run_cycles(w, 3, exit_probe=BLOCKED)
    assert w.rotations == 1
    await run_cycles(w, 1, exit_probe=GOOD)
    assert "cleared" in titles(notifier)
    assert w.rotations == 0


async def test_blocked_without_rotation_available_alerts_once_needs_you():
    w, notifier = make_watcher(with_rotation=False, AUDIBLE_PROXY_URL="http://gluetun:8888")
    rot = await run_cycles(w, 10, exit_probe=BLOCKED)
    rot.assert_not_awaited()
    assert titles(notifier) == ["exit blocked"]
    assert notifier.send.await_args.args[3] is True


# --- a failed send is retried, never marked open ----------------------------------

async def test_a_failed_send_opens_no_incident_and_sends_no_cleared():
    w, notifier = make_watcher(**APP)
    notifier.send.return_value = False
    await run_cycles(w, 3, exit_probe=GOOD, app_ok=False)
    assert titles(notifier) == ["Libex stuck"] and w.alerted == set()
    await run_cycles(w, 3, exit_probe=GOOD, app_ok=True)
    assert titles(notifier) == ["Libex stuck"]  # no "cleared" for an incident nobody saw


async def test_a_failed_send_is_retried_each_cycle_then_opens_once():
    w, notifier = make_watcher(**APP)
    notifier.send.return_value = False
    await run_cycles(w, 4, exit_probe=GOOD, app_ok=False)
    assert titles(notifier) == ["Libex stuck"] * 2  # cycle 3 and cycle 4 each try
    notifier.send.return_value = True
    await run_cycles(w, 3, exit_probe=GOOD, app_ok=False)
    assert titles(notifier) == ["Libex stuck"] * 3  # the next pass delivers, then it is quiet
    assert w.alerted == {"stuck"}
    await run_cycles(w, 1, exit_probe=GOOD, app_ok=True)
    assert titles(notifier) == ["Libex stuck"] * 3 + ["cleared"]


# --- decision records ---------------------------------------------------------------

def _decisions(caplog):
    return [r for r in caplog.records if r.getMessage().startswith("Watch: incident")]


async def test_an_opened_needs_you_incident_logs_a_warning_with_its_fields(caplog):
    w, _ = make_watcher(**APP)
    with caplog.at_level(logging.DEBUG, logger="libex"):
        await run_cycles(w, 3, exit_probe=GOOD, app_ok=False)
    (record,) = _decisions(caplog)
    assert record.levelno == logging.WARNING
    assert (record.stack, record.alert, record.needs_you) == ("stack", "Libex stuck", True)


async def test_an_opened_incident_that_needs_nobody_logs_info_and_a_clear_logs_info(caplog):
    w, _ = make_watcher(**APP)
    with caplog.at_level(logging.DEBUG, logger="libex"):
        await run_cycles(w, 3, exit_probe=BLOCKED, app_ok=True)  # "exit probe failing, Libex serving"
        await run_cycles(w, 1, exit_probe=GOOD, app_ok=True)
    opened, cleared = _decisions(caplog)
    assert (opened.getMessage(), opened.levelno, opened.needs_you) == ("Watch: incident opened", logging.INFO, False)
    assert (cleared.getMessage(), cleared.levelno) == ("Watch: incident cleared", logging.INFO)


async def test_a_failed_send_logs_no_decision(caplog):
    w, notifier = make_watcher(**APP)
    notifier.send.return_value = False
    with caplog.at_level(logging.DEBUG, logger="libex"):
        await run_cycles(w, 3, exit_probe=GOOD, app_ok=False)
    assert _decisions(caplog) == []


# --- rotation-failed and cleared wording ---------------------------------------------

async def _rotate_raising(control):
    w, notifier = make_watcher(control=control)
    with patch.object(sw, "rotate", AsyncMock(side_effect=httpx.ReadError("x"))):
        await w._rotate_or_escalate()
    failed = [c.args for c in notifier.send.await_args_list if c.args[0] == "rotation failed"]
    assert len(failed) == 1
    return failed[0]


async def test_rotation_failure_says_running_again_only_when_status_confirms_it():
    _, happened, did, needs_you = await _rotate_raising(FakeControl(status="running"))
    assert did == "the VPN is running again" and needs_you is False


@pytest.mark.parametrize("status", ["stopped", httpx.ReadError("x")])
async def test_rotation_failure_with_the_vpn_unconfirmed_asks_for_the_operator(status):
    _, happened, did, needs_you = await _rotate_raising(FakeControl(status=status))
    assert "running again" not in did
    assert "not confirmed running" in did
    assert needs_you is True


async def test_cleared_after_a_401_says_rotation_stays_off():
    w, notifier = make_watcher()
    with patch.object(sw, "probe_exit", AsyncMock(return_value=BLOCKED)), \
         patch.object(sw, "rotate", AsyncMock(side_effect=GluetunAuthError("401"))):
        for _ in range(3):
            await w.cycle()
    await run_cycles(w, 1, exit_probe=GOOD)
    cleared = [c.args for c in notifier.send.await_args_list if c.args[0] == "cleared"]
    assert len(cleared) == 1
    assert "rotation is still off" in cleared[0][1]


async def test_cleared_without_a_401_does_not_claim_rotation_is_off():
    w, notifier = make_watcher()
    await run_cycles(w, 3, exit_probe=BLOCKED)
    await run_cycles(w, 1, exit_probe=GOOD)
    (cleared,) = [c.args for c in notifier.send.await_args_list if c.args[0] == "cleared"]
    assert "rotation is still off" not in cleared[1]


# --- _run: shutdown ----------------------------------------------------------

def _run_patches(control, cycle):
    notifier = MagicMock()
    notifier.aclose = AsyncMock()
    return (
        patch.object(sw, "Notifier", return_value=notifier),
        patch.object(sw, "GluetunControl", return_value=control),
        patch.object(sw.Watcher, "cycle", cycle),
        patch.object(sw, "_ops_tasks", return_value=[]),
    )


def _control(we_stopped):
    c = FakeControl()
    c.we_stopped = we_stopped
    c.ensure_running = AsyncMock()
    c.aclose = AsyncMock()
    return c


def _rot_cfg():
    return cfg_for(AUDIBLE_PROXY_URL="http://gluetun:8888", GLUETUN_API_KEY="k",
                   WATCH_INTERVAL_S="3600")


async def test_sigterm_ends_loop_and_starts_vpn_we_stopped():
    control = _control(we_stopped=True)

    async def cycle(self):
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)

    p = _run_patches(control, cycle)
    with p[0], p[1], p[2], p[3]:
        code = await asyncio.wait_for(sw._run(_rot_cfg()), 5)
    assert code == 0
    control.ensure_running.assert_awaited_once()
    control.aclose.assert_awaited_once()


async def test_exit_leaves_vpn_alone_when_we_did_not_stop_it():
    control = _control(we_stopped=False)

    async def cycle(self):
        self.stop.set()

    p = _run_patches(control, cycle)
    with p[0], p[1], p[2], p[3]:
        assert await asyncio.wait_for(sw._run(_rot_cfg()), 5) == 0
    control.ensure_running.assert_not_awaited()


async def test_fatal_exception_still_starts_the_vpn_and_propagates():
    control = _control(we_stopped=True)

    async def cycle(self):
        raise asyncio.CancelledError()

    p = _run_patches(control, cycle)
    with p[0], p[1], p[2], p[3]:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(sw._run(_rot_cfg()), 5)
    control.ensure_running.assert_awaited_once()


async def test_failed_final_start_exits_nonzero():
    control = _control(we_stopped=True)
    control.ensure_running = AsyncMock(side_effect=httpx.ReadError("x"))

    async def cycle(self):
        self.stop.set()

    p = _run_patches(control, cycle)
    with p[0], p[1], p[2], p[3]:
        assert await asyncio.wait_for(sw._run(_rot_cfg()), 5) == 1


async def test_a_failing_cycle_does_not_stop_the_loop():
    control = _control(we_stopped=False)
    n = 0

    async def cycle(self):
        nonlocal n
        n += 1
        if n < 3:
            raise RuntimeError("boom")
        self.stop.set()

    cfg = cfg_for(AUDIBLE_PROXY_URL="http://gluetun:8888", GLUETUN_API_KEY="k", WATCH_INTERVAL_S="1")
    p = _run_patches(control, cycle)
    with p[0], p[1], p[2], p[3]:
        await asyncio.wait_for(sw._run(cfg), 10)
    assert n == 3


# --- secrets never leak ------------------------------------------------------

GL_KEY = "glk-Qw3Rt5Yu7Io9Pa1Sd"
G_TOKEN = "gtk-Zx2Cv4Bn6Mm8Lk0Jh"
PROXY_USER = "proxyuser-Ab12"
PROXY_PW = "proxypw-Cd34Ef56"
SECRETS = (GL_KEY, G_TOKEN, PROXY_USER, PROXY_PW)


async def test_no_secret_reaches_a_log_record_or_a_gotify_body(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(gl, "_POLL_INTERVAL_S", 0.01)
    env = {
        "GOTIFY_URL": "https://gotify.example.com", "GOTIFY_TOKEN": G_TOKEN,
        "AUDIBLE_PROXY_URL": f"http://{PROXY_USER}:{PROXY_PW}@gluetun:8888",
        "GLUETUN_API_KEY": GL_KEY, "WATCH_LIBEX_URL": "http://libex:3333",
    }
    cfg = sw.load_config(env)
    sw.report_config(cfg)

    bodies = []

    def gotify(request):
        bodies.append(request.url.query + request.content)
        return httpx.Response(200, json={})

    state = {"status": "running", "ip": "1.1.1.1", "n": 0}

    def control_handler(request):
        if request.method == "PUT":
            state["status"] = json.loads(request.content)["status"]
            state["n"] += 1
            state["ip"] = "" if state["status"] == "stopped" else f"2.2.2.{state['n']}"
            return httpx.Response(200, json={})
        if request.url.path == "/v1/vpn/status":
            return httpx.Response(200, json={"status": state["status"]})
        return httpx.Response(200, json={"public_ip": state["ip"]})

    notifier = Notifier(cfg.gotify_url, cfg.gotify_token, cfg.stack)
    notifier._http = httpx.AsyncClient(base_url=cfg.gotify_url, transport=httpx.MockTransport(gotify))
    control = gl.GluetunControl(cfg.control_host, cfg.control_port, cfg.gluetun_key)
    control._http = httpx.AsyncClient(base_url="http://gluetun:8000", transport=httpx.MockTransport(control_handler))

    async def blocked_fetch(get, asins, region):
        raise AudibleAPIException("blocked", upstream_status=403)

    w = sw.Watcher(cfg, notifier, asyncio.Event(), control)
    with patch.object(probes, "fetch_products", blocked_fetch), \
         patch.object(sw, "probe_app", AsyncMock(return_value=False)):
        for _ in range(3):
            await w.cycle()
    await notifier.aclose()
    await control.aclose()

    assert bodies, "the scenario must have sent something for this test to mean anything"
    assert any(b"rotat" in b for b in bodies)
    everything = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    for secret in SECRETS:
        assert secret not in everything
        assert all(secret.encode() not in b for b in bodies)
