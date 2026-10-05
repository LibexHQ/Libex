"""
The process-wide bound on in-flight Audible requests: two semaphores, the
pool constants that size them, and the ContextVar that selects between them.
They bound what one process does to one shared exit IP, which is why they
live here rather than on LibexClient -- every client instance in a process
draws from the same two.
"""

# Standard library
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# ============================================================
# CONCURRENCY BOUND
# ============================================================

# A fan-out built on this client can set its own per-walk concurrency
# constant, but that only bounds one walk at a time -- two simultaneous
# lookups for a large author already double the in-flight count, and nothing
# upstream of this module caps the total across every walk running at once.
# LibexClient.get is the one place every outbound Audible call passes
# through, so the bound lives here, process-wide, instead of at any
# individual call site: a per-call-site limit only expresses how eagerly
# that one walk wants to go, never what one event loop and one exit IP
# carry across all of them at once.
#
# This is the pool every call uses by default. That includes continuous,
# unattended background crawling -- the workload that once got Libex's exit
# IP throttled into a VPN rotation, since it runs sustained and unsupervised
# for as long as the process is up -- and it also includes every bulk
# lookup: hydrating up to 1000 ASINs means 20 concurrent 50-ASIN chunks, and
# author-by-name, series and search lookups fan out the same way. Only the
# author-ASIN path opts out, into the wider pool below.
#
# 10 IS A PER-PROCESS FAN-OUT WIDTH, not a share of a deployment-wide
# budget, and that distinction is why it is a flat literal. The number has
# two unrelated jobs -- it is a slice of what a shared exit IP tolerates at
# once, which is divisible, and it is the width one live request's own
# chunked fan-out passes through, which is not. Sizing it on the first job
# alone breaks the second: a 1000-ASIN bulk hydration is 20 concurrent calls
# into this client, two rounds at 10 permits and ten rounds at 2, against
# a fronting proxy that times out at 30s.
#
# So dividing this constant by the uvicorn worker count is not available as
# a way to hold a budget. It is the same defect as the outage documented
# under AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT below -- a gate narrower
# than a single request's own fan-out, serialising that fan-out into rounds
# until it 504s at the proxy -- and it lands harder: those 504s were
# measured on a 10-wide gate, while a divided figure is 2 at four workers
# and 1 at six.
#
# The deployment-wide total is therefore WEB_CONCURRENCY x (10 + 25), both
# pools being per-process; at six workers, 210. A concurrency ladder run
# against Audible at 10, 25, 50, 100, 125, 150, 175, 200 and 250 in flight
# returned 1122 requests and 1122 HTTP 200 -- zero 429, zero 5xx, zero
# transport errors. No ceiling was found: the run stopped at its own
# request budget, not at any signal from Audible, and an uncontended canary
# fired before and after every burst never trended, so the path was in the
# same state after 250 in flight as before 10. 210 sits at 84% of the
# largest figure measured clean.
#
# THE CAVEAT THAT LIMITS WHAT THAT BUYS: the ladder ran on the DIRECT path.
# Production reaches Audible through a VPN proxy, whose exit IP is
# shared with strangers and whose own headroom is unmeasured. 250 is a real
# number on a real path -- just not the path that runs.
#
# The latency rise the ladder did show inside a burst is Libex's own, not
# Audible's: at 150 concurrent, process CPU was 81.6% of wall and the event
# loop sat blocked >50ms for 50.7% of wall, on 0.93 MB/s -- one loop doing
# TLS decrypt and gunzip. That cost is per event loop and so tracks this
# constant rather than the worker count; each worker runs its own loop with
# its own permits and pays its own share.
AUDIBLE_CONCURRENCY_LIMIT = 10

