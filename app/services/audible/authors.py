"""
Audible authors service.
Fetches author metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Always fetches fresh data from Audible.
Writes every result to the relational DB for persistence.
Falls back to DB when Audible is unavailable.
"""

# Standard library
import time
from datetime import datetime, timezone
from typing import Any

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.core.utils import strip_html

# Services
from app.services.audible.client import audible_get, LOCALE_MAP
from app.services.cache import manager as cache
from app.services.cache.manager import author_key, author_books_key
from app.services.db.writer import upsert_author_profile
from app.services.db.reader import get_author_from_db, get_author_books_from_db

logger = get_logger()


# ============================================================
# HELPERS
# ============================================================

def _generate_session_id() -> str:
    """
    Generates a random session ID matching AudiMeta's format.
    Format: 000-XXXXXXX-XXXXXXX
    """
    import random

    def random_digits() -> str:
        return str(random.randint(0, 9999999)).zfill(7)

    return f"000-{random_digits()}-{random_digits()}"


def _normalize_author(data: dict, asin: str, region: str) -> dict[str, Any]:
    contributor = data.get("contributor", {})
    bio = contributor.get("bio")
    return {
        "id": None,
        "asin": asin,
        "name": contributor.get("name", "").replace("\t", "").strip(),
        "description": strip_html(bio),
        "image": contributor.get("profile_image_url"),
        "region": region,
        "regions": [region],
        "genres": [],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# AUDIBLE REQUESTS
# ============================================================


async def _fetch_author_details(asin: str, region: str) -> dict[str, Any]:
    """
    Fetches author profile from Audible contributors endpoint.
    Returns bio, image, and name.
    """
    path = f"/1.0/catalog/contributors/{asin}"
    params = {
        "locale": LOCALE_MAP.get(region, "en-US"),
    }
    return await audible_get(region, path, params)


async def _fetch_author_books_by_name(name: str, region: str) -> tuple[list[str], int]:
    """
    Fetches book ASINs by author name using the standard catalog endpoint.
    Returns (asins, pages_fetched).
    """
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

    return asins, page


async def _resolve_author_name(
    asin: str,
    region: str,
    session: AsyncSession,
) -> str | None:
    """
    Resolves an author ASIN to their name.
    Checks DB first, then fetches from Audible contributors endpoint.
    """
    # Check DB first
    db_author = await get_author_from_db(session, asin, region)
    if db_author and db_author.get("name"):
        return db_author["name"]

    # Fetch from Audible
    try:
        data = await _fetch_author_details(asin, region)
        name = data.get("contributor", {}).get("name", "").replace("\t", "").strip()
        if name:
            # Persist the author profile while we have it
            normalized = _normalize_author(data, asin, region)
            await upsert_author_profile(session, normalized)
            await cache.set(session, author_key(asin, region), normalized)
            return name
    except Exception:
        pass

    return None


# ============================================================
# PUBLIC API
# ============================================================


async def get_author(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> dict[str, Any]:
    """
    Fetches author profile by ASIN.
    Audible-first, writes to DB, falls back to DB then cache.
    """
    if use_cache:
        cached = await cache.get(session, author_key(asin, region))
        if cached:
            return cached

    try:
        start = time.monotonic()
        data = await _fetch_author_details(asin, region)
        author_took = round((time.monotonic() - start) * 1000, 2)

        if not data or data.get("contributor", {}).get("name") is None:
            raise NotFoundException(f"Author not found: {asin}")

        normalized = _normalize_author(data, asin, region)

        # Write to DB and cache
        await upsert_author_profile(session, normalized)
        await cache.set(session, author_key(asin, region), normalized)

        logger.info("Requested Audible Author", extra={
            "author_took": author_took,
            "region": region,
        })

        return normalized

    except NotFoundException:
        raise
    except Exception:
        # Try DB first
        db_result = await get_author_from_db(session, asin, region)
        if db_result:
            return db_result

        # Fall back to cache
        cached = await cache.get(session, author_key(asin, region))
        if cached:
            return cached

        raise NotFoundException("Audible unavailable and no cached author data found")


async def get_author_books(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> list[str]:
    """
    Fetches all book ASINs for an author.

    Resolves the author ASIN to a name via the contributors endpoint,
    then searches the Audible catalog by author name.

    The previous Android screen endpoint (/1.0/screens/audible-android-author-detail/)
    was retired by Audible server-side as of April 2026. The endpoint string still
    exists in the Audible APK but returns 404 for all regions, all ASINs, regardless
    of headers. Confirmed via direct curl testing and APK decompilation.

    Falls back to local DB if Audible is unavailable.
    """
    if use_cache:
        cached = await cache.get(session, author_books_key(asin, region))
        if cached:
            return cached

    try:
        start = time.monotonic()

        # Resolve author ASIN to name
        author_name = await _resolve_author_name(asin, region, session)
        if not author_name:
            raise NotFoundException(f"Could not resolve author name for: {asin}")

        # Search catalog by author name
        asins, pages_fetched = await _fetch_author_books_by_name(author_name, region)

        author_book_took = round((time.monotonic() - start) * 1000, 2)

        if not asins:
            # Try DB fallback before giving up
            db_books = await get_author_books_from_db(session, asin, region)
            if db_books:
                return [b["asin"] for b in db_books]
            raise NotFoundException(f"No books found for author: {asin}")

        await cache.set(session, author_books_key(asin, region), asins)

        logger.info("Requested Audible Author Books", extra={
            "author_name": author_name,
            "author_book_num": len(asins),
            "pages_fetched": pages_fetched,
            "author_book_took": author_book_took,
            "region": region,
        })

        return asins

    except NotFoundException:
        raise
    except Exception:
        # Try DB fallback
        db_books = await get_author_books_from_db(session, asin, region)
        if db_books:
            return [b["asin"] for b in db_books]

        # Try cache
        cached = await cache.get(session, author_books_key(asin, region))
        if cached:
            return cached

        raise NotFoundException("Audible unavailable and no cached author books found")


async def get_author_books_by_name(
    name: str,
    region: str,
    session: AsyncSession,
) -> list[str]:
    """Fetches book ASINs by author name."""
    try:
        start = time.monotonic()
        asins, pages_fetched = await _fetch_author_books_by_name(name, region)
        author_book_took = round((time.monotonic() - start) * 1000, 2)

        if not asins:
            raise NotFoundException(f"No books found for author name: {name}")

        logger.info("Requested Audible Author Books By Name", extra={
            "author_name": name,
            "author_book_num": len(asins),
            "pages_fetched": pages_fetched,
            "author_book_took": author_book_took,
            "region": region,
        })

        return asins

    except NotFoundException:
        raise
    except Exception:
        raise NotFoundException("Failed to fetch author books by name")


async def search_authors(
    name: str,
    region: str,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Searches for authors by name using Audible search suggestions."""
    try:
        start = time.monotonic()
        path = "/1.0/searchsuggestions"
        params = {
            "keywords": name,
            "key_strokes": name,
            "site_variant": "android-mshop",
            "session_id": _generate_session_id(),
            "local_time": datetime.utcnow().isoformat(),
            "surface": "Android",
        }

        data = await audible_get(region, path, params)
        search_took = round((time.monotonic() - start) * 1000, 2)

        asins: list[str] = []
        for item in data.get("model", {}).get("items", []):
            if item.get("view", {}).get("template") == "AuthorItemV2":
                asin = item.get("model", {}).get("person_metadata", {}).get("asin")
                if asin:
                    asins.append(asin)

        logger.info("Requested Audible Author Search", extra={
            "search_took": search_took,
            "region": region,
        })

        if not asins:
            return []

        authors = []
        for asin in asins:
            try:
                author = await get_author(asin, region, session)
                authors.append(author)
            except NotFoundException:
                continue

        return authors

    except NotFoundException:
        raise
    except Exception:
        raise NotFoundException("Author search failed")