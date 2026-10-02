"""
Looking books up by ASIN: one book, a bulk list, and a book's chapters, each
returned as the published response model.

These are the library's counterparts of the hosted routes' live path -- fetch,
normalize, settle the tri-state flags, split what Audible answered from what it
did not -- without the cache, the database backstop or any persistence. What
those leave out is what the hosted service answers an outage from, so here an
outage is simply an outage: an ASIN whose request failed is reported in
notFetched, and a request in which nothing could be served raises
AudibleAPIException. Audible confirming it has no record is a different fact
and stays NotFoundException (a bulk lookup reports it in notFound), never
retried and never mixed with an outage.

Every function takes the callable that makes the request (an AudibleGet) as its
first argument and the region as a keyword. Nothing here reads the
environment. Nothing logged or raised repeats raw caller input; where a value
does appear (a log field, the no-chapter-information message) it is a
validated ASIN in its uppercase form.
"""

# Standard library
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

# Core
from libex_core.asin import is_valid_asin, normalise_asin
from libex_core.audible.books import (
    MAX_ASINS_PER_REQUEST,
    fetch_products,
    filter_products,
    is_placeholder_record,
    normalize_products,
    settle_flags_list,
)
from libex_core.audible.chapters import (
    fetch_chapter_metadata,
    has_chapter_info,
    normalize_chapters,
)
from libex_core.audible.client import (
    AudibleGet,
    as_audible_failure,
    upstream_status_of,
    validate_region,
)
from libex_core.exceptions import ErrorCode, NotFoundException
from libex_core.models import BookResponse, BulkBookResponse, ChapterResponse

logger = logging.getLogger("libex")

# The most ASINs one bulk lookup accepts.
MAX_BULK_ASINS = 1000

_OUTAGE_MESSAGE = "Audible unavailable"


# ============================================================
# HYDRATION
# ============================================================

@dataclass(frozen=True)
class Hydration:
    """
    What a list of ASINs came to, with every requested ASIN accounted for in
    exactly one place.

    books are settled response dicts in the order Audible returned them, which
    is not necessarily the order requested. not_found holds the ASINs Audible
    confirmed it has no record of, plus any identifier that is not ASIN-shaped
    (given as it arrived; it never reached Audible). placeholders holds ASINs
    Audible answered with a placeholder record. not_fetched holds ASINs whose
    request failed, so nothing is known about them.
    """

    books: list[dict[str, Any]] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    not_fetched: list[str] = field(default_factory=list)


