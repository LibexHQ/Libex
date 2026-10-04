"""
Looking books up by ASIN: one book, a bulk list, and a book's chapters, each
returned as the published response model.

These are the library's counterparts of the hosted routes -- fetch, normalize,
settle the tri-state flags, split what Audible answered from what it did not --
without the cache. With no store, nothing is persisted and an outage is simply
an outage: an ASIN whose request failed is reported in notFetched, and a
request in which nothing could be served raises AudibleAPIException. Audible
confirming it has no record is a different fact and stays NotFoundException (a
bulk lookup reports it in notFound), never retried and never mixed with an
outage.

With a LocalStore (keyword store), what Audible answered is written under the
hosted merge rules and each book is served as the store then holds it, which
is never less than either side; a chunk or a whole request that fails is
answered from the stored copies, and only what the store lacks too is
notFetched, or raises. A confirmed absence is never answered from the store.
See libex_core.lookup._store.

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
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# Core
from libex_core.asin import is_valid_asin, normalise_asin
from libex_core.audible.books import (
    MAX_ASINS_PER_REQUEST,
    fetch_products,
    filter_products,
    is_placeholder_record,
    explicit_nulls_by_asin,
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
    author_books_concurrency,
    upstream_status_of,
    validate_region,
)
from libex_core.exceptions import ErrorCode, NotFoundException
from libex_core.lookup import _store
from libex_core.lookup._common import OUTAGE_MESSAGE
from libex_core.lookup._shaping import check_shaping, shape_books
from libex_core.models import BookResponse, BulkBookResponse, ChapterResponse

if TYPE_CHECKING:
    from libex_core.storage.store import LocalStore

logger = logging.getLogger("libex")

# The most ASINs one bulk lookup accepts.
MAX_BULK_ASINS = 1000


# ============================================================
# HYDRATION
# ============================================================

@dataclass(frozen=True)
class Hydration:
    """
    What a list of ASINs came to, with every requested ASIN accounted for in
    exactly one place.

    books are settled response dicts in the order Audible returned them, which
    is not necessarily the order requested; when some of them were answered
    from the store, the whole list is in the order requested instead. not_found
    holds the ASINs Audible confirmed it has no record of, plus any identifier
    that is not ASIN-shaped (given as it arrived; it never reached Audible).
    placeholders holds ASINs Audible answered with a placeholder record.
    not_fetched holds ASINs whose request failed, so nothing is known about
    them. deadline_abandoned is the part of not_fetched whose request was cut
    off because the caller's deadline arrived rather than because it failed.
    from_store holds the ASINs
    of the books that were answered from the store because Audible could not
    answer for them; it is a subset of books, and those books are the stored
    copies, not anything Audible said this time. store_write_failed is True
    when a store was given and some fetched book could not be written to it.
    explicit_nulls maps an ASIN to the published fields Audible sent as an
    explicit null in this response, as opposed to omitting them (see
    libex_core.audible.books.explicit_null_fields); () means Audible's answer
    was read and carried none. It reports what Audible said and changes no
    value in books. An ASIN with no entry is unknown: every ASIN in from_store
    has none, because a stored copy is not an answer from Audible this time.
    """

    books: list[dict[str, Any]] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    not_fetched: list[str] = field(default_factory=list)
    deadline_abandoned: list[str] = field(default_factory=list)
    from_store: list[str] = field(default_factory=list)
    store_write_failed: bool = False
    explicit_nulls: dict[str, tuple[str, ...]] = field(default_factory=dict)


class _HydrationDeadlineExceeded(Exception):
    """One hydration request abandoned because the caller's deadline arrived.

    An Exception rather than a CancelledError, which is a BaseException and
    would skip the failed-request accounting: an abandoned request's ASINs
    must land in not_fetched like any other request that gave no answer.
    """


async def hydrate_books(
    get: AudibleGet,
    asins: list[str],
    region: str,
    *,
    deadline: float | None = None,
    high_concurrency: bool = False,
    store: "LocalStore | None" = None,
) -> Hydration:
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

    deadline, when given, is an absolute time.monotonic() bound: requests still
    in flight when it arrives are cancelled, the ones that already answered are
    kept, and the cancelled ones' ASINs are reported in not_fetched and
    deadline_abandoned. high_concurrency draws the requests from the wider pool
    reserved for an author's whole catalogue.

    store, when given, is written through and served from as the module
    docstring describes, on the hosted service's terms. Chunks that failed
    are answered from the stored copies of their ASINs (for the region asked
    only), and the stored copy of an ASIN is never used for one a chunk
    confirmed absent or a placeholder. Each such answer is logged. When no chunk produced a book and the store holds none of
    the ASINs either, the call raises exactly as it does without a store.

    Raises NotFoundException for an empty list, RegionException for a region
    that is not one of the eleven, and AudibleAPIException as above.
    """
    if store is not None:
        await _store.check(store)
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
        # One failed chunk must not discard the chunks that came back, so the
        # requests are waited on rather than gathered: those that answered are
        # kept and, when a deadline arrives, those still in flight are
        # cancelled and counted as unanswered. Results line up with chunks by
        # index. The client's own process-wide bound is the throttle, so
        # nothing here caps the fan-out.
        pool_context = author_books_concurrency() if high_concurrency else nullcontext()
        with pool_context:
            tasks = [
                asyncio.ensure_future(fetch_products(get, chunk, region))
                for chunk in chunks
            ]
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                if tasks:
                    _, pending = await asyncio.wait(tasks, timeout=timeout)
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                        logger.warning("Hydration deadline reached, chunks abandoned", extra={
                            "abandoned_chunks": len(pending),
                            "total_chunks": len(chunks),
                            "region": region,
                        })
            finally:
                # An outer cancellation unwinds out of the wait above and would
                # otherwise leave the fan-out running with nobody holding it.
                for task in tasks:
                    if not task.done():
                        task.cancel()
            results: list[Any] = []
            for task in tasks:
                if task.cancelled():
                    results.append(_HydrationDeadlineExceeded())
                else:
                    results.append(task.exception() or task.result())
        requested_took = round((time.monotonic() - start) * 1000, 2)

        all_products: list[dict[str, Any]] = []
        failed: list[str] = []
        abandoned: list[str] = []
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
                if isinstance(result, _HydrationDeadlineExceeded):
                    abandoned.extend(chunk)
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
        # Read after normalizing, so a product whose container null raises
        # there is an outage and never reaches a report of nulls.
        explicit_nulls = explicit_nulls_by_asin(all_products)

        # The books are written as normalized, tri-state flags and all, and
        # served as the store then holds them.
        write_failed = False
        served = settle_flags_list(normalized)
        if store is not None and normalized:
            written, write_failed = await _store.persist_books(
                store, normalized, region, confirm=True
            )
            served = await _store.serve_merged(store, normalized, written, region)

        # What the failed chunks left uncovered is answered from the store
        # where it holds the ASIN, and only the rest is reported not fetched.
        from_store: list[dict[str, Any]] = []
        if store is not None and failed:
            from_store = await _store.stored_books(store, failed, region)
            covered = {b["asin"] for b in from_store}
            if covered:
                _store.log_served_from_store(
                    "books, failed chunks", region,
                    stored_num=len(from_store), requested_num=len(failed),
                )
            failed = [a for a in failed if a not in covered]
            abandoned = [a for a in abandoned if a not in covered]

        logger.info("Requested books from Audible", extra={
            "requested_num": len(shaped),
            "requested_took": requested_took,
            "not_found_asins": len(not_found),
            "placeholder_asins": len(placeholders),
            "failed_asins": len(failed),
            "from_store_asins": len(from_store),
            "region": region,
        })

        books = served + from_store
        if from_store:
            # The stored copies were appended; put the whole list back in the
            # order requested.
            position = {a: i for i, a in enumerate(shaped)}
            books.sort(key=lambda b: position.get(b.get("asin"), len(position)))

        return Hydration(
            books=books,
            not_found=not_found,
            placeholders=placeholders,
            not_fetched=failed,
            deadline_abandoned=abandoned,
            from_store=[b["asin"] for b in from_store],
            store_write_failed=write_failed,
            explicit_nulls=explicit_nulls,
        )

    except NotFoundException:
        raise
    except Exception as e:
        logger.warning("Audible unavailable for book lookup", extra={"region": region})
        if store is not None:
            fallback = await _hydrate_from_store(
                store, region, shaped, not_found, placeholders
            )
            if fallback is not None:
                return fallback
        raise as_audible_failure(e, OUTAGE_MESSAGE) from e


