"""
Audible listing probe.
Asks Audible whether one ASIN is listed in one region, and nothing else.

The probe classifies Audible's own answer: no database row, cache entry or
background persist is read or written, and it never goes through
get_book_by_asin / get_books_by_asins, whose fallbacks would answer from
stored data and hide a delisting. A caller deciding whether a book is gone
needs exactly the answer Audible gave, or an error when it gave none.

audible_get is looked up in this module's namespace on every call, so a
stand-in assigned over it is the one that gets called.
"""

# Standard library
from typing import Any, Literal

# Internal
# Core
from libex_core.audible.books import fetch_products, is_placeholder_record
from libex_core.audible.client import as_audible_failure
from libex_core.exceptions import (
    AudibleAPIException,
    NotFoundException,
    RegionException,
)
from libex_core.log_safety import safe_asin_for_log

# Application
from app.core.logging import get_logger
from app.services.audible import audible_get

logger = get_logger()

Listing = Literal["live", "absent", "placeholder"]


async def probe_listing(asin: str, region: str) -> Listing:
    """Classifies one ASIN's listing in one region from Audible's answer.

    "live" is a titled product that is not a placeholder. "absent" is a 404
    from Audible or a titleless stub, which is how Audible answers for an
    ASIN it has no record of. "placeholder" is a titled record carrying the
    unreleased-placeholder date.

    Anything that is not an answer raises AudibleAPIException (timeout,
    transport error, 5xx, 429, a malformed or unexpected body), so a caller
    never reads an outage as a delisting. A 404 is terminal and is never
    retried. ValueError and RegionException for a malformed ASIN or an
    unknown region come from fetch_products before anything is sent and
    propagate unchanged.
    """
    try:
        products = await fetch_products(audible_get, [asin], region)
    except NotFoundException:
        result: Listing = "absent"
    except (AudibleAPIException, RegionException, ValueError):
        raise
    except Exception as e:
        raise as_audible_failure(e, "Audible listing probe failed") from e
    else:
        result = _classify(products)

    logger.info("Probed Audible listing", extra={
        "asin": safe_asin_for_log(asin),
        "region": region,
        "listing": result,
    })
    return result


def _classify(products: list[dict[str, Any]]) -> Listing:
    """Classifies the products fetch_products returned for a single ASIN."""
    if not products:
        return "absent"
    if len(products) != 1 or not isinstance(products[0], dict):
        raise AudibleAPIException("Audible answered a product request with an unexpected shape")
    product = products[0]
    if is_placeholder_record(product):
        return "placeholder"
    return "live" if product.get("title") else "absent"
