"""
Series route schemas.
Defines request parameters and response models for series endpoints.
"""

# Third party
from pydantic import BaseModel


# ============================================================
# RESPONSE MODELS
# ============================================================

class SeriesResponse(BaseModel):
    asin: str
    title: str | None = None
    description: str | None = None
    region: str


class SeriesBooksResponse(BaseModel):
    asin: str
    region: str
    book_asins: list[str]
    total: int