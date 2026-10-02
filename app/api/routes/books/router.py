"""
Books router.
Endpoints for fetching book metadata by ASIN, bulk ASINs, and chapters.
Response shapes are derived from AudiMeta's; they differ in places.
"""

# Standard library
from typing import Annotated, Any

# Third party
from fastapi import APIRouter, Query, Path, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.session import get_session

# Routes
from app.api.routes.errors import ERROR_RESPONSES
from app.api.routes.audible_outage import outage_as_unavailable
from app.api.routes.cache_param import CacheStandardParam, apply_cache_control
from app.api.routes.facts_headers import FACTS_RESPONSE_HEADERS, stamp_facts_headers
from app.api.routes.large_response import build_large_list_response
from app.api.routes.sort_params import BookSortField, SortOrder
from app.api.routes.filter_params import LiveBookFilters

# Services
from app.services.audible.books import get_book_by_asin, get_books_by_asins, get_chapters
from app.services.db.reader import get_books_by_sku_from_db
from app.services.sorting import sort_dicts, BOOK_SORT_FIELDS
from app.services.filtering import filter_dicts

# Core
from libex_core.exceptions import ErrorCode, NotFoundException
from libex_core.asin import is_valid_asin, normalise_asin
from libex_core.models import BookResponse, BulkBookResponse, ChapterResponse
from app.core.middleware import valid_asin, valid_region
from app.core.response_headers import ResponseFacts

router = APIRouter(prefix="/book", tags=["Books"])

# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/sku/{sku}", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def get_books_by_sku(
    sku: Annotated[str, Path(description="Audible SKU group")],
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """
    Get all books by SKU group.
    Queries the local database only — returns books that have been fetched and stored previously.
    A SKU group typically contains region variants of the same title.
    """
    books = await get_books_by_sku_from_db(session, sku)
    if not books:
        raise NotFoundException(f"No books found for SKU: {sku}", code=ErrorCode.NOT_IN_LIBEX)
    return books

@router.get("/{asin}", response_model=BookResponse, responses={**ERROR_RESPONSES, 200: {"headers": FACTS_RESPONSE_HEADERS}})
async def get_book(
    asin: Annotated[str, Depends(valid_asin("Audible ASIN"))],
    response: Response,
    region: str = Depends(valid_region),
    cache: CacheStandardParam = True,
    session: AsyncSession = Depends(get_session),
) -> BookResponse:
    """
    Get a single book by ASIN.
    Returns a single book object directly.
    """
    facts = ResponseFacts()
    data = await outage_as_unavailable(get_book_by_asin(asin, region, session, cache, facts=facts))
    apply_cache_control(response, cache)
    stamp_facts_headers(response, facts, has_entities=True)
    return BookResponse(**data)


