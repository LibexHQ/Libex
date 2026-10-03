"""key books, series and tracks by (asin, region), and link rows to a region

Revision ID: e7c2a94d1f58
Revises: b8d2e5a71c46
Create Date: 2026-10-02

The same ASIN can be a different marketplace's record, and until now the
primary keys said otherwise: books, series and tracks keyed on asin alone, so
the second region to return an ASIN overwrote the first, and every link table
pointed at asin alone.

This revision is catalog-only. The slow, data-proportional work (the region
columns on the link tables and on tracks, their backfill, the unique indexes,
NOT NULL) is done ahead of time by scripts/region_keys.py against the running
service, so what is left here swaps constraints onto indexes that already
exist, under lock_timeout, and takes no longer than the catalog changes need.

It never repairs. Every precondition is asserted first and any failure raises
with the full list, because a migration that quietly fixed a half-prepared
database would be choosing, on its own, what the data should say. The one
exception is a database with no rows in any of the eight tables, where there
is nothing to backfill and nothing to protect: a fresh install or a test
database runs the whole chain from the start and has never seen the script,
so the columns and indexes are built here, instantly.

What changes, on a prepared database:

  - books, series, tracks: the primary key becomes (asin, region), adopting
    the unique index the script built (PRIMARY KEY USING INDEX, a catalog
    change). books_asin_index and series_asin_index are redundant behind it
    and are dropped.
  - author_book, book_narrator, book_genre, book_series, series_author: the
    unique constraint widens to carry the region(s), adopting the script's
    unique index of the same columns. Without that a second region's link to
    the same ASIN would be silently dropped by the DO NOTHING insert. The
    non-unique indexes the new unique key now covers by prefix
    (book_narrator_index, book_genre_index, book_series_index,
    series_author_index) are dropped.
  - genre_book_index, which serves the genre and category filters, widens from
    (genre_asin, book_asin) to (genre_asin, book_asin, book_region), because
    those filters now read the region of every link they find. Without it the
    planner reads the whole of book_genre (about 11M rows) instead of probing
    it. The script builds the wider index ahead of time under the name
    genre_book_region_index; this revision drops the old one and renames it.
  - every foreign key onto books or series becomes the composite one,
    ON DELETE CASCADE, added NOT VALID: enforced for every new row and every
    delete at once, without a scan of the link tables while the table locks
    are held. VALIDATE CONSTRAINT it afterwards, which takes only a SHARE
    UPDATE EXCLUSIVE lock and blocks neither reads nor writes:
        ALTER TABLE <table> VALIDATE CONSTRAINT <name>;
    with the names listed in _COMPOSITE_FKS.

Also added: books.confirmed_at, books.chapters_confirmed_at,
series.confirmed_at and authors.confirmed_at (nullable, no default, NULL
meaning never confirmed), and the walk_results table.

books.is_primary and series.is_primary, boolean NOT NULL DEFAULT true, mark the
one row of an ASIN that a reader asked for no region answers with. Until now
asin was unique, so every row that exists is the only one of its ASIN and the
constant default is already right for all of them; a catalog-only change on
Postgres 16, no rewrite. Only a row inserted afterwards for an ASIN that
another region already holds is written false, by the writer. Region-less list
readers filter on it instead of ranking every row of the table per ASIN, which
the planner cannot estimate and which put the unfiltered list past the
statement timeout.

The downgrade restores the single-region keys, foreign keys and indexes, and
refuses, with the count, once any ASIN has rows in more than one region: that
data cannot be keyed by asin alone and nothing here will choose which to
delete. It keeps the region columns, now nullable again so the older code can
insert without them, and builds the old keys with ordinary (blocking) DDL, so
run it with the writers stopped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e7c2a94d1f58'
down_revision: Union[str, Sequence[str], None] = 'b8d2e5a71c46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LOCK_TIMEOUT = "3s"

# Columns scripts/region_keys.py adds (and fills) ahead of this revision.
_REGION_COLUMNS = (
    ("author_book", "book_region"),
    ("book_narrator", "book_region"),
    ("book_genre", "book_region"),
    ("book_series", "book_region"),
    ("book_series", "series_region"),
    ("series_author", "series_region"),
    ("tracks", "region"),
)

# series.region exists already and was nullable; the script makes it NOT NULL.
_NOT_NULL = _REGION_COLUMNS + (("series", "region"),)

# The unique indexes the script builds, by the names it gives them: table,
# columns. The names are a contract with that script.
_INDEXES = {
    "uq_books_asin_region": ("books", ("asin", "region")),
    "uq_series_asin_region": ("series", ("asin", "region")),
    "uq_tracks_asin_region": ("tracks", ("asin", "region")),
    "uq_author_book_region": ("author_book", ("author_id", "book_asin", "book_region")),
    "uq_book_narrator_region": ("book_narrator", ("book_asin", "book_region", "narrator_name")),
    "uq_book_genre_region": ("book_genre", ("book_asin", "book_region", "genre_asin")),
    "uq_book_series_region": (
        "book_series", ("book_asin", "book_region", "series_asin", "series_region"),
    ),
    "uq_series_author_region": ("series_author", ("series_asin", "series_region", "author_id")),
}

# The wider replacement for genre_book_index, built ahead of time by the
# script under this name (non-unique): name -> table, columns. This revision
# renames it to the old index's name once that is dropped.
_WIDENED = {
    "genre_book_region_index": ("book_genre", ("genre_asin", "book_asin", "book_region")),
}
_WIDENED_FINAL = "genre_book_index"

# table -> (primary key constraint name, index adopted as it)
_PRIMARY_KEYS = {
    "books": ("books_pkey", "uq_books_asin_region"),
    "series": ("series_pkey", "uq_series_asin_region"),
    "tracks": ("tracks_pkey", "uq_tracks_asin_region"),
}

# table -> (unique constraint name, columns before, index adopted as it)
_UNIQUES = {
    "author_book": ("uq_author_book", ("author_id", "book_asin"), "uq_author_book_region"),
    "book_narrator": ("uq_book_narrator", ("book_asin", "narrator_name"), "uq_book_narrator_region"),
    "book_genre": ("uq_book_genre", ("book_asin", "genre_asin"), "uq_book_genre_region"),
    "book_series": ("uq_book_series", ("book_asin", "series_asin"), "uq_book_series_region"),
    "series_author": ("uq_series_author", ("series_asin", "author_id"), "uq_series_author_region"),
}

# (table, name, local columns, referenced table, referenced columns)
_COMPOSITE_FKS = (
    ("tracks", "tracks_asin_region_fkey", ("asin", "region"), "books", ("asin", "region")),
    ("author_book", "author_book_book_asin_book_region_fkey",
     ("book_asin", "book_region"), "books", ("asin", "region")),
    ("book_narrator", "book_narrator_book_asin_book_region_fkey",
     ("book_asin", "book_region"), "books", ("asin", "region")),
    ("book_genre", "book_genre_book_asin_book_region_fkey",
     ("book_asin", "book_region"), "books", ("asin", "region")),
    ("book_series", "book_series_book_asin_book_region_fkey",
     ("book_asin", "book_region"), "books", ("asin", "region")),
    ("book_series", "book_series_series_asin_series_region_fkey",
     ("series_asin", "series_region"), "series", ("asin", "region")),
    ("series_author", "series_author_series_asin_series_region_fkey",
     ("series_asin", "series_region"), "series", ("asin", "region")),
)

# Redundant behind the new keys, with how to rebuild each on downgrade.
_DROPPED_INDEXES = (
    ("books_asin_index", "books", ("asin",)),
    ("series_asin_index", "series", ("asin",)),
    ("book_narrator_index", "book_narrator", ("book_asin", "narrator_name")),
    ("book_genre_index", "book_genre", ("book_asin", "genre_asin")),
    ("book_series_index", "book_series", ("book_asin", "series_asin")),
    ("series_author_index", "series_author", ("series_asin", "author_id")),
)

# Single-column keys of the old shape: table -> parent table.
_OLD_FKS = {
    "tracks": (("asin", "books", "asin"),),
    "author_book": (("book_asin", "books", "asin"),),
    "book_narrator": (("book_asin", "books", "asin"),),
    "book_genre": (("book_asin", "books", "asin"),),
    "book_series": (("book_asin", "books", "asin"), ("series_asin", "series", "asin")),
    "series_author": (("series_asin", "series", "asin"),),
}

_COUNTED_TABLES = (
    "books", "series", "tracks", "author_book", "book_narrator", "book_genre",
    "book_series", "series_author",
)


def _cols(columns: Sequence[str]) -> str:
    return ", ".join(columns)


def _tables_are_empty() -> bool:
    bind = op.get_bind()
    for table in _COUNTED_TABLES:
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            return False
    return True


def _build_empty() -> None:
    """Everything the script would have done, for a database holding no rows."""
    for table, column in _REGION_COLUMNS:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} region_enum")
    for table, column in _NOT_NULL:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")
    for name, (table, columns) in _INDEXES.items():
        op.execute(f"DROP INDEX IF EXISTS {name}")
        op.execute(f"CREATE UNIQUE INDEX {name} ON {table} ({_cols(columns)})")
    for name, (table, columns) in _WIDENED.items():
        op.execute(f"DROP INDEX IF EXISTS {name}")
        op.execute(f"CREATE INDEX {name} ON {table} ({_cols(columns)})")


def _precondition_failures() -> list[str]:
    bind = op.get_bind()
    failures: list[str] = []

    nullability = {
        (row[0], row[1]): row[2]
        for row in bind.execute(sa.text(
            "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        ))
    }
    for table, column in _NOT_NULL:
        state = nullability.get((table, column))
        if state is None:
            failures.append(f"column {table}.{column} does not exist")
        elif state != "NO":
            failures.append(f"column {table}.{column} is nullable")

    if nullability.get(("series", "region")) is not None:
        null_series = bind.execute(sa.text("SELECT count(*) FROM series WHERE region IS NULL")).scalar()
        if null_series:
            failures.append(f"{null_series} series rows have a NULL region")

    indexes = {
        row[0]: row
        for row in bind.execute(sa.text(
            "SELECT c.relname, t.relname, i.indisvalid, i.indisunique, i.indpred IS NOT NULL, "
            "  ARRAY(SELECT a.attname::text FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(attnum, ord) "
            "        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            "        ORDER BY k.ord) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = ANY(:names)"
        ), {"names": [*_INDEXES, *_WIDENED]})
    }
    for name, (table, columns) in _WIDENED.items():
        row = indexes.get(name)
        if row is None:
            failures.append(f"index {name} does not exist")
        elif row[1] != table or tuple(row[5]) != columns:
            failures.append(f"index {name} is not on {table} ({_cols(columns)})")
        elif not row[2]:
            failures.append(f"index {name} is invalid")
        elif row[3] or row[4]:
            failures.append(f"index {name} is unique or partial")
    for name, (table, columns) in _INDEXES.items():
        row = indexes.get(name)
        if row is None:
            failures.append(f"index {name} does not exist")
            continue
        if row[1] != table or tuple(row[5]) != columns:
            failures.append(f"index {name} is not on {table} ({_cols(columns)})")
        if not row[2]:
            failures.append(f"index {name} is invalid")
        if not row[3]:
            failures.append(f"index {name} is not unique")
        if row[4]:
            failures.append(f"index {name} is partial")
    return failures


def _primary_key_names(table: str) -> list[str]:
    return [
        row[0]
        for row in op.get_bind().execute(sa.text(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class t ON t.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = :table AND con.contype = 'p'"
        ), {"table": table})
    ]


def _drop_foreign_keys_onto(parents: Sequence[str]) -> None:
    """Drops every foreign key that references one of `parents`, whatever it
    is called and however many columns it has."""
    rows = op.get_bind().execute(sa.text(
        "SELECT t.relname, con.conname FROM pg_constraint con "
        "JOIN pg_class t ON t.oid = con.conrelid "
        "JOIN pg_class p ON p.oid = con.confrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE con.contype = 'f' AND n.nspname = current_schema() AND p.relname = ANY(:parents) "
        "ORDER BY t.relname, con.conname"
    ), {"parents": list(parents)}).all()
    for table, name in rows:
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")

    # Added unconditionally and first: nullable, no default, catalog-only.
    for table, column in (
        ("books", "confirmed_at"),
        ("books", "chapters_confirmed_at"),
        ("series", "confirmed_at"),
        ("authors", "confirmed_at"),
    ):
        op.add_column(table, sa.Column(column, sa.DateTime(timezone=True), nullable=True))
    for table in ("books", "series"):
        op.add_column(
            table,
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.true()),
        )

    op.create_table(
        'walk_results',
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('asin', sa.String(length=12), nullable=False),
        sa.Column(
            'region',
            postgresql.ENUM(name='region_enum', create_type=False),
            nullable=False,
        ),
        sa.Column('complete', sa.Boolean(), nullable=False),
        sa.Column('incomplete_reasons', postgresql.JSONB(), nullable=False),
        sa.Column('book_asins', postgresql.JSONB(), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('author_books', 'series_books')", name='ck_walk_results_kind'),
        sa.PrimaryKeyConstraint('kind', 'asin', 'region'),
    )

    if _tables_are_empty():
        _build_empty()

    failures = _precondition_failures()
    if failures:
        raise RuntimeError(
            "the region-key swap cannot run: the database is not prepared. "
            "Run scripts/region_keys.py (expand, backfill, index, then finalize in "
            "the maintenance window) first. Nothing has been changed. "
            + "; ".join(failures)
        )

    # Foreign keys come off first: the old primary keys cannot be dropped
    # while they depend on them.
    _drop_foreign_keys_onto(("books", "series"))

    for table, (pkey, index) in _PRIMARY_KEYS.items():
        for name in _primary_key_names(table):
            op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {pkey} PRIMARY KEY USING INDEX {index}")

    for table, (name, _old, index) in _UNIQUES.items():
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE USING INDEX {index}")

    for table, name, local, parent, remote in _COMPOSITE_FKS:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({_cols(local)}) "
            f"REFERENCES {parent} ({_cols(remote)}) ON DELETE CASCADE NOT VALID"
        )

    for name, _table, _columns in _DROPPED_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")

    # The narrower genre_book_index gives way to the wider one built ahead of
    # time; the rename is a catalog change.
    for name in _WIDENED:
        op.execute(f"DROP INDEX IF EXISTS {_WIDENED_FINAL}")
        op.execute(f"ALTER INDEX {name} RENAME TO {_WIDENED_FINAL}")


def _refuse_if_regions_diverge() -> None:
    bind = op.get_bind()
    findings = []
    for table in ("books", "series", "tracks"):
        count = bind.execute(sa.text(
            f"SELECT count(*) FROM (SELECT asin FROM {table} GROUP BY asin HAVING count(*) > 1) d"
        )).scalar()
        if count:
            findings.append(f"{count} ASINs in {table} have rows in more than one region")
    if findings:
        raise RuntimeError(
            "cannot downgrade to the single-region keys: " + "; ".join(findings)
            + ". Nothing was changed or deleted; resolve those rows deliberately first."
        )


def downgrade() -> None:
    _refuse_if_regions_diverge()
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")

    op.drop_table('walk_results')
    for table in ("series", "books"):
        op.drop_column(table, "is_primary")
    for table, column in (
        ("authors", "confirmed_at"),
        ("series", "confirmed_at"),
        ("books", "chapters_confirmed_at"),
        ("books", "confirmed_at"),
    ):
        op.drop_column(table, column)

    _drop_foreign_keys_onto(("books", "series"))

    for table in _PRIMARY_KEYS:
        for name in _primary_key_names(table):
            op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
    for table, (name, columns, _index) in _UNIQUES.items():
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE ({_cols(columns)})")

    # The new region columns stay, nullable again so the older code can insert
    # without them; series.region goes back to nullable as it was.
    for table, column in _NOT_NULL:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL")

    for table, (pkey, _index) in _PRIMARY_KEYS.items():
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {pkey} PRIMARY KEY (asin)")

    for table, keys in _OLD_FKS.items():
        for column, parent, remote in keys:
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {table}_{column}_fkey "
                f"FOREIGN KEY ({column}) REFERENCES {parent} ({remote}) ON DELETE CASCADE"
            )

    for name, table, columns in _DROPPED_INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({_cols(columns)})")

    # Back to the narrower genre_book_index. Ordinary (blocking) DDL, like the
    # rest of this downgrade.
    op.execute(f"DROP INDEX IF EXISTS {_WIDENED_FINAL}")
    op.execute(f"CREATE INDEX {_WIDENED_FINAL} ON book_genre (genre_asin, book_asin)")
