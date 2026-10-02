"""
Audible author-books catalog walk, windowed and category-sliced.
Fetches an author's book ASINs from the /1.0/catalog/products endpoint with
the ASIN-attributed walk (fetch_author_books_by_catalog) that the live
author-books lookup runs. The single-sort, name-only walk for a caller with
no author ASIN lives in by_name.py; the two share no code.

This module stays over 1000 lines because it is one algorithm with one
caller: the window/sort/category sequencing, its per-phase accounting and
the live measurements and incident notes that justify each bound only make
sense read together, and no seam inside it would cut cleanly.
"""

# Standard library
import asyncio
import time
from dataclasses import dataclass
from typing import Any

# Core
from libex_core.audible.client import AudibleGet, validate_region


# ============================================================
# CATALOG WINDOWED WALK (two of an author's books' four sources, with screens)
#
# Distinct from walk_author_books_by_name (by_name.py), which stays a
# single-sort, name-only walk for a caller with no author ASIN to attribute
# against. This walk does have the ASIN, and probed
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
# fetch_author_books_by_catalog's own docstring for the full sequence.
# ============================================================

# Priority order matters here: -ReleaseDate first is compat-critical.
# Consumers have always received the -ReleaseDate list at the front of
# the response and must keep receiving that same list, in that same
# order, unmoved at the front of the union.
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
# fetch_author_books_by_catalog's Phase 3) -- deliberately NOT one of
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
# content to give (see fetch_author_books_by_catalog's Phase 4) -- every
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
# real total of 203 stays under the ceiling on every sort and paginates
# normally. The endpoint never 404s or comes back short past the ceiling
# -- it returns HTTP 200 with the prior page's content repeating
# indefinitely. This bounds pages requested per window (unfiltered sort or
# category+sort pair alike -- see _pages_needed_for) rather than trusting
# total_results past it, the same principle the total_results-vs-repeated-
# signature check applies to the single-sort walk in by_name.py.
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
# fetch_author_books_by_catalog's Phase 3. Not measured directly (the live
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
# its own, mark CatalogBooksResult.slicing_incomplete -- see that field's
# own docstring.
CATALOG_MAX_CANDIDATE_CATEGORIES = 40

# Attribution tiers a catalog product can land in -- see
# _classify_catalog_product. Counted separately in CatalogBooksResult so
# a caller can log how often each tier fired; how often
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
    get: AudibleGet,
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
    fetch_author_books_by_catalog harvests candidate categories from --
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
    return await get(region, path, params)


