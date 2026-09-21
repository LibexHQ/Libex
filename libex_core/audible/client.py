"""
Audible API client.
Handles headers, region mapping, and one embedder-owned HTTP transport.
Every Audible service file that wants to reach Audible does so through a
LibexClient instance -- there is no shared, process-wide client library
code can silently fall back onto.

DESIGN PHILOSOPHY: Audible-first.
Every call that reaches this client goes straight to Audible -- it holds
no cache of its own and never consults one. Whether a request reaches this
client at all, or is answered from a cache first, is decided by the caller
before it gets here.

This module reads no environment and constructs no application settings --
how a LibexClient egresses (direct, or through a proxy) is a decision its
caller makes once, by name, at construction:
LibexClient(proxy_url=..., allow_direct_egress=...). There is no default
for proxy_url and no module-level transport left for a caller to default
onto, so constructing a LibexClient without deciding is a TypeError from
Python's own argument checking rather than a runtime guard this module has
to remember to enforce. Direct egress itself still needs a second,
explicit opt-in -- an empty or missing proxy_url alone is never enough,
because that is exactly the shape of a caller who meant to configure a
proxy and simply left the setting unset. This matters most for an embedded
copy of this library running on someone else's machine: without that
guard, a forgotten proxy setting would send that person's own IP to
Audible together with the ASINs they look up -- their library, from their
home address -- with nothing here to stop it.

Each instance owns its transport for its whole life; there is no
reconfigure. Closing an instance (aclose(), or the async context manager)
is terminal -- a closed instance never sends another request and never
rebuilds a client, for any reason. Concurrency, retry and backoff stay
process-wide rather than per-instance: two LibexClient instances in the
same process still share one exit IP and are bounded by the same two
module-level semaphores below, so that sharing has to hold regardless of
how many instances exist.
"""

# Standard library
import asyncio
import datetime
import logging
import random
from collections.abc import Coroutine
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

# Third party
import httpx

# Local
from libex_core.exceptions import AudibleAPIException, NotFoundException, RegionException

# The same logger object the hosted application's own get_logger() returns --
# this package cannot import that function, since doing so would pull in the
# hosted application's settings and environment behind it, so it names the
# logger directly instead.
logger = logging.getLogger("libex")

# ============================================================
# REGION MAPS
# ============================================================

REGION_MAP: dict[str, str] = {
    "us": ".com",
    "uk": ".co.uk",
    "ca": ".ca",
    "au": ".com.au",
    "de": ".de",
    "fr": ".fr",
    "it": ".it",
    "es": ".es",
    "jp": ".co.jp",
    "in": ".in",
    "br": ".com.br",
}

LOCALE_MAP: dict[str, str] = {
    "us": "en-US",
    "uk": "en-GB",
    "ca": "en-CA",
    "au": "en-AU",
    "de": "de-DE",
    "fr": "fr-FR",
    "it": "it-IT",
    "es": "es-ES",
    "jp": "ja-JP",
    "in": "en-IN",
    "br": "pt-BR",
}

VALID_REGIONS = set(REGION_MAP.keys())

# ============================================================
# HEADERS
# ============================================================

BASE_HEADERS: dict[str, str] = {
    "User-Agent": "Audible/4.15.0 Android/14 Build/SM-S928U",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    "Accept-Charset": "utf-8",
}

# Device type id for the Android screens endpoints (e.g. the author-detail
# screen). Scoped per-call via LibexClient.get's extra_headers, never merged
# into get_region_headers -- stamping every call with one stable device id
# across a single exit IP is exactly the shape a per-device throttle keys on.
ANDROID_DEVICE_TYPE_ID = "A10KISP2GWF0E4"


def get_region_headers(region: str) -> dict[str, str]:
    """Returns region-specific headers including locale, language, and X-ADP-SW."""
    locale = LOCALE_MAP.get(region, "en-US")
    return {
        **BASE_HEADERS,
        "ACCEPTED-LANGUAGE": locale,
        "Accept-Language": locale,
        "X-ADP-SW": str(random.randint(10000000, 99999999)),
    }


# ============================================================
# CONCURRENCY BOUND
# ============================================================

