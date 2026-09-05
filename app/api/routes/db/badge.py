"""
Stats rendered as badge images, served by Libex itself.

The README's counters were `img.shields.io/badge/dynamic/json` badges pointed
at /db/stats, which put a third party between GitHub and the numbers and gave
that third party a veto: shields allows an upstream fetch about 3.5 seconds
before it renders "inaccessible" (their maintainer, badges/shields#10996),
while a cold /db/stats takes 4.7s scoped to us and 15.3s unscoped -- so the
badges that failed were the ones whose cache entry is slowest to recompute,
the five global counters worst of all, on a service that was up and answering
the whole time. All 37 of them now point here instead, which takes shields
out of the chain: GitHub's camo proxy fetches from libexdb.com and nothing
else, one hop, no ceiling set by somebody else's service, and no dependence
on a free tier's availability. Only the counters moved -- the licence badge
and the two container-registry badges are still shields, neither of which
reads a number out of Libex.

Losing the ceiling does not lose the slow recompute underneath it; it changes
what a reader sees when they hit one. shields answered a slow upstream with a
grey "inaccessible" plate, so a stalled fetch was legible as a stalled fetch.
camo has no such answer -- it fetches whatever the origin gives it, however
long that takes -- so the same recompute now surfaces as an image that is
slow to appear or does not appear at all. Making that rare is the cache
policy's job, not this module's: see stats_headers.py.

These routes are a second representation of /db/stats, not a second source of
it: the same get_db_stats accessor, the same cache entry, the same
Cache-Control policy (see stats_headers.py). A badge and the JSON therefore
cannot disagree about a figure, and a badge request costs what the JSON
request costs -- a cache read while the entry is warm, the full count set
across every table once it has lapsed.

Everything drawn is Libex's own text -- a fixed label, a count in digits, or
one of three fixed error strings. No caller-supplied string ever reaches the
SVG, so this endpoint cannot be used to render arbitrary text hosted on
libexdb.com, and the label is chosen by a boolean rather than typed by the
caller for exactly that reason. It also means the character set is closed,
which is what makes the width table below sufficient.
"""

# Standard library
import html
from typing import Annotated, NamedTuple

# Third party
from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from app.api.routes.db.stats_headers import stats_cache_control
from app.core.exceptions import RegionException
from app.db.session import get_session
from app.services.audible.client import validate_region
from app.services.db.reader import get_db_stats

badge_router = APIRouter(prefix="/stats/badge", tags=["Database"])


# ============================================================
# BADGE GEOMETRY
# ============================================================

# The layout constants of shields' "flat" style, reproduced so a badge drawn
# here is pixel-identical to the shields badge of the same text. Every value
# below was read back out of a live shields SVG rather than taken from their
# source, and the reproduction is verified the same way: render a badge here,
# render the equivalent at img.shields.io, diff the numbers.
#
# Hand-rolled rather than taken as a dependency. pybadges and anybadge both
# do this job, but both carry a font-metrics engine to measure arbitrary text
# in Verdana, and arbitrary text is the one thing this endpoint deliberately
# never draws. For a closed set of five labels, three error strings and the
# digits 0-9, that engine reduces to the eight measured integers below -- so
# the dependency would buy a lookup Libex can write out by hand, at the cost
# of a pinned-by-hash package in the supply chain of a public service.
_HORIZ_PADDING = 5
_LABEL_MARGIN = 1
_LABEL_FILL = "#555"
_SHADOW_FILL = "#010101"

# Verdana 11px advance width of a digit. Every digit is the same width, which
# is why a count needs no measuring: a 10-digit string measured 69.0 units
# wide at shields, and 1 through 8 digits match this constant exactly under
# the rounding below.
#
# The constant is not that measurement over ten, which would be 6.90. The
# measurement fixes a band rather than a figure: _round_up_to_odd of
# int(len * width) gives the same result for 1 through 10 digits at any
# per-digit value from 6.9 up to but not including 7.0, and this sits at the
# top of that band. A number outside it changes what is drawn; one inside it
# cannot.
_DIGIT_WIDTH = 6.99

# Used only if a string reaches here that is neither digits nor one of the
# measured strings, which the tables below are written to prevent. It puts
# the badge a pixel or two out rather than raising, because a width lookup is
# not worth a 500 on an image.
_FALLBACK_CHAR_WIDTH = 7.0

