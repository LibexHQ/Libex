"""
Search router.
Compatible with AudiMeta endpoint structure for drop-in replacement.
"""

# Standard library
from typing import Annotated

# Third party
from fastapi import APIRouter, Query, Path, Depends
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.session import get_session

# Routes
from app.api.routes.books.schemas import BookResponse, AbsBookResponse, AbsSearchResponse, AbsSeriesRef

# Services
from app.services.audible.search import search, quick_search
from app.services.audible.client import validate_region

# Core
from app.core.logging import get_logger
from app.core.middleware import valid_region
from app.core.exceptions import NotFoundException, RegionException

logger = get_logger()

router = APIRouter(tags=["Search"])

# ============================================================
# HELPERS
# ============================================================

def _to_abs_book(book: dict) -> AbsBookResponse:
    """Converts a full BookResponse dict to AbsBookResponse format."""
    authors = book.get("authors", [])
    narrators = book.get("narrators", [])
    genres = book.get("genres", [])
    series = book.get("series", [])

    return AbsBookResponse(
        asin=book.get("asin", ""),
        title=book.get("title"),
        subtitle=book.get("subtitle"),
        description=book.get("summary") or book.get("description"),
        cover=book.get("imageUrl"),
        publisher=book.get("publisher"),
        publishedYear=book.get("releaseDate", "")[:4] if book.get("releaseDate") else None,
        isbn=book.get("isbn"),
        language=book.get("language"),
        duration=str(book.get("lengthMinutes")) if book.get("lengthMinutes") else None,
        author=", ".join(a.get("name", "") for a in authors if a.get("name")) or None,
        narrator=", ".join(n.get("name", "") for n in narrators if n.get("name")) or None,
        tags=[g.get("name") for g in genres if g.get("type") == "Tags" and g.get("name")] or None,
        genres=[g.get("name") for g in genres if g.get("type") == "Genres" and g.get("name")] or None,
        series=[AbsSeriesRef(series=s.get("name"), sequence=s.get("position")) for s in series] or None,
    )


# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/search", response_model=list[BookResponse])
async def search_books(
    region: str = Depends(valid_region),
    title: Annotated[str | None, Query(description="Book title")] = None,
    author: Annotated[str | None, Query(description="Author name")] = None,
    narrator: Annotated[str | None, Query(description="Narrator name")] = None,
    publisher: Annotated[str | None, Query(description="Publisher name")] = None,
    keywords: Annotated[str | None, Query(description="Keywords")] = None,
    query: Annotated[str | None, Query(description="General query")] = None,
    products_sort_by: Annotated[str | None, Query(description="Sort order")] = None,
    limit: Annotated[int, Query(description="Maximum results", ge=1, le=50)] = 10,
    page: Annotated[int, Query(description="Page number", ge=0, le=9)] = 0,
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """Search the Audible catalog. Returns 404 if nothing found."""
    # query param maps to title if title not provided (AudiMeta behavior)
    effective_title = title or (query if not title else None)

    books = await search(
        region, session, effective_title, author, keywords,
        limit, narrator, publisher, products_sort_by, page
    )
    if not books:
        raise NotFoundException("No books found")
    return [BookResponse(**b) for b in books]


@router.get("/quick-search", response_model=list[BookResponse])
async def quick_search_books(
    keywords: Annotated[str, Query(description="Search keywords")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """Quick search using Audible suggestions. Returns 404 if nothing found."""
    books = await quick_search(keywords, region, session)
    if not books:
        raise NotFoundException("No books found")
    return [BookResponse(**b) for b in books]


@router.get("/{region}/search", response_model=AbsSearchResponse)
async def abs_search(
    region: Annotated[str, Path(description="Audible region code")],
    title: Annotated[str | None, Query(description="Book title")] = None,
    query: Annotated[str | None, Query(description="General query")] = None,
    author: Annotated[str | None, Query(description="Author name")] = None,
    keywords: Annotated[str | None, Query(description="Keywords")] = None,
    session: AsyncSession = Depends(get_session),
) -> AbsSearchResponse:
    """
    Regional search for Audiobookshelf compatibility.
    Returns {"matches": [...]} with AbsBookDto format.
    """
    try:
        validated_region = validate_region(region)
    except RegionException:
        raise NotFoundException(f"Invalid region: {region}")

    effective_title = title or query
    books = await search(validated_region, session, effective_title, author, keywords, 5)
    if not books:
        raise NotFoundException("No books found")
    return AbsSearchResponse(matches=[_to_abs_book(b) for b in books])


@router.get("/{region}/quick-search/search", response_model=AbsSearchResponse)
async def abs_quick_search(
    region: Annotated[str, Path(description="Audible region code")],
    keywords: Annotated[str | None, Query(description="Keywords")] = None,
    query: Annotated[str | None, Query(description="Query")] = None,
    title: Annotated[str | None, Query(description="Title")] = None,
    session: AsyncSession = Depends(get_session),
) -> AbsSearchResponse:
    """
    Regional quick search for Audiobookshelf compatibility.
    Returns {"matches": [...]} with AbsBookDto format.
    """
    try:
        validated_region = validate_region(region)
    except RegionException:
        raise NotFoundException(f"Invalid region: {region}")

    effective_keywords = keywords or query or title
    if not effective_keywords:
        raise NotFoundException("No search terms provided")

    books = await quick_search(effective_keywords, validated_region, session)
    if not books:
        raise NotFoundException("No books found")
    return AbsSearchResponse(matches=[_to_abs_book(b) for b in books])