# Every fan-out in this app sets its own per-walk concurrency constant
# (SCREENS_FANOUT_CONCURRENCY in authors/screens.py), but those only bound
# one walk at a time -- two simultaneous requests for a
# large author already double the in-flight count, and nothing upstream of
# this module caps the total across every walk running at once.
# LibexClient.get is the one place every outbound Audible call passes
# through, so the bound lives here, process-wide, instead of at any
# individual call site: a per-call-site limit only expresses how eagerly
# that one walk wants to go, never what one event loop and one exit IP
# carry across all of them at once.
#
# This is the pool every call uses by default. That includes the seeder's
# continuous, unattended background work -- the workload that once got
# Libex's exit IP throttled into a VPN rotation, since it runs sustained and
# unsupervised for as long as the process is up -- and it also includes
# every bulk route: GET /books takes up to 1000 ASINs (books/router.py),
# hydrating them as 20 concurrent 50-ASIN chunks, and /author/books?name=,
# /series/{asin} and /search fan out the same way. Only the author-ASIN
# path opts out, into the wider pool below.
#
# 10 IS A PER-PROCESS FAN-OUT WIDTH, not a share of a deployment-wide
# budget, and that distinction is why it is a flat literal. The number has
# two unrelated jobs -- it is a slice of what a shared exit IP tolerates at
# once, which is divisible, and it is the width one live request's own
# chunked fan-out passes through, which is not. Sizing it on the first job
# alone breaks the second: a 1000-ASIN /books call is 20 concurrent calls
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
# Production reaches Audible through the AirVPN proxy, whose exit IP is
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
# self-terminating burst (measured locally, same session: 179 requests for
# Christie, 651 for Conan Doyle -- several times more than "well under 100"
# once assumed here, but still capped by the screens plateau and
# CATALOG_RESULT_CEILING rather than open-ended -- see screens.py and
# catalog.py) and then stops, driven by real user traffic Libex's own
# hard-noes already forbid amplifying, not a standing crawl. Reusing
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
# to a 1000-ASIN /books call's own fan-out, is what keeps
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
# incident was not. The VPN rotation came from the seeder's sustained,
# unattended crawl, and the seeder runs on the default pool alone at its
# own steady 10 per process; the peak needs a prolific-author walk in every
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
# intermediate fetch function in screens.py, catalog.py, and books.py,
# several of which are shared with the seeder and must never pick up the
# wider pool, and several existing tests patch audible_get at each of those
# consuming modules -- app.services.audible.books.audible_get and its
# siblings -- with narrow, fixed-arity stand-ins that a new always-passed
# kwarg would break outright. A ContextVar sidesteps both: it's invisible
# to every call site (none of them change), asyncio.gather's own tasks
# inherit whichever value was current when gather() created them
# (contextvars.copy_context() happens at task creation), and it flows
# unmodified through every further nested await and nested gather inside
# that task -- which is exactly why wrapping only the single outer gather
# in _walk_author_books and the single hydration gather in
# get_books_by_asins (see author_books_concurrency's call sites) is enough
# to cover every one of those calls' eventual descent into LibexClient.get,
# with nothing else in either module touched.
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
    -- single book/author/series lookups, the seeder, chapter backfills --
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


# ============================================================
# TRANSPORT
# ============================================================

# _TransportSnapshot is the whole transport LibexClient.__init__ builds,
# immutable for that instance's entire life. There is no reconfigure to
# guard against here the way there was under a single shared, replaceable,
# module-level transport: each instance builds exactly one of these, once,
# in its own constructor, and never replaces it.
_VALID_PROXY_SCHEMES = ("http", "https")
_DEFAULT_PROXY_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class _TransportSnapshot:
    """One LibexClient instance's whole validated transport. Never handed to
    a caller outside this module -- transport_summary() is the read-only
    view external code is allowed to see, deliberately narrower than this:
    `proxy` can carry embedded credentials and must never reach a log line
    or an exception message."""
    proxy: httpx.Proxy | None
    host: str | None


@dataclass(frozen=True)
class TransportSummary:
    """Read-only view of one LibexClient instance's configured transport,
    safe to log or assert against: which of the two states ("direct" or
    "proxy") that instance was constructed with, and -- in the "proxy"
    state only -- the hostname it resolves to. Never the URL, never any
    credentials it might embed. Produced by LibexClient.transport_summary()."""
    mode: str
    host: str | None


# ============================================================
# HTTP CLIENT POOL LIMITS
# ============================================================

