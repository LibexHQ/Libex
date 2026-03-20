"""
Database models for Libex.
"""

# Standard library
from datetime import datetime, timezone

# Third party
from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Local
from app.db.base import Base


class Cache(Base):
    """
    Cache table for Audible API responses.
    Key format: {type}:{region}:{identifier}
    Examples:
        book:us:B08G9PRS1K
        author:uk:B000APF21M
        series:us:B08G9PRS1K
        search:us:dune+frank+herbert
    """

    __tablename__ = "cache"

    key: Mapped[str] = mapped_column(String(500), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Cache key={self.key} expires={self.expires_at}>"