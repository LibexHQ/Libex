"""
Books route schemas.
All field names match AudiMeta's BookDto exactly for drop-in compatibility.
"""

# Standard library

# Third party
from pydantic import BaseModel, Field


# ============================================================
# NESTED OBJECT SCHEMAS
# ============================================================

class NarratorResponse(BaseModel):
    name: str
    updatedAt: str | None = None


class GenreResponse(BaseModel):
    asin: str | None = None
    name: str | None = None
    type: str | None = None
    betterType: str | None = None
    updatedAt: str | None = None


class SeriesRefResponse(BaseModel):
    asin: str | None = None
    name: str | None = None
    region: str | None = None
    position: str | None = None
    updatedAt: str | None = None


class AuthorRefResponse(BaseModel):
    id: int | None = None
    asin: str | None = None
    name: str | None = None
    region: str | None = None
    regions: list[str] = Field(default_factory=list)
    image: str | None = None
    updatedAt: str | None = None


# ============================================================
# BOOK RESPONSE
# ============================================================

class BookResponse(BaseModel):
    asin: str
    title: str | None = None
    subtitle: str | None = None
    description: str | None = None
    summary: str | None = None
    region: str
    regions: list[str] = Field(default_factory=list)
    publisher: str | None = None
    copyright: str | None = None
    isbn: str | None = None
    language: str | None = None
    rating: float | None = None
    bookFormat: str | None = None
    releaseDate: str | None = None
    explicit: bool = False
    hasPdf: bool = False
    whisperSync: bool = False
    imageUrl: str | None = None
    lengthMinutes: int | None = None
    link: str | None = None
    contentType: str | None = None
    contentDeliveryType: str | None = None
    episodeNumber: str | None = None
    episodeType: str | None = None
    sku: str | None = None
    skuGroup: str | None = None
    isListenable: bool = False
    isAvailable: bool = False
    isBuyable: bool = False
    isVvab: bool = False
    updatedAt: str | None = None
    authors: list[AuthorRefResponse] = Field(default_factory=list)
    narrators: list[NarratorResponse] = Field(default_factory=list)
    genres: list[GenreResponse] = Field(default_factory=list)
    series: list[SeriesRefResponse] = Field(default_factory=list)


# ============================================================
# BULK BOOK RESPONSE
# ============================================================

class BulkBookResponse(BaseModel):
    books: list[BookResponse]
    notFound: list[str] = Field(default_factory=list)


# ============================================================
# CHAPTERS RESPONSE
# ============================================================

class ChapterItem(BaseModel):
    lengthMs: int = 0
    startOffsetMs: int = 0
    startOffsetSec: int = 0
    title: str = ""


class ChapterResponse(BaseModel):
    brandIntroDurationMs: int = 0
    brandOutroDurationMs: int = 0
    isAccurate: bool = False
    runtimeLengthMs: int = 0
    runtimeLengthSec: int = 0
    chapters: list[ChapterItem] = Field(default_factory=list)


# ============================================================
# ABS BOOK RESPONSE
# ============================================================

class AbsSeriesRef(BaseModel):
    series: str | None = None
    sequence: str | None = None


class AbsBookResponse(BaseModel):
    asin: str
    title: str | None = None
    subtitle: str | None = None
    description: str | None = None
    cover: str | None = None
    publisher: str | None = None
    publishedYear: str | None = None
    isbn: str | None = None
    language: str | None = None
    duration: str | None = None
    author: str | None = None
    narrator: str | None = None
    tags: list[str] | None = None
    genres: list[str] | None = None
    series: list[AbsSeriesRef] | None = None


class AbsSearchResponse(BaseModel):
    matches: list[AbsBookResponse]