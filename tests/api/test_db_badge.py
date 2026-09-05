"""
/db/stats/badge SVG tests.
Tests badge geometry, colour, error rendering, region scoping and headers.
get_db_stats is mocked at the badge module's import location — the badge
route reads the same accessor the JSON route does, so the two mock at
separate paths and a test that patched the JSON one would not intercept
these calls at all.
"""

# Standard library
import inspect
import re
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.main import app
from app.api.routes.db.badge import (
    _MEASURED_TEXT_WIDTHS,
    _METRICS,
    _render_badge,
    get_unmatched_badge,
)
from app.api.routes.db.stats_headers import (
    STATS_STALE_IF_ERROR_SECONDS,
    STATS_STALE_WHILE_REVALIDATE_SECONDS,
)
from app.services.db.reader import _STAT_KEYS, DbStatsResult


BADGE_STATS_PATH = "app.api.routes.db.badge.get_db_stats"
JSON_STATS_PATH = "app.api.routes.db.router.get_db_stats"

MOCK_STATS = {
    "books": 150, "authors": 42, "narrators": 85, "series": 18, "booksWithChapters": 7,
}

# What each metric's badge is called and painted, carried over from the
# shields badges the README serves its counters from today. The two colours
# that matter are the ones whose shields URL name does not mean what it looks
# like: shields paints `orange` as #ea7233 after a contrast adjustment and
# `blue` as #007ec6, so neither is the CSS colour of that name and neither is
# the #fe7d37 shields' own docs give for orange. Pinned as hex because the
# plausible "correction" — reading the colour word out of those shields URLs
# and substituting the CSS value — restyles every badge this route draws and
# looks like a tidy-up in the diff.
METRIC_PRESENTATION = [
    ("books", "Books", "#ea7233"),
    ("booksWithChapters", "Books with Chapters", "#b5179e"),
    ("authors", "Authors", "#008080"),
    ("narrators", "Narrators", "#007ec6"),
    ("series", "Series", "#800080"),
]

# Every fixed string this route draws, against the width shields measured for
# it: `curl img.shields.io/badge/x-<text>-blue`, textLength off the last
# <text>, divided by 10. Held here as a second copy on purpose -- the module's
# own table is the thing under test, and a table that only agrees with itself
# proves nothing. A wrong entry is invisible in every other test in this file,
# because it moves the box and the text together and never raises.
SHIELDS_TEXT_WIDTHS = {
    "Books": 33,
    "Books with Chapters": 113,
    "Authors": 43,
    "Narrators": 53,
    "Series": 35,
    "unknown metric": 89,
    "invalid region": 75,
    "no such badge": 81,
}

# The greys: shields' own colour for a badge it could not resolve, and the
# label plate every two-part badge carries.
ERROR_COLOR = "#939393"
LABEL_PLATE_FILL = "#555"


def _stats_result(stats=None, seconds=300):
    """A DbStatsResult with an expiry `seconds` in the future, matching the
    helper of the same name in test_db.py. `seconds=None` produces the
    no-store case (cache_expires_at=None): the DB-failure fallback, or a
    cache-write failure after an otherwise successful query."""
    if seconds is None:
        return DbStatsResult(stats if stats is not None else MOCK_STATS, None)
    return DbStatsResult(
        stats if stats is not None else MOCK_STATS,
        datetime.now(timezone.utc) + timedelta(seconds=seconds),
    )


def _cache_directives(response):
    """The response's Cache-Control parsed into a {directive: value} map, a
    bare directive mapping to None. Order-independent for the same reason
    the copy in test_db.py is: the stale windows are only meaningful as
    values, and a reordered directive list must not change what an
    assertion about them measures."""
    directives = {}
    for part in response.headers["Cache-Control"].split(","):
        name, _, value = part.strip().partition("=")
        directives[name] = value or None
    return directives


def _drawn_strings(svg):
    """Every string the badge actually paints, in document order. Read out
    of the <text> elements rather than by substring-searching the whole
    document, so an assertion cannot be satisfied by a colour, an id or an
    aria attribute that happens to contain the same characters."""
    return re.findall(r"<text [^>]*>([^<]*)</text>", svg)


def _rect_fills(svg):
    """The fill of each <rect>, in document order: label plate, message
    plate, then the gradient overlay."""
    return re.findall(r'<rect [^>]*fill="([^"]+)"', svg)


# ============================================================
# GET /db/stats/badge/{metric}.svg — rendering
# ============================================================


