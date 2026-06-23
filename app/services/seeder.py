"""
Database seeder service.
Background tasks that expand the local database by walking
the relationship graph and scanning for new releases.

STRATEGY:
1. Author expansion — for authors not recently checked, search the
   Audible catalog by name and fetch any books we don't already have.
2. Series expansion — for series not recently checked, fetch the full
   book list from Audible and fill in missing books.
3. Narrator expansion — for narrators not recently checked, search the
   Audible catalog by narrator name and fetch missing books.
4. New releases — walk every genre's catalog by release date and collect
   all reachable books across configured regions (future and recent alike).
   Catches new content automatically.

Each phase compounds the next — new books bring in new series, authors,
and narrators that get expanded in subsequent cycles.

Entities are stamped with last_seeded_at after processing. The seeder
skips entities checked within the last 7 days, so only new and stale
entities are processed each cycle.

All phases use the standard catalog API (no screen endpoints).
Rate-limited by a configurable delay between Audible requests.
"""

# Standard library
import asyncio
from datetime import datetime, timedelta, timezone

# Third party
from sqlalchemy import select, update, or_
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

SEED_STALE_DAYS = 7

# Refresh cadence for upcoming (not-yet-released) books. As a book's release
# date approaches, its details (date, cover, narrator, runtime) firm up, so we
# re-fetch more often the closer it gets. Each tier is
# (max_days_until_release, refresh_if_not_updated_in_days): a book is refreshed
# when it falls within the day range and hasn't been updated within the tier's
# staleness threshold. Books already released are never refreshed — they're
# settled. Ordered nearest-release first; the first matching tier wins.
REFRESH_TIERS = [
    (14, 1),     # within 2 weeks  -> refresh if older than 1 day
    (30, 3),     # within a month  -> 3 days
    (60, 7),     # within 2 months -> 7 days
    (90, 14),    # within 3 months -> 14 days
    (180, 30),   # within 6 months -> 30 days
    (365, 60),   # within a year   -> 60 days
    (None, 90),  # beyond a year   -> 90 days (slow cadence)
]


# ============================================================
# HELPERS
# ============================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stale_cutoff() -> datetime:
    return _now() - timedelta(days=SEED_STALE_DAYS)


async def _get_missing_asins(session: AsyncSession, asins: list[str]) -> list[str]:
    if not asins:
        return []
    # Postgres caps a single query at 32767 bind parameters, so the IN list is
    # chunked — the genre-union scan can hand us tens of thousands of ASINs.
    existing: set[str] = set()
    for i in range(0, len(asins), 5000):
        chunk = asins[i:i + 5000]
        result = await session.execute(
            select(Book.asin).where(Book.asin.in_(chunk))
        )
        existing.update(row[0] for row in result.fetchall())
    return [a for a in asins if a not in existing]


async def _stamp_author(author_id: int) -> None:
    async with SessionFactory() as session:
        await session.execute(
            update(Author).where(Author.id == author_id).values(last_seeded_at=_now())
        )
        await session.commit()


async def _stamp_series(series_asin: str) -> None:
    async with SessionFactory() as session:
        await session.execute(
            update(Series).where(Series.asin == series_asin).values(last_seeded_at=_now())
        )
        await session.commit()


async def _stamp_narrator(narrator_name: str) -> None:
    async with SessionFactory() as session:
        await session.execute(
            update(Narrator).where(Narrator.name == narrator_name).values(last_seeded_at=_now())
        )
        await session.commit()


async def _fetch_author_book_asins(name: str, region: str) -> list[str]:
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
            "products_sort_by": "-ReleaseDate",
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
        await asyncio.sleep(0)

    return asins


async def _fetch_and_persist(missing: list[str], region: str, delay: float) -> None:
    for i in range(0, len(missing), 50):
        chunk = missing[i:i + 50]
        try:
            async with SessionFactory() as session:
                await get_books_by_asins(chunk, region, session)
        except Exception:
            pass
        await asyncio.sleep(delay)
        await asyncio.sleep(0)


# ============================================================
# PHASE 1: AUTHOR EXPANSION
# ============================================================