# A second, wider pool reserved for exactly one caller: a live author-books
# request's own discovery-and-hydration fan-out (screens + catalog walk in
# authors/, then get_books_by_asins hydrating the result), entered via
# author_books_concurrency() below. That workload is a fundamentally different
# shape from the sustained one above -- one user request fires a bounded,
# self-terminating burst (measured locally: 179 requests for
# Christie, 651 for Conan Doyle -- several times more than "well under 100"
# once assumed here, but still capped by the screens plateau and
# CATALOG_RESULT_CEILING rather than open-ended) and then stops, driven by
# one caller's request rather than a standing crawl, and not something to
# amplify. Reusing
# AUDIBLE_CONCURRENCY_LIMIT for it was the actual bug behind a live, measured
# production outage: 5 concurrent author lookups queued behind a shared
# 10-wide gate all 504'd at the fronting proxy's 30s timeout, and even a
# single uncontended prolific-author request (Christie) measured at 28.22s
# wall clock -- inside 2s of that same timeout.
#
# Measured directly against Audible (bypassing the production proxy, so
# these are relative, not absolute, numbers) across Conan Doyle and
# Christie: 5 concurrent mixed requests ran 12.71s at 10 vs 5.77s at 30,
# which is exactly the case this pool exists for -- five walks sharing a
# 10-permit gate queue behind each other. Zero throttled responses were
# observed at 30, 60, or even 100 in flight, and that reproduces exactly on
# re-measurement.
#
# What does not hold is any claim about where a useful ceiling sits. Two
# runs of one identical configuration measured 5.537s and 3.992s, so a
# single walk varies ~40% run to run at a fixed setting -- a 1.545s band
# that sits entirely inside, and covers ~84% of, the 1.85s spread across 25,
# 30, 50, 75 and 100 permits (3.69-5.54s). Nothing in that range is
# distinguishable from noise, and a single-request comparison (5.03s at 10
# vs 3.56s at 30) differs by 1.47s, narrower than that same 1.545s band, so
# it does not resolve a ceiling either.
#
# 25 therefore stands on cost, not on a measured upstream plateau. New
# connections opened per walk are at most the permit count, and in practice
# sit at it (25 permits -> 25, 100 -> 96), so every extra permit is one
# more handshake on any walk that starts cold -- cheap on a direct path, a
# full CONNECT+TLS through the production proxy, against a shared IP already
# throttled once on a different workload. Each also costs event-loop CPU
# that climbs monotonically for identical work: 0.84s at 25, 1.56s at 50,
# 3.72s at 100, where the loop sat blocked 3.44s of 5.16s wall. That cost is
# transport work -- TLS decrypt and gunzip of 50-product pages interleaving
# across streams -- not parsing, which measured 0.11s decode plus 0.08s
# normalization throughout. More handshakes and more blocked loop time for a
# latency gain no measurement here can resolve is a bad trade. None of it
# was measured through the production proxy, so none of it constrains
# behaviour on that path.
#
# The worker count divides neither pool. What sizes this one is a per-loop
# constraint -- a single walk's own fan-out has to fit through it -- and
# dividing it is exactly the change that produced the 504s described above,
# so it is not available as a way to make room. The same argument, applied
# to a 1000-ASIN bulk hydration's own fan-out, is what keeps
# AUDIBLE_CONCURRENCY_LIMIT above a flat literal; see its comment. What
# bounds this pool instead is the measured throttle-free band: 30, 60 and
# 100 in flight each drew zero throttled responses here, and the ladder
# recorded under AUDIBLE_CONCURRENCY_LIMIT reached 250 clean.
#
# The worst case counts BOTH pools in every worker, since both are
# per-process: WEB_CONCURRENCY x (25 + 10), or 210 at six workers, reached
# only when every worker takes a prolific author in the same moment. 210 is
# 84% of that ladder's top rung, so the worst case is a point inside the
# measured band rather than an extrapolation past it. Two things keep that
# from being reassurance on its own. The ladder ran on the direct path, not
# through the production proxy whose shared exit IP is what actually
# carries this traffic, and no run at any width has produced an onset
# gradient to read a real limit off -- 250 is the largest number tried, not
# an observed edge.
#
# What carries the worker count regardless is that 210 is a burst and the
# incident was not. The VPN rotation came from a sustained, unattended
# crawl, and background crawling runs on the default pool alone at its own
# steady 10 per process; the peak needs a prolific-author walk in every
# worker at once to appear at all. Since neither pool divides, the worker
# count is the only dial that moves that peak, which is what makes it the
# figure to weigh against a shared exit IP -- not either constant on its
# own.
#
# Halving this constant to 12-13 to buy room back lands in the regime the
# 504s came from, a gate narrower than a single walk's own fan-out, and
# pushes more walks past AUTHOR_BOOKS_TIME_BUDGET_SECONDS into
# truncated_by_deadline, which caches degraded for 900s and so brings those
# authors back for a re-walk sooner -- more outbound requests from the
# narrower gate, not fewer.
AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT = 25

