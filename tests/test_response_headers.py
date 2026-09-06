"""
Tests for app.core.response_headers: the header-name registry, the source
tally, the incomplete-reason vocabulary, and ResponseFacts -- plus the
three-part conformance check that keeps every consumer of the registry
honest.

The conformance check has three parts, and (b) is the one that actually
holds the line:

  (a) registry consistency -- every emitted-header constant is registered,
      and CORSMiddleware's expose_headers is exactly the registry unioned
      with the migration headers.
  (b) source scan -- walks app/ for four literal assignment forms --
      `response.headers["Literal"] = ...`, `Response(headers={"Literal": ...})`,
      `response.headers.update({"Literal": ...})`, and
      `response.headers.setdefault("Literal", ...)` -- and asserts every
      custom (X-prefixed) header name assigned that way resolves into the
      registry, or is one of the two already-identified, not-yet-ruled-on
      exceptions named where the scan is defined below. (a) and (c) both
      only ever see headers that were already registered; this one reads
      the header name out of the source text itself rather than out of a
      constant a forgetful change never touched. All four forms share that
      same limit: none of them sees a name that arrives through a variable
      rather than a literal -- `Response(headers=some_dict)` or
      `.headers.update(a_notice.headers)` hand the regex nothing to read at
      all, which is a real limit of a source-text scan and not a gap this
      widening closes. The extraction regex is proven separately against a
      fixture, not against app/'s own content, because app/ is not empty
      under the widened scan the way it was under the single-form one --
      see the scan's own definition for what it currently finds and why.
  (c) response walk -- for a representative request per route family, with
      services mocked, every x-libex-* header actually present on the
      response also appears in access-control-expose-headers, so a real
      request and the registry never quietly diverge.
"""

# Standard library
import re
from pathlib import Path
from unittest.mock import patch

# Third party
import pytest
from httpx import AsyncClient, ASGITransport

# Local
from app.main import app
from app.api.routes.facts_headers import FACTS_RESPONSE_HEADERS
from app.core.migration_notice import MIGRATION_HEADER_NAMES
from app.core.response_headers import (
    EXPOSED_HEADER_NAMES,
    HEADER_COMPLETE,
    HEADER_INCOMPLETE_REASON,
    HEADER_REQUEST_ID,
    HEADER_SOURCE,
    INCOMPLETE_REASONS,
    REASON_DISCOVERY_INCOMPLETE,
    REASON_HYDRATION_DEADLINE,
    REASON_HYDRATION_FAILED,
    REASON_HYDRATION_NOT_FOUND,
    SOURCE_AUDIBLE,
    SOURCE_CACHE,
    SOURCE_DB,
    SOURCE_MIXED,
    SOURCES,
    ResponseFacts,
    format_incomplete_reason_header,
    format_source_header,
    record_incomplete,
    record_source,
    record_source_keys,
)


@pytest.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ============================================================
# format_source_header
# ============================================================


def test_single_populated_source_renders_as_the_bare_token():
    assert format_source_header({SOURCE_CACHE: 1}) == "cache"


def test_single_populated_source_ignores_its_own_count():
    """One source already carries the whole truth -- the count is not
    appended, whatever its value."""
    assert format_source_header({SOURCE_CACHE: 41}) == "cache"


def test_zero_count_entries_are_dropped_before_judging_singular():
    """A tally naming all three sources at zero, with only one actually
    populated, still renders as that one bare token -- the zero entries do
    not make it look mixed."""
    assert format_source_header({SOURCE_AUDIBLE: 3, SOURCE_CACHE: 0, SOURCE_DB: 0}) == "audible"


def test_two_populated_sources_render_mixed_with_counts():
    assert format_source_header({SOURCE_AUDIBLE: 2, SOURCE_CACHE: 1}) == "mixed; audible=2; cache=1"


def test_mixed_parameter_order_is_the_closed_vocabularys_order_not_insertion_order():
    """SOURCES fixes audible, cache, db. A tally built with db and audible
    populated, inserted in that (reversed) order, must still render
    audible before db -- the header value for a given failure mode must not
    depend on which source happened to be recorded first."""
    counts = {SOURCE_DB: 4, SOURCE_AUDIBLE: 9}
    assert format_source_header(counts) == "mixed; audible=9; db=4"


def test_all_three_sources_populated():
    counts = {SOURCE_CACHE: 1, SOURCE_DB: 2, SOURCE_AUDIBLE: 3}
    assert format_source_header(counts) == "mixed; audible=3; cache=1; db=2"


