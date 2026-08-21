"""
Tests for the large-list response offload helper.
Covers both sides of the threshold, byte-for-byte parity with what FastAPI's
own response_model pass would have produced, and that the injected-response
header merge (X-Libex-Complete, Cache-Control) still happens on the
offloaded path.
"""

# Standard library
import asyncio
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from fastapi import Response
from pydantic import TypeAdapter

# Local
from app.api.routes.books.schemas import BookResponse
from app.api.routes.large_response import (
    LARGE_RESPONSE_THREAD_THRESHOLD,
    build_large_list_response,
)


def _books(n):
    return [
        BookResponse(asin=f"B{i:09d}", region="us")
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_below_threshold_returns_build_result_unchanged_and_inline():
    """Below the threshold, the helper is a pass-through: no thread hop, and
    the exact object `build()` returned comes back untouched."""
    built = _books(LARGE_RESPONSE_THREAD_THRESHOLD - 1)

    with patch(
        "app.api.routes.large_response.asyncio.to_thread", new_callable=AsyncMock
    ) as mock_to_thread:
        result = await build_large_list_response(
            list[BookResponse], len(built), lambda: built
        )

    mock_to_thread.assert_not_called()
    assert result is built


@pytest.mark.asyncio
async def test_at_threshold_offloads_to_a_worker_thread():
    """At the threshold, construction is handed to asyncio.to_thread exactly
    once rather than run inline."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD
    build_calls = []

    def build():
        build_calls.append(1)
        return _books(n)

    real_to_thread = asyncio.to_thread
    calls = []

    async def spy(fn, *a, **kw):
        calls.append(fn)
        return await real_to_thread(fn, *a, **kw)

    with patch("app.api.routes.large_response.asyncio.to_thread", side_effect=spy):
        result = await build_large_list_response(list[BookResponse], n, build)

    assert len(calls) == 1
    assert isinstance(result, Response)
    assert len(build_calls) == 1


@pytest.mark.asyncio
async def test_offloaded_bytes_match_what_fastapi_would_have_serialized_inline():
    """The pre-serialized bytes for the offloaded path must be identical to
    calling TypeAdapter(list[BookResponse]) the same way FastAPI's own
    response_model pass does -- validate_python(from_attributes=True) then
    dump_json with by_alias=True and every exclude_* flag False, which is
    what every route using this helper leaves as the default."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD
    built = _books(n)

    result = await build_large_list_response(list[BookResponse], n, lambda: built)

    adapter = TypeAdapter(list[BookResponse])
    validated = adapter.validate_python(built, from_attributes=True)
    expected = adapter.dump_json(
        validated,
        by_alias=True,
        exclude_unset=False,
        exclude_defaults=False,
        exclude_none=False,
    )

    assert isinstance(result, Response)
    assert result.body == expected


@pytest.mark.asyncio
async def test_offloaded_response_carries_over_headers_set_on_the_injected_response():
    """Returning our own Response for the offloaded path bypasses FastAPI's
    usual merge of headers a route set on its injected `response: Response`
    dependency (see fastapi.routing.get_request_handler) -- this helper must
    redo that merge itself or X-Libex-Complete/Cache-Control silently vanish
    on exactly the large responses that made them matter."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD
    injected = Response()
    injected.headers["X-Libex-Complete"] = "true"
    injected.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"

    result = await build_large_list_response(
        list[BookResponse], n, lambda: _books(n), injected_response=injected
    )

    assert result.headers["X-Libex-Complete"] == "true"
    assert result.headers["Cache-Control"] == "public, max-age=300, s-maxage=300"


@pytest.mark.asyncio
async def test_no_injected_response_is_a_no_op():
    """Routes that never set anything on an injected response (the
    name-based author lookup, the bulk /book route) pass nothing and the
    offloaded response is unaffected."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD

    result = await build_large_list_response(list[BookResponse], n, lambda: _books(n))

    assert "X-Libex-Complete" not in result.headers


@pytest.mark.asyncio
async def test_offloaded_response_defaults_to_200_with_no_injected_response():
    """No `injected_response` at all -- the offloaded path must still default to
    200, matching what every route without an injected response gets today."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD

    result = await build_large_list_response(list[BookResponse], n, lambda: _books(n))

    assert result.status_code == 200


@pytest.mark.asyncio
async def test_offloaded_response_carries_over_status_code_from_the_injected_response():
    """A status set on the injected `response: Response` must survive the
    offloaded path exactly as it would below the threshold, where FastAPI
    reads it directly off that same object. No current call site sets one,
    but the offloaded path must not default to 200 if a future one does."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD
    injected = Response(status_code=201)

    result = await build_large_list_response(
        list[BookResponse], n, lambda: _books(n), injected_response=injected
    )

    assert result.status_code == 201


@pytest.mark.asyncio
async def test_offloaded_response_falls_back_to_200_when_injected_status_is_none():
    """Every current call site sets headers on its injected `response:
    Response` but never touches `response.status_code`, so it stays at the
    None FastAPI injects it with (see
    fastapi.dependencies.utils.solve_dependencies), not at 200 -- an injected
    Response never goes through Starlette's own constructor default on the
    offloaded path. This must fall through to the helper's own 200 default
    the same way the sub-threshold path falls through to Response's default
    via fastapi.routing._build_response_args."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD
    injected = Response()
    del injected.headers["content-length"]
    injected.status_code = None
    injected.headers["X-Libex-Complete"] = "true"

    result = await build_large_list_response(
        list[BookResponse], n, lambda: _books(n), injected_response=injected
    )

    assert result.status_code == 200
