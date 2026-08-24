"""
Tests for the libexdb.com migration notice.

Off by default — the notice only fires when the public instance sets
MIGRATION_NOTICE_ENABLED and its companion settings. Self-hosters who set
nothing are not quite byte-identical: `Access-Control-Expose-Headers` names
Deprecation/Sunset/Link on any response carrying an `Origin` header
regardless. A request with no `Origin` genuinely is byte-identical. Two
directions matter equally here: proving the default path stays silent
(modulo that one CORS header), and proving the enabled path emits exactly
the spec-required header values.

`app.core.migration_notice.build_migration_notice` is the single predicate
every consumer (the middleware, `/health`, the OpenAPI description/`servers`,
and the generic 500 handler) shares — none of them re-derives the condition.
It is computed once at `app.main` import time from `settings = get_settings()`
— not per request. The only way to exercise the enabled branch through a real
request is therefore a fresh import with the env vars set beforehand, which
`app_with_env` below does via `importlib.reload`. The reload only replaces
the `app.main.app` name binding inside `app.main` itself; every other test
module already holds its own `app` reference captured via
`from app.main import app` at collection time (before any test runs), so this
never disturbs the rest of the suite.

Host-gating (the same container serving both the old and new hostnames) is
exercised through real requests with an explicit `Host` header, never a raw
call to `is_new_host_request` with a port attached — port-stripping is done
upstream by Starlette's `Request.url.hostname`, which is exactly what every
production call site passes in, so a unit call bypassing that would test a
contract the function was never given.
"""

# Standard library
import importlib
import logging
import os
from contextlib import contextmanager

# Third party
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Local
import app.main as main_module
from app.core.config import Settings, get_settings
from app.core.middleware import MigrationNoticeMiddleware, setup_middleware
from app.core.migration_notice import (
    MIGRATION_HEADER_NAMES,
    build_migration_notice,
    is_new_host_request,
)

# The real configured values from the runbook — asserted byte-exact, not as
# substrings, since both wire formats are spec-sensitive.
ENABLED_SETTINGS_KWARGS = {
    "migration_notice_enabled": True,
    "migration_new_host": "https://libexdb.com",
    "migration_announced": "2026-08-06",
    "migration_sunset": "2026-11-04",
    "migration_info_url": "https://github.com/LibexHQ/Libex/issues/999",
}

ENABLED_ENV = {
    "MIGRATION_NOTICE_ENABLED": "true",
    "MIGRATION_NEW_HOST": "https://libexdb.com",
    "MIGRATION_ANNOUNCED": "2026-08-06",
    "MIGRATION_SUNSET": "2026-11-04",
    "MIGRATION_INFO_URL": "https://github.com/LibexHQ/Libex/issues/999",
}

OLD_HOST_HEADER = "libex.lostcartographer.xyz"
# Four ways a request can arrive on the new host — plain, uppercase, the
# default HTTPS port spelled out, and the trailing-dot FQDN form. A naive
# `==` on the raw Host header (no `.lower()`, no port strip, no trailing-dot
# strip) suppresses correctly only for the first of these.
NEW_HOST_HEADER_VARIANTS = [
    "libexdb.com",
    "LIBEXDB.COM",
    "libexdb.com:443",
    "libexdb.com.",
]


@pytest.fixture
def app_with_env():
    """
    Factory fixture: reloads `app.main` with the given env vars applied,
    yields the app, and restores afterwards. `get_settings` is `lru_cache`d,
    so it is cleared both before the reload (so the fresh env vars are read)
    and after restoring (so the disabled Settings this leaves behind, rather
    than whatever the test set, is what `get_settings()` returns for anything
    that calls it afterwards).
    """

    @contextmanager
    def _reload(env: dict):
        for key, value in env.items():
            os.environ[key] = value
        get_settings.cache_clear()
        importlib.reload(main_module)
        try:
            yield main_module.app
        finally:
            for key in env:
                os.environ.pop(key, None)
            get_settings.cache_clear()
            importlib.reload(main_module)
            get_settings.cache_clear()

    return _reload


@pytest.fixture
def enabled_app(app_with_env):
    """The fully-enabled, real-world (per the runbook) migration config."""
    with app_with_env(ENABLED_ENV) as app:
        yield app