def test_every_badge_metric_is_a_key_db_stats_actually_returns():
    """
    The badge table names its metrics in the reader's vocabulary and nothing
    else binds the two. Rename a stat key, or move one into the region-only
    set, and every badge for it renders "unknown metric" -- grey, 200, and
    with this whole file still green, because every test here supplies the
    stats payload itself and would go on supplying the old name.

    A subset, not equality: a stat can exist without a badge, which is the
    seriesRegionUnknown ruling. What may not happen is a badge naming a key
    an unscoped /db/stats does not return.
    """
    assert set(_METRICS) <= _STAT_KEYS


def test_the_measured_width_table_holds_what_shields_measured():
    """
    Seven of these eight strings are drawn by no test that would notice the
    number being wrong: a bad width sizes the box and stretches the text to
    match it, so the badge renders happily at the wrong size and the only
    symptom is a plate that looks slightly off next to a real shields badge.
    Pinned against the measurements themselves, which is the one thing that
    cannot be derived from the module.
    """
    assert _MEASURED_TEXT_WIDTHS == SHIELDS_TEXT_WIDTHS


@pytest.mark.asyncio
@pytest.mark.parametrize("metric,label,color", METRIC_PRESENTATION)
async def test_badge_draws_each_metrics_own_label_and_colour(async_client, metric, label, color):
    """Every metric the README shows renders its own name, its own count and
    its own colour. Parametrised over all five rather than spot-checked on
    one, because the failure mode being guarded is a single wrong entry in
    the metric table — one badge quietly drawing another's colour, on a page
    where five sit side by side."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get(f"/db/stats/badge/{metric}.svg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    svg = response.text
    assert label in _drawn_strings(svg)
    assert str(MOCK_STATS[metric]) in _drawn_strings(svg)
    # The message plate — the second rect — is the one carrying the metric's
    # colour; the first is the grey label plate.
    assert _rect_fills(svg)[1] == color


@pytest.mark.asyncio
@pytest.mark.parametrize("metric,label,color", METRIC_PRESENTATION)
async def test_badge_colours_are_what_shields_painted_not_the_css_keywords(async_client, metric, label, color):
    """The colour names in the shields URLs these counts are served from
    today are not the colours shields drew. `orange` came back as #ea7233
    and `blue` as #007ec6, and neither the CSS keyword nor the hex shields'
    documentation gives for orange (#fe7d37) reproduces what a reader sees.
    This asserts the drawn value and the absence of every plausible
    substitute, so a "correction" back to the documented name fails here
    instead of silently restyling every badge this route draws."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get(f"/db/stats/badge/{metric}.svg")

    svg = response.text
    assert f'fill="{color}"' in svg
    for wrong in ("orange", "#ffa500", "#fe7d37", "blue", "#0000ff", "#007bff"):
        assert f'fill="{wrong}"' not in svg


@pytest.mark.asyncio
async def test_badge_geometry_matches_the_shields_flat_reproduction(async_client):
    """The measured half of the reproduction: a `Books: 150` badge is 74
    units wide, made of a 43-unit label plate and a 31-unit message plate,
    with each string stretched to exactly the width its box was sized for.
    These numbers came out of a live shields SVG for the same badge, so a
    change to the width table or the padding constants that shifts them
    shows as a box that no longer fits its text — caught here rather than in
    a rendered page somebody squints at weeks later."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats/badge/books.svg")

    svg = response.text
    assert '<svg xmlns="http://www.w3.org/2000/svg" width="74" height="20"' in svg
    assert '<rect width="43" height="20" fill="#555"/>' in svg
    assert '<rect x="43" width="31" height="20" fill="#ea7233"/>' in svg
    # textLength is ten times the box's text width: the template draws at a
    # .1 scale, so dropping it lets a renderer with a different Verdana lay
    # the text out at its own width inside a box sized for another. The x is
    # asserted with it because the two are independent -- a box of the right
    # size with its text a pixel off centre passes every width assertion.
    assert '<text x="225" y="140" textLength="330">Books</text>' in svg
    assert '<text x="575" y="140" textLength="210">150</text>' in svg


@pytest.mark.asyncio
async def test_bare_badge_geometry_matches_the_shields_flat_reproduction(async_client):
    """
    The unlabelled badge positions its text by a formula of its own -- there
    is no label plate for the message to sit after -- and being a second
    formula it can drift from the first while every plate is still the right
    size. Both numbers came out of live shields SVGs of the same two badges:
    `-150-` is 31 units wide with its text centred at 155, and a seven-digit
    count is 59 wide centred at 295.

    Two counts rather than one, because a constant offset and a wrong
    multiplier both reproduce any single badge, and the digits are the part
    of this route that varies every time it is called.
    """
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        narrow = await async_client.get("/db/stats/badge/books.svg?label=false")
        mock_stats.return_value = _stats_result({**MOCK_STATS, "books": 1234567})
        wide = await async_client.get("/db/stats/badge/books.svg?label=false")

    assert '<svg xmlns="http://www.w3.org/2000/svg" width="31" height="20"' in narrow.text
    assert '<text x="155" y="140" textLength="210">150</text>' in narrow.text
    assert '<svg xmlns="http://www.w3.org/2000/svg" width="59" height="20"' in wide.text
    assert '<text x="295" y="140" textLength="490">1234567</text>' in wide.text


@pytest.mark.asyncio
async def test_badge_width_follows_the_digits_it_draws(async_client):
    """A count is measured, not fixed: every digit is one advance width, so
    a seven-digit figure makes a wider plate than a three-digit one. Pinned
    at both ends because a width table that stopped varying with the number
    would render every library count in a box sized for whatever was
    measured last."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result({**MOCK_STATS, "books": 1234567})
        wide = await async_client.get("/db/stats/badge/books.svg?label=false")
        mock_stats.return_value = _stats_result({**MOCK_STATS, "books": 150})
        narrow = await async_client.get("/db/stats/badge/books.svg?label=false")

    wide_width = int(re.search(r'<svg [^>]*width="(\d+)"', wide.text).group(1))
    narrow_width = int(re.search(r'<svg [^>]*width="(\d+)"', narrow.text).group(1))
    assert wide_width == 59
    assert narrow_width == 31
    assert wide_width > narrow_width


@pytest.mark.asyncio
async def test_badge_draws_the_count_in_bare_digits(async_client):
    """Drawn exactly as /db/stats gives it, which is also how shields drew
    it. A thousands separator here would make the badge and the JSON it
    links to disagree about the same figure, and the README puts them next
    to each other."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result({**MOCK_STATS, "books": 1234567})
        response = await async_client.get("/db/stats/badge/books.svg")

    assert "1234567" in _drawn_strings(response.text)
    assert "1,234,567" not in response.text


@pytest.mark.asyncio
async def test_badge_label_false_draws_a_bare_number_in_the_metrics_colour(async_client):
    """The bare form, for a table whose row and column already say what the
    number counts: no label, and the zero-width label plate painted in the
    metric's colour rather than the usual grey. The grey is what a renderer
    rounding the clip path differently would show as a sliver down the left
    edge of every cell in such a table, which is why the fill is asserted
    and not just the width."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats/badge/books.svg?label=false")

    svg = response.text
    assert response.status_code == 200
    assert _drawn_strings(svg) == ["150", "150", "150"]
    assert "Books" not in svg
    assert '<rect width="0" height="20" fill="#ea7233"/>' in svg
    assert LABEL_PLATE_FILL not in svg


