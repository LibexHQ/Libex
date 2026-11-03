"""
Strict input models for a hand-entered book.

Standalone models, not BookResponse subclasses, and extra="forbid" on every
one, nested ones included. A key is accepted only when the writer stores it,
so the accepted set and the stored set are the same set by construction: a
derived field (link, regions, betterType) is refused as an unknown key, and a
field BookResponse gains later is refused too, never silently dropped. The
per-key map below is the one list of accepted keys; the models are checked
against it at import.

These are never a response_model. manualEntry is not accepted anywhere: the
server stamps provenance after parsing.
"""

# Standard library
import json
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

# Third party
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

# Local
from libex_core.asin import is_valid_asin, normalise_asin
from libex_core.audible.client import validate_region
from libex_core.exceptions import RegionException

__all__ = [
    "BOOK_FIELDS",
    "AUTHOR_FIELDS",
    "NARRATOR_FIELDS",
    "SERIES_FIELDS",
    "GENRE_FIELDS",
    "CHAPTERS_FIELDS",
    "CHAPTER_ITEM_FIELDS",
    "IngestBody",
    "IngestBook",
    "IngestChapters",
    "error_list",
]

# Author description and image, and series description and extras, are applied by the writer
# only when it inserts the entity; a differing value for a stored one is a
# conflict, never an update. isListenable, isBuyable and isVvab default to
# False: a book entered by hand is not on sale, whatever the column defaults say.
#
# Where each accepted key lands. Scalars name the books column; relationships
# name the pivot and the entity table the write inserts or links. Position in
# every list is the credit order the pivots store.
BOOK_FIELDS: dict[str, str] = {
    "asin": "books.asin",
    "region": "books.region",
    "title": "books.title",
    "subtitle": "books.subtitle",
    "description": "books.description",
    "summary": "books.summary",
    "extendedProductDescription": "books.extended_product_description",
    "publisher": "books.publisher",
    "copyright": "books.copyright",
    "isbn": "books.isbn",
    "language": "books.language",
    "rating": "books.rating",
    "numRatings": "books.num_ratings",
    "numReviews": "books.num_reviews",
    "releaseDate": "books.release_date",
    "publicationName": "books.publication_name",
    "publicationDatetime": "books.publication_datetime",
    "lengthMinutes": "books.length_minutes",
    "explicit": "books.explicit",
    "whisperSync": "books.whisper_sync",
    "hasPdf": "books.has_pdf",
    "imageUrl": "books.image",
    "bookFormat": "books.book_format",
    "contentType": "books.content_type",
    "contentDeliveryType": "books.content_delivery_type",
    "episodeNumber": "books.episode_number",
    "episodeType": "books.episode_type",
    "sku": "books.sku",
    "skuGroup": "books.sku_group",
    "isListenable": "books.is_listenable",
    "isBuyable": "books.is_buyable",
    "isVvab": "books.is_vvab",
    "plans": "books.plans",
    "productState": "books.product_state",
    "audibleExtras": "books.audible_extras",
    "extrasWithheld": "books.extras_withheld",
    "authors": "author_book pivot, authors rows",
    "narrators": "book_narrator pivot, narrators rows",
    "series": "book_series pivot, series rows",
    "genres": "book_genre pivot, genres rows",
}
AUTHOR_FIELDS: dict[str, str] = {
    "name": "authors.name",
    "asin": "authors.asin",
    "description": "authors.description",
    "image": "authors.image",
}
NARRATOR_FIELDS: dict[str, str] = {"name": "narrators.name"}
SERIES_FIELDS: dict[str, str] = {
    "asin": "series.asin",
    "name": "series.title",
    "position": "book_series.position",
    "region": "series.region",
    "description": "series.description",
    "audibleExtras": "series.audible_extras",
    "extrasWithheld": "series.extras_withheld",
}
GENRE_FIELDS: dict[str, str] = {
    "asin": "genres.asin",
    "name": "genres.name",
    "type": "genres.type",
}
# Stored as the one tracks.chapters document, in the shape ChapterResponse serves.
CHAPTERS_FIELDS: dict[str, str] = {
    "brandIntroDurationMs": "tracks.chapters",
    "brandOutroDurationMs": "tracks.chapters",
    "isAccurate": "tracks.chapters",
    "runtimeLengthMs": "tracks.chapters",
    "runtimeLengthSec": "tracks.chapters",
    "chapters": "tracks.chapters",
    "contentReference": "tracks.chapters",
    "contentUrl": "tracks.chapters",
    "audibleExtras": "tracks.chapters",
    "extrasWithheld": "tracks.chapters",
}
CHAPTER_ITEM_FIELDS: dict[str, str] = {
    "title": "tracks.chapters",
    "lengthMs": "tracks.chapters",
    "startOffsetMs": "tracks.chapters",
    "startOffsetSec": "tracks.chapters",
    "chapters": "tracks.chapters",
    "audibleExtras": "tracks.chapters",
}