def _bare_app_with_migration_notice(notice) -> FastAPI:
    """
    A minimal app run through the real `setup_middleware`, given a
    `MigrationNotice` (or `None`) directly. `setup_middleware` takes the
    notice as a plain argument rather than reading settings itself, so there
    is no `lru_cache`d `get_settings()` to patch or clear for this path —
    only the full-app `enabled_app`/`app_with_env` fixtures above, which
    reload `app.main`, need that dance.
    """
    bare = FastAPI()

    @bare.get("/probe")
    def probe():
        return {"ok": True}

    setup_middleware(bare, notice)
    return bare


# ============================================================
# DEFAULT SETTINGS — byte-identical for self-hosters
# ============================================================


def test_build_migration_notice_default_settings_returns_none():
    """The predicate itself is a no-op with a bare default Settings()."""
    assert build_migration_notice(Settings()) is None


def test_setup_middleware_skips_registering_migration_middleware_by_default():
    """
    The gating happens once, at registration time — not per request. Prove
    the actual behaviour (no headers on a real response through the real
    middleware stack), not just the predicate's return value, and confirm the
    middleware class was never added to the stack at all.
    """
    bare = _bare_app_with_migration_notice(build_migration_notice(Settings()))

    assert MigrationNoticeMiddleware not in [m.cls for m in bare.user_middleware]

    response = TestClient(bare).get("/probe")
    for header in MIGRATION_HEADER_NAMES:
        assert header not in response.headers


def test_real_default_app_request_carries_no_migration_headers(client):
    """A real request through the shipped app, with default settings, carries
    none of the migration headers."""
    response = client.get("/health")
    for header in MIGRATION_HEADER_NAMES:
        assert header not in response.headers


def test_health_default_has_no_notice_key(client):
    """/health with default settings returns exactly {status, version} —
    no notice key leaks into it."""
    response = client.get("/health")
    data = response.json()
    assert set(data.keys()) == {"status", "version"}


def test_openapi_has_no_servers_entry_by_default(client):
    """Guards the self-hoster case explicitly: with the notice off,
    openapi.json must have no `servers` entry pointing at the public host,
    or a self-hoster's generated SDK would target someone else's server."""
    spec = client.get("/openapi.json").json()
    assert "servers" not in spec


def test_openapi_description_has_no_migration_notice_by_default(client):
    """The OpenAPI description is also untouched by default."""
    spec = client.get("/openapi.json").json()
    assert "Migration notice" not in spec["info"]["description"]
    assert "libexdb.com" not in spec["info"]["description"]


# ============================================================
# ENABLED PATH — exact header values and edge cases
# ============================================================


def test_deprecation_header_is_rfc9745_epoch_seconds():
    """RFC 9745 §3: Deprecation is a structured-field Date — @ + epoch
    seconds, not an HTTP-date."""
    notice = build_migration_notice(Settings(**ENABLED_SETTINGS_KWARGS))
    assert notice.headers["Deprecation"] == "@1785974400"


def test_sunset_header_is_rfc8594_imf_fixdate():
    """RFC 8594: Sunset is an IMF-fixdate HTTP-date with a literal,
    case-sensitive GMT — never UTC, never an offset, and the weekday must be
    correct for the date."""
    notice = build_migration_notice(Settings(**ENABLED_SETTINGS_KWARGS))
    assert notice.headers["Sunset"] == "Wed, 04 Nov 2026 00:00:00 GMT"


def test_link_header_carries_canonical_and_deprecation_rel():
    notice = build_migration_notice(Settings(**ENABLED_SETTINGS_KWARGS))
    assert notice.headers["Link"] == (
        '<https://libexdb.com>; rel="canonical", '
        '<https://github.com/LibexHQ/Libex/issues/999>; rel="deprecation"; type="text/html"'
    )


def test_link_header_omits_deprecation_rel_without_info_url():
    kwargs = {**ENABLED_SETTINGS_KWARGS, "migration_info_url": ""}
    notice = build_migration_notice(Settings(**kwargs))
    assert notice.headers["Link"] == '<https://libexdb.com>; rel="canonical"'


def test_sunset_before_announced_yields_none_and_logs_warning(caplog):
    """RFC 9745 §4: Sunset MUST NOT be earlier than Deprecation. Rather than
    emit a spec-violating pair, the code refuses and logs instead."""
    kwargs = {
        **ENABLED_SETTINGS_KWARGS,
        "migration_announced": "2026-11-04",
        "migration_sunset": "2026-08-06",
    }
    with caplog.at_level(logging.WARNING, logger="libex"):
        notice = build_migration_notice(Settings(**kwargs))
    assert notice is None
    assert any("RFC 9745" in record.getMessage() for record in caplog.records)