# Widths of the fixed strings this module draws, in the same units, measured
# from shields' own rendering of each: `curl img.shields.io/badge/x-<text>-blue`
# and read textLength off the last <text>, divided by 10. Re-measure the same
# way if a label is ever reworded; a wrong number here shows up as a badge
# whose box is slightly too big or too small for its text, never as an error.
_MEASURED_TEXT_WIDTHS = {
    "Books": 33,
    "Books with Chapters": 113,
    "Authors": 43,
    "Narrators": 53,
    "Series": 35,
    "unknown metric": 89,
    "invalid region": 75,
    "no such badge": 81,
}


def _round_up_to_odd(value: int) -> int:
    """
    Round a text width up to the next odd number, as shields does.

    An odd text width makes the centred x-coordinate land on a half unit,
    which becomes a whole number once multiplied by the template's 10x scale.
    Dropping it produces coordinates half a unit off shields' and text that
    sits fractionally wrong inside its box.
    """
    return value + 1 if value % 2 == 0 else value


def _text_width(text: str) -> int:
    """Width of a string in the badge's units, matching shields' rounding."""
    measured = _MEASURED_TEXT_WIDTHS.get(text)
    if measured is not None:
        return measured
    per_char = _DIGIT_WIDTH if text.isdigit() else _FALLBACK_CHAR_WIDTH
    return _round_up_to_odd(int(len(text) * per_char))


def _text_group(x: int, width: int, text: str) -> str:
    """
    One piece of badge text: the drop shadow, then the text itself.

    textLength is emitted so the glyphs are stretched to exactly the width the
    box was sized for. Without it a renderer whose Verdana differs from the
    one measured here lays the text out at its own width and it no longer fits
    the box drawn around it.
    """
    body = html.escape(text)
    length = width * 10
    return (
        '<g transform="scale(.1)">'
        f'<g aria-hidden="true" fill="{_SHADOW_FILL}">'
        f'<text x="{x}" y="150" fill-opacity=".8" filter="url(#blur)" textLength="{length}">{body}</text>'
        f'<text x="{x}" y="150" fill-opacity=".3" textLength="{length}">{body}</text>'
        "</g>"
        f'<text x="{x}" y="140" textLength="{length}">{body}</text>'
        "</g>"
    )


def _render_badge(label: str | None, message: str, color: str) -> str:
    """
    Render one badge.

    label=None gives a single-part badge -- a bare number in the metric's
    colour, for a table whose row and column already say what it counts.
    shields fills the zero-width label box with the message colour in that
    case rather than the usual grey, and that is reproduced here so a
    renderer that rounds the clip path differently cannot show a grey sliver.

    Text is escaped even though every string drawn is Libex's own. It costs
    nothing, and the day someone adds a metric whose label contains an
    ampersand is not the day to discover this was the only thing standing
    between a label and a malformed document.
    """
    message_width = _text_width(message)
    message_box = message_width + 2 * _HORIZ_PADDING

    if label is None:
        label_box = 0
        label_fill = color
        label_group = ""
        message_x = 5 * message_width + 10 * _HORIZ_PADDING
        aria = message
    else:
        label_width = _text_width(label)
        label_box = label_width + 2 * _HORIZ_PADDING
        label_fill = _LABEL_FILL
        label_x = 10 * _LABEL_MARGIN + 5 * label_width + 10 * _HORIZ_PADDING
        label_group = _text_group(label_x, label_width, label)
        message_x = 10 * (label_box - 1) + 5 * message_width + 10 * _HORIZ_PADDING
        aria = f"{label}: {message}"

    total = label_box + message_box
    aria_attr = html.escape(aria, quote=True)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20"'
        f' role="img" aria-label="{aria_attr}">'
        f"<title>{aria_attr}</title>"
        '<filter id="blur"><feGaussianBlur stdDeviation="16"/></filter>'
        '<linearGradient id="s" x2="0" y2="100%">'
        '<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        '<stop offset="1" stop-opacity=".1"/>'
        "</linearGradient>"
        f'<clipPath id="r"><rect width="{total}" height="20" rx="3"/></clipPath>'
        '<g clip-path="url(#r)">'
        f'<rect width="{label_box}" height="20" fill="{label_fill}"/>'
        f'<rect x="{label_box}" width="{message_box}" height="20" fill="{color}"/>'
        f'<rect width="{total}" height="20" fill="url(#s)"/>'
        "</g>"
        '<g fill="#fff" text-anchor="middle"'
        ' font-family="Verdana,Geneva,DejaVu Sans,sans-serif"'
        ' text-rendering="geometricPrecision" font-size="110">'
        f"{label_group}{_text_group(message_x, message_width, message)}"
        "</g></svg>"
    )


