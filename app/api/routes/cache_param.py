"""
The closed set of meanings Libex's `cache` query parameter can carry.

Three shared parameter declarations, one per honest meaning, so a reader
looking at any route's `cache: Annotated[bool, ...]` can see the whole
vocabulary in one place rather than each route independently inventing its
own story for the same parameter name. A fourth meaning is not a variant to
add here -- it is a sign `cache` has drifted into naming something else,
and the route should get its own parameter instead of overloading this one
further.
"""

# Standard library
from typing import Annotated

# Third party
from fastapi import Query, Response

# ============================================================
# VARIANT 1 -- STANDARD
# ============================================================

# Single-entity routes where one cache read decides the whole response:
# /book/{asin}, /book (bulk), /series/{asin}, /author/{asin}, and
# /quick-search. Libex's own stored copy is served when one exists; `false`
# forces a fresh Audible fetch, still stores whatever comes back exactly as
# a cache hit would have, and marks this particular response uncacheable at
# every downstream layer (see apply_cache_control below) so a caller's
# explicit request for a fresh answer can't be served stale from a browser
# or edge cache on the very next identical request.
#
# /series/books/{asin} also uses this variant, with one asymmetry worth
# naming on its own terms: `cache` there governs only the series-relationships
# read that finds the ASIN list, not the per-book hydration that follows it,
# which always fetches live regardless of this flag. That route's own
# X-Libex-Source header describes hydration only -- the books actually in
# the body -- so the header and the parameter answer two different
# questions about the same request: what this flag decided, and where the
# body's elements actually came from. A 50-book series hydrated live on
# every request is the deliberate cost of that split; see
# series/router.py's own get_books_by_series for why folding `cache` into
# the hydration call too is backlogged rather than made here.
CacheStandardParam = Annotated[
    bool,
    Query(
        description=(
            "Serve Libex's stored copy when one exists. false forces a "
            "fresh Audible fetch -- the result is still stored, but this "
            "response is not cached at any layer."
        )
    ),
]

# ============================================================
# VARIANT 2 -- AUTHOR BOOKS
# ============================================================

# /author/books/{asin} and /author/{asin}/books only. Everything Standard
# above says, plus one effect Standard doesn't have: these routes run
# discovery (walking Audible's catalogue for the author's full ASIN list)
# before hydration (fetching each of those ASINs), and `cache` governs both
# phases as one decision, not two.
#
# Defaults to True, which is the point of the flag rather than an
# incidental choice. Defaulted False, the cache only ever served callers
# who explicitly asked for it, so there was no such thing as a warm request
# on the default public path: every request walked Audible in full, and a
# prolific author's walk runs to hundreds of upstream requests and 504s
# behind the proxy's 30s timeout. The walk already WRITES its result
# unconditionally, so only the read was gated, and the cache was being
# populated for almost nobody.
#
# Single-flight is not a substitute and must not be read as one: it
# collapses CONCURRENT duplicates only. Ten callers in one instant were
# already a single walk; ten callers a minute apart were ten walks.
#
# The DB is not a substitute either. It is already unioned into every walk
# as the fourth source, so an author being stored does not spare anyone the
# walk -- only a cache hit does.
#
# `cache=false` keeps its documented meaning and still forces the full
# walk, so a caller who genuinely needs an uncached answer has one. That
# leaves the expensive path reachable by anyone, Libex having neither auth
# nor rate limiting by design -- but it is reachable today as the default
# for everyone, so this narrows the exposure rather than opening anything.
CacheAuthorBooksParam = Annotated[
    bool,
    Query(
        description=(
            "Serve Libex's stored copy when one exists, covering both the "
            "catalogue discovery walk and the per-book hydration that "
            "follows it. false forces a full re-walk of the author's "
            "catalogue and a fresh fetch of every book in it."
        )
    ),
]

# ============================================================
# VARIANT 3 -- INERT
# ============================================================

# /search and /narrator/books only. Accepted for compatibility -- both
# routes always fetch live from Audible regardless of this value, so
# `cache` never changes which books come back or what they contain. It does
# reach apply_cache_control the same as Standard's does, so `cache=false`
# still marks the response uncacheable at every downstream layer -- inert
# describes the body, not the response. Stays False by default: flipping a
# default on a parameter whose body is unaffected by it would be a
# documentation lie in the other direction from not documenting it at all.
#
# Stays in this file, and stays a named third meaning rather than being
# folded into Standard, precisely so a reader can tell at a glance that a
# route using it is not one of the places `cache` picks between a stored
# copy and a live fetch -- collapsing it into Standard would say the
# opposite.
CacheInertParam = Annotated[
    bool,
    Query(
        description=(
            "Accepted for compatibility. This endpoint's results are "
            "always fetched live, so this value does not change which "
            "books are returned. false still marks this response "
            "uncacheable, the same as it does on every other route that "
            "takes this parameter."
        )
    ),
]


# ============================================================
# CACHE-CONTROL
# ============================================================

def apply_cache_control(response: Response, use_cache: bool) -> None:
    """
    Marks a response uncacheable when the caller explicitly asked for a
    fresh answer via `cache=false`. Never sets anything else here -- no
    `public`, `max-age` or `s-maxage` is ever added by this function,
    because eight of the ten routes that took a `cache` parameter before
    this change sent no Cache-Control at all, and Libex's fronting
    Cloudflare rule turns the absence of one into BYPASS. Adding a positive
    value for the `cache=true` case would newly make those responses
    edge-shareable when they never were -- a materially bigger change than
    this parameter asked for.

    The two author-books routes do not call this. They have their own
    completeness-aware Cache-Control logic in authors/router.py's
    `_mark_completeness`, which also advertises a positive, bounded
    lifetime for a complete cached result -- a decision earned by that
    route's own walk-then-hydrate shape, not one this generic helper
    should default every other route into.
    """
    if not use_cache:
        response.headers["Cache-Control"] = "no-store"
