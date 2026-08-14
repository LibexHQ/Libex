"""Tests for request-logging middleware.

Libex records nothing that identifies a caller. These tests are the guard on
that: they assert the client address never reaches the log record under any
header, and that caller-authored query values are replaced while the
operational fields a deploy is monitored by survive intact.
"""

# Standard library
import logging
import urllib.parse

# Third party
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Core
from app.core.middleware import _redact_query, _SAFE_QUERY_PARAMS, LoggingMiddleware


# ============================================================
# QUERY REDACTION
# ============================================================

# Every param whose value a caller authors. Keys survive; values must not.
CALLER_AUTHORED = ["name", "keywords", "title", "author", "narrator", "query", "search", "publisher"]


@pytest.mark.parametrize("param", CALLER_AUTHORED)
def test_redact_query_replaces_caller_authored_values(param):
    result = _redact_query(f"{param}=Some+Private+Text")
    assert "Some" not in result
    assert "Private" not in result
    assert result == f"{param}=REDACTED"


@pytest.mark.parametrize("param,value", [
    ("region", "us"),
    ("cache", "false"),
    ("limit", "10"),
    ("page", "2"),
    ("sort", "title"),
    ("order", "desc"),
    ("language", "english"),
])
def test_redact_query_keeps_structural_values(param, value):
    assert _redact_query(f"{param}={value}") == f"{param}={value}"


def test_redact_query_mixes_kept_and_redacted_in_one_string():
    result = _redact_query("region=us&name=Some+Person&page=2")
    parsed = dict(urllib.parse.parse_qsl(result))
    assert parsed["region"] == "us"
    assert parsed["page"] == "2"
    assert parsed["name"] == "REDACTED"


def test_redact_query_defaults_unknown_params_to_redacted():
    """An allowlist must fail closed: a param nobody has classified is redacted."""
    assert _redact_query("some_future_param=whatever") == "some_future_param=REDACTED"


def test_redact_query_sentinel_survives_urlencoding_readably():
    """A sentinel with <> comes back as %3C...%3E, unreadable in the logs."""
    assert "%3C" not in _redact_query("name=x")
    assert "REDACTED" in _redact_query("name=x")


@pytest.mark.parametrize("raw", ["", "%%%", "&&&", "=", "a" * 5000])
def test_redact_query_never_raises_on_hostile_input(raw):
    result = _redact_query(raw)
    assert isinstance(result, str)


def test_redact_query_leaks_nothing_on_malformed_input():
    """Falling back to the raw string would leak exactly what this withholds."""
    assert "Private" not in _redact_query("name=Private%ZZ&%%%")


def test_safe_params_excludes_every_caller_authored_field():
    assert _SAFE_QUERY_PARAMS.isdisjoint(CALLER_AUTHORED)


# ============================================================
# THE LOG RECORD
# ============================================================

def _app_with_logging():
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


async def _request(caplog, path, headers=None, client=("1.2.3.4", 1234)):
    transport = ASGITransport(app=_app_with_logging(), client=client)
    with caplog.at_level(logging.INFO):
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            await ac.get(path, headers=headers or {})
    return [r for r in caplog.records if r.getMessage() == "Request completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {},
    {"CF-Connecting-IP": "203.0.113.5"},
    {"x-real-ip": "203.0.113.5"},
    {"CF-Connecting-IP": "203.0.113.5", "x-real-ip": "198.51.100.7"},
])
async def test_no_client_address_reaches_the_log_record(caplog, headers):
    """The guard: no header and no connection can put an address in the log."""
    records = await _request(caplog, "/probe", headers)
    assert len(records) == 1
    record = records[0]

    assert not hasattr(record, "ip")
    serialised = str(record.__dict__)
    assert "203.0.113.5" not in serialised
    assert "198.51.100.7" not in serialised
    assert "1.2.3.4" not in serialised


@pytest.mark.asyncio
async def test_monitoring_fields_survive(caplog):
    """Per-endpoint failure rates depend on these; privacy work must not cost them."""
    records = await _request(caplog, "/probe?region=us")
    record = records[0]

    assert record.url == "/probe"
    assert record.method == "GET"
    assert record.status == 200
    assert isinstance(record.took, float)
    assert record.query == "region=us"
    assert hasattr(record, "userAgent")
    assert hasattr(record, "host")


@pytest.mark.asyncio
async def test_caller_authored_query_is_redacted_in_the_record(caplog):
    records = await _request(caplog, "/probe?name=Some+Person&region=us")
    record = records[0]

    assert "Some" not in record.query
    assert "region=us" in record.query
    assert "name=REDACTED" in record.query


@pytest.mark.asyncio
async def test_health_is_not_logged(caplog):
    assert await _request(caplog, "/health") == []
