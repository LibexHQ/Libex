"""
Searching Audible: the catalog search, and the search-suggestions lookup
that backs a quick search.

build_search_params turns a caller's filters into the paging and filter
parameters Audible's catalog search takes, and checks the two numbers that
bound it. fetch_search_products sends them and returns the matching products
raw. fetch_suggestion_asins asks the suggestions endpoint what a partial
query resolves to and returns the book ASINs it names.

Nothing here validates what a caller typed. Search text goes to Audible
as given, and no message raised from this module repeats any of it.
"""

# Standard library
import random
from datetime import datetime, timezone
from typing import Any

# Core
from libex_core.audible.books import BOOK_RESPONSE_GROUPS, IMAGE_SIZES
from libex_core.audible.client import AudibleGet, validate_region

# Not CATALOG_PRODUCTS_PATH: that one has no trailing slash and is the path
# for the ASIN lookup. The catalog search is requested with the slash.
SEARCH_PATH = "/1.0/catalog/products/"
SEARCH_SUGGESTIONS_PATH = "/1.0/searchsuggestions"

# Audible's cap on one page of catalog search results. Audible also stops
# returning results past page 9; that is its behaviour, not enforced here.
MAX_SEARCH_RESULTS = 50


def _generate_session_id() -> str:
    """
    Generates a random session ID matching AudiMeta's format.
    Format: 000-XXXXXXX-XXXXXXX
    """
    def random_digits() -> str:
        return str(random.randint(0, 9999999)).zfill(7)
    return f"000-{random_digits()}-{random_digits()}"


def build_search_params(
    *,
    title: str | None = None,
    author: str | None = None,
    keywords: str | None = None,
    narrator: str | None = None,
    publisher: str | None = None,
    products_sort_by: str | None = None,
    limit: int = 10,
    page: int = 0,
) -> dict[str, Any]:
    """
    Builds the filter and paging parameters of a catalog search.

    Only the filters that were given are included; an empty string counts as
    not given. No filter at all is allowed, and so is empty keywords -- what
    Audible makes of them is Audible's answer to give. The text is never
    inspected.

    Raises ValueError if limit is outside 1..MAX_SEARCH_RESULTS or page is
    negative. The message names the bounds only, never a search value.
    """
    if not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
    if page < 0:
        raise ValueError("page must not be negative")

    params: dict[str, Any] = {"num_results": limit, "page": page}

    if title:
        params["title"] = title
    if author:
        params["author"] = author
    if keywords:
        params["keywords"] = keywords
    if narrator:
        params["narrator"] = narrator
    if publisher:
        params["publisher"] = publisher
    if products_sort_by:
        params["products_sort_by"] = products_sort_by

    return params


async def fetch_search_products(
    get: AudibleGet, region: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Runs a catalog search in one region, through `get`, and returns the
    products Audible matched, raw.

    `params` is what build_search_params returned. The response groups and
    image sizes are added here, so Audible returns full product metadata
    directly and no per-book re-fetch is needed. A NotFoundException or
    AudibleAPIException from `get` propagates as it is.
    """
    region = validate_region(region)
    request = {
        **params,
        "response_groups": BOOK_RESPONSE_GROUPS,
        "image_sizes": IMAGE_SIZES,
    }
    data = await get(region, SEARCH_PATH, request)
    return data.get("products", [])


async def fetch_suggestion_asins(
    get: AudibleGet, keywords: str, region: str
) -> list[str]:
    """
    Asks Audible's search suggestions what `keywords` resolves to in one
    region, through `get`, and returns the ASINs of the book rows, in the
    order Audible gave them.

    The ASINs are returned as Audible sent them, unvalidated. A
    NotFoundException or AudibleAPIException from `get` propagates as it is.
    """
    region = validate_region(region)
    params = {
        "keywords": keywords,
        "key_strokes": keywords,
        "site_variant": "desktop",
        "session_id": _generate_session_id(),
        "local_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "surface": "Android",
    }
    data = await get(region, SEARCH_SUGGESTIONS_PATH, params)

    asins: list[str] = []
    for item in data.get("model", {}).get("items", []):
        if item.get("view", {}).get("template") == "AsinRow":
            asin = item.get("model", {}).get("product_metadata", {}).get("asin")
            if asin:
                asins.append(asin)
    return asins
