"""
Binding a normalized book or series for the statements in `statements`: every
coercion a values clause would otherwise do in SQL happens here, in Python.
"""

# Standard library
from datetime import datetime

# Local
from libex_core.storage.write.support import (
    asserted_bool,
    parse_publication_datetime,
    parse_release_date,
)


def series_params(
    series: dict, now: datetime, default_region: str | None = None
) -> dict | None:
    """
    Binds one series for the series upsert, or None when it carries too little
    to write -- the same asin-and-name guard the series writer has always
    applied.

    A series is identified by (asin, region). One that arrives through a
    book's relationships and names no region of its own belongs to the book's
    marketplace, which the caller passes as default_region; a series profile
    names its own. With no region from either, None: the row cannot be keyed.
    """
    asin = series.get("asin")
    name = series.get("name") or series.get("title")
    if not asin or not name:
        return None

    region = series.get("region") or default_region
    if not region:
        # No marketplace to file it under, and a series row's region is part of
        # its key. Refused rather than guessed: a standalone series write has
        # no book to take one from.
        return None

    description = series.get("description")
    return {
        "asin": asin,
        "title": name,
        "description": description,
        "region": region,
        "fetched_description": bool(description),
        "audible_extras": series.get("audibleExtras"),
        "extras_withheld": series.get("extrasWithheld"),
        "created_at": now,
        "updated_at": now,
    }


def book_params(data: dict, now: datetime) -> dict:
    """
    Binds one book for the book upsert.

    Every coercion the values clause used to perform in SQL happens here
    instead, because a bind that reaches a NOT NULL column as None is not a
    quiet fallback but an aborted statement -- and with a whole chunk sharing
    one execution, one such row costs all fifty their transaction. The six
    NOT NULL booleans -- is_listenable, is_buyable, is_vvab, explicit,
    whisper_sync, has_pdf -- are the deliberate exception: their None is
    answered by a coalesce on both sides of the statement and never reaches
    the column.
    """
    return {
        "asin": data["asin"],
        # Bound as received, '' included. The update merges it through
        # answered, which reads a blank title as no answer at all; the
        # normalizer drops titleless products upstream, but that filter
        # lives in another module and the merge does not lean on it.
        "title": data.get("title"),
        "subtitle": data.get("subtitle"),
        "region": data.get("region"),
        "description": data.get("description"),
        "summary": data.get("summary"),
        "publisher": data.get("publisher"),
        "copyright": data.get("copyright"),
        "isbn": data.get("isbn"),
        "language": data.get("language"),
        "rating": data.get("rating"),
        "release_date": parse_release_date(data.get("releaseDate")),
        "length_minutes": data.get("lengthMinutes"),
        "explicit": asserted_bool(data.get("explicit")),
        "whisper_sync": asserted_bool(data.get("whisperSync")),
        "has_pdf": asserted_bool(data.get("hasPdf")),
        "image": data.get("imageUrl"),
        "book_format": data.get("bookFormat"),
        "content_type": data.get("contentType"),
        "content_delivery_type": data.get("contentDeliveryType"),
        "episode_number": data.get("episodeNumber"),
        "episode_type": data.get("episodeType"),
        "sku": data.get("sku"),
        "sku_group": data.get("skuGroup"),
        "is_listenable": asserted_bool(data.get("isListenable")),
        "is_buyable": asserted_bool(data.get("isBuyable")),
        "is_vvab": asserted_bool(data.get("isVvab")),
        "plans": data.get("plans"),
        "num_ratings": data.get("numRatings"),
        "num_reviews": data.get("numReviews"),
        "publication_name": data.get("publicationName"),
        "publication_datetime": parse_publication_datetime(
            data.get("publicationDatetime"), data.get("asin")
        ),
        "extended_product_description": data.get("extendedProductDescription"),
        "product_state": data.get("productState"),
        "audible_extras": data.get("audibleExtras"),
        # Absent from the normalized dict altogether when nothing was
        # withheld, rather than present and empty, so None is the ordinary
        # case here rather than a sign anything went wrong.
        "extras_withheld": data.get("extrasWithheld"),
        "created_at": now,
        "updated_at": now,
    }
