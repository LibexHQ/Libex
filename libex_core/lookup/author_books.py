"""
Looking up the books an author is credited with, by author ASIN or by name.

The library's counterparts of the hosted author-books routes, without the
cache and background completion. With no store nothing is persisted. Hosted
tells a caller whether the list it got is whole in a
response header, X-Libex-Complete, with the reasons in X-Libex-Incomplete-Reason
on the by-name route; a library caller has no headers, so each lookup returns
a BookList carrying the same facts: complete, and incomplete_reasons drawn
from the same vocabulary. A list that is not complete is still returned, since
what was gathered is worth having; a caller that needs the whole catalogue
retries or reads complete first.

By ASIN, two Audible sources are unioned because neither is complete alone:
the Android author-detail screen (the only ASIN-exact source) and the
windowed, category-sliced catalog search attributed by author ASIN. The
hosted route also unions in the books it has stored; the library does so
when given a store. By
name, a single-sort catalog search is matched on the exact name.
Discovery and the hydration that follows share one 25 second budget on the
ASIN route, the figure the hosted route keeps under its proxy's timeout; a
prolific author's walk that hits it comes back incomplete, and nothing here
finishes it afterwards.

With a LocalStore (keyword store) the books are written through and served as
the store holds them (see libex_core.lookup.books), and on the ASIN route the
author's profile is stored when it is fetched, the name is taken from the
stored profile when there is one, and the ASINs of the books the store already
holds for the author are unioned into discovery on every request, as the
hosted route does, so what was once found is never lost to a thin walk. When
every live source comes up empty and the store holds none either, the outcome
is the one it is without a store. BookList.store_write_failed says that a
book could not be written, in which case it was still returned.

What a caller typed as a name is never logged or repeated in a message.
Nothing here reads the environment.
"""

# Standard library
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# Core
from libex_core.audible.authors.by_name import NameWalkOutcome, walk_author_books_by_name
from libex_core.audible.authors.catalog import CatalogBooksResult, fetch_author_books_by_catalog
from libex_core.audible.authors.profile import fetch_author_profile, normalize_author
from libex_core.audible.authors.screens import (
    SCREENS_BROKEN_REASONS,
    ScreenBooksResult,
    fetch_author_books_by_screen,
)
from libex_core.audible.client import (
    AudibleGet,
    as_audible_failure,
    author_books_concurrency,
    upstream_status_of,
    validate_region,
)
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import _store
from libex_core.lookup._common import OUTAGE_MESSAGE
from libex_core.lookup._shaping import check_shaping, shape_books
from libex_core.lookup.books import Hydration, _canonical_asin, hydrate_books
from libex_core.models import BookResponse

if TYPE_CHECKING:
    from libex_core.storage.store import LocalStore

logger = logging.getLogger("libex")

# Wall-clock budget across the whole ASIN route: discovery and hydration share
# it. It bounds one lookup's work and rejects nothing; it matches the hosted
# route's figure, which keeps a full request inside its proxy's window.
AUTHOR_BOOKS_TIME_BUDGET_SECONDS = 25.0

# Why a list is not complete, the same words the hosted routes put in
# X-Libex-Incomplete-Reason. In this order whenever more than one applies.
REASON_DISCOVERY_INCOMPLETE = "discovery-incomplete"
REASON_HYDRATION_DEADLINE = "hydration-deadline"
REASON_HYDRATION_FAILED = "hydration-failed"
REASON_HYDRATION_NOT_FOUND = "hydration-not-found"
INCOMPLETE_REASONS = (
    REASON_DISCOVERY_INCOMPLETE,
    REASON_HYDRATION_DEADLINE,
    REASON_HYDRATION_FAILED,
    REASON_HYDRATION_NOT_FOUND,
)

_NOT_FOUND_MESSAGE = "No books found for author"


