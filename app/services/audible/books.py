"""
Audible books service.
Fetches book metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Audible is the source of truth, and every result normalized from it is
written to the relational DB for persistence.

One cohesive job lives here: turning a list of ASINs into books, by whatever
mix of Audible, the relational DB, and the cache it takes to answer without
handing back less than a caller could have gotten a moment ago. Fetching,
normalizing Audible's raw product shape into AudiMeta's exact DTO, and
falling back through DB and cache are not three jobs in a trenchcoat -- the
fallback ladder in get_books_by_asins hands back DB rows and cache hits
alongside freshly normalized Audible products in the very same list, and the
tri-state flag settling that every return path goes through has to treat
whichever of the three produced a given element identically. A seam drawn
between fetch and fallback would need each side holding the internals of the
shape the other one produces, which is a shared contract stretched across
two files rather than a boundary.

A second cluster shares the module without sharing that job:
_normalize_chapters, get_chapters, fetch_and_store_chapters, and
_mark_chapters_checked fetch a book's chapter listing from Audible's own
/1.0/content/{asin}/metadata endpoint, normalize it into TrackContentDto
rather than BookDto, and persist it to its own table (Track) under its own
cache key -- never touching get_books_by_asins' fallback ladder, its DB
backstop, its facts ledger, or the tri-state flag settling above. The one
real tie to the rest of the module is _mark_chapters_checked stamping
chapters_checked_at on the same Book row the ladder resolves, coordinating
with the standalone chapters backfill so neither re-checks what the other
already covered; past that one column, the cluster is its own job, sharing
an ASIN and a file with the ladder above rather than a boundary. The module
stays long because it holds one long job and one short one that happens to
touch the same row.
"""

# Standard library
import asyncio
import json
import math
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

# Third party
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.models import Book

# Core
from libex_core.asin import is_valid_asin
from libex_core.audible.client import (
    as_audible_failure,
    author_books_concurrency,
    upstream_status_of,
    REGION_MAP,
)
from libex_core.exceptions import NotFoundException
from libex_core.text import strip_html, strip_image_size_suffix
from app.core.logging import get_logger, is_safe_log_value
from app.core.response_headers import (
    REASON_HYDRATION_DEADLINE,
    REASON_HYDRATION_FAILED,
    REASON_HYDRATION_NOT_FOUND,
    ResponseFacts,
    SOURCE_AUDIBLE,
    SOURCE_CACHE,
    SOURCE_DB,
    record_incomplete,
    record_source,
    record_source_keys,
)

# Services
from app.services.audible import audible_get
from app.services.cache import manager as cache
from app.services.cache.manager import book_key, chapters_key
from app.services.db.persist_queue import (
    PersistOutcome,
    persist_books_background,
    persist_track_background,
)
from app.services.db.writer import upsert_track
from app.services.db.reader import get_books_from_db, get_track_from_db

logger = get_logger()

# ============================================================
# CONSTANTS
# ============================================================

BOOK_RESPONSE_GROUPS = (
    "media, product_attrs, product_desc, product_details, "
    "product_extended_attrs, product_plans, rating, series, "
    "relationships, review_attrs, category_ladders, customer_rights"
)

IMAGE_SIZES = "500,1000,2400,3200"

# The publication_datetime Audible puts on a placeholder catalogue record --
# an entry that stands in for a title rather than being one, which Audible
# does send. See _filter_products for the two records measured carrying it,
# and for why one is dropped rather than served.
UNRELEASED_PLACEHOLDER = "2200-01-01T00:00:00Z"

# Below this many products, normalization runs inline on the event loop; at
# or above it, the whole batch is handed to a single asyncio.to_thread call.
#
# What one product costs is governed by how big that product is, so a single
# figure does not characterise it and the sizes are named alongside every
# number here. _build_extras walks every node of the product and then renders
# the whole blob to JSON, and that walk and dump is 50-80% of the total cost
# of normalizing a product -- work that scales with the product rather than
# with the count of fields the DTO names.
#
# Measured (scratchpad benchmark, not part of this repo; 300 iterations per
# size, median of seven runs, on a development machine): a 3.5 KB product
# normalizes in ~0.4ms, 6.5 KB in ~0.55ms, 17 KB in ~1.2ms and 45 KB in ~3ms,
# while the to_thread hop itself costs ~0.2ms of fixed overhead regardless of
# batch size. Read the ratios and the size dependence rather than the absolute
# numbers, which a production worker's hardware will move.
#
# A 50-ASIN chunk -- the largest a single Audible catalog request ever
# returns, see the chunking below -- is therefore ~30ms of uninterruptible
# loop time for ordinary books and ~150ms for a page of large multipart
# titles, and that is what a caller under this threshold pays inline.
#
# What decides the side is the whole fetch list, not the chunk: every chunk's
# products are accumulated and handed to _normalize_products in one call, so
# anything whose ASIN list runs past this count offloads. A single ASIN, a
# search page and a short series stay inline; a bulk /books request (the route
# admits 1000 ASINs), a long series, and above all get_author_books' hydration
# cross it. The flagged ~1530-product case blocks the loop for ~1s inline at
# 6.5 KB a product, with nothing else able to run in that window, against the
# same ~1s threaded but with the loop free to service other requests
# throughout. The thread hop is a net wall-clock loss at every size tested;
# the point is solely to stop a single request from stalling every other
# connection the process is holding.
NORMALIZE_THREAD_THRESHOLD = 100

# The upstream product keys _normalize_product already reproduces as
# first-class response fields. Every other top-level key Audible sends goes
# into audibleExtras verbatim, so this set is the whole of what decides
# which side of that line a key falls on.
#
# Membership is checked in both directions, because the two directions fail
# differently and only one of them is cheap. Each first-class field that
# reproduces an upstream key reads it through _reproduce, which refuses a key
# not named here, so adding a field without adding its key raises on the
# first product normalized -- in every test and every request alike -- and
# costs a duplicated value until it does. The reverse, a key named here that
# no field reads, is a silent drop rather than a duplicate: withheld from the
# blob by this set and reproduced nowhere. _verify_reproduced_keys_read
# establishes at import that every key below is genuinely read, so neither
# direction can drift unnoticed.
#
# merchandising_summary and publisher_summary are members even though
# strip_html runs over them. HTML-stripping is not a transformation of the
# content, only of its markup, and the two copies were measured at 26% of the
# raw product as pure duplication.
#
# Deliberately NOT members, and therefore passed through whole alongside the
# field parsed out of them: rating, product_images, plans, category_ladders,
# relationships, release_date, episode_number, episode_type,
# publication_datetime, authors, narrators. Each is transformed or only
# partly consumed -- imageUrl is one URL out of a dict of sizes, genres
# flattens category_ladders, series reads the series entries out of
# relationships and leaves the rest, publicationDatetime surfaces
# publication_datetime unchanged while _filter_products reads it for its own
# purposes -- so the upstream key still goes into the blob as sent, whole
# object or bare scalar alike. That
# list is documentation and nothing reads it as a constant; a second
# frozenset the code never consults would drift from the normalizer within a
# release. A field read with a plain product.get rather than _reproduce is
# duplicated into the blob, never dropped, which is the direction a mistake
# here has to fail in.
_REPRODUCED_KEYS: frozenset[str] = frozenset({
    "asin",
    "title",
    "subtitle",
    "publisher_name",
    "copyright",
    "isbn",
    "language",
    "format_type",
    "is_adult_product",
    "is_pdf_url_available",
    "read_along_support",
    "runtime_length_min",
    "content_type",
    "content_delivery_type",
    "sku",
    "sku_lite",
    "is_listenable",
    "is_buyable",
    "is_vvab",
    "merchandising_summary",
    "publisher_summary",
    "publication_name",
    "product_state",
    "extended_product_description",
})

# relationship_type values stripped out of the blob's relationships array.
# A podcast show carries one child entry per episode, which is the whole of
# why this exists: measured live in us, B08JJND27B is a 448 KB product of
# which 440 KB is 4,412 episode entries and a single season entry. All eight
# podcasts in that sample carried both types and ran from 60 KB to 448 KB on
# the same shape. Stripped, that 448 KB product leaves a 5 KB blob, and the
# largest blob anywhere in the nineteen-product sample is 8 KB.
#
# The strip is unconditional rather than podcast-gated. Nothing else in the
# catalogue was observed carrying either type -- ordinary books relate to
# series and components -- so gating it on content_type would add a branch
# that changes no outcome while leaving a second, easily-missed way for
# these entries to arrive.
_STRIPPED_RELATIONSHIP_TYPES = frozenset({"episode", "season"})

# Caps on the blob, both deliberately far above anything observed so they
# can only fire on something pathological. 64 KB is eight times the largest
# blob in the sample above, measured the same way this cap measures; 32
# levels of nesting is more than five times the deepest product in it, which
# reached six.
#
# Exceeding either drops the blob whole. Pruning the largest keys instead
# would hand back something that looks complete and is not, which is the
# silent loss this whole mechanism exists to stop -- a caller can see an
# absent blob and an extrasWithheld entry saying why, and cannot see a key
# that was quietly removed from a blob that still arrived.
_EXTRAS_MAX_BYTES = 64 * 1024
_EXTRAS_MAX_DEPTH = 32