async def _expand_authors(region: str, delay: float) -> dict[str, int]:
    stats = {"authors_processed": 0, "books_discovered": 0, "errors": 0}
    cutoff = _stale_cutoff()

    async with SessionFactory() as session:
        result = await session.execute(
            select(Author.id, Author.asin, Author.name)
            .where(
                Author.asin.isnot(None),
                Author.name.isnot(None),
                Author.region == region,
                or_(Author.last_seeded_at.is_(None), Author.last_seeded_at < cutoff),
            )
            .distinct()
        )
        authors = result.fetchall()

    total = len(authors)
    if total == 0:
        logger.info(f"Seeder: no stale authors in {region}, skipping")
        return stats

    logger.info(f"Seeder: expanding {total} stale authors in {region}")

    for author_id, author_asin, author_name in authors:
        try:
            book_asins = await _fetch_author_book_asins(author_name, region)
            await asyncio.sleep(delay)

            if book_asins:
                async with SessionFactory() as session:
                    missing = await _get_missing_asins(session, book_asins)
                if missing:
                    await _fetch_and_persist(missing, region, delay)
                    stats["books_discovered"] += len(missing)
                    logger.info(f"Seeder: {author_name} — {len(missing)} new books")

            await _stamp_author(author_id)
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
    stats = {"series_processed": 0, "books_discovered": 0, "errors": 0}
    cutoff = _stale_cutoff()

    from app.services.audible.client import audible_get

    async with SessionFactory() as session:
        result = await session.execute(
            select(Series.asin)
            .where(
                Series.asin.isnot(None),
                or_(Series.last_seeded_at.is_(None), Series.last_seeded_at < cutoff),
            )
            .distinct()
        )
        series_asins = [row[0] for row in result.fetchall()]

    total = len(series_asins)
    if total == 0:
        logger.info(f"Seeder: no stale series in {region}, skipping")
        return stats

    logger.info(f"Seeder: expanding {total} stale series in {region}")

    for series_asin in series_asins:
        try:
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
                book_asins = [r["asin"] for r in relationships if r.get("asin") and r.get("sort")]

            if book_asins:
                async with SessionFactory() as session:
                    missing = await _get_missing_asins(session, book_asins)
                if missing:
                    await _fetch_and_persist(missing, region, delay)
                    stats["books_discovered"] += len(missing)
                    logger.info(f"Seeder: series {series_asin} — {len(missing)} new books")

            await _stamp_series(series_asin)
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
    stats = {"narrators_processed": 0, "books_discovered": 0, "errors": 0}
    cutoff = _stale_cutoff()

    from app.services.audible.client import audible_get

    async with SessionFactory() as session:
        result = await session.execute(
            select(Narrator.name)
            .where(
                Narrator.name.isnot(None),
                or_(Narrator.last_seeded_at.is_(None), Narrator.last_seeded_at < cutoff),
            )
            .distinct()
        )
        narrator_names = [row[0] for row in result.fetchall()]

    total = len(narrator_names)
    if total == 0:
        logger.info(f"Seeder: no stale narrators in {region}, skipping")
        return stats

    logger.info(f"Seeder: expanding {total} stale narrators in {region}")

    for narrator_name in narrator_names:
        try:
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
            book_asins = []
            if products:
                book_asins = [
                    p["asin"] for p in products
                    if p.get("asin") and any(
                        n.get("name", "").lower() == narrator_name.lower()
                        for n in p.get("narrators", [])
                    )
                ]

            if book_asins:
                async with SessionFactory() as session:
                    missing = await _get_missing_asins(session, book_asins)
                if missing:
                    await _fetch_and_persist(missing, region, delay)
                    stats["books_discovered"] += len(missing)
                    logger.info(f"Seeder: {narrator_name} — {len(missing)} new books")

            await _stamp_narrator(narrator_name)
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

async def _fetch_catalog_genres(region: str) -> list[dict[str, str]]:
    """
    Fetches the genre taxonomy from Audible and flattens it to every node —
    parents AND their leaves, each tagged with its parent_id. The seeder's own
    copy: it shares nothing with the live release endpoints and never touches
    the catalog_genres table.

    The response is two levels — a top-level `categories` list of parents, each
    with a `children` list of leaves (both carry `id` + `name`). We keep BOTH:
    every catalog/products query caps at ~535 results, and a parent query is not
    a superset of its children (it surfaces titles no leaf does), so walking
    parents plus leaves and unioning is what reaches the full catalog. Parents
    get parent_id="" (top level); leaves get their parent's id. A leaf that
    appears under two parents yields one node per parent. Deduped by
    (genre_id, parent_id).
    """
    from app.services.audible.client import audible_get

    data = await audible_get(region, "/1.0/catalog/categories", {"root": "Genres"})
    seen: set[tuple[str, str]] = set()
    nodes: list[dict[str, str]] = []
    for parent in data.get("categories", []):
        pid = parent.get("id")
        pname = parent.get("name")
        if pid and pname and (pid, "") not in seen:
            seen.add((pid, ""))
            nodes.append({"genre_id": pid, "name": pname, "parent_id": ""})
        for child in parent.get("children", []):
            gid = child.get("id")
            name = child.get("name")
            if gid and name and pid and (gid, pid) not in seen:
                seen.add((gid, pid))
                nodes.append({"genre_id": gid, "name": name, "parent_id": pid})
    return nodes


