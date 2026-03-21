"""
Authors route schemas.
"""

# Third party
from pydantic import BaseModel, Field


class AuthorResponse(BaseModel):
    asin: str
    name: str
    description: str | None = None
    image: str | None = None
    region: str
    genres: list = Field(default_factory=list)
    updatedAt: str | None = None