@dataclass(frozen=True)
class BookList:
    """
    An author's books and whether the list is whole.

    books are the full books, filtered and sorted as asked. complete is False
    when discovery stopped before confirming it had every ASIN, or when fewer
    books came back than ASINs were found; both are judged before filtering,
    since a filter legitimately shortens the list and says nothing about the
    fetch. incomplete_reasons names why, in INCOMPLETE_REASONS order, and is
    empty exactly when complete is True. store_write_failed is True when a
    store was given and some fetched book could not be written to it.
    from_store holds the ASINs of the books answered from the store because
    Audible could not answer for them (before filtering, so a filtered-out
    ASIN can appear here); those books are stored copies, not what Audible
    said this time. explicit_nulls maps the ASIN of each book in books to the
    published fields Audible sent as an explicit null rather than omitting
    (see libex_core.audible.books.explicit_null_fields); it reports and changes
    no value. A book with no entry is unknown, as every stored copy is, and
    that is not the same as () -- Audible answered and sent none.
    """

    books: list[BookResponse] = field(default_factory=list)
    complete: bool = True
    incomplete_reasons: tuple[str, ...] = ()
    store_write_failed: bool = False
    from_store: tuple[str, ...] = ()
    explicit_nulls: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _reasons(discovery_complete: bool, hydration: Hydration) -> tuple[str, ...]:
    """The incomplete reasons for a discovery outcome and the hydration that
    followed. Not-found and placeholder ASINs are both a hydration-not-found,
    as on the hosted routes."""
    found: set[str] = set()
    if not discovery_complete:
        found.add(REASON_DISCOVERY_INCOMPLETE)
    if hydration.deadline_abandoned:
        found.add(REASON_HYDRATION_DEADLINE)
    abandoned = set(hydration.deadline_abandoned)
    if any(a not in abandoned for a in hydration.not_fetched):
        found.add(REASON_HYDRATION_FAILED)
    if hydration.not_found or hydration.placeholders:
        found.add(REASON_HYDRATION_NOT_FOUND)
    return tuple(r for r in INCOMPLETE_REASONS if r in found)


def _assemble(
    discovery_complete: bool,
    hydration: Hydration,
    filters: dict[str, Any] | None,
    sort: str | None,
    order: str,
) -> BookList:
    reasons = _reasons(discovery_complete, hydration)
    books = shape_books(hydration.books, filters, sort, order)
    kept = {book["asin"] for book in books}
    return BookList(
        books=[BookResponse(**book) for book in books],
        complete=not reasons,
        incomplete_reasons=reasons,
        store_write_failed=hydration.store_write_failed,
        from_store=tuple(hydration.from_store),
        explicit_nulls={
            asin: nulls
            for asin, nulls in hydration.explicit_nulls.items()
            if asin in kept
        },
    )


# ============================================================
# BY ASIN
# ============================================================

async def _resolve_author_name(
    get: AudibleGet, asin: str, region: str, store: "LocalStore | None" = None
) -> str | None:
    """
    Resolves an author ASIN to a name, from the store when it holds the author
    and otherwise through Audible's contributors endpoint, storing the profile
    that endpoint returns.

    None only when Audible confirms the author carries no name (a 404, or a
    200 with an empty name). Any other failure propagates, so "no name on
    record" stays apart from "name resolution failed".
    """
    if store is not None:
        stored = await _store.stored_author(store, asin, region)
        if stored and stored.get("name"):
            return stored["name"]
    try:
        data = await fetch_author_profile(get, asin, region)
    except NotFoundException:
        return None
    name = (data.get("contributor", {}).get("name") or "").replace("\t", "").strip()
    if name and store is not None:
        await _store.persist_author(
            store, normalize_author(data, asin, region), region, confirm=True
        )
    return name or None


