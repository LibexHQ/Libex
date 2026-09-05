"""
Shared Cache-Control policy for the /db/stats family.

One copy of the policy, used by the JSON stats route and by the badge images
rendered from the same figures, so two representations of one number cannot
drift into advertising different freshness. A reader comparing the badge on
the README against the JSON it links to is comparing the same cache entry;
they should also be being told the same thing about how old it may be.
"""

# Standard library
from datetime import datetime, timezone

# How long past its advertised freshness a cache may keep serving a stats
# copy while it refreshes that copy in the background.
#
# Sized for the gap between README renders, not for the refresh. What costs
# on this route is the FIRST request after a cache entry lapses: it recomputes
# counts over ~1.5M rows. Arrival rate adds nothing to it. Measured at
# concurrency 1, 5, 15 and 37: 1.598s, 0.127s, 0.155s, 0.367s. The single
# serial request paid a cold recompute and every batch behind it read the warm
# entry; the endpoint does not degrade under load, and an earlier reading of
# these numbers as a concurrency effect was wrong.
#
# What that first request costs varies by entry, and region is not the largest
# term. A scoped recompute runs 1.6s for de against 4.7s for us, tracking row
# count as expected -- but a cold UNSCOPED /db/stats, measured against the live
# instance with the cache row confirmed absent beforehand, took 15.3s. That
# entry is not region-scoped at all, and it is the one the five global badges
# at the top of the README read. Treat the figure as an upper bound on database
# time rather than as database time: it is end-to-end wall clock, TLS and
# Cloudflare and network and origin together, and the EXPLAIN (ANALYZE,
# BUFFERS) that would separate them has not been run. What it settles is the
# direction, which is what this policy turns on -- the global entry is nowhere
# near bounded by the 4.7s region figure.
#
# So the badges fail on a spread rather than on one variable. While they were
# shields badges the threshold was explicit: shields.io gives an upstream fetch
# about 3.5 seconds before it gives up and renders "inaccessible" (their
# maintainer, badges/shields#10996), so a 1.6s region answered in time, a 4.7s
# region did not, and the global entry was over by 4x rather than marginally --
# which is consistent with the global badges being the ones reported failing
# most often. The counters are served from /db/stats/badge now, so that ceiling
# is gone; the spread it exposed is not. camo waits on whatever origin takes,
# so a slow entry no longer draws a grey "inaccessible" plate, it draws nothing
# until it arrives. The limit moved from a number in somebody else's service to
# a reader's patience, which is the weaker of the two to be held to.
#
# A README render is 37 fetches across 37 distinct origin URLs -- one per badge
# now that each is its own image, where four region counts used to share a
# single `?region=xx` JSON response -- but still only nine cache entries: the
# unscoped entry behind the five global badges, plus one `?region=xx` entry
# behind each of the eight regions' four counts. What arrives together is nine
# entries' worth of first-request, not 37. Fetches sharing an entry are not
# separate costs: once any one of them has recomputed it the rest read it warm,
# which is what the concurrency figures above show. Across the nine there is no
# contention either -- each pays what its own entry costs. Serving the lapsed
# copy at 0.12s and refreshing behind it takes that window away for all of them
# at once.
#
# An hour is far more than any one of those refreshes needs, and that is the
# point: this window has to span the gap BETWEEN renders, because a lapsed edge
# copy is only rescued if the next reader arrives while the window is still
# open. The sharper limit is that stale-while-revalidate does nothing whatever
# at a PoP holding no copy, and 37 origin URLs spread across Cloudflare's PoP
# fleet means most first-requests at a given PoP are a genuine MISS that goes
# to origin and waits out the recompute. Going from nine URLs to 37 sharpens
# that rather than softening it: the same nine entries are spread over four
# times as many cache keys, so a PoP is that much likelier to be holding some
# other badge than the one being asked for. That case is what earns the
# background refresher; this header cannot reach it. The staleness bought is
# bounded and self-correcting -- what a reader sees is at most as old as the
# last time somebody looked, since their own request is what triggers the
# refresh -- and a running total of a continuously growing table is the rare
# figure where that costs nothing a reader would notice.
STATS_STALE_WHILE_REVALIDATE_SECONDS = 3600

# How long a cache may keep serving that copy when the refresh actually
# fails. Deliberately much longer than the window above, because this is not
# a staleness policy -- it is what the badge reads while origin is unwell.
# The alternative to a day-old real count is a badge that never loads, which
# is worse for the reader and tells them nothing at all. It stops applying
# the instant a refresh succeeds. Nothing else in Libex covers this case: a
# background refresher keeps the entry warm, but only a cache holding a copy
# can answer at all while origin is the thing that is down.
STATS_STALE_IF_ERROR_SECONDS = 86400


def stats_cache_control(cache_expires_at: datetime | None) -> str:
    """
    Build the Cache-Control header for a stats response.

    Cache-Control has to be set explicitly on this family: left unset,
    Cloudflare never caches these routes -- `cf-cache-status: BYPASS` was
    measured on every call -- and every badge on the README becomes a fetch
    that reaches origin and waits out whatever its entry costs to recompute.

    s-maxage and max-age carry the same value. There is a blast-radius gap
    between an edge copy and a browser copy -- the same one author-books cites,
    where an edge copy is purgeable and a browser copy is not -- but it is safe
    to ignore here because the value handed to both is bounded above by
    STATS_CACHE_TTL_SECONDS: neither copy can ever be told it is FRESH for
    longer than that ceiling permits. The stale windows deliberately reach past
    it, and the gap stays ignorable for the same reason it always was: what is
    held past the ceiling is a real count that has merely aged, and the next
    reader's own request replaces it.

    The freshness figure is the real remaining life of the cache entry
    get_db_stats already read or wrote, carried back on the result rather than
    re-read independently (see DbStatsResult): quoting the full TTL regardless
    of how far into its life the underlying entry already is would tell the
    edge to hold a copy for a fresh window measured from whenever it happened
    to ask, which can leave the edge serving a copy well after origin has
    already moved on to a newer one.

    stale-while-revalidate and stale-if-error join it, and they are doing work
    of their own rather than backstopping something else. In steady state a
    cache holds a copy that has lapsed -- that is precisely the state the
    badges fail in -- so where Cloudflare honours stale-while-revalidate, this
    header alone is enough to keep them rendering: the reader is served the
    lapsed copy immediately and the refresh happens behind them.
    stale-if-error then covers a case nothing else does, origin itself being
    unwell, where there is no request that could recompute anything.

    A cache_expires_at of None means nothing trustworthy was stored -- the
    DB-failure fallback, or a cache-write failure after an otherwise successful
    query -- and the response is marked no-store rather than handed the longest
    freshness Libex offers. The no-store branch gets no stale windows either: a
    stale-but-real count is a good answer to serve past its expiry, and
    all-zeros is not, so the one case where Libex knows the numbers are
    untrustworthy is the one case no cache may hold on to.
    """
    if cache_expires_at is None:
        return "no-store"

    remaining = (cache_expires_at - datetime.now(timezone.utc)).total_seconds()
    edge_seconds = max(0, int(remaining))
    return (
        f"public, max-age={edge_seconds}, s-maxage={edge_seconds}, "
        f"stale-while-revalidate={STATS_STALE_WHILE_REVALIDATE_SECONDS}, "
        f"stale-if-error={STATS_STALE_IF_ERROR_SECONDS}"
    )
