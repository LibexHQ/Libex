"""
Database seeder service.
Background tasks that expand the local database by walking
the relationship graph and scanning for new releases.

STRATEGY:
1. Author expansion — for every author in the DB, search the Audible
   catalog by name and fetch any books we don't already have. Each new
   book brings in new series, authors, and narrators, compounding the
   next cycle.
2. New releases — daily search for recently released books across
   configured regions. Catches new content automatically.

Both phases use the standard catalog API (no screen endpoints).
Rate-limited by a configurable delay between Audible requests.
"""

# Standard library
import asyncio
from typing import Any

# Third party
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Database
from app.db.models import Author, Book
from app.db.session import engine

# Core
from app.core.config import get_settings
from app.core.logging import get_logger

# Services
from app.services.audible.books import get_books_by_asins

logger = get_logger()
settings = get_settings()

SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


# ============================================================
# HELPERS
# ============================================================

async def _get_missing_asins(session: AsyncSession, asins: list[str]) -> list[str]:
    """Returns ASINs from the input list that are not already in the local DB."""
    if not asins:
        return []
    result = await session.execute(
        select(Book.asin).where(Book.asin.in_(asins))
    )
    existing = {row[0] for row in result.fetchall()}
    return [a for a in asins if a not in existing]


async def _fetch_author_book_asins(name: str, region: str) -> list[str]:
    """
    Searches the Audible catalog for books by author name.
    Returns a list of ASINs. Does not write to DB — that happens
    when the caller fetches full metadata via get_books_by_asins.

    Duplicates the catalog search logic from authors.py to avoid
    circular imports and session entanglement with the seeder's
    own session management.
    """
    from app.services.audible.client import audible_get

    asins: list[str] = []
    page = 0
    while page <= 20:
        path = "/1.0/catalog/products"
        params = {
            "author": name,
            "num_results": 50,
            "page": page,
            "response_groups": "product_desc,contributors,series,product_attrs,media",
            "sort_by": "-ReleaseDate",
        }
        data = await audible_get(region, path, params)
        products = data.get("products", [])
        if not products:
            break

        for product in products:
            matches = any(
                a.get("name", "").lower() == name.lower()
                for a in product.get("authors", [])
            )
            language = product.get("language", "").lower()
            is_english = language.startswith("english") or language == "englisch"
            asin = product.get("asin")
            if asin and matches and is_english and asin not in asins:
                asins.append(asin)

        if len(products) < 50:
            break
        page += 1

    return asins


# ============================================================
# PHASE 1: AUTHOR EXPANSION
# ============================================================

async def _expand_authors(region: str, delay: float) -> dict[str, int]:
    """
    Walks all known authors in the DB for a given region.
    For each author, searches the Audible catalog by name and fetches
    full metadata for any books not already in the local DB.
    """
    stats = {"authors_processed": 0, "books_discovered": 0, "errors": 0}

    # Get all authors with ASINs in this region
    async with SessionFactory() as session:
        result = await session.execute(
            select(Author.asin, Author.name)
            .where(
                Author.asin.isnot(None),
                Author.name.isnot(None),
                Author.region == region,
            )
            .distinct()
        )
        authors = result.fetchall()

    logger.info(f"Seeder: expanding {len(authors)} authors in {region}")

    for author_asin, author_name in authors:
        try:
            # Search Audible catalog by author name
            book_asins = await _fetch_author_book_asins(author_name, region)
            await asyncio.sleep(delay)

            if not book_asins:
                stats["authors_processed"] += 1
                continue

            # Check which books we're missing
            async with SessionFactory() as session:
                missing = await _get_missing_asins(session, book_asins)

            if missing:
                # Fetch only the missing books in chunks
                for i in range(0, len(missing), 50):
                    chunk = missing[i:i + 50]
                    try:
                        async with SessionFactory() as session:
                            await get_books_by_asins(chunk, region, session)
                    except Exception:
                        pass
                    await asyncio.sleep(delay)

                stats["books_discovered"] += len(missing)
                logger.info(
                    f"Seeder: {author_name} — {len(missing)} new books",
                    extra={"author_asin": author_asin, "region": region},
                )

            stats["authors_processed"] += 1

        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"Seeder: failed to expand author {author_asin} ({author_name}): {e}")

        await asyncio.sleep(delay)

    return stats


