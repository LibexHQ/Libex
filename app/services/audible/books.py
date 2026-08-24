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
two files rather than a boundary. The module stays long because the job is
one job.
"""

# Standard library
import asyncio
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
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.core.response_headers import (
    ResponseFacts,
    SOURCE_AUDIBLE,
    SOURCE_CACHE,
    SOURCE_DB,
    record_source,
    record_source_keys,
)
from app.core.utils import strip_html, strip_image_size_suffix

# Services
from app.services.audible.client import audible_get, author_books_concurrency, REGION_MAP
from app.services.cache import manager as cache
from app.services.cache.manager import book_key, chapters_key
from app.services.db.persist_queue import persist_books_background, persist_track_background
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

# Sentinel this repo has never seen Audible actually send -- see
# _filter_products for what was measured and why the clause stays anyway.
UNRELEASED_PLACEHOLDER = "2200-01-01T00:00:00Z"

# Below this many products, normalization runs inline on the event loop; at
# or above it, the whole batch is handed to a single asyncio.to_thread call.
# Measured (scratchpad benchmark, not part of this repo): a lone product
# normalizes in ~0.2ms and a 50-ASIN chunk -- the largest a single Audible
# catalog request ever returns, see the chunking below -- in ~4-5ms, while
# the to_thread hop itself costs ~0.3-0.5ms of fixed overhead regardless of
# batch size. Every book/series/search caller (single ASIN up to one chunk)
# stays under this threshold and keeps its current inline timing exactly.
# It's only get_author_books' hydration, which accumulates products across
# many chunks before normalizing once, that can cross it -- the flagged
# ~1530-product case blocks the loop for ~150ms inline with nothing else
# able to run in that window, against ~180ms threaded but with the loop free
# to service other requests throughout. The thread hop is a net wall-clock
# loss at every size tested; the point is solely to stop a single request
# from stalling every other connection the process is holding.
NORMALIZE_THREAD_THRESHOLD = 100


# ============================================================
# HELPERS
# ============================================================

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
    """Extracts author objects matching AudiMeta's MinimalAuthorDto."""
    authors = []
    for author in product.get("authors", []):
        name = author.get("name", "").replace("\t", "").strip()
        asin = author.get("asin", "").replace("\t", "").strip() if author.get("asin") else None
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
    per _UNREADABLE_PLANS_LOG_INTERVAL_SECONDS, the same windowing
    persist_queue.py's _record_shed applies to its own repeat-incident
    warning and for the same reason: this can fire on every product at once
    if plan_name itself is what changed shape, and an uncapped line per
    product would flood out the report of the one incident causing all of
    them. asin is the most recently affected product in the window, not
    every one of them -- enough to start looking without paying for a line
    per occurrence.
    """
    global _unreadable_plans_count, _unreadable_plans_last_logged
    _unreadable_plans_count += 1
    now = time.monotonic()
    # None rather than 0.0, and the distinction is not cosmetic:
    # time.monotonic() counts from boot, so against a 0.0 sentinel this
    # comparison reads "never reported" as "reported at boot" and stays true
    # for the first minute of a process's life -- swallowing the very first
    # report. That is the worst minute to lose this particular warning: it
    # exists to catch an upstream rename before the plans column quietly
    # empties across the corpus, and six worker processes all start fresh on
    # every deploy. persist_queue's _window_elapsed carries the same guard
    # for the same reason.
    if (
        _unreadable_plans_last_logged is not None
        and now - _unreadable_plans_last_logged < _UNREADABLE_PLANS_LOG_INTERVAL_SECONDS
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


def _normalize_product(product: dict, region: str) -> dict[str, Any]:
    """
    Normalizes a raw Audible product into Libex response format.
    Field names match AudiMeta's BookDto exactly for drop-in compatibility.
    """
    asin = product.get("asin", "")
    series_list = _parse_series(product, region)

    content_type = product.get("content_type")
    is_podcast = content_type and content_type.lower() == "podcast"

    return {
        "asin": asin,
        "title": product.get("title"),
        "subtitle": product.get("subtitle"),
        "description": strip_html(product.get("merchandising_summary")),
        "summary": strip_html(product.get("publisher_summary")),
        "region": region,
        "regions": [region],
        "publisher": product.get("publisher_name"),
        "copyright": product.get("copyright"),
        "isbn": product.get("isbn"),
        "language": product.get("language"),
        "rating": product.get("rating", {}).get("overall_distribution", {}).get("average_rating"),
        "bookFormat": product.get("format_type"),
        "releaseDate": _parse_release_date(product.get("release_date")),
        "explicit": product.get("is_adult_product", False),
        "hasPdf": product.get("is_pdf_url_available", False),
        "whisperSync": product.get("read_along_support", False),
        "imageUrl": _best_image(product.get("product_images", {})),
        "lengthMinutes": product.get("runtime_length_min"),
        "link": _audible_link(asin, region),
        "contentType": content_type,
        "contentDeliveryType": product.get("content_delivery_type"),
        "episodeNumber": str(product.get("episode_number")) if is_podcast and product.get("episode_number") else None,
        "episodeType": product.get("episode_type") if is_podcast else None,
        "sku": product.get("sku"),
        "skuGroup": product.get("sku_lite"),
        # No default: True/False here would be Libex asserting an answer
        # Audible never gave. None means "Audible said nothing" and is what
        # lets the writer's tri-state merge (see _asserted_bool in writer.py)
        # tell that apart from an explicit false and leave a stored value
        # alone. isAvailable and isBuyable are both is_buyable -- AudiMeta's
        # own DTO derives them the same way (see _settle_flags below for
        # where None stops being a valid outward value).
        "isListenable": product.get("is_listenable"),
        "isAvailable": product.get("is_buyable"),
        "isBuyable": product.get("is_buyable"),
        "isVvab": product.get("is_vvab"),
        "plans": _parse_plans(product),
        "updatedAt": None,
        "authors": _parse_authors(product, region),
        "narrators": _parse_narrators(product),
        "genres": _parse_genres(product),
        "series": series_list,
    }


# Settled values for the four tri-state flags above, matched to the columns'
# own DB defaults (app/db/models.py) so a live-fetched book and a DB-backed
# one never disagree about the same ASIN once the row exists: True mirrors
# AudiMeta's own ingest (no ?? false, unlike explicit/hasPdf/whisperSync) for
# the three it defines, and isVvab -- which AudiMeta doesn't have -- settles
# to Libex's own prior emitted value rather than inventing a new one.
_FLAG_SETTLE_DEFAULTS: dict[str, bool] = {
    "isListenable": True,
    "isAvailable": True,
    "isBuyable": True,
    "isVvab": False,
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
    variant, same as the four flags below. A None here reaches neither
    filtering.py's equality match nor BookResponse otherwise: pydantic does
    not fall back to a field's default_factory on an explicit None, it
    raises, and every route this feeds fell over on exactly that until this
    settled it here rather than in _parse_plans.

    Returns a new dict; the input is never mutated, since some callers still
    need the unsettled version afterwards (see get_new_releases/
    get_coming_soon in releases.py, which persist the tri-state and cache the
    settled result under the same call).

    Only settles a key that is PRESENT and None -- never adds a key a dict
    never carried. _normalize_product always carries all four flags plus
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
    one per product -- per-product hops would each pay the hop's own fixed
    cost on top of the ~0.2ms of work being moved, which loses badly at the
    hundreds-to-low-thousands sizes this exists for.

    _normalize_product touches only its own arguments and pure helpers
    (strip_html, strip_image_size_suffix, datetime parsing) -- no DB session,
    no cache, no shared mutable state, and no read of any ContextVar, so
    running it on another thread carries no correctness risk. In particular
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
    drop-in-replacement language, so the clause is more likely inherited
    from that source than derived from an observed Audible response, though
    the AudiMeta service that could confirm it is gone. 1,600 products is a
    sample, not the catalogue: strong enough that this is no longer an
    unexamined, possibly-wrong inheritance, not strong enough to delete an
    original guard on the strength of its absence. The clause stays. What
    would justify removing it is an actual product observed carrying the
    sentinel -- so far there has never been one.
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
) -> list[dict[str, Any]]:
    """
    Public entry point. Delegates to _get_books_by_asins_unsettled and settles
    the four tri-state flags (see _settle_flags) on whatever it returns before
    handing it back.

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
    """
    books = await _get_books_by_asins_unsettled(
        asins, region, session, use_cache, high_concurrency, deadline, facts=facts
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
                    results.append(HydrationDeadlineExceeded())
                elif task.exception() is not None:
                    results.append(task.exception())
                else:
                    results.append(task.result())

        requested_took = round((time.monotonic() - start) * 1000, 2)

        all_products: list[dict[str, Any]] = []
        not_found_asins: list[str] = []
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
                continue
            if isinstance(result, Exception):
                transient_failed_asins.extend(chunk)
                transient_errors.append(result)
                logger.warning(
                    f"Hydration chunk {idx + 1}/{len(chunks)} failed for "
                    f"{len(chunk)} ASINs ({region}): {type(result).__name__}: {result}"
                )
                continue
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

        normalized = await _normalize_products(all_products, region)

        if all_products:
            # Persist to DB and cache in the background
            persist_books_background(normalized, region)

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

    except Exception:
        await session.rollback()
        logger.warning(f"Audible unavailable, attempting DB fallback for {fetch_asins}")

        # Try relational DB first for the misses
        db_results = await get_books_from_db(session, fetch_asins)
        if db_results:
            record_source_keys(facts, SOURCE_CACHE, [b["asin"] for b in cached_results])
            record_source_keys(facts, SOURCE_DB, [b["asin"] for b in db_results])
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
            return cached_results + fallback_results

        raise NotFoundException("Audible unavailable and no cached data found")


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

    except Exception:
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

        raise NotFoundException("Audible unavailable and no cached chapter data found")


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

    Coordinates with the standalone backfill via chapters_checked_at: a book
    marked here won't be re-fetched there, and vice versa.
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
        logger.warning(f"Seeder chapters: fetch error for {asin} ({region}): {type(e).__name__}: {e}")
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
        logger.warning(f"Seeder chapters: store failed for {asin}: {type(e).__name__}: {e}")
        await session.rollback()
        return "error"


async def _mark_chapters_checked(session: AsyncSession, asin: str) -> None:
    """Stamps chapters_checked_at on a book so it leaves the backfill queue."""
    await session.execute(
        update(Book)
        .where(Book.asin == asin)
        .values(chapters_checked_at=datetime.now(timezone.utc))
    )
    await session.commit()