"""
Authors route schemas.
Defines request parameters and response models for author endpoints.
"""

# Standard library

# Third party
from pydantic import BaseModel, Field


# ============================================================
# RESPONSE MODELS
# ============================================================

class AuthorResponse(BaseModel):
    asin: str
    name: str
    description: str | None = None
    image: str | None = None
    region: str


class AuthorBooksResponse(BaseModel):
    asin: str
    region: str
    book_asins: list[str]
    total: int


# ============================================================
# REQUEST PARAMS
# ============================================================

class AuthorQueryParams(BaseModel):
    region: str = Field(default="us", description="Audible region code")
    cache: bool = Field(default=False, description="Return cached data if available")


class AuthorBooksQueryParams(BaseModel):
    region: str = Field(default="us", description="Audible region code")
    cache: bool = Field(default=False, description="Return cached data if available")
    name: str | None = Field(default=None, description="Author name fallback if no ASIN")