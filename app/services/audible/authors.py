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
import base64
import json
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

# Audible's page size for this screen is fixed at 20 rows regardless of
# num_results/page_size query params -- both tried live and confirmed
# inert. That fixed width is what makes it possible to compute, from
# product_count alone, how many pages a walk will need before fetching any
# of them, and therefore to fan out to pages 2..N directly (see
# _fanout_screen_pages) instead of only ever discovering page N+1 by
# walking page N first.
SCREENS_PAGE_SIZE = 20

# The page_load_id field inside a minted continuation token (see
# _mint_screen_token). Verified live: a real captured value, a fabricated
# one, and omitting the field entirely all returned byte-identical pages --
# there is nothing to harvest from a real response, so this is a fixed
# constant rather than anything scraped off page 1.
_SCREENS_TOKEN_PAGE_LOAD_ID = "libex-direct-page"

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

# Bounded concurrency for the screens walk's page 2..N fan-out (see
# _fanout_screen_pages). This IP is shared with the live seeder and has
# already been throttled into a VPN rotation once from an amplified
# request burst, and that risk is the harder failure mode (indefinite,
# global) to weigh against the alternative of going too low. Too low
# reintroduces the exact problem the fan-out exists to fix: the author's
# underlying list drifts (a live probe of the identical page 25-40
# seconds apart returned different rows), and a walk that stretches back
# into that drift window starts silently duplicating and dropping titles
# at page boundaries again. At 8 in flight and roughly 0.5s/page, Conan
# Doyle's ~225-page catalog (the largest known real one) is ~28 batches,
# ~14s wall clock -- comfortably inside the 25-40s drift window, not a
# photo finish. Unlike the catalog multi-sort walk's own wave 3
# (_fetch_author_books_by_catalog), which fires its remaining pages in one
# unbounded gather and relies on the shared client's own global
# throttling, this walk keeps its own explicit per-batch bound, since it
# is the walk that surfaced the drift problem the bound exists to fix.
SCREENS_FANOUT_CONCURRENCY = 8

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
# A repeated page signature during the direct-page-addressed fan-out
# (_fanout_screen_pages) is NOT the same signal as a null continuation
# token. Verified live: a real 404 past an author's true last page
# (Sanderson page 10) confirms genuine completion, but requesting a page
# past the true last one via a minted token does not error or come back
# empty -- it PLATEAUS, returning the last real page's content again,
# byte-identical, forever (Christie page 30 of a 56-page product_count-
# implied walk repeats page 26's real content). A walk that stops on a
# repeated signature has NOT been told by upstream that nothing further
# remains -- product_count itself overstated the real catalog by more
# than 2x for Christie -- so this is scored unclean, distinct from
# SCREENS_REASON_COMPLETED, even though the walk correctly stops rather
# than looping on repeated content forever.
SCREENS_REASON_PLATEAU_TRUNCATED = "plateau_truncated"
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
        # Validated here, unlike the two /1.0/catalog/products-sourced
        # walks (_process_catalog_page, _accept_name_search_products),
        # which admit any truthy ASIN because that endpoint mixes in
        # ISBN-keyed records: this endpoint is the Android author-detail
        # screens grid, a different response shape with no such records
        # observed, so a non-B-format value here is invalid rather than a
        # legitimate class of record to keep.
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


async def _fetch_screen_page(
    asin: str,
    region: str,
    path: str,
    token: str | None,
) -> dict:
    """
    Fetches a single screens page for an already-upper-cased author ASIN.
    Raises on failure; callers (the sequential walk in
    _fetch_author_books_by_screen and the concurrent one in
    _fanout_screen_pages) decide what a failed page means for their walk.
    """
    params: dict[str, str] = {"author_asin": asin}
    if token:
        params["pageSectionContinuationToken"] = token
    return await audible_get(
        region,
        path,
        params,
        extra_headers={"X-Device-Type-Id": ANDROID_DEVICE_TYPE_ID},
    )


