"""
Audible author-books screens walk.
Fetches an author's book ASINs from Audible's Android author-detail screen
(the /1.0/screens/audible-android-author-detail endpoint), the only
ASIN-exact source among get_author_books' four.
"""

# Standard library
import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass

# Core
from app.core.logging import get_logger
from app.core.middleware import is_valid_asin

# Services
from app.services.audible.client import (
    audible_get,
    ANDROID_DEVICE_TYPE_ID,
    AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT,
)

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

# Bounded concurrency for the screens walk's page 2..N fan-out (see
# _fanout_screen_pages). Too low reintroduces the exact problem the fan-out
# exists to fix: the author's underlying list drifts (a live probe of the
# identical page 25-40 seconds apart returned different rows), and a walk
# that stretches back into that drift window starts silently duplicating
# and dropping titles at page boundaries again -- so wider is strictly
# safer here, not just faster, up to whatever the shared IP can actually
# take at once.
#
# Tied directly to AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT rather than kept
# as its own independent number: this walk only ever runs as part of the
# live author-books request that pool is reserved for (see that constant's
# own docstring in client.py for the measurements behind its value and why
# that call path specifically gets a wider pool than the shared IP's
# default), and every audible_get call this walk makes already draws from
# that same pool via author_books_concurrency() (entered by the caller in
# authors/__init__.py, not here). Capping this walk's own batch width below
# the pool it draws from would just leave permits unused on that pool for
# no benefit, so there is no separate, smaller number to justify -- this
# was previously kept deliberately tighter (8) specifically because it
# shared its risk exposure, through the single default pool, with the
# seeder's own sustained background work; splitting that pool off is what
# removes the reason to hold this one back independently.
SCREENS_FANOUT_CONCURRENCY = AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT

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

# get_author_books' own completeness gate (screens_clean in
# authors/__init__.py) asks a different question than SCREENS_CLEAN_REASONS
# above: not "did upstream explicitly confirm nothing remains" (COMPLETED
# alone) but "did this wave fail its own job, or did it hand off to the
# other three sources the way it was designed to". SCREENS_REASON_
# PLATEAU_TRUNCATED is deliberately excluded from this set even though it
# is not a SCREENS_CLEAN_REASON: Christie's grid plateaus at page 26 of a
# 56-page product_count-implied walk and contributes roughly half of her
# total ASIN union, and Doyle's plateaus the same way against his own
# product_count -- for both, upstream is silently repeating the last real
# page's content byte-identical rather than confirming an end, which is
# functionally the same "nothing further remains" signal COMPLETED gives,
# just detected by this walk noticing its own repetition instead of
# upstream sending a null token (see _fanout_screen_pages' own docstring).
# Gating completeness on COMPLETED alone made is_complete permanently
# unreachable for exactly the prolific authors this feature exists to
# serve, forcing them onto AUTHOR_BOOKS_DEGRADED_CACHE_TTL_SECONDS (900s)
# forever instead of the default day-long TTL -- up to 96 full walks a day,
# each hundreds of upstream requests through Libex's single exit IP,
# strictly worse than not caching them at all.
#
# SCREENS_REASON_TOKEN_REPEATED stays in this broken set rather than
# joining PLATEAU_TRUNCATED, even though the sequential fallback walk's own
# repeat check (see the token-chasing loop below) is mechanistically
# similar -- noticing a repeat on its own rather than being told by
# upstream. There is no live measurement on record of it firing against a
# genuine plateau the way PLATEAU_TRUNCATED's live Christie/Doyle traces
# exist for; extending the same reclassification to it without that
# evidence would be guessing from architectural resemblance, not deciding
# from a measurement the way PLATEAU_TRUNCATED's reclassification was.
SCREENS_BROKEN_REASONS = frozenset({
    SCREENS_REASON_TOKEN_REPEATED,
    SCREENS_REASON_TOKEN_REJECTED,
    SCREENS_REASON_PAGE_CAP,
    SCREENS_REASON_ASIN_CAP,
    SCREENS_REASON_PAGE_ERROR,
    SCREENS_REASON_NON_DICT_PAGE,
    SCREENS_REASON_TIME_BUDGET,
    SCREENS_REASON_GRID_NOT_FOUND,
    SCREENS_REASON_TRUNCATED,
})


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
    climbing (pages 56, 57, and 999 of a 56-page product_count-implied walk
    all came back identical, even though that author's real content
    plateaus at page 26 -- see SCREENS_REASON_PLATEAU_TRUNCATED). So every
    page's ASIN signature is checked against every
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