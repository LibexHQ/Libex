"""
Audible author-books lookup by author name.
Fetches an author's book ASINs from the /1.0/catalog/products endpoint with
a single-sort, name-only walk (_fetch_author_books_by_name_detailed), for
callers that have a name and no author ASIN to attribute against: the
get_author_books_by_name service call and the seeder's per-author
expansion loop. The ASIN-attributed, category-sliced walk that
get_author_books uses lives in catalog.py.
"""

# Standard library
import asyncio
import time
from dataclasses import dataclass

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from libex_core.audible.client import as_audible_failure, upstream_status_of
from libex_core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.core.response_headers import (
    REASON_DISCOVERY_INCOMPLETE,
    ResponseFacts,
    record_incomplete,
)

# Services
from app.services.audible import audible_get

logger = get_logger()

# Bound for the name-search walk in _fetch_author_books_by_name_detailed,
# same principle as the screens bounds (screens.py): the walk's real terminators
# are the catalog endpoint's own total_results field (read from a live
# response, not guessed -- see that function's docstring) and, for the
# rare query where total_results overstates what's actually retrievable, a
# detected repeat of an already-seen page's content. This cap exists only
# beneath those, to stop a pathological upstream that never gives either
# signal, sized with the same order-of-magnitude headroom over the largest
# known real catalog (Conan Doyle, 4500) as the screens page cap -- never
# to limit a legitimate author.
NAME_SEARCH_MAX_PAGES = 5000


_NAME_SEARCH_PAGE_SIZE = 50

# How a by-name walk ended. Only page-failed and deadline are transient -- the same walk
# run again could get further -- so a caller deciding whether to retry reads
# NameWalkOutcome.transient rather than the kind itself. A plateau and the
# page cap are deterministic: they stop at the same place every time.
STOP_COMPLETED = "completed"
STOP_PLATEAU = "plateau"
STOP_PAGE_CAP = "page-cap"
STOP_PAGE_FAILED = "page-failed"
STOP_DEADLINE = "deadline"
_TRANSIENT_STOPS = frozenset({STOP_PAGE_FAILED, STOP_DEADLINE})


@dataclass
class NameWalkOutcome:
    """Out-param a caller may pass to the by-name walk to learn how it
    ended. stop is None until the walk finishes."""

    stop: str | None = None

    @property
    def transient(self) -> bool:
        return self.stop in _TRANSIENT_STOPS


async def _fetch_name_search_page(name: str, region: str, page: int) -> dict:
    """Fetches a single page of the catalog author-name search. Raises on
    failure; the caller decides what a failed page means for the walk."""
    path = "/1.0/catalog/products"
    params = {
        "author": name,
        "num_results": _NAME_SEARCH_PAGE_SIZE,
        "page": page,
        "response_groups": "product_desc,contributors,series,product_attrs,media",
        "products_sort_by": "-ReleaseDate",
    }
    return await audible_get(region, path, params)


def _accept_name_search_products(
    products: list, name: str, seen: set[str], asins: list[str]
) -> None:
    """Applies the exact-name, dedupe filter and appends matches to asins
    in-place, in the order products was given.

    Region scoping is per-host (a de-region call hits api.audible.de), so
    a store's catalogue already IS what that region means -- a
    Spanish-language Christie sold in the US store is a legitimate
    US-region product, so this applies no language filter at all: region
    does the scoping, and a live check confirmed a language filter here
    silently drops a large share of a real author's ASINs (490 of
    Christie's 1100 US ASINs -- 125 German, 118 Spanish, 94 Italian, 63
    Swedish and more).

    ASIN admission is truthy-only, matching _process_catalog_page's rule
    for the same reason: both functions read products from this same
    /1.0/catalog/products endpoint, which includes ISBN-keyed records
    whose asin field is not a 10-char B-format ASIN (see libex_core/audible/_retry.py's
    ~84k-record note and _process_catalog_page's own comment) -- rejecting
    those here would be the same data loss the less-data-never-accepted
    invariant exists to stop. This is unlike _extract_row_asins, which
    validates with is_valid_asin because it reads a different endpoint,
    the Android author-detail screens grid."""
    for product in products:
        matches = any(
            a.get("name", "").lower() == name.lower()
            for a in product.get("authors", [])
        )
        asin = product.get("asin")
        if asin and matches:
            asin = asin.upper()
            if asin not in seen:
                seen.add(asin)
                asins.append(asin)


