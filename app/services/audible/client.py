"""
Audible API client.
Handles headers, region mapping, and the shared httpx session.
All Audible service files call through this client exclusively.

DESIGN PHILOSOPHY: Audible-first.
Every request hits Audible directly for fresh data.
Cache is used only as a fallback when Audible is unavailable.
This ensures data accuracy and freshness at all times.
"""

# Standard library
import asyncio
import datetime
import random
from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

# Third party
import httpx

# Local
from app.core.config import get_settings
from app.core.exceptions import AudibleAPIException, RegionException
from app.core.logging import get_logger

settings = get_settings()
logger = get_logger()

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
# screen). Scoped per-call via audible_get's extra_headers, never merged into
# get_region_headers — stamping every call with one stable device id across a
# single exit IP is exactly the shape a per-device throttle keys on.
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
# (NAME_SEARCH_CONCURRENCY, SCREENS_FANOUT_CONCURRENCY in authors.py), but
# those only bound one walk at a time -- two simultaneous requests for a
# large author already double the in-flight count, and nothing upstream of
# this module caps the total across every walk running at once. audible_get
# is the one place every outbound Audible call passes through, so the bound
# lives here, process-wide, instead of at any individual call site: a
# per-call-site limit only expresses how eagerly that one walk wants to go,
# never what one event loop and one exit IP carry across all of them at
# once.
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
# alone breaks the second: a 1000-ASIN /books call is 20 concurrent
# audible_get calls, two rounds at 10 permits and ten rounds at 2, against
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


# Selects which pool audible_get acquires from, without adding a parameter to
# audible_get itself or to any of its call sites: a threaded-through pool
# parameter would have to be plumbed through every intermediate fetch
# function in screens.py, catalog.py, and books.py, several of which are
# shared with the seeder and must never pick up the wider pool, and several
# existing tests patch audible_get itself with narrow, fixed-arity stand-ins
# that a new always-passed kwarg would break outright. A ContextVar
# sidesteps both: it's invisible to every call site (none of them change),
# asyncio.gather's own tasks inherit whichever value was current when
# gather() created them (contextvars.copy_context() happens at task
# creation), and it flows unmodified through every further nested await and
# nested gather inside that task -- which is exactly why wrapping only the
# single outer gather in _walk_author_books and the single hydration gather
# in get_books_by_asins (see author_books_concurrency's call sites) is
# enough to cover every audible_get call underneath either, with nothing
# else in either module touched.
_audible_concurrency_pool: ContextVar[str] = ContextVar(
    "_audible_concurrency_pool", default="default"
)


@contextmanager
def author_books_concurrency() -> Iterator[None]:
    """
    Marks every audible_get call made within this block -- directly or via
    any further nested await, task, or gather it spawns -- as belonging to
    the wider AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT pool instead of the
    default AUDIBLE_CONCURRENCY_LIMIT one. Reserved for the live
    author-books discovery and hydration fan-out (see
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
# SHARED HTTP CLIENT
# ============================================================

# The two semaphores together guarantee at most AUDIBLE_CONCURRENCY_LIMIT +
# AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT calls -- 35 -- are ever in flight
# FROM THIS PROCESS across both pools at once. Both are per-process, so the
# deployment total is that sum times the worker count, which is the 210
# worked out under AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT. Sizing from the
# sum keeps this tracking whatever those two constants become, so the shared
# client's own connection pool is sized to match that sum rather than
# httpx's defaults (100 / 20) or either semaphore alone:
# capping it at just one pool's limit would let that pool's own connections
# fill the shared client's ceiling and leave the other pool queuing on a free
# connection despite still having permits free on its own semaphore --
# exactly the hidden cross-pool contention the two-pool split exists to
# avoid. Keeping every connection up to that sum alive -- instead of the
# smaller default keepalive pool -- means a fan-out that reuses the same
# shared client across dozens to hundreds of sequential requests (measured:
# 179 for Christie, 651 for Conan Doyle -- see
# AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT's own docstring) gets a reused,
# already-negotiated connection almost every time instead of paying a fresh
# TCP+TLS handshake per call.
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
_AUDIBLE_POOL_LIMITS = httpx.Limits(
    max_connections=AUDIBLE_CONCURRENCY_LIMIT + AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT,
    max_keepalive_connections=AUDIBLE_CONCURRENCY_LIMIT + AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT,
    keepalive_expiry=120.0,
)

# audible_get is called from three different lifetimes -- request handlers,
# asyncio.create_task background persisters, and the seeder's long-running
# task -- with no single object owning all three, exactly the situation
# _get_audible_semaphore above already solves. An httpx.AsyncClient binds to
# the running loop through its connection pool the same way a Semaphore binds
# through its waiter state, so this follows the identical shape: built lazily,
# keyed to the loop that built it, rebuilt when that loop changes. Under
# uvicorn that's once for the life of the process; under pytest-asyncio,
# which hands every test function its own loop, each test gets its own client
# instead of reusing pooled connections tied to a loop that's already gone.
_audible_client: httpx.AsyncClient | None = None
_audible_client_loop: asyncio.AbstractEventLoop | None = None


async def _close_stale_client(client: httpx.AsyncClient) -> None:
    """Closes a client left behind by a loop change. Best-effort: the client
    may never have opened a real connection (nothing to close), and awaiting
    aclose() on a loop other than the one that built it is unusual enough
    that a failure here should never surface as this request's error."""
    try:
        await client.aclose()
    except Exception:
        logger.debug("Failed closing a stale Audible HTTP client", exc_info=True)