@pytest.mark.asyncio
async def test_badge_label_defaults_to_drawn(async_client):
    """Omitting the param is the labelled badge, so the five global badges
    keep their names with no query string at all."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        default = await async_client.get("/db/stats/badge/books.svg")
        explicit = await async_client.get("/db/stats/badge/books.svg?label=true")

    assert default.text == explicit.text
    assert "Books" in _drawn_strings(default.text)


@pytest.mark.asyncio
async def test_badge_carries_its_text_in_the_accessible_attributes(async_client):
    """What a screen reader and a curl get. The image is the whole content
    of this response, and a count embedded as an image has no text
    alternative anywhere else on the page it sits in, so these two
    attributes are the only place the figure exists as words."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        labelled = await async_client.get("/db/stats/badge/books.svg")
        bare = await async_client.get("/db/stats/badge/books.svg?label=false")

    assert 'role="img" aria-label="Books: 150"' in labelled.text
    assert "<title>Books: 150</title>" in labelled.text
    assert 'aria-label="150"' in bare.text
    assert "<title>150</title>" in bare.text


# ============================================================
# THE ESCAPING GUARD
# ============================================================
# Every string this module draws is Libex's own -- the metric table's fixed
# labels and a decimal count -- and test_caller_text_never_reaches_the_svg
# holds that closed. So the escaping is not defending against today's inputs
# and no test that goes through the route can reach it: the drawn set
# contains nothing that needs escaping, and deleting html.escape entirely
# leaves this file green.
#
# It is kept for the change that has not happened yet, which is exactly what
# the module says it is for -- "the day someone adds a metric whose label
# contains an ampersand is not the day to discover this was the only thing
# standing between a label and a malformed document." A guard against a
# future input can only be exercised by supplying that input directly, so
# these two call the renderer rather than the route. That is the point of
# them rather than a shortcut around one.


