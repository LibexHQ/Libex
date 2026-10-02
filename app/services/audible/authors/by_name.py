"""
Audible author-books lookup by author name, bound to the hosted client.
The walk itself lives in libex_core/audible/authors/by_name.py. This module
supplies the client it runs through and decides what a walk that stopped
short means for the hosted response: it marks the response's facts incomplete
and logs the early stop. It serves the get_author_books_by_name service call
and the seeder's per-author expansion loop, for callers that have a name and
no author ASIN to attribute against; the ASIN-attributed, category-sliced
walk that get_author_books uses is in catalog.py.
"""

# Standard library
import time

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from libex_core.audible.authors.by_name import NameWalkOutcome, walk_author_books_by_name
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
    Runs the by-name walk through the hosted client and returns
    (asins, pages_fetched, completed).

    facts, when given, is marked incomplete (REASON_DISCOVERY_INCOMPLETE)
    whenever the walk stops short of a confirmed end -- a later-page failure,
    the deadline, the page cap or a detected repeat -- so a caller holding
    the same ResponseFacts can tell a truncated list from a whole one.
    outcome, when given, reports how the walk stopped (see NameWalkOutcome).
    A first-page failure raises AudibleAPIException.

    audible_get is looked up in this module's namespace on every call, never
    captured, so a stand-in assigned over it is the one called.
    """
    walk_outcome = outcome if outcome is not None else NameWalkOutcome()
    asins, pages_fetched, completed = await walk_author_books_by_name(
        audible_get,
        name,
        region,
        deadline=deadline,
        concurrency=concurrency,
        outcome=walk_outcome,
    )
    if not completed:
        record_incomplete(facts, REASON_DISCOVERY_INCOMPLETE)
        # One summary for every early stop, including the ones (deadline,
        # plateau, page cap) that log nothing else. Never the name.
        logger.info(
            "Audible Author Books by-name walk ended before a confirmed end",
            extra={
                "region": region,
                "stop": walk_outcome.stop,
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
