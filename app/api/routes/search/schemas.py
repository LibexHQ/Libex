"""
Search route schemas.
Defines request parameters and response models for search endpoints.
"""

# Third party
from pydantic import BaseModel, Field


# ============================================================
# REQUEST PARAMS
# ============================================================

class SearchQueryParams(BaseModel):
    region: str = Field(default="us", description="Audible region code")
    title: str | None = Field(default=None, description="Book title")
    author: str | None = Field(default=None, description="Author name")
    keywords: str | None = Field(default=None, description="Keywords")
    limit: int = Field(default=10, description="Maximum results", ge=1, le=50)
    cache: bool = Field(default=False, description="Return cached data if available")


class QuickSearchQueryParams(BaseModel):
    region: str = Field(default="us", description="Audible region code")
    keywords: str = Field(description="Search keywords")