_UNSAFE_LABEL = 'R&D "core" <b>'
_UNSAFE_MESSAGE = "1<2"


def test_every_drawn_string_is_escaped_before_it_reaches_the_document():
    """
    A label and a message carrying all three characters that break an SVG,
    asserted as the entities they become and as the document staying
    parseable.

    Well-formedness is the assertion that matches what the guard is for. A
    bare ampersand or an unclosed tag inside a <text> element does not
    render the badge wrongly, it stops the badge being a document at all --
    and a browser given a broken SVG shows the alt text or nothing, so the
    first symptom is a README with a hole in it rather than anything a log
    would carry.
    """
    svg = _render_badge(_UNSAFE_LABEL, _UNSAFE_MESSAGE, "#4c1")

    ElementTree.fromstring(svg)
    assert _drawn_strings(svg) == [
        "R&amp;D &quot;core&quot; &lt;b&gt;",
        "R&amp;D &quot;core&quot; &lt;b&gt;",
        "R&amp;D &quot;core&quot; &lt;b&gt;",
        "1&lt;2",
        "1&lt;2",
        "1&lt;2",
    ]
    assert "R&D" not in svg
    assert "<b>" not in svg


def test_the_accessible_attributes_escape_the_quote_character_too():
    """
    aria-label is an attribute and <title> is not, and both are built from
    the same string -- so the escape that serves them has to be the
    attribute-safe one. Dropping quote=True leaves the element content
    correct and ends the aria-label early at the first double quote,
    spilling the rest of the label into the tag as attributes: a difference
    no assertion on the <title> would ever show, and the reason this is a
    test of its own rather than another line in the one above.

    The default is already quote=True, which is the trap. Anyone tidying
    `html.escape(aria, quote=True)` down to `html.escape(aria)` changes
    nothing at all and would be right; anyone removing the call is what this
    catches.
    """
    svg = _render_badge(_UNSAFE_LABEL, "1", "#4c1")

    assert 'aria-label="R&amp;D &quot;core&quot; &lt;b&gt;: 1"' in svg
    assert "<title>R&amp;D &quot;core&quot; &lt;b&gt;: 1</title>" in svg
    assert ElementTree.fromstring(svg).get("aria-label") == 'R&D "core" <b>: 1'


# ============================================================
# GET /db/stats/badge/{metric}.svg — error badges
# ============================================================


@pytest.mark.asyncio
async def test_unknown_metric_returns_an_error_badge_with_http_200(async_client):
    """The deliberate exception this one surface makes, and the one most
    likely to be "fixed" by someone reading it as a bug. What a caller
    receives here is an image inside an <img> tag: GitHub's camo proxy does
    not draw the body of a non-200, so an honest 404 renders as the
    browser's broken-image icon and tells the reader nothing. A grey box
    reading "unknown metric" tells them exactly what happened. The status
    is the assertion — flipping it to 404 puts the broken-image icon back in
    front of the reader while looking like a correctness fix."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats/badge/nope.svg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert "unknown metric" in _drawn_strings(response.text)
    assert _rect_fills(response.text)[1] == ERROR_COLOR


@pytest.mark.asyncio
async def test_error_badge_is_never_cached(async_client):
    """no-store, so a typo is not held for a day by a CDN that would
    happily do so, and so the mistake stays visible until it is fixed."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats/badge/nope.svg")

    assert response.headers["Cache-Control"] == "no-store"
    assert set(_cache_directives(response)) == {"no-store"}


@pytest.mark.asyncio
async def test_unknown_metric_never_reaches_the_database(async_client):
    """An unrecognised metric is rejected before the stats read, so a
    scripted sweep of made-up metric names cannot make this endpoint run a
    count per request."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        await async_client.get("/db/stats/badge/nope.svg")

    mock_stats.assert_not_called()


@pytest.mark.asyncio
async def test_series_region_unknown_is_a_real_stat_with_deliberately_no_badge(async_client):
    """seriesRegionUnknown is a genuine /db/stats field and still gets the
    unknown-metric badge, which is the point rather than an oversight: it
    is the size of a gap in the data and needs the JSON route's sentence to
    mean anything, where a number alone in a coloured box reads as a sixth
    library count. Pinned because the obvious "completeness" change is to
    add it to the metric table."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result(
            {**MOCK_STATS, "seriesRegionUnknown": 3}
        )
        response = await async_client.get("/db/stats/badge/seriesRegionUnknown.svg?region=us")

    assert response.status_code == 200
    assert "unknown metric" in _drawn_strings(response.text)
    assert "3" not in _drawn_strings(response.text)


