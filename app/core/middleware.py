"""
Middleware configuration for Libex.
CORS and request validation.
"""

# Standard library
import re
import time
import unicodedata
import urllib.parse
import uuid

# Third party
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Services
from app.services.audible.client import validate_region

# Core
from app.core.logging import get_logger
from app.core.exceptions import RegionException
from app.core.migration_notice import MigrationNotice, MIGRATION_HEADER_NAMES, is_new_host_request
from app.core.response_headers import HEADER_REQUEST_ID, EXPOSED_HEADER_NAMES

logger = get_logger()


# ============================================================
# INPUT VALIDATION
# ============================================================

ASIN_PATTERN = re.compile(r'^[A-Z0-9]{10}$')


def is_valid_asin(asin: str) -> bool:
    """Validates that a string matches Audible ASIN format."""
    return bool(ASIN_PATTERN.fullmatch(asin.upper()))


# ============================================================
# REGION VALIDATION
# ============================================================

def valid_region(
    region: str = Query(default="us", description="Audible region code")
) -> str:
    """FastAPI dependency that validates and normalises region parameter."""
    try:
        return validate_region(region)
    except RegionException:
        raise


# ============================================================
# HTTP LOGGING
# ============================================================

# Query params whose values may be logged verbatim: region selectors,
# pagination, sort order, and catalogue facets. Every one of them describes HOW
# a caller asked, never WHAT they typed. Membership is necessary but not
# sufficient -- the value must also pass is_safe_log_value, and one that does not
# is replaced by the sentinel with its key kept, exactly like a param that is
# known but not listed here. A key in neither set is dropped whole, pair and
# all; see _KNOWN_QUERY_PARAMS.
#
# An allowlist rather than a denylist, deliberately. A denylist leaks every
# param added after it was written, silently and by default, and the failure
# is invisible until someone reads the logs. This fails closed instead, in both
# directions: nothing reaches the log line until it is listed, here for its
# value and below for its key.
_SAFE_QUERY_PARAMS = frozenset({
    "region", "book_region", "cache", "limit", "page", "sort", "order",
    "flat", "depth", "days", "source", "products_sort_by",
    "category", "genre", "book_format", "language", "plan_name",
    "explicit", "has_pdf", "is_vvab", "whisper_sync",
    "audiobooks_produced", "cultural_heritage", "gender",
    "longer_than", "shorter_than", "rating_better_than", "rating_worse_than",
})

# Every query param name a Libex route accepts. The keys above, plus the ones
# whose value is caller-authored and so is replaced while the key is kept.
#
# A key is written to the log only if it appears here, because a key matching
# no route param is not a param name at all -- it is caller text that reached
# key position. That happens without any hostile intent: an unencoded "&" in
# something typed splits mid-value ("title=Salt&Pepper" parses as title=Salt
# plus a bare "Pepper" key), and a query string carrying no "=" becomes one
# bare key holding the whole typed string. Unrecognised keys are dropped, not
# redacted in place -- see _redact_query.
#
# Written out rather than derived from the router, since deriving it would
# have this module import the app that installs it. Drift fails closed: a
# param added to a route and not added here is dropped from the log line,
# never leaked into it.
_KNOWN_QUERY_PARAMS = _SAFE_QUERY_PARAMS | frozenset({
    "asins", "author", "author_name", "content_delivery_type", "content_type",
    "copyright", "description", "is_buyable", "is_listenable", "isbn",
    "keywords", "name", "narrator", "publisher", "query", "search",
    "series_name", "subtitle", "summary", "title",
})

# Deliberately free of characters urlencode would escape -- "<redacted>"
# comes back as "%3Credacted%3E", which is unreadable in exactly the logs
# this field exists to make readable.
_REDACTED = "REDACTED"

# Reports that unrecognised params were present without keeping any of their
# text. Leads with an underscore, which no route param does, and a caller
# sending it is itself unrecognised and counted like any other, so the marker
# only ever appears with a count Libex produced.
_UNRECOGNISED = "_unrecognised"

# An allowlisted key earns its value logged verbatim only if that value looks
# like the short token these params take -- a region code, a number, an enum, a
# facet name in any language.
#
# Judged by Unicode category rather than by a list of permitted characters,
# because the vocabulary being judged is Audible's and it grows without notice.
# A literal list has to be extended for every taxonomy a marketplace adds, and
# the character it misses next is region-specific and invisible from a US test
# run. Measured across all eleven live taxonomies, an ASCII-shaped rule lost
# 83% of top-level genre names in us/uk/ca/au/in to "&" alone, all 402 jp names
# built on "・", the fr and it names spelled with U+2019 instead of the ASCII
# apostrophe, and every Devanagari, Tamil and Thai name whose letters carry a
# combining mark -- those redact on their letters, with no punctuation in them
# at all. Letters, marks, numbers, punctuation, symbols and spaces are what a
# catalogue name is made of in every script, so that is what the rule names.
_SAFE_VALUE_CATEGORIES = frozenset({
    "Lu", "Ll", "Lt", "Lm", "Lo",              # letters, every script
    "Mn", "Mc", "Me",                          # combining marks
    "Nd", "Nl", "No",                          # numbers
    "Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po",  # punctuation
    "Sc", "Sk", "Sm", "So",                    # symbols
    "Zs",                                      # spaces, ASCII and ideographic
})

