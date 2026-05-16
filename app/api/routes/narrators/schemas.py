"""
Narrators route schemas.
"""

# Third party
from pydantic import BaseModel


class AudioSampleResponse(BaseModel):
    url: str
    title: str | None = None
    genre: str | None = None
    source: str | None = None


class NarratorProfileResponse(BaseModel):
    name: str
    description: str | None = None
    image: str | None = None
    website: str | None = None
    wikipediaUrl: str | None = None
    languages: dict[str, int] | None = None
    accents: dict[str, int] | None = None
    gender: str | None = None
    genresNarrated: list[str] | None = None
    audiobooksProduced: str | None = None
    culturalHeritage: str | None = None
    publishers: list[str] | None = None
    socialLinks: dict[str, str] | None = None
    audioSamples: list[AudioSampleResponse] | None = None
    source: str | None = None
    sourceUrl: str | None = None
    sourceUpdatedAt: str | None = None
    attribution: str | None = None
    updatedAt: str | None = None