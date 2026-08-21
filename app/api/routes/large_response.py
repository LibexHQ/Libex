"""
Offloads construction and JSON serialization of a large list response onto
a worker thread, so building one big catalogue can't stall every other
connection the process is holding while it runs. Exactly two response
shapes ever pass through here -- list[BookResponse] and BulkBookResponse --
reused across every call site that returns one of them; a shape is what
this module is built around, not a call-site count, so a new route
returning one of the two existing shapes needs no changes here at all.

This is the diverged half of a fix already applied to the same data one
step earlier: app.services.audible.books offloads normalization batches at
NORMALIZE_THREAD_THRESHOLD to a worker thread, for exactly this reason, on
the same request path. Response construction and JSON encoding of that same
hydrated list never got the equivalent treatment -- this module is that
treatment.
"""

# Standard library
import asyncio
from functools import lru_cache
from typing import Any, Callable

# Third party
from fastapi import Response
from pydantic import TypeAdapter

# ============================================================
# THRESHOLD
# ============================================================

# A deliberately separate figure from NORMALIZE_THREAD_THRESHOLD, not a
# reuse of it. That constant was calibrated to per-product normalization
# cost (~0.2ms/item, measured against a ~0.3-0.5ms thread-hop fixed cost);
# this one is calibrated to model construction plus JSON serialization,
# which is a different, more expensive operation per item (measured ~35-55
# us/item combined at this threshold's own size, against a ~0.17ms hop
# measured the same way). The two happen to land in the same neighborhood
# today, but that is a coincidence of two independent measurements, not a
# reason to alias them -- if either layer's per-item cost shifts later,
# nothing about the other layer's judgement should move with it.
#
# 200 items is where the inline cost of building and serializing the
# response (~13ms measured for a synthetic 200-book payload through this
# exact construct-then-validate-then-dump_json pipeline) is comfortably
# past the point where a single thread hop's fixed cost is negligible
# against it -- roughly 30-80x the hop cost at and above this size, versus
# a hop costing more than it saves below it on the common, narrow request
# (a handful to a few dozen books) that every other list-returning route
# keeps making. Only an author's full catalogue and a bulk /book request
# near its 1000-ASIN cap realistically cross this line.
#
# One constant governs every call site rather than each getting its own,
# because every call site constructs the same BookResponse from the same
# dict shape -- an author's live catalogue, a bulk /book lookup, and a
# DB-backed author or series read all pay an identical per-item cost, so
# there is nothing for a per-site figure to capture that this one doesn't
# already. That stops being true, and only then, the day a call site
# passes a model shape genuinely more or less expensive than BookResponse
# to construct and serialize -- that call site earns its own threshold, not
# a re-tuning of this one.
LARGE_RESPONSE_THREAD_THRESHOLD = 200


# ============================================================
# HELPERS
# ============================================================

@lru_cache(maxsize=None)
def _adapter_for(model_type: Any) -> TypeAdapter:
    """
    One TypeAdapter per response shape, built once and reused. Constructing
    a TypeAdapter is itself non-trivial, and there are only ever two shapes
    passed here (list[BookResponse] and BulkBookResponse) -- caching keeps
    that cost out of the request path entirely rather than paying it inside
    the very thread hop meant to be cheap.
    """
    return TypeAdapter(model_type)


def _build_and_serialize(model_type: Any, build: Callable[[], Any]) -> bytes:
    """
    Runs entirely off the event loop. Builds the response model(s) via
    `build`, then reproduces exactly what FastAPI's own response_model
    machinery does on the loop afterward for every route in this file --
    field.validate (TypeAdapter.validate_python(from_attributes=True)) then
    field.serialize_json (TypeAdapter.dump_json with by_alias=True and every
    exclude_* flag False, FastAPI's defaults, which none of these routes
    override) -- so the bytes handed back are byte-identical to what the
    normal pipeline would have produced; only which thread does the work
    changes. See fastapi.routing.serialize_response and
    fastapi._compat.v2.ModelField for the code this mirrors.

    `build` touches only its own captured arguments and BookResponse's
    plain pydantic validation -- no DB session, no cache, no shared mutable
    state, and no ContextVar read, so running it on another thread carries
    no correctness risk, the same argument
    app.services.audible.books._normalize_products already makes for
    offloading this same data one step earlier.
    """
    value = build()
    adapter = _adapter_for(model_type)
    validated = adapter.validate_python(value, from_attributes=True)
    return adapter.dump_json(
        validated,
        by_alias=True,
        exclude_unset=False,
        exclude_defaults=False,
        exclude_none=False,
    )


async def build_large_list_response(
    model_type: Any,
    item_count: int,
    build: Callable[[], Any],
    *,
    injected_response: Response | None = None,
) -> Any:
    """
    Returns whatever the route should hand back to FastAPI.

    Below LARGE_RESPONSE_THREAD_THRESHOLD: `build()` is called and returned
    exactly as every route here called it inline before this module
    existed. Nothing changes for any response under the threshold -- same
    object, same thread, same FastAPI response_model pass afterward.

    At or above it: `build` and the serialization FastAPI would otherwise
    do on the loop both run on a worker thread, and the result comes back
    as a pre-serialized Response. Returning a Response instance directly
    from a route makes FastAPI skip its own response_model serialization
    pass entirely (see fastapi.routing.get_request_handler: `if
    isinstance(raw_response, Response)`), which is what actually gets the
    JSON-encoding half off the loop rather than just the construction half.

    That same short-circuit is also why `injected_response` exists. FastAPI
    only merges the headers a route set on its injected `response: Response`
    dependency (X-Libex-Complete, Cache-Control on the author-by-ASIN
    routes) when the route returns bare content for FastAPI to wrap itself
    -- returning a Response of our own bypasses that merge, so it is
    redone here explicitly for the routes that rely on it. Callers that
    never set anything on an injected response pass nothing and get
    nothing extended, which is a no-op either way.

    The same bypass applies to `status_code`. FastAPI injects `response:
    Response` with status_code set to None, not 200 (see
    fastapi.dependencies.utils.solve_dependencies) -- None is what tells
    fastapi.routing._build_response_args to fall through to the response
    class's own default instead. Below the threshold, a route that never
    touches `response.status_code` still ends up at 200 by that fallthrough;
    a Response built fresh here has no such fallthrough of its own, so None
    has to be translated to 200 explicitly rather than passed through
    literally, which Response's constructor cannot accept. A route that
    mutates `response.status_code` at runtime is carried over from
    `injected_response`, the same source the header merge above already
    reads from.

    That coverage is real but partial, and calling it complete would be
    wrong: it only reaches a status set on the injected Response object at
    runtime. A route declaring `@router.get(..., status_code=201)` sets its
    status a different way entirely -- `_build_response_args`
    (fastapi.routing:333-348) gives that decorator-level value priority
    below the threshold, and this module has no plumbing to see it at all,
    injected or otherwise. Such a route would return 201 below the
    threshold and silently fall back to 200 at or above it. No current call
    site declares one, so nothing is broken today -- but this is a genuine
    constraint on correct use of this helper, not a hypothetical: any route
    added here with a decorator-level status_code needs that plumbing added
    first, not assumed to already work.
    """
    if item_count < LARGE_RESPONSE_THREAD_THRESHOLD:
        return build()
    body = await asyncio.to_thread(_build_and_serialize, model_type, build)
    status_code = 200
    if injected_response is not None and injected_response.status_code is not None:
        status_code = injected_response.status_code
    response = Response(content=body, media_type="application/json", status_code=status_code)
    if injected_response is not None:
        response.headers.raw.extend(injected_response.headers.raw)
    return response
