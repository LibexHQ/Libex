"""
`libex-core config` and the environment it reads: how a proxy and the direct
egress switch resolve, and that no credential in either ever reaches an
output stream, an exception message, or a repr.
"""

import base64
import json
import socket
import threading
import traceback

import pytest

from libex_core.cli.environment import (
    ALLOW_DIRECT_EGRESS_VARIABLE,
    PROXY_URL_VARIABLE,
    Config,
    ConfigError,
    load_config,
)
from libex_core.cli.session import build_client
from tests.libex_core._cli_support import PROBE_SCRIPT, clean_env, run_python

USER = "alice"
PASSWORD_RAW = "p%40ss%3Aw0rd"
PASSWORD_DECODED = "p@ss:w0rd"
BASIC = base64.b64encode(f"{USER}:{PASSWORD_DECODED}".encode()).decode()

# Every spelling the credential could come back in.
CREDENTIAL_FORMS = (
    PASSWORD_RAW,
    PASSWORD_DECODED,
    f"{USER}:",
    BASIC,
    USER,
    f"{USER}:{PASSWORD_RAW}",
)


def credentialed(host="proxy.example.net", port=8080, scheme="http"):
    return f"{scheme}://{USER}:{PASSWORD_RAW}@{host}:{port}"


def assert_no_credential(*streams):
    for stream in streams:
        text = stream if isinstance(stream, str) else stream.decode("utf-8", "replace")
        for form in CREDENTIAL_FORMS:
            assert form not in text, f"{form!r} found in {text!r}"


# ============================================================
# RESOLUTION
# ============================================================

def test_no_environment_at_all_is_a_config_error(run_cli):
    result = run_cli(["config"])
    assert result.code == 5
    assert result.stdout == b""
    assert PROXY_URL_VARIABLE in result.err
    assert ALLOW_DIRECT_EGRESS_VARIABLE in result.err
    assert result.err.endswith("(code: config_error)\n")


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On ", "Yes"])
def test_a_truthy_direct_egress_switch_allows_direct(run_cli, value):
    result = run_cli(["config"], {ALLOW_DIRECT_EGRESS_VARIABLE: value})
    assert result.code == 0, result.err
    assert json.loads(result.out) == {"transport": {"mode": "direct", "host": None}}


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "OFF"])
def test_a_falsy_direct_egress_switch_is_refused_without_a_proxy(run_cli, value):
    result = run_cli(["config"], {ALLOW_DIRECT_EGRESS_VARIABLE: value})
    assert result.code == 5
    assert result.stdout == b""


@pytest.mark.parametrize("value", ["maybe", "2", "y", "t", "enabled", "zq-not-a-bool"])
def test_an_invalid_direct_egress_switch_is_a_config_error(run_cli, value):
    result = run_cli(["config"], {ALLOW_DIRECT_EGRESS_VARIABLE: value})
    assert result.code == 5
    assert result.stdout == b""
    assert ALLOW_DIRECT_EGRESS_VARIABLE in result.err


def test_an_invalid_switch_value_is_not_echoed_back(run_cli):
    result = run_cli(["config"], {ALLOW_DIRECT_EGRESS_VARIABLE: "zq-not-a-bool"})
    assert "zq-not-a-bool" not in result.err


def test_an_invalid_switch_is_found_even_when_a_proxy_is_set(run_cli):
    result = run_cli(
        ["config"],
        {PROXY_URL_VARIABLE: "http://proxy.example.net:8080", ALLOW_DIRECT_EGRESS_VARIABLE: "zq"},
    )
    assert result.code == 5


@pytest.mark.parametrize("switch", ["", "0", "1", "true"])
def test_a_proxy_is_used_whatever_the_direct_egress_switch_says(run_cli, switch):
    result = run_cli(
        ["config"],
        {PROXY_URL_VARIABLE: "http://proxy.example.net:8080", ALLOW_DIRECT_EGRESS_VARIABLE: switch},
    )
    assert result.code == 0, result.err
    assert json.loads(result.out) == {
        "transport": {"mode": "proxy", "host": "proxy.example.net"}
    }


def test_an_empty_proxy_variable_means_unset(run_cli):
    assert run_cli(["config"], {PROXY_URL_VARIABLE: ""}).code == 5
    result = run_cli(["config"], {PROXY_URL_VARIABLE: "", ALLOW_DIRECT_EGRESS_VARIABLE: "1"})
    assert json.loads(result.out)["transport"]["mode"] == "direct"


@pytest.mark.parametrize("name", ["AUDIBLE_PROXY_URL", "HTTPS_PROXY", "ALL_PROXY", "HTTP_PROXY", "https_proxy"])
def test_a_proxy_in_another_variable_is_never_used(run_cli, name):
    """Only LIBEX_CORE_* is read. A credentialed URL under any other name is
    ignored, so it neither configures a proxy nor leaks."""
    result = run_cli(["config"], {name: credentialed()})
    assert result.code == 5
    assert result.stdout == b""
    assert_no_credential(result.stdout, result.stderr)