async def _hydrate_from_store(
    store: "LocalStore",
    region: str,
    shaped: list[str],
    not_found: list[str],
    placeholders: list[str],
) -> Hydration | None:
    """
    The whole-request outage answer: every requested ASIN the store holds,
    minus any a chunk that did answer confirmed absent or a placeholder, which
    stored data does not overrule. None when the store holds none of them, so
    the caller raises the outage rather than returning an empty answer that
    would read as a confirmed absence.
    """
    confirmed = set(not_found) | set(placeholders)
    owed = [a for a in shaped if a not in confirmed]
    rows = await _store.stored_books(store, owed, region)
    if not rows:
        return None
    held = {b["asin"] for b in rows}
    _store.log_served_from_store(
        "books", region, stored_num=len(rows), requested_num=len(owed)
    )
    return Hydration(
        books=rows,
        not_found=not_found,
        placeholders=placeholders,
        not_fetched=[a for a in owed if a not in held],
        from_store=[b["asin"] for b in rows],
    )


# ============================================================
# PUBLIC API
# ============================================================

def _canonical_asin(asin: str) -> str:
    """The uppercase form of a valid ASIN; the rejected value is not echoed."""
    if not isinstance(asin, str) or not is_valid_asin(asin):
        raise NotFoundException("Invalid ASIN format", code=ErrorCode.INVALID_REQUEST)
    return normalise_asin(asin)