def test_empty_tally_renders_as_the_empty_string():
    assert format_source_header({}) == ""


def test_all_zero_tally_renders_as_the_empty_string():
    assert format_source_header({SOURCE_AUDIBLE: 0, SOURCE_CACHE: 0, SOURCE_DB: 0}) == ""


# ============================================================
# format_incomplete_reason_header
# ============================================================


def test_single_reason_renders_bare():
    assert format_incomplete_reason_header({REASON_HYDRATION_FAILED}) == "hydration-failed"


def test_multiple_reasons_render_in_vocabulary_order_not_insertion_order():
    """Inserted in reverse of INCOMPLETE_REASONS order -- the rendered
    value must still come out in that fixed order, so the string for a
    given failure combination is always the same regardless of which
    reason was recorded first."""
    reasons = {REASON_HYDRATION_NOT_FOUND, REASON_DISCOVERY_INCOMPLETE, REASON_HYDRATION_DEADLINE}
    assert format_incomplete_reason_header(reasons) == (
        "discovery-incomplete, hydration-deadline, hydration-not-found"
    )


def test_no_reasons_renders_as_the_empty_string():
    assert format_incomplete_reason_header(set()) == ""


def test_incomplete_reasons_is_the_closed_vocabulary_in_render_order():
    assert INCOMPLETE_REASONS == (
        REASON_DISCOVERY_INCOMPLETE,
        REASON_HYDRATION_DEADLINE,
        REASON_HYDRATION_FAILED,
        REASON_HYDRATION_NOT_FOUND,
    )


# ============================================================
# ResponseFacts / record_source / record_incomplete
# ============================================================


def test_fresh_facts_starts_complete_with_every_source_at_zero():
    facts = ResponseFacts()
    assert facts.is_complete is True
    assert facts.source_counts == {source: 0 for source in SOURCES}
    assert facts.source_header_value() == ""


def test_record_source_increments_the_named_source():
    facts = ResponseFacts()
    record_source(facts, SOURCE_CACHE)
    record_source(facts, SOURCE_CACHE, 4)
    assert facts.source_counts[SOURCE_CACHE] == 5
    assert facts.source_header_value() == "cache"


def test_record_source_rejects_mixed_as_a_recorded_value():
    """"mixed" is a rendering outcome the formatter produces, never
    something a caller records directly -- recording it is a programming
    mistake and must raise, not silently miscount."""
    facts = ResponseFacts()
    with pytest.raises(ValueError):
        record_source(facts, SOURCE_MIXED)


def test_record_source_rejects_an_unknown_source():
    facts = ResponseFacts()
    with pytest.raises(ValueError):
        record_source(facts, "not-a-real-source")


def test_record_source_is_a_no_op_when_facts_is_none():
    """Must not raise -- every call site can pass facts=None unconditionally
    rather than growing its own `if facts is not None` guard."""
    record_source(None, SOURCE_CACHE)


def test_record_incomplete_marks_facts_incomplete_and_names_the_reason():
    facts = ResponseFacts()
    record_incomplete(facts, REASON_HYDRATION_DEADLINE)
    assert facts.is_complete is False
    assert facts.incomplete_reason_header_value() == "hydration-deadline"


def test_record_incomplete_rejects_an_unknown_reason():
    facts = ResponseFacts()
    with pytest.raises(ValueError):
        record_incomplete(facts, "not-a-real-reason")


def test_record_incomplete_is_a_no_op_when_facts_is_none():
    record_incomplete(None, REASON_HYDRATION_DEADLINE)


# ============================================================
# record_source_keys / source_header_value_for -- the honest-degradation rule
# ============================================================


def test_record_source_keys_increments_the_aggregate_and_the_per_key_map():
    facts = ResponseFacts()
    record_source_keys(facts, SOURCE_AUDIBLE, ["B01", "B02"])
    assert facts.source_counts[SOURCE_AUDIBLE] == 2
    assert facts.source_by_key == {"B01": SOURCE_AUDIBLE, "B02": SOURCE_AUDIBLE}


def test_record_source_keys_is_a_no_op_when_facts_is_none():
    record_source_keys(None, SOURCE_AUDIBLE, ["B01"])


def test_record_source_keys_rejects_an_unknown_source():
    facts = ResponseFacts()
    with pytest.raises(ValueError):
        record_source_keys(facts, "not-a-real-source", ["B01"])