async def hydrate_books(get: AudibleGet, asins: list[str], region: str) -> Hydration:
    """
    Turns a list of ASINs into books, 50 to a request, all requests at once.

    The shared core of get_book, get_books, get_series_books and quick_search.
    Duplicates are collapsed, and an ASIN is sent in its uppercase form because
    Audible's catalogue is case-sensitive. An identifier that is not
    ASIN-shaped is a not-found for that identifier alone and costs nothing
    else: sent in a chunk, the core fetch would reject all 50 and the chunk
    would read as an outage.

    A chunk that fails with NotFoundException puts its ASINs in not_found. A
    chunk that fails any other way puts its ASINs in not_fetched, and the
    chunks that answered are still returned. When any chunk failed and no chunk
    produced a servable book, nothing can be told apart from an outage, so the
    call raises AudibleAPIException carrying the first failure's upstream
    status, even if another chunk confirmed some ASINs absent.

    Raises NotFoundException for an empty list, RegionException for a region
    that is not one of the eleven, and AudibleAPIException as above.
    """
    region = validate_region(region)
    if not asins:
        raise NotFoundException("No ASINs provided")

    seen: set[str] = set()
    unique = [a for a in asins if not (a in seen or seen.add(a))]  # type: ignore

    shaped: list[str] = []
    shaped_seen: set[str] = set()
    unshaped: list[str] = []
    for a in unique:
        if isinstance(a, str) and is_valid_asin(a):
            upper = normalise_asin(a)
            if upper not in shaped_seen:
                shaped_seen.add(upper)
                shaped.append(upper)
        else:
            unshaped.append(a)

    not_found: list[str] = list(unshaped)
    placeholders: list[str] = []
    if unshaped:
        logger.warning("Skipped non-ASIN identifiers before fetch", extra={
            "skipped_num": len(unshaped),
            "region": region,
        })

    chunks = [
        shaped[i:i + MAX_ASINS_PER_REQUEST]
        for i in range(0, len(shaped), MAX_ASINS_PER_REQUEST)
    ]

    try:
        start = time.monotonic()
        # return_exceptions so one failed chunk cannot discard the chunks that
        # came back; results line up with chunks by index. The client's own
        # process-wide bound is the throttle, so nothing here caps the fan-out.
        results = await asyncio.gather(
            *(fetch_products(get, chunk, region) for chunk in chunks),
            return_exceptions=True,
        )
        requested_took = round((time.monotonic() - start) * 1000, 2)

        all_products: list[dict[str, Any]] = []
        failed: list[str] = []
        errors: list[Exception] = []

        for idx, (chunk, result) in enumerate(zip(chunks, results)):
            if isinstance(result, NotFoundException):
                # Only a single-ASIN request can 404; a batch answers 200 with
                # hollow stubs for what it does not know. Terminal either way.
                not_found.extend(chunk)
                continue
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result
            if isinstance(result, Exception):
                failed.extend(chunk)
                errors.append(result)
                logger.warning("Hydration chunk failed", extra={
                    "chunk_index": idx + 1,
                    "chunk_count": len(chunks),
                    "chunk_size": len(chunk),
                    "region": region,
                    "error_type": type(result).__name__,
                })
                continue

            # A hollow titleless stub is a not-found; a titled record carrying
            # the placeholder date is a different fact, so it is split out.
            # Only ASINs that were requested count; Audible can return others.
            kept = filter_products(result)
            kept_asins = {p.get("asin") for p in kept}
            chunk_set = set(chunk)
            chunk_placeholders = list(dict.fromkeys(
                p["asin"] for p in result
                if p.get("asin") in chunk_set and is_placeholder_record(p)
            ))
            placeholder_set = set(chunk_placeholders)
            not_found.extend(
                a for a in chunk if a not in kept_asins and a not in placeholder_set
            )
            placeholders.extend(chunk_placeholders)
            all_products.extend(kept)

        if not_found or placeholders or failed:
            logger.warning("Partial hydration shortfall", extra={
                "requested_num": len(shaped),
                "not_found_asins": len(not_found),
                "placeholder_asins": len(placeholders),
                "failed_asins": len(failed),
                "region": region,
            })

        if failed and not all_products:
            raise errors[0]

        normalized = await normalize_products(all_products, region)

        logger.info("Requested books from Audible", extra={
            "requested_num": len(shaped),
            "requested_took": requested_took,
            "not_found_asins": len(not_found),
            "placeholder_asins": len(placeholders),
            "failed_asins": len(failed),
            "region": region,
        })

        return Hydration(
            books=settle_flags_list(normalized),
            not_found=not_found,
            placeholders=placeholders,
            not_fetched=failed,
        )

    except NotFoundException:
        raise
    except Exception as e:
        logger.warning("Audible unavailable for book lookup", extra={"region": region})
        raise as_audible_failure(e, _OUTAGE_MESSAGE) from e


# ============================================================
# PUBLIC API
# ============================================================

def _canonical_asin(asin: str) -> str:
    """The uppercase form of a valid ASIN; the rejected value is not echoed."""
    if not isinstance(asin, str) or not is_valid_asin(asin):
        raise NotFoundException("Invalid ASIN format", code=ErrorCode.INVALID_REQUEST)
    return normalise_asin(asin)


async def get_book(get: AudibleGet, asin: str, *, region: str = "us") -> BookResponse:
    """
    Fetches one book by ASIN.

    Book ASINs are region-specific: the book resolves only in its own
    marketplace. Raises NotFoundException (code invalid_request) for a value
    that is not an ASIN; NotFoundException (code not_on_audible) when Audible
    has no record; NotFoundException (code withheld) when Audible sent only a
    placeholder record, which is never returned; AudibleAPIException when
    Audible could not be reached; RegionException for an unknown region.
    """
    canonical = _canonical_asin(asin)
    hydration = await hydrate_books(get, [canonical], region)
    if not hydration.books:
        if canonical in hydration.placeholders:
            raise NotFoundException(
                "Audible returned only a placeholder record for this ASIN",
                code=ErrorCode.WITHHELD,
            )
        raise NotFoundException("Book not found")
    return BookResponse(**hydration.books[0])