# ============================================================
# METRICS
# ============================================================

class BadgeMetric(NamedTuple):
    """The presentation half of a stat: what to call it and what colour it is."""
    label: str
    color: str


# Keyed by the field name /db/stats returns, so a badge URL is derivable from
# the JSONPath the dynamic-JSON badge it replaced read -- $.booksWithChapters
# becomes booksWithChapters.svg, which is what let the README be repointed a
# URL at a time with no lookup table in between. One vocabulary for the JSON
# and the images; a prettier hyphenated spelling would be a second naming
# scheme to keep in sync, with a mapping in the middle to get wrong.
#
# The colours are the ones the README's shields badges painted, read out of
# what shields actually rendered rather than taken from the colour names in
# those URLs, because two of those names do not mean what they look like:
# shields maps its own `blue` to #007ec6, and renders `orange` as #ea7233
# after a contrast adjustment -- neither is the CSS colour of that name.
# `teal` and `purple` it passes through untouched, and those are the CSS
# values below. booksWithChapters was already a literal hex.
#
# Hex throughout, where shields writes the bare CSS name for those two. Same
# pixels, different bytes, and the distinction matters to anyone verifying
# this module by diffing its output against a live shields SVG: most badges
# come out byte-for-byte identical, but authors and series carry `#008080`
# and `#800080` where shields sent `teal` and `purple`. CSS teal is #008080,
# so nothing renders differently. It is a notation difference, not drift.
#
# Only the five counts the README displays are here. seriesRegionUnknown is a
# real /db/stats field and deliberately has no badge: it is the size of a gap
# in the data, which needs the sentence in the JSON route's docstring to mean
# anything, and a number alone in a coloured box would read as a sixth
# library count.
_METRICS = {
    "books": BadgeMetric("Books", "#ea7233"),
    "booksWithChapters": BadgeMetric("Books with Chapters", "#b5179e"),
    "authors": BadgeMetric("Authors", "#008080"),
    "narrators": BadgeMetric("Narrators", "#007ec6"),
    "series": BadgeMetric("Series", "#800080"),
}

# shields' own colour for a badge it could not resolve. Reusing it means a
# broken badge here looks like the broken badges a reader has seen before,
# rather than like a live metric that happens to be grey.
_ERROR_COLOR = "#939393"
_UNKNOWN_METRIC = "unknown metric"
_INVALID_REGION = "invalid region"

# Two error strings that sound alike and are not. "unknown metric" means the
# URL was the right shape and named a count Libex does not have -- check the
# metric name. "no such badge" means the router never recognised the request
# as a badge at all -- check the URL. They point at different fixes, which is
# the whole reason a badge says anything rather than just going grey.
_NO_SUCH_BADGE = "no such badge"


# Rendered once: it never varies, and the route that serves it is the one
# most likely to be hit by something scanning for a hole.
_UNMATCHED_BADGE = _render_badge(None, _NO_SUCH_BADGE, _ERROR_COLOR)


def _svg_response(svg: str, cache_control: str) -> Response:
    """
    Wrap a rendered badge in the response headers an image needs.

    nosniff and a CSP that permits nothing are here because an SVG is a
    document, not just a picture: served from libexdb.com and opened directly
    rather than through an <img>, it would run script in Libex's origin if it
    ever contained any. Nothing rendered here does, and no caller text reaches
    it, so these headers guard against a future change rather than a present
    hole -- which is the cheapest moment to add them.
    """
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": cache_control,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


def _error_badge(message: str) -> Response:
    """
    A badge that says what went wrong, with a 200 and no caching.

    200 on a bad request is the deliberate exception this one surface makes,
    and it is not a precedent for the JSON API. What a caller receives here is
    an image inside an <img> tag: a 404 body is never drawn by GitHub's camo
    proxy, so the honest status code renders as the browser's broken-image
    icon, which tells the reader nothing and tells the person who mistyped the
    URL nothing either. A grey box reading "unknown metric" tells both of them
    exactly what happened, and says it in the title and aria-label too, so it
    is also what a screen reader and a curl get.

    no-store keeps a typo from being cached for a day by a CDN that would
    happily do so, and keeps the mistake visible until it is fixed.
    """
    return _svg_response(_render_badge(None, message, _ERROR_COLOR), "no-store")


