"""Tests for request-logging middleware.

Libex records nothing that identifies a caller. These tests are the guard on
that: they assert the client address never reaches the log record under any
header, and that caller-authored query values are replaced while the
operational fields a deploy is monitored by survive intact.
"""

# Standard library
import logging
import re
import urllib.parse
from pathlib import Path

# Third party
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Local
from app.main import _FAVICON, app as libex_app

# Core
from app.core.middleware import (
    _redact_query,
    _KNOWN_QUERY_PARAMS,
    _SAFE_QUERY_PARAMS,
    LoggingMiddleware,
)


# ============================================================
# QUERY REDACTION
# ============================================================

# Every param whose value a caller authors. Keys survive; values must not.
CALLER_AUTHORED = ["name", "keywords", "title", "author", "narrator", "query", "search", "publisher"]


@pytest.mark.parametrize("param", CALLER_AUTHORED)
def test_redact_query_replaces_caller_authored_values(param):
    result = _redact_query(f"{param}=Some+Private+Text")
    assert "Some" not in result
    assert "Private" not in result
    assert result == f"{param}=REDACTED"


@pytest.mark.parametrize("param,value", [
    ("region", "us"),
    ("cache", "false"),
    ("limit", "10"),
    ("page", "2"),
    ("sort", "title"),
    ("order", "desc"),
    ("language", "english"),
])
def test_redact_query_keeps_structural_values(param, value):
    assert _redact_query(f"{param}={value}") == f"{param}={value}"


def test_redact_query_mixes_kept_and_redacted_in_one_string():
    result = _redact_query("region=us&name=Some+Person&page=2")
    parsed = dict(urllib.parse.parse_qsl(result))
    assert parsed["region"] == "us"
    assert parsed["page"] == "2"
    assert parsed["name"] == "REDACTED"


def test_redact_query_drops_unknown_params_entirely():
    """
    An allowlist must fail closed: a param nobody has classified never reaches
    the log. Not its value, and not its key either -- a key matching no route
    param is not a param name, it is caller text that landed in key position,
    so keeping it would leak the thing the value redaction withholds.
    """
    result = _redact_query("some_future_param=whatever")
    assert result == "_unrecognised=1"
    assert "some_future_param" not in result
    assert "whatever" not in result


def test_redact_query_counts_every_unknown_param_in_one_marker():
    """The operator still learns unexpected params arrived, and how many."""
    assert _redact_query("a=1&b=2&c=3") == "_unrecognised=3"


@pytest.mark.parametrize("raw,expected", [
    # An unencoded "&" splits mid-value: what follows it arrives as a bare key.
    ("title=Salt&Pepper", "title=REDACTED&_unrecognised=1"),
    ("publisher=Simon%20&%20Schuster", "publisher=REDACTED&_unrecognised=1"),
    ("author=Crosby, Stills & Nash", "author=REDACTED&_unrecognised=1"),
])
def test_redact_query_drops_the_tail_of_a_value_split_by_an_unencoded_amp(raw, expected):
    """The half a typed value that lands in key position is text, not a param."""
    result = _redact_query(raw)
    assert result == expected
    for leaked in ("Pepper", "Schuster", "Nash", "Salt", "Simon", "Crosby", "Stills"):
        assert leaked not in result


def test_redact_query_drops_a_query_string_carrying_no_equals_sign():
    """No "=" anywhere makes the whole typed string one bare key."""
    assert _redact_query("The+Diary+Of+A+Nobody") == "_unrecognised=1"


def test_redact_query_marker_cannot_be_forged_by_a_caller():
    """A caller sending the marker is itself unrecognised, and counted as one."""
    assert _redact_query("_unrecognised=99") == "_unrecognised=1"


def test_redact_query_redacts_a_second_query_smuggled_inside_a_safe_value():
    """
    Python 3.10 stopped splitting on ";", so "region=us;name=Jane+Doe" is a
    single param to parse_qsl -- an allowlisted key whose value is a whole
    second query. The value guard is what stops the name riding in on it.
    """
    result = _redact_query("region=us;name=Jane+Doe")
    assert result == "region=REDACTED"
    assert "Jane" not in result


