"""
Authors route schemas.
Field names match AudiMeta's AuthorDto and MinimalAuthorDto exactly.
"""

# Third party
from pydantic import BaseModel, Field


class AuthorResponse(BaseModel):
    id: int | None = None
    asin: str
    name: str
    description: str | None = None
    image: str | None = None
    region: str
    regions: list[str] = Field(default_factory=list)
    genres: list = Field(default_factory=list)
    updatedAt: str | None = None