def test_source_header_value_for_restricts_the_tally_to_the_given_keys():
    """Two books recorded, one per source; restricting to just the
    Audible-sourced key must read the bare "audible" token, not "mixed" --
    the whole point of a keyed tally over the plain aggregate one."""
    facts = ResponseFacts()
    record_source_keys(facts, SOURCE_AUDIBLE, ["B01"])
    record_source_keys(facts, SOURCE_CACHE, ["B02"])
    assert facts.source_header_value_for(["B01"]) == "audible"


def test_source_header_value_for_drops_keys_the_map_has_no_entry_for():
    """A key present in `keys` but never recorded is silently skipped
    rather than treated as an error -- attribute what is known, drop what
    isn't, applied per key."""
    facts = ResponseFacts()
    record_source_keys(facts, SOURCE_AUDIBLE, ["B01"])
    assert facts.source_header_value_for(["B01", "B99-never-recorded"]) == "audible"


def test_incomplete_attribution_omits_the_header_entirely():
    """The honest-degradation rule itself: when the aggregate count and the
    per-key map disagree -- here, record_source added 1 to the aggregate
    with no key behind it at all -- the per-key map cannot be trusted to
    account for the whole tally, so source_header_value_for must return the
    empty string rather than approximate. This is the exact shape a mixed
    caller (one call site using record_source, another using
    record_source_keys, against the same facts) produces."""
    facts = ResponseFacts()
    record_source_keys(facts, SOURCE_AUDIBLE, ["B01"])
    record_source(facts, SOURCE_CACHE)  # aggregate-only, no key recorded
    assert sum(facts.source_counts.values()) != len(facts.source_by_key)
    assert facts.source_header_value_for(["B01"]) == ""


def test_complete_attribution_is_unaffected_by_the_same_check():
    """Complement to the above -- the counts-vs-map equality check must not
    misfire on a ledger that is fully keyed, or every keyed route would
    lose its header."""
    facts = ResponseFacts()
    record_source_keys(facts, SOURCE_AUDIBLE, ["B01", "B02"])
    assert sum(facts.source_counts.values()) == len(facts.source_by_key)
    assert facts.source_header_value_for(["B01", "B02"]) == "audible"


# ============================================================
# (a) REGISTRY CONSISTENCY
# ============================================================


def test_every_emitted_header_constant_is_registered():
    for name in (HEADER_REQUEST_ID, HEADER_SOURCE, HEADER_COMPLETE, HEADER_INCOMPLETE_REASON):
        assert name in EXPOSED_HEADER_NAMES


def test_cors_expose_headers_is_exactly_the_registry_unioned_with_migration_headers(client):
    """The value handed to CORSMiddleware -- read back off a real response,
    not re-derived from setup_middleware's source -- is exactly
    EXPOSED_HEADER_NAMES unioned with MIGRATION_HEADER_NAMES, deduped,
    first-seen order."""
    response = client.get("/health", headers={"Origin": "https://example.com"})
    expected = ", ".join(dict.fromkeys((*EXPOSED_HEADER_NAMES, *MIGRATION_HEADER_NAMES)))
    assert response.headers["access-control-expose-headers"] == expected


# ============================================================
# X-LIBEX-INCOMPLETE-REASON -- OPENAPI SCHEMA HONESTY
# ============================================================
#
# The value is a comma-joined subset of the closed reason vocabulary
# (INCOMPLETE_REASONS), not a single member of it -- "discovery-incomplete,
# hydration-failed" is a real, valid value that is not itself any one entry
# in that tuple. An OpenAPI `enum` naming the four reasons individually
# would therefore describe a header whose real values routinely fall
# outside the declared enum -- unsatisfiable for exactly the multi-reason
# case the header exists to carry -- so the schema must declare it as a
# plain string, no `enum` key at all.


def test_incomplete_reason_header_schema_declares_no_enum():
    schema = FACTS_RESPONSE_HEADERS[HEADER_INCOMPLETE_REASON]["schema"]
    assert "enum" not in schema
    assert schema["type"] == "string"


@pytest.mark.asyncio
async def test_a_multi_reason_incomplete_value_renders_through_a_real_response(async_client):
    """A response whose facts carry more than one incomplete reason must
    still render the full comma-joined value on the wire, not just the
    first one or a truncated/rejected value -- the schema change above only
    holds if the header it describes actually can carry this shape."""

    async def fake_get_book(asin, region, session, cache, *, facts=None):
        record_incomplete(facts, REASON_HYDRATION_FAILED)
        record_incomplete(facts, REASON_DISCOVERY_INCOMPLETE)
        return MOCK_BOOK_FOR_HEADER_WALK

    with patch("app.api.routes.books.router.get_book_by_asin", side_effect=fake_get_book):
        response = await async_client.get("/book/B08G9PRS1K")

    assert response.headers["x-libex-complete"] == "false"
    assert response.headers["x-libex-incomplete-reason"] == "discovery-incomplete, hydration-failed"


