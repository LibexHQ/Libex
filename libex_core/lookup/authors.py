"""
Looking an author up: the profile by ASIN, and a search by name.

The library's counterparts of the hosted routes' live path, without the cache,
the database backstop or persistence: an outage is AudibleAPIException, and
Audible having no such author is NotFoundException. An author ASIN is global,
so it resolves in all eleven regions, though the name and bio that come back
are the marketplace's own, which is why region is still passed through. The
author's books are in author_books.py.

What a caller typed as a name goes to Audible as given and is never logged or
repeated in a message. Nothing here reads the environment.
"""

# Standard library
import logging
import time

# Core
from libex_core.audible.authors.profile import (
    fetch_author_profile,
    fetch_author_suggestion_asins,
    normalize_author,
)
from libex_core.audible.client import (
    AudibleGet,
    as_audible_failure,
    upstream_status_of,
    validate_region,
)
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup._common import OUTAGE_MESSAGE
from libex_core.lookup.books import _canonical_asin
from libex_core.models import AuthorResponse

logger = logging.getLogger("libex")


async def get_author(get: AudibleGet, asin: str, *, region: str = "us") -> AuthorResponse:
    """
    Fetches an author's profile by ASIN.

    Raises NotFoundException (code invalid_request) for a value that is not an
    ASIN, NotFoundException when Audible has no author behind the ASIN or the
    record carries no name, AudibleAPIException when Audible could not be
    reached, and RegionException for an unknown region.
    """
    canonical = _canonical_asin(asin)
    region = validate_region(region)
    try:
        start = time.monotonic()
        data = await fetch_author_profile(get, canonical, region)
        author_took = round((time.monotonic() - start) * 1000, 2)

        if not data or (data.get("contributor") or {}).get("name") is None:
            raise NotFoundException("Author not found")

        normalized = normalize_author(data, canonical, region)

        logger.info("Requested Audible Author", extra={
            "author_took": author_took,
            "region": region,
        })
    except NotFoundException:
        raise
    except Exception as e:
        logger.warning("Audible unavailable for author", extra={
            "author_asin": canonical,
            "region": region,
            "error_type": type(e).__name__,
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, OUTAGE_MESSAGE) from e
    return AuthorResponse(**normalized)


async def search_authors(
    get: AudibleGet, name: str, *, region: str = "us"
) -> list[AuthorResponse]:
    """
    Searches for authors by name through Audible's search suggestions, then
    fetches each suggested author's profile, in the order Audible suggested
    them.

    A suggestion Audible has no record of is left out, and one that could not
    be fetched is left out with a warning, as the hosted route does, since the
    others are still worth returning.

    Raises NotFoundException when no author was found, AudibleAPIException
    when the suggestions could not be fetched, or when authors were suggested
    and every one of them failed to fetch (an empty list there would pass an
    outage off as a confirmed absence), and RegionException for an unknown
    region.
    """
    region = validate_region(region)
    try:
        start = time.monotonic()
        asins = await fetch_author_suggestion_asins(get, name, region)
        search_took = round((time.monotonic() - start) * 1000, 2)

        logger.info("Requested Audible Author Search", extra={
            "search_took": search_took,
            "region": region,
        })

        authors: list[AuthorResponse] = []
        skipped_num = 0
        for asin in asins:
            try:
                authors.append(await get_author(get, asin, region=region))
            except NotFoundException:
                continue
            except AudibleAPIException:
                # get_author already logged the failure; one warning for the
                # lot follows the loop.
                skipped_num += 1
                continue

        if skipped_num:
            logger.warning(
                "Author search: could not resolve one or more suggested authors, skipping",
                extra={"region": region, "skipped_num": skipped_num},
            )

        if not authors:
            if skipped_num:
                raise AudibleAPIException("Author search failed")
            raise NotFoundException("No authors found")
        return authors

    except NotFoundException:
        raise
    except Exception as e:
        logger.warning("Author search failed", extra={
            "name_length": len(name),
            "region": region,
            "error_type": type(e).__name__,
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, "Author search failed") from e
