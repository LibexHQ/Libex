"""Tests for response compression (GZipMiddleware, registered first/innermost
in app.core.middleware.setup_middleware so it sees a route's single-message
body before any BaseHTTPMiddleware re-chunks it).

TRAP this file is built around: httpx's test clients (TestClient/AsyncClient)
send `Accept-Encoding: gzip, deflate` by default and auto-decode a compressed
body when it is read through the normal `.content`/`.json()` accessors. Every
"not asked for compression" case below sends `Accept-Encoding: identity`
explicitly, and every size/byte-exactness assertion reads the wire bytes via
`client.stream(...)` + `response.iter_raw()`, which bypasses httpx's decoding
entirely -- `.content` would silently hand back the already-decompressed body
and make a broken Content-Length or a wrong Content-Encoding invisible.

GZip has to be the innermost middleware (the first add_middleware call):
anything registered inside it re-chunks the body first, which pushes GZip
onto its streaming branch, where the minimum-size threshold stops applying
and Content-Length drops from every response, not just the small ones.
"""

# Standard library
import asyncio
import contextlib
import gzip
import json
import random
import threading
from pathlib import Path

# Third party
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel

# Local
import app.api.routes.large_response as large_response_module
import app.core.middleware as middleware_module
from app.api.routes.large_response import build_large_list_response
from app.core.config import Settings
from app.core.middleware import setup_middleware
from app.core.migration_notice import MIGRATION_HEADER_NAMES, build_migration_notice
from app.core.response_headers import (
    EXPOSED_HEADER_NAMES,
    HEADER_COMPLETE,
    HEADER_REQUEST_ID,
    HEADER_SOURCE,
)


# ============================================================
# SHARED FIXTURES
# ============================================================

# 2000 'x' characters serializes to ~2011 bytes -- comfortably over the
# 1000-byte GZip minimum_size so it is always compressed when a caller asks
# for it, and identical across requests so a gzip response and an identity
# response of the same route can be compared byte for byte.
_BIG_PAYLOAD = {"data": "x" * 2000}

# The real, validated migration-notice config (mirrors the runbook values
# used in tests/test_migration_notice.py's ENABLED_SETTINGS_KWARGS), built
# through the real predicate rather than hand-assembled, so a change to what
# build_migration_notice requires shows up here too.
_MIGRATION_SETTINGS_KWARGS = {
    "migration_notice_enabled": True,
    "migration_new_host": "https://libexdb.com",
    "migration_announced": "2026-08-06",
    "migration_sunset": "2026-11-04",
    "migration_info_url": "https://github.com/LibexHQ/Libex/issues/999",
}


def _build_app(migration_notice=None, *, stamp_libex_headers=False):
    """A minimal app run through the real setup_middleware, the same pattern
    tests/test_migration_notice.py's _bare_app_with_migration_notice uses --
    what's under test is the real middleware stack and its registration
    order, not a hand-rolled stand-in for it."""
    app = FastAPI()

    @app.get("/big")
    async def big(response: Response):
        if stamp_libex_headers:
            response.headers[HEADER_COMPLETE] = "true"
            response.headers[HEADER_SOURCE] = "cache"
        return _BIG_PAYLOAD

    setup_middleware(app, migration_notice)
    return app


def _raw_get(client: TestClient, path: str, headers: dict):
    """Reads a response's wire bytes and headers without httpx's automatic
    content decoding -- see the module docstring's TRAP note. Returns the
    httpx Headers object (case-insensitive) rather than a plain dict."""
    with client.stream("GET", path, headers=headers) as response:
        raw = b"".join(response.iter_raw())
        return response.status_code, response.headers, raw


def _assert_never_compressed(client: TestClient, path: str, expected_status: int):
    """Asserts a GZip-eligible request to `path` still comes back uncompressed
    with an honest Content-Length -- the shape both /health and a 404 miss
    have to hold."""
    status, headers, raw = _raw_get(client, path, {"Accept-Encoding": "gzip"})
    assert status == expected_status
    assert "content-encoding" not in headers
    assert "content-length" in headers
    assert headers["content-length"] == str(len(raw))


# ============================================================
# (a) OVER THRESHOLD, REQUESTED WITH GZIP
# ============================================================


def test_gzip_requested_over_threshold_is_compressed_and_decodes_identically():
    app = _build_app()
    client = TestClient(app)

    _, gzip_headers, gzip_raw = _raw_get(client, "/big", {"Accept-Encoding": "gzip"})
    _, identity_headers, identity_raw = _raw_get(client, "/big", {"Accept-Encoding": "identity"})

    assert gzip_headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in gzip_headers.get("vary", "")
    # Content-Length must describe the compressed bytes actually on the
    # wire, not the original body -- a caller sizing a read buffer from this
    # header would under-read if it lied in either direction.
    assert gzip_headers["content-length"] == str(len(gzip_raw))
    assert gzip.decompress(gzip_raw) == identity_raw