async def _walk_author_books(
    get: AudibleGet,
    asin: str,
    region: str,
    deadline: float,
    store: "LocalStore | None" = None,
) -> tuple[list[str], bool]:
    """
    Discovers an author's book ASINs as a union of the catalog walk (already
    -ReleaseDate first, so that stays at the front) and then any screens-only
    ASIN, deduped on ASIN alone, never title. Returns (asins, complete). With
    a store, the author's stored book ASINs are unioned in after those, and a
    failed read of them is a failed source, not an author with no stored books.

    A source that fails does not fail the lookup; what the other surfaced is
    still served and the list is incomplete. When the union is empty,
    NotFoundException means every source answered clean and confirmed nothing,
    and AudibleAPIException means at least one failed instead, so nothing was
    established.
    """
    start = time.monotonic()

    author_name: str | None = None
    name_resolution_error: str | None = None
    try:
        author_name = await _resolve_author_name(get, asin, region, store)
    except Exception as e:
        name_resolution_error = type(e).__name__

    # The wider pool is reserved for exactly this fan-out; every nested gather
    # the two walks run inherits it.
    with author_books_concurrency():
        tasks = [fetch_author_books_by_screen(get, asin, region, deadline=deadline)]
        if author_name:
            tasks.append(
                fetch_author_books_by_catalog(get, asin, author_name, region, deadline=deadline)
            )
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    screen_result: ScreenBooksResult | None = None
    screen_error: str | None = None
    if isinstance(outcomes[0], BaseException):
        screen_error = type(outcomes[0]).__name__
    else:
        screen_result = outcomes[0]
    screen_asins = screen_result.asins if screen_result is not None else []

    catalog_result: CatalogBooksResult | None = None
    catalog_error: str | None = None
    if author_name:
        if isinstance(outcomes[1], BaseException):
            catalog_error = type(outcomes[1]).__name__
        else:
            catalog_result = outcomes[1]
    catalog_asins = catalog_result.asins if catalog_result is not None else []

    # Most catalog failures never raise: they are swallowed into sort_errors,
    # truncated_by_deadline and slicing_incomplete, so those are what degraded
    # means. Slicing by category on its own is the walk working as designed
    # for a prolific author, not a failure.
    catalog_degraded = (
        author_name is not None
        and (
            catalog_error is not None
            or catalog_result is None
            or bool(catalog_result.sort_errors)
            or catalog_result.truncated_by_deadline
            or catalog_result.slicing_incomplete
        )
    )

    seen = set(catalog_asins)
    asins = list(catalog_asins)
    for screen_asin in screen_asins:
        if screen_asin not in seen:
            seen.add(screen_asin)
            asins.append(screen_asin)

    db_error: str | None = None
    if store is not None:
        stored_asins = await _store.stored_author_book_asins(store, asin, region)
        if stored_asins is None:
            db_error = "StoreReadFailed"
        for stored_asin in stored_asins or []:
            stored_asin = stored_asin.upper()
            if stored_asin not in seen:
                seen.add(stored_asin)
                asins.append(stored_asin)

    if not asins:
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
            # At least one source failed instead of confirming an empty
            # catalogue, so this is silence, not Audible saying there are no
            # books.
            raise AudibleAPIException(OUTAGE_MESSAGE)
        raise NotFoundException(_NOT_FOUND_MESSAGE)

    # Did each source do its own job. The screens grid plateauing is the
    # designed handoff to the catalog, so only its broken reasons count; the
    # catalog is trivially clean when Audible confirmed the author has no name
    # to search with, but not when resolving the name failed.
    screens_clean = (
        screen_result is not None
        and screen_result.termination_reason not in SCREENS_BROKEN_REASONS
    )
    catalog_clean = (
        (author_name is None and name_resolution_error is None)
        or (
            catalog_result is not None
            and catalog_error is None
            and not catalog_result.sort_errors
            and not catalog_result.truncated_by_deadline
            and not catalog_result.slicing_incomplete
        )
    )
    is_complete = screens_clean and catalog_clean

    if screen_error is not None or catalog_degraded or name_resolution_error is not None:
        logger.warning("Audible Author Books served from a degraded path", extra={
            "author_asin": asin,
            "region": region,
            "screen_error": screen_error,
            "screen_page_error": screen_result.page_error if screen_result else None,
            "catalog_error": catalog_error,
            "catalog_sort_errors": catalog_result.sort_errors if catalog_result else [],
            "name_resolution_error": name_resolution_error,
        })

    logger.info("Requested Audible Author Books", extra={
        "author_asin": asin,
        "author_book_num": len(asins),
        "screen_asin_num": len(screen_asins),
        "catalog_asin_num": len(catalog_asins),
        "screen_termination_reason": screen_result.termination_reason if screen_result else None,
        "catalog_total_results": catalog_result.total_results if catalog_result else None,
        "catalog_sliced": catalog_result.sliced if catalog_result else False,
        "catalog_truncated_by_deadline": (
            catalog_result.truncated_by_deadline if catalog_result else False
        ),
        "complete": is_complete,
        "author_book_took": round((time.monotonic() - start) * 1000, 2),
        "region": region,
    })
    return asins, is_complete