@dataclass(frozen=True)
class BookLookup:
    """
    One book with what Audible said about its nulls.

    book is exactly what get_book returns. explicit_nulls names the published
    fields Audible sent as an explicit null rather than omitting, or is None
    when unknown because the book was answered from the store and not by
    Audible this time. () means Audible answered and sent none.
    """

    book: BookResponse
    explicit_nulls: tuple[str, ...] | None = None


async def get_book_with_nulls(
    get: AudibleGet,
    asin: str,
    *,
    region: str = "us",
    store: "LocalStore | None" = None,
) -> BookLookup:
    """
    get_book, returning the book together with its explicit nulls.

    Same arguments, same errors and same book as get_book; see it. The only
    addition is BookLookup.explicit_nulls, which reports and changes nothing.
    """
    canonical = _canonical_asin(asin)
    hydration = await hydrate_books(get, [canonical], region, store=store)
    if not hydration.books:
        if canonical in hydration.placeholders:
            raise NotFoundException(
                "Audible returned only a placeholder record for this ASIN",
                code=ErrorCode.WITHHELD,
            )
        raise NotFoundException("Book not found")
    book = hydration.books[0]
    return BookLookup(
        book=BookResponse(**book),
        explicit_nulls=hydration.explicit_nulls.get(book["asin"]),
    )


async def get_book(
    get: AudibleGet,
    asin: str,
    *,
    region: str = "us",
    store: "LocalStore | None" = None,
) -> BookResponse:
    """
    Fetches one book by ASIN.

    Book ASINs are region-specific: the book resolves only in its own
    marketplace. Raises NotFoundException (code invalid_request) for a value
    that is not an ASIN; NotFoundException (code not_on_audible) when Audible
    has no record; NotFoundException (code withheld) when Audible sent only a
    placeholder record, which is never returned; AudibleAPIException when
    Audible could not be reached and the store (if given) does not hold the
    book; RegionException for an unknown region. With a store the book is
    written through and served as the store holds it, and an outage is
    answered from the stored copy.

    get_book_with_nulls is the same lookup that also reports which fields
    Audible sent as an explicit null.
    """
    return (await get_book_with_nulls(get, asin, region=region, store=store)).book


@dataclass(frozen=True)
class BooksLookup:
    """
    A bulk lookup with what Audible said about each book's nulls.

    response is exactly what get_books returns. explicit_nulls maps the ASIN
    of each book in it to the published fields Audible sent as an explicit
    null; a book with no entry is unknown (answered from the store), and ()
    means Audible answered and sent none.
    """

    response: BulkBookResponse
    explicit_nulls: dict[str, tuple[str, ...]] = field(default_factory=dict)


