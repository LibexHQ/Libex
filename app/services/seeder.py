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
4. New releases — search for recently released books across configured
   regions. Catches new content automatically.

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
from app.services.db.reader import get_seeder_covered_through
from app.services.db.writer import upsert_seeder_covered_through

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


# Unreleased pre-orders carry this sentinel publication_datetime.
_UNRELEASED_PLACEHOLDER = "2200-01-01T00:00:00Z"


def _product_release_dt(product: dict) -> datetime | None:
    """
    Parses a raw catalog product's release_date (YYYY-MM-DD) into a UTC
    datetime, or None if absent/unparseable. The seeder's own copy — it only
    needs the date to gate the walk; full normalization happens on persist.
    """
    raw = product.get("release_date")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def _get_missing_asins(session: AsyncSession, asins: list[str]) -> list[str]:
    if not asins:
        return []
    result = await session.execute(
        select(Book.asin).where(Book.asin.in_(asins))
    )
    existing = {row[0] for row in result.fetchall()}
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

async def _scan_new_releases(region: str, delay: float) -> dict[str, int]:
    """
    Walks the catalog by -ReleaseDate (newest first), skipping future
    pre-orders, collecting released books within the configured day window,
    and persisting the ones we don't already have.

    Stops on whichever comes first:
      - date edge: a release date older than (now - days) — the window is fully
        covered (the correctness backstop; always terminates the walk)
      - caught up: two consecutive pages whose in-window books are all already
        in the DB — but ONLY when not expanding the window
      - short page: the catalog returned fewer than a full page (no more
        products — also full coverage)

    covered_through (days back, per region) is read to decide whether the window
    is expanding. It's written ONLY on a clean, complete walk (date edge or
    short page), so a failed/partial run never records false coverage and the
    next run re-walks instead of stopping early on knowns.
    """
    stats = {"books_discovered": 0, "pages_scanned": 0, "errors": 0}

    try:
        from app.services.audible.client import audible_get

        days = settings.seeder_new_releases_days
        now = _now()
        window_start = now - timedelta(days=days)

        all_asins: list[str] = []
        missing: list[str] = []
        expanding = True

        async with SessionFactory() as session:
            covered = await get_seeder_covered_through(session, region)
            expanding = covered is None or days > covered

            page = 0
            consecutive_known = 0
            reached_edge = False
            ran_out = False

            while True:
                params = {
                    "num_results": 50,
                    "page": page,
                    "response_groups": "product_desc,contributors,series,product_attrs,media",
                    "products_sort_by": "-ReleaseDate",
                }
                data = await audible_get(region, "/1.0/catalog/products", params)
                products = data.get("products", [])
                if not products:
                    ran_out = True
                    break

                # In-window, released ASINs on this page.
                page_asins: list[str] = []
                for product in products:
                    if not product.get("title"):
                        continue
                    if product.get("publication_datetime") == _UNRELEASED_PLACEHOLDER:
                        continue
                    asin = product.get("asin")
                    if not asin:
                        continue
                    dt = _product_release_dt(product)
                    if dt is None:
                        continue
                    if dt > now:
                        continue  # skip future pre-orders
                    if dt < window_start:
                        reached_edge = True
                        break
                    page_asins.append(asin)

                for asin in page_asins:
                    if asin not in all_asins:
                        all_asins.append(asin)

                stats["pages_scanned"] += 1

                # Per-page caught-up check (only acted on when not expanding).
                if page_asins:
                    missing_here = await _get_missing_asins(session, page_asins)
                    if not missing_here:
                        consecutive_known += 1
                    else:
                        consecutive_known = 0

                if reached_edge:
                    break
                if len(products) < 50:
                    ran_out = True
                    break
                if consecutive_known >= 2 and not expanding:
                    break

                page += 1
                await asyncio.sleep(delay)
                await asyncio.sleep(0)

            # Persist the books we don't already have.
            missing = await _get_missing_asins(session, all_asins) if all_asins else []
            if missing:
                await _fetch_and_persist(missing, region, delay)
                stats["books_discovered"] = len(missing)

            # Clean completion stamps coverage; partial/failed runs do not.
            if reached_edge or ran_out:
                await upsert_seeder_covered_through(session, region, days)
                await session.commit()

        total_found = len(all_asins)
        new_books = len(missing) if missing else 0
        logger.info(
            f"Seeder: new releases scan complete for {region} — "
            f"{total_found} found, {new_books} new, {stats['pages_scanned']} pages scanned",
            extra={
                "total_found": total_found,
                "new_books": new_books,
                "pages_scanned": stats["pages_scanned"],
                "window_days": days,
                "expanding": expanding,
            },
        )

    except Exception as e:
        stats["errors"] += 1
        logger.warning(f"Seeder: new releases scan failed for {region}: {e}")

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
            "window_days": settings.seeder_new_releases_days,
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