async def get_author_books(
    get: AudibleGet,
    asin: str,
    *,
    region: str = "us",
    filters: dict[str, Any] | None = None,
    sort: str | None = None,
    order: str = "asc",
    store: "LocalStore | None" = None,
) -> BookList:
    """
    Fetches the full books an author is credited with, by author ASIN.

    With a store, discovery also draws on the books the store holds for the
    author, and the books are written through and served as the store holds
    them; see the module docstring.

    Author ASINs are global, but the catalogue is each marketplace's own, so
    the same ASIN lists different books per region. The books come back in the
    order discovery found them (the catalog's newest-first list at the front)
    unless a sort is given. filters, sort and order are as on the hosted route;
    see libex_core.shaping for the filter names and sortable fields. A name or
    value outside them is ValueError, before anything is sent.

    Raises NotFoundException (code invalid_request) for a value that is not an
    ASIN, NotFoundException when every source confirmed the author has no
    books, AudibleAPIException when no source could say (or hydration failed
    for every book), and RegionException for an unknown region. A list that is
    short of the whole is returned with complete False.
    """
    canonical = _canonical_asin(asin)
    region = validate_region(region)
    check_shaping(filters, sort, order)
    if store is not None:
        await _store.check(store)

    # One deadline for discovery and hydration together, so the two cannot add
    # up past it.
    deadline = time.monotonic() + AUTHOR_BOOKS_TIME_BUDGET_SECONDS
    # The store is passed only when there is one, so a walk without storage is
    # called exactly as it always was.
    walk_extra = {"store": store} if store is not None else {}
    asins, discovery_complete = await _walk_author_books(
        get, canonical, region, deadline, **walk_extra
    )
    hydration = await hydrate_books(
        get, asins, region, deadline=deadline, high_concurrency=True, store=store
    )
    return _assemble(discovery_complete, hydration, filters, sort, order)


# ============================================================
# BY NAME
# ============================================================

async def get_author_books_by_name(
    get: AudibleGet,
    name: str,
    *,
    region: str = "us",
    filters: dict[str, Any] | None = None,
    sort: str | None = None,
    order: str = "asc",
    store: "LocalStore | None" = None,
) -> BookList:
    """
    Fetches the full books an author is credited with, by exact author name.

    With a store the books are written through and served as the store holds
    them, a book Audible could not give is answered from its stored copy, and
    the store plays no part in discovery, as on the hosted route.

    For a caller with a name and no ASIN. The catalog is searched on the name
    and the results kept only where an author's name matches it exactly,
    ignoring case, one page at a time. A walk that stopped before a confirmed
    end (a later page failed, a plateau, the page cap) still returns what it
    gathered, with complete False and discovery-incomplete. filters, sort and
    order are as on the hosted route; see libex_core.shaping.

    The name goes to Audible as given and is never logged or repeated in a
    message. Raises NotFoundException when no book matched, AudibleAPIException
    when the first page failed (an empty list there would read as an author
    with no books) or hydration failed for every book, ValueError for a filter
    or sort outside what libex_core.shaping allows, and RegionException for an
    unknown region.
    """
    region = validate_region(region)
    check_shaping(filters, sort, order)
    if store is not None:
        await _store.check(store)
    outcome = NameWalkOutcome()
    try:
        start = time.monotonic()
        asins, pages_fetched, completed = await walk_author_books_by_name(
            get, name, region, outcome=outcome
        )
        author_book_took = round((time.monotonic() - start) * 1000, 2)

        if not asins:
            raise NotFoundException(_NOT_FOUND_MESSAGE)

        logger.info("Requested Audible Author Books By Name", extra={
            "author_book_num": len(asins),
            "pages_fetched": pages_fetched,
            "author_book_took": author_book_took,
            "region": region,
        })
    except NotFoundException:
        raise
    except Exception as e:
        logger.warning("Failed to fetch author books by name", extra={
            "name_length": len(name),
            "region": region,
            "error_type": type(e).__name__,
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, "Failed to fetch author books by name") from e

    if not completed:
        logger.info("Audible Author Books by-name walk ended before a confirmed end", extra={
            "region": region,
            "stop": outcome.stop,
            "pages_fetched": pages_fetched,
            "asins_collected": len(asins),
        })

    hydration = await hydrate_books(get, asins, region, store=store)
    return _assemble(completed, hydration, filters, sort, order)