async def get_books(
    get: AudibleGet, asins: list[str], *, region: str = "us"
) -> BulkBookResponse:
    """
    Fetches up to 1000 books by ASIN.

    Each entry may itself be comma-separated, as the hosted route's query
    parameter is, and is stripped. Every identifier is checked first: any that
    is not ASIN-shaped rejects the whole request, as does an empty list or
    more than 1000, with NotFoundException (code invalid_request).

    The lookup runs on the uppercase form. notFound, placeholderRecords and
    notFetched hold the ASINs exactly as the caller sent them, in request order
    (a repeated ASIN repeats), and no ASIN is in more than one of books,
    notFound, placeholderRecords and notFetched: found wins, then placeholder,
    then notFetched. notFound is a confirmed absence on Audible; notFetched is
    an outage kept Libex from finding out, and a retry may resolve it.

    Raises AudibleAPIException when a request failed and no book came back at
    all, and RegionException for an unknown region.
    """
    asin_list = [
        a.strip()
        for entry in asins
        for a in entry.split(",")
        if a.strip()
    ]

    invalid = [a for a in asin_list if not is_valid_asin(a)]
    if invalid:
        raise NotFoundException(
            f"Invalid ASIN format ({len(invalid)} of {len(asin_list)} identifiers)",
            code=ErrorCode.INVALID_REQUEST,
        )
    if not asin_list:
        raise NotFoundException("No valid ASINs provided", code=ErrorCode.INVALID_REQUEST)
    if len(asin_list) > MAX_BULK_ASINS:
        raise NotFoundException(
            f"Maximum {MAX_BULK_ASINS} ASINs per request", code=ErrorCode.INVALID_REQUEST
        )

    hydration = await hydrate_books(get, [normalise_asin(a) for a in asin_list], region)

    found = {normalise_asin(book["asin"]) for book in hydration.books}
    placeholder_set = {normalise_asin(a) for a in hydration.placeholders} - found
    not_fetched_set = (
        {normalise_asin(a) for a in hydration.not_fetched} - found - placeholder_set
    )

    return BulkBookResponse(
        books=[BookResponse(**book) for book in hydration.books],
        notFound=[
            a for a in asin_list
            if normalise_asin(a) not in found
            and normalise_asin(a) not in placeholder_set
            and normalise_asin(a) not in not_fetched_set
        ],
        placeholderRecords=[a for a in asin_list if normalise_asin(a) in placeholder_set],
        notFetched=[a for a in asin_list if normalise_asin(a) in not_fetched_set],
    )


async def get_chapters(
    get: AudibleGet, asin: str, *, region: str = "us"
) -> ChapterResponse:
    """
    Fetches a book's chapter listing.

    A 404 from Audible, and a 200 that carries no chapter listing, are both
    NotFoundException: the record is not there and will not be on a retry (a
    population of books has no chapters, and Audible answers those with a 404).
    Only a transient failure is AudibleAPIException, and only that is worth
    retrying.
    A value that is not an ASIN is NotFoundException (code invalid_request);
    an unknown region is RegionException.
    """
    canonical = _canonical_asin(asin)
    region = validate_region(region)
    try:
        start = time.monotonic()
        data = await fetch_chapter_metadata(get, canonical, region)
        chapters_took = round((time.monotonic() - start) * 1000, 2)

        if not has_chapter_info(data):
            raise NotFoundException(f"No chapter information found for {canonical}")

        result = normalize_chapters(data)
        logger.info("Requested chapters from Audible", extra={
            "chapters_took": chapters_took,
            "region": region,
        })
    except NotFoundException:
        raise
    except Exception as e:
        logger.warning("Audible unavailable for chapters", extra={
            "asin": canonical,
            "region": region,
            "error_type": type(e).__name__,
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, _OUTAGE_MESSAGE) from e
    return ChapterResponse(**result)