# Excluded by name from the punctuation and symbol categories above, because
# they are how a whole second query rides inside one value: in "?region=us;
# Jane+Doe" the whole of "us;Jane Doe" is one param to parse_qsl, which has not
# split on ";" since CVE-2021-23336, so without this the typed name is logged
# verbatim.
_UNSAFE_VALUE_CHARS = frozenset(";=")

# The categories the set above leaves out are left out deliberately: the
# control and format categories, which is where CR, LF, NUL and U+0085 live,
# and the line and paragraph separators U+2028 and U+2029. None of them belongs
# in a catalogue name, and any of them would put part of a value on a log line
# of its own. urlencode percent-encodes them downstream regardless; the
# guarantee is held here as well rather than borrowed from a later caller.

# The longest of 6,787 genre names measured across the eleven marketplaces is
# 62 characters, so the bound costs no real vocabulary. It counts decoded
# characters rather than bytes, so a name in a multi-byte script is not charged
# for its encoding.
_MAX_VALUE_LENGTH = 64


def is_safe_log_value(value: str) -> bool:
    """
    True if a value is short catalogue vocabulary rather than caller text.

    Public rather than local to this module: it is the one place the
    value-safety judgment is made, and every other log field that could embed
    caller-supplied text -- a cache key built from a query param, for one --
    calls this rather than growing its own copy of the same rule.
    """
    if len(value) > _MAX_VALUE_LENGTH:
        return False
    return all(
        ch not in _UNSAFE_VALUE_CHARS
        and unicodedata.category(ch) in _SAFE_VALUE_CATEGORIES
        for ch in value
    )


def _redact_query(raw_query: str) -> str:
    """
    Reduces a query string to the parts that describe how a caller asked.

    Libex logs the query string because the path alone cannot answer which
    params consumers actually use -- but on the search and name-lookup routes
    that string is whatever a person typed, and Libex deliberately records
    nothing that identifies a caller and nothing a caller typed. Keeping a
    recognised key and dropping its value preserves the operational answer
    ("this route was called with name=") without keeping the content.

    A key Libex does not recognise is caller text rather than a param name, so
    there is no form of it worth keeping: the pair goes, and only a count of
    how many went is reported, which still tells an operator that unexpected
    params arrived.

    Nothing here raises, whatever the URL contains. Losing the query field on
    one log line beats losing the request, and beats falling back to the raw
    string, which would leak exactly what this exists to withhold. No input
    tested reaches that fallback -- parse_qsl does not raise without strict
    parsing -- and it stays because the cost of being wrong about that is the
    leak itself.
    """
    if not raw_query:
        return ""

    try:
        logged = []
        unrecognised = 0

        for key, value in urllib.parse.parse_qsl(raw_query, keep_blank_values=True):
            if key not in _KNOWN_QUERY_PARAMS:
                unrecognised += 1
                continue
            logged.append((key, _logged_value(key, value)))

        if unrecognised:
            logged.append((_UNRECOGNISED, str(unrecognised)))

        return urllib.parse.urlencode(logged)
    except (ValueError, UnicodeDecodeError):
        return ""


def _logged_value(key: str, value: str) -> str:
    """Returns an allowlisted key's value if it is safe to log, else the sentinel."""
    if key in _SAFE_QUERY_PARAMS and is_safe_log_value(value):
        return value
    return _REDACTED