@pytest.mark.parametrize("name", ["AUDIBLE_PROXY_URL", "HTTPS_PROXY", "ALL_PROXY"])
def test_another_variable_does_not_redirect_a_direct_run(run_cli, name):
    result = run_cli(
        ["config"], {name: credentialed(), ALLOW_DIRECT_EGRESS_VARIABLE: "1"}
    )
    assert result.code == 0
    assert json.loads(result.out)["transport"] == {"mode": "direct", "host": None}
    assert_no_credential(result.stdout, result.stderr)


def test_config_prints_the_host_only_never_the_url(run_cli):
    result = run_cli(["-vv", "config"], {PROXY_URL_VARIABLE: credentialed()})
    assert result.code == 0
    assert json.loads(result.out) == {
        "transport": {"mode": "proxy", "host": "proxy.example.net"}
    }
    assert_no_credential(result.stdout, result.stderr)
    assert "8080" not in result.out


# ============================================================
# CREDENTIALS NEVER APPEAR -- configuration failures
# ============================================================

_BAD_PROXY_URLS = {
    "schemeless user:pw@h:1": f"{USER}:{PASSWORD_RAW}@h:1",
    "bad scheme": f"socks5://{USER}:{PASSWORD_RAW}@proxy.example.net:1080",
    "ftp scheme": f"ftp://{USER}:{PASSWORD_RAW}@proxy.example.net:21",
    "no host": f"http://{USER}:{PASSWORD_RAW}@:8080",
    "port out of range": f"http://{USER}:{PASSWORD_RAW}@proxy.example.net:99999",
    "garbage": f"{USER}:{PASSWORD_RAW}@@@",
}


@pytest.mark.parametrize("name", sorted(_BAD_PROXY_URLS))
@pytest.mark.parametrize("verbosity", [[], ["-v"], ["-vv"]])
def test_a_rejected_proxy_url_exits_five_without_the_credential(run_cli, name, verbosity):
    result = run_cli([*verbosity, "config"], {PROXY_URL_VARIABLE: _BAD_PROXY_URLS[name]})
    assert result.code == 5
    assert result.stdout == b""
    assert PROXY_URL_VARIABLE in result.err
    assert_no_credential(result.stdout, result.stderr)


_INVALID_PROXY_TEXT = (
    f"{PROXY_URL_VARIABLE} is not a valid proxy URL: it needs an http or "
    "https scheme and a host (the port is optional)"
)


@pytest.mark.parametrize("name", sorted(_BAD_PROXY_URLS))
def test_a_rejected_proxy_url_gets_the_fixed_text_and_never_the_value(run_cli, name):
    result = run_cli(["config"], {PROXY_URL_VARIABLE: _BAD_PROXY_URLS[name]})
    assert result.code == 5
    assert result.stdout == b""
    assert result.err == f"libex-core: error: {_INVALID_PROXY_TEXT} (code: config_error)\n"
    assert _BAD_PROXY_URLS[name] not in result.err


@pytest.mark.parametrize(
    "url",
    ["ftp://proxy.example.net:21", "socks5://proxy.example.net:1080", "http://", "http://:8080"],
)
def test_an_invalid_proxy_url_without_credentials_gets_the_same_text(run_cli, url):
    result = run_cli(["config"], {PROXY_URL_VARIABLE: url})
    assert result.code == 5
    assert _INVALID_PROXY_TEXT in result.err
    assert url not in result.err.replace(_INVALID_PROXY_TEXT, "")


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://proxy.example.net", "proxy.example.net"),
        ("https://proxy.example.net", "proxy.example.net"),
        (f"http://{USER}:{PASSWORD_RAW}@proxy.example.net", "proxy.example.net"),
        ("http://proxy.example.net:3128", "proxy.example.net"),
    ],
    ids=["http-no-port", "https-no-port", "credentialed-no-port", "explicit-port"],
)
def test_a_proxy_url_without_a_port_is_accepted(run_cli, url, host):
    result = run_cli(["-vv", "config"], {PROXY_URL_VARIABLE: url})
    assert result.code == 0, result.err
    assert json.loads(result.out) == {"transport": {"mode": "proxy", "host": host}}
    assert_no_credential(result.stdout, result.stderr)


def test_a_port_less_proxy_url_builds_a_client():
    client = build_client(Config(proxy_url="http://proxy.example.net", allow_direct_egress=False))
    summary = client.transport_summary()
    assert (summary.mode, summary.host) == ("proxy", "proxy.example.net")