# The two semaphores above together guarantee at most
# AUDIBLE_CONCURRENCY_LIMIT + AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT calls --
# 35 -- are ever in flight FROM THIS PROCESS across both pools at once,
# across any number of LibexClient instances, since both semaphores are
# module-level functions every instance draws from. Both are per-process, so
# the deployment total is that sum times the worker count, which is the 210
# worked out under AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT. Sizing from the
# sum keeps this tracking whatever those two constants become, so each
# instance's own connection pool is sized to match that sum rather than
# httpx's defaults (100 / 20) or either semaphore alone: capping it at just
# one pool's limit would let that pool's own connections fill an instance's
# ceiling and leave the other pool queuing on a free connection despite
# still having permits free on its own semaphore -- exactly the hidden
# cross-pool contention the two-pool split exists to avoid. Keeping every
# connection up to that sum alive -- instead of the smaller default
# keepalive pool -- means a fan-out that reuses the same client across
# dozens to hundreds of sequential requests (measured: 179 for Christie, 651
# for Conan Doyle -- see AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT's own
# docstring) gets a reused, already-negotiated connection almost every time
# instead of paying a fresh TCP+TLS handshake per call.
#
# keepalive_expiry overrides httpx's 5.0s default because the waste this
# pool exists to avoid falls between walks, not inside one: new connections
# opened per walk are at most the permit count, so nothing goes idle for
# five seconds while a 4-5s walk is running. The next request pays instead
# -- arriving more than five seconds after the last Audible traffic, it
# finds an empty pool and re-handshakes once per permit its limit allows,
# 25 CONNECT+TLS for an author-books walk. That count is environment
# independent; only the price per handshake moves, and through the
# production proxy each one is a full CONNECT+TLS. 120s is the longest idle
# gap measured to still be reused end to end, probed against Audible on a
# direct path with no proxy configured (gaps of 1, 6, 20, 30, 60 and 120s
# all reused a live connection; at the 5.0s default a 6s gap discarded the
# whole pool), and it sits there rather than higher because holding a socket
# past the far end's own idle limit only hands out connections it has
# already closed -- 120s is what was observed, anything beyond it is an
# assumption. On the production path the far end holding the idle tunnel is
# the proxy rather than Audible, and the proxy's own idle tolerance was
# never probed, so 120s is a direct-path measurement carried over, not a
# production-verified ceiling.
#
# ONE MORE THING THIS BOUNDS, AND ONE IT DOES NOT: httpx.Limits here is
# per-client CONFIGURATION, not a shared budget -- verified in the pinned
# httpx 0.28.1 source, AsyncHTTPTransport.__init__ builds a fresh
# httpcore.AsyncConnectionPool per client from these three ints. The two
# semaphores above bound in-flight REQUESTS process-wide, across any number
# of LibexClient instances, because every instance calls the same two
# module-level semaphore functions; they say nothing about CONNECTIONS. N
# instances is N times this sum in sockets against one exit IP -- up to
# N x 35 today -- with up to that same N x 35 of them able to sit idle for
# the full keepalive_expiry=120.0s, since max_connections and
# max_keepalive_connections below are set to the identical sum: every
# connection this pool opens is also one it is willing to keep idle, not
# some smaller subset of it. This does not break anything a single-instance
# process ships, and does not need fixing on that basis; it would matter
# only the moment a second instance is constructed in the same process. Do
# not "fix" this by sharing one httpx.AsyncClient across instances -- that
# re-couples instances that are supposed to be independent, defeats
# per-instance aclose() (closing one would tear down another instance's
# traffic mid-flight), and nothing today needs it.
_AUDIBLE_POOL_LIMITS = httpx.Limits(
    max_connections=AUDIBLE_CONCURRENCY_LIMIT + AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT,
    max_keepalive_connections=AUDIBLE_CONCURRENCY_LIMIT + AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT,
    keepalive_expiry=120.0,
)


async def _close_stale_client(client: httpx.AsyncClient) -> None:
    """Closes a client left behind by a loop change. Best-effort: the client
    may never have opened a real connection (nothing to close), and awaiting
    aclose() on a loop other than the one that built it is unusual enough
    that a failure here should never surface as this request's error."""
    try:
        await client.aclose()
    except Exception:
        logger.debug("Failed closing a stale Audible HTTP client", exc_info=True)


# _close_stale_client above runs as a background task rather than being
# awaited inline, since the client it closes is being replaced precisely
# because it belongs to a loop the caller is no longer running on. A bare
# asyncio.create_task is fire-and-forget: the event loop holds only a weak
# reference to an unretained task, so it can be garbage-collected mid-
# execution, and RUF006 -- the ruff rule that would catch this -- is not in
# ruff's default rule selection. Held here instead, for the lifetime of the
# close, with a done-callback that discards it once finished. This set is
# shared across every LibexClient instance and every stale client any of
# them ever discards -- retaining a task reference until it finishes is a
# garbage-collection concern, not a per-instance one, so there is nothing
# for splitting it per instance to buy.
_pending_client_closes: set[asyncio.Task[None]] = set()