# ============================================================
# PHASE 2: NEW RELEASES
# ============================================================

async def _scan_new_releases(region: str, delay: float) -> dict[str, int]:
    """
    Searches Audible for the most recently released books and
    fetches full metadata for any not already in the local DB.
    """
    stats = {"books_discovered": 0, "pages_scanned": 0, "errors": 0}

    try:
        from app.services.audible.client import audible_get

        all_asins: list[str] = []
        page = 0
        while page < 5:
            path = "/1.0/catalog/products"
            params = {
                "num_results": 50,
                "page": page,
                "response_groups": "product_desc,contributors,series,product_attrs,media",
                "sort_by": "-ReleaseDate",
            }
            data = await audible_get(region, path, params)
            products = data.get("products", [])
            if not products:
                break

            for product in products:
                asin = product.get("asin")
                if asin and asin not in all_asins:
                    all_asins.append(asin)

            stats["pages_scanned"] += 1
            if len(products) < 50:
                break
            page += 1
            await asyncio.sleep(delay)

        if not all_asins:
            return stats

        # Check which are new to us
        async with SessionFactory() as session:
            missing = await _get_missing_asins(session, all_asins)

        if missing:
            for i in range(0, len(missing), 50):
                chunk = missing[i:i + 50]
                try:
                    async with SessionFactory() as session:
                        await get_books_by_asins(chunk, region, session)
                except Exception:
                    stats["errors"] += 1
                await asyncio.sleep(delay)

            stats["books_discovered"] = len(missing)

        logger.info(
            f"Seeder: new releases scan complete for {region}",
            extra={
                "total_found": len(all_asins),
                "new_books": len(missing) if missing else 0,
                "pages_scanned": stats["pages_scanned"],
            },
        )

    except Exception as e:
        stats["errors"] += 1
        logger.warning(f"Seeder: new releases scan failed for {region}: {e}")

    return stats


# ============================================================
# MAIN LOOP
# ============================================================

async def run_seeder() -> None:
    """
    Main seeder loop. Runs on a configurable schedule.
    Disabled by default — set SEEDER_ENABLED=true to activate.
    """
    if not settings.seeder_enabled:
        logger.info("Seeder: disabled")
        return

    regions = [r.strip() for r in settings.seeder_regions.split(",") if r.strip()]
    interval = settings.seeder_interval_hours * 3600
    delay = settings.seeder_request_delay

    logger.info(
        f"Seeder: starting",
        extra={
            "regions": regions,
            "interval_hours": settings.seeder_interval_hours,
            "delay_seconds": delay,
        },
    )

    # Let the app fully start before beginning
    await asyncio.sleep(30)

    while True:
        try:
            logger.info("Seeder: starting cycle")

            cycle_stats = {
                "authors_processed": 0,
                "books_discovered": 0,
                "new_releases": 0,
                "errors": 0,
            }

            for region in regions:
                # Phase 1: expand from known authors
                author_stats = await _expand_authors(region, delay)
                cycle_stats["authors_processed"] += author_stats["authors_processed"]
                cycle_stats["books_discovered"] += author_stats["books_discovered"]
                cycle_stats["errors"] += author_stats["errors"]

                # Phase 2: scan for new releases
                release_stats = await _scan_new_releases(region, delay)
                cycle_stats["new_releases"] += release_stats["books_discovered"]
                cycle_stats["errors"] += release_stats["errors"]

            logger.info("Seeder: cycle complete", extra=cycle_stats)

        except Exception as e:
            logger.error(f"Seeder: cycle failed: {e}")

        await asyncio.sleep(interval)