def test_inverted_dates_refuse_silently_to_the_client_while_logging(caplog):
    """The refusal is silent to the client (no headers, no error, no 500)
    but not silent in the logs — through a real middleware stack, not just
    the predicate."""
    inverted = Settings(
        **{
            **ENABLED_SETTINGS_KWARGS,
            "migration_announced": "2026-11-04",
            "migration_sunset": "2026-08-06",
        }
    )
    with caplog.at_level(logging.WARNING, logger="libex"):
        notice = build_migration_notice(inverted)
        bare = _bare_app_with_migration_notice(notice)
        response = TestClient(bare).get("/probe")

    assert notice is None
    assert response.status_code == 200
    for header in MIGRATION_HEADER_NAMES:
        assert header not in response.headers
    assert any("RFC 9745" in record.getMessage() for record in caplog.records)


def test_unparseable_dates_yield_none_and_logs_warning(caplog):
    kwargs = {**ENABLED_SETTINGS_KWARGS, "migration_announced": "not-a-date"}
    with caplog.at_level(logging.WARNING, logger="libex"):
        notice = build_migration_notice(Settings(**kwargs))
    assert notice is None
    assert any("ISO dates" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "missing_field",
    ["migration_new_host", "migration_announced", "migration_sunset"],
)
def test_partial_config_yields_none_and_names_the_missing_field(missing_field, caplog):
    """Unparseable or partially-filled config yields no notice — no
    half-built header, no exception, no 500 — and the warning names exactly
    which field was missing, so an operator can fix it without guessing."""
    kwargs = {**ENABLED_SETTINGS_KWARGS, missing_field: ""}
    with caplog.at_level(logging.WARNING, logger="libex"):
        notice = build_migration_notice(Settings(**kwargs))
    assert notice is None
    assert any(missing_field in record.getMessage() for record in caplog.records)


def test_whitespace_only_new_host_is_treated_as_missing(caplog):
    """A whitespace-only value used to pass the truthiness check and produce
    a header like `<   >; rel="canonical"`. Values are stripped before the
    missing-field check now, so whitespace-only counts as absent."""
    kwargs = {**ENABLED_SETTINGS_KWARGS, "migration_new_host": "   "}
    with caplog.at_level(logging.WARNING, logger="libex"):
        notice = build_migration_notice(Settings(**kwargs))
    assert notice is None
    assert any("migration_new_host" in record.getMessage() for record in caplog.records)


def test_surrounding_whitespace_on_new_host_is_stripped_not_baked_into_the_header():
    """
    A whitespace-only value is also caught by the separate parseable-hostname
    check below, so it alone doesn't prove `.strip()` is what is doing the
    work. This uses whitespace *around* an otherwise-valid URL instead —
    `urlparse` still finds a hostname there even without stripping, so this
    is the case that only the `.strip()` call, specifically, protects: an
    operator's env var with a stray trailing newline or leading space must
    not leak into the wire-format `Link` header as literal whitespace.
    """
    kwargs = {**ENABLED_SETTINGS_KWARGS, "migration_new_host": "  https://libexdb.com  "}
    notice = build_migration_notice(Settings(**kwargs))
    assert notice is not None
    assert notice.new_host == "https://libexdb.com"
    assert notice.headers["Link"].startswith('<https://libexdb.com>; rel="canonical"')


def test_successful_activation_logs_one_info_line(caplog):
    """A working config logs exactly one INFO line naming the new host and
    sunset date — previously silent even on the happy path."""
    with caplog.at_level(logging.INFO, logger="libex"):
        notice = build_migration_notice(Settings(**ENABLED_SETTINGS_KWARGS))
    assert notice is not None
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    message = info_records[0].getMessage()
    assert "https://libexdb.com" in message
    assert "2026-11-04" in message


def test_headers_appear_on_404_route_miss(enabled_app):
    """A 404 route-miss still goes through MigrationNoticeMiddleware
    normally — Starlette resolves it inside the middleware stack, not
    through ServerErrorMiddleware. This does NOT cover an unhandled
    exception's 500, which bypasses every middleware registered in
    setup_middleware entirely; see the HOST-GATING section below for that
    path, which app.main's generic exception handler applies the headers to
    itself."""
    response = TestClient(enabled_app).get("/this-route-does-not-exist")
    assert response.status_code == 404
    assert response.headers["Deprecation"] == "@1785974400"
    assert response.headers["Sunset"] == "Wed, 04 Nov 2026 00:00:00 GMT"
    assert "libexdb.com" in response.headers["Link"]


