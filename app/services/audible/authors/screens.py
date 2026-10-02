"""
Audible author-books screens walk, bound to the hosted client.
The walk itself lives in libex_core/audible/authors/screens.py; this module
supplies the one thing that package leaves to its caller, the function that
makes the request.
"""

# Core
from libex_core.audible.authors.screens import ScreenBooksResult, fetch_author_books_by_screen

# Services
from app.services.audible import audible_get


async def _fetch_author_books_by_screen(
    asin: str,
    region: str,
    deadline: float | None = None,
) -> ScreenBooksResult:
    """
    Runs the screens walk through the hosted client. audible_get is looked up
    in this module's namespace on every call, never captured, so a stand-in
    assigned over it is the one called.
    """
    return await fetch_author_books_by_screen(audible_get, asin, region, deadline=deadline)