# ============================================================
# (b) NOT ASKED FOR -- IDENTITY REQUEST
# ============================================================


def test_identity_requested_over_threshold_is_uncompressed_but_still_varies():
    app = _build_app()
    client = TestClient(app)

    status, headers, raw = _raw_get(client, "/big", {"Accept-Encoding": "identity"})

    assert status == 200
    assert "content-encoding" not in headers
    assert json.loads(raw) == _BIG_PAYLOAD
    # Vary: Accept-Encoding still has to be sent even when this particular
    # request didn't compress -- a cache sitting in front of Libex needs it
    # to know a *different* Accept-Encoding could get a different body.
    assert "Accept-Encoding" in headers.get("vary", "")


# ============================================================
# (c) THE ORDERING PIN -- /health AND A 404 STAY UNCOMPRESSED
# ============================================================


def test_health_and_404_error_body_are_never_compressed(client):
    """/health and a 404 route-miss are both far under the 1000-byte
    threshold, so they must never carry Content-Encoding, with or without a
    Content-Length -- and losing Content-Length here is exactly what happens
    if GZip stops being the innermost middleware: every response arrives
    pre-chunked and falls onto GZip's streaming branch, which drops
    Content-Length unconditionally."""
    _assert_never_compressed(client, "/health", 200)
    _assert_never_compressed(client, "/nonexistent-libex-path-xyz", 404)


# ============================================================
# (d) OTHER RESPONSE HEADERS SURVIVE COMPRESSION
# ============================================================


def test_libex_cors_and_migration_headers_survive_a_compressed_response():
    notice = build_migration_notice(Settings(**_MIGRATION_SETTINGS_KWARGS))
    assert notice is not None, "the migration-notice config used here must actually build a notice"

    app = _build_app(migration_notice=notice, stamp_libex_headers=True)
    client = TestClient(app)

    status, headers, raw = _raw_get(
        client,
        "/big",
        {"Accept-Encoding": "gzip", "Origin": "https://example.com"},
    )

    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert json.loads(gzip.decompress(raw)) == _BIG_PAYLOAD

    # X-Request-Id: minted fresh per request, so only presence and shape are
    # checked, never a fixed value.
    assert headers.get(HEADER_REQUEST_ID)

    # X-Libex-* facts headers, stamped by the route the same way a real
    # book/author/series route does.
    assert headers.get(HEADER_COMPLETE) == "true"
    assert headers.get(HEADER_SOURCE) == "cache"

    # Migration-notice headers, byte-exact against what build_migration_notice
    # produced -- not substrings, since both wire formats are spec-sensitive.
    for name, value in notice.headers.items():
        assert headers.get(name) == value
    assert set(MIGRATION_HEADER_NAMES) <= {h for h in notice.headers}

    # CORS: an Origin header was sent, so both the allow and expose headers
    # must be on the response, compressed or not.
    assert headers.get("access-control-allow-origin") == "*"
    exposed = headers.get("access-control-expose-headers", "")
    for name in (*EXPOSED_HEADER_NAMES, *MIGRATION_HEADER_NAMES):
        assert name in exposed


# ============================================================
# (e) build_large_list_response PATH
# ============================================================


class _Widget(BaseModel):
    """A synthetic model, not BookResponse -- large_response.py's threading
    and serialization logic is independent of which shape it validates, and
    a local model keeps this test decoupled from BookResponse's own required
    fields and any future change to them."""

    name: str


_LARGE_ITEMS = [{"name": "x" * 200} for _ in range(8)]


def _build_large_response_app():
    app = FastAPI()

    @app.get("/large")
    async def large(response: Response):
        response.headers[HEADER_COMPLETE] = "true"
        response.headers["Cache-Control"] = "public, max-age=300"
        return await build_large_list_response(
            list[_Widget], len(_LARGE_ITEMS), lambda: _LARGE_ITEMS, injected_response=response
        )

    setup_middleware(app, None)
    return app


def test_large_list_response_at_threshold_compresses_and_keeps_injected_headers(monkeypatch):
    """Forces the thread-offload branch with a small threshold rather than
    building an 8-item-really-is-200 payload -- what's under test is that a
    pre-serialized Response built off the event loop still passes through
    GZip and keeps the headers build_large_list_response merges from
    injected_response, not the threshold's real production value."""
    monkeypatch.setattr(large_response_module, "LARGE_RESPONSE_THREAD_THRESHOLD", 2)
    assert len(_LARGE_ITEMS) >= large_response_module.LARGE_RESPONSE_THREAD_THRESHOLD

    app = _build_large_response_app()
    client = TestClient(app)

    status, headers, raw = _raw_get(client, "/large", {"Accept-Encoding": "gzip"})

    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert headers.get("cache-control") == "public, max-age=300"
    assert headers.get(HEADER_COMPLETE) == "true"
    assert json.loads(gzip.decompress(raw)) == _LARGE_ITEMS