@pytest.mark.asyncio
async def test_invalid_region_returns_an_error_badge_not_a_400(async_client):
    """Same reasoning as the unknown metric, on the other input a reader
    types by hand. The JSON route answers a bad region with a 400; this one
    cannot, because a 400 body is not drawn either."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats/badge/books.svg?region=zz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.headers["Cache-Control"] == "no-store"
    assert "invalid region" in _drawn_strings(response.text)
    mock_stats.assert_not_called()


@pytest.mark.asyncio
async def test_empty_region_is_an_invalid_region_not_a_global_badge(async_client):
    """`?region=` with nothing after it is a caller who meant to name a
    region and did not. Treating the empty string as "unscoped" would draw
    the global count under a region's heading, which is worse than saying
    the region was unreadable."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats/badge/books.svg?region=")

    assert response.status_code == 200
    assert "invalid region" in _drawn_strings(response.text)
    mock_stats.assert_not_called()


@pytest.mark.asyncio
async def test_metric_missing_from_the_stats_payload_draws_the_unknown_badge(async_client):
    """The third route to an error badge: the metric is known here but the
    stats response has no such key -- an entry written before the field
    existed, or a scoped response missing a scoped-only key. Drawing
    nothing, or a blank box, would be indistinguishable from a count of
    zero on a public page."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result(
            {"authors": 42, "narrators": 85, "series": 18, "booksWithChapters": 7}
        )
        response = await async_client.get("/db/stats/badge/books.svg")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "unknown metric" in _drawn_strings(response.text)


@pytest.mark.asyncio
async def test_caller_text_never_reaches_the_svg(async_client):
    """Nothing drawn is caller-supplied, and this is what keeps it that
    way. An endpoint on libexdb.com that echoed its path segment into an
    SVG would be a way to host arbitrary text -- and arbitrary markup -- on
    Libex's own origin. The metric is looked up in a fixed table and the
    unrecognised value is discarded, never rendered."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get(
            "/db/stats/badge/%3Cimg%20src=x%20onerror=alert(1)%3E.svg"
        )

    assert response.status_code == 200
    body = response.text
    assert "onerror" not in body
    assert "alert" not in body
    assert "<img" not in body
    assert _drawn_strings(body) == ["unknown metric"] * 3


@pytest.mark.asyncio
async def test_malformed_label_is_the_documented_json_exception(async_client):
    """The one input on this route that does not get a badge. metric and
    region are typed into a URL from the documentation and get a readable
    image back when they are wrong; a boolean FastAPI cannot parse means a
    caller building the URL programmatically, who is better served by the
    error the rest of the API gives. Asserted as JSON, not merely as a
    422, because the asymmetry is the thing worth knowing -- a later change
    making every bad input on this route render a badge would take this
    caller's error message away."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats/badge/books.svg?label=maybe")

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    assert "detail" in response.json()
    assert "<svg" not in response.text


# ============================================================
# GET /db/stats/badge/{metric}.svg — region scoping
# ============================================================


@pytest.mark.asyncio
async def test_badge_forwards_no_region_as_none(async_client):
    """The five global badges must read the unscoped entry -- passing any
    region here would put a region's counts under the library-wide
    headline."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        await async_client.get("/db/stats/badge/books.svg")

    assert mock_stats.await_args[0][1] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"])
async def test_badge_forwards_every_region_to_the_reader(async_client, region):
    """All eleven, not just us. A badge that hardcoded or dropped the region
    passes a US-only test and draws the same number in every row of a region
    table, which reads as eleven regions agreeing rather than as a bug."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get(f"/db/stats/badge/books.svg?region={region}")

    assert response.status_code == 200
    assert mock_stats.await_args[0][1] == region


@pytest.mark.asyncio
async def test_badge_normalizes_the_region_before_forwarding_it(async_client):
    """Case and surrounding space are normalised by validate_region, the
    same as on the JSON route. An unnormalised value would compose a cache
    key nothing else ever writes to, so every `?region=US` badge would miss
    the warm entry and pay the cold recompute this whole change exists to
    remove."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        await async_client.get("/db/stats/badge/books.svg?region=%20US%20")

    assert mock_stats.await_args[0][1] == "us"