async def _fetch_author_books_by_name_detailed(
    name: str,
    region: str,
    deadline: float | None = None,
    concurrency: int = 1,
    *,
    facts: ResponseFacts | None = None,
    outcome: NameWalkOutcome | None = None,
) -> tuple[list[str], int, bool]:
    """
    Fetches book ASINs by author name using the standard catalog endpoint,
    reporting whether the walk reached a confirmed natural end.

    Unlike the screens endpoint, /1.0/catalog/products takes a real integer
    page index with no continuation-token chain, so pages are independently
    addressable and don't have to be fetched one at a time. concurrency
    bounds how many pages are ever in flight at once and defaults to 1
    (fully sequential, one request at a time) specifically so the shared
    fetch_author_books_by_name wrapper, which the seeder's paced per-author
    expansion loop calls, is untouched by this: it never passes concurrency,
    so it always runs sequential. get_author_books, the live ASIN-scoped
    request path, fetches the catalog through the separate multi-sort,
    ASIN-attributed walk in _fetch_author_books_by_catalog instead of this
    function -- this walk is reached today only via fetch_author_books_by_name
    and get_author_books_by_name, both of which stay on the default
    concurrency=1.

    Fetching is strictly ordered: pages within a batch are requested
    concurrently but always reassembled and processed in ascending page-
    index order (asyncio.gather preserves input order regardless of
    completion order), so the ASIN list this returns for a given author is
    in the same order a sequential, one-page-at-a-time walk would produce
    -- concurrency only changes wall-clock time, never the result or the
    number of requests made for a normal-sized catalog (see below).

    Termination: a real captured response was read (not the docs) to
    answer this rather than guessing. The catalog endpoint's top-level
    total_results field is genuine and, for any author whose real result
    count stays under Audible's own internal retrieval ceiling for this
    query type (measured at 500 distinct results -- Brandon Sanderson's 203
    paginates exactly to a short final page as total_results promises),
    lets every page this walk will ever need be known after fetching page
    0 alone: page 0 is always fetched solo for exactly that reason -- one
    extra request to learn the true page count is far cheaper than
    guessing wrong in either direction, and it keeps the request count for
    a small catalog at exactly the pages it needs, not inflated by a
    speculative full concurrency-sized batch. Once total_results is known,
    subsequent batches are sized to the pages it implies (capped at
    NAME_SEARCH_MAX_PAGES).

    But total_results is NOT trustworthy as an upper bound once a query's
    real match count exceeds that same internal ceiling: probed live
    against Arthur Conan Doyle (many editions/narrators/homonyms share the
    name), total_results reported 5367, yet page 9 was the last page with
    new content -- every page from 10 onward silently returned page 9's
    exact content again, forever, rather than a short/empty page or an
    error. Trusting total_results alone there would have paginated through
    ~98 entirely wasted pages. So every page's product-ASIN signature is
    checked against every signature already seen this walk; a repeat means
    this walk has found that same retrieval ceiling on its own, which is a
    different signal from upstream confirming nothing further remains --
    total_results itself is exactly what overstated the real ceiling here,
    so it cannot also be trusted to certify the stop as complete. A repeat
    stops the walk without marking it complete, rather than looping
    through wasted, identical pages; this bounds the wasted overshoot from
    that case to at most one batch's width, not the walk's full page cap.

    If total_results is absent from page 0's response, batches fall back
    to speculative concurrency-sized ones, stopping at the first short or
    empty page within a batch (or the repeat check above) -- nothing
    collected before that point is discarded.

    completed is True only when the walk reached a genuine end signal
    upstream itself confirmed: total_results running out (the next page
    would be past the known last one) or a short/empty page. It is False
    for every other stop, including a detected content repeat -- that is
    this walk noticing its own plateau, not upstream confirming nothing
    remains, the same distinction the screens walk's
    SCREENS_REASON_PLATEAU_TRUNCATED draws against SCREENS_REASON_COMPLETED
    (see that constant). It is also False for the page cap, the deadline,
    or a page-fetch failure. A caller relying on this list as exhaustive
    needs to know the difference, not just that a list came back.

    A failure fetching page 0 raises AudibleAPIException: with nothing
    harvested yet, an empty list would be indistinguishable from an author
    with no books, and an outage must never read as an empty catalog.

    A failure fetching any later page ends the walk but keeps every ASIN already
    harvested from pages before it -- specifically, the result is
    truncated at the last successfully processed page in ascending index
    order (pages later in the same batch that happened to complete are
    discarded, never used to paper over the gap), a "clean prefix, never a
    hole" guarantee that mirrors the per-page resilience
    _fetch_author_books_by_screen already has. The failing page's index
    and error are logged.

    facts, when given, is marked incomplete (REASON_DISCOVERY_INCOMPLETE)
    whenever the walk stops short of a confirmed end -- a later-page failure,
    the deadline, the page cap or a detected repeat -- so a caller holding
    the same ResponseFacts can tell a truncated list from a whole one.

    outcome, when given, has its stop set to one of the STOP_* kinds, so a
    caller can tell a transient stop (page failure, deadline) from a
    deterministic one (plateau, page cap) that a rerun would repeat.

    deadline, when given, is an absolute time.monotonic() bound checked
    once before dispatching each batch; once passed the walk stops without
    starting another batch and returns what it has.

    pages_fetched counts pages that were actually, successfully fetched --
    it increments once per successful response, independent of the page
    index requested. It is therefore correct on every termination path,
    including a batch that overshot the real end: those pages were still
    genuinely fetched, their content just wasn't new.

    Returns (asins, pages_fetched, completed).
    """
    asins: list[str] = []
    seen: set[str] = set()
    seen_page_signatures: set[tuple[str | None, ...]] = set()
    pages_fetched = 0
    total_results: int | None = None
    next_page = 0
    # Falls through to the page cap if the loop runs out without a break.
    stop_kind = STOP_PAGE_CAP

    while next_page <= NAME_SEARCH_MAX_PAGES:
        if deadline is not None and time.monotonic() >= deadline:
            stop_kind = STOP_DEADLINE
            break

        if total_results is not None:
            last_known_page = min(
                (total_results - 1) // _NAME_SEARCH_PAGE_SIZE, NAME_SEARCH_MAX_PAGES
            )
            if next_page > last_known_page:
                stop_kind = STOP_COMPLETED
                break
            batch_size = min(concurrency, last_known_page - next_page + 1)
        elif next_page == 0:
            # The only page we fetch solo on principle: it's the sole way
            # to learn total_results (or, absent that, the endpoint's own
            # short/empty signal), and a full concurrency-sized batch fired
            # before that is known would send concurrency-1 wasted requests
            # for the common case of a small catalog -- exactly what
            # bounded concurrency exists to avoid.
            batch_size = 1
        else:
            # total_results was absent from page 0's response; fall back
            # to speculative bounded batches (see docstring).
            batch_size = concurrency

        batch_pages = list(range(next_page, next_page + batch_size))
        results = await asyncio.gather(
            *(_fetch_name_search_page(name, region, p) for p in batch_pages),
            return_exceptions=True,
        )

        stop = False
        for page, result in zip(batch_pages, results):
            if isinstance(result, BaseException):
                if page == 0:
                    # Nothing harvested yet: report the outage instead of
                    # returning an empty list that reads as "no books".
                    logger.warning(
                        "Audible Author Books by-name first page fetch failed",
                        extra={
                            "region": region,
                            "error": f"{type(result).__name__}: {result}",
                        },
                    )
                    if isinstance(result, Exception):
                        raise as_audible_failure(
                            result, "Audible author books by name unavailable"
                        ) from result
                    raise result
                # No "author_name" field here either: this walk is reached
                # both from the seeder, where name is a stored catalogue
                # value, and from the by-name route, where it is whatever a
                # caller typed. The two are indistinguishable at this point,
                # so the identifying value is never written and the failure
                # is reported by where and how it failed instead.
                logger.warning(
                    "Audible Author Books by-name page fetch failed, keeping partial harvest",
                    extra={
                        "region": region,
                        "page": page,
                        "asins_collected": len(asins),
                        "error": f"{type(result).__name__}: {result}",
                    },
                )
                stop_kind = STOP_PAGE_FAILED
                stop = True
                break

            pages_fetched += 1
            data = result
            if total_results is None:
                candidate_total = data.get("total_results")
                if isinstance(candidate_total, int) and candidate_total >= 0:
                    total_results = candidate_total

            products = data.get("products", [])
            if not products:
                stop_kind = STOP_COMPLETED
                stop = True
                break

            page_signature = tuple(p.get("asin") for p in products)
            if page_signature in seen_page_signatures:
                # A repeat is this walk noticing its own plateau, not
                # upstream confirming nothing further remains -- see the
                # docstring above. The walk stays incomplete so a caller can
                # tell this apart from a genuine short/empty-page end.
                stop_kind = STOP_PLATEAU
                stop = True
                break
            seen_page_signatures.add(page_signature)

            _accept_name_search_products(products, name, seen, asins)

            if len(products) < _NAME_SEARCH_PAGE_SIZE:
                stop_kind = STOP_COMPLETED
                stop = True
                break

        if stop:
            break

        next_page = batch_pages[-1] + 1

    completed = stop_kind == STOP_COMPLETED
    if outcome is not None:
        outcome.stop = stop_kind
    if not completed:
        record_incomplete(facts, REASON_DISCOVERY_INCOMPLETE)
        # One summary for every early stop, including the ones (deadline,
        # plateau, page cap) that log nothing else. Never the name.
        logger.info(
            "Audible Author Books by-name walk ended before a confirmed end",
            extra={
                "region": region,
                "stop": stop_kind,
                "pages_fetched": pages_fetched,
                "asins_collected": len(asins),
            },
        )

    return asins, pages_fetched, completed