# ============================================================
# (g) THE THREAD-OFFLOAD THRESHOLD
# ============================================================

# Word-salad text, not a repeated character -- real bodies don't compress
# uniformly, and a single-character payload would compress so well it could
# mask a Content-Length or size-comparison bug that a mixed body would catch.
_WORD_POOL = (
    "audiobook narrator chapter marker region catalogue metadata series author "
    "title subtitle publisher summary keywords rating format language whisper "
    "sync pdf explicit vvab produced heritage gender longer shorter better worse"
).split()


def _pseudo_random_body(min_bytes: int, seed: int) -> str:
    """Deterministic word-salad text at least `min_bytes` long. Seeded so the
    same call always returns the same text -- needed since it's compared
    byte-for-byte against a second, independently-served response."""
    rng = random.Random(seed)
    words = []
    total = 0
    while total < min_bytes:
        word = rng.choice(_WORD_POOL)
        words.append(word)
        total += len(word) + 1
    return " ".join(words)


# Comfortably over _GZIP_THREAD_OFFLOAD_SIZE once JSON-wrapped.
_HUGE_PAYLOAD = {"data": _pseudo_random_body(70_000, seed=1)}

# Over _GZIP_MINIMUM_SIZE but comfortably under _GZIP_THREAD_OFFLOAD_SIZE.
_MID_PAYLOAD = {"data": _pseudo_random_body(20_000, seed=2)}


def _build_sized_app(payload: dict, handler_thread_log: list):
    """Same minimal app as _build_app, but returns a caller-supplied payload
    and records the OS thread id the route handler ran on -- the one fixed
    point every request-handling coroutine passes through regardless of what
    the middleware stack does with the body afterward, and so a stable
    baseline for "did compression run somewhere else"."""
    app = FastAPI()

    @app.get("/big")
    async def big():
        handler_thread_log.append(threading.get_ident())
        return payload

    setup_middleware(app, None)
    return app


def _spy_on_apply_compression(monkeypatch, thread_log: list):
    """Wraps _OffloadingGZipResponder.apply_compression to record the OS
    thread it actually ran on, without changing what it returns. Covers the
    inline (below-threshold) path, which still goes through
    GZipResponder.apply_compression -- see _spy_on_offload_compress for the
    offload path, which no longer does."""
    real_apply_compression = middleware_module._OffloadingGZipResponder.apply_compression

    def spy(self, *args, **kwargs):
        thread_log.append(threading.get_ident())
        return real_apply_compression(self, *args, **kwargs)

    monkeypatch.setattr(middleware_module._OffloadingGZipResponder, "apply_compression", spy)


def _spy_on_offload_compress(monkeypatch, thread_log: list):
    """Wraps gzip.compress as bound in the middleware module to record the OS
    thread it actually ran on, without changing what it returns. This is what
    the thread-offload branch calls directly rather than going through
    apply_compression -- see the cancellation-safety note on
    _OffloadingGZipResponder."""
    real_compress = middleware_module.gzip.compress

    def spy(*args, **kwargs):
        thread_log.append(threading.get_ident())
        return real_compress(*args, **kwargs)

    monkeypatch.setattr(middleware_module.gzip, "compress", spy)


def test_gzip_at_or_above_offload_threshold_compresses_off_the_loop_thread(monkeypatch):
    """At/above _GZIP_THREAD_OFFLOAD_SIZE: the response must still be a
    correct compressed response, and the compression call must run on a
    different OS thread than the one handling the request -- proving the
    work actually left the event loop rather than merely being scheduled to
    look like it did."""
    payload_bytes = json.dumps(_HUGE_PAYLOAD).encode()
    assert len(payload_bytes) >= middleware_module._GZIP_THREAD_OFFLOAD_SIZE

    handler_thread_log: list[int] = []
    compression_thread_log: list[int] = []
    _spy_on_offload_compress(monkeypatch, compression_thread_log)

    app = _build_sized_app(_HUGE_PAYLOAD, handler_thread_log)
    client = TestClient(app)

    _, gzip_headers, gzip_raw = _raw_get(client, "/big", {"Accept-Encoding": "gzip"})
    _, _, identity_raw = _raw_get(client, "/big", {"Accept-Encoding": "identity"})

    assert gzip_headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in gzip_headers.get("vary", "")
    assert gzip_headers["content-length"] == str(len(gzip_raw))
    assert gzip.decompress(gzip_raw) == identity_raw

    assert len(compression_thread_log) == 1
    assert compression_thread_log[0] not in handler_thread_log