async def _walk_genre_for_asins(
    region: str,
    genre: dict[str, str],
    delay: float,
) -> tuple[list[str], int]:
    """
    Walks a single category's catalog by -ReleaseDate and returns the ASINs
    found plus the number of pages scanned. Stops when a page repeats the
    previous one (Audible's ~535 wall) or a page comes back short/empty. Paced
    by SEEDER_REQUEST_DELAY. Raises on an Audible failure — the caller decides
    whether one bad category should stop the whole scan (it shouldn't).
    """
    from app.services.audible.client import audible_get

    asins: list[str] = []
    pages = 0
    page = 0
    prev_asins: list[str] | None = None
    while True:
        params = {
            "category_id": genre["genre_id"],
            "num_results": 50,
            "page": page,
            "response_groups": "product_desc,contributors,series,product_attrs,media",
            "products_sort_by": "-ReleaseDate",
        }
        data = await audible_get(region, "/1.0/catalog/products", params)
        await asyncio.sleep(delay)

        products = data.get("products", [])
        if not products:
            break

        # Duplicate-page wall: Audible repeats the last page at the cap.
        page_asins = [p.get("asin") for p in products]
        if page_asins == prev_asins:
            break
        prev_asins = page_asins

        for product in products:
            if not product.get("title"):
                continue
            asin = product.get("asin")
            if asin:
                asins.append(asin)

        pages += 1

        if len(products) < 50:
            break
        page += 1

    return asins, pages


async def _scan_new_releases(region: str, delay: float) -> dict[str, int]:
    """
    Walks every catalog node (parents AND leaves) by -ReleaseDate, collecting
    ALL reachable ASINs — future pre-orders and recent releases alike, no date
    gate — and persisting the ones we don't already have. This is how both
    new-releases and coming-soon data lands in the DB; the tiered refresh
    (_refresh_upcoming) then keeps near-release pre-orders current.

    Audible caps every catalog/products query at ~535 results and a parent query
    is not a superset of its children, so we walk parents plus leaves and union
    the results, deduped by ASIN.

    Resilience: each node is walked independently. A failure on one node (a
    transient Audible error) is logged and skipped — it increments the error
    count but does NOT abort the scan, so the rest of the walk and the books
    already collected are preserved.
    """
    stats = {"books_discovered": 0, "pages_scanned": 0, "errors": 0}

    try:
        genres = await _fetch_catalog_genres(region)
    except Exception as e:
        stats["errors"] += 1
        logger.warning(f"Seeder: new releases scan failed for {region}: {e}")
        return stats

    all_asins: list[str] = []
    seen: set[str] = set()

    for genre in genres:
        try:
            found, pages = await _walk_genre_for_asins(region, genre, delay)
        except Exception as e:
            stats["errors"] += 1
            logger.warning(
                f"Seeder: new releases genre walk failed for {region}: {e}",
                extra={"genre_id": genre.get("genre_id"), "genre_name": genre.get("name")},
            )
            continue
        stats["pages_scanned"] += pages
        for asin in found:
            if asin not in seen:
                seen.add(asin)
                all_asins.append(asin)

    # Persist the books we don't already have, even if some genres failed above.
    try:
        async with SessionFactory() as session:
            missing = await _get_missing_asins(session, all_asins) if all_asins else []
        if missing:
            await _fetch_and_persist(missing, region, delay)
            stats["books_discovered"] = len(missing)
    except Exception as e:
        stats["errors"] += 1
        logger.warning(f"Seeder: new releases persist failed for {region}: {e}")

    logger.info(
        f"Seeder: new releases scan complete for {region} — "
        f"{len(all_asins)} found, {stats['books_discovered']} new, "
        f"{stats['pages_scanned']} pages scanned",
        extra={
            "total_found": len(all_asins),
            "new_books": stats["books_discovered"],
            "pages_scanned": stats["pages_scanned"],
            "genres": len(genres),
            "errors": stats["errors"],
        },
    )

    return stats


# ============================================================
# PHASE 5: REFRESH UPCOMING
# ============================================================

async def _select_refresh_asins(
    session: AsyncSession, region: str, now: datetime
) -> list[str]:
    """
    Returns ASINs of upcoming books due for a refresh, tiered by REFRESH_TIERS
    and ordered oldest-first (by updated_at). Pure selection — no fetching — so
    the tier and staleness logic can be tested against a real database.
    """
    tier_conditions = []
    prev_max = 0
    for max_days, stale_days in REFRESH_TIERS:
        stale_cutoff = now - timedelta(days=stale_days)
        lower = now + timedelta(days=prev_max)
        if max_days is None:
            window = Book.release_date > lower
        else:
            upper = now + timedelta(days=max_days)
            window = (Book.release_date > lower) & (Book.release_date <= upper)
            prev_max = max_days
        tier_conditions.append(window & (Book.updated_at < stale_cutoff))

    result = await session.execute(
        select(Book.asin)
        .where(
            Book.region == region,
            Book.release_date.isnot(None),
            Book.release_date > now,
            or_(*tier_conditions),
        )
        .order_by(Book.updated_at.asc())
    )
    return [row[0] for row in result.fetchall()]


