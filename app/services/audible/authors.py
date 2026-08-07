"""
Audible authors service.
Fetches author metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Always fetches fresh data from Audible.
Writes every result to the relational DB for persistence.
Falls back to DB when Audible is unavailable.
"""

# Standard library
import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.core.middleware import is_valid_asin
from app.core.utils import strip_html

# Services
from app.services.audible.client import audible_get, ANDROID_DEVICE_TYPE_ID, LOCALE_MAP
from app.services.cache import manager as cache
from app.services.cache.manager import author_key, author_books_key
from app.services.db.writer import persist_author_background, persist_author_books_cache_background
from app.services.db.reader import get_author_from_db, get_author_book_asins_from_db

logger = get_logger()

# Bounds for the screens-based author-books walk. These are NOT sized to
# fit any real author's catalog -- that was tried once (a 60-page cap sized
# to Christie's then-known 1115 titles, then a 100-page/2000-ASIN cap sized
# with headroom over that) and both turned out to be limits on a legitimate
# author rather than a backstop against a hostile one: Arthur Conan Doyle
# (B000AQ43GQ) reports product_count 4500, which at Audible's fixed ~19-20
# grid rows/page is ~225-240 pages, and a 2000-ASIN cap truncated him (and
# would have truncated Christie again the moment her catalog grew past
# 2000) silently. The walk's real, correct terminators are the null
# continuation token (SCREENS_REASON_COMPLETED) and the seen_tokens repeat
# check (SCREENS_REASON_TOKEN_REPEATED) -- both already run on every page
# and both already work. SCREENS_MAX_PAGES and SCREENS_MAX_ASINS exist
# beneath those purely to stop a pathological or adversarial upstream that
# never sends a null/repeated token from looping forever; they are sized
# with an order of magnitude of headroom over the largest known real
# catalog (4500) precisely so they can never bind on real data. Hitting
# either is demoted from a clean/complete walk regardless (see
# SCREENS_CLEAN_REASONS), so a real hit here always shows up loudly rather
# than silently passing as "the author only has this many books."
SCREENS_MAX_PAGES = 5000
SCREENS_MAX_ASINS = 100000
# SCREENS_MAX_SECTIONS bounds only the non-grid candidate sections examined
# per page once the grid itself is already identified (see
# _select_asin_rows) -- but those non-grid rails are a real, proven source
# of titles absent from the grid itself (uk B004SOKICO), not just decoy
# noise, so this is sized generously rather than to "however many rails a
# real page has" the same way the walk-level bounds above are: a real page
# carries a handful of rails, so this only ever binds against a page
# sending an implausible number of sections.
SCREENS_MAX_SECTIONS = 1000
# SCREENS_MAX_ROWS_PER_PAGE bounds rows within a single section. A real
# section holds Audible's fixed ~20-row page size, so this is sized purely
# as a non-binding backstop against a single degenerate section carrying an
# implausible number of rows, never as a limit on real data.
SCREENS_MAX_ROWS_PER_PAGE = 10000
SCREENS_TOKEN_MAX_LEN = 512
_SCREENS_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9+/=_-]+")

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

# Bounded concurrency for the name-search walk's parallel page fetches --
# unlike the screens endpoint's opaque continuation token, /catalog/products
# takes a real page index, so pages don't have to be fetched one at a time.
# This bound only ever applies on the live request path (get_author_books
# passes NAME_SEARCH_CONCURRENCY explicitly); _fetch_author_books_by_name_detailed
# defaults to concurrency=1 and fetch_author_books_by_name -- the wrapper the
# seeder's paced per-author loop calls -- never overrides that default, so
# the seeder's own pacing (SEEDER_REQUEST_DELAY) is untouched by this and
# stays fully sequential. 8 is deliberately modest, not "as fast as
# possible": this IP is shared with the live seeder and has already been
# throttled into a VPN rotation once from an amplified request burst.
NAME_SEARCH_CONCURRENCY = 8

# Wall-clock budget across the whole author-books union in get_author_books
# (one screens walk plus one name search). This bounds the work a single
# request can do -- it is not a rate limit, counts nothing across requests,
# keys on no client identity, and rejects nobody. Unlike SCREENS_MAX_PAGES /
# SCREENS_MAX_ASINS / NAME_SEARCH_MAX_PAGES above, this one DOES bind on a
# real, very prolific author's catalog before those far-larger caps ever
# would -- at roughly 0.5s/page it cuts off around 1800 titles, well short
# of Conan Doyle's 4500 -- but a deadline-truncated walk is reported as
# degraded (not clean/complete) and is never persisted as authoritative
# (see the less-data-never-accepted gate below), so it costs latency and a
# cache write on the prolific tail, not silent, permanent data loss the way
# the old page/ASIN caps did. That's a deliberate latency/completeness
# tradeoff, not an oversight, and changing it is a separate decision from
# the caps above.
AUTHOR_BOOKS_TIME_BUDGET_SECONDS = 45.0

