"""
Books route schemas.

The AudiMeta-shaped models (BookResponse, ChapterResponse, and the rest of
the drop-in DTOs) live in libex_core.models so they carry no route
dependency. What stays here is the Audiobookshelf (ABS) custom-metadata-
provider shape: a narrower, differently-cased response format that only the
region-prefixed search routes produce, for a consumer that expects it rather
than AudiMeta's own shape.
"""

# Third party
from pydantic import BaseModel


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