# ============================================================
# (b) SOURCE SCAN -- the part that actually holds the line
# ============================================================

# app/ two directories up from this file (tests/test_response_headers.py).
_APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Four literal assignment shapes, matched against raw source text rather
# than executed:
#
#   (1) response.headers["Name"] = ...            -- the original form
#   (2) Response(..., headers={"Name": ...})       -- a literal dict handed
#       to a Response (or any other call) as a `headers=` keyword argument
#   (3) response.headers.update({"Name": ...})     -- a literal dict merged in
#   (4) response.headers.setdefault("Name", ...)   -- a single literal name
#
# None of the four matches a constant-based assignment
# (`response.headers[HEADER_SOURCE] = ...`) -- those can't drift from the
# registry because they *are* the registry; what this exists to catch is
# exactly what a constant can't produce: a hand-typed literal never added to
# EXPOSED_HEADER_NAMES at all. And none of the four sees a name that arrives
# through a variable rather than a literal -- `Response(headers=some_dict)`
# or `.headers.update(a_notice.headers)` hand the regex nothing to read.
#
# (2) is deliberately unanchored to `Response(` specifically: it matches any
# `..._headers = {` assignment, keyword or plain, because that is the only
# way to also catch a name assigned to a differently-named kwarg that still
# ends in "headers" -- see _custom_headers_assigned_in_app's own result for
# what that widening currently surfaces in app/, including one hit that is a
# request header rather than a response one for exactly this reason.
_HEADER_ASSIGNMENT_RE = re.compile(r"""\.headers\[\s*["']([^"']+)["']\s*\]\s*=""")
_HEADERS_KWARG_RE = re.compile(r"""headers\s*=\s*\{(?P<body>[^{}]*)\}""")
_HEADERS_UPDATE_RE = re.compile(r"""\.headers\.update\(\s*\{(?P<body>[^{}]*)\}\s*\)""")
_HEADERS_SETDEFAULT_RE = re.compile(r"""\.headers\.setdefault\(\s*["'](?P<name>[^"']+)["']""")
# A dict-literal key: a quoted string immediately followed by a colon. Used
# to pull names back out of whatever _HEADERS_KWARG_RE / _HEADERS_UPDATE_RE
# captured as a dict body -- deliberately not a nested-brace-aware parser,
# so a value itself containing braces (an f-string interpolation such as
# `f"@{...}"`) can make a match start at that inner brace pair instead of the
# dict's own -- harmless here because an interpolation's own contents are
# never themselves a quoted-key-colon pair, so nothing spurious is extracted
# from it, but this is a plain-text pattern match, not a Python parser.
_DICT_LITERAL_KEY_RE = re.compile(r"""["']([^"']+)["']\s*:""")


def _extract_custom_headers(text: str) -> set[str]:
    """Every literal header name `text` assigns via one of the four forms
    above -- restricted to Libex's own custom vocabulary (the `X-` prefix),
    the part of the wire format response_headers.py actually claims
    ownership of. Standard headers Libex also sets by hand (Cache-Control,
    Access-Control-Allow-Origin, Access-Control-Expose-Headers) are a
    different, already-standard vocabulary with no registry of their own
    and are deliberately not in scope here.

    Takes raw text rather than reading app/ itself, so the extraction logic
    can be proven correct against a fixture (see
    test_header_assignment_regex_extracts_a_known_positive below) as well as
    run for real over app/'s own source (see
    _custom_headers_assigned_in_app). The two are deliberately split: unlike
    the original single-form scan, app/ does not assign zero custom headers
    this way -- see _KNOWN_NON_RESPONSE_HEADER_NAMES below for the two names
    the widened scan currently finds and why neither is registered -- so a
    fixture that does not depend on what app/ currently contains is what
    tells a broken regex apart from a codebase whose only findings are the
    ones already named.
    """
    found = set()
    for match in _HEADER_ASSIGNMENT_RE.finditer(text):
        name = match.group(1)
        if name.lower().startswith("x-"):
            found.add(name)
    for pattern in (_HEADERS_KWARG_RE, _HEADERS_UPDATE_RE):
        for match in pattern.finditer(text):
            for name in _DICT_LITERAL_KEY_RE.findall(match.group("body")):
                if name.lower().startswith("x-"):
                    found.add(name)
    for match in _HEADERS_SETDEFAULT_RE.finditer(text):
        name = match.group("name")
        if name.lower().startswith("x-"):
            found.add(name)
    return found