@pytest.mark.parametrize("name", sorted(_BAD_PROXY_URLS))
def test_a_rejected_proxy_url_leaves_no_credential_on_the_exception(name):
    config = Config(proxy_url=_BAD_PROXY_URLS[name], allow_direct_egress=False)
    with pytest.raises(ConfigError) as caught:
        build_client(config)
    error = caught.value
    assert error.__cause__ is None
    assert error.__suppress_context__
    rendered = "".join(traceback.format_exception(error))
    assert_no_credential(str(error), repr(error), repr(error.args), rendered)


def test_config_repr_and_str_carry_no_credential():
    config = Config(proxy_url=credentialed(), allow_direct_egress=True)
    assert_no_credential(repr(config), str(config), repr([config]), f"{config!r}")
    assert "allow_direct_egress=True" in repr(config)
    assert config == Config(proxy_url="http://other:1", allow_direct_egress=True)


def test_config_error_carries_no_value_from_the_environment(monkeypatch):
    monkeypatch.setenv(PROXY_URL_VARIABLE, credentialed())
    monkeypatch.setenv(ALLOW_DIRECT_EGRESS_VARIABLE, f"{USER}:{PASSWORD_RAW}")
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert_no_credential(str(caught.value), repr(caught.value), repr(caught.value.args))


def test_load_config_reads_both_variables(monkeypatch):
    monkeypatch.setenv(PROXY_URL_VARIABLE, "http://h:1")
    monkeypatch.setenv(ALLOW_DIRECT_EGRESS_VARIABLE, "yes")
    config = load_config()
    assert config.proxy_url == "http://h:1" and config.allow_direct_egress is True


# ============================================================
# CREDENTIALS NEVER APPEAR -- failures after the proxy is reached
# ============================================================

class _StubProxy:
    """Answers the first CONNECT with 407 and keeps what it was sent, so a
    test can show the credential really was on the wire before it asserts the
    credential is nowhere in the output."""

    def __init__(self):
        self.received = b""
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._server.settimeout(10)
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        with conn:
            self.received = conn.recv(4096)
            conn.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Proxy-Authenticate: Basic realm=stub\r\n"
                b"Content-Length: 0\r\n\r\n"
            )

    def close(self):
        self._server.close()
        self._thread.join(timeout=5)


def _probe(tmp_path, proxy_url, verbosity=("-vv",), **env):
    return run_python(
        ["-c", PROBE_SCRIPT, *verbosity, "probe"],
        env=clean_env(**{PROXY_URL_VARIABLE: proxy_url}, **env),
        cwd=tmp_path,
        timeout=25,
    )


def _closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize("verbosity", [("-vv",), ("-v",), ()])
def test_a_407_from_the_proxy_leaves_the_credential_out_of_every_stream(tmp_path, verbosity):
    stub = _StubProxy()
    try:
        result = _probe(tmp_path, credentialed("127.0.0.1", stub.port), verbosity)
    finally:
        stub.close()
    assert f"Basic {BASIC}".encode() in stub.received, "the credential never reached the proxy"
    assert result.returncode == 4
    assert result.stdout == b""
    assert result.stderr.strip().splitlines()[-1].startswith(b"libex-core: error:")
    assert_no_credential(result.stdout, result.stderr)


def test_a_407_traceback_is_present_at_vv_and_still_clean(tmp_path):
    stub = _StubProxy()
    try:
        result = _probe(tmp_path, credentialed("127.0.0.1", stub.port))
    finally:
        stub.close()
    assert b"Traceback (most recent call last)" in result.stderr
    assert b"407 Proxy Authentication Required" in result.stderr
    assert_no_credential(result.stdout, result.stderr)


def test_connection_refused_leaves_the_credential_out_of_every_stream(tmp_path):
    result = _probe(tmp_path, credentialed("127.0.0.1", _closed_port()))
    assert result.returncode == 4
    assert result.stdout == b""
    assert b"Traceback (most recent call last)" in result.stderr
    assert_no_credential(result.stdout, result.stderr)


def test_an_unresolvable_host_leaves_the_credential_out_of_every_stream(tmp_path):
    result = _probe(tmp_path, credentialed("no-such-host.invalid", 8080))
    assert result.returncode == 4
    assert result.stdout == b""
    assert b"Traceback (most recent call last)" in result.stderr
    assert_no_credential(result.stdout, result.stderr)


@pytest.mark.parametrize("name", sorted(_BAD_PROXY_URLS))
def test_a_rejected_url_in_a_real_process_leaves_the_credential_out(tmp_path, name):
    result = _probe(tmp_path, _BAD_PROXY_URLS[name])
    assert result.returncode == 5
    assert result.stdout == b""
    assert_no_credential(result.stdout, result.stderr)