_MAX_CHAPTER_DEPTH = 4
_MAX_EXTRAS_CHARS = 65536


def _no_nul(value: str) -> str:
    # Postgres cannot store NUL in text; refused here so it is a 422, not an
    # aborted statement.
    if "\x00" in value:
        raise ValueError("NUL character")
    return value


def _text(limit: int):
    return Annotated[str, Field(max_length=limit), AfterValidator(_no_nul)]


def _name(value: str) -> str:
    if not value.strip():
        raise ValueError("blank")
    return value


Short = _text(2000)
Name = Annotated[_text(2000), AfterValidator(_name)]
LongText = _text(65536)
Title = Annotated[_text(1000), AfterValidator(_name)]
Subtitle = _text(1000)
Position = _text(100)
Count = Annotated[int, Field(ge=0, le=10**9)]
Millis = Annotated[int, Field(ge=0, le=10**10)]


def _asin(value: str) -> str:
    if not is_valid_asin(value):
        raise ValueError("invalid ASIN")
    return normalise_asin(value)


def _genre_asin(value: str) -> str:
    # Genre ids are Audible category numbers, not product ASINs.
    if not value.isascii() or not value.isalnum() or len(value) > 12:
        raise ValueError("invalid genre id")
    return value


def _region(value: str) -> str:
    try:
        return validate_region(value)
    except RegionException:
        raise ValueError("invalid region") from None


def _iso_date(value: str) -> str:
    # The writer nulls a date it cannot parse; here that is a refusal.
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("not an ISO 8601 date") from None
    return value


