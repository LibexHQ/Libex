"""
SQLAlchemy declarative base.
All models inherit from this.
"""

# Third party
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass