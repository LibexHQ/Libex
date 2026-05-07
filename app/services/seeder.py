"""
Database seeder service.
Background tasks that expand the local database by walking
the relationship graph and scanning for new releases.

STRATEGY:
1. Author expansion — for every author in the DB, search the Audible
   catalog by name and fetch any books we don't already have.
2. Series expansion — for every series in the DB, fetch the full book
   list from Audible and fill in any missing books.
3. Narrator expansion — for every narrator in the DB, search the Audible
   catalog by narrator name and fetch missing books.
4. New releases — search for recently released books across configured
   regions. Catches new content automatically.

Each phase compounds the next — new books bring in new series, authors,
and narrators that get expanded in subsequent cycles.

All phases use the standard catalog API (no screen endpoints).
Rate-limited by a configurable delay between Audible requests.
"""

# Standard library
import asyncio

# Third party
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Database
from app.db.models import Author, Book, Narrator, Series
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
        await asyncio.sleep(0)  # yield to event loop — let API requests through

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

    total = len(authors)
    logger.info(f"Seeder: expanding {total} authors in {region}")

    for author_asin, author_name in authors:
        try:
            # Search Audible catalog by author name
            book_asins = await _fetch_author_book_asins(author_name, region)
            await asyncio.sleep(delay)

            if not book_asins:
                stats["authors_processed"] += 1
                if stats["authors_processed"] % 100 == 0:
                    logger.info(f"Seeder: author progress {stats['authors_processed']}/{total}, {stats['books_discovered']} new books so far")
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
                    await asyncio.sleep(0)  # yield to event loop

                stats["books_discovered"] += len(missing)
                logger.info(
                    f"Seeder: {author_name} — {len(missing)} new books",
                    extra={"author_asin": author_asin, "region": region},
                )

            stats["authors_processed"] += 1

            if stats["authors_processed"] % 100 == 0:
                logger.info(f"Seeder: author progress {stats['authors_processed']}/{total}, {stats['books_discovered']} new books so far")

        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"Seeder: failed to expand author {author_asin} ({author_name}): {e}")

        await asyncio.sleep(delay)

    return stats


# ============================================================
# PHASE 2: SERIES EXPANSION
# ============================================================

async def _expand_series(region: str, delay: float) -> dict[str, int]:
    """
    Walks all known series in the DB. For each series, fetches the full
    book list from Audible and fetches any books not already in the local DB.
    """
    stats = {"series_processed": 0, "books_discovered": 0, "errors": 0}

    from app.services.audible.client import audible_get

    # Get all series ASINs
    async with SessionFactory() as session:
        result = await session.execute(
            select(Series.asin)
            .where(Series.asin.isnot(None))
            .distinct()
        )
        series_asins = [row[0] for row in result.fetchall()]

    total = len(series_asins)
    logger.info(f"Seeder: expanding {total} series in {region}")

    for series_asin in series_asins:
        try:
            # Fetch series book list from Audible
            path = f"/1.0/catalog/products/{series_asin}"
            params = {"response_groups": "relationships"}
            data = await audible_get(region, path, params)
            await asyncio.sleep(delay)
            await asyncio.sleep(0)

            product = data.get("product", {})
            relationships = product.get("relationships", [])

            book_asins = [
                r["asin"] for r in relationships
                if r.get("asin") and r.get("relationship_type") == "product"
            ]

            if not book_asins:
                # Try sort-based extraction (some series use sort instead of type)
                book_asins = [r["asin"] for r in relationships if r.get("asin") and r.get("sort")]

            if not book_asins:
                stats["series_processed"] += 1
                if stats["series_processed"] % 100 == 0:
                    logger.info(f"Seeder: series progress {stats['series_processed']}/{total}, {stats['books_discovered']} new books so far")
                continue

            # Check which books we're missing
            async with SessionFactory() as session:
                missing = await _get_missing_asins(session, book_asins)

            if missing:
                for i in range(0, len(missing), 50):
                    chunk = missing[i:i + 50]
                    try:
                        async with SessionFactory() as session:
                            await get_books_by_asins(chunk, region, session)
                    except Exception:
                        pass
                    await asyncio.sleep(delay)
                    await asyncio.sleep(0)

                stats["books_discovered"] += len(missing)
                logger.info(
                    f"Seeder: series {series_asin} — {len(missing)} new books",
                )

            stats["series_processed"] += 1

            if stats["series_processed"] % 100 == 0:
                logger.info(f"Seeder: series progress {stats['series_processed']}/{total}, {stats['books_discovered']} new books so far")

        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"Seeder: failed to expand series {series_asin}: {e}")

        await asyncio.sleep(delay)

    return stats


# ============================================================
# PHASE 3: NARRATOR EXPANSION
# ============================================================

async def _expand_narrators(region: str, delay: float) -> dict[str, int]:
    """
    Walks all known narrators in the DB. For each narrator, searches the
    Audible catalog by name and fetches any books not already in the local DB.
    """
    stats = {"narrators_processed": 0, "books_discovered": 0, "errors": 0}

    from app.services.audible.client import audible_get

    # Get all narrator names
    async with SessionFactory() as session:
        result = await session.execute(
            select(Narrator.name)
            .where(Narrator.name.isnot(None))
            .distinct()
        )
        narrator_names = [row[0] for row in result.fetchall()]

    total = len(narrator_names)
    logger.info(f"Seeder: expanding {total} narrators in {region}")

    for narrator_name in narrator_names:
        try:
            # Search Audible catalog by narrator name
            path = "/1.0/catalog/products"
            params = {
                "narrator": narrator_name,
                "num_results": 50,
                "response_groups": "product_desc,contributors,series,product_attrs,media",
            }
            data = await audible_get(region, path, params)
            await asyncio.sleep(delay)
            await asyncio.sleep(0)

            products = data.get("products", [])
            if not products:
                stats["narrators_processed"] += 1
                if stats["narrators_processed"] % 100 == 0:
                    logger.info(f"Seeder: narrator progress {stats['narrators_processed']}/{total}, {stats['books_discovered']} new books so far")
                continue

            book_asins = [
                p["asin"] for p in products
                if p.get("asin") and any(
                    n.get("name", "").lower() == narrator_name.lower()
                    for n in p.get("narrators", [])
                )
            ]

            if not book_asins:
                stats["narrators_processed"] += 1
                if stats["narrators_processed"] % 100 == 0:
                    logger.info(f"Seeder: narrator progress {stats['narrators_processed']}/{total}, {stats['books_discovered']} new books so far")
                continue

            # Check which books we're missing
            async with SessionFactory() as session:
                missing = await _get_missing_asins(session, book_asins)

            if missing:
                for i in range(0, len(missing), 50):
                    chunk = missing[i:i + 50]
                    try:
                        async with SessionFactory() as session:
                            await get_books_by_asins(chunk, region, session)
                    except Exception:
                        pass
                    await asyncio.sleep(delay)
                    await asyncio.sleep(0)

                stats["books_discovered"] += len(missing)
                logger.info(
                    f"Seeder: {narrator_name} — {len(missing)} new books",
                )

            stats["narrators_processed"] += 1

            if stats["narrators_processed"] % 100 == 0:
                logger.info(f"Seeder: narrator progress {stats['narrators_processed']}/{total}, {stats['books_discovered']} new books so far")

        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"Seeder: failed to expand narrator {narrator_name}: {e}")

        await asyncio.sleep(delay)

    return stats


# ============================================================
# PHASE 4: NEW RELEASES
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
            await asyncio.sleep(0)  # yield to event loop

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
                await asyncio.sleep(0)  # yield to event loop

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
        "Seeder: starting",
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
                "series_processed": 0,
                "narrators_processed": 0,
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

                # Phase 2: expand from known series
                series_stats = await _expand_series(region, delay)
                cycle_stats["series_processed"] += series_stats["series_processed"]
                cycle_stats["books_discovered"] += series_stats["books_discovered"]
                cycle_stats["errors"] += series_stats["errors"]

                # Phase 3: expand from known narrators
                narrator_stats = await _expand_narrators(region, delay)
                cycle_stats["narrators_processed"] += narrator_stats["narrators_processed"]
                cycle_stats["books_discovered"] += narrator_stats["books_discovered"]
                cycle_stats["errors"] += narrator_stats["errors"]

                # Phase 4: scan for new releases
                release_stats = await _scan_new_releases(region, delay)
                cycle_stats["new_releases"] += release_stats["books_discovered"]
                cycle_stats["errors"] += release_stats["errors"]

            logger.info("Seeder: cycle complete", extra=cycle_stats)

        except Exception as e:
            logger.error(f"Seeder: cycle failed: {e}")

        await asyncio.sleep(interval)