# Termination reasons for the screens walk. "completed" is the only clean
# reason: it means the walk stopped because upstream reported no further
# page (the continuation token was absent or null), so every ASIN upstream
# had to offer was seen. Every other reason -- including a repeated token --
# means the walk stopped without that confirmation and is therefore unclean.
# Downstream keys off termination_reason directly rather than inferring
# cleanliness from page/ASIN counts.
SCREENS_REASON_COMPLETED = "completed"
SCREENS_REASON_TOKEN_REPEATED = "token_repeated"
SCREENS_REASON_TOKEN_REJECTED = "token_rejected"
SCREENS_REASON_PAGE_CAP = "page_cap"
SCREENS_REASON_ASIN_CAP = "asin_cap"
SCREENS_REASON_PAGE_ERROR = "page_error"
SCREENS_REASON_NON_DICT_PAGE = "non_dict_page"
SCREENS_REASON_TIME_BUDGET = "time_budget"
# A real page 1 always carries a product_count on its grid section, so a walk
# that fetched at least one page and ended with no product_count and not a
# single ASIN has no positive evidence the grid was ever found -- an absent
# sections list, an empty one, or a page shape upstream has silently renamed
# all look like a "no more pages" token to _extract_next_token, so this
# reason exists to keep that case from scoring as a clean, complete walk.
SCREENS_REASON_GRID_NOT_FOUND = "grid_not_found"
# SCREENS_MAX_SECTIONS / SCREENS_MAX_ROWS_PER_PAGE exist to bound a hostile
# upstream's worst case, not to be a normal occurrence -- hitting either one
# means some sections or rows upstream sent were never looked at at all, so
# a walk that otherwise reached a confirmed pagination end cannot be scored
# as having seen everything upstream had to offer.
SCREENS_REASON_TRUNCATED = "sections_or_rows_truncated"
SCREENS_CLEAN_REASONS = frozenset({SCREENS_REASON_COMPLETED})


# ============================================================
# HELPERS
# ============================================================

def _generate_session_id() -> str:
    """
    Generates a random session ID matching AudiMeta's format.
    Format: 000-XXXXXXX-XXXXXXX
    """
    import random

    def random_digits() -> str:
        return str(random.randint(0, 9999999)).zfill(7)

    return f"000-{random_digits()}-{random_digits()}"