# The widest int that survives the whole path from this module to a jsonb
# column, in bits.
#
# There are two ceilings and Python's is by far the lower. Postgres jsonb
# stores every number as numeric, which holds 131,072 digits; CPython
# refuses outright to render an int wider than sys.get_int_max_str_digits(),
# 4,300 by default since 3.11, and json.dumps renders every int it is given.
# So an int of 5,000 digits is perfectly storable and still unprintable, and
# the bound that matters is Python's.
#
# Counted in bits rather than digits because counting digits means calling
# str(), which is the exact call that raises on the values this exists to
# catch -- the precise check would blow up on precisely its own subject
# matter, out of the normalizer and down the caller's DB ladder. 2**14283 is
# below 10**4300, so anything at or under this bit length always renders.
# An operator who lowers the limit below the default is still covered: the
# dump below is wrapped, and a raise there is recorded rather than thrown.
_INT_BITS_ALWAYS_RENDERABLE = 14283

# Why a blob, or part of one, did not survive. These are the values that
# reach the caller in extrasWithheld, so they are part of the response and
# not just log vocabulary.
_WITHHELD_SANITIZED = "sanitized"
_WITHHELD_DEPTH = "depth"
_WITHHELD_SIZE = "size"
_WITHHELD_UNSERIALIZABLE = "unserializable"


# ============================================================
# HELPERS
# ============================================================

def _has_uncovered(asins: list[str], covered: set[str]) -> bool:
    """
    True when at least one of `asins` is absent from `covered`.

    The one predicate every hydration-incomplete site in
    _get_books_by_asins_unsettled shares: a loss class earns its incomplete
    reason only when it actually owns an ASIN missing from what the function
    is about to hand back, never merely because that class experienced a
    loss internally. `covered` is always the ASIN set already present in the
    result about to be returned (a DB backstop, a DB fallback, a cache
    fallback) -- never a count, since two same-sized sets can still miss
    each other entirely.
    """
    return any(asin not in covered for asin in asins)


def _window_elapsed(last_logged: float | None, now: float, interval: float) -> bool:
    """
    True when a windowed incident report is due: nothing reported yet, or the
    last report is at least `interval` seconds old.

    Shared by the two repeat-incident warnings below -- unreadable plans, and
    anything withheld from an extras blob -- which cap themselves the way
    persist_queue.py's incident logs cap theirs, and for the same reason:
    either can fire on every product in a page at once when what changed is
    upstream and systematic rather than one product being odd, and a line per
    product buries the only thing worth reading, which is that it happened
    and how often. The two keep their own state and their own fields, since
    one windows per reason and the other globally, and only this predicate is
    genuinely the same rule twice.

    None rather than 0.0 for "never reported", and the distinction is not
    cosmetic: time.monotonic() counts from boot, so against a 0.0 sentinel
    this arithmetic reads "never reported" as "reported at boot" and stays
    false for the first interval of a process's life -- swallowing the very
    first report. That is the worst window in which to lose either warning:
    the plans one exists to catch an upstream rename before the plans column
    quietly empties across the corpus, and six worker processes all start
    fresh on every deploy. persist_queue's own _window_elapsed carries the
    same guard for the same reason.
    """
    if last_logged is None:
        return True
    return now - last_logged >= interval


def _best_image(product_images: dict | None) -> str | None:
    """Returns the highest resolution image URL with size suffix stripped."""
    if not product_images:
        return None
    highest_key = max((int(k) for k in product_images if k.isdigit()), default=None)
    if highest_key is None:
        return None
    url = product_images.get(str(highest_key))
    return strip_image_size_suffix(url)


def _audible_link(asin: str, region: str) -> str:
    """Builds an Audible product page link."""
    tld = REGION_MAP.get(region, ".com")
    return f"https://audible{tld}/pd/{asin}"

def _parse_release_date(raw: str | None) -> str | None:
    """
    Converts a raw Audible release date string to ISO 8601 format.
    Audimeta stores dates as DateTime and outputs .toISO(), e.g. "2021-03-02T00:00:00.000+00:00".
    """
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return raw


def _parse_authors(product: dict, region: str) -> list[dict]:
    """
    Extracts author objects matching AudiMeta's MinimalAuthorDto.

    An author entry's asin is checked for ASIN shape -- ten characters of
    A-Z0-9, the same test every author route applies before it queries --
    and then written through unchanged whether it passes or not. The check
    only reports. That is deliberate, and the reason is the pivot rather
    than the predicate.

    Audible's contributor entries are not always identifiers. Measured live
    2026-09-06 against /1.0/catalog/products, the authors array can carry
    the contributor's own name ({"asin": "Trinka Enell", "name": "Trinka
    Enell"}, us B0DKQBH3CR), a single stray character ({"asin": "v"}, jp
    B0H6ZCMBW5), or a fragment of a twice percent-encoded surname
    ({"asin": "25A7anha", "name": "Vitor Peçanha"} -- the tail of
    "Pe%25C3%25A7anha" -- us B09W33RNX7 and br B0CC8M5KXV). Nothing further
    down validates either, not upsert_author and not
    upsert_author_profile, so a value like one of those lands in
    authors.asin verbatim and becomes somebody's identifier.

    Nulling one now would cost more than it recovers. A book whose author
    row already holds a junk asin is matched on that asin, so writing null
    instead -- or uppercasing a lowercased real ASIN, which is the same
    move -- resolves to a second row for the same person rather than the
    one already there. author_book links are inserted on conflict do
    nothing and are never removed, so the book keeps its old link and gains
    a new one, and the search route joins author names without dedup: the
    same contributor renders twice, permanently, in a state AudiMeta itself
    cannot reach, because it replaces a book's link set on every write
    where Libex only ever adds to it.

    The warning is therefore the whole product of this pass. The affected
    population cannot be counted from the table -- junk that is already ten
    uppercase characters is indistinguishable from a real ASIN in a query,
    and only Audible sending it again reveals it -- so logging every
    rejected value with the product, the contributor name and the region is
    the only way to size it. Enforcement waits on that number and on a plan
    for the rows already written; adding it here without them trades a rare
    bad asin for a permanent duplicate author on an unknown number of
    books.

    The >12-character value that still gets nulled below predates all of
    this: authors.asin is a String(12) and the ceiling keeps an over-long
    value from failing the insert. It is malformed by the same measure, so
    it is logged too.
    """
    authors = []
    for author in product.get("authors", []):
        name = author.get("name", "").replace("\t", "").strip()
        asin = author.get("asin", "").replace("\t", "").strip() if author.get("asin") else None
        if asin and not is_valid_asin(asin):
            logger.warning("Audible sent a malformed author ASIN", extra={
                "asin": product.get("asin", ""),
                "malformed_author_asin": asin,
                "author_name": name,
                "region": region,
            })
        if asin and len(asin) > 12:
            asin = None
        if name:
            authors.append({
                "id": None,
                "asin": asin,
                "name": name,
                "region": region,
                "regions": [region],
                "image": None,
                "updatedAt": None,
            })
    return authors


def _parse_narrators(product: dict) -> list[dict]:
    """Extracts narrator objects matching AudiMeta's NarratorDto."""
    return [
        {"name": n.get("name", "").strip(), "updatedAt": None}
        for n in product.get("narrators", [])
        if n.get("name")
    ]


def _parse_genres(product: dict) -> list[dict]:
    """Extracts genre objects matching AudiMeta's GenreDto with type and betterType."""
    genres = []
    seen = set()
    for ladder in product.get("category_ladders", []):
        for rung_index, rung in enumerate(ladder.get("ladder", [])):
            name = rung.get("name")
            asin = rung.get("id")
            if name and name not in seen:
                seen.add(name)
                genre_type = "Genres" if rung_index == 0 else "Tags"
                genres.append({
                    "asin": asin,
                    "name": name,
                    "type": genre_type,
                    "betterType": genre_type.lower().rstrip("s"),
                    "updatedAt": None,
                })
    return genres


# How often the "plans entries present but unreadable" warning below actually
# logs, once it starts firing repeatedly. An upstream rename of plan_name is
# exactly the scenario that trips this on every product in a page, or a whole
# search response, at once -- a line per product is the flood that would bury
# the one signal worth keeping: that it happened at all, and how much.
_UNREADABLE_PLANS_LOG_INTERVAL_SECONDS = 60

_unreadable_plans_count = 0
_unreadable_plans_last_logged: float | None = None


def _log_unreadable_plans(asin: str) -> None:
    """
    Reports "Audible sent plans entries this parser can't read" at most once
    per _UNREADABLE_PLANS_LOG_INTERVAL_SECONDS, through the shared
    _window_elapsed gate above -- this can fire on every product at once if
    plan_name itself is what changed shape, and an uncapped line per product
    would flood out the report of the one incident causing all of them. asin
    is the most recently affected product in the window, not every one of
    them -- enough to start looking without paying for a line per
    occurrence.
    """
    global _unreadable_plans_count, _unreadable_plans_last_logged
    _unreadable_plans_count += 1
    now = time.monotonic()
    if not _window_elapsed(
        _unreadable_plans_last_logged, now, _UNREADABLE_PLANS_LOG_INTERVAL_SECONDS
    ):
        return
    logger.warning("Audible plans entries present but unreadable", extra={
        "asin": asin,
        "occurrences": _unreadable_plans_count,
    })
    _unreadable_plans_count = 0
    _unreadable_plans_last_logged = now


