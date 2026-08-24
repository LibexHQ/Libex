"""
Tests for X-Request-Id: minted once per request by LoggingMiddleware, read
back (never re-minted) by app.main's generic exception handler for an
unhandled 500, and never taken from a caller-supplied value.

The 500 path needs a route that actually raises, so a handful of tests here
add one to a freshly reloaded `app.main` instance (the same
importlib.reload dance tests/test_migration_notice.py's `app_with_env`
fixture already uses) rather than mutating the shared `app` object every
other test module imports at collection time.
"""

# Standard library
import importlib
import logging
import re
import uuid

# Third party
import pytest
from fastapi.testclient import TestClient

# Local
import app.main as main_module
from app.core.config import get_settings

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@pytest.fixture
def fresh_app():
    """A freshly reloaded app.main.app, isolated from the shared one every
    other test module holds a reference to. Needed here because these tests
    add a route that deliberately raises -- doing that on the shared `app`
    object would leave a `/test-boom` route sitting on it for the rest of
    the session."""
    get_settings.cache_clear()
    importlib.reload(main_module)
    try:
        yield main_module.app
    finally:
        get_settings.cache_clear()
        importlib.reload(main_module)
        get_settings.cache_clear()


# ============================================================
# PRESENCE, SHAPE, UNIQUENESS
# ============================================================


def test_present_on_a_normal_200(client):
    response = client.get("/health")
    assert "x-request-id" in response.headers


def test_present_on_health_specifically(client):
    """/health returns early inside LoggingMiddleware, before the rest of
    its dispatch runs -- the header must still be stamped on that early
    return, not only on the general path."""
    response = client.get("/health")
    assert "x-request-id" in response.headers


def test_present_on_a_404(client):
    response = client.get("/this-route-does-not-exist")
    assert response.status_code == 404
    assert "x-request-id" in response.headers


def test_value_is_a_valid_uuid4(client):
    response = client.get("/health")
    value = response.headers["x-request-id"]
    assert _UUID4_RE.match(value), f"{value!r} is not a valid uuid4"
    # Belt and braces: the stdlib's own parser must also accept it as a v4.
    parsed = uuid.UUID(value)
    assert parsed.version == 4


def test_differs_across_two_requests(client):
    first = client.get("/health").headers["x-request-id"]
    second = client.get("/health").headers["x-request-id"]
    assert first != second


def test_caller_supplied_request_id_is_not_echoed(client):
    """A caller-chosen id is never trusted back onto the wire -- it would
    let that caller correlate their own requests with each other, or with
    someone else's, using a value Libex never generated itself."""
    supplied = "11111111-1111-4111-8111-111111111111"
    response = client.get("/health", headers={"X-Request-Id": supplied})
    assert response.headers["x-request-id"] != supplied


# ============================================================
# UNHANDLED 500 -- header, log record, and CORS
# ============================================================


def test_present_on_an_unhandled_500(fresh_app):
    @fresh_app.get("/test-boom-present")
    def boom():
        raise RuntimeError("boom")

    response = TestClient(fresh_app, raise_server_exceptions=False).get("/test-boom-present")
    assert response.status_code == 500
    assert "x-request-id" in response.headers


def test_500_header_value_equals_the_value_in_the_error_log_record(fresh_app, caplog):
    """The one read app.main's generic_exception_handler does of
    request.state.request_id feeds both the header and the log line --
    assert the two are actually equal, not merely that both are present."""

    @fresh_app.get("/test-boom-log-match")
    def boom():
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="libex"):
        response = TestClient(fresh_app, raise_server_exceptions=False).get("/test-boom-log-match")

    assert response.status_code == 500
    header_value = response.headers["x-request-id"]

    error_records = [
        r for r in caplog.records
        if r.name == "libex" and "Unhandled exception" in r.getMessage()
    ]
    assert len(error_records) == 1
    logged_request_id = getattr(error_records[0], "request_id", None)
    assert logged_request_id is not None
    assert logged_request_id == header_value


def test_500_with_origin_carries_cors_headers(fresh_app):
    """ServerErrorMiddleware sits outside CORSMiddleware, so an unhandled
    500 gets no CORS treatment unless app.main's handler applies it by
    hand -- and only when the request actually carried Origin, mirroring a
    real CORS preflight's own condition."""

    @fresh_app.get("/test-boom-origin")
    def boom():
        raise RuntimeError("boom")

    response = TestClient(fresh_app, raise_server_exceptions=False).get(
        "/test-boom-origin", headers={"Origin": "https://example.com"}
    )
    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-expose-headers" in response.headers


def test_500_without_origin_carries_neither_cors_header(fresh_app):
    @fresh_app.get("/test-boom-no-origin")
    def boom():
        raise RuntimeError("boom")

    response = TestClient(fresh_app, raise_server_exceptions=False).get("/test-boom-no-origin")
    assert response.status_code == 500
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-expose-headers" not in response.headers
