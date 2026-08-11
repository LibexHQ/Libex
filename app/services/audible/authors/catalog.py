"""
Audible author-books catalog walks.
Fetches an author's book ASINs from the /1.0/catalog/products endpoint --
both the single-sort, name-only walk (_fetch_author_books_by_name_detailed)
and the ASIN-attributed multi-sort walk (_fetch_author_books_by_catalog).
Both hit the same endpoint, differing only in response_groups and
products_sort_by, so they stay together in this module rather than two
sibling modules that would each own only half of it.
"""

# Standard library
import asyncio
import time
from dataclasses import dataclass
from typing import Any

# Core
from app.core.logging import get_logger

# Services
from app.services.audible.client import audible_get

logger = get_logger()

# Bound for the name-search walk in _fetch_author_books_by_name_detailed,
# same principle as the screens bounds above: the walk's real terminators
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
    whose asin field is not a 10-char B-format ASIN (see client.py's
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

    A failure fetching one page ends the walk but keeps every ASIN already
    harvested from pages before it -- specifically, the result is
    truncated at the last successfully processed page in ascending index
    order (pages later in the same batch that happened to complete are
    discarded, never used to paper over the gap), a "clean prefix, never a
    hole" guarantee that mirrors the per-page resilience
    _fetch_author_books_by_screen already has. The failing page's index
    and error are logged.

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
    completed = False
    total_results: int | None = None
    next_page = 0

    while next_page <= NAME_SEARCH_MAX_PAGES:
        if deadline is not None and time.monotonic() >= deadline:
            break

        if total_results is not None:
            last_known_page = min(
                (total_results - 1) // _NAME_SEARCH_PAGE_SIZE, NAME_SEARCH_MAX_PAGES
            )
            if next_page > last_known_page:
                completed = True
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
                logger.warning(
                    "Audible Author Books by-name page fetch failed, keeping partial harvest",
                    extra={
                        "author_name": name,
                        "region": region,
                        "page": page,
                        "asins_collected": len(asins),
                        "error": f"{type(result).__name__}: {result}",
                    },
                )
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
                completed = True
                stop = True
                break

            page_signature = tuple(p.get("asin") for p in products)
            if page_signature in seen_page_signatures:
                # A repeat is this walk noticing its own plateau, not
                # upstream confirming nothing further remains -- see the
                # docstring above. completed stays False so a caller can
                # tell this apart from a genuine short/empty-page end.
                stop = True
                break
            seen_page_signatures.add(page_signature)

            _accept_name_search_products(products, name, seen, asins)

            if len(products) < _NAME_SEARCH_PAGE_SIZE:
                completed = True
                stop = True
                break

        if stop:
            break

        next_page = batch_pages[-1] + 1

    return asins, pages_fetched, completed


async def fetch_author_books_by_name(
    name: str,
    region: str,
    deadline: float | None = None,
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
    """
    asins, pages_fetched, _ = await _fetch_author_books_by_name_detailed(
        name, region, deadline=deadline
    )
    return asins, pages_fetched


# ============================================================
# CATALOG MULTI-SORT WALK (get_author_books' 3 of its 4 sources)
#
# Distinct from _fetch_author_books_by_name_detailed above, which stays a
# single-sort, name-only walk serving get_author_books_by_name and the
# seeder's per-author expansion loop, neither of which has an author ASIN
# to attribute against. get_author_books does have the ASIN, and probed live, the
# catalog is neither a subset of the screens grid nor complete on its
# own: for Agatha Christie (total_results 1138), walking -ReleaseDate
# alone plateaus at 500 distinct results, and walking ascending
# ReleaseDate plateaus at a DIFFERENT 499 with zero overlap -- different
# sort orders open disjoint windows into the same underlying result set,
# so ANY single sort misses results a second sort would surface, and
# every sort still plateaus at roughly the same ~500-result ceiling no
# matter how far paging continues past it.
# ============================================================

# Priority order matters here: -ReleaseDate first is compat-critical.
# Consumers have always received the -ReleaseDate list at the front of
# the response and must keep receiving that same list, in that same
# order, unmoved at the front of the union -- see get_author_books.
# Ascending
# ReleaseDate is the bare field name, not "+ReleaseDate" -- probed live,
# the leading "+" 400s. -Title is third, the last sort trusted enough to
# spend a request on.
_CATALOG_SORTS: tuple[str, ...] = ("-ReleaseDate", "ReleaseDate", "-Title")

CATALOG_PAGE_SIZE = 50

# Audible's observed deep-paging ceiling on /1.0/catalog/products,
# independent of what total_results itself claims. Verified live, us
# region: descending -ReleaseDate plateaus at 500 distinct results for
# both Arthur Conan Doyle and Agatha Christie (total_results 1138), page
# 11 byte-identical to page 10; Christie's ascending ReleaseDate
# plateaus one short of that, at 499, also from page 11 on; Brandon
# Sanderson's real total of 153 stays under the ceiling and paginates
# normally. The endpoint never 404s or comes back short past the
# ceiling -- it returns HTTP 200 with the prior page's content repeating
# indefinitely. This bounds pages requested per sort (see
# _fetch_author_books_by_catalog) rather than trusting total_results
# past it, the same principle NAME_SEARCH's own
# total_results-vs-repeated-signature check applies to the single-sort
# walk above.
CATALOG_RESULT_CEILING = 500

# Attribution tiers a catalog product can land in -- see
# _classify_catalog_product. Counted separately in _CatalogBooksResult so
# get_author_books can log how often each tier fired; how often
# attribution falls back to a name match (rather than an authoritative
# ASIN match) is worth knowing on its own.
_CATALOG_TIER_ASIN_MATCH = "asin_match"
_CATALOG_TIER_ASIN_REJECT = "asin_reject"
_CATALOG_TIER_NAME_MATCH = "name_match"
_CATALOG_TIER_NAME_REJECT = "name_reject"


def _classify_catalog_product(
    product: dict, author_asin: str, author_name: str | None
) -> str:
    """
    Tiered author attribution for a single catalog product. An ASIN match
    is authoritative and never loses to a name mismatch elsewhere on the
    same product, but a product carrying no author ASIN at all must not
    be excluded outright -- verified live, catalog products carry author
    ASINs (`{"asin": "B000APENBC", "name": "Agatha Christie"}`) but not
    always; some carry names only.

    Order of decision:
      1. any author entry's asin equals author_asin (case-insensitive)
         -> _CATALOG_TIER_ASIN_MATCH, authoritative inclusion.
      2. a case-insensitive name match against the resolved author name,
         checked regardless of whether the product carries any author
         ASINs at all -> _CATALOG_TIER_NAME_MATCH, inclusion. Audible
         commonly lists the same author under a second, alias contributor
         ASIN on a given product; a product like that still belongs to
         this author's catalog, and there is no DB backstop for it --
         hydration would have written its pivot under that other Author
         row entirely, so this is the only tier that can ever surface it.
      3. the product carries at least one author ASIN and none of the
         above matched -> _CATALOG_TIER_ASIN_REJECT, exclusion: Audible
         told us who wrote this, by ASIN and by name, and it is not the
         requested author.
      4. no author entry carries any ASIN and no name matched ->
         _CATALOG_TIER_NAME_REJECT, exclusion.
    """
    authors = product.get("authors")
    if not isinstance(authors, list):
        authors = []

    required = author_asin.upper()
    any_asin_present = False
    for entry in authors:
        if not isinstance(entry, dict):
            continue
        entry_asin = entry.get("asin")
        if isinstance(entry_asin, str) and entry_asin:
            any_asin_present = True
            if entry_asin.upper() == required:
                return _CATALOG_TIER_ASIN_MATCH

    if author_name:
        name_lower = author_name.strip().lower()
        for entry in authors:
            entry_name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(entry_name, str) and entry_name.strip().lower() == name_lower:
                return _CATALOG_TIER_NAME_MATCH

    if any_asin_present:
        return _CATALOG_TIER_ASIN_REJECT

    return _CATALOG_TIER_NAME_REJECT


async def _fetch_catalog_page(name: str, region: str, page: int, sort: str) -> dict:
    """
    Fetches a single page of the catalog author-name search for a given
    sort order, scoped to only what discovery needs. Raises on failure;
    the caller decides what a failed page means for the walk.

    response_groups is limited to contributors -- discovery only needs
    asin plus author-attribution data, never the full response-group set
    hydration (get_books_by_asins) requests. Fetching that here would be
    both wasted work and a shape this function has no use for; hydration
    still owns the DTO.
    """
    path = "/1.0/catalog/products"
    params = {
        "author": name,
        "num_results": CATALOG_PAGE_SIZE,
        "page": page,
        "response_groups": "contributors",
        "products_sort_by": sort,
    }
    return await audible_get(region, path, params)


@dataclass
class _CatalogBooksResult:
    """
    asins is ordered by _CATALOG_SORTS' own priority: every ASIN from
    -ReleaseDate's pages first (page 0 through however many further pages
    that sort needed, in ascending page order), then ascending
    ReleaseDate's pages the same way, then -Title's -- this is what lets
    get_author_books preserve the compat-critical -ReleaseDate-first
    prefix without a separate sort step. Wave 3 fetches every needed
    sort's remaining pages together in a single gather for speed, but
    folding them into asins is a separate step done strictly one sort at
    a time, in _CATALOG_SORTS order -- fetch concurrency and fold order
    are deliberately decoupled, since firing the fetches together does
    not by itself guarantee they land in this order.

    sort_errors carries one entry per page fetch that raised, prefixed
    with which sort/page it was, so a partial catalog failure is tellable
    from a clean walk that simply didn't need every sort.

    ceiling_saturated is True when at least one sort's own pages were
    observed to plateau -- Audible re-serving an earlier page's exact
    content instead of new results (verified live: Conan Doyle's
    -ReleaseDate page 11 is byte-identical to page 10; Christie's
    -ReleaseDate and ascending ReleaseDate both plateau the same way, also
    at page 11) -- while the union this walk produced still falls short of
    total_results, upstream's own claim. This is a measured signal, read
    off what the walk actually observed on the wire, not an arithmetic
    threshold computed from total_results and CATALOG_RESULT_CEILING alone
    -- that measurement was taken in the us store only, so a region whose
    real deep-paging ceiling differs would make an arithmetic-only
    threshold wrong in either direction: too low flags a walk that
    genuinely finished, too high misses one that silently stopped short.
    """

    asins: list[str]
    pages_fetched: int
    total_results: int | None
    sorts_used: int
    asin_match_count: int
    asin_reject_count: int
    name_match_count: int
    name_reject_count: int
    sort_errors: list[str]
    truncated_by_deadline: bool = False
    ceiling_saturated: bool = False


def _catalog_page_signature(data: Any) -> tuple[Any, ...] | None:
    """
    Raw per-page content signature for plateau detection: every product's
    ASIN, in page order, before any attribution filtering -- Audible's
    deep-paging plateau repeats a page's exact content regardless of which
    of those products would go on to pass or fail _classify_catalog_product,
    so the signature has to be taken from the unfiltered page, not from
    what _process_catalog_page ends up accepting.

    Returns None for a page that can't be compared at all (not a dict, or
    no products list) rather than an empty tuple, so a fetch failure or a
    malformed response is never mistaken for two genuinely identical pages
    of content.
    """
    if not isinstance(data, dict):
        return None
    products = data.get("products")
    if not isinstance(products, list):
        return None
    return tuple(p.get("asin") if isinstance(p, dict) else None for p in products)


def _process_catalog_page(
    data: Any,
    author_asin: str,
    author_name: str | None,
    seen: set[str],
    asins: list[str],
    counts: dict[str, int],
) -> None:
    """
    Applies tiered attribution (_classify_catalog_product) to every
    product on one already-fetched catalog page, appending accepted,
    upper-cased, deduped ASINs to asins in-place and tallying every
    tier's count in counts, including the rejected ones -- rejections are
    not silent, they are how get_author_books reports the attribution
    breakdown.
    """
    if not isinstance(data, dict):
        return
    products = data.get("products")
    if not isinstance(products, list):
        return
    for product in products:
        if not isinstance(product, dict):
            continue
        tier = _classify_catalog_product(product, author_asin, author_name)
        counts[tier] = counts.get(tier, 0) + 1
        if tier not in (_CATALOG_TIER_ASIN_MATCH, _CATALOG_TIER_NAME_MATCH):
            continue
        asin = product.get("asin")
        # Truthy-only, not is_valid_asin: catalog products include
        # ISBN-keyed records whose asin field is not a 10-char B-format
        # ASIN. Those are a real, already-known class of record (see
        # client.py's ~84k-record note) and consumers already receive them
        # from this source -- rejecting them here would be exactly the
        # data loss the less-data-never-accepted invariant exists to stop.
        # _accept_name_search_products applies this same rule for the same
        # reason -- both read /1.0/catalog/products.
        if not isinstance(asin, str) or not asin:
            continue
        asin = asin.upper()
        if asin not in seen:
            seen.add(asin)
            asins.append(asin)


async def _fetch_author_books_by_catalog(
    author_asin: str,
    author_name: str,
    region: str,
    deadline: float | None = None,
) -> _CatalogBooksResult:
    """
    Fetches book ASINs for an author from the catalog endpoint across
    every trusted sort order, ASIN-attributed via _classify_catalog_product
    rather than name-matched alone -- the catalog has no author-ASIN
    filter (verified live: author_asin, authorAsin, contributor_asin, and
    author_id are all silently ignored, returning the entire 74k-item
    catalogue while looking like success), so author_name is what scopes
    the query and author_asin is what scopes which of its results belong
    to this author once results come back.

    Two waves, matching get_author_books' own wave numbering:

    - Wave 2 (here: page 0 of every sort in _CATALOG_SORTS, fired
      together): the only way to learn total_results, and every sort's
      page 0 is fetched regardless of how many sorts end up being needed
      -- the request is already paid for, so its products are always
      processed (see below), even for a sort beyond sorts_needed.
    - Wave 3 (here: every further page every needed sort requires, fired
      together in one gather -- no per-walk concurrency bound is applied;
      the shared client throttles this globally): sorts_needed is
      ceil(total_results / CATALOG_RESULT_CEILING), capped at
      len(_CATALOG_SORTS) -- the number of trusted sort orders that exist
      -- and pages per sort is ceil(min(total_results,
      CATALOG_RESULT_CEILING) / CATALOG_PAGE_SIZE), since deep paging
      caps at CATALOG_RESULT_CEILING regardless of what total_results
      itself claims (see that constant's docstring).

    Every page-0 response already fetched in wave 2 is processed
    unconditionally, even for a sort past sorts_needed -- data already
    paid for from a live request is never discarded. Only the *further*
    pages of a sort past sorts_needed are skipped.

    Fetching and folding are deliberately different orders: every sort's
    page 0 (wave 2) and every needed sort's further pages (wave 3) are
    each fired together in their own single gather, for speed, but the
    responses are folded into asins strictly one sort at a time, in
    _CATALOG_SORTS priority order -- a sort's page 0 and every further
    page it needed, in ascending page order, before the next sort's page
    0 is touched at all. Firing requests together does not by itself
    produce that order, so it is enforced as a separate fold step; this
    is what keeps -ReleaseDate an unbroken prefix of asins (see
    _CatalogBooksResult's own docstring) rather than interleaved with
    another sort's page 0.

    A single page's fetch failure is recorded in sort_errors and does not
    stop the rest of the walk; every other page, sort, and the page-0
    wave are unaffected -- a source that fails must not fail the whole
    request (see get_author_books' own per-source failure handling for
    the same principle one level up).

    deadline, when given, is an absolute time.monotonic() bound: checked
    once before wave 2 (an already-passed deadline skips catalog
    entirely, returning an empty, deadline-truncated result) and once
    while building the wave-3 page list (pages beyond the deadline are
    never requested at all, not merely abandoned mid-flight).

    total_results is read from whichever sort's page 0 reports it first
    in _CATALOG_SORTS' own priority order -- every sort queries the same
    author name, so the count is the same query surfaced through a
    different sort, not a per-sort quantity.

    ceiling_saturated (see _CatalogBooksResult) is derived in the fold
    step below from what the walk actually observed -- a sort's own pages
    plateauing (_catalog_page_signature) compared against total_results --
    not from CATALOG_RESULT_CEILING arithmetic alone; that constant is
    used only above, to bound sorts_needed and pages_needed, the page
    budget this walk is allowed to spend.
    """
    seen: set[str] = set()
    asins: list[str] = []
    counts: dict[str, int] = {}
    sort_errors: list[str] = []
    pages_fetched = 0

    if deadline is not None and time.monotonic() >= deadline:
        return _CatalogBooksResult(
            asins=[],
            pages_fetched=0,
            total_results=None,
            sorts_used=0,
            asin_match_count=0,
            asin_reject_count=0,
            name_match_count=0,
            name_reject_count=0,
            sort_errors=[],
            truncated_by_deadline=True,
        )

    # Wave 2: page 0 of every trusted sort order, fired together.
    page0_outcomes = await asyncio.gather(
        *(_fetch_catalog_page(author_name, region, 0, sort) for sort in _CATALOG_SORTS),
        return_exceptions=True,
    )

    total_results: int | None = None
    page0_data: list[dict | None] = []
    for sort, outcome in zip(_CATALOG_SORTS, page0_outcomes):
        if isinstance(outcome, BaseException):
            sort_errors.append(f"{sort} page 0: {type(outcome).__name__}: {outcome}")
            page0_data.append(None)
            continue
        pages_fetched += 1
        data = outcome if isinstance(outcome, dict) else None
        page0_data.append(data)
        if total_results is None and isinstance(data, dict):
            candidate = data.get("total_results")
            if isinstance(candidate, int) and candidate >= 0:
                total_results = candidate

    if total_results is not None:
        sorts_needed = min(
            len(_CATALOG_SORTS),
            max(1, -(-total_results // CATALOG_RESULT_CEILING)),
        )
    else:
        sorts_needed = 1

    # Wave 3: every remaining page every needed sort requires, fired
    # together in a single gather -- see docstring for why no per-walk
    # concurrency bound is applied here. Fetched here, but not folded into
    # asins yet -- see the fold loop below for why.
    wave3_targets: list[tuple[str, int]] = []
    truncated_by_deadline = False
    capped_total = (
        min(total_results, CATALOG_RESULT_CEILING) if total_results is not None else CATALOG_PAGE_SIZE
    )
    pages_needed = -(-capped_total // CATALOG_PAGE_SIZE)
    for sort in _CATALOG_SORTS[:sorts_needed]:
        for page in range(1, pages_needed):
            if deadline is not None and time.monotonic() >= deadline:
                truncated_by_deadline = True
                break
            wave3_targets.append((sort, page))
        if truncated_by_deadline:
            break

    wave3_outcomes_by_target: dict[tuple[str, int], Any] = {}
    if wave3_targets:
        wave3_outcomes = await asyncio.gather(
            *(_fetch_catalog_page(author_name, region, page, sort) for sort, page in wave3_targets),
            return_exceptions=True,
        )
        wave3_outcomes_by_target = dict(zip(wave3_targets, wave3_outcomes))

    # Fold order is the ordering contract: every page-0 response already
    # fetched is processed, from every sort, since the request has
    # already been made -- but strictly one sort at a time, in
    # _CATALOG_SORTS priority order, a sort's own page 0 followed
    # immediately by every further page that same sort needed, before the
    # next sort's page 0 is touched at all. Wave 2 and wave 3 were each
    # fired together purely for fetch speed; folding them in fetch-
    # completion or gather-return order instead of this one would let a
    # later sort's page 0 land ahead of an earlier sort's own further
    # pages, breaking -ReleaseDate's unbroken-prefix guarantee (see
    # _CatalogBooksResult's docstring).
    # sort_plateaued tracks, across every sort folded below, whether any
    # one of them was caught re-serving an earlier page's exact raw
    # content -- Audible's own deep-paging ceiling, observed on the wire,
    # rather than assumed from CATALOG_RESULT_CEILING. Compared per sort
    # against that sort's own previous page in ascending order (seeded
    # with page 0's signature), the same "current page repeats the one
    # immediately before it" check the name-search and screens walks each
    # use for their own plateau detection. A page whose signature can't be
    # read (fetch failed, non-dict, no products list -- see
    # _catalog_page_signature) never counts as a repeat of anything and
    # is never itself compared against, so a fetch failure can't be
    # mistaken for two genuinely identical pages of content.
    sort_plateaued = False
    for index, sort in enumerate(_CATALOG_SORTS):
        _process_catalog_page(page0_data[index], author_asin, author_name, seen, asins, counts)
        if index >= sorts_needed:
            continue
        previous_signature = _catalog_page_signature(page0_data[index])
        for page in range(1, pages_needed):
            target = (sort, page)
            if target not in wave3_outcomes_by_target:
                continue
            outcome = wave3_outcomes_by_target[target]
            if isinstance(outcome, BaseException):
                sort_errors.append(f"{sort} page {page}: {type(outcome).__name__}: {outcome}")
                continue
            pages_fetched += 1
            page_signature = _catalog_page_signature(outcome)
            if (
                page_signature
                and previous_signature is not None
                and page_signature == previous_signature
            ):
                sort_plateaued = True
            if page_signature is not None:
                previous_signature = page_signature
            _process_catalog_page(outcome, author_asin, author_name, seen, asins, counts)

    # Measured, not assumed (see _CatalogBooksResult.ceiling_saturated):
    # true only when a sort was actually observed to plateau AND the union
    # it fed still falls short of what upstream itself claimed via
    # total_results. CATALOG_RESULT_CEILING never enters this predicate --
    # it only ever bounded the page budget above, in sorts_needed and
    # pages_needed.
    ceiling_saturated = (
        sort_plateaued
        and total_results is not None
        and len(asins) < total_results
    )

    return _CatalogBooksResult(
        asins=asins,
        pages_fetched=pages_fetched,
        total_results=total_results,
        sorts_used=sorts_needed,
        asin_match_count=counts.get(_CATALOG_TIER_ASIN_MATCH, 0),
        asin_reject_count=counts.get(_CATALOG_TIER_ASIN_REJECT, 0),
        name_match_count=counts.get(_CATALOG_TIER_NAME_MATCH, 0),
        name_reject_count=counts.get(_CATALOG_TIER_NAME_REJECT, 0),
        sort_errors=sort_errors,
        truncated_by_deadline=truncated_by_deadline,
        ceiling_saturated=ceiling_saturated,
    )
