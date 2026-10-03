"""
Turning stored rows into the response dicts a caller sees.

The dict shape is what `libex_core.audible.books.normalize_product` produces,
so a book read back from the store looks the same as one fetched live.
"""

# Standard library
from datetime import datetime, timezone
from typing import Any

# Third party
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# Local
from libex_core.audible.client import REGION_MAP
from libex_core.storage.models import Book, book_series

# The relationships every book dict reads, loaded with the book so building the
# dict never lazy-loads.
BOOK_RELATIONS = (
    selectinload(Book.authors),
    selectinload(Book.narrators),
    selectinload(Book.genres),
    selectinload(Book.series),
)


def audible_link(asin: str, region: str) -> str:
    tld = REGION_MAP.get(region, ".com")
    return f"https://audible{tld}/pd/{asin}"


def utc_z(value: datetime | None) -> str | None:
    """
    Renders a stored timestamp the way Audible sent it: UTC, ISO 8601, with a
    literal trailing Z.

    isoformat() on its own writes the offset as +00:00. That is the same
    instant and a different string, and the difference matters for this one
    field: on the live path publicationDatetime is passed through from
    Audible untouched, so a caller comparing the two surfaces is comparing
    bytes. Every value observed carries seconds resolution and no sub-second
    component, so this reproduces exactly what Audible sent.

    A value out of a timestamptz column always carries a zone. One built by
    hand does not, and is read as UTC rather than as the host's local zone,
    which is what astimezone would otherwise assume.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def series_positions(
    session: AsyncSession, book_asin: str, *, region: str | None = None
) -> dict[str, str | None]:
    """Returns {series_asin: position} for a book.

    Positions belong to the book's record in one marketplace, so the readers
    pass its region. Without one, the links of every region's record of the
    ASIN are read together, which only a caller with no record in hand wants.
    """
    stmt = select(book_series.c.series_asin, book_series.c.position).where(
        book_series.c.book_asin == book_asin
    )
    if region is not None:
        stmt = stmt.where(book_series.c.book_region == region)
    result = await session.execute(stmt)
    return {row[0]: row[1] for row in result.fetchall()}


async def series_positions_by_book(
    session: AsyncSession, books
) -> dict[tuple[str, str], dict[str, str | None]]:
    """Returns {(asin, region): {series_asin: position}} for many stored books.

    One round trip per 5000 ASINs, and a link is attributed only to the
    record of the region it was written for, so two regions' positions for
    one ASIN stay apart.
    """
    positions: dict[tuple[str, str], dict[str, str | None]] = {}
    asins = sorted({book.asin for book in books})
    for i in range(0, len(asins), 5000):
        result = await session.execute(
            select(
                book_series.c.book_asin,
                book_series.c.book_region,
                book_series.c.series_asin,
                book_series.c.position,
            )
            .where(book_series.c.book_asin.in_(asins[i:i + 5000]))
        )
        for book_asin, book_region, series_asin, position in result.fetchall():
            positions.setdefault((book_asin, book_region), {})[series_asin] = position
    return positions


def book_to_dict(book: Book, series_positions: dict[str, str | None]) -> dict[str, Any]:
    """Converts a Book ORM object to the dict shape that
    libex_core.audible.books.normalize_product produces."""
    release_date = None
    if book.release_date:
        try:
            release_date = book.release_date.isoformat()
        except Exception:
            pass

    authors = [
        {
            "id": a.id,
            "asin": a.asin,
            "name": a.name,
            "region": a.region,
            "regions": [a.region],
            "image": a.image,
            "updatedAt": a.updated_at.isoformat() if a.updated_at else None,
        }
        for a in (book.authors or [])
    ]

    narrators = [
        {
            "name": n.name,
            "updatedAt": n.updated_at.isoformat() if n.updated_at else None,
        }
        for n in (book.narrators or [])
    ]

    genres = [
        {
            "asin": g.asin,
            "name": g.name,
            "type": g.type,
            "betterType": g.type.lower().rstrip("s"),
            "updatedAt": g.updated_at.isoformat() if g.updated_at else None,
        }
        for g in (book.genres or [])
    ]

    series = [
        {
            "asin": s.asin,
            "name": s.title,
            "position": series_positions.get(s.asin),
            "region": s.region,
            "updatedAt": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in (book.series or [])
    ]

    content_type = book.content_type
    is_podcast = content_type and content_type.lower() == "podcast"

    result: dict[str, Any] = {
        "asin": book.asin,
        "title": book.title,
        "subtitle": book.subtitle,
        "description": book.description,
        "summary": book.summary,
        "region": book.region,
        "regions": [book.region],
        "publisher": book.publisher,
        "copyright": book.copyright,
        "isbn": book.isbn,
        "language": book.language,
        "rating": book.rating,
        "bookFormat": book.book_format,
        "releaseDate": release_date,
        "explicit": book.explicit,
        "hasPdf": book.has_pdf,
        "whisperSync": book.whisper_sync,
        "imageUrl": book.image,
        "lengthMinutes": book.length_minutes,
        "link": audible_link(book.asin, book.region),
        "contentType": content_type,
        "contentDeliveryType": book.content_delivery_type,
        "episodeNumber": book.episode_number if is_podcast else None,
        "episodeType": book.episode_type if is_podcast else None,
        "sku": book.sku,
        "skuGroup": book.sku_group,
        "isListenable": book.is_listenable,
        "isAvailable": book.is_buyable,
        "isBuyable": book.is_buyable,
        "isVvab": book.is_vvab,
        # The column is nullable, but the response contract is a list and always
        # has been. A stored NULL reaching the model raises ResponseValidationError,
        # which surfaces as a dropped connection rather than a 5xx.
        "plans": book.plans or [],
        "numRatings": book.num_ratings,
        "numReviews": book.num_reviews,
        "publicationName": book.publication_name,
        "publicationDatetime": utc_z(book.publication_datetime),
        "extendedProductDescription": book.extended_product_description,
        "productState": book.product_state,
        # Emitted as stored, NULL included, and unlike plans above it is not
        # coalesced to an empty container. The two columns look alike and the
        # contracts are opposite. plans is a list on the wire with no null
        # variant, so a stored NULL has to become []. audibleExtras is
        # tri-state on the wire as well as in the column: the live path emits
        # null when the blob was dropped whole and {} when Audible genuinely
        # sent nothing extra, so null is a value this field is already
        # defined to carry. Substituting {} here would assert that Audible
        # was asked and answered empty for every row no response has written
        # since the column was added.
        "audibleExtras": book.audible_extras,
        "updatedAt": book.updated_at.isoformat() if book.updated_at else None,
        "authors": authors,
        "narrators": narrators,
        "genres": genres,
        "series": series,
    }

    # Present only when the stored record holds something, matching the live
    # path, which omits the key rather than sending an empty record. Omitted
    # from this dict, not from the response -- BookResponse declares the
    # field and supplies it as null when it is absent here, so the wire
    # carries it either way. Silence here is the absence of any withholding
    # ever recorded against the row, so a row written before the column
    # existed reads the same as one whose every fetch came through complete.
    #
    # What it carries when present is an accumulation rather than a snapshot:
    # per kind of withholding, the record left by the most recent fetch that
    # withheld that kind, over the same span of fetches audibleExtras above
    # it covers. That is what lets the two be read together, and it is also
    # why neither of them answers "what is missing from the blob right now".
    # The extras_withheld merge in libex_core.storage.write.statements sets out
    # why the column has that shape.
    if book.extras_withheld:
        result["extrasWithheld"] = book.extras_withheld
    return result


def narrator_to_dict(n) -> dict[str, Any]:
    """Converts a Narrator model to a response dict with attribution."""
    result = {
        "name": n.name,
        "description": n.description,
        "image": n.image,
        "website": n.website,
        "wikipediaUrl": n.wikipedia_url,
        "languages": n.languages,
        "accents": n.accents,
        "gender": n.gender,
        "genresNarrated": n.genres_narrated,
        "audiobooksProduced": n.audiobooks_produced,
        "culturalHeritage": n.cultural_heritage,
        "publishers": n.publishers,
        "socialLinks": n.social_links,
        "audioSamples": n.audio_samples,
        "source": n.source,
        "sourceUrl": n.source_url,
        "sourceUpdatedAt": n.source_updated_at.isoformat() if n.source_updated_at else None,
        "attribution": None,
        "updatedAt": n.updated_at.isoformat() if n.updated_at else None,
    }
    if n.source and n.source_updated_at:
        date_str = n.source_updated_at.strftime("%B %Y")
        result["attribution"] = f"Profile data provided by {n.source}, retrieved {date_str}"
    return result


async def hydrate_books(session: AsyncSession, books) -> list[dict[str, Any]]:
    """Builds the dict for each book, one series-position lookup per book."""
    results = []
    for book in books:
        positions = await series_positions(session, book.asin, region=book.region)
        results.append(book_to_dict(book, positions))
    return results