def _custom_headers_assigned_in_app() -> set[str]:
    """_extract_custom_headers run for real over every .py file in app/."""
    found = set()
    for path in _APP_DIR.rglob("*.py"):
        found |= _extract_custom_headers(path.read_text())
    return found


def test_header_assignment_regex_extracts_a_known_positive():
    """Proves the scanner's extraction logic against a fixture, independent
    of what app/ currently contains -- covering all four forms, both quote
    styles where relevant, and confirming a same-shape assignment to a
    non-X- header is correctly left out on every form, matching what
    _extract_custom_headers itself filters on. Also confirms the one
    deliberately-loose piece of (2): a kwarg named `extra_headers` (not
    `headers`) still matches, because that is exactly the shape
    app/services/audible/authors/screens.py has today and the scan is
    widened to see it on purpose -- see _KNOWN_NON_RESPONSE_HEADER_NAMES
    below for why that particular hit is not itself a registry gap."""
    fixture = (
        'response.headers["X-Test-Header"] = "value"\n'
        "response.headers['X-Other-Header'] = compute()\n"
        'response.headers["Not-Custom"] = "value"\n'
        'return Response(headers={"X-Kwarg-Header": "value", "Not-Custom": "v"})\n'
        'extra_headers={"X-Extra-Header": "value"}\n'
        'response.headers.update({"X-Update-Header": "value", "Not-Custom": "v"})\n'
        'response.headers.update(some_variable_notice.headers)\n'
        'response.headers.setdefault("X-Setdefault-Header", "value")\n'
        'response.headers.setdefault("Not-Custom", "value")\n'
    )
    assert _extract_custom_headers(fixture) == {
        "X-Test-Header",
        "X-Other-Header",
        "X-Kwarg-Header",
        "X-Extra-Header",
        "X-Update-Header",
        "X-Setdefault-Header",
    }


# The widened scan below finds exactly two X-prefixed literals that are not
# in EXPOSED_HEADER_NAMES, and neither is a defect this test can resolve on
# its own -- each is an open scope question for the lane that owns its call
# site, not something a test-only slice rules on:
#
#   X-Content-Type-Options (app/api/routes/db/badge.py, via a literal
#   `Response(headers={...})`) -- a standard browser security header the
#   badge route sets on its own SVG responses, not a Libex-invented token a
#   JS caller would ever read via fetch()/XHR the way the four names in
#   EXPOSED_HEADER_NAMES are. It may belong in the same already-standard,
#   no-registry-needed category this module's docstring already carves
#   Cache-Control and the two Access-Control-* headers out of -- it simply
#   keeps the legacy "X-" spelling that category's other members don't.
#
#   X-Device-Type-Id (app/services/audible/authors/screens.py, via
#   `extra_headers={...}` passed to audible_get()) -- not a response header
#   at all: it is a request header Libex sends *to* Audible. This registry
#   and scan exist to govern what Libex emits on its own responses; a
#   request-bound header dict happens to share the literal "headers={"
#   shape the widened (2) form looks for, which a source-text scan cannot
#   tell apart from the response-bound case just by reading it.
#
# Both are named exactly, rather than swept up in a broader "anything
# already found is fine" allowance, so a third name appearing here later
# still fails this test loudly instead of being silently absorbed.
_KNOWN_NON_RESPONSE_HEADER_NAMES = frozenset({
    "X-Content-Type-Options",
    "X-Device-Type-Id",
})


def test_every_custom_header_assigned_in_app_resolves_into_the_registry():
    """The one part of this module that inspection alone can't verify: a
    header assigned somewhere in app/ under a hand-typed literal name, with
    that literal never added to EXPOSED_HEADER_NAMES, is exactly the defect
    this test exists to catch -- now across all four assignment forms, not
    only the subscript one. The two names in
    _KNOWN_NON_RESPONSE_HEADER_NAMES are asserted for exactly, not merely
    excluded: this test fails if app/ ever assigns a third unregistered
    custom header this way, and also fails if either of the two named ones
    stops being found, so a stale exception cannot outlive the code it was
    written against."""
    found = _custom_headers_assigned_in_app()
    unregistered = found - set(EXPOSED_HEADER_NAMES)
    assert unregistered == _KNOWN_NON_RESPONSE_HEADER_NAMES, (
        "assigned in app/ but never registered in EXPOSED_HEADER_NAMES, and "
        "not matching the already-identified exceptions above -- "
        f"unexpected: {unregistered - _KNOWN_NON_RESPONSE_HEADER_NAMES}, "
        f"missing: {_KNOWN_NON_RESPONSE_HEADER_NAMES - unregistered}"
    )