def _get_audible_client() -> httpx.AsyncClient:
    global _audible_client, _audible_client_loop
    loop = asyncio.get_running_loop()
    if _audible_client is None or _audible_client_loop is not loop:
        stale = _audible_client
        # settings is process-wide and never reloaded at runtime (get_settings
        # is lru_cache'd), so reading audible_proxy_url once at construction
        # here -- instead of per call, as the old per-request client did --
        # can't go stale for the life of the process.
        _audible_client = httpx.AsyncClient(
            proxy=settings.audible_proxy_url or None,
            limits=_AUDIBLE_POOL_LIMITS,
        )
        _audible_client_loop = loop
        if stale is not None:
            asyncio.create_task(_close_stale_client(stale))
    return _audible_client


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


# Kept small on purpose. AUTHOR_BOOKS_TIME_BUDGET_SECONDS in authors.py caps
# a whole discovery walk's wall-clock time, but that deadline is checked by
# the callers between requests -- it never reaches audible_get, since this
# function's signature (region, path, params, extra_headers) doesn't carry
# one. A wide fan-out (author-books discovery alone can fire ~60 concurrent
# requests) turning every throttled response into several extra seconds
# would eat into that budget fast with no way for this module to know it's
# happening, so attempts and backoff both stay deliberately small rather
# than aggressive. Making retries budget-aware would need an explicit
# optional deadline parameter threaded from authors.py's existing deadline
# value down through every intermediate call into audible_get itself --
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
    """Builds a full Audible API URL for the given region and path."""
    tld = REGION_MAP.get(region, ".com")
    return f"https://api.audible{tld}{path}"


async def audible_get(
    region: str,
    path: str,
    params: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """
    Makes a GET request to the Audible API.
    Returns parsed JSON response.
    Raises AudibleAPIException on non-200 responses.

    extra_headers overlays get_region_headers for this call only and must
    contain module-level constants only — never a request-derived value.

    Bounded process-wide by AUDIBLE_CONCURRENCY_LIMIT concurrent in-flight
    requests regardless of caller, and retries a 429 or 5xx up to
    AUDIBLE_MAX_ATTEMPTS times with backoff (see the CONCURRENCY BOUND and
    RETRY / BACKOFF sections above). A 404 stays terminal and is never
    retried; neither is any other 4xx, nor a timeout or connection failure.

    Which of the two concurrency pools this call draws from is read from a
    ContextVar (_current_audible_semaphore), never a parameter here -- see
    author_books_concurrency's own docstring for why. Every call site in
    this codebase is unaffected either way; only whether it currently runs
    inside an author_books_concurrency() block changes.
    """
    region = validate_region(region)
    url = get_audible_url(region, path)
    headers = get_region_headers(region)
    if extra_headers:
        headers = {**headers, **extra_headers}

    client = _get_audible_client()
    for attempt in range(AUDIBLE_MAX_ATTEMPTS):
        async with _current_audible_semaphore():
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
                # blank and the failure is undiagnosable.
                detail = str(e) or type(e).__name__
                raise AudibleAPIException(
                    f"Audible API request failed: {detail} for {url}"
                )

        if response.status_code == 404:
            from app.core.exceptions import NotFoundException
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