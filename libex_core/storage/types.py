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


def _as_utc(value):
    """UTC-aware form of a datetime; anything else passes through unchanged,
    as a plain DateTime column would let the driver take it."""
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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

    def process_bind_param(self, value, dialect):
        return _as_utc(value)

    def process_result_value(self, value, dialect):
        return _as_utc(value)


# JSONB on Postgres, exactly as the hosted schema has it. On SQLite it is JSON
# stored as text with none_as_null=True. The two differ for a bare Python None:
# Postgres binds it as the JSON value 'null' (the JSONB variant is plain), while
# SQLite stores SQL NULL. Writers that want "no value" to be SQL NULL on both
# must bind None explicitly with none_as_null, as the hosted writer does.
JSONDocument = JSONB().with_variant(JSON(none_as_null=True), "sqlite")