def test_expose_headers_lists_migration_headers_unconditionally(client):
    """Access-Control-Expose-Headers is unconditional — present even with the
    notice off — because without it those headers are invisible to browser
    JavaScript, and a known third-party integrator is browser-based.

    Asserts membership, not the exact list: the value is now the union of
    the migration headers with the response_headers.py registry
    (app.core.response_headers.EXPOSED_HEADER_NAMES), and a literal string
    here would break every time a header is added to that registry for a
    reason having nothing to do with the migration notice. Registry-level
    exactness — the full union, in order, deduped — is
    tests/test_response_headers.py's job, not this file's; this test's job
    is only that the migration headers themselves are always present."""
    response = client.get("/health", headers={"Origin": "https://example.com"})
    exposed = response.headers["access-control-expose-headers"].split(", ")
    for name in MIGRATION_HEADER_NAMES:
        assert name in exposed


def test_enabled_health_gains_notice(enabled_app):
    response = TestClient(enabled_app).get("/health")
    data = response.json()
    assert data["notice"] == (
        "Libex is moving to https://libexdb.com; this host stops serving after 2026-11-04."
    )


def test_enabled_openapi_servers_entry_targets_new_host(enabled_app):
    """`servers` appears only when enabled, and points at the new host so
    generated SDKs target it."""
    spec = TestClient(enabled_app).get("/openapi.json").json()
    assert spec["servers"] == [{"url": "https://libexdb.com"}]


# ============================================================
# ONE SOURCE OF TRUTH — no consumer can render a notice alone
# ============================================================


def test_partial_config_produces_consistent_silence_everywhere(app_with_env, caplog):
    """
    A half-configured environment (MIGRATION_SUNSET missing here) must stay
    silent in EVERY consumer — no headers, no `/health` notice, no OpenAPI
    `servers`/description notice — not merely a `None` from the predicate.
    Regression guard for the old `main.py`, which gated on
    `enabled and new_host` while the middleware separately required both
    valid dates, so a missing `MIGRATION_SUNSET` used to produce
    "...stops serving after ." on `/health` with no headers at all — a
    half-rendered notice, not a consistently silent one.
    """
    partial_env = {
        "MIGRATION_NOTICE_ENABLED": "true",
        "MIGRATION_NEW_HOST": "https://libexdb.com",
        "MIGRATION_ANNOUNCED": "2026-08-06",
        # MIGRATION_SUNSET intentionally absent.
    }
    with caplog.at_level(logging.WARNING, logger="libex"):
        with app_with_env(partial_env) as app:
            probe_client = TestClient(app)
            health = probe_client.get("/health")
            spec = probe_client.get("/openapi.json").json()

    assert health.status_code == 200
    for header in MIGRATION_HEADER_NAMES:
        assert header not in health.headers
    assert "notice" not in health.json()
    assert "servers" not in spec
    assert "Migration notice" not in spec["info"]["description"]
    assert "libexdb.com" not in spec["info"]["description"]
    assert any("migration_sunset" in record.getMessage() for record in caplog.records)


# ============================================================
# HOST-GATING — the same container serves both hostnames
# ============================================================
#
# `libexdb.com`, `LIBEXDB.COM`, `libexdb.com:443`, and `libexdb.com.` must
# ALL be recognised as the new host once it starts serving the same
# container as the old host, and suppress every trace of the notice — on a
# 200, a 404, and an unhandled 500. A naive case-sensitive, port-blind,
# trailing-dot-blind string compare passes only the plain lowercase form.


@pytest.mark.parametrize("host_header", NEW_HOST_HEADER_VARIANTS)
def test_new_host_variants_suppress_notice_on_200(enabled_app, host_header):
    response = TestClient(enabled_app).get("/health", headers={"Host": host_header})
    assert response.status_code == 200
    for header in MIGRATION_HEADER_NAMES:
        assert header not in response.headers
    assert "notice" not in response.json()


def test_old_host_gets_full_notice_on_200(enabled_app):
    response = TestClient(enabled_app).get("/health", headers={"Host": OLD_HOST_HEADER})
    assert response.status_code == 200
    assert response.headers["Deprecation"] == "@1785974400"
    assert response.headers["Sunset"] == "Wed, 04 Nov 2026 00:00:00 GMT"
    assert "libexdb.com" in response.headers["Link"]
    assert response.json()["notice"] == (
        "Libex is moving to https://libexdb.com; this host stops serving after 2026-11-04."
    )


