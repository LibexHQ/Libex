"""
Audible author profiles.
Fetches an author's profile (name, bio, image) from the Audible contributors
endpoint and searches authors by name through Audible search suggestions.

DESIGN PHILOSOPHY: Audible-first.
Audible is the source of truth. get_author fetches a profile fresh or from
cache and writes what Audible returns to the relational DB and the cache.
The author-books walks live elsewhere in the package: get_author_books
in __init__.py, the ASIN-attributed catalog walk in catalog.py, the by-name
walk in by_name.py and the screens walk in screens.py.
"""

# Standard library
import time
from typing import Any

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from libex_core.asin import is_valid_asin
from libex_core.audible.authors.profile import (
    fetch_author_profile,
    fetch_author_suggestion_asins,
    normalize_author,
)
from libex_core.audible.client import as_audible_failure, upstream_status_of
from libex_core.exceptions import AudibleAPIException, NotFoundException
from app.core.logging import get_logger
from app.core.response_headers import (
    ResponseFacts,
    SOURCE_AUDIBLE,
    SOURCE_CACHE,
    SOURCE_DB,
    record_source,
)

# Services
from app.services.audible import audible_get
from app.services.cache import manager as cache
from app.services.cache.manager import author_key
from app.services.db.persist_queue import persist_author_background
from app.services.db.reader import get_author_from_db

logger = get_logger()


# ============================================================
# AUDIBLE REQUESTS
# ============================================================


async def _fetch_author_details(asin: str, region: str) -> dict[str, Any]:
    """
    Fetches an author's profile through the hosted client. audible_get is
    looked up in this module's namespace on every call, never captured, so a
    stand-in assigned over it is the one called.
    """
    return await fetch_author_profile(audible_get, asin, region)


# ============================================================
# PUBLIC API
# ============================================================


async def get_author(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
    *,
    facts: ResponseFacts | None = None,
) -> dict[str, Any]:
    """
    Fetches author profile by ASIN.
    Audible-first, writes to DB, falls back to DB then cache.

    Single-source by construction -- cache, then audible, then db, then
    cache again, never more than one per call -- so facts takes exactly one
    record_source per return path. get_author_books, which lives in the
    package __init__, is deliberately not given the same treatment: it
    resolves to an ASIN list unioned from up to four sources at once, not
    one dict a single token could describe, and the books it names are attributed by whichever
    call the route makes to get_books_by_asins afterward.
    """
    # Screened before anything else: a value that is not an ASIN names no
    # author, which is a terminal answer -- not an outage, so it must not
    # reach the fetch (which would refuse it with a ValueError that the
    # fallback below reads as Audible being unreachable).
    if not is_valid_asin(asin):
        raise NotFoundException(f"Author not found: {asin}")

    if use_cache:
        cached = await cache.get(session, author_key(asin, region))
        # Same reason as the two rollbacks in _walk_author_books (in
        # authors/__init__.py): a connection is held for work, not for a
        # request. The read above autobegins a transaction on session, and a
        # READ COMMITTED
        # transaction advertises backend_xmin and can become the cluster's
        # oldest xmin even when it has only ever read -- measured on
        # PostgreSQL 16.14 -- holding the xmin horizon against autovacuum
        # until it ends. Nothing between here and the contributors fetch
        # below needs it open, and that fetch queues against the process-wide
        # pool in libex_core/audible/_concurrency.py rather than going out immediately.
        #
        # Nothing after this point depends on transaction state established
        # before it: cached is a plain already-materialized value, and the DB
        # and cache reads in the failure branch each open their own
        # transaction on their next statement, which SQLAlchemy re-acquires
        # transparently.
        await session.rollback()
        if cached:
            record_source(facts, SOURCE_CACHE)
            return cached

    try:
        start = time.monotonic()
        data = await _fetch_author_details(asin, region)
        author_took = round((time.monotonic() - start) * 1000, 2)

        if not data or data.get("contributor", {}).get("name") is None:
            raise NotFoundException(f"Author not found: {asin}")

        normalized = normalize_author(data, asin, region)

        # Persist to DB and cache in the background
        persist_author_background(normalized, region, confirm=True)

        logger.info("Requested Audible Author", extra={
            "author_took": author_took,
            "region": region,
        })

        record_source(facts, SOURCE_AUDIBLE)
        return normalized

    except NotFoundException:
        raise
    except Exception as e:
        # Try DB first
        db_result = await get_author_from_db(session, asin, region)
        if db_result:
            record_source(facts, SOURCE_DB)
            return db_result

        # Fall back to cache
        cached = await cache.get(session, author_key(asin, region))
        if cached:
            record_source(facts, SOURCE_CACHE)
            return cached

        # Neither a stored copy nor a cached one exists -- that is silence,
        # not a confirmed absence, so what reaches the caller has to say
        # Audible could not be reached rather than that the author is not
        # there.
        logger.warning("Audible unavailable and no cached author data found", extra={
            "author_asin": asin,
            "region": region,
            "error": str(e),
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(
            e, "Audible unavailable and no cached author data found"
        ) from e


async def search_authors(
    name: str,
    region: str,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Searches for authors by name using Audible search suggestions."""
    try:
        start = time.monotonic()
        asins = await fetch_author_suggestion_asins(audible_get, name, region)
        search_took = round((time.monotonic() - start) * 1000, 2)

        logger.info("Requested Audible Author Search", extra={
            "search_took": search_took,
            "region": region,
        })

        if not asins:
            return []

        authors = []
        skipped_asins: list[str] = []
        for asin in asins:
            try:
                author = await get_author(asin, region, session)
                authors.append(author)
            except NotFoundException:
                continue
            except AudibleAPIException:
                # One suggested author being unreachable does not sink a
                # search that already has other hits to show. get_author
                # itself already logs the failure that produced this
                # exception, so collecting the ASIN here and warning once
                # below, after the loop, avoids a second warning per item
                # on top of that.
                skipped_asins.append(asin)
                continue

        if skipped_asins:
            logger.warning(
                "Author search: could not resolve one or more suggested authors, skipping",
                extra={
                    "region": region,
                    "skipped_num": len(skipped_asins),
                    "skipped_asins": skipped_asins,
                },
            )

        return authors

    except NotFoundException:
        raise
    except Exception as e:
        # name is caller-authored and never logged -- see the "Deliberately
        # no author_name field" note on get_author_books_by_name in by_name.py.
        logger.warning("Author search failed", extra={
            "name_length": len(name),
            "region": region,
            "error": str(e),
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, "Author search failed") from e
