"""
Ranked search over the local database: books, authors and series.

Unlike the filters on /db/book, which are substring tests returned in sort
order, these rank what they find by how well it matches and return the best
first. The matching and ranking live in libex_core.storage.read.search; these
routes validate the input and shape the response.
"""

# Standard library
import unicodedata
from enum import Enum
from typing import Annotated, Any

# Third party
from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from app.api.routes.errors import ERROR_RESPONSES
from app.db.session import get_session
from app.services.db.stored import (
    search_authors_ranked_from_db,
    search_books_ranked_from_db,
    search_series_ranked_from_db,
)
from libex_core.audible.client import validate_region
from libex_core.exceptions import ErrorCode, NotFoundException
from libex_core.models import AuthorResponse, BookResponse, SeriesSearchResponse

search_router = APIRouter(prefix="/search")

_MIN_CHARS = 2
_MAX_CHARS = 200


class MatchMode(str, Enum):
    fuzzy = "fuzzy"
    exact = "exact"


_REGION_DESCRIPTION = (
    "Audible region code. Validated, so an unknown region is a 400. Give one to "
    "search only the records stored under it. Omit it and you get one record "
    "per book, the first one stored, and only that record's text is searched."
)
_LIMIT = Query(ge=1, le=50, description="Results to return (max 50)")
_MATCH_DESCRIPTION = (
    "fuzzy (default) tolerates typos and word order; exact needs the whole "
    "name to be equal, ignoring case and accents."
)


def _text(label: str, description: str):
    """A text query param, checked by its length once whitespace is stripped."""

    def dependency(
        value: Annotated[str | None, Query(alias=label, description=description)] = None,
    ) -> str | None:
        if value is None:
            return None
        # NFKC first, as the reader does, so a full-width space is not a character.
        text = unicodedata.normalize("NFKC", value).strip()
        if "\x00" in text:
            problem = "must not contain a NUL character"
        elif not _MIN_CHARS <= len(text) <= _MAX_CHARS:
            problem = f"must be {_MIN_CHARS} to {_MAX_CHARS} characters"
        else:
            return text
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", label), "msg": problem, "input": None}]
        )

    return Depends(dependency)


def _language(
    language: Annotated[str | None, Query(description="Book language, e.g. english")] = None,
) -> str | None:
    # Compared as given, so no stripping or normalising; only what the database would choke on.
    if language is not None and ("\x00" in language or len(language) > _MAX_CHARS):
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "language"),
                    "msg": f"must be at most {_MAX_CHARS} characters with no NUL character",
                    "input": None,
                }
            ]
        )
    return language


def _region(region: str | None) -> str | None:
    # Not "us" when omitted: no region asked for means the first-stored record.
    return validate_region(region) if region is not None else None


def _no_match(what: str) -> NotFoundException:
    return NotFoundException(f"No {what} found matching the given parameters", code=ErrorCode.NOT_IN_LIBEX)


@search_router.get("/books", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def search_db_books_ranked(
    title: str | None = _text("title", "Title, with or without the subtitle"),
    author: str | None = _text("author", "Author name"),
    narrator: str | None = _text("narrator", "Narrator name"),
    series: str | None = _text("series", "Series name"),
    region: Annotated[str | None, Query(description=_REGION_DESCRIPTION)] = None,
    language: str | None = Depends(_language),
    limit: Annotated[int, _LIMIT] = 10,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """
    Search stored books by title, author, narrator and series, best match first.

    Give at least one of title, author, narrator or series; each is 2 to 200
    characters. When more than one is given a book has to match all of them,
    and the title counts for more than the rest in the ranking. Matching
    ignores case and accents and tolerates typos. Results are full records,
    as /db/book serves them, best match first, then by number of ratings.

    With region omitted you get one record per book, the first one stored, and
    only that record's text is searched. Authors with no ASIN still count
    towards an author match. Searches of 2 Japanese characters can
    be slow. No match is a 404 NOT_IN_LIBEX.
    """
    if not any((title, author, narrator, series)):
        raise NotFoundException("No search parameters provided", code=ErrorCode.INVALID_REQUEST)
    books = await search_books_ranked_from_db(
        session,
        title=title,
        author=author,
        narrator=narrator,
        series=series,
        region=_region(region),
        language=language,
        limit=limit,
    )
    if not books:
        raise _no_match("books")
    return books


@search_router.get("/authors", response_model=list[AuthorResponse], responses=ERROR_RESPONSES)
async def search_db_authors_ranked(
    name: str | None = _text("name", "Author name (required)"),
    region: Annotated[str | None, Query(description=_REGION_DESCRIPTION)] = None,
    match: Annotated[MatchMode, Query(description=_MATCH_DESCRIPTION)] = MatchMode.fuzzy,
    limit: Annotated[int, _LIMIT] = 10,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """
    Search stored authors by name, best match first.

    The name is 2 to 200 characters. Matching ignores case and accents and is
    fuzzy by default; match=exact asks for the whole name to be equal instead.
    Results are full records, best match first. With region omitted an author
    is returned once, from the first region it was stored under.

    Authors stored without an ASIN are not listed here, since the record is
    keyed by it, but they still appear in the authors of the books they wrote.
    No match is a 404 NOT_IN_LIBEX.
    """
    if name is None:
        raise NotFoundException("No search parameters provided", code=ErrorCode.INVALID_REQUEST)
    authors = await search_authors_ranked_from_db(
        session, name, region=_region(region), exact=match is MatchMode.exact, limit=limit
    )
    if not authors:
        raise _no_match("authors")
    return authors


@search_router.get("/series", response_model=list[SeriesSearchResponse], responses=ERROR_RESPONSES)
async def search_db_series_ranked(
    name: str | None = _text("name", "Series name (required)"),
    region: Annotated[str | None, Query(description=_REGION_DESCRIPTION)] = None,
    match: Annotated[MatchMode, Query(description=_MATCH_DESCRIPTION)] = MatchMode.fuzzy,
    limit: Annotated[int, _LIMIT] = 10,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """
    Search stored series by name, best match first.

    The name is 2 to 200 characters. Matching ignores case and accents and is
    fuzzy by default; match=exact asks for the whole name to be equal instead.
    Results are full records with bookCount, the number of stored books linked
    to the series, best match first. With region omitted a series is returned
    once, from the first region it was stored under, and only that record's
    name is searched. No match is a 404 NOT_IN_LIBEX.
    """
    if name is None:
        raise NotFoundException("No search parameters provided", code=ErrorCode.INVALID_REQUEST)
    series = await search_series_ranked_from_db(
        session, name, region=_region(region), exact=match is MatchMode.exact, limit=limit
    )
    if not series:
        raise _no_match("series")
    return series