@pytest.mark.asyncio
async def test_narrators_badge_still_forwards_the_region_it_was_given(async_client):
    """narrators has no region column, so the count cannot be scoped -- but
    the scoping happens in get_db_stats, not here. This route forwards the
    region like any other metric and draws whatever comes back, which is
    what keeps a `?region=de` narrators badge reading the de cache entry
    rather than a second, unscoped one. Swallowing the region here would
    look like a tidy shortcut and would split the region's four counts
    across two cache entries."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result(
            {**MOCK_STATS, "seriesRegionUnknown": 3}
        )
        response = await async_client.get("/db/stats/badge/narrators.svg?region=de")

    assert response.status_code == 200
    assert mock_stats.await_args[0][1] == "de"
    assert "85" in _drawn_strings(response.text)


@pytest.mark.asyncio
async def test_narrators_badge_shows_the_same_figure_scoped_or_not(async_client):
    """The documented consequence of that: a narrator is not owned by any
    marketplace, so the scoped call returns the global narrator count and
    the badge draws it. Two badges reading the same number is correct here
    and would look like a bug to anyone who did not know -- pinned so the
    behaviour is stated somewhere a reader will find it."""
    scoped_stats = {**MOCK_STATS, "books": 9, "seriesRegionUnknown": 3}
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        unscoped = await async_client.get("/db/stats/badge/narrators.svg?label=false")
        mock_stats.return_value = _stats_result(scoped_stats)
        scoped = await async_client.get("/db/stats/badge/narrators.svg?region=de&label=false")

    assert _drawn_strings(unscoped.text) == _drawn_strings(scoped.text) == ["85"] * 3


# ============================================================
# GET /db/stats/badge/{metric}.svg — headers
# ============================================================


@pytest.mark.asyncio
async def test_badge_cache_control_tracks_the_entrys_remaining_life(async_client):
    """The badge is a second representation of the same cache entry, so it
    advertises the same freshness the JSON does: the real life left on the
    entry, not a flat re-quote of the TTL. An entry with ~42 seconds left
    must produce a max-age in that neighbourhood, and the stale windows
    ride along -- stale-while-revalidate is what keeps a lapsed copy
    rendering a number at warm speed instead of an error."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result(seconds=42)
        response = await async_client.get("/db/stats/badge/books.svg")

    directives = _cache_directives(response)
    assert 40 <= int(directives["max-age"]) <= 42
    assert directives["max-age"] == directives["s-maxage"]
    assert int(directives["max-age"]) != 300
    assert directives["stale-while-revalidate"] == str(STATS_STALE_WHILE_REVALIDATE_SECONDS)
    assert directives["stale-if-error"] == str(STATS_STALE_IF_ERROR_SECONDS)


@pytest.mark.asyncio
async def test_badge_and_json_advertise_the_same_freshness_for_the_same_entry(async_client):
    """One policy, two representations. A reader comparing a badge against
    the JSON it links to is looking at one cache entry, and
    the two must not tell them different things about how old it may be --
    which is the drift a second, local copy of this header logic would
    introduce silently, since neither route's own tests would notice."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=120)
    result = DbStatsResult(MOCK_STATS, expires_at)

    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_badge, \
         patch(JSON_STATS_PATH, new_callable=AsyncMock) as mock_json:
        mock_badge.return_value = result
        mock_json.return_value = result
        badge = await async_client.get("/db/stats/badge/books.svg")
        json_response = await async_client.get("/db/stats")

    badge_directives = _cache_directives(badge)
    json_directives = _cache_directives(json_response)
    assert set(badge_directives) == set(json_directives)
    assert badge_directives["stale-while-revalidate"] == json_directives["stale-while-revalidate"]
    assert badge_directives["stale-if-error"] == json_directives["stale-if-error"]
    # Read a moment apart, so the two may differ by the second that passed
    # between them and by no more than that.
    assert abs(int(badge_directives["max-age"]) - int(json_directives["max-age"])) <= 1


@pytest.mark.asyncio
async def test_badge_from_the_db_failure_fallback_is_never_cached(async_client):
    """cache_expires_at=None means nothing trustworthy was stored -- the
    all-zeros fallback, or a cache write that failed. no-store, with no
    stale window: a real count is a good answer to serve past its expiry
    and all-zeros is not, and stale-if-error here would pin those zeros in
    front of every reader for a day with no way for origin to correct
    it."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = DbStatsResult(
            {"books": 0, "authors": 0, "narrators": 0, "series": 0, "booksWithChapters": 0},
            None,
        )
        response = await async_client.get("/db/stats/badge/books.svg")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert set(_cache_directives(response)) == {"no-store"}