def test_redact_query_redacts_text_smuggled_after_a_semicolon_with_no_equals():
    """
    Isolates what ";" alone buys, which the case above does not.

    "region=us;name=Jane+Doe" is caught with or without ";" in the excluded
    set, because the smuggled query carries an "=" and "=" is excluded too --
    so that case cannot tell which character did the work. Drop the ";" from
    the value guard and it still passes. This one does not: with no "=" in it
    at all, ";" is the only thing standing between a typed name and the log
    line, and the whole value is logged verbatim without it.
    """
    result = _redact_query("region=us;Jane+Doe")
    assert result == "region=REDACTED"
    assert "Jane" not in result


def test_redact_query_redacts_a_second_query_smuggled_in_after_a_second_equals():
    """
    parse_qsl splits on the FIRST "=" only, so everything after the second one
    stays inside the value -- the same ride as ";" above, through the other
    character excluded by name rather than by category.
    """
    result = _redact_query("region=us=name=Jane+Doe")
    assert result == "region=REDACTED"
    assert "Jane" not in result


def test_redact_query_redacts_a_safe_param_whose_value_is_too_long():
    """The guard bounds length as well as shape: these params take short tokens."""
    assert _redact_query("limit=" + "a" * 64) == "limit=" + "a" * 64
    assert _redact_query("limit=" + "a" * 65) == "limit=REDACTED"


def test_redact_query_keeps_a_non_ascii_facet_value():
    """
    Facet names are catalogue data in any language, not caller identity.

    A shape case, not a measured one: `language` is the param it is easiest to
    imagine carrying an accent, but every marketplace answers it in lowercase
    English (english, german, japanese, portuguese, hindi), so no caller ever
    sends this. The allowlisted param that genuinely carries localized text is
    `genre` -- see the per-region cases below, which are the real evidence.
    """
    assert _redact_query("language=Français") == "language=Fran%C3%A7ais"


def test_redact_query_leaves_an_ordinary_request_untouched():
    """The whole point: a normal call still reads as itself, keys and values."""
    assert _redact_query("region=us&limit=10&sort=title") == "region=us&limit=10&sort=title"


def test_redact_query_keeps_a_known_key_while_dropping_an_unknown_one():
    assert _redact_query("name=Jane+Doe&region=uk") == "name=REDACTED&region=uk"


def test_known_params_match_the_live_route_surface_exactly():
    """
    The drop rule is only safe while the enumeration is complete.

    A param a route accepts but _KNOWN_QUERY_PARAMS omits is dropped from
    every log line silently -- fail-closed, so nothing leaks, but the
    operator loses the answer the query field exists to give. A name here
    that no route accepts is dead weight that reads as a supported param.
    Both directions are asserted, and the assertion is made against the app
    itself so a route change cannot drift past it.

    Collected by alias, not by name: the alias is what arrives on the wire and
    therefore what _redact_query matches, while the name is the Python
    identifier the route handler binds it to. No route sets an alias today, so
    the two sets are identical -- which is exactly why reading the wrong one
    stays invisible until the first alias is added, at which point the guard
    would pass while the live param is dropped from every log line.
    """
    live = set()

    def collect(dependant):
        for param in dependant.query_params:
            live.add(param.alias)
        for sub in dependant.dependencies:
            collect(sub)

    for route in libex_app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            collect(dependant)

    assert live, "walked the router and found no query params at all"
    assert live - _KNOWN_QUERY_PARAMS == set(), "route params missing from the allowlist are dropped from every log line"
    assert _KNOWN_QUERY_PARAMS - live == set(), "allowlisted names no route accepts"


def test_safe_params_are_all_known_params():
    """A value cannot be kept for a key that is never written."""
    assert _SAFE_QUERY_PARAMS <= _KNOWN_QUERY_PARAMS


def test_redact_query_sentinel_survives_urlencoding_readably():
    """A sentinel with <> comes back as %3C...%3E, unreadable in the logs."""
    assert "%3C" not in _redact_query("name=x")
    assert "REDACTED" in _redact_query("name=x")


@pytest.mark.parametrize("raw", ["", "%%%", "&&&", "=", "a" * 5000])
def test_redact_query_never_raises_on_hostile_input(raw):
    result = _redact_query(raw)
    assert isinstance(result, str)