@badge_router.get(
    "/{metric}.svg",
    response_class=Response,
    responses={
        200: {
            "content": {"image/svg+xml": {}},
            "description": "An SVG badge. Also returned, in grey, for an unknown metric or an invalid region.",
        }
    },
)
async def get_stats_badge(
    metric: Annotated[
        str,
        Path(description=f"Which count to draw. One of: {', '.join(_METRICS)}."),
    ],
    region: Annotated[
        str | None,
        Query(
            description=(
                "Audible region code. Omit for the global count. Scopes "
                "books/authors/series/booksWithChapters the same way "
                "/db/stats does; narrators has no region column, so a "
                "narrators badge shows the global figure whatever is passed."
            )
        ),
    ] = None,
    label: Annotated[
        bool,
        Query(
            description=(
                "Draw the metric's name beside the number. Pass false for a "
                "bare count, for use in a table whose row and column already "
                "say what it counts."
            )
        ),
    ] = True,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """
    Draw one /db/stats count as an SVG badge.

    Same numbers as `GET /db/stats`, same cache entry, same freshness policy;
    this is that response rendered as an image so a README can embed it
    without a badge service in the middle. The count is drawn exactly as the
    JSON gives it -- digits, no thousands separators -- which is also how
    shields renders the same figure.

    A count of zero is drawn as a zero, including in the case where /db/stats
    is serving its all-zeros fallback because the database was unreachable.
    That is deliberate: the badge is not given a way to tell a real zero from
    a fallback zero, because the JSON does not have one either, and the two
    representations of one figure disagreeing would be worse than either
    answer alone. What marks that case is the Cache-Control the JSON route
    already uses for it -- no-store, so nothing keeps it.

    An unknown metric or an invalid region is answered with a badge saying so
    rather than an error status, for the reason in _error_badge. A request
    that never reaches this function because the router would not match it at
    all is answered the same way, one route below.

    `label` is the one value a caller can send that still gets JSON: one
    FastAPI cannot read as a boolean is a 422 with the API's usual body. That
    is deliberate rather than an oversight -- metric and region are typed into
    a URL from the documentation and get a readable image back when they are
    wrong, while a malformed boolean means a caller building the URL
    programmatically, who is better served by the error the rest of the API
    gives them.
    """
    spec = _METRICS.get(metric)
    if spec is None:
        return _error_badge(_UNKNOWN_METRIC)

    if region is not None:
        try:
            region = validate_region(region)
        except RegionException:
            return _error_badge(_INVALID_REGION)

    result = await get_db_stats(session, region)
    value = result.stats.get(metric)
    if value is None:
        return _error_badge(_UNKNOWN_METRIC)

    svg = _render_badge(spec.label if label else None, str(value), spec.color)
    return _svg_response(svg, stats_cache_control(result.cache_expires_at))


@badge_router.get("/{unmatched:path}", include_in_schema=False)
async def get_unmatched_badge() -> Response:
    """
    Anything else under /db/stats/badge/, drawn as a badge rather than a 404.

    Without this, the ruling in _error_badge only held for requests that got
    as far as a handler. A request the router rejects first never reaches one,
    and Starlette answers it with `{"detail": "Not Found"}` -- JSON, which
    camo cannot draw, so the reader gets the broken-image icon that the error
    badge exists to prevent. The layer the rejection happens at is invisible
    to the person looking at the badge, so it cannot be what decides whether
    they are told anything.

    More shapes land here than the exotic one that found it. The metric
    segment holding an encoded slash (%2F, or a %3Cscript%3E payload
    containing one) decodes to extra path segments and stops matching, but so
    does dropping the extension (/db/stats/badge/books), getting it wrong
    (books.png), and adding a segment (/us/books.svg) -- and those last three
    are what a person copying one of these URLs by hand actually does.

    Scoped to this prefix, which serves nothing but images, so it cannot mask
    a routing mistake anywhere else in the API; it is registered after the
    real route, which therefore always wins; and it is kept out of the OpenAPI
    schema, since /db/stats/badge/<anything> is not an endpoint anyone should
    be told to call. It takes no argument at all: the path text is captured by
    the converter and never bound into Python, so nothing arbitrary is read,
    rendered or held -- the response is a constant rendered once at import.
    """
    return _svg_response(_UNMATCHED_BADGE, "no-store")