# ============================================================
# (c) RESPONSE WALK -- a real request per route family
# ============================================================


def _facts_recorder(source, book):
    async def _fake(*args, **kwargs):
        record_source(kwargs.get("facts"), source)
        return book

    return _fake


def _libex_headers(response) -> set[str]:
    return {name for name in response.headers if name.lower().startswith("x-libex")}


def _exposed_names(response) -> set[str]:
    return {name.strip().lower() for name in response.headers["access-control-expose-headers"].split(",")}


@pytest.mark.asyncio
async def test_book_route_headers_are_all_exposed(async_client):
    with patch("app.api.routes.books.router.get_book_by_asin", side_effect=_facts_recorder(SOURCE_CACHE, MOCK_BOOK_FOR_HEADER_WALK)):
        response = await async_client.get("/book/B08G9PRS1K", headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    libex = _libex_headers(response)
    assert libex, "no x-libex-* headers present to check"
    assert libex <= _exposed_names(response)


@pytest.mark.asyncio
async def test_series_route_headers_are_all_exposed(async_client):
    with patch("app.api.routes.series.router.get_series", side_effect=_facts_recorder(SOURCE_AUDIBLE, MOCK_SERIES_FOR_HEADER_WALK)):
        response = await async_client.get("/series/B00SERIES1", headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    libex = _libex_headers(response)
    assert libex, "no x-libex-* headers present to check"
    assert libex <= _exposed_names(response)


@pytest.mark.asyncio
async def test_author_route_headers_are_all_exposed(async_client):
    with patch("app.api.routes.authors.router.get_author", side_effect=_facts_recorder(SOURCE_DB, MOCK_AUTHOR_FOR_HEADER_WALK)):
        response = await async_client.get("/author/B000APF21M", headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    libex = _libex_headers(response)
    assert libex, "no x-libex-* headers present to check"
    assert libex <= _exposed_names(response)


MOCK_BOOK_FOR_HEADER_WALK = {
    "asin": "B08G9PRS1K",
    "title": "Test Book",
    "subtitle": None,
    "description": "A test book description",
    "summary": "A test summary",
    "region": "us",
    "regions": ["us"],
    "publisher": "Test Publisher",
    "copyright": None,
    "isbn": None,
    "language": "english",
    "rating": 4.5,
    "bookFormat": None,
    "releaseDate": "2021-01-01T00:00:00+00:00",
    "explicit": False,
    "hasPdf": False,
    "whisperSync": False,
    "imageUrl": "https://example.com/cover.jpg",
    "lengthMinutes": 600,
    "link": "https://audible.com/pd/B08G9PRS1K",
    "contentType": "Product",
    "contentDeliveryType": None,
    "episodeNumber": None,
    "episodeType": None,
    "sku": None,
    "skuGroup": None,
    "isListenable": True,
    "isAvailable": True,
    "isBuyable": True,
    "updatedAt": None,
    "authors": [{"id": None, "asin": "B000TEST01", "name": "Test Author", "region": "us", "regions": ["us"], "image": None, "updatedAt": None}],
    "narrators": [{"name": "Test Narrator", "updatedAt": None}],
    "genres": [{"asin": None, "name": "Fiction", "type": "Genres", "betterType": "genre", "updatedAt": None}],
    "series": [],
}

MOCK_SERIES_FOR_HEADER_WALK = {
    "asin": "B00SERIES1",
    "name": "Dune Chronicles",
    "description": "The Dune Chronicles is a science fiction series.",
    "region": "us",
    "position": None,
    "updatedAt": None,
}

MOCK_AUTHOR_FOR_HEADER_WALK = {
    "id": None,
    "asin": "B000APF21M",
    "name": "Frank Herbert",
    "description": "Frank Herbert was an American science fiction author.",
    "image": "https://example.com/frank-herbert.jpg",
    "region": "us",
    "regions": ["us"],
    "genres": [],
    "updatedAt": "2024-01-01T00:00:00+00:00",
}
