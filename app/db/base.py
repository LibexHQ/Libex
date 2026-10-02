"""
SQLAlchemy declarative base.
The base itself lives in libex_core.storage so the hosted tables and the
library's share one metadata.
"""

# Local
from libex_core.storage.base import Base

__all__ = ["Base"]
