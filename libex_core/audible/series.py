"""
Fetching and normalizing Audible series records, and the ordered list of a
series' book ASINs.

A series is a catalog product of its own on Audible; its members are the
relationships on that product. fetch_series returns the series product raw,
fetch_series_book_asins returns its member ASINs in series order, and
normalize_series turns a series product into the response shape Libex
serves, derived from AudiMeta's SeriesDto.
"""

# Standard library
from typing import Any

# Core
from libex_core.audible.client import AudibleGet, validate_region, validated_asin
from libex_core.exceptions import NotFoundException
from libex_core.text import strip_html

SERIES_PATH = "/1.0/catalog/products/{asin}"

SERIES_RESPONSE_GROUPS = "product_attrs, product_desc, product_extended_attrs"
SERIES_BOOKS_RESPONSE_GROUPS = "relationships"


def normalize_series(product: dict, region: str) -> dict[str, Any]:
    """Normalizes raw Audible product data into Libex series format."""
    return {
        "asin": product.get("asin"),
        "name": product.get("title"),
        "description": strip_html(product.get("publisher_summary")),
        "region": region,
        "position": None,
        "updatedAt": None,
    }


async def fetch_series(get: AudibleGet, asin: str, region: str) -> dict[str, Any]:
    """
    Fetches a series' own product from Audible, through `get`, and returns it
    as Audible sent it.

    Raises NotFoundException when Audible answers with no series behind the
    ASIN: a response that names no response groups, or only one, or carries no
    product, is Audible answering for something that is not a series record,
    and that answer is terminal. Transient failures surface as
    AudibleAPIException from `get` and are the caller's to retry.

    Raises RegionException for a region that is not one of the eleven, and
    ValueError for a value that is not an ASIN -- before anything is sent.
    """
    region = validate_region(region)
    asin = validated_asin(asin)
    path = SERIES_PATH.format(asin=asin)
    params = {
        "response_groups": SERIES_RESPONSE_GROUPS,
    }
    data = await get(region, path, params)

    if (
        not data
        or not data.get("response_groups")
        or len(data.get("response_groups", [])) == 1
    ):
        raise NotFoundException(f"Series not found: {asin}")

    product = data.get("product")
    if not product:
        raise NotFoundException(f"Series not found: {asin}")

    return product


async def fetch_series_book_asins(get: AudibleGet, asin: str, region: str) -> list[str]:
    """
    Fetches the ASINs of a series' books, sorted by position, through `get`.

    Reads the relationships response group of the series product, keeping the
    entries that carry both an ASIN and a sort position. Raises
    NotFoundException when none do. Transient failures surface as
    AudibleAPIException from `get` and are the caller's to retry.

    Raises RegionException for a region that is not one of the eleven, and
    ValueError for a value that is not an ASIN -- before anything is sent.
    """
    region = validate_region(region)
    asin = validated_asin(asin)
    path = SERIES_PATH.format(asin=asin)
    params = {
        "response_groups": SERIES_BOOKS_RESPONSE_GROUPS,
    }
    data = await get(region, path, params)

    product = data.get("product") or {}
    relationships = product.get("relationships") or []

    items = sorted(
        [r for r in relationships if r.get("asin") and r.get("sort")],
        key=lambda r: float(r.get("sort", 0)),
    )

    asins = [item["asin"] for item in items]
    if not asins:
        raise NotFoundException(f"No books found for series: {asin}")
    return asins