def _parse_plans(product: dict) -> list[str] | None:
    """
    Extracts plan_names from the plans array.

    None when the response carries no `plans` key at all; [] when it carries
    an explicitly empty one. The two reach the writer differently -- None
    coalesces onto whatever is already stored, [] replaces it (see writer.py's
    JSONB(none_as_null=True) book upsert) -- so this response field can now
    surface null where it previously always surfaced a list.

    A third case folds into the None side rather than the [] side: entries
    are present but not one of them yields a readable plan_name (the key
    renamed, an entry carrying only plan_id, a plan_name that is itself
    null). plan_name appears nowhere else in the codebase but this line, so
    an upstream shape change here is invisible until it's read back --
    and unlike an explicitly empty array, which is Audible asserting "there
    are no plans," this is Libex failing to read whatever Audible actually
    sent. Treating it as silence (None: coalesce, leave whatever's stored
    alone) rather than as an assertion ([]: overwrite) is what stops a
    rename from silently emptying the plans column across the whole corpus
    the next time every stored book gets re-read. Logged, because otherwise
    nobody would find out until the column was already empty.

    A partial miss -- some entries readable, some not -- does NOT take this
    branch: it returns whatever it could read, same as always, because a
    partial answer is a real answer, not a total parse failure.
    """
    raw = product.get("plans")
    if raw is None:
        return None
    names = [p["plan_name"] for p in raw if p.get("plan_name")]
    if raw and not names:
        _log_unreadable_plans(product.get("asin", ""))
        return None
    return names


def _parse_series(product: dict, region: str) -> list[dict]:
    """Extracts series objects matching AudiMeta's MinimalSeriesDto."""
    series_list = []
    for item in product.get("relationships", []):
        if item.get("relationship_type") == "series":
            series_list.append({
                "asin": item.get("asin"),
                "name": item.get("title"),
                "position": item.get("sequence"),
                "region": region,
                "updatedAt": None,
            })
    return series_list


# How often the extras-withheld warning below actually logs for any one
# reason, once that reason starts firing repeatedly. The same windowing
# _log_unreadable_plans applies, for the same reason: every one of these can
# fire on every product in a page at once if what changed is upstream and
# systematic -- a shape Audible started sending, not one product being odd --
# and a line per product would bury the only thing worth reading, which is
# that it happened at all and how much.
_EXTRAS_LOG_INTERVAL_SECONDS = 60

_extras_incident_counts: dict[str, int] = {}
_extras_incident_last_logged: dict[str, float] = {}


def _safe_asin_for_log(asin: str) -> str:
    """
    Returns an ASIN as-is for logging if it is safe, else the sentinel.

    Reuses is_safe_log_value rather than growing a second rule, the same way
    the cache layer's own _safe_key_for_log does. An ASIN reaching here came
    off an Audible product rather than out of a validated route argument, and
    Audible's identifier fields are not reliably identifiers (see
    _parse_authors for values measured in that position), so the one place
    the value-safety judgment is made covers this too.
    """
    return asin if is_safe_log_value(asin) else "REDACTED"


def _log_extras_incident(asin: str, region: str, reason: str, blob_bytes: int | None = None) -> None:
    """
    Reports that something was withheld from a product's extras blob, at most
    once per _EXTRAS_LOG_INTERVAL_SECONDS per reason.

    Windowed per reason rather than globally, so a flood of one kind cannot
    silence the first occurrence of another -- which is why the state here is
    a dict keyed by reason where _log_unreadable_plans needs only a pair of
    scalars, and why the two share the _window_elapsed predicate rather than
    one recording function. asin names the one product that reopened the
    window, not every product the line speaks for; occurrences is how many
    the window that just closed covered, which is the number worth reading
    when this starts repeating.

    Nothing from the blob reaches this line. No upstream key name is used as
    a field name here either -- that would let Audible's response shape
    define Libex's log schema, so a change upstream would silently rewrite
    what every downstream query has to match on. reason is one of the
    _WITHHELD_* constants above, all of them Libex's own vocabulary.
    """
    count = _extras_incident_counts.get(reason, 0) + 1
    _extras_incident_counts[reason] = count
    now = time.monotonic()
    last = _extras_incident_last_logged.get(reason)
    if not _window_elapsed(last, now, _EXTRAS_LOG_INTERVAL_SECONDS):
        return
    logger.warning("Audible extras withheld", extra={
        "asin": _safe_asin_for_log(asin),
        "region": region,
        "withheld_reason": reason,
        "occurrences": count,
        "blob_bytes": blob_bytes,
    })
    _extras_incident_counts[reason] = 0
    _extras_incident_last_logged[reason] = now


# The keys _reproduce has been asked for, while _verify_reproduced_keys_read
# is normalizing its one probe product, and None at every other moment --
# including the whole of normal service, where the check below costs a single
# is-None comparison per read.
#
# Recorded here rather than by watching product.get, because the two do not
# mean the same thing. A key read with a plain product.get is transformed or
# only partly consumed and therefore must stay OUT of _REPRODUCED_KEYS, so a
# probe that counted those reads as coverage would wave through exactly the
# mistake it exists to catch: relationships added to the set, its series
# entries parsed out, and every other entry in it gone from the blob with
# nothing raising.
_reproduced_keys_read: set[str] | None = None


def _reproduce(product: dict, key: str) -> Any:
    """
    Reads an upstream key that _normalize_product reproduces as a first-class
    response field, refusing any key that is not in _REPRODUCED_KEYS.

    This is what turns that set from a convention into a requirement. A
    first-class field added to the normalizer without its upstream key added
    to the set raises here on the first product normalized -- every test and
    every request hits this path -- so the pair cannot fall out of step
    quietly and leave the same value appearing twice in the response.

    That is the cheap direction. The expensive one -- a key the set names
    that no field reads, which is excluded from the blob and reproduced
    nowhere -- is caught by _verify_reproduced_keys_read, which watches this
    function to establish it.

    Not used for a field that is transformed or only partly consumed; those
    are read with a plain product.get and appear in the blob as well, which
    is why reaching for the wrong one of the two costs a duplicated value
    rather than a lost one.
    """
    if key not in _REPRODUCED_KEYS:
        raise RuntimeError(
            f"{key} is read as a first-class response field but is missing from _REPRODUCED_KEYS"
        )
    if _reproduced_keys_read is not None:
        _reproduced_keys_read.add(key)
    return product.get(key)


def _strip_podcast_relationships(relationships: list) -> tuple[list, dict[str, int]]:
    """
    Removes the relationship entries named in _STRIPPED_RELATIONSHIP_TYPES,
    returning what is kept and a count per type of what was not.

    The counts are the whole point of returning them: they go into
    extrasWithheld, so the caller is told an episode list existed and how
    long it was. Without that the strip would be exactly the silent drop this
    blob was built to end, hidden inside the mechanism meant to prevent it.
    """
    kept = []
    stripped: dict[str, int] = {}
    for entry in relationships:
        relationship_type = entry.get("relationship_type") if isinstance(entry, dict) else None
        if relationship_type in _STRIPPED_RELATIONSHIP_TYPES:
            stripped[relationship_type] = stripped.get(relationship_type, 0) + 1
        else:
            kept.append(entry)
    return kept, stripped


def _is_unstorable_int(value: int) -> bool:
    """
    True when an int is too wide to render and store -- see
    _INT_BITS_ALWAYS_RENDERABLE for the two ceilings and why this is measured
    in bits.

    Conservative by design. A value just past the bound may well have been
    storable, and is withheld anyway rather than resolved exactly; that band
    begins at a 4,300-digit number, and a withholding that gets recorded is
    the better error than a write the database or the encoder refuses.
    """
    return value.bit_length() > _INT_BITS_ALWAYS_RENDERABLE


