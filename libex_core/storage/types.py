"""
Column types that read the same on every supported backend.

Postgres is the hosted backend and its DDL must not move, so each type here
keeps the Postgres spelling as its base and adds a SQLite variant beside it.
"""

# Standard library
from datetime import datetime, timezone

# Third party
from sqlalchemy import JSON, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A timestamp that is always a UTC-aware datetime in Python.

    Postgres keeps a timestamptz and hands back an aware value; SQLite has no
    zone-aware type, stores the wall-clock text it is given and hands back a
    naive datetime, which a reader then takes for local time. Normalising to
    UTC on the way in and attaching UTC on the way out makes both backends
    agree, and keeps SQLite's text ordering the same as chronological order.

    A naive datetime written in is taken to be UTC, the only zone the rest of
    Libex ever produces.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# JSONB on Postgres, exactly as the hosted schema has it. On SQLite it is JSON
# stored as text, and a Python None binds as SQL NULL rather than the JSON
# value 'null', so "no value" reads back as None either way and a NULL check
# in a query still means what it means on Postgres.
JSONDocument = JSONB().with_variant(JSON(none_as_null=True), "sqlite")
