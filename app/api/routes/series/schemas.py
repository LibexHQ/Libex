"""
Series route schemas.
"""

# Third party
from pydantic import BaseModel


class SeriesResponse(BaseModel):
    asin: str
    name: str | None = None
    description: str | None = None
    region: str
    position: str | None = None
    updatedAt: str | None = None