@pytest.mark.asyncio
async def test_badge_draws_a_fallback_zero_as_a_zero(async_client):
    """The badge is given no way to tell a real zero from a fallback zero,
    because the JSON has none either, and two representations of one figure
    disagreeing would be worse than either answer alone. What marks the
    case is the header above, not the picture."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = DbStatsResult(
            {"books": 0, "authors": 0, "narrators": 0, "series": 0, "booksWithChapters": 0},
            None,
        )
        response = await async_client.get("/db/stats/badge/books.svg")

    assert "0" in _drawn_strings(response.text)
    assert "unknown metric" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "/db/stats/badge/books.svg",
    "/db/stats/badge/nope.svg",
    "/db/stats/badge/books.svg?region=zz",
    "/db/stats/badge/books",
    "/db/stats/badge/us/books.svg",
])
async def test_every_badge_response_carries_the_svg_containment_headers(async_client, url):
    """An SVG is a document, not just a picture: fetched from libexdb.com
    and opened directly rather than through an <img>, it would run script in
    Libex's own origin if it ever contained any. nosniff and a CSP that
    permits nothing hold on every path out of this route, including the two
    error badges and the unmatched-URL fallback -- the paths most likely to
    be added to later without the wrapper, and the fallback is the one an
    unencoded payload actually arrives on."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get(url)

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response.headers["content-type"] == "image/svg+xml"


# ============================================================
# GET /db/stats/badge/<anything else> — the unmatched fallback
# ============================================================
# The error-badge ruling only held for requests that reached a handler. A
# request the router rejects first never gets there, and Starlette answers it
# with {"detail": "Not Found"} -- JSON, which camo cannot draw, so the reader
# sees the broken-image icon the badge exists to prevent. Which layer said no
# is invisible to the person looking at the badge, so it cannot decide whether
# they are told anything.

# The URL shapes measured to miss the real route. The middle three are the
# ones that matter: they are what somebody copying one of these URLs by hand
# actually types, and the case for documenting the gap rather than closing it
# rested on nobody arriving here by accident.
UNMATCHED_SHAPES = [
    "/db/stats/badge/books",                                   # dropped extension
    "/db/stats/badge/books.png",                               # wrong extension
    "/db/stats/badge/us/books.svg",                            # extra segment
    "/db/stats/badge/",                                        # prefix alone
    "/db/stats/badge/.svg",                                    # no metric at all
    "/db/stats/badge/a%2Fb.svg",                               # encoded slash
    "/db/stats/badge/%3Cscript%3ECANARY%3C%2Fscript%3E.svg",   # payload with one in it
]

# The three a person copying one of these URLs by hand produces, called out
# separately because they are the reason this route exists at all.
FORK_MISTAKES = UNMATCHED_SHAPES[:3]

NO_SUCH_BADGE = "no such badge"
UNKNOWN_METRIC = "unknown metric"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", FORK_MISTAKES)
async def test_a_fork_mistake_renders_a_badge_instead_of_a_json_404(async_client, url):
    """
    Dropping the extension, getting it wrong, and leaving a region in the
    path are what people actually do to a URL they copied by hand. Each
    would otherwise be answered with JSON the reader's browser draws as a
    broken image; each is a grey badge that says what went wrong -- 200, because a
    non-200 body is not drawn by camo either, which is the same reason the
    unknown-metric badge is a 200.
    """
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert NO_SUCH_BADGE in _drawn_strings(response.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", UNMATCHED_SHAPES)
async def test_every_measured_unmatched_shape_renders_the_same_badge(async_client, url):
    """All seven shapes measured to miss the real route, held as one set. A
    fallback that covered the obvious ones and not the exotic ones would
    leave the reader's outcome depending on which layer of the stack said
    no -- and that layer is invisible to the person looking at the
    badge."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert _drawn_strings(response.text) == [NO_SUCH_BADGE] * 3


@pytest.mark.asyncio
async def test_the_prefix_without_its_trailing_slash_still_ends_at_a_badge(async_client):
    """`/db/stats/badge` redirects to the slashed form, which renders the
    fallback. Pinned as the reader-visible outcome rather than as a
    redirect, because camo follows redirects -- so what reaches the <img>
    is the badge at the end of it, not the 307."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats/badge", follow_redirects=True)

    assert response.status_code == 200
    assert NO_SUCH_BADGE in _drawn_strings(response.text)


