"""
Every statement the hosted readers issue, compiled for PostgreSQL.

The readers' query text is the one thing SQLite cannot vouch for: the
equivalence tests compare answers, and an answer can stay the same while the
SQL that produces it changes. So the compiled text and the bound parameters of
every read entry point, under a representative spread of filters and sorts, are
recorded in a golden file and compared exactly. Values that come from the
clock (the new-release and coming-soon windows) are masked; everything else,
bind names included, is pinned.
"""

# Standard library
import asyncio
import hashlib
import inspect
import json
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

# Third party
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

# Local
import app.services.db.filtering as hosted_filtering
import app.services.db.reader as hosted_reader
from app.services import sorting
from libex_core.storage.models import Book, Narrator

DIALECT = postgresql.dialect()
GOLDEN = Path(__file__).parent / "golden" / "postgres_read_statements.json"

BOOK_FILTERS = dict(
    title="t", subtitle="s", description="d", summary="su", publisher="p", copyright="c",
    isbn="i", language="en", rating_better_than=1.5, rating_worse_than=4.5,
    longer_than=10, shorter_than=100, explicit=True, whisper_sync=False, has_pdf=True,
    book_format="x", content_type="y", content_delivery_type="z", is_listenable=True,
    is_buyable=False, is_vvab=True, plan_name="Plus", genre="fan", category="1,2",
)
NARRATOR_FILTERS = dict(
    gender="g", language="English", audiobooks_produced="x", source="s", cultural_heritage="c",
)
EXTRA_FILTERS = {"region": "us", "author_name": "a", "series_name": "sn", "book_region": "uk"}

_AUTHOR_READS = {"get_author_from_db", "get_author_books_from_db", "get_author_book_asins_from_db"}


class _Result:
    def scalars(self):
        return self

    def all(self):
        return []

    def fetchall(self):
        return []

    def scalar_one_or_none(self):
        return None

    def scalar_one(self):
        return 0


class _RecordingSession:
    """Compiles each statement for PostgreSQL and answers as an empty table."""

    def __init__(self, tag, out):
        self.tag = tag
        self.out = out

    async def execute(self, stmt):
        self.out.append(_record(self.tag, stmt))
        return _Result()

    async def rollback(self):
        pass


def _mask(value):
    if isinstance(value, (datetime, date)):
        return "<clock>"
    if isinstance(value, (list, tuple)):
        return [_mask(v) for v in value]
    return value


def _record(tag, stmt):
    compiled = stmt.compile(dialect=DIALECT)
    params = {name: repr(_mask(value)) for name, value in sorted(compiled.params.items())}
    return {"tag": tag, "sql": " ".join(str(compiled).split()), "params": params}


async def _call(out, tag, fn, **kwargs):
    accepted = inspect.signature(fn).parameters
    await fn(_RecordingSession(tag, out), **{k: v for k, v in kwargs.items() if k in accepted})


async def collect():
    """Every recorded statement, in a fixed order, as JSON-ready dicts."""
    out = []
    sort_variants = [(None, None)] + [
        (field, order) for field in sorting.BOOK_SORT_FIELDS for order in ("asc", "desc")
    ]
    entry_points = sorted(n for n in dir(hosted_reader) if n.endswith("_from_db"))
    for name in entry_points:
        fn = getattr(hosted_reader, name)
        params = inspect.signature(fn).parameters
        required = [p for p in list(params)[1:] if params[p].default is inspect.Parameter.empty]
        base = {p: "X" for p in required}
        if name == "get_books_from_db":
            base = {"asins": ["A", "B"]}
        if name in _AUTHOR_READS:
            base["region"] = "us"
        for filters in ({}, {**BOOK_FILTERS, **EXTRA_FILTERS}):
            for sort, order in sort_variants:
                tag = f"{name}|{'filtered' if filters else 'plain'}|{sort}|{order}"
                await _call(out, tag, fn, **{**base, **filters, "sort": sort, "order": order})

    async def no_entry(*args, **kwargs):
        return None

    for region in (None, "us"):
        with patch.object(hosted_reader.cache, "get_entry", no_entry), patch.object(
            hosted_reader.cache, "set", no_entry
        ):
            await hosted_reader.get_db_stats(_RecordingSession(f"stats|{region}", out), region, True)
    await hosted_reader.get_stored_genres(_RecordingSession("stored_genres", out), "us")
    await hosted_reader._get_series_positions(_RecordingSession("positions", out), "A")
    await hosted_reader._get_series_positions_batch(_RecordingSession("positions_batch", out), ["A", "B"])

    helpers = (
        ("apply_book_filters", select(Book), BOOK_FILTERS),
        ("apply_narrator_filters", select(Narrator), NARRATOR_FILTERS),
    )
    for name, stmt, filters in helpers:
        out.append(_record(name, getattr(hosted_filtering, name)(stmt, **filters)))
    out.append(_record("apply_genre_filter", hosted_filtering.apply_genre_filter(select(Book), "x")))
    out.append(_record("apply_category_filter", hosted_filtering.apply_category_filter(select(Book), " 1, 2,")))
    return out


def pack(records):
    """The golden's shape: each distinct SQL text once, keyed by its digest,
    and every tag pointing at its text and its bound parameters."""
    texts, statements = {}, {}
    for record in records:
        digest = hashlib.sha256(record["sql"].encode()).hexdigest()[:16]
        texts[digest] = record["sql"]
        statements[record["tag"]] = {"sql": digest, "params": record["params"]}
    return {"sql": texts, "statements": statements}


if __name__ == "__main__":
    # Writes the golden to the path given. It is captured once, from the
    # readers as they stood before they moved into libex_core, and rewritten
    # only when a query is meant to change.
    Path(sys.argv[1]).write_text(
        json.dumps(pack(asyncio.run(collect())), indent=1, sort_keys=True) + "\n"
    )
