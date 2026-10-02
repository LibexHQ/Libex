"""
Every conflict target and link insert the writers issue names the region.

A target of asin alone would, under the composite keys, fail to match the
index it needs (Postgres raises) or, for the DO NOTHING link inserts, silently
drop the second marketplace's link. The compiled SQL is checked directly so no
database is needed to see it.
"""

# Third party
import pytest
from sqlalchemy.dialects import postgresql

# Local
from libex_core.storage.write.statements import statements_for


def _sql(name):
    return str(getattr(statements_for("postgresql"), name).compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize("name", ["book_upsert", "series_upsert"])
def test_book_and_series_upserts_conflict_on_asin_and_region(name):
    assert "ON CONFLICT (asin, region) DO UPDATE" in _sql(name)


def test_the_upserts_never_assign_the_region_they_are_keyed_on():
    for name in ("book_upsert", "series_upsert"):
        update = _sql(name).split("DO UPDATE SET", 1)[1]
        assert " region = " not in update and not update.lstrip().startswith("region")


def test_the_book_series_link_conflicts_on_both_records():
    assert (
        "ON CONFLICT (book_asin, book_region, series_asin, series_region) DO UPDATE"
        in _sql("book_series_upsert")
    )


@pytest.mark.parametrize("name, columns", [
    ("book_genre_insert", ("book_asin", "book_region", "genre_asin")),
    ("book_narrator_insert", ("book_asin", "book_region", "narrator_name")),
    ("author_book_insert", ("author_id", "book_asin", "book_region")),
    ("series_author_insert", ("series_asin", "series_region", "author_id")),
])
def test_every_do_nothing_link_carries_its_region(name, columns):
    sql = _sql(name)
    inserted = sql.split("(", 1)[1].split(")", 1)[0]
    assert sorted(c.strip() for c in inserted.split(",")) == sorted(columns)
    assert "ON CONFLICT DO NOTHING" in sql
