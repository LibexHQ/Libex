"""
Books route schemas.
Defines request parameters and response models for book endpoints.
"""

# Standard library
from typing import Any

# Third party
from pydantic import BaseModel, Field


# ============================================================
# RESPONSE MODELS
# ============================================================

class AuthorRef(BaseModel):
    name: str
    asin: str | None = None
    region: str | None = None


class SeriesRef(BaseModel):
    asin: str | None = None
    title: str | None = None
    position: str | None = None
    region: str | None = None


class BookResponse(BaseModel):
    asin: str
    title: str | None = None
    subtitle: str | None = None
    authors: list[AuthorRef] = Field(default_factory=list)
    narrators: list[str] = Field(default_factory=list)
    series: list[SeriesRef] = Field(default_factory=list)
    series_name: str | None = None
    series_asin: str | None = None
    series_position: str | None = None
    series_region: str | None = None
    cover_url: str | None = None
    description: str | None = None
    summary: str | None = None
    publisher: str | None = None
    language: str | None = None
    runtime_length_min: int | None = None
    rating: float | None = None
    genres: list[str] = Field(default_factory=list)
    release_date: str | None = None
    explicit: bool = False
    has_pdf: bool = False
    whisper_sync: bool = False
    isbn: str | None = None
    content_type: str | None = None
    sku: str | None = None
    region: str


class ChapterInfo(BaseModel):
    chapters: list[dict[str, Any]]
    asin: str
    region: str


# ============================================================
# REQUEST PARAMS
# ============================================================

class BookQueryParams(BaseModel):
    region: str = Field(default="us", description="Audible region code")
    cache: bool = Field(default=False, description="Return cached data if available")


class BulkBookQueryParams(BaseModel):
    asins: str = Field(description="Comma-separated list of ASINs, max 1000")
    region: str = Field(default="us", description="Audible region code")
    cache: bool = Field(default=False, description="Return cached data if available")