"""
Audible author-books catalog walks.
Fetches an author's book ASINs from the /1.0/catalog/products endpoint --
both the single-sort, name-only walk (_fetch_author_books_by_name_detailed)
and the ASIN-attributed, category-sliced walk (_fetch_author_books_by_catalog).
Both hit the same endpoint, differing only in response_groups and the sort/
category_id params sent, so they stay together in this module rather than two
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
# CATALOG WINDOWED WALK (get_author_books' 3 of its 4 sources)
#
# Distinct from _fetch_author_books_by_name_detailed above, which stays a
# single-sort, name-only walk serving get_author_books_by_name and the
# seeder's per-author expansion loop, neither of which has an author ASIN
# to attribute against. get_author_books does have the ASIN, and probed
# live, the catalog is neither a subset of the screens grid nor complete
# on its own -- and, further, Audible's deep-paging ceiling (see
# CATALOG_RESULT_CEILING) turned out to apply per (products_sort_by,
# category_id) pair, not per author: two different sorts over the SAME
# unfiltered query open disjoint windows into the same result set (for
# Agatha Christie, total_results 1138, descending -ReleaseDate plateaus
# at 500 distinct results and ascending ReleaseDate plateaus at a
# DIFFERENT 499, zero overlap), and scoping the same sort to a
# category_id the author's own books actually carry opens ANOTHER,
# separately-ceilinged window (measured against Arthur Conan Doyle,
# total_results 4501 us: five sorts over one fat category alone reached
# roughly 1850 distinct results, well past what five unfiltered sorts
# could ever reach against the same 500-per-sort ceiling). This walk
# spends its request budget accordingly: two cheap, disjoint unfiltered
# sorts first, then further sorts only inside whichever categories the
# author's own books were actually seen to carry and that a one-sort
# probe showed still had new content to give -- see
# _fetch_author_books_by_catalog's own docstring for the full sequence.
# ============================================================

# Priority order matters here: -ReleaseDate first is compat-critical.
# Consumers have always received the -ReleaseDate list at the front of
# the response and must keep receiving that same list, in that same
# order, unmoved at the front of the union -- see get_author_books.
# Ascending ReleaseDate is the bare field name, not "+ReleaseDate" --
# probed live, the leading "+" 400s. -Title, Title and Relevance are the
# only other sorts trusted enough to spend a request on -- every other
# spelling tried (BestSellers, Runtime, Price, Rating, Popularity,
# PurchaseDate in either direction) either silently duplicates Relevance
# or 400s outright, so there is no sixth sort to add here.
_CATALOG_SORTS: tuple[str, ...] = ("-ReleaseDate", "ReleaseDate", "-Title", "Title", "Relevance")

# The two cheap, provably disjoint sorts spent unfiltered on every author,
# regardless of size -- see the module-level walk's Phase 1. Kept to just
# these two (not all five) unfiltered: a third, fourth and fifth unfiltered
# sort would cost as much as this walk's entire category-probing phase
# while -- unlike a category window -- never opening a genuinely new slice
# of a small-to-medium author's results, which the unfiltered pair (or
# even one of them alone) has already fully captured.
_CATALOG_BASELINE_SORTS: tuple[str, ...] = _CATALOG_SORTS[:2]

# The sort a candidate category is first tried with (see
# _fetch_author_books_by_catalog's Phase 3) -- deliberately NOT one of
# _CATALOG_BASELINE_SORTS. A first attempt reused -ReleaseDate here and
# measured badly wrong for exactly the case that matters most: an author
# whose catalog is dominated by one genre has a "mega-category" covering
# nearly every book they've written, and that category's own -ReleaseDate
# window is then nearly identical to Phase 1's unfiltered -ReleaseDate
# window already folded in -- both are "this author's most recent books",
# scoped or not. Measured live against Arthur Conan Doyle: his three
# highest-frequency harvested categories (a mega-category and its two
# nested children, together covering the overwhelming majority of his
# baseline products) each probed at exactly 0 new ASINs under
# -ReleaseDate, immediately tripping CATALOG_DRY_STREAK_LIMIT and ending
# the walk before it reached a single category actually worth spending
# on. -Title is untouched by Phase 1 and orthogonal to recency, so it
# does not share that bias regardless of how much of the catalog a
# candidate category covers.
_CATALOG_CATEGORY_PROBE_SORT: str = "-Title"

# The remaining sorts a category earns once its probe shows it has real new
# content to give (see _fetch_author_books_by_catalog's Phase 4) -- every
# _CATALOG_SORTS entry except whichever one Phase 3 already spent as the
# probe, in the same priority order.
_CATALOG_CATEGORY_SPEND_SORTS: tuple[str, ...] = tuple(
    sort for sort in _CATALOG_SORTS if sort != _CATALOG_CATEGORY_PROBE_SORT
)

CATALOG_PAGE_SIZE = 50

# Audible's observed deep-paging ceiling on /1.0/catalog/products,
# independent of what total_results itself claims, and independent of
# category_id -- it applies per (products_sort_by, category_id) pair, not
# per author or per query shape (see the section banner above). Verified
# live, us region: descending -ReleaseDate plateaus at 500 distinct
# results for both Arthur Conan Doyle and Agatha Christie (unfiltered
# total_results 1138), page 11 byte-identical to page 10; Christie's
# ascending ReleaseDate plateaus one short of that, at 499, also from page
# 11 on; a category-scoped query plateaus the same way once its own
# results run past 500, at the same page 11 boundary. Brandon Sanderson's
# real total of 153 stays under the ceiling on every sort and paginates
# normally. The endpoint never 404s or comes back short past the ceiling
# -- it returns HTTP 200 with the prior page's content repeating
# indefinitely. This bounds pages requested per window (unfiltered sort or
# category+sort pair alike -- see _pages_needed_for) rather than trusting
# total_results past it, the same principle NAME_SEARCH's own
# total_results-vs-repeated-signature check applies to the single-sort
# walk above.
CATALOG_RESULT_CEILING = 500

# A window (Phase 1's unfiltered sorts, a Phase 3 probe, or a Phase 4
# spend) whose fold adds fewer than this many ASINs the walk hadn't
# already seen is "dry" for the purposes of CATALOG_DRY_STREAK_LIMIT
# below. Measured live against Arthur Conan Doyle: a nested category's
# probe (Mystery, a child of the already-probed Mystery, Thriller &
# Suspense) added only 21 new ASINs -- explicitly the "near worthless"
# case this threshold exists to catch -- while every category actually
# worth its remaining sorts added well over 200. 50 sits comfortably
# between the two with margin on both sides, rather than at either
# measured value itself.
CATALOG_DRY_WINDOW_MIN_NEW = 50

# How many consecutive dry windows (see CATALOG_DRY_WINDOW_MIN_NEW) end the
# walk's exploration of further, lower-ranked categories -- see
# _fetch_author_books_by_catalog's Phase 3. Not measured directly (the live
# trace behind this feature never produced two dry probes back to back),
# so this is a deliberate margin rather than a fitted value: one dry
# category alone (a nested subcategory whose parent was already probed, the
# single case actually observed) must not end the walk early and cost a
# real, separately-ceilinged category its chance, but the walk still has to
# stop somewhere once the signal has genuinely dried up. 3 gives that
# margin cheaply -- see the Phase 3 docstring for why a wider streak here
# costs wall-clock time only in the batch it appears in, not per category.
CATALOG_DRY_STREAK_LIMIT = 3

# Cap on how many of the ranked candidates (see Phase 2) Phase 3 will ever
# probe. Unlike every other bound in this codebase, this one is routinely
# expected to bind on real, large multi-genre authors, not just a
# pathological upstream: measured live, Arthur Conan Doyle's baseline pages
# alone surface on the order of 120 distinct category ladder rungs (every
# level of every ladder on every accepted product -- see
# _harvest_category_ladders), the overwhelming majority of them niche
# leaves a single product happens to carry. Because Phase 2 ranks
# candidates by descending frequency before this cap is applied, truncating
# here drops exactly that long, low-frequency tail first -- a rung seen on
# one or two products is both the least likely to be a real, separately-
# ceilinged category worth a request and the least likely to ever ranked-in
# ahead of a genuinely fat one. This is why hitting this cap does NOT, on
# its own, mark _CatalogBooksResult.slicing_incomplete -- see that field's
# own docstring.
CATALOG_MAX_CANDIDATE_CATEGORIES = 40

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


async def _fetch_catalog_page(
    name: str,
    region: str,
    page: int,
    sort: str,
    category_id: str | None = None,
    include_categories: bool = False,
) -> dict:
    """
    Fetches a single page of the catalog author-name search for a given
    sort order, optionally scoped to a category_id, scoped to only what
    discovery needs. Raises on failure; the caller decides what a failed
    page means for the walk.

    response_groups is limited to contributors -- plus category_ladders
    when include_categories is set, for the unfiltered baseline pages
    _fetch_author_books_by_catalog harvests candidate categories from --
    discovery only needs asin, author-attribution data and, for those
    baseline pages, category placement; never the full response-group set
    hydration (get_books_by_asins) requests. Fetching that here would be
    both wasted work and a shape this function has no use for; hydration
    still owns the DTO.
    """
    path = "/1.0/catalog/products"
    params: dict[str, Any] = {
        "author": name,
        "num_results": CATALOG_PAGE_SIZE,
        "page": page,
        "response_groups": "contributors, category_ladders" if include_categories else "contributors",
        "products_sort_by": sort,
    }
    if category_id:
        params["category_id"] = category_id
    return await audible_get(region, path, params)


@dataclass
class _CatalogBooksResult:
    """
    asins is ordered by fold order, not fetch order: Phase 1's
    -ReleaseDate window in full (page 0 through however many further
    pages it needed, in ascending page order), then Phase 1's ReleaseDate
    window the same way, then -- only for an author whose baseline
    plateaued or over-claimed total_results (see
    _fetch_author_books_by_catalog) -- every category window Phases 3 and
    4 folded in, each in ranked-candidate-then-sort-priority order. This
    is what lets get_author_books preserve the compat-critical
    -ReleaseDate-first prefix without a separate sort step: every window
    is fetched concurrently with its siblings for speed, but folded into
    asins strictly one window at a time, in this order -- fetch
    concurrency and fold order are deliberately decoupled throughout this
    walk, since firing requests together does not by itself guarantee
    they land in this order.

    sort_errors carries one entry per page fetch that raised, prefixed
    with which window/page it was, so a partial catalog failure is
    tellable from a clean walk that simply didn't need every window.

    truncated_by_deadline is True when the caller's deadline cut the walk
    short before it reached a natural end -- a real interruption, not the
    walk's own designed stopping point (see CATALOG_DRY_STREAK_LIMIT).

    slicing_incomplete is True only when the walk determined it needed to
    slice by category (the baseline plateaued or over-claimed
    total_results) but found literally nothing to slice with -- no
    category ever surfaced on a baseline page at all, which for an author
    large enough to need slicing points at something broken (a response-
    shape change upstream, category_ladders silently empty) rather than a
    genuinely uncategorized catalog. It is deliberately NOT set merely
    because CATALOG_MAX_CANDIDATE_CATEGORIES capped how many ranked
    candidates got probed -- see that constant's own docstring for why
    that cap routinely binds on a real, large multi-genre author and does
    not mean anything was missed: Phase 2's frequency ranking already
    tries the candidates most likely to matter first, and Phase 3's dry
    streak already self-limits how far down that ranked list is worth
    going. categories_harvested (the pre-cap count) and
    categories_considered (the post-cap count actually probed) are both
    logged so that gap stays visible without gating completeness on it.

    sliced is True whenever Phase 3/4 category slicing ran at all, purely
    informational (logged, never used to gate completeness) -- distinct
    from slicing_incomplete, which only fires when slicing was needed but
    nothing was found to slice with.
    """

    asins: list[str]
    pages_fetched: int
    total_results: int | None
    sort_errors: list[str]
    truncated_by_deadline: bool = False
    slicing_incomplete: bool = False
    sliced: bool = False
    categories_harvested: int = 0
    categories_considered: int = 0
    categories_expanded: int = 0
    windows_used: int = 0
    asin_match_count: int = 0
    asin_reject_count: int = 0
    name_match_count: int = 0
    name_reject_count: int = 0


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


def _harvest_category_ladders(
    product: dict, frequency: dict[str, int], names: dict[str, str]
) -> None:
    """
    Tallies every rung of an already-accepted baseline product's
    category_ladders into frequency (category_id -> how many of this
    author's own baseline products carried it) and records each id's
    display name the first time it's seen, in names -- this is the ranking
    signal _fetch_author_books_by_catalog's Phase 2 sorts candidate
    categories by.

    Every rung at every level counts, not just the leaf: a ladder runs
    top (a broad genre) to bottom (a narrow subcategory), and a broad
    parent naturally accumulates a higher count than its own children
    simply because more of the author's products carry it -- which is
    exactly what should rank it as a candidate ahead of its children,
    without this module ever having to model the ladder's own tree shape
    to get that ordering (see the Phase 2/3 docstrings for why the walk
    deliberately does not).
    """
    ladders = product.get("category_ladders")
    if not isinstance(ladders, list):
        return
    for ladder in ladders:
        if not isinstance(ladder, dict):
            continue
        rungs = ladder.get("ladder")
        if not isinstance(rungs, list):
            continue
        for rung in rungs:
            if not isinstance(rung, dict):
                continue
            category_id = rung.get("id")
            if not isinstance(category_id, str) or not category_id:
                continue
            frequency[category_id] = frequency.get(category_id, 0) + 1
            name = rung.get("name")
            if category_id not in names and isinstance(name, str) and name:
                names[category_id] = name


def _process_catalog_page(
    data: Any,
    author_asin: str,
    author_name: str | None,
    seen: set[str],
    asins: list[str],
    counts: dict[str, int],
    category_frequency: dict[str, int] | None = None,
    category_names: dict[str, str] | None = None,
) -> None:
    """
    Applies tiered attribution (_classify_catalog_product) to every
    product on one already-fetched catalog page, appending accepted,
    upper-cased, deduped ASINs to asins in-place and tallying every
    tier's count in counts, including the rejected ones -- rejections are
    not silent, they are how get_author_books reports the attribution
    breakdown.

    category_frequency, when given (only for Phase 1's baseline pages --
    see _fetch_author_books_by_catalog), also harvests every accepted
    product's category_ladders via _harvest_category_ladders. Harvesting
    only from accepted products, never rejected ones, keeps a name-search
    false positive (a same-named but different author) from polluting
    this author's own category signal.
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
        if category_frequency is not None:
            # `category_names if category_names is not None else {}`, not
            # `category_names or {}` -- an already-populated dict is still
            # falsy once it happens to be empty on a given call, and `or`
            # would silently swap it for a throwaway that's discarded the
            # moment this call returns, losing every name harvested so far.
            _harvest_category_ladders(
                product, category_frequency, category_names if category_names is not None else {}
            )
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


@dataclass(frozen=True)
class _CatalogWindow:
    """
    One (category_id, products_sort_by) pair -- the atomic unit this walk
    spends a request budget on. category_id is None for Phase 1's two
    unfiltered sorts; harvest is True only for those same two, since
    category candidates are only ever mined from the baseline (see
    _fetch_author_books_by_catalog's Phase 2). Frozen and hashable so a
    window can key the outcome dicts the batch helpers below return.
    """

    category_id: str | None
    sort: str
    harvest: bool = False


def _window_label(window: _CatalogWindow) -> str:
    """Formats a window for a sort_errors entry -- category-qualified for
    a category window, bare for a Phase 1 unfiltered one."""
    if window.category_id is None:
        return window.sort
    return f"category {window.category_id} {window.sort}"


def _pages_needed_for(total_results: int | None) -> int:
    """
    Pages one window needs to reach CATALOG_RESULT_CEILING, given that
    window's own page-0 total_results claim. Absent a usable total_results
    (missing, wrong type, or negative), falls back to a single page --
    there is nothing else to size a further-page batch from, and firing a
    speculative multi-page batch for a window whose real size is unknown
    would risk paying for empty pages past whatever this window's real
    total is. Deep paging caps at CATALOG_RESULT_CEILING regardless of
    what total_results itself claims (see that constant's docstring), so
    total_results is capped before the page count is derived from it.
    """
    capped = min(total_results, CATALOG_RESULT_CEILING) if total_results is not None else CATALOG_PAGE_SIZE
    return -(-capped // CATALOG_PAGE_SIZE)


async def _fetch_window_page0_batch(
    author_name: str, region: str, windows: list[_CatalogWindow]
) -> dict[_CatalogWindow, Any]:
    """Fetches page 0 of every window in one gather, keyed back by window
    so the caller can look up any one window's outcome (a dict response,
    or the exception it raised) without depending on gather's return
    order lining up with anything but the input list, which zip already
    guarantees regardless."""
    outcomes = await asyncio.gather(
        *(
            _fetch_catalog_page(
                author_name, region, 0, w.sort,
                category_id=w.category_id, include_categories=w.harvest,
            )
            for w in windows
        ),
        return_exceptions=True,
    )
    return dict(zip(windows, outcomes))


async def _fetch_window_rest_batch(
    author_name: str,
    region: str,
    targets: list[tuple[_CatalogWindow, int]],
) -> dict[tuple[_CatalogWindow, int], Any]:
    """Fetches every (window, page) pair in targets in one gather, keyed
    back the same way _fetch_window_page0_batch is. An empty targets list
    (every window in this round was small enough that page 0 was already
    the whole window) skips the gather call entirely rather than firing
    one for nothing."""
    if not targets:
        return {}
    outcomes = await asyncio.gather(
        *(
            _fetch_catalog_page(
                author_name, region, page, w.sort,
                category_id=w.category_id, include_categories=w.harvest,
            )
            for w, page in targets
        ),
        return_exceptions=True,
    )
    return dict(zip(targets, outcomes))


async def _fetch_author_books_by_catalog(
    author_asin: str,
    author_name: str,
    region: str,
    deadline: float | None = None,
) -> _CatalogBooksResult:
    """
    Fetches book ASINs for an author from the catalog endpoint, ASIN-
    attributed via _classify_catalog_product rather than name-matched
    alone -- the catalog has no author-ASIN filter (verified live:
    author_asin, authorAsin, contributor_asin, and author_id are all
    silently ignored, returning the entire 74k-item catalogue while
    looking like success), so author_name is what scopes the query and
    author_asin is what scopes which of its results belong to this
    author once results come back.

    Audible's deep-paging ceiling (CATALOG_RESULT_CEILING, measured at
    500 distinct results) turned out to apply per (products_sort_by,
    category_id) pair, not per author -- see the module-level section
    banner above. This walk exploits that in four phases, spending more
    of its request budget only on an author actually large enough to need
    it:

    Phase 1 (baseline, always run): the two cheap, provably disjoint
    unfiltered sorts in _CATALOG_BASELINE_SORTS (-ReleaseDate, then
    ReleaseDate), each windowed to CATALOG_RESULT_CEILING. category_ladders
    is harvested from every accepted product on these pages (see
    _process_catalog_page) -- free, no extra requests, since the pages
    were already being fetched for their ASINs. For any author whose real
    catalog stays under the ceiling (Brandon Sanderson's 203 titles,
    total_results well under CATALOG_RESULT_CEILING), this phase alone
    already has everything, and phases 2-4 below never run at all -- no
    author under every ceiling pays anything for slicing it doesn't need.

    Phase 1 also decides whether slicing is needed at all: only when the
    baseline's own total_results claim exceeds CATALOG_RESULT_CEILING, or
    a baseline sort's own pages were directly observed to plateau
    (Audible re-serving an earlier page's exact content -- see
    _catalog_page_signature), does this walk spend anything past Phase 1.

    Phase 2 (rank candidates, no requests): the harvested category ids are
    ranked by how many baseline products carried each one, descending,
    capped at CATALOG_MAX_CANDIDATE_CATEGORIES. This walk deliberately
    does not model the category ladder's own parent/child structure to
    order or prune this list -- a live trace showed it wouldn't help: a
    nested category can self-eliminate on a low probe yield (a strict
    subset largely already seen via its already-probed parent), but a
    MORE deeply nested category can also turn out to hold hundreds of
    genuinely new ASINs a shallower relative did not surface, because
    each sort order opens a different slice of that category's own
    separately-ceilinged result set -- hierarchy position doesn't predict
    that, only spending a request and observing it does (Phase 3).

    Phase 3 (probe, cheap-then-full): every ranked candidate's page 0 is
    fetched together in one gather -- this is the cheap tier, one request
    per candidate regardless of how many total this walk ends up ranking.
    A candidate whose page 0 adds not a single new ASIN over everything
    already seen is dropped here, at the cost of the one request already
    spent learning that -- a category whose first (and best-sorted) 50
    results are already fully known is not worth a further nine requests
    to confirm. Every surviving candidate's remaining pages (up to
    CATALOG_RESULT_CEILING, via _pages_needed_for) are then fetched
    together in a second gather -- the full-probe tier. Folded strictly in
    rank order, each candidate's total new-ASIN count (across both tiers)
    is compared against CATALOG_DRY_WINDOW_MIN_NEW: at or above, the
    category earns Phase 4; below, it's dry. CATALOG_DRY_STREAK_LIMIT
    consecutive dry candidates in this ranked fold stops the walk from
    considering any further, lower-ranked one -- see that constant's own
    docstring for why the whole batch is still fetched up front rather
    than probed one candidate at a time: every candidate in a single
    batch was already going to be paid for regardless of where the streak
    lands, so batching costs nothing a strictly sequential probe wouldn't
    also have cost, while turning what would be dozens of sequential
    round trips into two.

    Phase 4 (spend): every category that earned it in Phase 3 gets its
    remaining sorts (_CATALOG_CATEGORY_SPEND_SORTS) -- again fetched as
    two gathers (every window's page 0 together, then every window's
    remaining pages together) and folded in category-rank-then-sort-
    priority order. A category found to pay is never revisited or
    re-judged mid-Phase-4; only Phase 3's ranked-candidate exploration is
    what CATALOG_DRY_STREAK_LIMIT can cut short.

    Throughout every phase, fetching and folding are deliberately
    different orders -- every window in a round is fired together for
    speed, but folded into asins one window at a time in a fixed priority
    order (see _CatalogBooksResult's own docstring) -- and a page-0
    response already fetched is always processed, even for a window that
    turns out not to need (or not to have room in the deadline for) its
    remaining pages; data already paid for from a live request is never
    discarded.

    A single page's fetch failure is recorded in sort_errors and does not
    stop the rest of the walk -- every other page, window, and phase is
    unaffected (see get_author_books' own per-source failure handling for
    the same principle one level up).

    deadline, when given, is an absolute time.monotonic() bound: checked
    once before Phase 1 starts (an already-passed deadline skips the
    whole walk, returning an empty, deadline-truncated result) and once
    before each further gather this walk fires; a deadline crossed
    between rounds stops the walk from starting another one rather than
    letting it run to a natural end.

    total_results is read from whichever baseline sort's page 0 reports
    it first, in _CATALOG_BASELINE_SORTS' own priority order -- both
    sorts query the identical unfiltered author-name search, so the
    count is the same query surfaced through a different sort, not a
    per-sort quantity. It is never read from a category-scoped window; a
    category's own total_results describes only that category's slice,
    not the author's whole catalog, and would be the wrong claim for
    get_author_books' own completeness check against this field.
    """
    seen: set[str] = set()
    asins: list[str] = []
    counts: dict[str, int] = {}
    sort_errors: list[str] = []
    pages_fetched = 0

    def deadline_passed() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    if deadline_passed():
        return _CatalogBooksResult(
            asins=[],
            pages_fetched=0,
            total_results=None,
            sort_errors=[],
            truncated_by_deadline=True,
        )

    # ---- Phase 1: baseline, unfiltered, the two disjoint sorts ----
    baseline_windows = [_CatalogWindow(None, sort, harvest=True) for sort in _CATALOG_BASELINE_SORTS]
    baseline_page0 = await _fetch_window_page0_batch(author_name, region, baseline_windows)

    total_results: int | None = None
    baseline_totals: dict[_CatalogWindow, int | None] = {}
    for window in baseline_windows:
        outcome = baseline_page0[window]
        if isinstance(outcome, BaseException):
            sort_errors.append(f"{_window_label(window)} page 0: {type(outcome).__name__}: {outcome}")
            baseline_totals[window] = None
            continue
        pages_fetched += 1
        candidate_total = outcome.get("total_results") if isinstance(outcome, dict) else None
        baseline_totals[window] = candidate_total if isinstance(candidate_total, int) and candidate_total >= 0 else None
        if total_results is None and baseline_totals[window] is not None:
            total_results = baseline_totals[window]

    truncated_by_deadline = False
    baseline_rest_targets: list[tuple[_CatalogWindow, int]] = []
    for window in baseline_windows:
        pages_needed = _pages_needed_for(baseline_totals.get(window))
        for page in range(1, pages_needed):
            baseline_rest_targets.append((window, page))

    baseline_rest: dict[tuple[_CatalogWindow, int], Any] = {}
    if baseline_rest_targets:
        if deadline_passed():
            truncated_by_deadline = True
        else:
            baseline_rest = await _fetch_window_rest_batch(author_name, region, baseline_rest_targets)

    category_frequency: dict[str, int] = {}
    category_names: dict[str, str] = {}
    baseline_plateaued = False
    for window in baseline_windows:
        page0_outcome = baseline_page0[window]
        page0_data = page0_outcome if isinstance(page0_outcome, dict) else None
        _process_catalog_page(
            page0_data, author_asin, author_name, seen, asins, counts,
            category_frequency=category_frequency, category_names=category_names,
        )
        previous_signature = _catalog_page_signature(page0_data)
        pages_needed = _pages_needed_for(baseline_totals.get(window))
        for page in range(1, pages_needed):
            target = (window, page)
            if target not in baseline_rest:
                continue
            outcome = baseline_rest[target]
            if isinstance(outcome, BaseException):
                sort_errors.append(f"{_window_label(window)} page {page}: {type(outcome).__name__}: {outcome}")
                continue
            pages_fetched += 1
            signature = _catalog_page_signature(outcome)
            if signature and previous_signature is not None and signature == previous_signature:
                baseline_plateaued = True
            if signature is not None:
                previous_signature = signature
            _process_catalog_page(
                outcome, author_asin, author_name, seen, asins, counts,
                category_frequency=category_frequency, category_names=category_names,
            )

    # A baseline that neither over-claimed nor plateaued already has
    # everything this author has to give -- see Phase 1's own docstring.
    needs_slicing = baseline_plateaued or (
        total_results is not None and total_results > CATALOG_RESULT_CEILING
    )

    categories_harvested = 0
    categories_considered = 0
    categories_expanded = 0
    slicing_incomplete = False

    if needs_slicing and not truncated_by_deadline and not deadline_passed():
        # ---- Phase 2: rank harvested candidates, no requests ----
        ranked = sorted(category_frequency.items(), key=lambda kv: kv[1], reverse=True)
        candidate_ids = [category_id for category_id, _frequency in ranked[:CATALOG_MAX_CANDIDATE_CATEGORIES]]
        categories_harvested = len(ranked)
        categories_considered = len(candidate_ids)
        # See CATALOG_MAX_CANDIDATE_CATEGORIES and _CatalogBooksResult's own
        # docstrings: the cap truncating the ranked list is routine on a
        # real large author and not, on its own, evidence anything was
        # missed. Only a genuinely empty harvest despite needing to slice
        # counts as incomplete here.
        slicing_incomplete = not category_frequency

        if candidate_ids:
            # ---- Phase 3: cheap page-0 tier, every candidate at once ----
            probe_windows = [_CatalogWindow(cid, _CATALOG_CATEGORY_PROBE_SORT) for cid in candidate_ids]
            probe_page0 = await _fetch_window_page0_batch(author_name, region, probe_windows)

            probe_ok: set[_CatalogWindow] = set()
            probe_totals: dict[_CatalogWindow, int | None] = {}
            advancing: list[_CatalogWindow] = []
            for window in probe_windows:
                outcome = probe_page0[window]
                if isinstance(outcome, BaseException):
                    sort_errors.append(f"{_window_label(window)} page 0: {type(outcome).__name__}: {outcome}")
                    continue
                pages_fetched += 1
                probe_ok.add(window)
                candidate_total = outcome.get("total_results") if isinstance(outcome, dict) else None
                probe_totals[window] = candidate_total if isinstance(candidate_total, int) and candidate_total >= 0 else None
                before = len(seen)
                _process_catalog_page(outcome, author_asin, author_name, seen, asins, counts)
                if len(seen) > before:
                    advancing.append(window)

            # ---- Phase 3 continued: full-probe tier for surviving candidates ----
            probe_rest_targets: list[tuple[_CatalogWindow, int]] = []
            for window in advancing:
                pages_needed = _pages_needed_for(probe_totals.get(window))
                for page in range(1, pages_needed):
                    probe_rest_targets.append((window, page))

            probe_rest: dict[tuple[_CatalogWindow, int], Any] = {}
            if probe_rest_targets:
                if deadline_passed():
                    truncated_by_deadline = True
                else:
                    probe_rest = await _fetch_window_rest_batch(author_name, region, probe_rest_targets)

            paying: list[str] = []
            dry_streak = 0
            for window in probe_windows:
                if window not in probe_ok:
                    continue
                before = len(seen)
                if window in advancing:
                    pages_needed = _pages_needed_for(probe_totals.get(window))
                    for page in range(1, pages_needed):
                        target = (window, page)
                        if target not in probe_rest:
                            continue
                        outcome = probe_rest[target]
                        if isinstance(outcome, BaseException):
                            sort_errors.append(f"{_window_label(window)} page {page}: {type(outcome).__name__}: {outcome}")
                            continue
                        pages_fetched += 1
                        _process_catalog_page(outcome, author_asin, author_name, seen, asins, counts)
                new_count = len(seen) - before
                if new_count >= CATALOG_DRY_WINDOW_MIN_NEW:
                    dry_streak = 0
                    paying.append(window.category_id)
                else:
                    dry_streak += 1
                    if dry_streak >= CATALOG_DRY_STREAK_LIMIT:
                        break

            categories_expanded = len(paying)

            # ---- Phase 4: remaining sorts for every category that paid ----
            if paying:
                if truncated_by_deadline or deadline_passed():
                    truncated_by_deadline = True
                else:
                    expand_windows = [
                        _CatalogWindow(category_id, sort)
                        for category_id in paying
                        for sort in _CATALOG_CATEGORY_SPEND_SORTS
                    ]
                    expand_page0 = await _fetch_window_page0_batch(author_name, region, expand_windows)

                    expand_ok: set[_CatalogWindow] = set()
                    expand_totals: dict[_CatalogWindow, int | None] = {}
                    for window in expand_windows:
                        outcome = expand_page0[window]
                        if isinstance(outcome, BaseException):
                            sort_errors.append(f"{_window_label(window)} page 0: {type(outcome).__name__}: {outcome}")
                            continue
                        pages_fetched += 1
                        expand_ok.add(window)
                        candidate_total = outcome.get("total_results") if isinstance(outcome, dict) else None
                        expand_totals[window] = candidate_total if isinstance(candidate_total, int) and candidate_total >= 0 else None

                    expand_rest_targets: list[tuple[_CatalogWindow, int]] = []
                    for window in expand_ok:
                        pages_needed = _pages_needed_for(expand_totals.get(window))
                        for page in range(1, pages_needed):
                            expand_rest_targets.append((window, page))

                    expand_rest: dict[tuple[_CatalogWindow, int], Any] = {}
                    if expand_rest_targets:
                        if deadline_passed():
                            truncated_by_deadline = True
                        else:
                            expand_rest = await _fetch_window_rest_batch(author_name, region, expand_rest_targets)

                    for window in expand_windows:
                        if window not in expand_ok:
                            continue
                        page0_outcome = expand_page0[window]
                        page0_data = page0_outcome if isinstance(page0_outcome, dict) else None
                        _process_catalog_page(page0_data, author_asin, author_name, seen, asins, counts)
                        pages_needed = _pages_needed_for(expand_totals.get(window))
                        for page in range(1, pages_needed):
                            target = (window, page)
                            if target not in expand_rest:
                                continue
                            outcome = expand_rest[target]
                            if isinstance(outcome, BaseException):
                                sort_errors.append(f"{_window_label(window)} page {page}: {type(outcome).__name__}: {outcome}")
                                continue
                            pages_fetched += 1
                            _process_catalog_page(outcome, author_asin, author_name, seen, asins, counts)

    windows_used = len(baseline_windows) + categories_considered + categories_expanded * len(_CATALOG_CATEGORY_SPEND_SORTS)

    return _CatalogBooksResult(
        asins=asins,
        pages_fetched=pages_fetched,
        total_results=total_results,
        sort_errors=sort_errors,
        truncated_by_deadline=truncated_by_deadline,
        slicing_incomplete=slicing_incomplete,
        sliced=needs_slicing,
        categories_harvested=categories_harvested,
        categories_considered=categories_considered,
        categories_expanded=categories_expanded,
        windows_used=windows_used,
        asin_match_count=counts.get(_CATALOG_TIER_ASIN_MATCH, 0),
        asin_reject_count=counts.get(_CATALOG_TIER_ASIN_REJECT, 0),
        name_match_count=counts.get(_CATALOG_TIER_NAME_MATCH, 0),
        name_reject_count=counts.get(_CATALOG_TIER_NAME_REJECT, 0),
    )