def _sanitize_for_jsonb(extras: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """
    Returns a copy of the blob that Postgres jsonb will actually accept, plus
    a count of each thing that had to be changed. Returns None for the copy
    when the blob is nested deeper than _EXTRAS_MAX_DEPTH.

    Three values Python's json parses and re-emits happily that jsonb
    refuses, all of which would otherwise turn one odd product into a failed
    write: U+0000 anywhere in a key or a value, a non-finite float (1e999
    parses to inf, and json.dumps writes it back as the bare token Infinity,
    which is not valid JSON at all), and an int too wide to store or even to
    render (see _is_unstorable_int). A NUL is stripped out of the string
    rather than costing the key or the product -- losing a whole book
    permanently over one invisible byte is the worse of the two outcomes --
    and a number that cannot be stored becomes null. Every one of those is
    counted, and the counts reach the caller in extrasWithheld, because a
    sanitization nothing records is itself a silent drop.

    This lives here rather than in the writer because the writer does not
    cover every surface the normalized dict reaches: cache.manager stores
    this same dict into a jsonb column of its own, so a writer-side check
    would leave the cache holding a value the database had already rejected
    and the two surfaces answering differently for the same book.

    Pure, and iterative on an explicit stack. Pure because _normalize_products
    hands whole batches to a worker thread past NORMALIZE_THREAD_THRESHOLD;
    iterative because a recursive walk over deeply nested input is itself the
    stack overflow the depth cap exists to prevent, so a recursive
    implementation of this check would be the bug it is checking for.
    """
    counts = {"nulCharacters": 0, "nonFiniteNumbers": 0, "oversizedNumbers": 0}
    root: dict[str, Any] = {}
    # (source container, the copy being built from it, that copy's depth,
    #  counting the blob itself as 1)
    stack: list[tuple[Any, Any, int]] = [(extras, root, 1)]

    while stack:
        source, target, depth = stack.pop()
        pairs = source.items() if isinstance(source, dict) else enumerate(source)
        for key, value in pairs:
            if isinstance(key, str) and "\x00" in key:
                counts["nulCharacters"] += key.count("\x00")
                key = key.replace("\x00", "")

            if isinstance(value, (dict, list)):
                if depth + 1 > _EXTRAS_MAX_DEPTH:
                    return None, counts
                child: Any = {} if isinstance(value, dict) else []
                stack.append((value, child, depth + 1))
            elif isinstance(value, str) and "\x00" in value:
                counts["nulCharacters"] += value.count("\x00")
                child = value.replace("\x00", "")
            elif isinstance(value, float) and not math.isfinite(value):
                counts["nonFiniteNumbers"] += 1
                child = None
            elif isinstance(value, int) and not isinstance(value, bool) and _is_unstorable_int(value):
                # The bool exclusion is not decorative: bool subclasses int,
                # so without it True is measured as a number.
                counts["oversizedNumbers"] += 1
                child = None
            else:
                child = value

            # A list is rebuilt by appending, which holds its order because
            # the whole source container is walked in one pass here and only
            # its children are deferred to the stack.
            if isinstance(target, list):
                target.append(child)
            else:
                target[key] = child

    return root, counts


def _build_extras(product: dict, asin: str, region: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """
    Builds a product's audibleExtras blob and the record of anything withheld
    from it.

    Everything Audible sent at the top level that _REPRODUCED_KEYS does not
    name goes in verbatim. That is the point: the key Audible invents next
    month surfaces on its own, rather than vanishing between the fetch and
    the response with nobody in a position to notice it was ever there. Two
    of the keys that ride along today, social_media_images and
    relationships[].url, are URLs; they are data, and nothing anywhere
    fetches them.

    Returns (None, record) when the blob is dropped whole -- None rather than
    an empty dict, so the writer's merge can tell "Libex has nothing to say"
    from "Audible sent nothing extra", the same tri-state _parse_plans keeps
    for its own field. An empty record means nothing was withheld, and the
    caller omits extrasWithheld entirely in that case.
    """
    extras = {k: v for k, v in product.items() if k not in _REPRODUCED_KEYS}
    withheld: dict[str, Any] = {}

    relationships = extras.get("relationships")
    if isinstance(relationships, list):
        kept, stripped = _strip_podcast_relationships(relationships)
        if stripped:
            extras["relationships"] = kept
            withheld["relationships"] = stripped

    sanitized, counts = _sanitize_for_jsonb(extras)
    hits = {name: total for name, total in counts.items() if total}
    if hits:
        withheld[_WITHHELD_SANITIZED] = hits
        _log_extras_incident(asin, region, _WITHHELD_SANITIZED)

    if sanitized is None:
        withheld["audibleExtras"] = _WITHHELD_DEPTH
        _log_extras_incident(asin, region, _WITHHELD_DEPTH)
        return None, withheld

    try:
        # allow_nan=False so anything non-finite that somehow survived above
        # raises here and is recorded, instead of being written out as the
        # Infinity token and becoming invalid JSON nothing would catch until
        # a reader choked on it.
        encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        withheld["audibleExtras"] = _WITHHELD_UNSERIALIZABLE
        _log_extras_incident(asin, region, _WITHHELD_UNSERIALIZABLE)
        return None, withheld

    blob_bytes = len(encoded.encode("utf-8"))
    if blob_bytes > _EXTRAS_MAX_BYTES:
        withheld["audibleExtras"] = _WITHHELD_SIZE
        _log_extras_incident(asin, region, _WITHHELD_SIZE, blob_bytes=blob_bytes)
        return None, withheld

    return sanitized, withheld


def _normalize_product(product: dict, region: str) -> dict[str, Any]:
    """
    Normalizes a raw Audible product into Libex response format.
    Field names match AudiMeta's BookDto exactly for drop-in compatibility,
    and everything past that DTO is additive.

    audibleExtras carries every top-level key Audible sent that the fields
    above it do not already reproduce, verbatim (see _REPRODUCED_KEYS and
    _build_extras). Two things about it never relax. Nothing out of it is
    ever hoisted into this dict -- no splat, no key added at runtime -- which
    is what makes an upstream key called "asin" or "__proto__" structurally
    unable to collide with a first-class field: it stays nested, and the
    caller reads it there. And nothing in it is ever fetched, not to validate
    it, not to check an image is still there, not from a script; it carries
    product_images, social_media_images and relationships[].url, and those
    are data, not inputs to a request.

    extrasWithheld is a top-level key here, deliberately not one inside
    audibleExtras, and is absent altogether from THIS dict when nothing was
    withheld -- the response model carries it to the wire as null either way,
    so a book object always has the key. Inside the blob it would be Libex's
    own invention sitting in a namespace documented as verbatim Audible, and
    it would collide with a real upstream key of that name the day Audible
    ships one.

    audibleExtras is None rather than {} when the blob was dropped whole: {}
    would assert Audible sent nothing extra, where None says Libex has
    nothing to offer and lets the writer's merge leave a stored blob alone.
    Same tri-state and same reasoning as plans (see _parse_plans), though
    the two are not merged the same way at the other end.
    """
    asin = _reproduce(product, "asin") or ""
    series_list = _parse_series(product, region)

    content_type = _reproduce(product, "content_type")
    is_podcast = content_type and content_type.lower() == "podcast"

    extras, withheld = _build_extras(product, asin, region)

    book: dict[str, Any] = {
        "asin": asin,
        "title": _reproduce(product, "title"),
        "subtitle": _reproduce(product, "subtitle"),
        "description": strip_html(_reproduce(product, "merchandising_summary")),
        "summary": strip_html(_reproduce(product, "publisher_summary")),
        "region": region,
        "regions": [region],
        "publisher": _reproduce(product, "publisher_name"),
        "copyright": _reproduce(product, "copyright"),
        "isbn": _reproduce(product, "isbn"),
        "language": _reproduce(product, "language"),
        # All three rating reads walk the same unguarded chain, and the two
        # halves of it are at different depths: num_ratings sits inside
        # overall_distribution beside average_rating, num_reviews sits
        # directly on rating. Both confirmed against live products rather
        # than taken from the fixtures, which carry neither -- us B08G9PRS1K
        # returns rating.overall_distribution.num_ratings 312,915 and
        # rating.num_reviews 48,002.
        #
        # A rating key present and explicitly null raises AttributeError out
        # of this chain, and that raise is the less-data guard doing its job:
        # it falls into the caller's broad except and down to the DB ladder,
        # which still holds the numbers. A defensive .get on each step would
        # turn a shrinkage signal into three silent Nones written over real
        # stored values.
        "rating": product.get("rating", {}).get("overall_distribution", {}).get("average_rating"),
        "numRatings": product.get("rating", {}).get("overall_distribution", {}).get("num_ratings"),
        "numReviews": product.get("rating", {}).get("num_reviews"),
        "bookFormat": _reproduce(product, "format_type"),
        "releaseDate": _parse_release_date(product.get("release_date")),
        # Tri-state like isListenable/isBuyable/isVvab below -- see the
        # comment there for the full contract; a hard False here on a
        # missing key would be Libex asserting an answer Audible never
        # gave, and the writer would take it unguarded and overwrite a
        # stored True with a fetch that said nothing at all.
        #
        # is_adult_product and is_pdf_url_available are present on every
        # product Audible sends, so this guard is precautionary rather than
        # a fix for a hole seen in the wild. read_along_support is
        # genuinely absent for a real, common slice of the catalog --
        # podcasts and some anthology titles -- and its absence there reads
        # as "does not apply to this content" rather than Audible declining
        # to answer. The effect the writer needs to guard against is
        # identical either way: a response that omits the key is not a
        # negative assertion, which is why the flag is tri-state rather
        # than defaulted at normalization time.
        "explicit": _reproduce(product, "is_adult_product"),
        "hasPdf": _reproduce(product, "is_pdf_url_available"),
        "whisperSync": _reproduce(product, "read_along_support"),
        "imageUrl": _best_image(product.get("product_images", {})),
        "lengthMinutes": _reproduce(product, "runtime_length_min"),
        "link": _audible_link(asin, region),
        "contentType": content_type,
        "contentDeliveryType": _reproduce(product, "content_delivery_type"),
        "episodeNumber": str(product.get("episode_number")) if is_podcast and product.get("episode_number") else None,
        "episodeType": product.get("episode_type") if is_podcast else None,
        "sku": _reproduce(product, "sku"),
        "skuGroup": _reproduce(product, "sku_lite"),
        # No default: True/False here would be Libex asserting an answer
        # Audible never gave. None means "Audible said nothing" and is what
        # lets the writer's tri-state merge (see _asserted_bool in writer.py)
        # tell that apart from an explicit false and leave a stored value
        # alone. isAvailable and isBuyable are both is_buyable -- AudiMeta's
        # own DTO derives them the same way (see _settle_flags below for
        # where None stops being a valid outward value).
        "isListenable": _reproduce(product, "is_listenable"),
        "isAvailable": _reproduce(product, "is_buyable"),
        "isBuyable": _reproduce(product, "is_buyable"),
        "isVvab": _reproduce(product, "is_vvab"),
        "plans": _parse_plans(product),
        "updatedAt": None,
        "authors": _parse_authors(product, region),
        "narrators": _parse_narrators(product),
        "genres": _parse_genres(product),
        "series": series_list,
        "publicationName": _reproduce(product, "publication_name"),
        # Read straight through rather than parsed. Audible already sends an
        # ISO-8601 instant here ("2026-03-02T00:00:00Z"), unlike release_date,
        # which is a bare date _parse_release_date has to give a timezone to.
        #
        # This one is both a field and a blob key on purpose: _filter_products
        # reads publication_datetime for its own decision, which makes it
        # partly consumed rather than reproduced, so it is not in
        # _REPRODUCED_KEYS and appears in audibleExtras as well.
        "publicationDatetime": product.get("publication_datetime"),
        # Stored exactly as Audible sent it, markup and all. strip_html
        # replaces a tag with nothing, so a paragraph break becomes no break
        # at all and "...end.</p><p>Next..." reads as "...end.Next..." --
        # paragraph structure destroyed in the one field whose whole job is
        # the long description. This field is excluded from the blob, so this
        # is the only copy of it there is.
        "extendedProductDescription": _reproduce(product, "extended_product_description"),
        "productState": _reproduce(product, "product_state"),
        "audibleExtras": extras,
    }
    if withheld:
        book["extrasWithheld"] = withheld
    return book


# The region the probe below normalizes under. Deliberately not one of the
# eleven: the probe's output is discarded, region never decides which keys
# _normalize_product reads, and a real region sitting here would read as a
# default that some later caller could inherit.
_PROBE_REGION = "probe"


def _verify_reproduced_keys_read() -> None:
    """
    Raises unless every key in _REPRODUCED_KEYS is actually read as a
    first-class response field, established by normalizing one probe product
    and recording which keys _normalize_product asked _reproduce for.

    _reproduce guards the other direction, and that is the cheap mistake: a
    field reading a key the set does not name leaves the value in the
    response twice. This is the expensive one. A key the set names that
    nothing reads is excluded from audibleExtras precisely because the set
    names it, and reproduced in no field because nothing asks for it, so it
    is dropped with nothing raising -- the silent loss the blob exists to end,
    happening inside the mechanism built to prevent it. It is one forgotten
    line away at all times: remove or rename a first-class field, leave its
    key in the set, and the field and its blob entry disappear together.

    Proved by running the real normalizer rather than by scanning this file
    for _reproduce calls. A source scan is only ever as good as the spellings
    it anticipates: the call shape it fails to recognise reads as an unused
    key and raises against a normalizer that is in fact correct, and a key
    genuinely read through a shape the scan does not model reads as covered
    when it is not. Running the thing removes the question -- nothing about
    how a call is written can fool it.

    Two limits, stated rather than papered over. It establishes that a key is
    read, not that the value reaches the response, so a read whose result was
    then discarded would still pass. And the probe product is empty, so a key
    reproduced only inside a branch an empty product does not take is
    reported as unread -- deliberately, because a conditionally reproduced
    key is dropped for every product that misses that branch, which is the
    same defect in a narrower window. An empty product is also the weakest
    input this normalizer will ever be handed, Audible omitting keys being
    ordinary; if normalizing one raises, that raise is a defect in its own
    right and is left to propagate rather than caught here.

    Runs at import, so the mismatch stops a process instead of waiting for a
    request: this module cannot be imported, in a test run or a worker start
    alike, while a key in the set is reproduced by nothing.
    """
    global _reproduced_keys_read
    _reproduced_keys_read = set()
    try:
        _normalize_product({}, _PROBE_REGION)
        read = _reproduced_keys_read
    finally:
        _reproduced_keys_read = None

    unread = sorted(_REPRODUCED_KEYS - read)
    if unread:
        raise RuntimeError(
            "_REPRODUCED_KEYS names upstream keys that no first-class response "
            "field reads, so each is withheld from audibleExtras and reproduced "
            f"nowhere: {', '.join(unread)}"
        )


_verify_reproduced_keys_read()


# Settled values for the seven tri-state flags above, matched to the
# columns' own DB defaults (app/db/models.py). Matching the two holds at
# insert time: a brand-new row and a live fetch settle to the same value,
# because there is nothing stored yet to disagree with. Once a row holds a
# real, asserted value and a later fetch is silent on that same key, the
# writer's shrinkage guard keeps the stored value (see _asserted_bool in
# writer.py) while the live and cached surfaces still settle to the fixed
# default here -- the two can disagree at that point, and that is the
# accepted trade: an asserted answer already sitting in the database
# outranks a default standing in for silence on the wire.
# isListenable/isAvailable/isBuyable settle to True, mirroring AudiMeta's
# own ingest for the three it defines that way. explicit/hasPdf/whisperSync
# settle to False, mirroring AudiMeta's own ingest the other way -- AudiMeta
# applies `?? false` to exactly these three -- which is also each column's
# own DB default (Boolean, nullable=False, default=False). isVvab -- which
# AudiMeta doesn't have -- settles to Libex's own prior emitted value rather
# than inventing a new one.
_FLAG_SETTLE_DEFAULTS: dict[str, bool] = {
    "isListenable": True,
    "isAvailable": True,
    "isBuyable": True,
    "isVvab": False,
    "explicit": False,
    "hasPdf": False,
    "whisperSync": False,
}


def _settle_flags(book: dict[str, Any]) -> dict[str, Any]:
    """
    Fills a None tri-state flag with its settled default, for anything that
    is about to leave the Audible service layer: filtering.py's equality
    match runs on these same dict keys, and every BookResponse field is a
    plain bool with no null variant, so None can reach neither. Persistence
    is the one consumer this must NOT run before -- the writer needs the
    None to tell "Audible said nothing" apart from an explicit false (see
    _normalize_product above) -- so every caller settles only the value it
    hands back to its own caller, never what it hands to
    persist_books_background.

    plans rides along in the same step for the same reason, even though it
    isn't a bool: _parse_plans is deliberately tri-state too (None for "no
    plans key at all" so the writer's coalesce leaves a stored array alone,
    vs. [] for "Audible said explicitly empty," which overwrites -- see
    _parse_plans), and BookResponse.plans is `list[str]` with no null
    variant, same as the seven flags below. A None here reaches neither
    filtering.py's equality match nor BookResponse otherwise: pydantic does
    not fall back to a field's default_factory on an explicit None, it
    raises, and every route this feeds fell over on exactly that until this
    settled it here rather than in _parse_plans.

    Returns a new dict; the input is never mutated, since some callers still
    need the unsettled version afterwards (see get_new_releases/
    get_coming_soon in releases.py, which persist the tri-state and cache the
    settled result under the same call).

    Only settles a key that is PRESENT and None -- never adds a key a dict
    never carried. _normalize_product always carries all seven flags plus
    plans, so nothing that went through it is affected by the distinction;
    what it protects is a dict from a source outside that contract (the DB
    backstop path's rows always carry real, already-non-null booleans and an
    already-listed plans -- see reader.py's `book.plans or []` -- so nothing
    there needs settling in the first place).
    """
    settled = dict(book)
    for key, default in _FLAG_SETTLE_DEFAULTS.items():
        if key in settled and settled[key] is None:
            settled[key] = default
    if "plans" in settled and settled["plans"] is None:
        settled["plans"] = []
    return settled


def _settle_flags_list(books: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Maps _settle_flags over a list. See that function for what and why."""
    return [_settle_flags(b) for b in books]


async def _normalize_products(products: list[dict], region: str) -> list[dict[str, Any]]:
    """
    Normalizes a batch of raw Audible products, offloading the whole batch
    to a worker thread when it's large enough to be worth the hop (see
    NORMALIZE_THREAD_THRESHOLD). One thread hop for the entire batch, never
    one per product -- a hop costs ~0.2ms whatever it carries, against
    ~0.4-3ms of work per product depending on that product's size, so paying
    it per product would add hundreds of milliseconds of pure overhead across
    the hundreds-to-low-thousands batches this exists for.

    _normalize_product touches only its own arguments and pure helpers
    (strip_html, strip_image_size_suffix, datetime parsing, the extras walk in
    _sanitize_for_jsonb) -- no DB session, no cache, and no read of any
    ContextVar, so running it on another thread carries no correctness risk.
    Two pieces of shared mutable state are in reach and neither changes that.
    The counters behind the windowed warnings (_log_unreadable_plans,
    _log_extras_incident) decide only how often a warning prints, never what
    any product normalizes to, so a lost increment across concurrent batches
    costs an off-by-a-few occurrence count and nothing else. The
    _reproduced_keys_read recorder every _reproduce call checks is written
    only by _verify_reproduced_keys_read, at import, long before any batch
    exists: every read from a worker thread sees the None it is left at, and
    no batch ever writes it. In particular
    it never reads the author_books_concurrency ContextVar in client.py,
    which wouldn't propagate into a to_thread worker the way a normal await
    does -- moot here since nothing in this path looks at it.

    Ordering matches the input list either way (a single list comprehension,
    run inline or inside one to_thread call), and a malformed product raises
    out of that comprehension exactly as it always has -- offloading doesn't
    change which products succeed or fail relative to each other, only which
    thread does the work.
    """
    if len(products) < NORMALIZE_THREAD_THRESHOLD:
        return [_normalize_product(p, region) for p in products]
    return await asyncio.to_thread(lambda: [_normalize_product(p, region) for p in products])


def _normalize_chapters(data: dict, asin: str) -> dict[str, Any]:
    """Normalizes raw Audible chapter data into AudiMeta's TrackContentDto format."""
    chapter_info = data.get("content_metadata", {}).get("chapter_info", {})
    raw_chapters = chapter_info.get("chapters", [])

    chapters = [
        {
            "lengthMs": c.get("length_ms", 0),
            "startOffsetMs": c.get("start_offset_ms", 0),
            "startOffsetSec": c.get("start_offset_sec", 0),
            "title": c.get("title", ""),
        }
        for c in raw_chapters
    ]

    return {
        "brandIntroDurationMs": chapter_info.get("brandIntroDurationMs", 0),
        "brandOutroDurationMs": chapter_info.get("brandOutroDurationMs", 0),
        "isAccurate": chapter_info.get("is_accurate", False),
        "runtimeLengthMs": chapter_info.get("runtime_length_ms", 0),
        "runtimeLengthSec": chapter_info.get("runtime_length_sec", 0),
        "chapters": chapters,
    }


def _filter_products(products: list[dict]) -> list[dict]:
    """
    Drops two disjoint categories of product from a raw Audible list:
    anything with no title, and anything whose publication_datetime is the
    UNRELEASED_PLACEHOLDER sentinel. Two callers -- _walk_one_catalog in
    releases.py, which serves both get_new_releases and get_coming_soon,
    and search.py's product search -- never hydrate afterwards, so what
    survives this filter is exactly what those responses ship; a name
    built around "unreleased" placeholders alone would undersell what
    /coming-soon and search results actually pass through.

    Also load-bearing for resolving a nonexistent-but-well-formed ASIN to
    "not found": probed live, Audible's catalog endpoints don't 404 for one
    of those in either _fetch_chunk branch -- they return 200 with a
    hollow, titleless stub instead (a bogus single ASIN and a bogus ASIN
    in a batch request both came back that way; a real ASIN came back
    fully populated). Dropping anything with no title here is what turns
    that stub into an empty result rather than a phantom book with every
    field blank.

    The UNRELEASED_PLACEHOLDER clause carries no comparable confirmation
    from when it was written. Measured since: 1,100 products across all
    eleven regions, two sorts each (-ReleaseDate and -Title, 50 per call --
    11 regions x 2 sorts x 50), plus a further 500 in us across ten calls
    spanning nine query shapes (10 calls x 50 per call) -- ascending and
    descending release-date sorts, the descending sort run over two pages
    (which is why the call count exceeds the shape count), three prolific
    author catalogues, a narrator catalogue, "preorder" and "coming soon"
    keyword searches, a title sort -- carried zero products with a
    publication_datetime matching the sentinel, and zero hollow titleless
    stubs of the kind above. Only the -ReleaseDate calls carry a
    sort-order argument, and only on an assumption this sample cannot
    verify -- every observed product had already passed the filter, so the
    sample is silent on what it removed -- that Audible's -ReleaseDate sort
    keys on the same publication_datetime field the clause reads. On that
    assumption, a product carrying the sentinel would have ranked first in
    every one of those calls, and none did. The -Title and
    catalogue/keyword calls carry no such guarantee; their zero count is a
    plain absence, nothing more. Real pre-orders pass through untouched
    with real dates in both publication_datetime and release_date --
    furthest observed: jp 2029-01-01, br 2028-05-02, us/uk/ca/au/fr
    2027-12-10, de 2027-05-11 (the remaining three regions matched one of
    these dates rather than adding a new one).

    The constant and this clause appear in 74a79a3, the project's earliest
    substantive commit -- the two commits before it are a bare license/
    readme and empty scaffolding -- whose own README carries the AudiMeta
    drop-in-replacement language, so where the clause came from is not
    recorded and the AudiMeta service that could confirm it is gone.

    What the clause catches is real, and asking Audible for one directly is
    what shows it. us B0182NWM9I and us B009CFOEGK each answer 200 carrying
    publication_datetime exactly 2200-01-01T00:00:00Z, a title borrowed from
    a real franchise ("Harry Potter", "The Lord of the Rings"), publisher_name
    "ZZZ - Series Advisor Placeholder", product_state
    NOT_AVAILABLE_FOR_PURCHASE, is_buyable and is_listenable both false, a
    zero runtime and no ISBN -- placeholder catalogue entries standing in for
    a series, not titles anyone can listen to. Dropping them is the right
    answer, so the clause stays and the records it removes stay removed.

    The 1,600-product sample above is not in tension with that. Every call in
    it went through a sort or a keyword search, and what those surface is the
    sellable catalogue; a zero count there says a placeholder does not show up
    in ordinary browse results, not that Audible has none to send. Both
    confirmations above came from asking for one ASIN by name, which is
    exactly the path a caller takes and the sample never did.
    """
    return [
        p for p in products
        if p.get("title")
        and p.get("publication_datetime") != UNRELEASED_PLACEHOLDER
    ]


# ============================================================
# CHUNKING
# ============================================================

async def _await_chunks(tasks, timeout, chunks, region) -> None:
    """Waits out the chunk fan-out, cancelling whatever is still in flight
    when the request's deadline arrives.

    Split from the caller so the try/finally that guarantees cancellation on
    an OUTER cancel stays one readable statement -- see that finally for why
    it has to exist at all.
    """
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        # Let the cancellations settle before anything reads a task, so
        # nothing is still running when results are assembled.
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning("Hydration deadline reached, chunks abandoned", extra={
            "abandoned_chunks": len(pending),
            "total_chunks": len(chunks),
            "region": region,
        })


class HydrationDeadlineExceeded(Exception):
    """One hydration chunk abandoned because the request ran out of time.

    Deliberately an Exception rather than letting CancelledError through:
    CancelledError is a BaseException in 3.12, so it would slip past the
    `isinstance(result, Exception)` branch below that routes a failed chunk to
    the DB backstop. Abandoned chunks must take that path -- their ASINs are
    still worth answering from stored rows -- and must also leave the response
    visibly short, which is what makes the route mark it incomplete.
    """


async def _fetch_chunk(asins: list[str], region: str) -> list[dict[str, Any]]:
    """Fetches a single chunk of up to 50 ASINs from Audible."""
    if not asins:
        return []

    if len(asins) == 1:
        path = f"/1.0/catalog/products/{asins[0]}"
        params: dict[str, Any] = {
            "response_groups": BOOK_RESPONSE_GROUPS,
            "image_sizes": IMAGE_SIZES,
        }
        data = await audible_get(region, path, params)
        products = [data.get("product", {})] if data.get("product") else []
    else:
        path = "/1.0/catalog/products"
        params = {
            "asins": ",".join(asins),
            "response_groups": BOOK_RESPONSE_GROUPS,
            "image_sizes": IMAGE_SIZES,
        }
        data = await audible_get(region, path, params)
        products = data.get("products", [])

    return _filter_products(products)


# ============================================================
# PUBLIC API
# ============================================================

async def get_books_by_asins(
    asins: list[str],
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
    high_concurrency: bool = False,
    deadline: float | None = None,
    *,
    facts: ResponseFacts | None = None,
    persist_outcome: list[PersistOutcome] | None = None,
) -> list[dict[str, Any]]:
    """
    Public entry point. Delegates to _get_books_by_asins_unsettled and settles
    the seven tri-state flags (see _settle_flags) on whatever it returns
    before handing it back.

    That inner function has several return points -- an early cache hit, the
    full-fetch success path, and two different failure fallbacks -- and every
    one of them can carry a None flag, whether freshly normalized or read back
    from a cache entry someone else wrote with the tri-state still in it.
    Settling once here, on the outside, covers all of them from a single place
    and can't be silently bypassed by a return path added inside later; the
    alternative was inserting the same call at each of those points.

    facts is threaded straight through, unexamined -- it is
    _get_books_by_asins_unsettled's own return points, not this wrapper's
    settling step, that know which source produced which element.

    persist_outcome is the same out-parameter idiom as facts, kept as its own
    plain list rather than folded into ResponseFacts: whether the background
    write queue admitted or shed this call's books is an internal storage
    fact a caller like the seeder needs to gate a retry decision on, not a
    caller-facing hydration fact the way ResponseFacts' tally and incomplete-
    reason set are -- those feed X-Libex-Incomplete-Reason, which persistence
    shedding has nothing to do with. None (the default) costs every existing
    caller nothing, the same as facts=None. When given, gets at most one
    PersistOutcome appended -- this function's own fetch path calls
    persist_books_background at most once per invocation.
    """
    books = await _get_books_by_asins_unsettled(
        asins,
        region,
        session,
        use_cache,
        high_concurrency,
        deadline,
        facts=facts,
        persist_outcome=persist_outcome,
    )
    return _settle_flags_list(books)


async def _get_books_by_asins_unsettled(
    asins: list[str],
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
    high_concurrency: bool = False,
    deadline: float | None = None,
    *,
    facts: ResponseFacts | None = None,
    persist_outcome: list[PersistOutcome] | None = None,
) -> list[dict[str, Any]]:
    """
    Fetches one or more books by ASIN from Audible.
    Writes results to relational DB and cache.
    Falls back to DB then cache when Audible is unavailable.

    high_concurrency, when True, runs the chunk fan-out below inside
    author_books_concurrency() (see client.py), drawing from the wider
    AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT pool instead of the default one.
    Set only by the author-ASIN routes hydrating get_author_books' own
    result -- the one path where a single request legitimately fans out to
    dozens of chunk requests and a live, measured production outage
    (5 concurrent author lookups 504ing at the fronting proxy's 30s timeout)
    traced directly back to that fan-out being serialized behind the
    default pool. Every other caller (single/small ASIN lists from the book,
    series, and search routes, and the seeder) defaults to False and is
    unaffected.

    facts, when given, is credited at every return point below with exactly
    which source each returned element came from -- this is the one function
    in the module where a single response can genuinely mix cache, fresh
    Audible, and DB-backstop elements, so the tally is built at each
    source-segmented concatenation rather than inferred afterwards from the
    merged list, which by that point no longer carries where any one element
    came from.

    persist_outcome, when given, records whether the background write queue
    admitted or shed the books freshly fetched from Audible in this call --
    see get_books_by_asins for why this rides its own list rather than
    ResponseFacts. Populated only where persist_books_background is actually
    called below (the all_products branch of the main success path); every
    other return -- an all-cache-hit early return, a not-found-only result,
    the Audible-unavailable fallback -- has nothing freshly fetched to
    persist and leaves the list untouched, exactly as it would if the caller
    hadn't asked.
    """
    if not asins:
        raise NotFoundException("No ASINs provided")

    seen: set[str] = set()
    unique_asins = [a for a in asins if not (a in seen or seen.add(a))]  # type: ignore

    if use_cache and len(unique_asins) == 1:
        cached = await cache.get(session, book_key(unique_asins[0], region))
        if cached:
            record_source_keys(facts, SOURCE_CACHE, [b["asin"] for b in [cached]])
            return [cached]

    # Batch cache: one lookup for the whole list, only fetch misses from
    # Audible. Reading the keys back in unique_asins order keeps cached_results
    # in the caller's order: that is the entire response on the all-hits early
    # return below, and its leading segment on every other return, each of
    # which concatenates it ahead of the freshly-fetched and backstop results
    # rather than reordering it.
    cached_results: list[dict[str, Any]] = []
    fetch_asins = unique_asins
    if use_cache and len(unique_asins) > 1:
        keys = [book_key(a, region) for a in unique_asins]
        hits = await cache.get_many(session, keys)
        cached_results = []
        fetch_asins = []
        for asin, key in zip(unique_asins, keys):
            hit = hits.get(key)
            if hit:
                cached_results.append(hit)
            else:
                fetch_asins.append(asin)

        # All ASINs found in cache
        if not fetch_asins:
            record_source_keys(facts, SOURCE_CACHE, [b["asin"] for b in cached_results])
            return cached_results

    # A connection is held for work, not for a request. Either cache read
    # above autobegins a transaction on session, and a READ COMMITTED
    # transaction advertises backend_xmin and can become the cluster's oldest
    # xmin even when it has only ever read -- measured on PostgreSQL 16.14 --
    # holding the xmin horizon against autovacuum until it ends. Held across
    # the fan-out below, that window is whatever Audible takes: the chunk
    # requests are unbounded in time and queue against the process-wide pool
    # in client.py, so a bulk request at the route's 1000-ASIN cap is 20
    # chunks draining through a handful of permits in successive waves, with
    # nothing between the cache read and the last chunk that needs a
    # transaction open. Released here, before any of it.
    #
    # One placement covers every path into the fan-out. With use_cache False
    # neither read ran, session is still lazy and holds no connection, and
    # rollback with no transaction in progress is a pass-through that touches
    # neither the session nor the pool. Nothing after this point depends on
    # transaction state established before it: both reads return plain
    # already-materialized values into cached_results, and the two later
    # session users -- the DB backstop for transiently failed chunks, and the
    # outage fallback in the except branch -- each open their own transaction
    # on their next statement, which SQLAlchemy re-acquires transparently.
    await session.rollback()

    try:
        start = time.monotonic()
        chunks = [fetch_asins[i:i + 50] for i in range(0, len(fetch_asins), 50)]

        # Fire every chunk concurrently -- audible_get itself is the throttle
        # point (a process-wide bound lives there), so nothing here needs to
        # cap fan-out. return_exceptions=True so a bad chunk can't wipe out
        # the chunks that already came back; gather still guarantees results
        # line up with chunks by index regardless of completion order, so
        # reassembly below stays in the same order the caller passed in.
        #
        # nullcontext when high_concurrency is False keeps every other
        # caller's behavior byte-identical to before this parameter existed
        # -- the pool draw only changes for the one path that opts in.
        pool_context = author_books_concurrency() if high_concurrency else nullcontext()
        with pool_context:
            # asyncio.wait with a timeout rather than gather, so the request's
            # own budget bounds hydration as well as discovery. Hydration used
            # to run entirely outside that budget: the walk stopped at its
            # deadline and then handed an unbounded fan-out to a caller the
            # proxy was already timing out on, so the worst case was the
            # discovery budget PLUS however long the books took.
            #
            # wait, not wait_for: wait_for cancels the whole gather and throws
            # away every chunk that had already come back. Here the chunks
            # that landed are kept, the ones still in flight are cancelled,
            # and their ASINs fall through to the DB backstop below exactly
            # as a transiently failed chunk does. The response is then
            # visibly short, which is what makes the route mark it incomplete
            # rather than advertising a half-hydrated body as whole.
            tasks = [
                asyncio.ensure_future(_fetch_chunk(chunk, region))
                for chunk in chunks
            ]
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                await _await_chunks(tasks, timeout, chunks, region)
            finally:
                # gather cancelled its children when the coroutine awaiting it
                # was cancelled; wait does not, and swapping one for the other
                # silently dropped that. Without this, an outer cancellation --
                # a graceful shutdown, or anything that later wraps these routes
                # in wait_for -- unwinds straight out of the await above and
                # leaves the whole fan-out running detached: no one holding the
                # tasks, each still holding an Audible pool permit and an httpx
                # connection, and any exception they raise never retrieved.
                # Discovery's own fan-out still uses gather and still gets this
                # for free; hydration has to ask for it.
                for task in tasks:
                    if not task.done():
                        task.cancel()
            # Rebuilt in the original chunk order, which zip() below relies on.
            results: list[Any] = []
            for task in tasks:
                if task.cancelled():
                    # Not recorded here: a cancelled chunk's ASINs still have
                    # the DB backstop below to answer from, and only the
                    # residue that backstop can't cover is an actual gap in
                    # what this function returns.
                    results.append(HydrationDeadlineExceeded())
                elif task.exception() is not None:
                    results.append(task.exception())
                else:
                    results.append(task.result())

        requested_took = round((time.monotonic() - start) * 1000, 2)

        all_products: list[dict[str, Any]] = []
        not_found_asins: list[str] = []
        deadline_asins: list[str] = []
        transient_failed_asins: list[str] = []
        transient_errors: list[Exception] = []

        for idx, (chunk, result) in enumerate(zip(chunks, results)):
            if isinstance(result, NotFoundException):
                # Only the single-ASIN branch of _fetch_chunk can raise this
                # at all -- the batch endpoint's own response is always a 200
                # with a products array, even when every requested ASIN is
                # unknown, so a batch chunk never surfaces as an exception
                # here. A nonexistent-but-well-formed ASIN doesn't reach this
                # branch either way: probed live, Audible returns 200 with a
                # hollow, titleless stub for one of those in both branches,
                # and _filter_products (see that function) is what turns that
                # stub into nothing to add rather than a 404. Whatever does
                # reach this branch is terminal for that one ASIN regardless,
                # not a reason to discard everything else that already
                # succeeded.
                not_found_asins.extend(chunk)
                record_incomplete(facts, REASON_HYDRATION_NOT_FOUND)
                continue
            if isinstance(result, Exception):
                transient_failed_asins.extend(chunk)
                transient_errors.append(result)
                if isinstance(result, HydrationDeadlineExceeded):
                    deadline_asins.extend(chunk)
                logger.warning(
                    "Hydration chunk failed",
                    extra={
                        "chunk_index": idx + 1,
                        "chunk_count": len(chunks),
                        "chunk_size": len(chunk),
                        "region": region,
                        "error_type": type(result).__name__,
                        "error": str(result),
                    },
                )
                continue
            # A batch chunk's own response is always a 200 even when some of
            # its requested ASINs have no such record -- Audible answers
            # those with a hollow, titleless stub rather than a 404 (see
            # _filter_products), and that stub is already gone from `result`
            # by the time it lands here. But `result` is _filter_products'
            # output, and that function drops two disjoint categories: the
            # titleless stub above, and any product whose
            # publication_datetime equals UNRELEASED_PLACEHOLDER. A chunk
            # ASIN with no matching product in `result` could be either one
            # -- a genuinely nonexistent ASIN, or a title Audible returned in
            # full that simply hasn't released yet, filtered out before this
            # comparison ever runs. Both are structurally indistinguishable
            # here, and both are correctly absent from the body either way:
            # the ASIN is not present in what this function returns.
            returned_asins = {p.get("asin") for p in result}
            stub_asins = [a for a in chunk if a not in returned_asins]
            if stub_asins:
                not_found_asins.extend(stub_asins)
                record_incomplete(facts, REASON_HYDRATION_NOT_FOUND)
            all_products.extend(result)

        if not_found_asins or transient_failed_asins:
            logger.warning("Partial hydration shortfall", extra={
                "requested_num": len(fetch_asins),
                "not_found_asins": len(not_found_asins),
                "failed_asins": len(transient_failed_asins),
                "region": region,
            })

        # Every chunk that mattered failed transiently and nothing else came
        # back -- fall through to the DB/cache fallback below exactly as a
        # single sequential failure would have. A pure not-found (no transient
        # errors) does NOT take this path: 404 is terminal, not a retry signal.
        if transient_failed_asins and not all_products and not cached_results:
            raise transient_errors[0]

        if not all_products and not cached_results:
            return []

        # less-data-never-accepted for a partial transient failure: some
        # chunks succeeding (or use_cache already producing cache hits) used
        # to skip the DB backstop entirely for transient_failed_asins,
        # silently omitting whatever was already stored for exactly the
        # ASINs the failed chunk(s) covered -- a 1500-ASIN author plus one
        # upstream 503 dropped the ~50 stored books that chunk owned, and
        # use_cache=True with every chunk failing returned cache hits only.
        # Scoped to transient_failed_asins alone, never not_found_asins: a
        # 404 is a confirmed absence, not a retry signal, and must not be
        # papered over by stale DB data. Runs whenever any chunk failed
        # transiently, independent of whether all_products or cached_results
        # already have something, so neither can silently swallow it the way
        # both used to.
        db_backstop_results: list[dict[str, Any]] = []
        if transient_failed_asins:
            db_backstop_results = await get_books_from_db(session, transient_failed_asins)
            # The backstop covers what the DB actually had; anything still
            # missing after it is a real shortfall the caller has to be told
            # about, not just an internal retry detail -- and which reason it
            # carries follows the ASIN, not the query: a deadline chunk the
            # backstop couldn't cover is still a deadline loss, not a generic
            # hydration failure, even though both walked the same query.
            backstop_asins = {b["asin"] for b in db_backstop_results}
            deadline_set = set(deadline_asins)
            other_failed_asins = [a for a in transient_failed_asins if a not in deadline_set]
            if _has_uncovered(deadline_asins, backstop_asins):
                record_incomplete(facts, REASON_HYDRATION_DEADLINE)
            if _has_uncovered(other_failed_asins, backstop_asins):
                record_incomplete(facts, REASON_HYDRATION_FAILED)

        normalized = await _normalize_products(all_products, region)

        if all_products:
            # Persist to DB and cache in the background
            outcome = persist_books_background(normalized, region)
            if persist_outcome is not None:
                persist_outcome.append(outcome)

        logger.info("Requested books from Audible", extra={
            "requested_num": len(fetch_asins),
            "cache_hits": len(cached_results),
            "requested_took": requested_took,
            "not_found_asins": len(not_found_asins),
            "failed_asins": len(transient_failed_asins),
            "db_backstop_num": len(db_backstop_results),
            "region": region,
        })

        record_source_keys(facts, SOURCE_CACHE, [b["asin"] for b in cached_results])
        record_source_keys(facts, SOURCE_AUDIBLE, [b["asin"] for b in normalized])
        record_source_keys(facts, SOURCE_DB, [b["asin"] for b in db_backstop_results])
        return cached_results + normalized + db_backstop_results

    except NotFoundException:
        raise

    except Exception as e:
        await session.rollback()
        logger.warning(
            "Audible unavailable, attempting DB fallback",
            extra={"asins": fetch_asins},
        )

        # Try relational DB first for the misses
        db_results = await get_books_from_db(session, fetch_asins)
        if db_results:
            record_source_keys(facts, SOURCE_CACHE, [b["asin"] for b in cached_results])
            record_source_keys(facts, SOURCE_DB, [b["asin"] for b in db_results])
            db_asins = {b["asin"] for b in db_results}
            if _has_uncovered(fetch_asins, db_asins):
                record_incomplete(facts, REASON_HYDRATION_FAILED)
            return cached_results + db_results

        # Fall back to cache for the misses -- one lookup, same as the
        # pre-fetch check above, and read back in fetch_asins order.
        fallback_results = []
        fallback_keys = [book_key(a, region) for a in fetch_asins]
        fallback_hits = await cache.get_many(session, fallback_keys)
        for key in fallback_keys:
            hit = fallback_hits.get(key)
            if hit:
                fallback_results.append(hit)
        if fallback_results or cached_results:
            # Both segments are cache reads -- cached_results from the
            # pre-fetch batch lookup, fallback_results from this outage
            # fallback's own -- so they're one source, one token, added
            # together rather than as two separate calls.
            record_source_keys(
                facts,
                SOURCE_CACHE,
                [b["asin"] for b in cached_results] + [b["asin"] for b in fallback_results],
            )
            # fetch_asins is what this fallback owed an answer for -- always
            # non-empty here, since an empty one would have returned via the
            # all-cache-hits path above before any of this outage handling
            # ran. cached_results predates fetch_asins by construction and
            # never overlaps it, so the coverage test reads only against
            # fallback_results, not the two summed.
            fallback_asins = {b["asin"] for b in fallback_results}
            if _has_uncovered(fetch_asins, fallback_asins):
                record_incomplete(facts, REASON_HYDRATION_FAILED)
            return cached_results + fallback_results

        # Neither a stored copy nor a cached one exists -- that is silence,
        # not a confirmed absence, so what reaches the caller has to say
        # Audible could not be reached rather than that these books are not
        # there.
        raise as_audible_failure(
            e, "Audible unavailable and no cached data found"
        ) from e


async def get_book_by_asin(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
    *,
    facts: ResponseFacts | None = None,
) -> dict[str, Any]:
    """Fetches a single book by ASIN."""
    books = await get_books_by_asins([asin], region, session, use_cache, facts=facts)
    if not books:
        raise NotFoundException(f"Book not found: {asin}")
    return books[0]


async def get_chapters(
    asin: str,
    region: str,
    session: AsyncSession,
    *,
    facts: ResponseFacts | None = None,
) -> dict[str, Any]:
    """
    Fetches chapter information for a book by ASIN.
    Returns data matching AudiMeta's TrackContentDto format.

    Single-source by construction -- audible, then db, then cache, never more
    than one per call -- so facts takes exactly one record_source per return
    path rather than the per-element tally _get_books_by_asins_unsettled
    needs.
    """
    try:
        path = f"/1.0/content/{asin}/metadata"
        params = {
            "response_groups": "chapter_info, always-returned, content_reference, content_url",
            "quality": "High",
        }

        start = time.monotonic()
        data = await audible_get(region, path, params)
        chapters_took = round((time.monotonic() - start) * 1000, 2)

        if not data.get("content_metadata", {}).get("chapter_info"):
            raise NotFoundException(f"No chapter information found for {asin}")

        result = _normalize_chapters(data, asin)

        # Persist to DB and cache in the background
        persist_track_background(asin, result, region)

        logger.info("Requested chapters from Audible", extra={
            "chapters_took": chapters_took,
            "region": region,
        })

        record_source(facts, SOURCE_AUDIBLE)
        return result

    except NotFoundException:
        raise

    except Exception as e:
        # Try DB first
        db_result = await get_track_from_db(session, asin)
        if db_result:
            record_source(facts, SOURCE_DB)
            return db_result

        # Fall back to cache
        cached = await cache.get(session, chapters_key(asin, region))
        if cached:
            record_source(facts, SOURCE_CACHE)
            return cached

        # Neither a stored copy nor a cached one exists -- that is silence,
        # not a confirmed absence, so what reaches the caller has to say
        # Audible could not be reached rather than that this book has no
        # chapters.
        logger.warning("Audible unavailable and no cached chapter data found", extra={
            "asin": asin,
            "region": region,
            "error": str(e),
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(
            e, "Audible unavailable and no cached chapter data found"
        ) from e


async def fetch_and_store_chapters(
    asin: str,
    region: str,
    session: AsyncSession,
) -> str:
    """
    Fetches a book's chapters and stores them, stamping chapters_checked_at so the
    book is recorded as checked whatever the outcome. Best-effort: this never
    raises, so a chapter failure can't break whatever persistence flow called it
    (the seeder relies on that — its job is book metadata, chapters are a bonus).

    Outcomes mirror the standalone backfill:
    - "stored":    chapters fetched and written to tracks; marked checked.
    - "none":      resolved but Audible exposes no chapters; marked checked.
    - "not_found": 404 — no chapter metadata anywhere (e.g. the ISBN-keyed
                   records); marked checked so it isn't retried.
    - "error":     transient failure (Audible 500/timeout/network) or a write
                   failure; NOT marked, so a later pass (or the backfill) retries.

    A book asked about before its release date is marked like any other, but
    the mark does not settle it: Audible answers a chapter request for audio
    that does not exist yet with a 404, so both selection sites re-admit a book
    whose stamp predates its release_date once that date has passed. Asking
    early therefore costs one wasted request rather than retiring the title
    before it ever had chapters to find.

    Coordinates with the standalone backfill via chapters_checked_at: neither
    path re-fetches what the other has already marked, on the same terms.
    """
    path = f"/1.0/content/{asin}/metadata"
    params = {
        "response_groups": "chapter_info, always-returned, content_reference, content_url",
        "quality": "High",
    }

    try:
        data = await audible_get(region, path, params)
    except NotFoundException:
        await _mark_chapters_checked(session, asin)
        return "not_found"
    except Exception as e:
        logger.warning(
            "Seeder chapters: fetch error",
            extra={
                "asin": asin,
                "region": region,
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
        return "error"

    if not data.get("content_metadata", {}).get("chapter_info"):
        await _mark_chapters_checked(session, asin)
        return "none"

    try:
        chapters = _normalize_chapters(data, asin)
        await upsert_track(session, asin, chapters)
        await _mark_chapters_checked(session, asin)
        return "stored"
    except Exception as e:
        logger.warning(
            "Seeder chapters: store failed",
            extra={"asin": asin, "error_type": type(e).__name__, "error": str(e)},
        )
        await session.rollback()
        return "error"


async def _mark_chapters_checked(session: AsyncSession, asin: str) -> None:
    """
    Stamps chapters_checked_at on a book, recording that its chapters have
    been asked about.

    Nothing ever clears the column, so for a book that was already out when it
    was asked this is final and it leaves the queue for good. For one asked
    ahead of its release date it is not: both selection sites re-admit a book
    whose stamp predates its release_date once that date has passed. That is
    what keeps an early 404 -- Audible has no chapters for audio that does not
    exist yet -- from retiring a title before release day, and it needs no
    condition here, because the stamp itself is the record of when the
    question was asked. See _gather_chapters in the seeder and _select_work in
    scripts/backfill_chapters.py.
    """
    await session.execute(
        update(Book)
        .where(Book.asin == asin)
        .values(chapters_checked_at=datetime.now(timezone.utc))
    )
    await session.commit()