@router.get("/{asin}/chapters", response_model=ChapterResponse, responses={**ERROR_RESPONSES, 200: {"headers": FACTS_RESPONSE_HEADERS}})
async def get_book_chapters(
    asin: Annotated[str, Depends(valid_asin("Audible ASIN"))],
    response: Response,
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> ChapterResponse:
    """Get chapter information for a book by ASIN."""
    facts = ResponseFacts()
    data = await outage_as_unavailable(get_chapters(asin, region, session, facts=facts))
    stamp_facts_headers(response, facts, has_entities=True)
    return ChapterResponse(**data)


@router.get(
    "/chapters/{asin}",
    response_model=ChapterResponse,
    include_in_schema=False,
    responses={200: {"headers": FACTS_RESPONSE_HEADERS}},
)
async def get_book_chapters_legacy(
    asin: Annotated[str, Depends(valid_asin("Audible ASIN"))],
    response: Response,
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> ChapterResponse:
    """Legacy endpoint. Use /book/{asin}/chapters instead."""
    facts = ResponseFacts()
    data = await outage_as_unavailable(get_chapters(asin, region, session, facts=facts))
    stamp_facts_headers(response, facts, has_entities=True)
    return ChapterResponse(**data)


@router.get("", response_model=BulkBookResponse, responses={**ERROR_RESPONSES, 200: {"headers": FACTS_RESPONSE_HEADERS}})
async def get_books_bulk(
    asins: Annotated[list[str], Query(description="ASINs — comma-separated, repeated params, or both. Max 1000.")],
    response: Response,
    region: str = Depends(valid_region),
    cache: CacheStandardParam = True,
    filters: LiveBookFilters = Depends(),
    sort: Annotated[BookSortField | None, Query(description="Field to sort the returned books by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> BulkBookResponse | Response:
    """
    Get multiple books by ASIN.
    Accepts all three forms: ?asins=X,Y — ?asins=X&asins=Y — ?asins=X,Y&asins=Z
    Returns {"books": [...], "notFound": [...], "placeholderRecords": [...],
    "notFetched": [...]}; the bulk envelope is Libex's own, not a copy of
    AudiMeta's. Returns 503 with Retry-After if Audible can't be reached and
    no requested book could be served (code `upstream_unavailable`).
    """
    asin_list = [
        a.strip()
        for entry in asins
        for a in entry.split(",")
        if a.strip()
    ]

    invalid = [a for a in asin_list if not is_valid_asin(a)]
    if invalid:
        raise NotFoundException(f"Invalid ASIN format: {', '.join(invalid)}", code=ErrorCode.INVALID_REQUEST)

    # The lookup runs on the canonical form -- Audible's catalogue is
    # case-sensitive, so only that form resolves. notFound entries are kept
    # against the caller's own strings below: a missing ASIN is not a
    # product, so there is no canonical value to report it as.
    original_asin_list = asin_list
    asin_list = [normalise_asin(a) for a in asin_list]

    if not asin_list:
        raise NotFoundException("No valid ASINs provided", code=ErrorCode.INVALID_REQUEST)

    if len(asin_list) > 1000:
        raise NotFoundException("Maximum 1000 ASINs per request", code=ErrorCode.INVALID_REQUEST)

    facts = ResponseFacts()
    placeholders: list[str] = []
    unfetched: list[str] = []
    data = await outage_as_unavailable(
        get_books_by_asins(
            asin_list,
            region,
            session,
            cache,
            facts=facts,
            placeholder_asins=placeholders,
            not_fetched_asins=unfetched,
        )
    )

    # notFound holds ASINs Audible confirmed it doesn't have; notFetched holds
    # ASINs an outage kept Libex from finding out about. Both are computed
    # before filtering, so a book that was found but filtered out is not
    # reported as missing. Both sides are normalised so the caller's form and
    # the product's form are compared on equal terms.
    found_asins = {normalise_asin(book["asin"]) for book in data}
    placeholder_set = {normalise_asin(a) for a in placeholders} - found_asins
    not_fetched_set = {normalise_asin(a) for a in unfetched} - found_asins - placeholder_set
    # Found wins, then placeholder, then notFetched: books, placeholderRecords,
    # notFetched and notFound never share an ASIN.
    placeholder_records = [
        a for a in original_asin_list if normalise_asin(a) in placeholder_set
    ]
    not_fetched = [
        a for a in original_asin_list if normalise_asin(a) in not_fetched_set
    ]
    not_found = [
        a
        for a in original_asin_list
        if normalise_asin(a) not in found_asins
        and normalise_asin(a) not in placeholder_set
        and normalise_asin(a) not in not_fetched_set
    ]

    data = filter_dicts(data, filters.as_kwargs())
    data = sort_dicts(data, sort.value if sort is not None else None, order.value, BOOK_SORT_FIELDS)

    apply_cache_control(response, cache)
    stamp_facts_headers(response, facts, entities=data)

    return await build_large_list_response(
        BulkBookResponse,
        len(data),
        lambda: BulkBookResponse(
            books=[BookResponse(**book) for book in data],
            notFound=not_found,
            placeholderRecords=placeholder_records,
            notFetched=not_fetched,
        ),
        injected_response=response,
    )
