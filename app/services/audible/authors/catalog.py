"""
Audible author-books catalog walk, bound to the hosted client.
The walk itself lives in libex_core/audible/authors/catalog.py; this module
supplies the one thing that package leaves to its caller, the function that
makes the request.
"""

# Core
from libex_core.audible.authors.catalog import CatalogBooksResult, fetch_author_books_by_catalog

# Services
from app.services.audible import audible_get


async def _fetch_author_books_by_catalog(
    author_asin: str,
    author_name: str,
    region: str,
    deadline: float | None = None,
) -> CatalogBooksResult:
    """
    Runs the catalog walk through the hosted client. audible_get is looked up
    in this module's namespace on every call, never captured, so a stand-in
    assigned over it is the one called.
    """
    return await fetch_author_books_by_catalog(
        audible_get, author_asin, author_name, region, deadline=deadline
    )