def _mint_screen_token(page_num: int) -> str:
    """
    Builds a pageSectionContinuationToken for an arbitrary page directly,
    without ever having walked to page_num - 1 first.

    Verified live against the real endpoint: a
    real captured token, base64-decoded, is just
    {"scheduling_info": {"page_load_id": "...", "slot": "center-10",
    "version": "1"}, "pagination_info": {"page_num": "<N>"}}.
    page_load_id is completely ignored by the endpoint -- a real captured
    value, a fabricated one, and omitting the field entirely all returned
    byte-identical pages -- so a fixed constant is used here rather than
    anything harvested from a response. slot must be present and must be
    the literal string "center-10": omitting it 400s, a garbage value
    404s. version is optional and omitted here (confirmed live to change
    nothing). page_num is a STRING in the real payloads, matched here
    rather than an int, and the JSON carries no whitespace, matching what
    Audible's own client sends.
    """
    payload = {
        "scheduling_info": {
            "page_load_id": _SCREENS_TOKEN_PAGE_LOAD_ID,
            "slot": "center-10",
        },
        "pagination_info": {"page_num": str(page_num)},
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("ascii")
    return base64.b64encode(raw).decode("ascii")


async def _fanout_screen_pages(
    asin: str,
    region: str,
    path: str,
    total_pages: int,
    deadline: float | None,
    asins: list[str],
    seen_asins: set[str],
    seen_page_signatures: set[tuple[str, ...]],
    invalid_skipped: int,
    attribution_rejected: int,
    sections_truncated: int,
    rows_truncated: int,
) -> tuple[int, int, int, int, int, str, str | None]:
    """
    Fetches pages 2..total_pages of the screens walk concurrently, bounded
    by SCREENS_FANOUT_CONCURRENCY, using continuation tokens minted
    directly per page (_mint_screen_token) rather than harvested from the
    previous page's response -- which is what makes fetching them out of
    walk order possible at all. Only ever called once product_count on page
    1 was a usable positive int and implies more than one page (see
    _fetch_author_books_by_screen); a missing or unusable product_count
    keeps that caller on its own sequential, token-following pagination
    loop instead.

    This exists because the underlying list drifts under a slow sequential
    walk: a live probe of the identical page 25-40 seconds apart returned
    different rows -- a new ASIN at the front, the tail item pushed off --
    while pages fetched roughly 2 seconds apart tiled perfectly, zero
    overlap and zero gap. A large author walked one page at a time (Conan
    Doyle's ~4500 titles is ~225 pages, roughly two minutes sequentially)
    silently duplicates and drops titles at page boundaries purely from
    taking too long to read the whole list; the concurrency here is what
    makes the result correct, not merely faster.

    total_pages, computed by the caller from product_count, is never
    trusted as an exact upper bound: probed live, requesting a page past a
    real author's last one does not error or come back empty, it
    PLATEAUS -- returning the last real page's content again,
    byte-identical, forever, while the requested page number keeps
    climbing (pages 56, 57, and 999 of a real 56-page author all came back
    identical). So every page's ASIN signature is checked against every
    signature already seen this walk (seen_page_signatures, seeded by the
    caller with page 1's own); a repeat means the page immediately before
    it was the true last page the fan-out will ever see new content from.
    Unlike a null continuation token (a genuine upstream confirmation that
    nothing remains -- the same signal a real 404 gives the sequential
    walk), a repeated signature is this walk noticing it has started
    plateauing on its own, not upstream telling it so: product_count is
    exactly what fed total_pages here, and it overstated Christie's real
    catalog by more than 2x. So a repeat is scored SCREENS_REASON_
    PLATEAU_TRUNCATED -- unclean, distinct from SCREENS_REASON_COMPLETED --
    even though the walk still correctly stops there rather than looping
    on repeated content up to total_pages. Nothing from the repeating page
    onward is kept either way.

    Reassembly is always strict page-index order, never completion order:
    asyncio.gather preserves input order regardless of which request
    finishes first, and batches themselves are dispatched and folded in in
    ascending page-number order, so a clean walk's ASIN list ends up in
    the same order a page-at-a-time walk would produce.

    A page-fetch failure, a non-dict page, or the SCREENS_MAX_ASINS cap
    all end the walk at exactly the point they would in the sequential
    walk -- everything from pages before that point is kept (asins and
    seen_asins are mutated in place as pages are folded in, so the
    caller's copies are already up to date on return), nothing from that
    point on is used, even if it was already fetched as part of the same
    batch. total_pages itself is pre-clamped to SCREENS_MAX_PAGES by the
    caller; running the fan-out all the way through that clamp without a
    clean stop is reported as SCREENS_REASON_PAGE_CAP, the same as the
    sequential walk's own page cap -- this only ever fires against a
    pathological upstream, since no real catalog approaches that page
    count.

    deadline, when given, is an absolute time.monotonic() bound checked
    once before dispatching each batch; once passed the walk stops without
    starting another batch and keeps what it already has.

    Returns (pages_fetched, invalid_skipped, attribution_rejected,
    sections_truncated, rows_truncated, termination_reason, page_error) --
    the last five folding the caller's own running totals in, the same
    convention _fetch_author_books_by_screen already uses for its own
    counters.
    """
    page_cap_clamped = total_pages >= SCREENS_MAX_PAGES
    pages_fetched = 0
    reason = SCREENS_REASON_COMPLETED
    page_error_detail: str | None = None
    next_page_num = 2

    while next_page_num <= total_pages:
        if deadline is not None and time.monotonic() >= deadline:
            reason = SCREENS_REASON_TIME_BUDGET
            break

        batch_page_nums = list(
            range(next_page_num, min(next_page_num + SCREENS_FANOUT_CONCURRENCY, total_pages + 1))
        )
        results = await asyncio.gather(
            *(_fetch_screen_page(asin, region, path, _mint_screen_token(p)) for p in batch_page_nums),
            return_exceptions=True,
        )

        stop = False
        for result in results:
            if isinstance(result, BaseException):
                reason = SCREENS_REASON_PAGE_ERROR
                page_error_detail = f"{type(result).__name__}: {result}"
                stop = True
                break

            if not isinstance(result, dict):
                reason = SCREENS_REASON_NON_DICT_PAGE
                stop = True
                break

            grid_rows, other_rows, _page_product_count, page_sections_truncated = (
                _select_asin_rows(result)
            )
            sections_truncated += page_sections_truncated
            grid_asins, grid_invalid, grid_rejected, grid_rows_truncated = _extract_row_asins(
                grid_rows
            )
            other_asins, other_invalid, other_rejected, other_rows_truncated = _extract_row_asins(
                other_rows, required_author_asin=asin
            )
            invalid_skipped += grid_invalid + other_invalid
            attribution_rejected += grid_rejected + other_rejected
            rows_truncated += grid_rows_truncated + other_rows_truncated
            pages_fetched += 1

            page_asins = grid_asins + other_asins
            signature = tuple(page_asins)
            is_plateau = signature in seen_page_signatures
            seen_page_signatures.add(signature)

            for row_asin in page_asins:
                if row_asin not in seen_asins:
                    seen_asins.add(row_asin)
                    asins.append(row_asin)
                if len(asins) >= SCREENS_MAX_ASINS:
                    break

            if len(asins) >= SCREENS_MAX_ASINS:
                reason = SCREENS_REASON_ASIN_CAP
                stop = True
                break

            if is_plateau:
                reason = SCREENS_REASON_PLATEAU_TRUNCATED
                stop = True
                break

        if stop:
            break
        next_page_num = batch_page_nums[-1] + 1
    else:
        if page_cap_clamped:
            reason = SCREENS_REASON_PAGE_CAP

    return (
        pages_fetched,
        invalid_skipped,
        attribution_rejected,
        sections_truncated,
        rows_truncated,
        reason,
        page_error_detail,
    )


async def _fetch_author_books_by_screen(
    asin: str,
    region: str,
    deadline: float | None = None,
) -> _ScreenBooksResult:
    """
    Fetches book ASINs for an author from Audible's Android author-detail
    screen, which returns the author's title grid ASIN-exact rather than by
    name-matching a catalog search.

    Page 1 is always fetched sequentially -- it is the only way to learn
    product_count, and its teaser ("most popular") section carries titles
    absent from the grid. From there:

    - When page 1 reports a usable product_count (a positive int) that
      implies more than one page at Audible's fixed SCREENS_PAGE_SIZE,
      pages 2..N are fetched concurrently, bounded by
      SCREENS_FANOUT_CONCURRENCY, via _fanout_screen_pages -- see that
      function's docstring for why this is a correctness fix (the
      underlying list drifts under a slow sequential walk) and not merely
      a speed one, and for how it still detects and stops at the real end
      of the catalog even when product_count overstates it.
    - Otherwise (product_count missing, zero, or implying only page 1 is
      needed), the walk falls back to the original behavior: paginating
      via the opaque continuation token, one page at a time, until it
      repeats, goes bad, is absent, or a page/ASIN/time bound is hit. This
      keeps small catalogs and a page-shape change upstream degrading
      gracefully rather than breaking outright.

    A transient failure fetching one page ends the walk but keeps every
    ASIN already harvested from the pages before it rather than discarding
    the whole walk, in both modes.

    deadline, when given, is an absolute time.monotonic() bound checked
    between pages (sequential mode) or once before each batch (fan-out
    mode); once passed the walk stops and returns what it has.

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

        try:
            data = await _fetch_screen_page(asin, region, path, token)
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

        # Direct page addressing: once page 1 has told us how many titles
        # the author has, every remaining page's continuation token can be
        # minted outright (see _mint_screen_token) instead of only ever
        # being learned by fetching the page before it -- so pages 2..N
        # are fetched concurrently here rather than one at a time for the
        # rest of this loop. Only attempted once, on page 1: a usable
        # product_count that implies more than SCREENS_PAGE_SIZE titles is
        # required, so a missing/zero/unusable product_count (or one small
        # enough that page 1 is already the whole catalog) falls through
        # unchanged to the original token-chasing loop below, which is the
        # explicit fallback this walk must degrade to rather than break on.
        if pages_fetched == 1 and isinstance(product_count, int) and product_count > 0:
            total_pages = min(-(-product_count // SCREENS_PAGE_SIZE), SCREENS_MAX_PAGES)
            if total_pages > 1:
                (
                    fanout_pages_fetched,
                    invalid_skipped,
                    attribution_rejected,
                    sections_truncated,
                    rows_truncated,
                    reason,
                    page_error_detail,
                ) = await _fanout_screen_pages(
                    asin,
                    region,
                    path,
                    total_pages=total_pages,
                    deadline=deadline,
                    asins=asins,
                    seen_asins=seen_asins,
                    seen_page_signatures={tuple(grid_asins + other_asins)},
                    invalid_skipped=invalid_skipped,
                    attribution_rejected=attribution_rejected,
                    sections_truncated=sections_truncated,
                    rows_truncated=rows_truncated,
                )
                pages_fetched += fanout_pages_fetched
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

    # No warning here compares len(asins) against product_count as a
    # shortfall signal: probed live, Christie's grid-section product_count
    # claims 1116 while the grid itself plateaus at 520 real ASINs --
    # product_count over-claims by more than 2x, so a magnitude computed
    # against it would be noise, not signal, on exactly the prolific
    # authors this walk cares most about getting right. termination_reason
    # already carries the meaningful distinction such a comparison would
    # be trying to surface (SCREENS_REASON_PLATEAU_TRUNCATED vs
    # SCREENS_REASON_COMPLETED vs every other unclean reason), and
    # product_count is still included below for anyone reading the log who
    # wants to eyeball it themselves.
    if unclean:
        logger.warning("Audible Author Books screen ended without confirmed completion", extra={
            "author_asin": asin,
            "region": region,
            "termination_reason": reason,
            "pages_fetched": pages_fetched,
            "asins_collected": len(asins),
            "product_count": product_count,
            "page_error": page_error_detail,
            "sections_truncated": sections_truncated,
            "rows_truncated": rows_truncated,
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
    Fetches all book ASINs for an author, as a four-source parallel union:
    the Android author-detail screen (the only ASIN-exact source), and the
    catalog endpoint walked across three sort orders (-ReleaseDate,
    ascending ReleaseDate, -Title). None of the four is complete or a
    subset of another -- verified live against Agatha Christie
    (product_count 1116 on the screens grid, total_results 1138 on the
    catalog): the screens grid itself serves only 520 unique ASINs before
    plateauing; -ReleaseDate alone plateaus at 500 with zero overlap
    against ascending ReleaseDate's own, different 499; and 28 of the screens
    grid's 520 ASINs never appear anywhere in the catalog union at all.
    Only the union of every source gets close to complete.

    Four waves:
      1. Resolve the author ASIN to a name via the contributors lookup --
         required because the catalog has no author-ASIN filter (probed
         live: author_asin, authorAsin, contributor_asin, and author_id
         are all silently ignored).
      2 & 3. The screens walk (_fetch_author_books_by_screen) and the
         catalog walk (_fetch_author_books_by_catalog) each run their own
         internal page-1/page-0-then-fan-out sequence, but the two
         top-level walks run concurrently with each other via
         asyncio.gather -- so screens' page 1 and every catalog sort's
         page 0 go out together, and each source's own remaining pages go
         out together shortly after, without one source's walk blocking
         the other's. Neither walk applies its own concurrency bound
         beyond what's already built into it; the shared client throttles
         fan-out globally.
      4. Union everything: catalog ASINs first (already -ReleaseDate-
         first internally -- _fetch_author_books_by_catalog folds every
         sort's pages into its own result strictly one sort at a time, in
         _CATALOG_SORTS priority order, not merely in fetch order), then
         any screens-only extra, then any DB-only extra the two live
         sources didn't happen to surface this run.

    List order is a consumer-visible contract: consumers have always
    received the -ReleaseDate catalog list at the front of the response
    and must keep receiving that same list, in that same order, unmoved
    at the front of this union.
    Nothing in this path is sorted -- sort_dicts leaves order untouched
    when no sort param is passed, and both routes default to None, so
    this order reaches consumers directly.

    The DB's own record of this author's books is unioned in on every
    request, not only when a live source fails -- this is the
    less-data-never-accepted invariant applied at the response level: once
    Libex has seen a book for an author, it can never vanish from this
    response just because a source timed out or a sort window came back
    short this run.

    A source that fails (screens raises, or every catalog sort errors)
    does not fail the whole request -- whatever the other sources and the
    DB union surfaced is still served. Only when every source came back
    entirely empty, live and DB alike, is NotFoundException raised.
    """
    if use_cache:
        cached = await cache.get(session, author_books_key(asin, region))
        if cached:
            return cached

    start = time.monotonic()
    deadline = start + AUTHOR_BOOKS_TIME_BUDGET_SECONDS

    # Wave 1.
    author_name: str | None = None
    name_resolution_error: str | None = None
    try:
        author_name = await _resolve_author_name(asin, region, session)
    except Exception as e:
        name_resolution_error = f"{type(e).__name__}: {e}"

    # _resolve_author_name queries the DB (get_author_from_db) and, on the
    # name-write path, only ever hands off to a background task running
    # its own separate session (persist_author_background) -- nothing
    # past this point still needs session's transaction open. Releasing
    # it here, rather than leaving it checked out idle-in-transaction
    # across the network gather below (bounded by
    # AUTHOR_BOOKS_TIME_BUDGET_SECONDS) and the hydration after it,
    # returns the connection to a pool shared with the seeder and
    # background writers; SQLAlchemy re-acquires automatically the next
    # time session touches the DB (the wave 4 DB backstop read further
    # down).
    await session.rollback()

    # Waves 2 & 3: screens and catalog run concurrently as two top-level
    # tasks. Catalog is only scheduled at all when a name was resolved --
    # there is nothing to query the catalog with otherwise.
    tasks = [_fetch_author_books_by_screen(asin, region, deadline=deadline)]
    if author_name:
        tasks.append(
            _fetch_author_books_by_catalog(asin, author_name, region, deadline=deadline)
        )
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    screen_outcome = outcomes[0]
    screen_result: _ScreenBooksResult | None = None
    screen_error: str | None = None
    if isinstance(screen_outcome, BaseException):
        screen_error = f"{type(screen_outcome).__name__}: {screen_outcome}"
    else:
        screen_result = screen_outcome
    screen_asins = screen_result.asins if screen_result is not None else []

    catalog_result: _CatalogBooksResult | None = None
    catalog_error: str | None = None
    if author_name:
        catalog_outcome = outcomes[1]
        if isinstance(catalog_outcome, BaseException):
            catalog_error = f"{type(catalog_outcome).__name__}: {catalog_outcome}"
        else:
            catalog_result = catalog_outcome
    catalog_asins = catalog_result.asins if catalog_result is not None else []

    # Measured by _fetch_author_books_by_catalog itself, from what the
    # catalog wave actually observed on the wire (see
    # _CatalogBooksResult.ceiling_saturated) -- a sort's own pages caught
    # plateauing while the union it fed still fell short of upstream's own
    # total_results claim, not an arithmetic threshold guessed against a
    # constant.
    catalog_ceiling_saturated = catalog_result is not None and catalog_result.ceiling_saturated

    # catalog_error alone is essentially never set: _fetch_author_books_by_catalog
    # swallows every per-page and per-sort failure into sort_errors /
    # truncated_by_deadline rather than raising, so an outright exception
    # from the gather is the rare case, not the common one. catalog_degraded
    # is the signal that actually reflects that -- it also covers those
    # swallowed failures -- but deliberately excludes
    # catalog_ceiling_saturated (that has its own dedicated warning below
    # with the exact ceiling numbers attached; reusing this signal for it
    # too would double-report the identical condition) and excludes the
    # case where catalog never ran because no author name was resolved
    # (that is name_resolution_error's concern, not catalog's).
    catalog_degraded = (
        author_name is not None
        and not catalog_ceiling_saturated
        and (
            catalog_error is not None
            or catalog_result is None
            or catalog_result.sort_errors
            or catalog_result.truncated_by_deadline
        )
    )

    # Wave 4, part one: catalog first (already -ReleaseDate-first
    # internally), then any screens-only extra. Dedupe is ASIN-only, never
    # on title -- the screens grid alone holds 18 distinct ASINs of a
    # single title, each a real, separately trackable edition.
    seen = set(catalog_asins)
    asins = list(catalog_asins)
    for screen_asin in screen_asins:
        if screen_asin not in seen:
            seen.add(screen_asin)
            asins.append(screen_asin)

    # Wave 4, part two: the DB backstop, unioned on every request rather
    # than only when a live source came back short or empty (see
    # docstring). Canonicalized upper-case here to match the two live
    # producers above, since the reader returns ASINs as stored, not
    # normalized. get_author_book_asins_from_db returns None, distinct
    # from an empty list, when the read itself failed -- treated as empty
    # here for the union (there is nothing else to add), but db_clean /
    # db_error track the failure itself for the cache-write gate and the
    # logs below, so a failed DB read can never look identical to "this
    # author genuinely has no stored books" downstream.
    db_asins = await get_author_book_asins_from_db(session, asin, region)
    db_clean = db_asins is not None
    db_error = None if db_clean else "DB read for author book asins returned None"
    for db_asin in db_asins or []:
        db_asin = db_asin.upper()
        if db_asin not in seen:
            seen.add(db_asin)
            asins.append(db_asin)

    if not asins:
        cached = await cache.get(session, author_books_key(asin, region))
        if cached:
            return cached

        logger.warning("Audible Author Books unavailable from every path", extra={
            "author_asin": asin,
            "region": region,
            "screen_error": screen_error,
            "screen_page_error": screen_result.page_error if screen_result else None,
            "catalog_error": catalog_error,
            "catalog_sort_errors": catalog_result.sort_errors if catalog_result else [],
            "name_resolution_error": name_resolution_error,
            "db_error": db_error,
        })
        if (
            screen_error is not None
            or catalog_degraded
            or name_resolution_error is not None
            or db_error is not None
        ):
            raise NotFoundException("Audible unavailable and no cached author books found")
        raise NotFoundException(f"No books found for author: {asin}")

    author_book_took = round((time.monotonic() - start) * 1000, 2)

    # Completeness signal, measured against this union's own len(asins) --
    # never against what a single source alone returned, since
    # product_count over-claims a single source's reach by more than 2x
    # (see _fetch_author_books_by_screen, where the same reasoning is why
    # no comparable per-source warning exists there either).
    # catalog_result.total_results is the preferred claim;
    # screen_result.product_count is used only when
    # catalog produced none at all (catalog errored, or its page 0 never
    # carried the field), since the two describe the same underlying
    # catalog size rather than independent counts to add together. The
    # union can legitimately exceed the claim -- screens and the DB
    # backstop both surface ASINs the catalog sorts never do -- so an
    # overshoot is never treated as a shortfall.
    claimed_total = catalog_result.total_results if catalog_result is not None else None
    if claimed_total is None and screen_result is not None:
        claimed_total = screen_result.product_count

    if claimed_total is None:
        logger.info("Audible Author Books completeness check skipped: no claimed total from catalog or screens", extra={
            "author_asin": asin,
            "region": region,
        })
    else:
        shortfall = claimed_total - len(asins)
        if shortfall > 0:
            shortfall_ratio = shortfall / claimed_total
            # Christie -- a real, healthy result -- sits 5 short of a 1138
            # claim, a 0.4% gap that must not warn or the signal is noise
            # again on day one. 10% clears that gap with wide margin while
            # still catching a result at half the claim, which obviously
            # must warn. Excluded when catalog_ceiling_saturated: that case
            # is reported separately below, at the cache-write gate, with
            # the exact ceiling numbers attached rather than a ratio -- the
            # two would otherwise both fire over the identical condition.
            if shortfall_ratio > 0.10 and not catalog_ceiling_saturated:
                logger.warning("Audible Author Books union fell short of the claimed total", extra={
                    "author_asin": asin,
                    "region": region,
                    "author_book_num": len(asins),
                    "claimed_total": claimed_total,
                    "shortfall": shortfall,
                    "shortfall_ratio": round(shortfall_ratio, 4),
                    "screens_plateau_truncated": (
                        screen_result is not None
                        and screen_result.termination_reason == SCREENS_REASON_PLATEAU_TRUNCATED
                    ),
                })

    # less-data-never-accepted: this union is only ever written back to
    # cache as the authoritative list when every source that actually ran
    # reached a confirmed, clean natural end. screens_clean mirrors the
    # existing SCREENS_CLEAN_REASONS gate. catalog_clean is True either
    # when catalog ran with no page-fetch errors, wasn't cut short by the
    # deadline, and didn't saturate its own sort ceiling
    # (catalog_ceiling_saturated -- known arithmetically from upstream's
    # own total_results, not inferred from the union's eventual size), or
    # when author_name is a confirmed absence: _resolve_author_name
    # returned None without raising, meaning Audible itself reported this
    # author has no name, so the catalog wave has nothing to search and
    # that case is trivially complete, not degraded. author_name is also
    # None when name resolution raised instead of confirming an absence --
    # name_resolution_error is checked explicitly here to keep the two
    # apart, since that case means the catalog wave never got a chance to
    # run at all and must not read as trivially complete. db_clean is
    # False when the DB backstop read itself failed (returned None,
    # distinct from a genuinely empty list) -- a failed DB read must not
    # let this union be persisted as authoritative, since it never got the
    # chance to contribute whatever it already had stored. The shrink
    # guard in the writer -- which re-reads the stored list and refuses a
    # shorter one or a strict subset -- only applies to an existing,
    # unexpired cache row, so it cannot stand in for this gate on a first
    # write or an expired one.
    screens_clean = (
        screen_result is not None
        and screen_result.termination_reason in SCREENS_CLEAN_REASONS
    )
    catalog_clean = (
        (author_name is None and name_resolution_error is None)
        or (
            catalog_result is not None
            and catalog_error is None
            and not catalog_result.sort_errors
            and not catalog_result.truncated_by_deadline
            and not catalog_ceiling_saturated
        )
    )
    if screens_clean and catalog_clean and db_clean:
        persist_author_books_cache_background(author_books_key(asin, region), asins)
    elif catalog_ceiling_saturated:
        # A distinct, measured signal -- a sort's own pages plateaued,
        # not an outright per-page error -- so it's called out on its own
        # with the exact numbers, separate from the generic degraded-path
        # warning below, which only fires on an outright error.
        logger.warning("Audible Author Books catalog saturated its sort ceiling, cache write suppressed", extra={
            "author_asin": asin,
            "region": region,
            "catalog_total_results": catalog_result.total_results,
            "catalog_max_fetchable": len(_CATALOG_SORTS) * CATALOG_RESULT_CEILING,
            "author_book_num": len(asins),
        })

    # A path that errored still counts as a degraded response even when the
    # union it contributed to came back non-empty -- folding these into the
    # INFO line below as bare fields would let that degradation stay under
    # the WARNING threshold and never reach a WARNING-level alert or
    # stderr. catalog_degraded stands in for a bare catalog_error is not
    # None check here (see where it's computed above): the raw exception
    # case is rare, the swallowed-into-sort_errors/truncated_by_deadline
    # case is the common one, and only catalog_degraded catches both.
    if (
        screen_error is not None
        or catalog_degraded
        or name_resolution_error is not None
        or db_error is not None
    ):
        logger.warning("Audible Author Books served from a degraded path", extra={
            "author_asin": asin,
            "region": region,
            "screen_error": screen_error,
            "screen_page_error": screen_result.page_error if screen_result else None,
            "catalog_error": catalog_error,
            "catalog_sort_errors": catalog_result.sort_errors if catalog_result else [],
            "name_resolution_error": name_resolution_error,
            "db_error": db_error,
        })

    logger.info("Requested Audible Author Books", extra={
        "author_asin": asin,
        "author_name": author_name,
        "author_book_num": len(asins),
        "screen_asin_num": len(screen_asins),
        "catalog_asin_num": len(catalog_asins),
        "screen_pages_fetched": screen_result.pages_fetched if screen_result else 0,
        "screen_product_count": screen_result.product_count if screen_result else None,
        "screen_invalid_skipped": screen_result.invalid_skipped if screen_result else 0,
        "screen_attribution_rejected": screen_result.attribution_rejected if screen_result else 0,
        "screen_sections_truncated": screen_result.sections_truncated if screen_result else 0,
        "screen_rows_truncated": screen_result.rows_truncated if screen_result else 0,
        "screen_termination_reason": screen_result.termination_reason if screen_result else None,
        "screen_error": screen_error,
        "screen_page_error": screen_result.page_error if screen_result else None,
        "catalog_pages_fetched": catalog_result.pages_fetched if catalog_result else 0,
        "catalog_total_results": catalog_result.total_results if catalog_result else None,
        "catalog_sorts_used": catalog_result.sorts_used if catalog_result else 0,
        "catalog_asin_match": catalog_result.asin_match_count if catalog_result else 0,
        "catalog_asin_reject": catalog_result.asin_reject_count if catalog_result else 0,
        "catalog_name_match": catalog_result.name_match_count if catalog_result else 0,
        "catalog_name_reject": catalog_result.name_reject_count if catalog_result else 0,
        "catalog_truncated_by_deadline": catalog_result.truncated_by_deadline if catalog_result else False,
        "catalog_ceiling_saturated": catalog_ceiling_saturated,
        "catalog_sort_errors": catalog_result.sort_errors if catalog_result else [],
        "catalog_error": catalog_error,
        "name_resolution_error": name_resolution_error,
        "db_error": db_error,
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