@pytest.mark.parametrize("host_header", NEW_HOST_HEADER_VARIANTS)
def test_new_host_variants_suppress_headers_on_404(enabled_app, host_header):
    response = TestClient(enabled_app).get(
        "/this-route-does-not-exist", headers={"Host": host_header}
    )
    assert response.status_code == 404
    for header in MIGRATION_HEADER_NAMES:
        assert header not in response.headers


def test_old_host_gets_headers_on_404(enabled_app):
    response = TestClient(enabled_app).get(
        "/this-route-does-not-exist", headers={"Host": OLD_HOST_HEADER}
    )
    assert response.status_code == 404
    assert response.headers["Deprecation"] == "@1785974400"
    assert response.headers["Sunset"] == "Wed, 04 Nov 2026 00:00:00 GMT"
    assert "libexdb.com" in response.headers["Link"]


@pytest.mark.parametrize("host_header", NEW_HOST_HEADER_VARIANTS)
def test_new_host_variants_suppress_headers_on_unhandled_500(enabled_app, host_header):
    """
    ServerErrorMiddleware sits outside every middleware added in
    setup_middleware, so a bare, unhandled exception never reaches
    MigrationNoticeMiddleware — app.main's generic_exception_handler applies
    the same headers itself, and must apply the same host-gating.
    """

    @enabled_app.get(f"/test-boom-{host_header.replace(':', '-').replace('.', '_')}")
    def boom():
        raise RuntimeError("boom")

    response = TestClient(enabled_app, raise_server_exceptions=False).get(
        f"/test-boom-{host_header.replace(':', '-').replace('.', '_')}",
        headers={"Host": host_header},
    )
    assert response.status_code == 500
    for header in MIGRATION_HEADER_NAMES:
        assert header not in response.headers


def test_old_host_gets_headers_on_unhandled_500(enabled_app):
    @enabled_app.get("/test-boom-old-host")
    def boom():
        raise RuntimeError("boom")

    response = TestClient(enabled_app, raise_server_exceptions=False).get(
        "/test-boom-old-host", headers={"Host": OLD_HOST_HEADER}
    )
    assert response.status_code == 500
    assert response.headers["Deprecation"] == "@1785974400"
    assert response.headers["Sunset"] == "Wed, 04 Nov 2026 00:00:00 GMT"
    assert "libexdb.com" in response.headers["Link"]


def test_is_new_host_request_case_insensitive():
    """Unit-level guard on the shared predicate's own case handling — the
    part of host-gating that lives inside the function itself rather than
    being delegated to Starlette's URL parsing upstream."""
    notice = build_migration_notice(Settings(**ENABLED_SETTINGS_KWARGS))
    assert is_new_host_request(notice, "libexdb.com") is True
    assert is_new_host_request(notice, "LIBEXDB.COM") is True
    assert is_new_host_request(notice, "libex.lostcartographer.xyz") is False
    assert is_new_host_request(notice, None) is False


# ============================================================
# LOGGING MIDDLEWARE — the Host field
# ============================================================


def test_logging_middleware_extra_includes_query_string(client, caplog):
    """
    The path alone cannot answer which query parameters consumers actually
    send — a question that could not be answered at all before this field
    existed, because only `request.url.path` was recorded.
    """
    with caplog.at_level(logging.INFO, logger="libex"):
        client.get("/openapi.json?cache=false&region=de")

    request_records = [r for r in caplog.records if r.getMessage() == "Request completed"]
    assert request_records
    assert request_records[-1].query == "cache=false&region=de"


def test_logging_middleware_query_is_empty_string_when_absent(client, caplog):
    """A request with no query string logs an empty value, never None — the
    field must be present on every record so a query for its absence is
    meaningful rather than ambiguous."""
    with caplog.at_level(logging.INFO, logger="libex"):
        client.get("/openapi.json")

    request_records = [r for r in caplog.records if r.getMessage() == "Request completed"]
    assert request_records
    assert request_records[-1].query == ""


def test_logging_middleware_extra_includes_actual_host(client, caplog):
    """
    The `Host` a request arrived on is the only way to tell old-host traffic
    from new-host traffic apart in Axiom once both serve the same container
    — the entire migration decision depends on being able to see it per
    request.
    """
    with caplog.at_level(logging.INFO, logger="libex"):
        client.get("/openapi.json", headers={"Host": "libex.lostcartographer.xyz"})

    request_records = [r for r in caplog.records if r.getMessage() == "Request completed"]
    assert request_records
    assert request_records[-1].host == "libex.lostcartographer.xyz"
