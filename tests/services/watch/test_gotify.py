"""
Tests for Gotify URL validation and the notifier's wire behaviour.
"""

# Third party
import httpx
import pytest

# Services
from app.services.watch.gotify import Notifier, validate_url

TOKEN = "gotify-tok-Ab12Cd34Ef56"


def _notifier(handler, url="https://gotify.example.com"):
    n = Notifier(url, TOKEN, "api")
    n._http = httpx.AsyncClient(
        base_url=url, headers={"X-Gotify-Key": TOKEN}, transport=httpx.MockTransport(handler)
    )
    return n


def _raise_connect(request):
    raise httpx.ConnectError("down")


# --- validate_url ------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://gotify.example.com",
    "https://gotify.example.com/",
    "http://gotify:80",
    "http://192.168.1.5:8080",
    "http://10.0.0.2",
    "http://127.0.0.1:8080",
    "http://nas.lan",
])
def test_validate_url_accepts(url):
    assert validate_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "https://user:pw@gotify.example.com",
    "https://user@gotify.example.com",
    "http://user:pw@192.168.1.5",
    "http://gotify.example.com",
    "http://8.8.8.8",
    "ftp://gotify.example.com",
    "https://",
    "gotify.example.com",
    "",
])
def test_validate_url_rejects(url):
    with pytest.raises(ValueError) as err:
        validate_url(url)
    assert "pw" not in str(err.value)


async def test_notifier_client_does_not_read_env_or_follow_redirects():
    n = Notifier("https://gotify.example.com", TOKEN, "api")
    try:
        assert n._http.follow_redirects is False
        assert n._http.trust_env is False
    finally:
        await n.aclose()


def test_notifier_refuses_bad_url():
    with pytest.raises(ValueError):
        Notifier("https://u:p@gotify.example.com", TOKEN, "api")


# --- send --------------------------------------------------------------------

async def test_send_token_only_in_header_and_body_shape():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    assert await _notifier(handler).send("exit blocked", "it broke", "rotated", True) is True
    (req,) = seen
    body = req.content.decode()
    assert req.headers["X-Gotify-Key"] == TOKEN
    assert TOKEN not in str(req.url)
    assert TOKEN not in body
    assert "[api] exit blocked" in body
    assert "Needs you: yes" in body
    assert "What happened: it broke" in body and "What I did: rotated" in body


async def test_send_needs_you_no():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    assert await _notifier(handler).send("w", "h", "d", False) is True
    assert "Needs you: no" in seen[0].content.decode()


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500),
    lambda r: httpx.Response(401),
    lambda r: httpx.Response(302, headers={"location": "https://evil.example"}),
    _raise_connect,
], ids=["500", "401", "redirect-not-followed", "connect-error"])
async def test_failed_send_returns_false_and_never_raises(handler, caplog):
    assert await _notifier(handler).send("w", "h", "d", True) is False
    assert TOKEN not in caplog.text
    assert "gotify.example.com" not in caplog.text
