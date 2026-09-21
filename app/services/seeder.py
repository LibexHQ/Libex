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

Entities are stamped with last_seeded_at after processing, unless
_fetch_and_persist reports that the background persist queue shed a chunk
of the books it just fetched from Audible — an unstamped entity is picked
up again next cycle instead of going quiet with its books unwritten. The
seeder skips entities checked within the last 7 days, so only new and
stale entities are processed each cycle.

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
from app.services.audible.authors import fetch_author_books_by_name
from app.services.audible.books import fetch_and_store_chapters, get_books_by_asins
from app.services.db.persist_queue import PersistOutcome

logger = get_logger()
settings = get_settings()

SessionFactory = async_sessionmaker(engine, expire_on_commit=False)

SEED_STALE_DAYS = 7

# How far past its release date a book stays in the refresh rotation. Read by
# REFRESH_TIERS' post-release tiers and by _select_refresh_asins' outer bound;
# see both for why it is the only place the backward reach is written down.
POST_RELEASE_WINDOW_DAYS = 30

# Refresh cadence for books around their release date, tightest at the instant
# and loosening in both directions from it. A title's details — date, cover,
# narrator credits, runtime, availability — firm up as the date approaches and
# finish settling in the weeks just after it rather than at it: a pre-order
# carries an estimated runtime and often placeholder cover art, and the full
# narrator credit list is frequently complete only once the title is actually
# out. So the rotation runs from beyond a year out to POST_RELEASE_WINDOW_DAYS
# past release, with no step change at the release instant itself — that is
# when a title's data moves fastest, not when watching it should slow down.
#
# Each tier is (max_days_until_release, refresh_if_not_updated_in_days): a book
# is refreshed when it falls within the day range and hasn't been updated
# within the tier's staleness threshold. Ascending by max_days; the first
# matching tier wins. The post-release tiers' max_days are negative — days
# already past release, not days still to go — and the chain still meets at
# the release instant (max_days=0), so a title is watched at the same cadence
# the day after release as the day before.
#
# The tightest post-release tier is 3 days wide rather than 1: a Friday
# release still needs Monday's corrections, and a 1-day-wide window checked
# against a 24-hour cadence can land on zero passes. The post side stops at
# POST_RELEASE_WINDOW_DAYS — 30 days — because by then a title has settled,
# and an unbounded rotation across the corpus would be a corpus refresh, not
# a cadence.
REFRESH_TIERS = [
    (-14, 7),    # out 14-30 days -> every 7 days
    (-3,  3),    # out 3-14 days  -> every 3 days
    (0,   1),    # out 0-3 days   -> daily
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


async def _gather_chapters(asins: list[str], region: str, delay: float) -> None:
    """
    Fetches and stores chapters for the books in this batch whose chapters are
    not already settled.

    Best-effort and paced by the seeder delay: each book is a separate Audible
    call on the live IP, so we only touch the ones the query below admits —
    newly discovered books, whose chapters_checked_at is still null — and space
    the calls out. Anything already checked, including whatever the backfill
    covered, is left alone. fetch_and_store_chapters never raises, and the whole
    thing is wrapped, so a chapter failure can never disrupt the metadata
    persistence that is the seeder's actual job.

    A book is also eligible again once it has released, if it was checked
    before its release date. Audible answers a chapter request for audio that
    does not exist yet with a 404, and fetch_and_store_chapters marks that
    answer like any other, so a title asked about while it was still upcoming
    would otherwise be retired before it ever had chapters to find. Comparing
    the stamp against the release date re-admits any such book that appears in
    a batch handed to this function — no flag to set and nothing to un-mark.
    A book checked after it was already out stays settled forever.

    A book with no release date is settled by its first check and never
    re-admitted: the comparison is null-propagating, so it cannot re-enter on
    a date that does not exist. That is deliberate and it matches the
    behaviour every such book already had — the alternative reads a missing
    date as evidence about a release that nobody has measured. The backfill's
    _select_work spells the same rule with an explicit None check, because
    Python raises on that comparison rather than answering null.

    The `&` below is one argument to or_() on purpose. The two comparisons are
    a single conjunction — checked before the date, and the date has passed —
    so flattening them into three arguments would make "has released" an
    alternative to "never checked" rather than a qualifier on the stamp, and
    re-admit every released book on every pass.
    """
    if not asins:
        return

    now = _now()
    async with SessionFactory() as session:
        result = await session.execute(
            select(Book.asin).where(
                Book.asin.in_(asins),
                or_(
                    Book.chapters_checked_at.is_(None),
                    (Book.chapters_checked_at < Book.release_date)
                    & (Book.release_date <= now),
                ),
            )
        )
        need = [row[0] for row in result.fetchall()]

    for asin in need:
        try:
            async with SessionFactory() as session:
                await fetch_and_store_chapters(asin, region, session)
        except Exception:
            pass
        await asyncio.sleep(delay)
        await asyncio.sleep(0)


async def _fetch_and_persist(missing: list[str], region: str, delay: float) -> bool:
    """
    Fetches and persists ASINs in 50-wide chunks, paced by delay.

    Returns False if any chunk of this call did not reach storage, whether
    the background persist queue shed it or the fetch/persist call raised
    before it could report an outcome at all. A chunk Audible confirms has
    no matching records returns an empty list rather than raising, and
    costs nothing here -- there is nothing to persist. get_books_by_asins
    raises AudibleAPIException instead when a chunk's ASINs are in neither
    Audible, the DB, nor the cache -- a genuine outage, not a confirmed
    absence -- and a chunk that raised persisted nothing just as surely as
    one the queue shed. Both are treated as not-admitted so whichever
    entity discovered these books (an author, series, or narrator) is not
    stamped as seeded over books that never reached storage -- or they stay
    invisible for SEED_STALE_DAYS with nothing left pointing back at them.
    Returns True only when every chunk both completed and was admitted.
    """
    all_admitted = True
    for i in range(0, len(missing), 50):
        chunk = missing[i:i + 50]
        outcome: list[PersistOutcome] = []
        try:
            async with SessionFactory() as session:
                await get_books_by_asins(chunk, region, session, persist_outcome=outcome)
        except Exception:
            all_admitted = False
        if PersistOutcome.SHED in outcome:
            all_admitted = False
        await asyncio.sleep(delay)
        await asyncio.sleep(0)

        # Gather chapters for the newly-persisted books (paced, best-effort).
        await _gather_chapters(chunk, region, delay)

    return all_admitted


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
        logger.info("Seeder: no stale authors, skipping", extra={"region": region})
        return stats

    logger.info(
        "Seeder: expanding stale authors",
        extra={"total": total, "region": region},
    )

    for author_id, author_asin, author_name in authors:
        try:
            book_asins, _ = await fetch_author_books_by_name(author_name, region)
            await asyncio.sleep(delay)

            persisted = True
            if book_asins:
                async with SessionFactory() as session:
                    missing = await _get_missing_asins(session, book_asins)
                if missing:
                    persisted = await _fetch_and_persist(missing, region, delay)
                    stats["books_discovered"] += len(missing)
                    logger.info(
                        "Seeder: author yielded new books",
                        extra={"author_name": author_name, "new_books": len(missing)},
                    )

            if persisted:
                await _stamp_author(author_id)
                stats["authors_processed"] += 1
            else:
                # The persist queue shed at least one chunk of this author's new
                # books -- they were fetched from Audible but never reached
                # storage. Not stamping leaves the author stale, so the next
                # cycle finds it again and retries rather than the missing
                # books going unnoticed for SEED_STALE_DAYS.
                logger.warning(
                    "Seeder: not stamping author — persist queue shed a chunk "
                    "of its new books, will retry next cycle",
                    extra={"author_name": author_name},
                )

            if stats["authors_processed"] % 100 == 0:
                logger.info(
                    "Seeder: author progress",
                    extra={
                        "authors_processed": stats["authors_processed"],
                        "total": total,
                        "books_discovered": stats["books_discovered"],
                    },
                )

        except Exception as e:
            stats["errors"] += 1
            logger.warning(
                "Seeder: failed to expand author",
                extra={
                    "author_asin": author_asin,
                    "author_name": author_name,
                    "error": str(e),
                },
            )

        await asyncio.sleep(delay)

    return stats


# ============================================================
# PHASE 2: SERIES EXPANSION
# ============================================================

async def _expand_series(region: str, delay: float) -> dict[str, int]:
    stats = {"series_processed": 0, "books_discovered": 0, "errors": 0}
    cutoff = _stale_cutoff()

    from app.services.audible import audible_get

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
        logger.info("Seeder: no stale series, skipping", extra={"region": region})
        return stats

    logger.info(
        "Seeder: expanding stale series",
        extra={"total": total, "region": region},
    )

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

            persisted = True
            if book_asins:
                async with SessionFactory() as session:
                    missing = await _get_missing_asins(session, book_asins)
                if missing:
                    persisted = await _fetch_and_persist(missing, region, delay)
                    stats["books_discovered"] += len(missing)
                    logger.info(
                        "Seeder: series yielded new books",
                        extra={"series_asin": series_asin, "new_books": len(missing)},
                    )

            if persisted:
                await _stamp_series(series_asin)
                stats["series_processed"] += 1
            else:
                # See _expand_authors' identical guard: a shed chunk means these
                # books never reached storage, so the series is left stale for
                # the next cycle to retry rather than stamped over the gap.
                logger.warning(
                    "Seeder: not stamping series — persist queue shed a chunk "
                    "of its new books, will retry next cycle",
                    extra={"series_asin": series_asin},
                )

            if stats["series_processed"] % 100 == 0:
                logger.info(
                    "Seeder: series progress",
                    extra={
                        "series_processed": stats["series_processed"],
                        "total": total,
                        "books_discovered": stats["books_discovered"],
                    },
                )

        except Exception as e:
            stats["errors"] += 1
            logger.warning(
                "Seeder: failed to expand series",
                extra={"series_asin": series_asin, "error": str(e)},
            )

        await asyncio.sleep(delay)

    return stats


# ============================================================
# PHASE 3: NARRATOR EXPANSION
# ============================================================

async def _expand_narrators(region: str, delay: float) -> dict[str, int]:
    stats = {"narrators_processed": 0, "books_discovered": 0, "errors": 0}
    cutoff = _stale_cutoff()

    from app.services.audible import audible_get

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
        logger.info("Seeder: no stale narrators, skipping", extra={"region": region})
        return stats

    logger.info(
        "Seeder: expanding stale narrators",
        extra={"total": total, "region": region},
    )

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

            persisted = True
            if book_asins:
                async with SessionFactory() as session:
                    missing = await _get_missing_asins(session, book_asins)
                if missing:
                    persisted = await _fetch_and_persist(missing, region, delay)
                    stats["books_discovered"] += len(missing)
                    logger.info(
                        "Seeder: narrator yielded new books",
                        extra={"narrator_name": narrator_name, "new_books": len(missing)},
                    )

            if persisted:
                await _stamp_narrator(narrator_name)
                stats["narrators_processed"] += 1
            else:
                # See _expand_authors' identical guard: a shed chunk means these
                # books never reached storage, so the narrator is left stale
                # for the next cycle to retry rather than stamped over the gap.
                logger.warning(
                    "Seeder: not stamping narrator — persist queue shed a "
                    "chunk of its new books, will retry next cycle",
                    extra={"narrator_name": narrator_name},
                )

            if stats["narrators_processed"] % 100 == 0:
                logger.info(
                    "Seeder: narrator progress",
                    extra={
                        "narrators_processed": stats["narrators_processed"],
                        "total": total,
                        "books_discovered": stats["books_discovered"],
                    },
                )

        except Exception as e:
            stats["errors"] += 1
            logger.warning(
                "Seeder: failed to expand narrator",
                extra={"narrator_name": narrator_name, "error": str(e)},
            )

        await asyncio.sleep(delay)

    return stats


# ============================================================
# PHASE 4: NEW RELEASES
# ============================================================

async def _fetch_catalog_genres(region: str) -> list[dict[str, str]]:
    """
    Fetches the genre taxonomy from Audible and flattens every node at every level
    to a list, each tagged with its parent_id. The seeder's own copy: it shares
    nothing with the live release endpoints and never touches the catalog_genres
    table.

    The taxonomy is a tree up to five levels deep and ragged — some branches stop
    at two levels, some go five. Every catalog/products query caps at ~535 results
    and a node is not a superset of its children (each level deeper surfaces titles
    the level above misses), so walking every node at every level and unioning is
    what reaches the full catalog. The flatten recurses to whatever depth Audible
    returns (requested via categories_num_levels). A top-level parent gets
    parent_id="" ; every other node gets its parent's id. A node that appears under
    two parents yields one row per parent. Deduped by (genre_id, parent_id).
    """
    from app.services.audible import audible_get

    data = await audible_get(
        region,
        "/1.0/catalog/categories",
        {"root": "Genres", "categories_num_levels": 5},
    )
    seen: set[tuple[str, str]] = set()
    nodes: list[dict[str, str]] = []

    def emit(node_list: list[dict], parent_id: str) -> None:
        for n in node_list:
            nid = n.get("id")
            name = n.get("name")
            if nid and name and (nid, parent_id) not in seen:
                seen.add((nid, parent_id))
                nodes.append({"genre_id": nid, "name": name, "parent_id": parent_id})
            if nid:
                emit(n.get("children", []), nid)

    emit(data.get("categories", []), "")
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
    from app.services.audible import audible_get

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
    (_refresh_release_window) then keeps titles current on both sides of their
    release date.

    Audible caps every catalog/products query at ~535 results and a parent query
    is not a superset of its children, so we walk parents plus leaves and union
    the results, deduped by ASIN.

    Resilience: each node is walked independently. A failure on one node (a
    transient Audible error) is logged and skipped — it increments the error
    count but does NOT abort the scan, so the rest of the walk and the books
    already collected are preserved.

    _fetch_and_persist's admission signal is not checked here, unlike the
    expansion phases: there is no entity to withhold a stamp from, and a shed
    chunk's ASINs are simply found again — every genre is re-walked from
    scratch each cycle, with nothing recording that a given ASIN was already
    seen.
    """
    stats = {"books_discovered": 0, "pages_scanned": 0, "errors": 0}

    try:
        genres = await _fetch_catalog_genres(region)
    except Exception as e:
        stats["errors"] += 1
        logger.warning(
            "Seeder: new releases scan failed",
            extra={"region": region, "error": str(e)},
        )
        return stats

    all_asins: list[str] = []
    seen: set[str] = set()

    for genre in genres:
        try:
            found, pages = await _walk_genre_for_asins(region, genre, delay)
        except Exception as e:
            stats["errors"] += 1
            logger.warning(
                "Seeder: new releases genre walk failed",
                extra={
                    "region": region,
                    "genre_id": genre.get("genre_id"),
                    "genre_name": genre.get("name"),
                    "error": str(e),
                },
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
        logger.warning(
            "Seeder: new releases persist failed",
            extra={"region": region, "error": str(e)},
        )

    logger.info(
        "Seeder: new releases scan complete",
        extra={
            "region": region,
            "total_found": len(all_asins),
            "new_books": stats["books_discovered"],
            "pages_scanned": stats["pages_scanned"],
            "genres": len(genres),
            "errors": stats["errors"],
        },
    )

    return stats


# ============================================================
# PHASE 5: REFRESH RELEASE WINDOW
# ============================================================

async def _select_refresh_asins(
    session: AsyncSession, region: str, now: datetime
) -> list[str]:
    """
    Returns ASINs of books due for a refresh under the graduated cadence in
    REFRESH_TIERS, ordered oldest-first (by updated_at). Pure selection — no
    fetching — so the tier and staleness logic can be tested against a real
    database.

    Every tier's lower bound is the previous tier's upper bound, so seeding
    the first tier's lower bound at window_floor rather than at now is what
    opens the leading tier's window POST_RELEASE_WINDOW_DAYS behind the
    release instant; the chain still meets at now for every tier after it.
    The outer release_date bound is fed from that same window_floor rather
    than a second computation, so the seed and the filter cannot disagree.
    Were they ever to diverge, the outer WHERE would filter out rows the
    post-release tiers select while the tiers kept sitting there looking
    correct, matching nothing.

    max_days is compared against None, not truth-tested: the (0, 1) tier's 0
    is a real upper bound, the release instant, not an absent one.

    The tiers whose max_days is zero or negative — release_date already
    behind now — additionally require Book.created_at < Book.release_date.
    Those tiers exist to correct pre-release data (estimated runtime,
    placeholder art, an incomplete narrator list) that only settles once a
    title is actually out, and a book only carries that kind of data if
    Libex had it on record before it released. A book first discovered after
    its release date was fetched fresh, with the same settled data this
    window exists to chase toward — there is nothing left in it for the
    window to correct. The forward tiers carry no such gate: a book that has
    not yet released is pre-release data by construction, regardless of when
    Libex found it.
    """
    window_floor = now - timedelta(days=POST_RELEASE_WINDOW_DAYS)
    tier_conditions = []
    lower = window_floor
    for max_days, stale_days in REFRESH_TIERS:
        stale_cutoff = now - timedelta(days=stale_days)
        if max_days is None:
            window = Book.release_date > lower
        else:
            upper = now + timedelta(days=max_days)
            window = (Book.release_date > lower) & (Book.release_date <= upper)
            lower = upper
        condition = window & (Book.updated_at < stale_cutoff)
        if max_days is not None and max_days <= 0:
            condition = condition & (Book.created_at < Book.release_date)
        tier_conditions.append(condition)

    result = await session.execute(
        select(Book.asin)
        .where(
            Book.region == region,
            Book.release_date.isnot(None),
            Book.release_date > window_floor,
            or_(*tier_conditions),
        )
        .order_by(Book.updated_at.asc())
    )
    return [row[0] for row in result.fetchall()]


async def _refresh_release_window(region: str, delay: float) -> dict[str, int]:
    """
    Re-fetches books whose details may still be moving — everything from
    beyond a year out to POST_RELEASE_WINDOW_DAYS past release — on the
    graduated cadence in REFRESH_TIERS: the closer a book is to its release
    date, in either direction, the shorter the staleness threshold before it's
    refreshed.

    Runs only when settings.seeder_refresh_enabled is true (it defaults to
    false), as one step of run_new_releases_seeder's cycle — on
    seeder_new_releases_interval_hours, not the main expansion loop's interval.

    Books are processed oldest-first (by updated_at) so the most stale get
    priority, and refreshing a book updates its updated_at — which drops it out
    of the next cycle's selection until it ages back past its tier threshold.

    Carrying the window past release also revives a book's chapters, which is
    a consequence of the metadata window rather than the reason for it. A
    title asked for chapters while it was still a pre-order gets a legitimate
    404 — the audio does not exist yet — and fetch_and_store_chapters stamps
    that answer like any other. _gather_chapters re-admits a book whose stamp
    predates its release_date once that date has passed, and this window is
    the only path that ever hands it such a stored, released book, so a title
    that passes through it recovers its chapters on the cycle after it comes
    out. Only for the regions in SEEDER_REGIONS, which defaults to us alone.

    That re-admission is a one-shot credit rather than a standing retry:
    _gather_chapters and scripts/backfill_chapters.py::_select_work both
    permanently retire a book once chapters_checked_at moves past
    release_date, and fetch_and_store_chapters stamps chapters_checked_at on
    a 404 exactly as it does on a real answer. So how promptly this window
    reaches a title after release is not a minor timing detail — it is the
    only shot that title gets at ever carrying chapters at all.

    _fetch_and_persist's admission signal is not checked here, unlike the
    expansion phases: there is no entity to withhold a stamp from, and
    selection keys directly on Book.updated_at, which a shed chunk leaves
    unmoved — the book stays selected and gets retried next cycle.
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
            "Seeder: refreshed books in release window",
            extra={"region": region, "books_refreshed": len(asins)},
        )

    except Exception as e:
        stats["errors"] += 1
        logger.warning(
            "Seeder: refresh release window failed",
            extra={"region": region, "error": str(e)},
        )

    return stats