async def fetch_author_books_by_name(
    name: str,
    region: str,
    deadline: float | None = None,
    *,
    facts: ResponseFacts | None = None,
    outcome: NameWalkOutcome | None = None,
) -> tuple[list[str], int]:
    """
    Fetches book ASINs by author name using the standard catalog endpoint.
    Returns (asins, pages_fetched). Shared by get_author_books_by_name in
    this module and by the seeder's author-expansion phase, neither of
    which needs to distinguish a confirmed-complete walk from one that
    merely stopped, nor has an author ASIN to attribute against --
    get_author_books, the ASIN-scoped live request path, fetches the
    catalog through the separate, ASIN-attributed multi-sort walk in
    _fetch_author_books_by_catalog instead of this function entirely.

    Always runs at _fetch_author_books_by_name_detailed's default
    concurrency=1 (fully sequential, one page in flight at a time) since
    it never passes concurrency through -- this is deliberate, not an
    oversight: the seeder's paced per-author expansion loop calls this
    wrapper once per author inside its own deliberately spaced loop, and
    parallelizing page fetches inside a helper the seeder shares would
    silently turn every seeder author into a concurrent-request burst,
    defeating that pacing across the seeder's whole run.

    deadline, when given, is an absolute time.monotonic() bound checked
    once before dispatching each batch; once passed the walk stops and
    returns what it has.

    A page-0 failure raises AudibleAPIException. facts, when given, is marked
    incomplete if the walk stopped short of a confirmed end, and outcome, when
    given, reports how it stopped (see NameWalkOutcome).
    """
    asins, pages_fetched, _ = await _fetch_author_books_by_name_detailed(
        name, region, deadline=deadline, facts=facts, outcome=outcome
    )
    return asins, pages_fetched