def _normalize_author(data: dict, asin: str, region: str) -> dict[str, Any]:
    contributor = data.get("contributor", {})
    bio = contributor.get("bio")
    return {
        "id": None,
        "asin": asin,
        "name": contributor.get("name", "").replace("\t", "").strip(),
        "description": strip_html(bio),
        "image": contributor.get("profile_image_url"),
        "region": region,
        "regions": [region],
        "genres": [],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# AUDIBLE REQUESTS
# ============================================================


async def _fetch_author_details(asin: str, region: str) -> dict[str, Any]:
    """
    Fetches author profile from Audible contributors endpoint.
    Returns bio, image, and name.
    """
    path = f"/1.0/catalog/contributors/{asin}"
    params = {
        "locale": LOCALE_MAP.get(region, "en-US"),
    }
    return await audible_get(region, path, params)


@dataclass
class _ScreenBooksResult:
    """
    termination_reason explains why the walk stopped rather than leaving the
    caller to infer it from counts; see SCREENS_CLEAN_REASONS for the single
    reason that counts as a confirmed-complete walk. page_error carries the
    exception type and message when termination_reason is
    SCREENS_REASON_PAGE_ERROR, so a transient failure, a mid-walk 404, and a
    genuine code defect stay tellable apart downstream instead of collapsing
    into the same static string.

    sections_truncated and rows_truncated count, summed across every page
    fetched, how many candidate sections beyond the grid and how many rows
    within a single section were cut off by SCREENS_MAX_SECTIONS /
    SCREENS_MAX_ROWS_PER_PAGE respectively. Either being nonzero means some
    data upstream sent was never looked at, which is why it also demotes an
    otherwise-COMPLETED walk to SCREENS_REASON_TRUNCATED rather than leaving
    it to score as having seen everything upstream had to offer.
    """

    asins: list[str]
    pages_fetched: int
    product_count: int | None
    invalid_skipped: int
    attribution_rejected: int
    termination_reason: str
    page_error: str | None = None
    sections_truncated: int = 0
    rows_truncated: int = 0


def _select_asin_rows(data: dict) -> tuple[list, list, int | None, int]:
    """
    Splits a page's StandardAsinRowList sections into the author's title grid
    and every other rail on the page.

    A page can carry more than one StandardAsinRowList section (page 1 has a
    "most popular" teaser row alongside the full grid). The section whose
    header reports product_count is the grid, and its rows need no further
    check; every other candidate's rows are returned separately, since a
    rail other than the grid is not guaranteed to be author-scoped and the
    caller must attribute each of its rows before accepting it.

    The search for the product_count-bearing grid always runs over every
    section on the page, never a capped prefix of them -- capping the search
    itself is what let a decoy rail sitting inside the cap window get
    admitted as the grid by elimination while the real, product_count-
    bearing grid sat beyond the cap, unseen and never even considered.
    SCREENS_MAX_SECTIONS instead bounds only how many of the *other*,
    non-grid candidate sections get their rows collected once the grid has
    already been identified (or ruled unidentifiable); any of those beyond
    the cap are reported via the fourth return value rather than silently
    dropped.

    When no candidate reports product_count -- including when there is
    exactly one candidate -- nothing can be trusted as the unconstrained
    grid by elimination, since a single unattributed decoy rail looks
    identical to a genuine single-section continuation page. Every row from
    every candidate (within the cap) is returned as needing attribution
    instead; admission from there is row-shape driven (see
    _extract_row_asins), so genuine grid rows among them still get through
    even though the section they arrived in can't be singled out.

    Returns (grid_rows, other_rows, product_count, other_sections_truncated).
    """
    sections = data.get("sections")
    if not isinstance(sections, list):
        return [], [], None, 0

    candidates: list[tuple[list, int | None]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        model = section.get("model")
        if not isinstance(model, dict):
            continue
        if model.get("__component_type") != "StandardAsinRowList":
            continue

        rows = model.get("rows")
        if not isinstance(rows, list):
            rows = []

        header = model.get("header")
        header_model = header.get("header_model") if isinstance(header, dict) else None
        product_count = (
            header_model.get("product_count") if isinstance(header_model, dict) else None
        )
        if not isinstance(product_count, int):
            product_count = None
        candidates.append((rows, product_count))

    if not candidates:
        return [], [], None, 0

    grid_index = next((i for i, c in enumerate(candidates) if c[1] is not None), None)

    if grid_index is None:
        capped = candidates[:SCREENS_MAX_SECTIONS]
        other_sections_truncated = len(candidates) - len(capped)
        other_rows = []
        for rows, _ in capped:
            other_rows.extend(rows)
        return [], other_rows, None, other_sections_truncated

    grid_rows, product_count = candidates[grid_index]
    others = [c for i, c in enumerate(candidates) if i != grid_index]
    capped_others = others[:SCREENS_MAX_SECTIONS]
    other_sections_truncated = len(others) - len(capped_others)
    other_rows = []
    for rows, _ in capped_others:
        other_rows.extend(rows)
    return grid_rows, other_rows, product_count, other_sections_truncated


def _extract_row_asins(
    rows: list,
    required_author_asin: str | None = None,
) -> tuple[list[str], int, int, int]:
    """
    Pulls valid, upper-cased ASINs out of a page's rows.

    Admission is row-shape driven when required_author_asin is given: a row
    whose product_metadata carries an authors list is treated as a
    name-attributed rail row and is admitted only when that list names the
    requested author ASIN (compared case-insensitively) -- a miss there is
    counted as an attribution rejection, not an invalid ASIN. A row with no
    authors key at all carries no attribution to check and is admitted
    unconditionally, since the live grid never carries product_metadata
    .authors; this keeps an ambiguous page's grid rows from being silently
    rejected on shape alone when they end up routed through this check.

    Rows beyond SCREENS_MAX_ROWS_PER_PAGE are never inspected; the fourth
    return value reports how many were cut off so the caller can treat that
    as an unclean signal rather than a silent drop.

    Returns (asins, invalid_skipped, attribution_rejected, rows_truncated).
    """
    required = required_author_asin.upper() if required_author_asin else None
    asins: list[str] = []
    invalid_skipped = 0
    attribution_rejected = 0
    rows_truncated = max(0, len(rows) - SCREENS_MAX_ROWS_PER_PAGE)
    for row in rows[:SCREENS_MAX_ROWS_PER_PAGE]:
        if not isinstance(row, dict):
            continue
        product_metadata = row.get("product_metadata")
        if not isinstance(product_metadata, dict):
            continue
        if required is not None:
            authors = product_metadata.get("authors")
            if isinstance(authors, list):
                attributed = any(
                    isinstance(a, dict)
                    and isinstance(a.get("asin"), str)
                    and a["asin"].upper() == required
                    for a in authors
                )
                if not attributed:
                    attribution_rejected += 1
                    continue
        asin = product_metadata.get("asin")
        if not isinstance(asin, str):
            continue
        if is_valid_asin(asin):
            asins.append(asin.upper())
        else:
            invalid_skipped += 1
    return asins, invalid_skipped, attribution_rejected, rows_truncated


def _extract_next_token(data: dict) -> tuple[str | None, bool]:
    """
    Reads the continuation token the way the Android app does: the last
    section's pagination field, not the titles section. Opaque — never
    decoded, only bounds- and shape-checked before reuse.

    Distinguishes a genuinely absent token (no sections, no last section, no
    pagination field, or an explicit empty string -- all read as "no more
    pages") from one that was present but failed its shape check (wrong
    type, over length, or outside the observed token alphabet), since the
    caller treats the former as a clean, confirmed-complete walk and the
    latter as an unclean one.

    Returns (token, rejected).
    """
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        return None, False
    last = sections[-1]
    if not isinstance(last, dict):
        return None, False
    token = last.get("pagination")
    if token is None or token == "":
        return None, False
    if not isinstance(token, str):
        return None, True
    if len(token) > SCREENS_TOKEN_MAX_LEN:
        return None, True
    if not _SCREENS_TOKEN_PATTERN.fullmatch(token):
        return None, True
    return token, False


async def _fetch_author_books_by_screen(
    asin: str,
    region: str,
    deadline: float | None = None,
) -> _ScreenBooksResult:
    """
    Fetches book ASINs for an author from Audible's Android author-detail
    screen, which returns the author's title grid ASIN-exact rather than by
    name-matching a catalog search. Paginates via the opaque continuation
    token until it repeats, goes bad, is absent, or a page/ASIN/time bound
    is hit. A transient failure fetching one page ends the walk but keeps
    every ASIN already harvested from the pages before it rather than
    discarding the whole walk.

    deadline, when given, is an absolute time.monotonic() bound checked
    between pages; once passed the walk stops and returns what it has.

    Never raises for "author not found" — the screen endpoint returns 200
    with zero rows for a bogus ASIN, so the empty result is the caller's
    fallback signal, not an exception.
    """
    if not is_valid_asin(asin):
        return _ScreenBooksResult(
            asins=[],
            pages_fetched=0,
            product_count=None,
            invalid_skipped=0,
            attribution_rejected=0,
            termination_reason=SCREENS_REASON_COMPLETED,
        )
    asin = asin.upper()
    path = f"/1.0/screens/audible-android-author-detail/{asin}"

    asins: list[str] = []
    seen_asins: set[str] = set()
    seen_tokens: set[str] = set()
    invalid_skipped = 0
    attribution_rejected = 0
    sections_truncated = 0
    rows_truncated = 0
    product_count: int | None = None
    token: str | None = None
    pages_fetched = 0
    reason = SCREENS_REASON_PAGE_CAP
    page_error_detail: str | None = None

    while pages_fetched < SCREENS_MAX_PAGES and len(asins) < SCREENS_MAX_ASINS:
        if deadline is not None and time.monotonic() >= deadline:
            reason = SCREENS_REASON_TIME_BUDGET
            break

        params: dict[str, str] = {"author_asin": asin}
        if token:
            params["pageSectionContinuationToken"] = token

        try:
            data = await audible_get(
                region,
                path,
                params,
                extra_headers={"X-Device-Type-Id": ANDROID_DEVICE_TYPE_ID},
            )
        except Exception as e:
            reason = SCREENS_REASON_PAGE_ERROR
            page_error_detail = f"{type(e).__name__}: {e}"
            break
        pages_fetched += 1

        if not isinstance(data, dict):
            reason = SCREENS_REASON_NON_DICT_PAGE
            break

        grid_rows, other_rows, page_product_count, page_sections_truncated = _select_asin_rows(
            data
        )
        sections_truncated += page_sections_truncated
        if page_product_count is not None:
            product_count = page_product_count

        grid_asins, grid_invalid, grid_rejected, grid_rows_truncated = _extract_row_asins(
            grid_rows
        )
        other_asins, other_invalid, other_rejected, other_rows_truncated = _extract_row_asins(
            other_rows, required_author_asin=asin
        )
        invalid_skipped += grid_invalid + other_invalid
        attribution_rejected += grid_rejected + other_rejected
        rows_truncated += grid_rows_truncated + other_rows_truncated
        for row_asin in grid_asins + other_asins:
            if row_asin not in seen_asins:
                seen_asins.add(row_asin)
                asins.append(row_asin)
            if len(asins) >= SCREENS_MAX_ASINS:
                break

        if len(asins) >= SCREENS_MAX_ASINS:
            reason = SCREENS_REASON_ASIN_CAP
            break

        next_token, rejected = _extract_next_token(data)
        if rejected:
            reason = SCREENS_REASON_TOKEN_REJECTED
            break
        if not next_token:
            reason = SCREENS_REASON_COMPLETED
            break
        if next_token in seen_tokens:
            reason = SCREENS_REASON_TOKEN_REPEATED
            break
        seen_tokens.add(next_token)
        token = next_token

    # A real page 1's grid section always reports product_count. A walk that
    # fetched at least one page and ended with no product_count ever seen
    # and not a single ASIN collected has no positive evidence the grid
    # existed on any page it saw -- reaching SCREENS_REASON_COMPLETED that
    # way (an absent/empty sections list, or a page shape upstream renamed)
    # is indistinguishable from a genuine end of pagination unless this is
    # checked explicitly, so it is reclassified as unclean here rather than
    # left to score as a confirmed-complete walk.
    if (
        reason == SCREENS_REASON_COMPLETED
        and pages_fetched > 0
        and product_count is None
        and not asins
    ):
        reason = SCREENS_REASON_GRID_NOT_FOUND

    # A page or row cap that trimmed data during an otherwise clean walk
    # means some upstream content was never looked at -- that cannot be
    # scored as a confirmed-complete read either, even though pagination
    # itself reached its natural end.
    if reason == SCREENS_REASON_COMPLETED and (sections_truncated > 0 or rows_truncated > 0):
        reason = SCREENS_REASON_TRUNCATED

    unclean = reason not in SCREENS_CLEAN_REASONS
    shortfall = product_count is not None and len(asins) < product_count

    if unclean:
        logger.warning("Audible Author Books screen ended without confirmed completion", extra={
            "author_asin": asin,
            "region": region,
            "termination_reason": reason,
            "pages_fetched": pages_fetched,
            "asins_collected": len(asins),
            "page_error": page_error_detail,
            "sections_truncated": sections_truncated,
            "rows_truncated": rows_truncated,
        })
    if shortfall:
        logger.warning("Audible Author Books screen shortfall against reported product_count", extra={
            "author_asin": asin,
            "region": region,
            "termination_reason": reason,
            "pages_fetched": pages_fetched,
            "asins_collected": len(asins),
            "product_count": product_count,
            "shortfall": product_count - len(asins),
        })

    return _ScreenBooksResult(
        asins=asins,
        pages_fetched=pages_fetched,
        product_count=product_count,
        invalid_skipped=invalid_skipped,
        attribution_rejected=attribution_rejected,
        termination_reason=reason,
        page_error=page_error_detail,
        sections_truncated=sections_truncated,
        rows_truncated=rows_truncated,
    )


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
    """Applies the exact-name, English-only, dedupe filter and appends
    matches to asins in-place, in the order products was given."""
    for product in products:
        matches = any(
            a.get("name", "").lower() == name.lower()
            for a in product.get("authors", [])
        )
        language = product.get("language", "").lower()
        is_english = language.startswith("english") or language == "englisch"
        asin = product.get("asin")
        if asin and matches and is_english:
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
    bounds how many pages are ever in flight at once; it defaults to 1
    (fully sequential, one request at a time -- behaviorally identical to
    this function before parallel fetching existed) specifically so the
    shared fetch_author_books_by_name wrapper, which the seeder's paced
    per-author expansion loop calls, is untouched by this: it never passes
    concurrency, so it always runs sequential. get_author_books, the live
    request path, is the only caller that opts into NAME_SEARCH_CONCURRENCY.

    Fetching is still strictly ordered: pages within a batch are requested
    concurrently but always reassembled and processed in ascending page-
    index order (asyncio.gather preserves input order regardless of
    completion order), so the ASIN list this returns for a given author's
    data is identical, in the same order, to what the old one-page-at-a-
    time walk would have returned -- only wall-clock time changes, not the
    result or the number of requests made for a normal-sized catalog (see
    below).

    Termination: a real captured response was read (not the docs) to
    answer this rather than guessing. The catalog endpoint's top-level
    total_results field is genuine and, for any author whose real result
    count stays under Audible's own internal retrieval ceiling for this
    query type (empirically ~500-550 results -- Brandon Sanderson's 203
    paginates exactly to a short final page as total_results promises),
    lets every page this walk will ever need be known after fetching page
    0 alone: page 0 is always fetched solo for exactly that reason -- one
    extra request to learn the true page count is far cheaper than
    guessing wrong in either direction, and it keeps the request count for
    a small catalog identical to the old sequential walk's, not inflated
    by a speculative full concurrency-sized batch. Once total_results is
    known, subsequent batches are sized to the pages it implies (capped at
    NAME_SEARCH_MAX_PAGES).

    But total_results is NOT trustworthy as an upper bound once a query's
    real match count exceeds that same internal ceiling: probed live
    against Arthur Conan Doyle (many editions/narrators/homonyms share the
    name), total_results reported 5367, yet page 10 was the last page with
    new content -- every page from 11 onward silently returned page 10's
    exact content again, forever, rather than a short/empty page or an
    error. Trusting total_results alone there would have paginated through
    ~97 entirely wasted pages. So every page's product-ASIN signature is
    checked against every signature already seen this walk; a repeat is
    treated as having found that same retrieval ceiling and stops the walk
    there as a confirmed-complete end, the same way seen_tokens stops the
    screens walk on an echoing continuation token. This bounds the wasted
    overshoot from that case to at most one batch's width
    (NAME_SEARCH_CONCURRENCY), not the walk's full page cap.

    If total_results is absent from page 0's response, batches fall back
    to speculative concurrency-sized ones, stopping at the first short or
    empty page within a batch (or the repeat check above) -- nothing
    collected before that point is discarded.

    completed is True only when the walk reached one of those two genuine
    end signals (a short/empty page, or a detected content repeat) -- the
    catalog endpoint's own confirmation that nothing further remains. It
    is False for every other stop (the page cap, the deadline, or a
    page-fetch failure), since none of those confirms upstream has nothing
    left to offer; a caller relying on this list as exhaustive needs to
    know the difference, not just that a list came back.

    A failure fetching one page ends the walk but keeps every ASIN already
    harvested from pages before it -- specifically, the result is
    truncated at the last successfully processed page in ascending index
    order (pages later in the same batch that happened to complete are
    discarded, never used to paper over the gap), the same "clean prefix,
    never a hole" guarantee the old sequential walk gave, and mirrors the
    per-page resilience _fetch_author_books_by_screen already has. The
    failing page's index and error are logged.

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
                completed = True
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
    merely stopped -- get_author_books, which does, calls
    _fetch_author_books_by_name_detailed directly instead.

    Always runs at _fetch_author_books_by_name_detailed's default
    concurrency=1 (fully sequential, one page in flight at a time) since
    it never passes concurrency through -- this is deliberate, not an
    oversight: the seeder's paced per-author expansion loop calls this
    wrapper once per author inside its own deliberately spaced loop, and
    parallelizing page fetches inside a helper the seeder shares would
    silently turn every seeder author into a concurrent-request burst,
    defeating that pacing across the seeder's whole run. Only
    get_author_books, the live request path, opts into
    NAME_SEARCH_CONCURRENCY, and it does so by calling
    _fetch_author_books_by_name_detailed directly rather than through this
    wrapper.

    deadline, when given, is an absolute time.monotonic() bound checked
    once before dispatching each batch; once passed the walk stops and
    returns what it has.
    """
    asins, pages_fetched, _ = await _fetch_author_books_by_name_detailed(
        name, region, deadline=deadline
    )
    return asins, pages_fetched


async def _resolve_author_name(
    asin: str,
    region: str,
    session: AsyncSession,
) -> str | None:
    """
    Resolves an author ASIN to their name, checking DB first and then
    Audible's contributors endpoint.

    Returns None only for the terminal, non-degraded case: Audible confirms
    this author carries no name (a 404, or a 200 with an empty name field).
    Any other failure fetching from Audible -- a timeout, a malformed
    response, anything that is not a confirmed absence -- propagates rather
    than collapsing into the same None, so the caller can tell "this author
    has no name on record" apart from "name resolution failed" instead of
    treating both as the same silent miss.
    """
    db_author = await get_author_from_db(session, asin, region)
    if db_author and db_author.get("name"):
        return db_author["name"]

    try:
        data = await _fetch_author_details(asin, region)
    except NotFoundException:
        return None

    name = data.get("contributor", {}).get("name", "").replace("\t", "").strip()
    if name:
        normalized = _normalize_author(data, asin, region)
        persist_author_background(normalized, region)
        return name

    return None


# ============================================================
# PUBLIC API
# ============================================================


async def get_author(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> dict[str, Any]:
    """
    Fetches author profile by ASIN.
    Audible-first, writes to DB, falls back to DB then cache.
    """
    if use_cache:
        cached = await cache.get(session, author_key(asin, region))
        if cached:
            return cached

    try:
        start = time.monotonic()
        data = await _fetch_author_details(asin, region)
        author_took = round((time.monotonic() - start) * 1000, 2)

        if not data or data.get("contributor", {}).get("name") is None:
            raise NotFoundException(f"Author not found: {asin}")

        normalized = _normalize_author(data, asin, region)

        # Persist to DB and cache in the background
        persist_author_background(normalized, region)

        logger.info("Requested Audible Author", extra={
            "author_took": author_took,
            "region": region,
        })

        return normalized

    except NotFoundException:
        raise
    except Exception:
        # Try DB first
        db_result = await get_author_from_db(session, asin, region)
        if db_result:
            return db_result

        # Fall back to cache
        cached = await cache.get(session, author_key(asin, region))
        if cached:
            return cached

        raise NotFoundException("Audible unavailable and no cached author data found")


async def get_author_books(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> list[str]:
    """
    Fetches all book ASINs for an author.

    Unions two independent Audible paths so the result can never be smaller
    than either alone: the standard catalog search by the author's resolved
    name, and the Android author-detail screen, which returns the author's
    title grid ASIN-exact. The name search runs first, against the full time
    budget, and the screens walk runs second against whatever budget is
    left: the name-search half is the one consumers already receive today
    (get_author_books_by_name serves it standalone), so a degraded upstream
    must not be able to starve it of every request before it gets to run --
    a screens walk that consumes the whole budget and leaves the name search
    zero requests would silently turn this union into screens-only, and
    screens is not a superset of name-search results. The local DB is
    consulted at two separate points: as a backstop appended to a degraded
    but non-empty union (any DB-known ASIN neither Audible path surfaced),
    and, only when both Audible paths come back entirely empty, as a full
    fallback in its own right, followed by cache.

    List order is a consumer-visible contract: name-search ASINs come
    first, in fetch_author_books_by_name's own (-ReleaseDate) order, and
    screens-only extras are appended after, in screens order. Nothing in
    this path is sorted — sort_dicts leaves order untouched when no sort
    param is passed, and both routes default to None, so this order reaches
    consumers directly. Ordering name-first means every ASIN a caller sees
    today keeps its index; only new titles append.
    """
    if use_cache:
        cached = await cache.get(session, author_books_key(asin, region))
        if cached:
            return cached

    start = time.monotonic()
    deadline = start + AUTHOR_BOOKS_TIME_BUDGET_SECONDS

    author_name: str | None = None
    name_asins: list[str] = []
    name_pages_fetched = 0
    name_completed = True
    name_error: str | None = None
    try:
        author_name = await _resolve_author_name(asin, region, session)
        if author_name:
            name_asins, name_pages_fetched, name_completed = (
                await _fetch_author_books_by_name_detailed(
                    author_name, region, deadline=deadline,
                    concurrency=NAME_SEARCH_CONCURRENCY,
                )
            )
    except Exception as e:
        name_error = f"{type(e).__name__}: {e}"

    screen_result: _ScreenBooksResult | None = None
    screen_error: str | None = None
    try:
        screen_result = await _fetch_author_books_by_screen(asin, region, deadline=deadline)
    except Exception as e:
        screen_error = f"{type(e).__name__}: {e}"
    screen_asins = screen_result.asins if screen_result is not None else []

    # Union ordering: name-search ASINs first, in their own order, then any
    # screens-only ASIN not already present, in screens order.
    seen = set(name_asins)
    asins = list(name_asins)
    for screen_asin in screen_asins:
        if screen_asin not in seen:
            seen.add(screen_asin)
            asins.append(screen_asin)

    # name_clean is False whenever the name half errored, or ran but didn't
    # reach a confirmed natural end (deadline-truncated or page-capped); it
    # stays True when there was simply no name to search, since that half
    # is then trivially complete rather than degraded.
    name_clean = name_error is None and name_completed
    name_degraded = not name_clean

    # The DB fallback further down only fires when the union is entirely
    # empty, so a degraded name half would otherwise skip this backstop and
    # lose any DB-known title neither Audible path happened to surface -- a
    # silent shrink against what a pure DB read for this author ASIN would
    # offer on its own. This covers both arms of that: a degraded name half
    # with an empty screens half (screen_asins alone used to gate this off
    # before), and a degraded name half with a page-capped or otherwise
    # partial but non-empty screens half that still doesn't cover every
    # DB-known title. Any DB-known ASIN not already present is appended at
    # the tail instead -- the existing prefix (name-search results, then
    # screens extras) is never disturbed, only extended. Canonicalized
    # upper-case here to match the three sibling ASIN producers above,
    # since the reader returns ASINs as stored, not normalized.
    if name_degraded:
        db_asins = await get_author_book_asins_from_db(session, asin, region)
        for db_asin in db_asins:
            db_asin = db_asin.upper()
            if db_asin not in seen:
                seen.add(db_asin)
                asins.append(db_asin)

    if not asins:
        db_asins = await get_author_book_asins_from_db(session, asin, region)
        if db_asins:
            return [db_asin.upper() for db_asin in db_asins]

        cached = await cache.get(session, author_books_key(asin, region))
        if cached:
            return cached

        logger.warning("Audible Author Books unavailable from every path", extra={
            "author_asin": asin,
            "region": region,
            "screen_error": screen_error,
            "screen_page_error": screen_result.page_error if screen_result else None,
            "name_error": name_error,
        })
        if screen_error is not None or name_error is not None:
            raise NotFoundException("Audible unavailable and no cached author books found")
        raise NotFoundException(f"No books found for author: {asin}")

    author_book_took = round((time.monotonic() - start) * 1000, 2)

    # less-data-never-accepted: this union is only ever written back to
    # cache as the authoritative list when the screens walk confirmed it
    # saw every upstream page (a clean termination -- SCREENS_CLEAN_REASONS
    # already excludes every cap, page error, bad token, grid-not-found,
    # time-budget, and truncation outcome, so screens_clean alone is the
    # full "terminated cleanly" gate) and the name search that seeds the
    # front of the list reached its own confirmed natural end. Any walk
    # short of that is still served to this caller, but is not persisted as
    # if it were complete -- this also covers the DB-backstopped union
    # above, since a degraded name half never satisfies name_clean.
    #
    # This gate used to also require len(asins) >= product_count, but
    # Audible's own reported product_count routinely over-counts by one or
    # two (delisted or region-restricted titles still counted), and a name
    # search that hit its own page cap (back when that cap was still small
    # enough to bind on a real, prolific author) was never "completed" --
    # combined, that made the write essentially never fire for exactly the
    # prolific authors this union exists to serve. product_count is still
    # checked for the shortfall WARNING below; it no longer vetoes the
    # write. The backstop against writing back less than what's already
    # stored is the shrink guard in the writer, which re-reads the stored
    # list and refuses a shorter one or a strict subset.
    screens_clean = (
        screen_result is not None
        and screen_result.termination_reason in SCREENS_CLEAN_REASONS
    )
    if screens_clean and name_clean:
        persist_author_books_cache_background(author_books_key(asin, region), asins)

    # A path that errored still counts as a degraded response even when the
    # union it contributed to came back non-empty -- folding screen_error /
    # name_error into the INFO line below as bare fields would let that
    # degradation stay under the WARNING threshold and never reach a
    # WARNING-level alert or stderr.
    if screen_error is not None or name_error is not None:
        logger.warning("Audible Author Books served from a degraded path", extra={
            "author_asin": asin,
            "region": region,
            "screen_error": screen_error,
            "screen_page_error": screen_result.page_error if screen_result else None,
            "name_error": name_error,
        })

    logger.info("Requested Audible Author Books", extra={
        "author_asin": asin,
        "author_name": author_name,
        "author_book_num": len(asins),
        "screen_asin_num": len(screen_asins),
        "name_asin_num": len(name_asins),
        "screen_pages_fetched": screen_result.pages_fetched if screen_result else 0,
        "screen_product_count": screen_result.product_count if screen_result else None,
        "screen_invalid_skipped": screen_result.invalid_skipped if screen_result else 0,
        "screen_attribution_rejected": screen_result.attribution_rejected if screen_result else 0,
        "screen_sections_truncated": screen_result.sections_truncated if screen_result else 0,
        "screen_rows_truncated": screen_result.rows_truncated if screen_result else 0,
        "screen_termination_reason": screen_result.termination_reason if screen_result else None,
        "screen_error": screen_error,
        "screen_page_error": screen_result.page_error if screen_result else None,
        "name_pages_fetched": name_pages_fetched,
        "name_completed": name_completed,
        "name_error": name_error,
        "author_book_took": author_book_took,
        "region": region,
    })

    return asins


async def get_author_books_by_name(
    name: str,
    region: str,
    session: AsyncSession,
) -> list[str]:
    """Fetches book ASINs by author name."""
    try:
        start = time.monotonic()
        asins, pages_fetched = await fetch_author_books_by_name(name, region)
        author_book_took = round((time.monotonic() - start) * 1000, 2)

        if not asins:
            raise NotFoundException(f"No books found for author name: {name}")

        logger.info("Requested Audible Author Books By Name", extra={
            "author_name": name,
            "author_book_num": len(asins),
            "pages_fetched": pages_fetched,
            "author_book_took": author_book_took,
            "region": region,
        })

        return asins

    except NotFoundException:
        raise
    except Exception:
        raise NotFoundException("Failed to fetch author books by name")


async def search_authors(
    name: str,
    region: str,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Searches for authors by name using Audible search suggestions."""
    try:
        start = time.monotonic()
        path = "/1.0/searchsuggestions"
        params = {
            "keywords": name,
            "key_strokes": name,
            "site_variant": "android-mshop",
            "session_id": _generate_session_id(),
            "local_time": datetime.utcnow().isoformat(),
            "surface": "Android",
        }

        data = await audible_get(region, path, params)
        search_took = round((time.monotonic() - start) * 1000, 2)

        asins: list[str] = []
        for item in data.get("model", {}).get("items", []):
            if item.get("view", {}).get("template") == "AuthorItemV2":
                asin = item.get("model", {}).get("person_metadata", {}).get("asin")
                if asin:
                    asins.append(asin)

        logger.info("Requested Audible Author Search", extra={
            "search_took": search_took,
            "region": region,
        })

        if not asins:
            return []

        authors = []
        for asin in asins:
            try:
                author = await get_author(asin, region, session)
                authors.append(author)
            except NotFoundException:
                continue

        return authors

    except NotFoundException:
        raise
    except Exception:
        raise NotFoundException("Author search failed")