def test_gzip_below_offload_threshold_compresses_on_the_loop_thread(monkeypatch):
    """Between _GZIP_MINIMUM_SIZE and _GZIP_THREAD_OFFLOAD_SIZE: the response
    still compresses correctly, but the compression call runs on the same OS
    thread that handled the request -- the offload only fires above its own
    threshold, not on every eligible body."""
    payload_bytes = json.dumps(_MID_PAYLOAD).encode()
    assert middleware_module._GZIP_MINIMUM_SIZE <= len(payload_bytes) < middleware_module._GZIP_THREAD_OFFLOAD_SIZE

    handler_thread_log: list[int] = []
    compression_thread_log: list[int] = []
    _spy_on_apply_compression(monkeypatch, compression_thread_log)

    app = _build_sized_app(_MID_PAYLOAD, handler_thread_log)
    client = TestClient(app)

    _, gzip_headers, gzip_raw = _raw_get(client, "/big", {"Accept-Encoding": "gzip"})
    _, _, identity_raw = _raw_get(client, "/big", {"Accept-Encoding": "identity"})

    assert gzip_headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in gzip_headers.get("vary", "")
    assert gzip_headers["content-length"] == str(len(gzip_raw))
    assert gzip.decompress(gzip_raw) == identity_raw

    assert len(compression_thread_log) == 1
    assert compression_thread_log[0] in handler_thread_log


# ============================================================
# (h) CANCELLATION DURING THE OFFLOADED COMPRESSION
# ============================================================


async def _drive_offloading_responder_and_cancel(monkeypatch, body: bytes) -> list[BaseException]:
    """Runs _OffloadingGZipResponder directly against a canned single-message
    ASGI app, cancels the driving task once the worker thread is confirmed to
    be inside the offloaded compression call, waits for the cancelled
    coroutine's own buffer/file cleanup to finish, then releases the worker
    and reports whatever it raised.

    Driven directly rather than through TestClient: the race is a
    microsecond-wide window between the worker starting and the cancelled
    coroutine's cleanup running, and only a harness that blocks the worker on
    a hand-held gate can land in it reliably."""
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_exceptions: list[BaseException] = []

    real_compress = middleware_module.gzip.compress

    def blocking_compress(*args, **kwargs):
        worker_started.set()
        release_worker.wait(timeout=5)
        try:
            return real_compress(*args, **kwargs)
        except BaseException as exc:
            worker_exceptions.append(exc)
            raise

    monkeypatch.setattr(middleware_module.gzip, "compress", blocking_compress)

    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

    responder = middleware_module._OffloadingGZipResponder(app, 1000, compresslevel=1)

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        pass

    task = asyncio.ensure_future(responder({"type": "http", "headers": []}, receive, send))

    for _ in range(500):
        if worker_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert worker_started.is_set(), "worker thread never reached the offloaded compression call"

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # The cancelled task has now unwound through GZipResponder.__call__'s
    # "with self.gzip_buffer, self.gzip_file:" cleanup, closing both -- the
    # state the worker thread must survive without touching either.
    assert responder.gzip_buffer.closed
    assert responder.gzip_file.closed

    release_worker.set()
    await asyncio.sleep(0.1)

    return worker_exceptions


async def test_cancelling_mid_offload_does_not_raise_in_the_worker_thread(monkeypatch):
    """Cancelling the request while the offloaded compression is still
    running must not raise there, even though the coroutine that launched it
    has already unwound and closed the responder's own buffer/file by the
    time the worker resumes -- the offloaded call is self-contained and never
    touches either."""
    body = _pseudo_random_body(70_000, seed=3).encode()

    worker_exceptions = await _drive_offloading_responder_and_cancel(monkeypatch, body)

    assert worker_exceptions == []


# ============================================================
# (f) STATIC DOCS ASSET
# ============================================================


def test_static_asset_requested_with_gzip_decodes_byte_equal_to_disk(client):
    """swagger-libex.css is small enough to be a single ASGI body message
    (StaticFiles/FileResponse only streams in 64KB chunks, which would push
    this onto GZip's other, still-correct-but-differently-shaped streaming
    branch for a larger file) and, at 2.5KB, comfortably over the 1000-byte
    threshold -- so a plain, non-chunked compressed static response."""
    on_disk = (Path(__file__).resolve().parent.parent / "app" / "static" / "swagger-libex.css").read_bytes()
    assert len(on_disk) > 1000, "fixture assumption: this asset must be over the gzip threshold"

    status, headers, raw = _raw_get(client, "/static/swagger-libex.css", {"Accept-Encoding": "gzip"})

    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert gzip.decompress(raw) == on_disk
