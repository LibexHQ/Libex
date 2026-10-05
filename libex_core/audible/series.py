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
import logging
from typing import Any

# Core
from libex_core.audible.client import AudibleGet, validate_region, validated_asin
from libex_core.audible.extras import build_extras
from libex_core.exceptions import NotFoundException
from libex_core.log_safety import safe_asin_for_log
from libex_core.text import is_unreadable_text, strip_html

logger = logging.getLogger("libex")

SERIES_PATH = "/1.0/catalog/products/{asin}"

SERIES_RESPONSE_GROUPS = "product_attrs, product_desc, product_extended_attrs"
SERIES_BOOKS_RESPONSE_GROUPS = "relationships"

SERIES_SEARCH_PATH = "/1.0/catalog/products"
SERIES_SEARCH_RESPONSE_GROUPS = "relationships"
SERIES_SEARCH_NUM_RESULTS = 10

# Keys of a series product that normalize_series reproduces as first-class
# fields. Everything else the three response groups return rides in
# audibleExtras, the same blob a book's unreproduced keys do.
_SERIES_CONSUMED = frozenset({"asin", "title", "publisher_summary"})


def normalize_series(product: dict, region: str) -> dict[str, Any]:
    """
    Normalizes raw Audible product data into Libex series format.

    Every key of the product beyond asin, title and publisher_summary is
    carried in audibleExtras, built the way a book's is (build_extras), with
    extrasWithheld recording anything that had to be left out of it. Both
    appear only when there is something to say, so a product that carries
    only the three consumed keys normalizes exactly as it did before.
    """
    # A summary that is not text is published as no description and rides
    # into the blob under its own key, as sent. publisher_summary is withheld
    # from the blob only because the description carries it, so a well-formed
    # response never writes that key there and no stored entry can be
    # overwritten; the description column is merged by longer_wins, which
    # leaves the stored text for an empty one.
    summary = product.get("publisher_summary")
    unreadable = is_unreadable_text(summary)
    if unreadable:
        logger.warning("Audible sent a text field that is not text", extra={
            "asin": safe_asin_for_log(product.get("asin") or ""),
            "region": region,
            "text_field": "publisher_summary",
        })
    series = {
        "asin": product.get("asin"),
        "name": product.get("title"),
        "description": None if unreadable else strip_html(summary),
        "region": region,
        "position": None,
        "updatedAt": None,
    }

    passthrough = {
        k: v for k, v in product.items()
        if k not in _SERIES_CONSUMED or (k == "publisher_summary" and unreadable)
    }
    if passthrough:
        extras, withheld = build_extras(passthrough, product.get("asin") or "", region)
        series["audibleExtras"] = extras
        if withheld:
            series["extrasWithheld"] = withheld
    return series


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

    product = data.get("product", {})
    relationships = product.get("relationships", [])

    items = sorted(
        [r for r in relationships if r.get("asin") and r.get("sort")],
        key=lambda r: float(r.get("sort", 0)),
    )

    asins = [item["asin"] for item in items]
    if not asins:
        raise NotFoundException(f"No books found for series: {asin}")
    return asins


async def fetch_series_search_asins(get: AudibleGet, name: str, region: str) -> list[str]:
    """
    Searches Audible's catalog by title, through `get`, and returns the unique
    series ASINs found on the relationships of the matching products, in the
    order they were found.

    The name is the caller's text: it goes to Audible as the title parameter
    and nowhere else -- it is not logged, and no message raised from here
    carries it. An empty list is a search with no series behind it, which is
    the caller's to treat as a miss; this does not raise NotFoundException
    for it, because the caller may have other places to look. Transient
    failures surface as AudibleAPIException from `get` and are the caller's
    to handle.

    Raises RegionException for a region that is not one of the eleven, before
    anything is sent.
    """
    region = validate_region(region)
    params = {
        "title": name,
        "response_groups": SERIES_SEARCH_RESPONSE_GROUPS,
        "num_results": SERIES_SEARCH_NUM_RESULTS,
    }
    data = await get(region, SERIES_SEARCH_PATH, params)

    seen: set[str] = set()
    asins: list[str] = []
    for product in data.get("products", []):
        for rel in product.get("relationships", []):
            if rel.get("relationship_type") == "series":
                asin = rel.get("asin")
                if asin and asin not in seen:
                    seen.add(asin)
                    asins.append(asin)
    return asins