def test_redact_query_leaks_nothing_on_malformed_input():
    """Falling back to the raw string would leak exactly what this withholds."""
    assert "Private" not in _redact_query("name=Private%ZZ&%%%")


def test_safe_params_excludes_every_caller_authored_field():
    assert _SAFE_QUERY_PARAMS.isdisjoint(CALLER_AUTHORED)


# ============================================================
# LOCALIZED FACET VALUES
# ============================================================

# `genre` is the one allowlisted param whose values are localized catalogue
# text, so it is where the value guard actually meets non-ASCII input. These
# pin, region by region, that a marketplace's real vocabulary survives the
# guard: it judges by Unicode category, and letters, marks, numbers,
# punctuation, symbols and spaces in any script are what a catalogue name is
# made of. So the katakana middle dot 402 jp names are built on is kept, as are
# the U+2019 apostrophe fr and it spell their names with, the "&" and ASCII
# apostrophe the us and uk taxonomies lean on, and the Devanagari vowel signs
# that are combining marks rather than letters.
#
# What the guard still refuses is what no catalogue name contains: ";" and "=",
# which are how a whole second query rides inside one value, and the control
# and separator characters, which would put part of a value on a log line of
# its own. Both have cases of their own below, and they are the reason these
# are pins rather than a blanket "non-ASCII is fine".
#
# Held per region rather than folded into one case because the value that
# breaks is region-specific. A category dropped from the set, or any future
# narrowing of the guard, surfaces here as the region it costs -- not as a
# single anonymous failure that a US-only test run would never produce at all.
#
# The jp, fr and it names come from a sweep of the live taxonomies; the rest
# are the shapes those taxonomies use, spelled with characters the same sweep
# measured.