async def get_author_books_by_name(
    name: str,
    region: str,
    session: AsyncSession,
    *,
    facts: ResponseFacts | None = None,
) -> list[str]:
    """Fetches book ASINs by author name. A first-page failure raises
    AudibleAPIException; a later-page failure returns what was gathered and
    marks facts incomplete (REASON_DISCOVERY_INCOMPLETE)."""
    try:
        start = time.monotonic()
        asins, pages_fetched = await fetch_author_books_by_name(name, region, facts=facts)
        author_book_took = round((time.monotonic() - start) * 1000, 2)

        if not asins:
            raise NotFoundException(f"No books found for author name: {name}")

        # Deliberately no "author_name" field: on this route the name is
        # verbatim caller-authored text, not catalogue data resolved from an
        # ASIN, and Libex records nothing that identifies a caller and nothing
        # a caller typed. Nothing stands in for it -- unlike a search query,
        # whose length correlates with how it performed, an author name's
        # length explains nothing the page count below does not already.
        logger.info("Requested Audible Author Books By Name", extra={
            "author_book_num": len(asins),
            "pages_fetched": pages_fetched,
            "author_book_took": author_book_took,
            "region": region,
        })

        return asins

    except NotFoundException:
        raise
    except Exception as e:
        # name is caller-authored and never logged -- see the "Deliberately
        # no author_name field" note above, which applies here too.
        logger.warning("Failed to fetch author books by name", extra={
            "name_length": len(name),
            "region": region,
            "error": str(e),
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, "Failed to fetch author books by name") from e