async def _refresh_upcoming(region: str, delay: float) -> dict[str, int]:
    """
    Re-fetches upcoming (not-yet-released) books whose details may have changed
    as their release date approaches. Selection is tiered by REFRESH_TIERS:
    the closer a book is to release, the shorter the staleness threshold before
    it's refreshed. Already-released books are left alone.

    Books are processed oldest-first (by updated_at) so the most stale get
    priority, and refreshing a book updates its updated_at — which drops it out
    of the next cycle's selection until it ages back past its tier threshold.
    """
    stats = {"books_refreshed": 0, "errors": 0}

    try:
        now = _now()

        async with SessionFactory() as session:
            asins = await _select_refresh_asins(session, region, now)

        if not asins:
            return stats

        await _fetch_and_persist(asins, region, delay)
        stats["books_refreshed"] = len(asins)

        logger.info(
            f"Seeder: refreshed {len(asins)} upcoming books for {region}",
            extra={"books_refreshed": len(asins)},
        )

    except Exception as e:
        stats["errors"] += 1
        logger.warning(f"Seeder: refresh upcoming failed for {region}: {e}")

    return stats


# ============================================================
# MAIN LOOP
# ============================================================

async def run_seeder() -> None:
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

    await asyncio.sleep(30)

    while True:
        try:
            logger.info("Seeder: starting cycle")

            cycle_stats = {
                "authors_processed": 0,
                "series_processed": 0,
                "narrators_processed": 0,
                "books_discovered": 0,
                "errors": 0,
            }

            for region in regions:
                author_stats = await _expand_authors(region, delay)
                cycle_stats["authors_processed"] += author_stats["authors_processed"]
                cycle_stats["books_discovered"] += author_stats["books_discovered"]
                cycle_stats["errors"] += author_stats["errors"]

                series_stats = await _expand_series(region, delay)
                cycle_stats["series_processed"] += series_stats["series_processed"]
                cycle_stats["books_discovered"] += series_stats["books_discovered"]
                cycle_stats["errors"] += series_stats["errors"]

                narrator_stats = await _expand_narrators(region, delay)
                cycle_stats["narrators_processed"] += narrator_stats["narrators_processed"]
                cycle_stats["books_discovered"] += narrator_stats["books_discovered"]
                cycle_stats["errors"] += narrator_stats["errors"]

            logger.info("Seeder: cycle complete", extra=cycle_stats)

        except Exception as e:
            logger.error(f"Seeder: cycle failed: {e}")

        await asyncio.sleep(interval)


async def run_new_releases_seeder() -> None:
    """
    Independent worker that scans new releases on its own interval.

    Runs separately from the main expansion cycle (run_seeder) so new content
    can be picked up more often than the heavier author/series/narrator walks.
    Shares the same enable flag, regions, and request delay; only the interval
    is its own. The two workers run independently and may occasionally overlap.
    """
    if not settings.seeder_enabled:
        return

    regions = [r.strip() for r in settings.seeder_regions.split(",") if r.strip()]
    interval = settings.seeder_new_releases_interval_hours * 3600
    delay = settings.seeder_request_delay

    logger.info(
        "Seeder: new releases worker starting",
        extra={
            "regions": regions,
            "interval_hours": settings.seeder_new_releases_interval_hours,
            "refresh_enabled": settings.seeder_refresh_enabled,
            "delay_seconds": delay,
        },
    )

    await asyncio.sleep(30)

    while True:
        try:
            logger.info("Seeder: starting new releases cycle")
            cycle_stats = {"new_releases": 0, "books_refreshed": 0, "errors": 0}

            for region in regions:
                release_stats = await _scan_new_releases(region, delay)
                cycle_stats["new_releases"] += release_stats["books_discovered"]
                cycle_stats["errors"] += release_stats["errors"]

                if settings.seeder_refresh_enabled:
                    refresh_stats = await _refresh_upcoming(region, delay)
                    cycle_stats["books_refreshed"] += refresh_stats["books_refreshed"]
                    cycle_stats["errors"] += refresh_stats["errors"]

            logger.info("Seeder: new releases cycle complete", extra=cycle_stats)

        except Exception as e:
            logger.error(f"Seeder: new releases cycle failed: {e}")

        await asyncio.sleep(interval)