"""
Tests for the gluetun control client and the rotation, against a simulated
control server (httpx.MockTransport). No network.
"""

# Standard library
import asyncio
import json

# Third party
import httpx
import pytest

# Services
from app.services.watch import gluetun
from app.services.watch.gluetun import (
    GluetunAuthError,
    GluetunControl,
    GluetunError,
    control_host,
    rotate,
)

KEY = "gl-key-Zx9Qw8Er7Ty6"


class FakeGluetun:
    """A control server: PUT stopped drops the IP, PUT running brings the next queued one."""

    def __init__(self, ips, status="running", fail_status_get=False, put_status=200):
        self.ips = list(ips)
        self.ip = self.ips.pop(0)
        self.status = status
        self.fail_status_get = fail_status_get
        self.put_status = put_status
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        path, method = request.url.path, request.method
        if method == "PUT" and path == "/v1/vpn/status":
            if self.put_status != 200:
                return httpx.Response(self.put_status)
            wanted = json.loads(request.content)["status"]
            self.status = wanted
            self.ip = "" if wanted == "stopped" else (self.ips.pop(0) if self.ips else self.ip)
            return httpx.Response(200, json={"outcome": wanted})
        if method == "GET" and path == "/v1/vpn/status":
            if self.fail_status_get:
                raise httpx.ReadError("boom")
            return httpx.Response(200, json={"status": self.status})
        if method == "GET" and path == "/v1/publicip/ip":
            return httpx.Response(200, json={"public_ip": self.ip})
        return httpx.Response(404)

    def puts(self):
        return [json.loads(r.content)["status"] for r in self.requests if r.method == "PUT"]


def _control(fake):
    ctl = GluetunControl("gluetun", 8000, KEY)
    ctl._http = httpx.AsyncClient(
        base_url="http://gluetun:8000", headers={"X-API-Key": KEY},
        transport=httpx.MockTransport(fake.handler),
    )
    return ctl


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(gluetun, "_POLL_INTERVAL_S", 0.01)


# --- control_host ------------------------------------------------------------

@pytest.mark.parametrize("url,host", [
    ("http://gluetun:8888", "gluetun"),
    ("http://user:p%40ss@gluetun:8888", "gluetun"),
    ("http://10.2.0.3:8888/", "10.2.0.3"),
    ("http://[fd00::2]:8888", "fd00::2"),
])
def test_control_host_is_the_proxy_hostname_only(url, host):
    assert control_host(url) == host


@pytest.mark.parametrize("url", ["", "gluetun:8888", "http://:8888", "http://user:secretpw@:8888", "http://[::1"])
def test_control_host_rejects_hostless_without_echo(url):
    with pytest.raises(ValueError) as err:
        control_host(url)
    assert "secretpw" not in str(err.value)


# --- client wiring -----------------------------------------------------------

async def test_client_sends_key_header_reads_no_env_and_follows_no_redirect():
    ctl = GluetunControl("gluetun", 8000, KEY)
    try:
        assert ctl._http.headers["X-API-Key"] == KEY
        assert ctl._http.trust_env is False
        assert ctl._http.follow_redirects is False
        assert KEY not in str(ctl._http.base_url)
    finally:
        await ctl.aclose()


async def test_ipv6_host_is_bracketed():
    ctl = GluetunControl("fd00::2", 8000, KEY)
    try:
        assert str(ctl._http.base_url).startswith("http://[fd00::2]:8000")
    finally:
        await ctl.aclose()


async def test_401_raises_auth_error_and_5xx_raises_plain_error():
    ctl = _control(FakeGluetun(["1.1.1.1"], put_status=401))
    with pytest.raises(GluetunAuthError):
        await ctl.set_status("running")
    ctl = _control(FakeGluetun(["1.1.1.1"], put_status=500))
    with pytest.raises(GluetunError) as err:
        await ctl.set_status("running")
    assert not isinstance(err.value, GluetunAuthError)


async def test_we_stopped_tracks_stop_even_when_the_put_fails():
    ctl = _control(FakeGluetun(["1.1.1.1"], put_status=500))
    with pytest.raises(GluetunError):
        await ctl.set_status("stopped")
    assert ctl.we_stopped is True  # an unanswered stop may have landed


async def test_we_stopped_clears_on_running():
    ctl = _control(FakeGluetun(["1.1.1.1"]))
    await ctl.set_status("stopped")
    assert ctl.we_stopped is True
    await ctl.ensure_running()
    assert ctl.we_stopped is False


# --- rotate ------------------------------------------------------------------

async def test_rotate_new_ip_is_ok_and_ends_running():
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"])
    result = await rotate(_control(fake), asyncio.Event(), timeout=1.0)
    assert (result.old_ip, result.new_ip, result.ok) == ("1.1.1.1", "2.2.2.2", True)
    assert fake.puts() == ["stopped", "running"]


async def test_rotate_same_ip_back_is_a_failed_rotation():
    fake = FakeGluetun(["1.1.1.1", "1.1.1.1"])
    result = await rotate(_control(fake), asyncio.Event(), timeout=1.0)
    assert (result.old_ip, result.new_ip, result.ok) == ("1.1.1.1", "1.1.1.1", False)
    assert fake.puts()[-1] == "running"


async def test_rotate_empty_ip_is_a_failed_rotation():
    fake = FakeGluetun(["1.1.1.1", ""])
    result = await rotate(_control(fake), asyncio.Event(), timeout=0.1)
    assert result.ok is False and result.new_ip == ""


async def test_rotate_exception_after_stop_still_puts_running():
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"], fail_status_get=True)
    with pytest.raises(httpx.ReadError):
        await rotate(_control(fake), asyncio.Event(), timeout=0.2)
    assert fake.puts() == ["stopped", "running"]


async def test_rotate_stop_requested_still_puts_running():
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"])
    stop = asyncio.Event()
    stop.set()
    result = await rotate(_control(fake), stop, timeout=1.0)
    assert fake.puts() == ["stopped", "running"]
    assert result.ok is False  # stopped waiting; never claims a rotation it did not see


async def test_rotate_401_on_the_stop_raises_auth_error_and_still_tries_running():
    fake = FakeGluetun(["1.1.1.1"], put_status=401)
    with pytest.raises(GluetunAuthError):
        await rotate(_control(fake), asyncio.Event(), timeout=0.2)
    # both PUTs were attempted: the stop, then the finally's running
    assert sum(r.method == "PUT" for r in fake.requests) == 2