def _iso_instant(value: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("not an ISO 8601 instant") from None
    return value


def _image_url(value: str) -> str:
    if len(value) > 2048 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("invalid URL") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("must be an absolute https URL")
    return value


def _extras(value: dict[str, Any] | None) -> dict[str, Any] | None:
    # Verbatim documents are bounded by their serialized size. allow_nan=False
    # refuses NaN and Infinity, which JSONB cannot hold.
    if value is None:
        return None
    try:
        text = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("not storable as JSON") from None
    if len(text) > _MAX_EXTRAS_CHARS or "\\u0000" in text:
        raise ValueError("too large or holds NUL")
    return value


Asin = Annotated[str, AfterValidator(_asin)]
Extras = Annotated[dict[str, Any], AfterValidator(_extras)]
_STRICT = ConfigDict(extra="forbid", strict=True)


class IngestAuthor(BaseModel):
    model_config = _STRICT

    name: Name
    asin: Asin | None = None
    description: LongText | None = None
    image: Annotated[str, AfterValidator(_image_url)] | None = None


class IngestNarrator(BaseModel):
    model_config = _STRICT

    name: Name


class IngestSeries(BaseModel):
    model_config = _STRICT

    asin: Asin
    name: Name
    position: Position | None = None
    region: Annotated[str, AfterValidator(_region)] | None = None
    description: LongText | None = None
    audibleExtras: Extras | None = None
    extrasWithheld: Extras | None = None


class IngestGenre(BaseModel):
    model_config = _STRICT

    asin: Annotated[str, AfterValidator(_genre_asin)]
    name: Name
    type: Literal["Genres", "Tags"]


class IngestBook(BaseModel):
    model_config = _STRICT

    asin: Asin
    region: Annotated[str, AfterValidator(_region)]
    title: Title
    language: Annotated[_text(50), AfterValidator(_name)]
    releaseDate: Annotated[_text(64), AfterValidator(_iso_date)]
    subtitle: Subtitle | None = None
    description: LongText | None = None
    summary: LongText | None = None
    extendedProductDescription: LongText | None = None
    publisher: Short | None = None
    copyright: Short | None = None
    isbn: _text(16) | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    numRatings: Count | None = None
    numReviews: Count | None = None
    publicationName: Short | None = None
    publicationDatetime: Annotated[_text(64), AfterValidator(_iso_instant)] | None = None
    lengthMinutes: Annotated[int, Field(ge=0, le=10**7)] | None = None
    explicit: bool | None = None
    whisperSync: bool | None = None
    hasPdf: bool | None = None
    imageUrl: Annotated[str, AfterValidator(_image_url)] | None = None
    bookFormat: _text(50) | None = None
    contentType: _text(100) | None = None
    contentDeliveryType: _text(100) | None = None
    episodeNumber: _text(20) | None = None
    episodeType: _text(50) | None = None
    sku: _text(20) | None = None
    skuGroup: _text(20) | None = None
    isListenable: bool = False
    isBuyable: bool = False
    isVvab: bool = False
    plans: Annotated[list[Short], Field(max_length=50)] | None = None
    productState: Short | None = None
    audibleExtras: Extras | None = None
    extrasWithheld: Extras | None = None
    authors: Annotated[list[IngestAuthor], Field(min_length=1, max_length=50)]
    narrators: Annotated[list[IngestNarrator], Field(max_length=50)] = Field(default_factory=list)
    series: Annotated[list[IngestSeries], Field(max_length=50)] = Field(default_factory=list)
    genres: Annotated[list[IngestGenre], Field(max_length=200)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _series_region_matches(self):
        # A series region of its own must be the book's: the entry is filed
        # under the book's marketplace, never a sibling's.
        if any(s.region not in (None, self.region) for s in self.series):
            raise ValueError("series region differs from the book's")
        return self


class IngestChapterItem(BaseModel):
    model_config = _STRICT

    title: _text(2000)
    lengthMs: Millis
    startOffsetMs: Millis
    startOffsetSec: Millis = 0
    chapters: Annotated[list["IngestChapterItem"], Field(max_length=2000)] | None = None
    audibleExtras: Extras | None = None


def _depth(items: list[IngestChapterItem] | None) -> int:
    if not items:
        return 0
    return 1 + max(_depth(item.chapters) for item in items)


class IngestChapters(BaseModel):
    model_config = _STRICT

    runtimeLengthMs: Millis
    chapters: Annotated[list[IngestChapterItem], Field(min_length=1, max_length=2000)]
    runtimeLengthSec: Millis = 0
    brandIntroDurationMs: Millis = 0
    brandOutroDurationMs: Millis = 0
    isAccurate: bool = False
    contentReference: Extras | None = None
    contentUrl: Extras | None = None
    audibleExtras: Extras | None = None
    extrasWithheld: Extras | None = None

    @model_validator(mode="after")
    def _bounded_depth(self):
        if _depth(self.chapters) > _MAX_CHAPTER_DEPTH:
            raise ValueError("sub-chapters nested too deep")
        return self


class IngestBody(BaseModel):
    model_config = _STRICT

    book: IngestBook
    chapters: IngestChapters | None = None


def _check_field_maps() -> None:
    pairs = (
        (IngestBook, BOOK_FIELDS),
        (IngestAuthor, AUTHOR_FIELDS),
        (IngestNarrator, NARRATOR_FIELDS),
        (IngestSeries, SERIES_FIELDS),
        (IngestGenre, GENRE_FIELDS),
        (IngestChapters, CHAPTERS_FIELDS),
        (IngestChapterItem, CHAPTER_ITEM_FIELDS),
    )
    for model, fields in pairs:
        if set(model.model_fields) != set(fields):
            raise RuntimeError(f"{model.__name__} and its field map disagree")


_check_field_maps()


def error_list(exc: ValidationError) -> list[dict[str, Any]]:
    """
    The location and type of every error, and nothing else.

    A pydantic error also carries the offending input, its message and a docs
    URL, any of which can echo a submitted value back; str(exc) is the same.
    """
    return [{"loc": list(err["loc"]), "type": err["type"]} for err in exc.errors()]