@pytest.mark.parametrize("query,expected", [
    # U+30FB, the katakana middle dot: the conjunction Japanese writes where
    # English writes "&", so this is ordinary in the jp taxonomy rather than an
    # edge case. Punctuation, and punctuation is vocabulary.
    pytest.param(
        "genre=文学・フィクション",
        "genre=%E6%96%87%E5%AD%A6%E3%83%BB%E3%83%95%E3%82%A3%E3%82%AF%E3%82%B7%E3%83%A7%E3%83%B3",
        id="jp-katakana-middle-dot",
    ),
    pytest.param(
        "genre=SF・ファンタジー",
        "genre=SF%E3%83%BB%E3%83%95%E3%82%A1%E3%83%B3%E3%82%BF%E3%82%B8%E3%83%BC",
        id="jp-katakana-middle-dot-2",
    ),
    # U+FF08/U+FF09, the fullwidth parentheses, and U+3001, the ideographic
    # comma. Categories Ps, Pe and Po -- three separate categories in one jp
    # name, so dropping any one of them costs the jp taxonomy on its own.
    pytest.param(
        "genre=文学・フィクション（日本）",
        "genre=%E6%96%87%E5%AD%A6%E3%83%BB%E3%83%95%E3%82%A3%E3%82%AF%E3%82%B7%E3%83%A7%E3%83%B3"
        "%EF%BC%88%E6%97%A5%E6%9C%AC%EF%BC%89",
        id="jp-fullwidth-parentheses",
    ),
    pytest.param(
        "genre=小説・文芸、ノンフィクション",
        "genre=%E5%B0%8F%E8%AA%AC%E3%83%BB%E6%96%87%E8%8A%B8%E3%80%81"
        "%E3%83%8E%E3%83%B3%E3%83%95%E3%82%A3%E3%82%AF%E3%82%B7%E3%83%A7%E3%83%B3",
        id="jp-ideographic-comma",
    ),
    # U+2019, the typographic apostrophe -- a different codepoint from ASCII
    # "'", and the one these catalogues use.
    pytest.param("genre=Femmes d’affaires", "genre=Femmes+d%E2%80%99affaires", id="fr-u2019-apostrophe"),
    pytest.param(
        "genre=Sport e attività all’aperto",
        "genre=Sport+e+attivit%C3%A0+all%E2%80%99aperto",
        id="it-u2019-apostrophe",
    ),
    # ":" and "/" are ordinary separators in a taxonomy name and are both Po.
    # They also both survive urlencode as an escape (%3A, %2F), which is the
    # detail worth pinning: kept is not the same as kept verbatim in the line.
    pytest.param(
        "genre=Arts et divertissement : cinéma",
        "genre=Arts+et+divertissement+%3A+cin%C3%A9ma",
        id="fr-colon",
    ),
    pytest.param(
        "genre=Bandes dessinées/Comics",
        "genre=Bandes+dessin%C3%A9es%2FComics",
        id="fr-solidus",
    ),
    # Accented letters are letters, so these survive whole -- and always did,
    # which is why they are here: they are the control on the cases above.
    pytest.param("genre=Économie et gestion", "genre=%C3%89conomie+et+gestion", id="fr-accents-kept"),
    pytest.param(
        "genre=Ciencia ficción y fantasía",
        "genre=Ciencia+ficci%C3%B3n+y+fantas%C3%ADa",
        id="es-accents-kept",
    ),
    pytest.param(
        "genre=Biografias e Memórias",
        "genre=Biografias+e+Mem%C3%B3rias",
        id="br-accents-kept",
    ),
    pytest.param("genre=Bücher", "genre=B%C3%BCcher", id="de-umlaut-kept"),
    # A Devanagari vowel sign is a combining mark rather than a letter, so this
    # name has no punctuation in it at all and still needs the mark categories
    # to survive. The same holds for Tamil and Thai names.
    pytest.param(
        "genre=साहित्य और कथा",
        "genre=%E0%A4%B8%E0%A4%BE%E0%A4%B9%E0%A4%BF%E0%A4%A4%E0%A5%8D%E0%A4%AF"
        "+%E0%A4%94%E0%A4%B0+%E0%A4%95%E0%A4%A5%E0%A4%BE",
        id="in-devanagari-combining-mark",
    ),
    # The us/uk taxonomies lean on "&" and the ASCII apostrophe, sent encoded
    # as a client should encode them. "&" is a symbol and "'" is punctuation.
    pytest.param(
        "genre=Mystery%2C+Thriller+%26+Suspense",
        "genre=Mystery%2C+Thriller+%26+Suspense",
        id="us-ampersand",
    ),
    pytest.param("genre=Children%27s+Books", "genre=Children%27s+Books", id="uk-ascii-apostrophe"),
])
def test_redact_query_pins_the_localized_genre_values_each_region_sends(query, expected):
    assert _redact_query(query) == expected


@pytest.mark.parametrize("genre", [
    "Économie et gestion",
    "Ciencia ficción y fantasía",
    "Biografias e Memórias",
    "Bücher",
    "文学・フィクション",
    "文学・フィクション（日本）",
    "小説・文芸、ノンフィクション",
    "Femmes d’affaires",
    "Sport e attività all’aperto",
    "साहित्य और कथा",
    "Mystery, Thriller & Suspense",
    "Children's Books",
    "Bandes dessinées/Comics",
])
def test_a_kept_genre_value_comes_back_as_itself(genre):
    """
    Not redacted is not the same as intact: the value has to survive exactly.

    The cases above assert the encoded log line character for character; this
    asserts the thing an operator actually reads, that decoding that line gives
    back the name the marketplace sent. A guard that kept a value but mangled a
    codepoint on the way through would pass one and fail the other.

    Built with urlencode rather than interpolated, so the "&" in the us name
    arrives as one value instead of splitting into a value and a bare key --
    the split is real and is pinned separately below, but it is not what this
    is measuring.
    """
    result = _redact_query(urllib.parse.urlencode({"genre": genre}))
    assert dict(urllib.parse.parse_qsl(result))["genre"] == genre


def test_an_unencoded_ampersand_in_a_genre_keeps_the_head_and_drops_the_tail():
    """
    "Arts & Entertainment" sent raw splits mid-value, exactly as a typed title
    does -- but the half that survives here is catalogue text, not caller text,
    so it is kept rather than redacted. Pinned because the result looks like a
    leak beside the caller-authored cases above and is not one.
    """
    assert _redact_query("genre=Arts & Entertainment") == "genre=Arts+&_unrecognised=1"