async def get_books_with_nulls(
    get: AudibleGet,
    asins: list[str],
    *,
    region: str = "us",
    filters: dict[str, Any] | None = None,
    sort: str | None = None,
    order: str = "asc",
    store: "LocalStore | None" = None,
) -> BooksLookup:
    """
    get_books, returning the response together with each book's explicit nulls.

    Same arguments, errors and response as get_books, all documented below. BooksLookup.explicit_nulls maps the ASIN of each book in the
    response to the published fields Audible sent as an explicit null rather
    than omitting; a book answered from the store has no entry, which means
    unknown and is not the same as ().

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

    filters, sort and order shape books only, as on the hosted route: they are
    applied after notFound, placeholderRecords and notFetched are worked out, so
    a book that was found but filtered out is not reported missing. See
    libex_core.shaping for the filter names and sortable fields; a name or
    value outside them is ValueError, before anything is sent.

    With a store, books are written through and served as the store holds them,
    and an ASIN whose request failed is answered from its stored copy; only
    what the store lacks too is notFetched.

    Raises AudibleAPIException when a request failed and no book came back at
    all (the store included), and RegionException for an unknown region.
    """
    check_shaping(filters, sort, order)
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

    hydration = await hydrate_books(
        get, [normalise_asin(a) for a in asin_list], region, store=store
    )

    found = {normalise_asin(book["asin"]) for book in hydration.books}
    placeholder_set = {normalise_asin(a) for a in hydration.placeholders} - found
    not_fetched_set = (
        {normalise_asin(a) for a in hydration.not_fetched} - found - placeholder_set
    )

    shaped = shape_books(hydration.books, filters, sort, order)

    shaped_asins = {book["asin"] for book in shaped}
    response = BulkBookResponse(
        books=[BookResponse(**book) for book in shaped],
        notFound=[
            a for a in asin_list
            if normalise_asin(a) not in found
            and normalise_asin(a) not in placeholder_set
            and normalise_asin(a) not in not_fetched_set
        ],
        placeholderRecords=[a for a in asin_list if normalise_asin(a) in placeholder_set],
        notFetched=[a for a in asin_list if normalise_asin(a) in not_fetched_set],
    )
    return BooksLookup(
        response=response,
        explicit_nulls={
            asin: nulls
            for asin, nulls in hydration.explicit_nulls.items()
            if asin in shaped_asins
        },
    )


async def get_books(
    get: AudibleGet,
    asins: list[str],
    *,
    region: str = "us",
    filters: dict[str, Any] | None = None,
    sort: str | None = None,
    order: str = "asc",
    store: "LocalStore | None" = None,
) -> BulkBookResponse:
    """
    Fetches up to 1000 books by ASIN. See get_books_with_nulls, which this
    returns the response of, for the arguments, the ordering, filtering and
    store behaviour, and the errors.
    """
    return (await get_books_with_nulls(
        get, asins, region=region, filters=filters, sort=sort, order=order, store=store
    )).response


async def get_chapters(
    get: AudibleGet,
    asin: str,
    *,
    region: str = "us",
    store: "LocalStore | None" = None,
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

    With a store the chapters are written through, keeping the richer listing
    of the stored and the offered, and served as the store holds them; when
    Audible could not be reached the stored listing answers, and without one
    that is AudibleAPIException. A NotFoundException is never answered from
    the store.
    """
    canonical = _canonical_asin(asin)
    region = validate_region(region)
    if store is not None:
        await _store.check(store)
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
        # Audible answered, with nothing to list; that is recorded, and is
        # never served as a stored answer.
        if store is not None:
            await _store.persist_chapters_confirmed_absent(store, canonical, region)
        raise
    except Exception as e:
        logger.warning("Audible unavailable for chapters", extra={
            "asin": canonical,
            "region": region,
            "error_type": type(e).__name__,
            "upstream_status": upstream_status_of(e),
        })
        if store is not None:
            stored = await _store.stored_track(store, canonical, region)
            if stored:
                _store.log_served_from_store("chapters", region, asin=canonical)
                return ChapterResponse(**stored)
        raise as_audible_failure(e, OUTAGE_MESSAGE) from e
    if store is not None and await _store.persist_track(
        store, canonical, result, region, confirm=True
    ):
        result = await _store.stored_track(store, canonical, region) or result
    return ChapterResponse(**result)
