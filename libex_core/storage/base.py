"""
The declarative base every stored table hangs off.

One base is shared with the hosted app, whose own tables (the response cache,
the catalog genre tree) register on it beside the ones defined here, so a
single metadata describes the whole hosted schema.
"""

# Third party
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