# ============================================================
# WHAT THE WIDENED GUARD STILL REFUSES
# ============================================================

# The categories the guard omits are the whole of its remaining teeth, and
# omission is invisible: nothing in the source says "Cf is excluded", the set
# simply does not list it. These are what make that silence load-bearing --
# each names a category that is absent on purpose and asserts what its absence
# buys. Add a category to the set and one of these goes red with the reason.


def test_redact_query_redacts_a_value_carrying_a_bidi_override():
    """
    U+202E reorders everything after it when a line is rendered, so a value
    carrying one can make a log line read as something other than what arrived
    -- a status, a path, another param's value, all forgeable in the reader's
    eyes without forging anything in the record.

    It is category Cf, which the set leaves out, and leaving Cf out is a trade
    with a real cost: Cf also holds ZWNJ and ZWJ (U+200C/U+200D), which Indic
    and Persian text genuinely uses. No measured genre name contains one, so
    the cost is currently nothing and the override stays out. This is the
    property that bought. If a real ZWNJ name does turn up, the fix is to admit
    those two codepoints by name and this must still pass afterwards.
    """
    assert _redact_query("genre=Thriller‮Suspense") == "genre=REDACTED"
    assert _redact_query("genre=Thriller%E2%80%AEJane+Doe") == "genre=REDACTED"
    assert "Jane" not in _redact_query("genre=Thriller%E2%80%AEJane+Doe")


@pytest.mark.parametrize("raw", [
    pytest.param("genre=Fiction%0AINFO+forged+log+line", id="LF"),
    pytest.param("genre=Fiction%0D%0AINFO+forged+log+line", id="CRLF"),
    pytest.param("genre=Fiction%00Thriller", id="NUL"),
    pytest.param("genre=Fiction%09Thriller", id="TAB"),
    pytest.param("genre=Fiction%C2%85Thriller", id="U+0085-NEL"),
    pytest.param("genre=Fiction%E2%80%A8Thriller", id="U+2028-line-separator"),
    pytest.param("genre=Fiction%E2%80%A9Thriller", id="U+2029-paragraph-separator"),
])
def test_redact_query_redacts_a_value_carrying_a_control_or_line_separator(raw):
    """
    Categories Cc, Zl and Zp are absent from the set, which is what covers
    these seven in one stroke rather than as seven listed characters.

    None of them appears in any catalogue name, and each would put part of a
    value on a log line of its own, where it reads as a record Libex emitted.
    urlencode would percent-encode them downstream anyway; the guarantee is
    held here as well so it does not depend on a later caller keeping it.
    """
    assert _redact_query(raw) == "genre=REDACTED"
    assert "forged" not in _redact_query(raw)


# ============================================================
# THE LOG RECORD
# ============================================================

def _app_with_logging():
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


async def _request(caplog, path, headers=None, client=("1.2.3.4", 1234)):
    transport = ASGITransport(app=_app_with_logging(), client=client)
    with caplog.at_level(logging.INFO):
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            await ac.get(path, headers=headers or {})
    return [r for r in caplog.records if r.getMessage() == "Request completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {},
    {"CF-Connecting-IP": "203.0.113.5"},
    {"x-real-ip": "203.0.113.5"},
    {"CF-Connecting-IP": "203.0.113.5", "x-real-ip": "198.51.100.7"},
])
async def test_no_client_address_reaches_the_log_record(caplog, headers):
    """The guard: no header and no connection can put an address in the log."""
    records = await _request(caplog, "/probe", headers)
    assert len(records) == 1
    record = records[0]

    assert not hasattr(record, "ip")
    serialised = str(record.__dict__)
    assert "203.0.113.5" not in serialised
    assert "198.51.100.7" not in serialised
    assert "1.2.3.4" not in serialised


@pytest.mark.asyncio
async def test_monitoring_fields_survive(caplog):
    """Per-endpoint failure rates depend on these; privacy work must not cost them."""
    records = await _request(caplog, "/probe?region=us")
    record = records[0]

    assert record.url == "/probe"
    assert record.method == "GET"
    assert record.status == 200
    assert isinstance(record.took, float)
    assert record.query == "region=us"
    assert hasattr(record, "userAgent")
    assert hasattr(record, "host")