@pytest.mark.asyncio
async def test_the_unmatched_badge_never_echoes_the_path_it_was_given(async_client):
    """
    The property that keeps this route from being a new input surface. The
    handler takes no argument at all -- the path text is consumed by the
    converter and never bound into Python -- so a request naming anything
    at all gets one constant image back. The plausible future change is to
    make the message more helpful by naming what was asked for, which would
    put caller-controlled text into a document served from libexdb.com's
    own origin, on the one route a scanner is most likely to probe.
    """
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get(
            "/db/stats/badge/CANARY%3Cscript%3Ealert(1)%3C%2Fscript%3E.svg"
        )

    body = response.text
    assert "CANARY" not in body
    assert "script" not in body
    assert "alert" not in body
    assert _drawn_strings(body) == [NO_SUCH_BADGE] * 3


def test_the_unmatched_handler_binds_no_path_text_at_all():
    """
    The same guarantee at the signature, where the change would actually be
    made. The canary above proves today's response holds no caller text;
    this proves there is nothing for a later edit to reach for -- adding
    `unmatched: str` to make the message specific is a one-word change that
    reads as an improvement and quietly makes the body caller-controlled.
    """
    assert list(inspect.signature(get_unmatched_badge).parameters) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "/db/stats/nope",
    "/db/bogus",
    "/db/stats/badges/books.svg",
])
async def test_the_fallback_does_not_swallow_routing_errors_elsewhere(async_client, url):
    """
    Scoped to a prefix that serves nothing but images. A catch-all mounted
    any wider turns every mistyped API path into a 200 with a picture --
    which is the right answer for an <img> and the wrong one for every JSON
    caller Libex has, who would get a success status and an SVG body where
    they expected an error they could act on.
    """
    response = await async_client.get(url)

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert "<svg" not in response.text


@pytest.mark.asyncio
async def test_a_real_badge_url_is_never_shadowed_by_the_fallback(async_client):
    """
    Registration order, held from the outside. The fallback matches
    everything under the prefix including the valid URLs, so it wins any
    request registered before the real route -- and the failure is total
    and silent: every badge this route serves turns grey while every test
    that mocks get_db_stats and asserts on a 200 SVG keeps passing. The
    assertion is that the real reader was called and its own header came
    back, not merely that something SVG-shaped arrived.
    """
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats/badge/books.svg")

    assert response.status_code == 200
    assert _drawn_strings(response.text) == ["Books", "Books", "Books", "150", "150", "150"]
    assert NO_SUCH_BADGE not in response.text
    mock_stats.assert_awaited_once()
    assert response.headers["Cache-Control"].startswith("public,")


@pytest.mark.asyncio
async def test_the_two_error_strings_point_at_different_fixes(async_client):
    """
    They sound alike and are not. "unknown metric" means the URL was the
    right shape and named a count Libex does not have -- check the name.
    "no such badge" means the router never recognised the request as a
    badge at all -- check the URL. Collapsing them into one string is the
    obvious tidy-up and it sends every reader to look in the wrong place
    half the time, which is worse than the 404 this replaced.
    """
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        wrong_metric = await async_client.get("/db/stats/badge/nope.svg")
        wrong_url = await async_client.get("/db/stats/badge/books")

    assert UNKNOWN_METRIC in _drawn_strings(wrong_metric.text)
    assert NO_SUCH_BADGE not in wrong_metric.text
    assert NO_SUCH_BADGE in _drawn_strings(wrong_url.text)
    assert UNKNOWN_METRIC not in wrong_url.text


@pytest.mark.asyncio
async def test_the_unmatched_badge_is_never_cached(async_client):
    """no-store, the same as the other error badges: a mistyped URL must
    not be held for a day by a CDN happy to do so, and the mistake stays
    visible until it is fixed. It is also the one thing stopping a scan of
    made-up paths from filling an edge cache with grey images."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats/badge/books.png")

    assert response.headers["Cache-Control"] == "no-store"
    assert set(_cache_directives(response)) == {"no-store"}


@pytest.mark.asyncio
async def test_the_unmatched_badge_never_reaches_the_database(async_client):
    """Nothing is looked up to answer it -- the image is rendered once at
    import and handed back. A scanner walking made-up paths under this
    prefix cannot make the endpoint run a count per request."""
    with patch(BADGE_STATS_PATH, new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        for url in UNMATCHED_SHAPES:
            await async_client.get(url)

    mock_stats.assert_not_called()


def test_the_fallback_is_kept_out_of_the_published_schema():
    """/db/stats/badge/<anything> is not an endpoint anyone should be told
    to call -- it is what happens when a URL is wrong. Publishing it would
    document a path that always returns the same error image as though it
    were a feature, next to the real route it exists to apologise for."""
    paths = app.openapi()["paths"]

    assert "/db/stats/badge/{metric}.svg" in paths
    assert not any("unmatched" in path for path in paths)
