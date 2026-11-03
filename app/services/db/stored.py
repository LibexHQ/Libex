"""
Reads for the routes that answer from the stored catalog alone.

The guarded readers in app.services.db.reader turn a failed read into an empty
answer, which is right where Audible can fill the gap. A route with nothing to
fall back to must not do that: an empty answer reads as "Libex has no such
record", and a broken database is not that. These raise a 503 instead, so the
caller retries rather than concluding the record does not exist.

Each name matches its guarded counterpart, so a route chooses by the module it
imports from.
"""

# Standard library
import functools

# Core
from app.core.logging import get_logger
from app.services.db.writer import _failure_fields
from libex_core.exceptions import ErrorCode, LibexException
from libex_core.storage.read import books as _books
from libex_core.storage.read import people as _people
from libex_core.storage.read import series as _series

logger = get_logger()

__all__ = [
    "get_author_books_from_db",
    "get_author_from_db",
    "get_book_from_db",
    "get_books_by_plan_from_db",
    "get_books_by_sku_from_db",
    "get_coming_soon_from_db",
    "get_delisted_books_from_db",
    "get_distinct_genres_from_db",
    "get_distinct_plans_from_db",
    "get_narrator_books_from_db",
    "get_new_releases_from_db",
    "get_series_books_from_db",
    "get_series_from_db",
    "get_track_from_db",
    "get_vvab_books_from_db",
    "search_books_from_db",
    "search_narrators_from_db",
]


def _raising(core):
    """
    Wraps a core read so a failure is logged and raised as a 503.

    Only the failure's type and SQLSTATE are logged. Search and filter text
    arrives from the query string and never reaches a log line, and the
    exception's own text can carry row content.
    """
    @functools.wraps(core)
    async def wrapper(session, *args, **kwargs):
        try:
            return await core(session, *args, **kwargs)
        except Exception as e:
            logger.warning(
                f"DB read failed in {core.__name__}", extra=_failure_fields(e)
            )
            raise LibexException(
                "The local database could not be read right now",
                status_code=503,
                code=ErrorCode.UPSTREAM_UNAVAILABLE,
            ) from e

    return wrapper


get_book_from_db = _raising(_books.get_book)
search_books_from_db = _raising(_books.search_books)
get_books_by_sku_from_db = _raising(_books.get_books_by_sku)
get_distinct_plans_from_db = _raising(_books.distinct_plans)
get_distinct_genres_from_db = _raising(_books.distinct_genres)
get_books_by_plan_from_db = _raising(_books.get_books_by_plan)
get_vvab_books_from_db = _raising(_books.get_vvab_books)
get_delisted_books_from_db = _raising(_books.get_delisted_books)
get_new_releases_from_db = _raising(_books.get_new_releases)
get_coming_soon_from_db = _raising(_books.get_coming_soon)
get_track_from_db = _raising(_books.get_track)

get_author_from_db = _raising(_people.get_author)
get_author_books_from_db = _raising(_people.get_author_books)
search_narrators_from_db = _raising(_people.search_narrators)
get_narrator_books_from_db = _raising(_people.get_narrator_books)

get_series_from_db = _raising(_series.get_series)
get_series_books_from_db = _raising(_series.get_series_books)