@pytest.mark.asyncio
async def test_caller_authored_query_is_redacted_in_the_record(caplog):
    records = await _request(caplog, "/probe?name=Some+Person&region=us")
    record = records[0]

    assert "Some" not in record.query
    assert "region=us" in record.query
    assert "name=REDACTED" in record.query


@pytest.mark.asyncio
async def test_unrecognised_query_key_never_reaches_the_record(caplog):
    """End to end: the half of a typed value an unencoded "&" split off."""
    records = await _request(caplog, "/probe?title=Salt&Pepper&region=us")
    record = records[0]

    assert record.query == "title=REDACTED&region=us&_unrecognised=1"
    assert "Pepper" not in str(record.__dict__)


@pytest.mark.asyncio
async def test_health_is_not_logged(caplog):
    assert await _request(caplog, "/health") == []


# ============================================================
# SELF-HOSTED DOCS ASSETS
# ============================================================

# FastAPI's defaults have the browser fetch Swagger UI and ReDoc from these,
# which would send a docs visitor's real IP address to each of them.
_THIRD_PARTY_ASSET_HOSTS = ["cdn.jsdelivr.net", "fonts.googleapis.com", "fastapi.tiangolo.com"]


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_docs_pages_reference_no_third_party_hosts(client, path):
    body = client.get(path).text
    referenced = [h for h in _THIRD_PARTY_ASSET_HOSTS if h in body]
    assert referenced == [], f"{path} would send a visitor's IP to {referenced}"


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_docs_pages_render(client, path):
    assert client.get(path).status_code == 200


# Every src/href the pages emit. A named-host check only catches the three
# hosts someone thought of; this catches a fourth.
_ASSET_URL = re.compile(r'(?:src|href)="([^"]+)"')

_FETCH_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch_docs_assets.sh"
_DEST_FILE = re.compile(r'"\$DEST/([^"]+)"')

# What a stylesheet makes a browser fetch by itself. Both forms of @import are
# matched, because a sheet that dropped the url() wrapper would still pull the
# file in and would otherwise read here as a sheet that imports nothing.
_CSS_IMPORT = re.compile(r'@import\s+(?:url\()?["\']([^"\']+)["\']')
_CSS_URL = re.compile(r'url\(\s*["\']?([^"\')]+)["\']?\s*\)')

# The first eight bytes of every PNG. StaticFiles types the response from the
# file extension and never looks inside, so a placeholder or an unresolved
# pointer is served as 200 image/png exactly like the real artwork.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _libex_stylesheets(client, path):
    """The stylesheets a docs page links that Libex commits itself, as
    (url, text) pairs.

    Sheets under /static/docs are excluded rather than skipped on 404: that
    directory is gitignored and populated at build time, so it is absent on a
    fresh checkout and in CI, and opening one would test what the runner
    happens to have on disk. Everything else under /static is committed, so a
    404 there is the failure being looked for and not a reason to pass.
    """
    urls = [
        url for url in _ASSET_URL.findall(client.get(path).text)
        if url.endswith(".css") and not url.startswith("/static/docs/")
    ]
    sheets = []
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, f"{path} links {url}, which does not resolve"
        sheets.append((url, response.text))
    return sheets


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_docs_pages_reference_only_same_origin_urls(client, path):
    """Any absolute URL here is a host a visitor's browser would be sent to."""
    urls = _ASSET_URL.findall(client.get(path).text)
    assert urls, f"{path} referenced no assets at all"
    external = [u for u in urls if not u.startswith("/") or u.startswith("//")]
    assert external == [], f"{path} would send a visitor's IP to {external}"