# /health builds a small dict and returns it -- no database, no cache, no
# outbound call, nothing in it that can be slow on its own. So its duration
# measures the event loop rather than the endpoint, and a value at or above
# this one means the loop could not get back to a handler that does no work.
#
# Logging every hit was considered and rejected: the container healthcheck
# alone polls every 30s -- ~2880 lines a day that say nothing -- and external
# uptime monitoring polls on top of it. A threshold keeps a healthy process at
# zero lines for this path while producing exactly the lines an outage needs,
# which is why the field set below is a duration and little else -- there is no
# caller input to describe.
#
# One second, not a value tuned to the 30s the reverse proxy gives up at. The
# useful signal starts far below the point any caller notices: a /health that
# took two seconds is the same starvation as one that took twenty, seen
# earlier. It also sits well above any benign scheduling jitter or GC pause on
# a loop that is keeping up, so sitting this low costs no false lines.
_SLOW_HEALTH_MS = 1000.0


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Minted here and nowhere else. Never taken from a caller-supplied
        # X-Request-Id header, even though echoing one back is the "standard
        # practice" a later change will be tempted to add: an id a caller
        # chose is an id that same caller can reuse to correlate their own
        # requests to Libex with each other, or hand to Libex on someone
        # else's behalf, and either way it is caller-supplied text landing in
        # a log field this module otherwise goes to real lengths (see
        # _redact_query) to keep clean of exactly that.
        request_id = str(uuid.uuid4())
        # Set on request.state before call_next, not after, so it is there
        # for the rest of this request's life -- including the generic 500
        # handler in app.main, which runs inside ServerErrorMiddleware,
        # outside every middleware setup_middleware registers, this one
        # included, and so can read nothing this middleware does after
        # call_next returns. request.state is the one thing shared across
        # every Request object built from the same ASGI scope, which is what
        # makes it reachable from there at all.
        request.state.request_id = request_id
        start = time.monotonic()
        response = await call_next(request)
        took = round((time.monotonic() - start) * 1000, 2)

        # Stamped before the /health branch below returns, so /health
        # carries it too -- this identifies the request, not the work the
        # request triggered, and /health is a request like any other.
        response.headers[HEADER_REQUEST_ID] = request_id

        if request.url.path == "/health":
            if took >= _SLOW_HEALTH_MS:
                # WARNING rather than INFO: the line only exists at all when
                # the value is already abnormal, and it has to be findable
                # without knowing to filter a duration field. It is not an
                # ERROR -- the check itself succeeded, and what it reports is a
                # process under strain rather than a failure.
                #
                # Nothing caller-derived is available to record here and none is
                # sought: /health takes no input, so there is no query worth
                # keeping, and the same rule as below applies regardless -- this
                # describes the request, never the requester.
                logger.warning(
                    "Health check slow",
                    extra={
                        "url": request.url.path,
                        "status": response.status_code,
                        "took": took,
                    },
                )
            return response

        # No client address is read here, from any header or from the
        # connection. Libex does not record who called it -- not in full, not
        # truncated, not hashed. Every field below describes the request, not
        # the requester, which is what keeps per-endpoint failure rates and
        # latency visible while leaving nothing that identifies a caller.
        #
        # The user agent stays because it names client SOFTWARE, not a person
        # -- it is how a spike gets traced to a particular consumer version.
        # With no address logged alongside it, it cannot be tied back to an
        # individual, so removing it on privacy grounds would blind the
        # monitoring for nothing.
        user_agent = request.headers.get("user-agent", "")
        # The only signal, once both libex.lostcartographer.xyz and libexdb.com serve
        # the same container, that tells old-host traffic apart from new-host traffic.
        host = request.headers.get("host", "")
        logger.info(
            "Request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "url": request.url.path,
                # Path alone cannot answer which parameters consumers actually
                # use. Values are allowlisted -- see _redact_query -- so this
                # answers that without keeping what a caller typed. No secret
                # travels this way either: the internal routes authenticate on
                # an Authorization header, not a query param.
                "query": _redact_query(request.url.query),
                "status": response.status_code,
                "userAgent": user_agent,
                "took": took,
                "host": host,
            },
        )
        return response

# ============================================================
# MIGRATION NOTICE
# ============================================================

class MigrationNoticeMiddleware(BaseHTTPMiddleware):
    """
    Adds the Deprecation/Sunset/Link headers to responses served on the old
    host. Responses served on migration_new_host itself never get them — see
    is_new_host_request — so libexdb.com never announces its own deprecation
    once both hostnames serve the same container. Not reached for a 500 built
    by the bare-Exception handler, since Starlette's ServerErrorMiddleware sits
    outside every middleware registered here; app.main applies the same
    headers itself on that one path.
    """

    def __init__(self, app: FastAPI, notice: MigrationNotice):
        super().__init__(app)
        self._notice = notice

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if not is_new_host_request(self._notice, request.url.hostname):
            response.headers.update(self._notice.headers)
        return response

# ============================================================
# SETUP
# ============================================================

def setup_middleware(app: FastAPI, migration_notice: MigrationNotice | None = None) -> None:
    """Configures all middleware for the application."""

    app.add_middleware(LoggingMiddleware)

    if migration_notice is not None:
        app.add_middleware(MigrationNoticeMiddleware, notice=migration_notice)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
        # dict.fromkeys dedupes while keeping first-seen order -- unlike
        # set(), whose iteration order Access-Control-Expose-Headers (a
        # single comma-joined header value) has no business depending on.
        # There is no overlap between the two tuples today; the union is
        # written this way so one added later still dedupes instead of
        # repeating a name in the header.
        expose_headers=list(dict.fromkeys((*EXPOSED_HEADER_NAMES, *MIGRATION_HEADER_NAMES))),
    )

    logger.info("Middleware configured")