# asyncio.Semaphore binds its internal waiter state to whichever event loop
# is running the first time it's touched. Under uvicorn that's one long-lived
# loop, but the test suite creates and tears down a fresh loop per test, and
# a semaphore built once at import time and reused across those loops risks
# waiters left over from a closed loop. Keying the instance to the current
# running loop and rebuilding it whenever that loop changes sidesteps this:
# a long-lived process creates it exactly once and keeps reusing it, and each
# fresh test loop gets its own fresh semaphore instead of inheriting stale
# state from whatever loop ran before it.
#
# These two semaphores, and the pool constants above them, stay process-wide
# module state rather than becoming LibexClient instance state: they bound
# what one process does to one shared exit IP, not what one client object
# does. Every LibexClient instance built in the same process draws from the
# same two semaphores below regardless of how many instances exist -- making
# them instance state would let two instances double the sustained fan-out
# a single exit IP sees, which is the exact failure that cost a VPN rotation
# once already.
_audible_semaphore: asyncio.Semaphore | None = None
_audible_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_audible_semaphore() -> asyncio.Semaphore:
    global _audible_semaphore, _audible_semaphore_loop
    loop = asyncio.get_running_loop()
    if _audible_semaphore is None or _audible_semaphore_loop is not loop:
        _audible_semaphore = asyncio.Semaphore(AUDIBLE_CONCURRENCY_LIMIT)
        _audible_semaphore_loop = loop
    return _audible_semaphore


# Same per-loop-rebuild reasoning as _get_audible_semaphore above, kept as a
# fully separate instance rather than a dict keyed by pool name: two pools
# only, and a separate pair of module globals means the existing default-pool
# tests (which poke _audible_semaphore / _audible_semaphore_loop directly)
# stay exactly as they are, untouched by this pool's own lifecycle.
_audible_author_books_semaphore: asyncio.Semaphore | None = None
_audible_author_books_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_audible_author_books_semaphore() -> asyncio.Semaphore:
    global _audible_author_books_semaphore, _audible_author_books_semaphore_loop
    loop = asyncio.get_running_loop()
    if (
        _audible_author_books_semaphore is None
        or _audible_author_books_semaphore_loop is not loop
    ):
        _audible_author_books_semaphore = asyncio.Semaphore(AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT)
        _audible_author_books_semaphore_loop = loop
    return _audible_author_books_semaphore


# Selects which pool LibexClient.get acquires from, without adding a
# parameter to get() itself, to the audible_get delegator every call site
# actually calls, or to any of the call sites behind that: a
# threaded-through pool parameter would have to be plumbed through every
# intermediate fetch function between the caller and the client, several of
# which are shared with background crawling and must never pick up the
# wider pool, and tests that patch audible_get at each consuming module
# use narrow, fixed-arity stand-ins that a new always-passed kwarg would
# break outright. A ContextVar sidesteps both: it's invisible
# to every call site (none of them change), asyncio.gather's own tasks
# inherit whichever value was current when gather() created them
# (contextvars.copy_context() happens at task creation), and it flows
# unmodified through every further nested await and nested gather inside
# that task -- which is exactly why wrapping only the outer gather of an
# author-books walk and the gather that hydrates its result (the
# author_books_concurrency call sites) is enough to cover every one of those
# calls' eventual descent into LibexClient.get, with nothing else touched.
_audible_concurrency_pool: ContextVar[str] = ContextVar(
    "_audible_concurrency_pool", default="default"
)


@contextmanager
def author_books_concurrency() -> Iterator[None]:
    """
    Marks every underlying LibexClient.get call made within this block --
    directly or via any further nested await, task, or gather it spawns --
    as belonging to the wider AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT pool
    instead of the default AUDIBLE_CONCURRENCY_LIMIT one. Reserved for the
    live author-books discovery and hydration fan-out (see
    AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT's own docstring for why that
    workload, and only that one, gets the wider pool); every other caller
    -- single book/author/series lookups, background crawling, chapter
    backfills --
    never enters this block and stays on the default pool exactly as before.
    """
    token = _audible_concurrency_pool.set("author_books")
    try:
        yield
    finally:
        _audible_concurrency_pool.reset(token)


def _current_audible_semaphore() -> asyncio.Semaphore:
    if _audible_concurrency_pool.get() == "author_books":
        return _get_audible_author_books_semaphore()
    return _get_audible_semaphore()