@dataclass
class CatalogBooksResult:
    """
    asins is ordered by fold order, not fetch order: Phase 1's
    -ReleaseDate window in full (page 0 through however many further
    pages it needed, in ascending page order), then Phase 1's ReleaseDate
    window the same way, then -- only for an author whose baseline
    plateaued or over-claimed total_results (see
    fetch_author_books_by_catalog) -- every category window Phases 3 and
    4 folded in, each in ranked-candidate-then-sort-priority order. This
    is what lets a caller preserve the compat-critical
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
    # Categories that earned Phase 4 but were already fully enumerated by
    # Phase 3's probe, so their extra sorts were skipped. Counted separately
    # from categories_expanded because "paid but needed nothing more" is a
    # different outcome from "did not pay", and collapsing them would hide
    # exactly the saving this split exists to make.
    categories_complete_after_probe: int = 0
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
    signal fetch_author_books_by_catalog's Phase 2 sorts candidate
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
    not silent, they are how a caller reports the attribution
    breakdown.

    category_frequency, when given (only for Phase 1's baseline pages --
    see fetch_author_books_by_catalog), also harvests every accepted
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
        # libex_core/audible/_retry.py's ~84k-record note) and consumers already receive them
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
    fetch_author_books_by_catalog's Phase 2). Frozen and hashable so a
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


def _needs_further_sorts(probe_total: int | None, probe_pages_lost: bool) -> bool:
    """
    Whether a category that earned Phase 4 has anything left for the extra
    sorts to find.

    The deep-paging ceiling applies per (products_sort_by, category_id) pair,
    not per author -- that is the whole reason Phase 4 spends several sorts on
    one category, because each opens a separately-ceilinged window into a
    result set too large for any single one to reach. It also means the
    reverse: a category whose own total_results sits at or under
    CATALOG_RESULT_CEILING has no second window to open. Phase 3's probe
    already paged that category to _pages_needed_for(total), which for a total
    under the ceiling is all of it, so every further sort can only re-return
    products already folded into seen -- up to four sorts by ten pages of
    them, per category, for nothing.

    The count is trustworthy for this because the query carries the author
    name as well as the category, so total_results is how many of THIS
    author's products sit in that category, not the category's own size.

    None means the probe's page 0 gave no usable total, in which case
    _pages_needed_for fell back to a single page and the category was NOT
    fully enumerated -- those keep their sorts.

    The comparison is >=, not >, and the difference matters at exactly one
    value. CATALOG_RESULT_CEILING is 500 because that is where most sorts
    plateau, but the same measurement recorded Christie's ascending
    ReleaseDate plateauing one short, at 499 -- the ceiling is not uniformly
    500 across (sort, category) pairs. A category claiming exactly 500 is
    therefore not provably reachable in full by one sort, and plateau
    detection runs on baseline windows only, never on probe windows, so
    nothing else would notice the shortfall. Spending four sorts on a
    category claiming exactly 500 is the cost of the one boundary where the
    module's own two measurements disagree.

    probe_pages_lost is the other way the enumeration can be incomplete, and
    it is not visible in the total at all: total_results is page 0's claim
    about how many results exist, made before any later page is fetched, so
    it still reads as a modest number when one of those pages then failed.
    A category whose probe lost a page has products nobody folded, and the
    extra sorts are the only remaining chance to reach them -- exactly the
    recovery this skip would otherwise remove. Erring toward spending four
    requests that turn out to be redundant is the cheap mistake; erring the
    other way drops books silently.
    """
    return probe_pages_lost or probe_total is None or probe_total >= CATALOG_RESULT_CEILING


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
    get: AudibleGet, author_name: str, region: str, windows: list[_CatalogWindow]
) -> dict[_CatalogWindow, Any]:
    """Fetches page 0 of every window in one gather, keyed back by window
    so the caller can look up any one window's outcome (a dict response,
    or the exception it raised) without depending on gather's return
    order lining up with anything but the input list, which zip already
    guarantees regardless."""
    outcomes = await asyncio.gather(
        *(
            _fetch_catalog_page(
                get, author_name, region, 0, w.sort,
                category_id=w.category_id, include_categories=w.harvest,
            )
            for w in windows
        ),
        return_exceptions=True,
    )
    return dict(zip(windows, outcomes))


async def _fetch_window_rest_batch(
    get: AudibleGet,
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
                get, author_name, region, page, w.sort,
                category_id=w.category_id, include_categories=w.harvest,
            )
            for w, page in targets
        ),
        return_exceptions=True,
    )
    return dict(zip(targets, outcomes))


async def fetch_author_books_by_catalog(
    get: AudibleGet,
    author_asin: str,
    author_name: str,
    region: str,
    deadline: float | None = None,
) -> CatalogBooksResult:
    """
    Fetches book ASINs for an author from the catalog endpoint, through
    `get`, ASIN-attributed via _classify_catalog_product rather than name-matched
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
    order (see CatalogBooksResult's own docstring) -- and a page-0
    response already fetched is always processed, even for a window that
    turns out not to need (or not to have room in the deadline for) its
    remaining pages; data already paid for from a live request is never
    discarded.

    A single page's fetch failure is recorded in sort_errors and does not
    stop the rest of the walk -- every other page, window, and phase is
    unaffected, the same principle a caller gathering several sources applies
    one level up.

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
    a caller's own completeness check against this field.

    Raises RegionException for a region that is not one of the eleven --
    before anything is sent. author_asin is only compared against what comes
    back, never sent, so it is not validated here.
    """
    region = validate_region(region)
    seen: set[str] = set()
    asins: list[str] = []
    counts: dict[str, int] = {}
    sort_errors: list[str] = []
    pages_fetched = 0

    def deadline_passed() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    if deadline_passed():
        return CatalogBooksResult(
            asins=[],
            pages_fetched=0,
            total_results=None,
            sort_errors=[],
            truncated_by_deadline=True,
        )

    # ---- Phase 1: baseline, unfiltered, the two disjoint sorts ----
    baseline_windows = [_CatalogWindow(None, sort, harvest=True) for sort in _CATALOG_BASELINE_SORTS]
    baseline_page0 = await _fetch_window_page0_batch(get, author_name, region, baseline_windows)

    total_results: int | None = None
    baseline_totals: dict[_CatalogWindow, int | None] = {}
    for window in baseline_windows:
        outcome = baseline_page0[window]
        if isinstance(outcome, BaseException):
            sort_errors.append(f"{_window_label(window)} page 0: {type(outcome).__name__}: {outcome}")
            baseline_totals[window] = None
            continue
        if not isinstance(outcome, dict) or not isinstance(outcome.get("products"), list):
            # SITE 4, reproduced live: the worst of every hole
            # in this function, because Phase 1 runs on EVERY author query,
            # not just a large or sliced one. A malformed-but-dict 200 --
            # `{"total_results": 100}` with no "products" at all -- used to
            # pass this loop's old BaseException-only check, get counted as
            # fetched, and then ALSO pass the fold loop's own
            # `isinstance(page0_outcome, dict)` gate below (a malformed
            # dict is still a dict), landing on _process_catalog_page's
            # silent no-op with zero sort_errors either way. The result:
            # asins == [], sort_errors == [], slicing_incomplete == False --
            # a walk that returned nothing looked perfectly clean, and
            # the hosted author-books lookup gates its cache write under
            # the full default TTL (24 hours) on nothing but sort_errors
            # being empty, so an empty result was one malformed 200 away
            # from being cached under that full default TTL as an
            # exhaustive walk.
            #
            # Fixed here, in the extraction loop, not at the fold below --
            # by the time the fold loop's `page0_data = ... else None` runs,
            # pages_fetched has already been incremented and there is no
            # longer one place left to also correct baseline_totals; this
            # loop is that one place; the BaseException branch just above
            # already proves the pattern (also sets baseline_totals[window]
            # = None before continuing), so a malformed dict is now handled
            # exactly the same way a raised exception already was rather
            # than inventing a second, divergent path for the same "page 0
            # unusable" fact. total_results is deliberately not trusted
            # from a malformed page even when the dict happens to carry
            # a total_results value -- as for the exception branch, this
            # window's pages_needed then falls back to just page 0 via
            # _pages_needed_for(None), matching the existing next
            # BaseException handling exactly rather than adding a
            # speculative rest-page recovery attempt no other page-0 site
            # in this function makes either.
            #
            # sort_errors alone is sufficient here, for the identical
            # reason it already was for the baseline rest-page site: read
            # live, the hosted lookup's cache gate does not distinguish a
            # SHORT clean walk from an EMPTY clean one -- both need
            # sort_errors empty before it writes ANYTHING to the cache (not
            # merely the full default TTL write; a non-empty sort_errors
            # writes nothing at all and re-runs the walk in the
            # background instead).
            # Nothing stronger than the signal already used everywhere
            # else in this function would do anything the existing gate
            # does not already do -- in particular, the stored union at
            # the DB layer cannot shrink regardless
            # (persist_author_books_cache_background unions under a row
            # lock), so the only thing this guard has to protect is the
            # walk's own completeness CLAIM, not the data itself.
            # Measured live: in the single-window case
            # this loses nothing at all -- both baseline sorts enumerate
            # the same unfiltered result set, so the sibling window
            # already covers it. The residual cost is both windows
            # malformed at once, transiently, page 0 only, with
            # total_results happening to survive -- and there the loss is
            # only transient, because nothing gets cached and the very
            # next request recovers it, against main's alternative of
            # caching the shortfall as complete for the full default TTL.
            # This is the other of Phase 1's two guards -- see the
            # malformed-rest-page guard further below in this same loop
            # for the narrower, page>=1 half of the same protection;
            # neither one alone covers the other's gap.
            sort_errors.append(f"{_window_label(window)} page 0: malformed response body")
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
            baseline_rest = await _fetch_window_rest_batch(get, author_name, region, baseline_rest_targets)

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
            if not isinstance(outcome, dict) or not isinstance(outcome.get("products"), list):
                # This is one of TWO guards Phase 1 needs, not the whole
                # of its protection alone -- the other covers this same
                # window's own page 0 (see the SITE 4 comment in the
                # extraction loop above). A malformed page 0 is the worse
                # of the two: it also poisons baseline_totals for this
                # whole window, which caps _pages_needed_for at a single
                # page and means this rest-page loop is never even
                # reached for that window at all. Losing a page HERE,
                # mid-walk, after page 0 already succeeded, is the
                # narrower failure -- but nothing downstream of Phase 1
                # ever re-examines a baseline window on its own either
                # way, so sort_errors is the only trace either kind of
                # loss ever leaves for that window, and that is exactly
                # what keeps the hosted lookup's cache gate from caching a
                # short OR an empty result under the full default TTL as
                # though the walk were complete. slicing_incomplete is
                # deliberately left untouched: that field's own contract
                # is narrower (a harvest that surfaced zero categories to
                # slice with, not a page that failed to fetch -- see
                # CatalogBooksResult's docstring), and folding this into
                # it would blur a signal that field already defines
                # precisely. Plateau detection needs no extra guard
                # either: _catalog_page_signature already returns None
                # for a body this shape can't be compared from, so a
                # malformed page here neither trips a false
                # baseline_plateaued nor overwrites previous_signature
                # with something the next real page would wrongly compare
                # against.
                sort_errors.append(f"{_window_label(window)} page {page}: malformed response body")
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
    categories_complete_after_probe = 0
    slicing_incomplete = False

    if needs_slicing and not truncated_by_deadline and not deadline_passed():
        # ---- Phase 2: rank harvested candidates, no requests ----
        ranked = sorted(category_frequency.items(), key=lambda kv: kv[1], reverse=True)
        candidate_ids = [category_id for category_id, _frequency in ranked[:CATALOG_MAX_CANDIDATE_CATEGORIES]]
        categories_harvested = len(ranked)
        categories_considered = len(candidate_ids)
        # See CATALOG_MAX_CANDIDATE_CATEGORIES and CatalogBooksResult's own
        # docstrings: the cap truncating the ranked list is routine on a
        # real large author and not, on its own, evidence anything was
        # missed. Only a genuinely empty harvest despite needing to slice
        # counts as incomplete here.
        slicing_incomplete = not category_frequency

        if candidate_ids:
            # ---- Phase 3: cheap page-0 tier, every candidate at once ----
            probe_windows = [_CatalogWindow(cid, _CATALOG_CATEGORY_PROBE_SORT) for cid in candidate_ids]
            probe_page0 = await _fetch_window_page0_batch(get, author_name, region, probe_windows)

            probe_ok: set[_CatalogWindow] = set()
            probe_totals: dict[_CatalogWindow, int | None] = {}
            # Populated here, not just below at the rest-page tier, because
            # a malformed page 0 (see the loop below) is folded into these
            # same two collections immediately -- there is no separate
            # "page-0 lost" set, it shares probe_pages_lost and paying with
            # the rest-page-loss case they were already built for.
            probe_pages_lost: set[_CatalogWindow] = set()
            paying: list[str] = []
            # Each window's own page-0 new-ASIN yield, captured the instant
            # it's folded below -- this is the cheap tier's half of the
            # "across both tiers" score the docstring above promises. It has
            # to be captured per-window here, in this same fold-order loop,
            # rather than re-derived later from len(seen): every window's
            # page 0 is folded into seen in this one loop before the second
            # loop below ever runs, so by the time that second loop reaches
            # any given window, seen already contains that window's own
            # page-0 ASINs and a before/after diff there would double-count
            # nothing -- it would count nothing at all for page 0.
            probe_page0_new: dict[_CatalogWindow, int] = {}
            advancing: list[_CatalogWindow] = []
            for window in probe_windows:
                outcome = probe_page0[window]
                if isinstance(outcome, BaseException):
                    sort_errors.append(f"{_window_label(window)} page 0: {type(outcome).__name__}: {outcome}")
                    continue
                if not isinstance(outcome, dict) or not isinstance(outcome.get("products"), list):
                    # Unlike the raising case just above, this window still
                    # got an HTTP 200 -- there was simply nothing usable in
                    # the body. That is a stronger case than a malformed
                    # REST page: page 0 is the ONLY source of
                    # probe_page0_new and advancing, so with no guard here
                    # this window silently scores added == 0, which is
                    # indistinguishable from a category that genuinely had
                    # nothing, and, worse, consumes a slot in the
                    # CATALOG_DRY_STREAK_LIMIT streak that can stop the walk
                    # from ever folding a real, lower-ranked category (see
                    # this function's own docstring on Phase 3). There is no
                    # yield signal at all to score here, so this window
                    # never enters probe_ok and skips the dry/paying fold
                    # below entirely -- it is instead pushed straight into
                    # paying and probe_pages_lost, the same treatment a
                    # window whose REST pages went missing gets, so
                    # _needs_further_sorts forces Phase 4 to give this
                    # category the one remaining chance to recover it
                    # (probe_totals has no entry for it either, which alone
                    # would already force that same recovery -- both reasons
                    # hold at once). Erring toward the extra Phase 4 request
                    # is the same call this module already made for a lost
                    # REST page: spending four requests that turn out
                    # redundant is cheap, silently dropping a category that
                    # was never actually dry is not. paying itself is not
                    # appended here -- see the second loop below, which adds
                    # every probe_pages_lost window to paying at the point
                    # in that loop's own probe_windows iteration order that
                    # keeps CatalogBooksResult's documented fold order
                    # (ranked-candidate-then-sort-priority) intact; appending
                    # here instead would put every malformed candidate ahead
                    # of higher-ranked ones the second loop scores later.
                    sort_errors.append(f"{_window_label(window)} page 0: malformed response body")
                    probe_pages_lost.add(window)
                    continue
                pages_fetched += 1
                probe_ok.add(window)
                candidate_total = outcome.get("total_results") if isinstance(outcome, dict) else None
                probe_totals[window] = candidate_total if isinstance(candidate_total, int) and candidate_total >= 0 else None
                before = len(seen)
                _process_catalog_page(outcome, author_asin, author_name, seen, asins, counts)
                added = len(seen) - before
                probe_page0_new[window] = added
                if added > 0:
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
                    probe_rest = await _fetch_window_rest_batch(get, author_name, region, probe_rest_targets)

            # Windows whose probe did not fetch every page it needed -- a
            # page that errored, one the batch never returned, or one that
            # came back 200 with an unusable body (the transport returns
            # response.json() unvalidated on any 200; _process_catalog_page
            # silently no-ops on a non-dict or a dict without a list
            # "products"). Their total_results says nothing about it, so it
            # has to be carried separately. See _needs_further_sorts.
            # probe_pages_lost and paying are declared above, alongside the
            # page-0 loop, because a malformed page 0 is folded into both of
            # them there rather than here.
            dry_streak = 0
            for window in probe_windows:
                if window in probe_pages_lost:
                    # A malformed page 0 (folded into probe_pages_lost and
                    # left out of paying in the loop above -- see that
                    # loop's own comment). Handled here, at this same
                    # probe_windows iteration point, purely to land in
                    # paying in rank order; it never touches probe_ok, the
                    # rest-page fetch below, or dry_streak, because there is
                    # no yield to score it against.
                    paying.append(window.category_id)
                    continue
                if window not in probe_ok:
                    continue
                before = len(seen)
                if window in advancing:
                    pages_needed = _pages_needed_for(probe_totals.get(window))
                    for page in range(1, pages_needed):
                        target = (window, page)
                        if target not in probe_rest:
                            probe_pages_lost.add(window)
                            continue
                        outcome = probe_rest[target]
                        if isinstance(outcome, BaseException):
                            sort_errors.append(f"{_window_label(window)} page {page}: {type(outcome).__name__}: {outcome}")
                            probe_pages_lost.add(window)
                            continue
                        if not isinstance(outcome, dict) or not isinstance(outcome.get("products"), list):
                            sort_errors.append(f"{_window_label(window)} page {page}: malformed response body")
                            probe_pages_lost.add(window)
                            continue
                        pages_fetched += 1
                        _process_catalog_page(outcome, author_asin, author_name, seen, asins, counts)
                # The full-probe (rest-page) tier's own yield, plus the
                # cheap (page-0) tier's yield already captured above -- see
                # probe_page0_new. Scoring the rest tier alone here (the
                # defect this replaced) silently scored every category
                # whose page 0 already carried CATALOG_DRY_WINDOW_MIN_NEW or
                # more new ASINs as dry the moment its remaining pages
                # added nothing further, and, worse, scored a category with
                # no total_results on its page 0 as dry BY CONSTRUCTION --
                # _pages_needed_for(None) returns 1, so range(1, 1) never
                # fetches a rest page at all regardless of how many new
                # ASINs page 0 alone genuinely contributed.
                new_count = probe_page0_new.get(window, 0) + (len(seen) - before)
                if new_count >= CATALOG_DRY_WINDOW_MIN_NEW:
                    dry_streak = 0
                    paying.append(window.category_id)
                else:
                    dry_streak += 1
                    if dry_streak >= CATALOG_DRY_STREAK_LIMIT:
                        break

            # A category the probe already saw in full has nothing for the
            # extra sorts to find -- see _needs_further_sorts. Splitting here
            # rather than filtering inside the comprehension below keeps the
            # skipped ones countable, because "paid but needed nothing more"
            # is a different fact from "did not pay" and the log should not
            # collapse them.
            to_expand = [
                cid for cid in paying
                if _needs_further_sorts(
                    probe_totals.get(_CatalogWindow(cid, _CATALOG_CATEGORY_PROBE_SORT)),
                    _CatalogWindow(cid, _CATALOG_CATEGORY_PROBE_SORT) in probe_pages_lost,
                )
            ]
            categories_complete_after_probe = len(paying) - len(to_expand)
            categories_expanded = len(to_expand)

            # ---- Phase 4: remaining sorts for every category that still needs one ----
            # The deadline is judged against `to_expand`, not `paying`.
            # Before the probe-completeness skip existed (see
            # _needs_further_sorts), the two sets were identical, so a
            # deadline trip here always meant real, unfetched work existed.
            # Now a category can pay -- score enough new ASINs on its own
            # probe -- and still need nothing further, because the probe
            # already saw it in full. Charging that as a truncation would
            # cost a walk that finished everything it needed its full-TTL
            # cache, for work that was never there to do. An already-True
            # truncated_by_deadline from an earlier phase (the probe rest
            # batch itself timing out) is never reset here -- `or` only
            # ever turns it on.
            if to_expand:
                if truncated_by_deadline or deadline_passed():
                    truncated_by_deadline = True
                else:
                    expand_windows = [
                        _CatalogWindow(category_id, sort)
                        for category_id in to_expand
                        for sort in _CATALOG_CATEGORY_SPEND_SORTS
                    ]
                    expand_page0 = await _fetch_window_page0_batch(get, author_name, region, expand_windows)

                    expand_ok: set[_CatalogWindow] = set()
                    expand_totals: dict[_CatalogWindow, int | None] = {}
                    for window in expand_windows:
                        outcome = expand_page0[window]
                        if isinstance(outcome, BaseException):
                            sort_errors.append(f"{_window_label(window)} page 0: {type(outcome).__name__}: {outcome}")
                            continue
                        if not isinstance(outcome, dict) or not isinstance(outcome.get("products"), list):
                            # SITE 5, reproduced live: the same
                            # shape as SITE 4 above, one phase later -- a
                            # malformed-but-dict page 0 used to pass this
                            # loop's old BaseException-only check, get
                            # wrongly added to expand_ok and counted as
                            # fetched, and then pass the fold loop's own
                            # `isinstance(page0_outcome, dict)` gate too,
                            # landing on a silent no-op with zero
                            # sort_errors -- up to 50 ASINs dropped with no
                            # trace and, as the neighbouring expand-rest
                            # guard's own comment already notes, Phase 4 has
                            # no further recovery mechanism, so a loss here
                            # is permanent. Fixed at the same point as
                            # SITE 4, for the same reason: this extraction
                            # loop is the one place pages_fetched and
                            # expand_ok are decided, so this is where "was
                            # page 0 usable" has to be decided too, rather
                            # than at the fold below where expand_ok
                            # membership has already been wrongly granted.
                            sort_errors.append(f"{_window_label(window)} page 0: malformed response body")
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
                            expand_rest = await _fetch_window_rest_batch(get, author_name, region, expand_rest_targets)

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
                            if not isinstance(outcome, dict) or not isinstance(outcome.get("products"), list):
                                # No recovery mechanism to wire this into --
                                # Phase 4 is the walk's last phase, so unlike
                                # the probe's lost rest pages (probe_pages_lost,
                                # which forces this same category's OTHER
                                # sorts to run in Phase 4) there is nothing
                                # further downstream for a lost expand page to
                                # feed. sort_errors is still required
                                # regardless: it is what blocks a caller's
                                # cache gate from storing the short result as complete,
                                # and the nothing-silently-dropped invariant
                                # demands the visibility whether or not
                                # recovery is possible.
                                sort_errors.append(f"{_window_label(window)} page {page}: malformed response body")
                                continue
                            pages_fetched += 1
                            _process_catalog_page(outcome, author_asin, author_name, seen, asins, counts)

    windows_used = len(baseline_windows) + categories_considered + categories_expanded * len(_CATALOG_CATEGORY_SPEND_SORTS)

    return CatalogBooksResult(
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
        categories_complete_after_probe=categories_complete_after_probe,
        windows_used=windows_used,
        asin_match_count=counts.get(_CATALOG_TIER_ASIN_MATCH, 0),
        asin_reject_count=counts.get(_CATALOG_TIER_ASIN_REJECT, 0),
        name_match_count=counts.get(_CATALOG_TIER_NAME_MATCH, 0),
        name_reject_count=counts.get(_CATALOG_TIER_NAME_REJECT, 0),
    )