@pytest.mark.parametrize("path,expected", [
    ("/docs", {"swagger-ui-bundle.js", "swagger-ui.css"}),
    ("/redoc", {"redoc.standalone.js"}),
])
def test_docs_pages_load_the_files_the_build_fetches(client, path, expected):
    """
    The absence of a CDN host is not the presence of a working local asset.

    Asserts the two sides of the contract line up: what the page asks the
    browser for, and what scripts/fetch_docs_assets.sh actually writes into
    app/static/docs. A rename on either side breaks the docs silently
    otherwise, because the page still returns 200 and still names no third
    party while loading nothing.

    The chain now spans two artefacts on /docs. get_swagger_ui_html takes one
    stylesheet URL, so pointing it at Libex's own skin means the vendored
    sheet is reached through that skin's @import rather than named by the page
    -- and a browser fetches it either way. Following the link is what keeps
    this test asserting the contract instead of the spelling of it.

    Deliberately not asserting those URLs return 200. The directory is
    gitignored and populated at build time, so it is absent on a fresh
    checkout and in CI -- a status check here would test what the runner
    happens to have on disk, not what the code does.
    """
    fetched = set(_DEST_FILE.findall(_FETCH_SCRIPT.read_text()))
    assert expected <= fetched, f"fetch_docs_assets.sh no longer produces {expected - fetched}"

    reached = set(_ASSET_URL.findall(client.get(path).text))
    for _, text in _libex_stylesheets(client, path):
        reached |= set(_CSS_IMPORT.findall(text))

    for name in expected:
        assert f"/static/docs/{name}" in reached, f"{path} never reaches {name}"


def test_docs_stylesheets_reference_no_third_party_hosts(client):
    """
    The pages themselves are checked above, and cannot see this: a stylesheet
    is fetched by the browser and then acts on its own, so a single absolute
    url() inside one sends a visitor's IP somewhere the page never named.
    Libex publishes that opening the docs contacts nothing but Libex, and that
    claim now spans a file the page-level checks only link to.

    data: URIs are same-origin by construction -- they are the bytes, not a
    request -- and are how the vendored sheet carries its own four assets.
    """
    sheets = [sheet for path in ("/docs", "/redoc") for sheet in _libex_stylesheets(client, path)]
    assert sheets, "the docs pages link no stylesheet Libex serves itself"

    for url, text in sheets:
        referenced = _CSS_URL.findall(text) + _CSS_IMPORT.findall(text)
        external = [
            ref for ref in referenced
            if not ref.startswith("data:") and (not ref.startswith("/") or ref.startswith("//"))
        ]
        assert external == [], f"{url} would send a visitor's IP to {external}"


def test_swagger_page_reaches_its_logo(client):
    """
    Swagger UI ignores the info.x-logo key ReDoc renders its logo from, so on
    /docs the mark is placed by a stylesheet instead -- which means nothing in
    the OpenAPI document guards it and a rename shows up only as a blank space
    in a visitor's browser.

    Deliberately blind to how it is placed: no rule text, no dimensions, no
    which-file, so a restyle or a swap to the dark artwork is not a failure.
    What is asserted is the join a restyle must not break -- the sheet /docs
    links references an image Libex serves, and that image is really there.
    """
    images = {
        ref
        for _, text in _libex_stylesheets(client, "/docs")
        for ref in _CSS_URL.findall(text)
        # Same-origin non-stylesheets: the data: URIs and any absolute URL
        # belong to the check above, and a second failure there reads as a
        # missing logo when it is nothing of the kind.
        if ref.startswith("/") and not ref.startswith("//") and not ref.endswith(".css")
    }
    assert images, "/docs links no stylesheet that references an image -- where is the logo?"

    for url in images:
        response = client.get(url)
        assert response.status_code == 200, f"/docs styles in {url}, which does not resolve"
        assert response.headers["content-type"].startswith("image/")
        if url.endswith(".png"):
            assert response.content[:8] == _PNG_MAGIC


def test_static_mount_serves_the_committed_favicon(client):
    """The favicon is committed, so unlike the fetched assets it must resolve.
    Fetched by the URL the app hands the pages, so repointing the constant at
    a path nothing serves fails here rather than in a browser's tab strip."""
    response = client.get(_FAVICON)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == _PNG_MAGIC


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_docs_pages_reference_the_committed_favicon(client, path):
    """Both pages take their icon from the same constant, so both are checked:
    an icon wired into one page and left off the other is a difference nobody
    notices from the page that has it."""
    assert f'href="{_FAVICON}"' in client.get(path).text