def _track_pending_close(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _pending_client_closes.add(task)
    task.add_done_callback(_pending_client_closes.discard)


# ============================================================
# RETRY / BACKOFF
# ============================================================

# 429 and 5xx are the only responses worth retrying: they mean Audible (or
# its edge) is asking for a retry, not answering the request. A 404 is a
# real, permanent answer -- this database carries ~84k ISBN-keyed records
# that 404 for chapters in every region, and retrying those would just burn
# requests against the same already-throttled-once IP for nothing. Every
# other 4xx is a real answer too and is left alone the same way.
_RETRYABLE_STATUS_CODES = {429}


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES or 500 <= status_code < 600


# Kept small on purpose. AUTHOR_BOOKS_TIME_BUDGET_SECONDS in authors/__init__.py caps
# a whole discovery walk's wall-clock time, but that deadline is checked by
# the callers between requests -- it never reaches LibexClient.get, since
# this method's signature (region, path, params, extra_headers) doesn't
# carry one. A wide fan-out (author-books discovery alone can fire ~60 concurrent
# requests) turning every throttled response into several extra seconds
# would eat into that budget fast with no way for this module to know it's
# happening, so attempts and backoff both stay deliberately small rather
# than aggressive. Making retries budget-aware would need an explicit
# optional deadline parameter threaded from authors.py's existing deadline
# value down through every intermediate call into LibexClient.get itself --
# that's a real signature change and out of scope here.
AUDIBLE_MAX_ATTEMPTS = 3
AUDIBLE_RETRY_BASE_SECONDS = 0.5
AUDIBLE_RETRY_MAX_BACKOFF_SECONDS = 8.0
# Retry-After is Audible telling us exactly how long it wants us to wait.
# Honoring it is the point, but it's still capped so one large value can't
# stall a fan-out far past what a few retries should ever cost.
AUDIBLE_RETRY_AFTER_CAP_SECONDS = 10.0


def _parse_retry_after(value: str | None) -> float | None:
    """Parses a Retry-After header, which per spec is either a number of
    seconds or an HTTP-date. Returns None on anything unparseable so the
    caller falls back to computed backoff instead of guessing."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (retry_at - now).total_seconds())


def _compute_backoff_seconds(attempt: int, retry_after: float | None) -> float:
    """attempt is the zero-indexed count of attempts already made. Retry-After,
    when present, wins outright (capped); otherwise full-jitter exponential
    backoff, so a burst of concurrent callers hitting the same throttle
    don't all retry in lockstep."""
    if retry_after is not None:
        return min(retry_after, AUDIBLE_RETRY_AFTER_CAP_SECONDS)
    ceiling = min(
        AUDIBLE_RETRY_MAX_BACKOFF_SECONDS,
        AUDIBLE_RETRY_BASE_SECONDS * (2 ** attempt),
    )
    return random.uniform(0, ceiling)


# ============================================================
# CLIENT
# ============================================================

def validate_region(region: str) -> str:
    """Validates and normalises region string. Raises RegionException if invalid."""
    region = region.lower().strip()
    if region not in VALID_REGIONS:
        raise RegionException(region)
    return region


def get_audible_url(region: str, path: str) -> str:
    """
    Builds a full Audible API URL for the given region and path.

    path must be an absolute path on the Audible host itself: it must start
    with a single "/", not with two, and must contain no backslash
    character. Plain f-string concatenation of a scheme and a caller-
    supplied path has no separator guarantee, so without this check a
    crafted path can take over the host entirely -- a leading "@" turns the
    rest of the built string into userinfo and moves the host to whatever
    follows it, a leading "." extends the intended host into a longer one
    the caller controls, a leading ":" overrides the port, and a leading
    backslash is treated by some parsers as a path separator and by others
    as part of the host. All four are rejected outright by the check below,
    before any URL is built from path at all. Raises ValueError, a caller
    error rather than an Audible failure -- the message may name the
    offending path, since it is the caller's own input, but this function
    never receives, and so can never leak into that message, any params or
    headers a caller also passed alongside it.

    The second check, after the URL is built, is deliberately redundant
    with the first: it parses the finished URL and asserts the host, scheme
    and port are exactly what this function meant to build, so a bypass of
    the first check -- found later, or introduced by some future edit to it
    -- still cannot reach a host or port this function did not intend.
    """
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError(f"invalid Audible API path: {path!r}")
    tld = REGION_MAP.get(region, ".com")
    url = f"https://api.audible{tld}{path}"
    parsed = httpx.URL(url)
    expected_host = f"api.audible{tld}"
    if (
        parsed.host != expected_host
        or parsed.scheme != "https"
        or parsed.port not in (None, 443)
    ):
        raise ValueError(f"invalid Audible API path: {path!r}")
    return url


class LibexClient:
    """
    One caller's whole way of reaching the Audible API: one transport,
    decided once at construction and never replaced; shared concurrency and
    retry behaviour with every other LibexClient in the same process (see
    the CONCURRENCY BOUND section above -- those bounds are process-wide by
    design, not per-instance); and one lazily-built httpx client kept in
    step with whichever event loop is currently running it.

    Typical use, as an async context manager:

        async with LibexClient(proxy_url=..., allow_direct_egress=...) as client:
            data = await client.get(region, path)

    Or, for a caller managing its own lifetime directly: construct it, call
    get() as many times as needed, and await aclose() exactly once when
    done with it. Every get() after that raises RuntimeError rather than
    reopening -- see aclose()'s own docstring for why a quiet reopen is
    exactly the behaviour this class refuses to have.
    """

    def __init__(self, *, proxy_url: str | None, allow_direct_egress: bool = False) -> None:
        """
        Decides, once, how this instance's Audible traffic egresses:
        through an HTTP(S) proxy, or directly on this process's own IP.
        There is no default for proxy_url -- omitting it is a TypeError
        from Python's own argument checking, not a runtime guard this
        constructor has to remember to enforce, which is what makes
        "nobody ever decided" an unrepresentable state rather than one this
        module has to detect.

        None or "" configure direct egress, but only alongside
        allow_direct_egress=True -- passed without it, a blank proxy_url
        raises ValueError instead of silently egressing unproxied. The two
        are refused together on purpose: this constructor cannot tell "I
        want this instance's traffic to leave on its own IP" from "my proxy
        setting happened to come through empty", and the second is
        indistinguishable, at the wire, from the first unless something
        forces the caller to say which one they meant. A caller building
        LibexClient(proxy_url=None) by forgetting to configure a proxy,
        rather than by deciding against one, is exactly the caller this
        guards against -- a hosted deployment with one operator-controlled
        egress IP can make that decision deliberately, at its own single
        point of construction; an embedded copy distributed to run on many
        separate machines cannot make it silently, because every one of
        those machines would egress on its own IP together with whatever
        ASINs it looks up -- a caller's reading history, from their own
        address, with nothing here to stop it.

        Any other value for proxy_url is parsed eagerly, right here, as a
        proxy URL, and must name an http or https scheme, a host, and a
        port (explicit, or the scheme's own default); anything else raises
        ValueError with a fixed message that never contains any part of the
        supplied value. That's deliberate, not merely tidy: a scheme-less
        value such as a bare "user:pass@host:port" makes httpx's own proxy
        parser raise a ValueError whose message embeds the credentials, and
        that exception has reached a log field before -- eagerly validating
        here, and re-raising a message with none of the original text via
        `from None`, is what stops it happening again. Raising here also
        means a malformed value crashes at construction, at the caller's
        own call site, rather than silently resolving to direct egress.
        allow_direct_egress is ignored whenever proxy_url is non-empty --
        it only ever governs the blank-proxy case.

        There is no reconfigure. This instance's transport is decided here
        and never replaces itself for the rest of this instance's life -- a
        caller that wants a different proxy constructs a second,
        independent LibexClient rather than mutating this one. The two
        share this module's process-wide concurrency semaphores (see the
        CONCURRENCY BOUND section above) regardless, since those bound one
        exit IP's fan-out rather than one instance's.

        Builds no httpx client here, but not because building one earlier
        would raise: constructing an httpx.AsyncClient with no event loop
        running at all does not fail, and a client built that way goes on
        to work the first time it is actually used, on whichever loop
        happens to be running then. What lazy construction actually guards
        against is what happens after a client has been used at least
        once: a connection it opens belongs to the loop that was running
        when that connection was opened, so a client built once here and
        then reused across a genuine change of running loop -- a
        long-lived uvicorn loop starting after this constructor ran at
        import time with no loop running yet, or a test suite that hands
        every test its own fresh loop -- would carry keepalive connections
        forward that belong to a loop that has since gone away. The client
        is built lazily instead, by _get_client below, keyed to whichever
        loop is actually running each time this instance is used, and
        rebuilt whenever that loop changes, so no live instance ever sends
        a request down a connection opened on a loop that is no longer
        running.
        """
        if not proxy_url:
            if not allow_direct_egress:
                raise ValueError(
                    "a blank or missing proxy URL does not configure direct "
                    "egress by itself -- pass allow_direct_egress=True to "
                    "LibexClient() if egressing to Audible without a proxy "
                    "is really what's intended, so a proxy setting that "
                    "came through empty by mistake fails loudly instead of "
                    "sending every request out on this process's own IP"
                )
            proxy, host = None, None
        else:
            try:
                proxy = httpx.Proxy(proxy_url)
            except Exception:
                raise ValueError("proxy URL could not be parsed") from None
            scheme = proxy.url.scheme
            if scheme not in _VALID_PROXY_SCHEMES:
                raise ValueError("proxy URL must use the http or https scheme") from None
            host = proxy.url.host
            port = proxy.url.port or _DEFAULT_PROXY_PORTS.get(scheme)
            if not host or not port or not (0 < port < 65536):
                raise ValueError("proxy URL must include a host and a valid port") from None

        self.__transport = _TransportSnapshot(proxy=proxy, host=host)
        self._closed = False
        self.__http: httpx.AsyncClient | None = None
        self.__http_loop: asyncio.AbstractEventLoop | None = None

    @property
    def _proxy(self) -> httpx.Proxy | None:
        """The validated httpx.Proxy this instance sends every request
        through, or None in direct-egress mode. Read-only -- there is no
        setter, because reassigning this to reconfigure egress after
        construction is exactly the per-instance immutability this class
        exists to provide.

        Single-underscore rather than public on purpose: this is not part
        of the published API surface and is not covered by the public-
        surface freeze that applies to the rest of this class. It exists
        for exactly one class of reader -- an operator script confirming
        which proxy object actually carries a running process's Audible
        traffic -- where the alternative, re-deriving a proxy from
        configuration elsewhere, could silently drift from what this
        instance actually validated and is using right now.
        """
        return self.__transport.proxy

    @property
    def is_open(self) -> bool:
        """True only when a live httpx.AsyncClient exists for this instance
        right now. Both "never built a client yet" and "aclose() has
        already run" read False -- this is deliberately NOT the complement
        of httpx's own AsyncClient.is_closed: a client that was never built
        at all has no is_closed to ask, and is_open must still read False
        for it, the same as for one this instance has since closed."""
        return self.__http is not None

    def _get_client(self) -> httpx.AsyncClient:
        """
        Builds or reuses this instance's httpx client, keyed to the
        currently running event loop the same way the module-level
        semaphores above are -- built lazily, rebuilt whenever the running
        loop changes, so a long-lived uvicorn process builds it once and a
        test suite that hands every test its own loop gets a fresh client
        per test instead of reusing connections tied to a loop that has
        already gone away.

        Refuses outright, with RuntimeError, once this instance has been
        closed -- and never rebuilds after that, for any reason, including
        a loop change. A closed instance that quietly rebuilt on next use
        would let a task that raced the close see nothing wrong and egress
        anyway, which is exactly the guarantee aclose() exists to give a
        caller who closed specifically to stop it (shutdown, revoked
        consent, a task this instance was never meant to outlive).
        """
        if self._closed:
            raise RuntimeError(
                "this LibexClient has been closed and will not reopen -- "
                "construct a new LibexClient instead of reusing one that "
                "has had aclose() called on it"
            )
        loop = asyncio.get_running_loop()
        if self.__http is None or self.__http_loop is not loop:
            stale = self.__http
            # trust_env=False: httpx's default otherwise reads HTTPS_PROXY
            # and SSL_CERT_FILE/SSL_CERT_DIR straight from the process
            # environment, which would make this constructor's explicit
            # direct/proxy choice only half the story -- an unrelated
            # environment variable could still redirect this instance's
            # traffic. The proxy this client uses is exactly, and only,
            # what this instance was constructed with.
            self.__http = httpx.AsyncClient(
                proxy=self.__transport.proxy,
                limits=_AUDIBLE_POOL_LIMITS,
                trust_env=False,
            )
            self.__http_loop = loop
            if stale is not None:
                _track_pending_close(_close_stale_client(stale))
        return self.__http

    async def get(
        self,
        region: str,
        path: str,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """
        Makes a GET request to the Audible API through this instance's
        transport. Returns parsed JSON response.
        Raises AudibleAPIException on non-200 responses.
        Raises RuntimeError, before any request is sent, if this instance
        has been closed -- see aclose()'s own docstring.

        extra_headers overlays get_region_headers for this call only and
        must contain module-level constants only -- never a request-derived
        value.

        Bounded process-wide by AUDIBLE_CONCURRENCY_LIMIT concurrent
        in-flight requests regardless of caller or instance, and retries a
        429 or 5xx up to AUDIBLE_MAX_ATTEMPTS times with backoff (see the
        CONCURRENCY BOUND and RETRY / BACKOFF sections above). A 404 stays
        terminal and is never retried; neither is any other 4xx, nor a
        timeout or connection failure.

        Which of the two concurrency pools this call draws from is read
        from a ContextVar (_current_audible_semaphore), never a parameter
        here -- see author_books_concurrency's own docstring for why.

        This instance's closed state is checked before the first semaphore
        acquire, and the client is fetched inside the retry loop, once per
        attempt, after that attempt's semaphore permit is already held --
        never before it. Acquiring the permit is itself an await, and under
        real contention on either shared pool that wait can run long enough
        for aclose() to complete on this instance from another task while
        it is still pending -- shutdown, revoked consent, a caller simply
        done with this instance. Fetching the client before acquiring the
        permit, as a single call outside this loop's per-attempt block,
        would leave that fetch's result live across the wait: its closed
        check would already have passed by the time aclose() ran, so
        nothing between the fetch and the eventual send would notice, and
        httpx raises a plain RuntimeError on a closed client, not a
        RequestError, so it would escape the except clauses below entirely
        rather than being retried or reported as the closed-instance error
        it actually is. Fetching the client only after the permit is held,
        with no further await before client.get() is called, closes that
        window: a close landing during the wait is caught by _get_client's
        own closed check instead, which raises its own explicit
        RuntimeError before any request is attempted. A close landing
        after the client is fetched, while a request is already in flight,
        is the separate case the RequestError branch below exists to
        handle.
        """
        if self._closed:
            raise RuntimeError(
                "this LibexClient has been closed and refuses to make "
                "further requests -- construct a new LibexClient instead "
                "of reusing one that has had aclose() called on it"
            )
        region = validate_region(region)
        url = get_audible_url(region, path)
        headers = get_region_headers(region)
        if extra_headers:
            headers = {**headers, **extra_headers}

        for attempt in range(AUDIBLE_MAX_ATTEMPTS):
            async with _current_audible_semaphore():
                # Fetched here, after the permit above is already held, and
                # not before: this call is synchronous, so nothing can run
                # between it and the client.get() below, which is exactly
                # what keeps a close landing during the semaphore wait from
                # leaving a live reference to a now-closed client for this
                # attempt to send through -- see this function's own
                # docstring for the failure that ordering closes.
                client = self._get_client()
                try:
                    response = await client.get(
                        url,
                        headers=headers,
                        params=params,
                        timeout=30.0,
                        follow_redirects=False,
                    )
                except httpx.TimeoutException as e:
                    raise AudibleAPIException(
                        f"Audible API timed out: {type(e).__name__} for {url}"
                    )
                except httpx.RequestError as e:
                    # Many httpx.RequestError subclasses (ConnectError, ReadError, etc.)
                    # have an empty str(), so include the type and URL or the message is
                    # blank and the failure is undiagnosable. This also catches the
                    # transport error httpx raises when aclose() tears down this exact
                    # client's connections out from under a request already reading
                    # from one -- a RequestError, not the bare RuntimeError a fully
                    # closed client raises on its next call, so it converts correctly
                    # here rather than escaping. It is not retried, since a 404-shaped
                    # status was never received and this exception is not in
                    # _RETRYABLE_STATUS_CODES, and retrying against an instance whose
                    # owner just closed it would defeat the close.
                    detail = str(e) or type(e).__name__
                    raise AudibleAPIException(
                        f"Audible API request failed: {detail} for {url}"
                    )

            if response.status_code == 404:
                raise NotFoundException()

            if response.status_code == 200:
                return response.json()

            if _is_retryable_status(response.status_code):
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                attempts_left = AUDIBLE_MAX_ATTEMPTS - attempt - 1
                # A 429 is the early warning for the exact failure that cost
                # this project a VPN rotation once already, so it's logged
                # every time it's seen, whether or not this call still has
                # attempts left to absorb it -- a retry succeeding on the
                # next attempt must not make this go quiet. pool is included so
                # a throttle traced to the wider author-books pool (see
                # AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT) is distinguishable at
                # the log line from one on the default pool every other caller
                # still uses, rather than only visible by cross-referencing path.
                logger.warning(
                    "Audible API throttled or degraded",
                    extra={
                        "status_code": response.status_code,
                        "region": region,
                        "path": path,
                        "pool": _audible_concurrency_pool.get(),
                        "attempt": attempt + 1,
                        "max_attempts": AUDIBLE_MAX_ATTEMPTS,
                        "retry_after": retry_after,
                        "attempts_left": attempts_left,
                    },
                )
                if attempts_left > 0:
                    sleep_for = _compute_backoff_seconds(attempt, retry_after)
                    await asyncio.sleep(sleep_for)
                    continue

            raise AudibleAPIException(
                f"Audible API returned {response.status_code} for {url}",
                upstream_status=response.status_code,
            )

    def transport_summary(self) -> TransportSummary:
        """Read-only view of this instance's configured transport -- see
        TransportSummary's own docstring for exactly what it does and
        doesn't expose. Reflects what this instance was constructed with;
        unaffected by aclose() or by any loop-driven client rebuild, since
        neither one changes the transport itself."""
        mode = "proxy" if self.__transport.proxy is not None else "direct"
        return TransportSummary(mode=mode, host=self.__transport.host)

    async def aclose(self) -> None:
        """
        Closes this instance's transport permanently. Idempotent, and safe
        to call even when get() was never called and no httpx client was
        ever built -- it still marks this instance closed and returns
        without raising. Terminal: once closed, this instance refuses every
        further get() (see get()'s own docstring) and _get_client never
        rebuilds a client for it again, for any reason, including a change
        of running event loop.

        That refusal is why this does not just discard the stored client
        reference the way a stale, loop-changed client is discarded
        elsewhere in this module -- doing only that would leave
        _get_client unable to tell "never built yet" from "closed on
        purpose", and a task racing this close on a new loop would rebuild
        a live client and egress right after a caller closed specifically
        to stop it. The `_closed` flag carries that distinction instead,
        and is checked ahead of every request this instance would otherwise
        make, not only here.
        """
        if self._closed:
            return
        self._closed = True
        client, self.__http, self.__http_loop = self.__http, None, None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> "LibexClient":
        """Returns self and builds nothing -- building a client here would
        open a second build path alongside _get_client's lazy one, splitting
        the loop-rebuild logic this module keeps in exactly one place."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


# ============================================================
# EXCEPTION HELPERS
# ============================================================

def upstream_status_of(exc: BaseException) -> int | None:
    """
    Returns the HTTP status Audible actually sent, when exc is an
    AudibleAPIException carrying one, else None. Shared by every warning log
    that precedes raising an AudibleAPIException and by as_audible_failure
    itself, so the status a caller logs and the status it raises always
    agree instead of being derived twice and risking drift between them.
    """
    return exc.upstream_status if isinstance(exc, AudibleAPIException) else None


def as_audible_failure(exc: Exception, message: str) -> AudibleAPIException:
    """
    Turns a caught exception into the type every Audible service module
    raises when it could not find out whether a record exists, as distinct
    from NotFoundException, which is reserved for Audible actually
    answering and saying no.

    Always carries `message` -- the calling site's own description of what
    it was doing when the caller lost the ability to answer -- rather than
    whatever str(exc) happened to be. That matters most when exc is itself
    an AudibleAPIException: LibexClient.get's own message is built from the
    request URL (`f"Audible API returned {status} for {url}"`), which is
    both meaningless to a caller of the service layer and an internal
    Audible endpoint that has no business reaching a response body.

    upstream_status is carried over from exc when exc is an
    AudibleAPIException -- the real HTTP status Audible sent, or None when
    no response ever arrived at all -- so that distinction survives the
    message rewrite. Anything else -- a bug elsewhere in the surrounding
    service logic, caught by the same broad `except Exception` that also
    catches a genuine transport failure -- gets upstream_status left at
    None, since there is no HTTP response to attribute it to either. Both
    cases mean the same thing to a caller of the service layer: the
    record's existence was never actually established, so a 404 would be
    asserting something nobody confirmed.
    """
    upstream_status = upstream_status_of(exc)
    return AudibleAPIException(message, upstream_status=upstream_status)