# ============================================================
# MAIN LOOP
# ============================================================

async def run_seeder(once: bool = False) -> None:
    """
    Runs the author/series/narrator expansion cycle on settings.seeder_interval_hours,
    forever by default.

    once, False by default, runs exactly one cycle and returns instead of looping —
    the standalone entry point's --once (scripts/seed.py) uses this for a single
    supervised pass. False preserves the original forever-loop behavior exactly.
    """
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
            logger.error("Seeder: cycle failed", extra={"error": str(e)})

        if once:
            return

        await asyncio.sleep(interval)


async def run_new_releases_seeder(once: bool = False) -> None:
    """
    Independent worker that scans new releases on its own interval.

    Runs separately from the main expansion cycle (run_seeder) so new content
    can be picked up more often than the heavier author/series/narrator walks.
    Shares the same regions and request delay; only the interval is its own.
    The two workers run independently and may occasionally overlap.

    once, False by default, runs exactly one cycle and returns instead of
    looping — see run_seeder's own once for the standalone entry point that
    uses it. False preserves the original forever-loop behavior exactly.
    """
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
                    refresh_stats = await _refresh_release_window(region, delay)
                    cycle_stats["books_refreshed"] += refresh_stats["books_refreshed"]
                    cycle_stats["errors"] += refresh_stats["errors"]

            logger.info("Seeder: new releases cycle complete", extra=cycle_stats)

        except Exception as e:
            logger.error("Seeder: new releases cycle failed", extra={"error": str(e)})

        if once:
            return

        await asyncio.sleep(interval)