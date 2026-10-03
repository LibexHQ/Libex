"""key books, series and tracks by (asin, region), and link rows to a region

Revision ID: a4d91f7c3b26
Revises: 438dbe70d041
Create Date: 2026-10-02

The same ASIN can be a different marketplace's record, and the first revision
keyed books, series and tracks on asin alone, with every link table pointing
at asin alone. This one keys them on (asin, region): books, series and tracks
take the composite primary key, the five link tables gain the region(s) of the
rows they point at and carry them in their unique keys and foreign keys, and
series.region becomes NOT NULL.

An embedded store never ran the hosted deployment's online script, so this
revision backfills the new columns itself, from the parent row's region (a
store keyed on asin alone has exactly one region per ASIN, so the answer is
never ambiguous). It does not repair: a series with no region, or a link row
whose parent is gone, raises with the count and the transaction rolls back,
because inventing a region would be choosing what the data says.

Also added: books.confirmed_at, books.chapters_confirmed_at,
series.confirmed_at and authors.confirmed_at (nullable, NULL meaning never
confirmed), the walk_results table, and books.is_primary and series.is_primary
(boolean NOT NULL DEFAULT true), which mark the one row of an ASIN that a
reader asked for no region answers with. A store keyed on asin alone has one
row per ASIN, so the default is already right for every row it holds.

On Postgres this is plain DDL. On SQLite every table that changes keys is
rebuilt in batch mode; the connection must have foreign keys off for that
(`libex_core.storage.upgrade` sees to it and checks the result), or each drop
would cascade into the child rows. The region columns are added with a native
ADD COLUMN and the rebuild then gives them their NOT NULL and the same CHECK
constraint every other region column carries.

The downgrade restores the single-region keys and refuses, with the count, once
any ASIN has rows in more than one region. It never deletes. It keeps the
region columns, nullable again.
"""
import warnings
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a4d91f7c3b26'
down_revision: Union[str, Sequence[str], None] = '438dbe70d041'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REGIONS = ("us", "ca", "uk", "au", "fr", "de", "jp", "it", "in", "es", "br")

# table, new column, the column holding the ASIN it follows, the parent table
_REGION_COLUMNS = (
    ("author_book", "book_region", "book_asin", "books"),
    ("book_narrator", "book_region", "book_asin", "books"),
    ("book_genre", "book_region", "book_asin", "books"),
    ("book_series", "book_region", "book_asin", "books"),
    ("book_series", "series_region", "series_asin", "series"),
    ("series_author", "series_region", "series_asin", "series"),
    ("tracks", "region", "asin", "books"),
)

_PRIMARY_KEYS = ("books", "series", "tracks")

# table -> (name, columns before, columns after)
_UNIQUES = {
    "author_book": (
        "uq_author_book", ("author_id", "book_asin"), ("author_id", "book_asin", "book_region"),
    ),
    "book_narrator": (
        "uq_book_narrator", ("book_asin", "narrator_name"),
        ("book_asin", "book_region", "narrator_name"),
    ),
    "book_genre": (
        "uq_book_genre", ("book_asin", "genre_asin"), ("book_asin", "book_region", "genre_asin"),
    ),
    "book_series": (
        "uq_book_series", ("book_asin", "series_asin"),
        ("book_asin", "book_region", "series_asin", "series_region"),
    ),
    "series_author": (
        "uq_series_author", ("series_asin", "author_id"),
        ("series_asin", "series_region", "author_id"),
    ),
}

# table -> foreign keys onto books/series: (local column, parent, parent column)
_OLD_FKS = {
    "tracks": (("asin", "books", "asin"),),
    "author_book": (("book_asin", "books", "asin"),),
    "book_narrator": (("book_asin", "books", "asin"),),
    "book_genre": (("book_asin", "books", "asin"),),
    "book_series": (("book_asin", "books", "asin"), ("series_asin", "series", "asin")),
    "series_author": (("series_asin", "series", "asin"),),
}

# table -> composite foreign keys: (local columns, parent, parent columns)
_NEW_FKS = {
    "tracks": ((("asin", "region"), "books"),),
    "author_book": ((("book_asin", "book_region"), "books"),),
    "book_narrator": ((("book_asin", "book_region"), "books"),),
    "book_genre": ((("book_asin", "book_region"), "books"),),
    "book_series": (
        (("book_asin", "book_region"), "books"),
        (("series_asin", "series_region"), "series"),
    ),
    "series_author": ((("series_asin", "series_region"), "series"),),
}

# name, table, columns: redundant behind the new keys
_DROPPED_INDEXES = (
    ("books_asin_index", "books", ("asin",)),
    ("series_asin_index", "series", ("asin",)),
    ("book_narrator_index", "book_narrator", ("book_asin", "narrator_name")),
    ("book_genre_index", "book_genre", ("book_asin", "genre_asin")),
    ("book_series_index", "book_series", ("book_asin", "series_asin")),
    ("series_author_index", "series_author", ("series_asin", "author_id")),
)

# The convention batch mode names reflected, unnamed SQLite foreign keys by:
# the same names Postgres gives them when it creates them unnamed.
_FK_NAMING = {"fk": "%(table_name)s_%(column_0_name)s_fkey"}

_JSON = postgresql.JSONB().with_variant(sa.JSON(none_as_null=True), "sqlite")


def _region():
    return sa.Enum(*_REGIONS, name="region_enum", create_constraint=True).with_variant(
        postgresql.ENUM(*_REGIONS, name="region_enum", create_type=False), "postgresql"
    )


def _new_fk_name(table: str, columns: Sequence[str]) -> str:
    return f"{table}_{'_'.join(columns)}_fkey"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _cols(columns: Sequence[str]) -> str:
    return ", ".join(columns)


def _add_confirmed_columns() -> None:
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


def _create_walk_results() -> None:
    op.create_table(
        'walk_results',
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('asin', sa.String(length=12), nullable=False),
        sa.Column('region', _region(), nullable=False),
        sa.Column('complete', sa.Boolean(), nullable=False),
        sa.Column('incomplete_reasons', _JSON, nullable=False),
        sa.Column('book_asins', _JSON, nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('author_books', 'series_books')", name='ck_walk_results_kind'),
        sa.PrimaryKeyConstraint('kind', 'asin', 'region'),
    )


def _backfill_and_check() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Checked before any link row is backfilled: a series with no region is
    # also what leaves its links with nothing to take a region from, and the
    # error should name that cause rather than the symptom.
    unknown = bind.execute(sa.text("SELECT count(*) FROM series WHERE region IS NULL")).scalar()
    if unknown:
        raise RuntimeError(
            f"cannot key series by region: {unknown} series rows have no region. "
            "Nothing was changed."
        )
    for table, column, key, parent in _REGION_COLUMNS:
        # A downgrade keeps these columns, so a second upgrade finds them.
        # Only the type's name differs per dialect; the column is added bare.
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            if _is_postgres():
                op.execute(f"ALTER TABLE {table} ADD COLUMN {column} region_enum")
            else:
                op.execute(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR(2)")
        op.execute(
            f"UPDATE {table} SET {column} = "
            f"(SELECT p.region FROM {parent} p WHERE p.asin = {table}.{key})"
        )
        missing = bind.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {column} IS NULL")).scalar()
        if missing:
            raise RuntimeError(
                f"cannot key {table} by region: {missing} rows have no matching "
                f"{parent} row to take a region from. Nothing was changed."
            )


# --- Postgres ----------------------------------------------------------------

def _drop_foreign_keys_onto_parents() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in _OLD_FKS:
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] in ("books", "series"):
                op.drop_constraint(fk["name"], table, type_="foreignkey")


def _upgrade_postgresql() -> None:
    _backfill_and_check()
    for table, column, _key, _parent in _REGION_COLUMNS:
        op.alter_column(table, column, existing_type=_region(), nullable=False)
    op.alter_column('series', 'region', existing_type=_region(), nullable=False)

    _drop_foreign_keys_onto_parents()
    inspector = sa.inspect(op.get_bind())
    for table in _PRIMARY_KEYS:
        name = inspector.get_pk_constraint(table)["name"]
        op.drop_constraint(name, table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, ["asin", "region"])
    for table, (name, _old, new) in _UNIQUES.items():
        op.drop_constraint(name, table, type_="unique")
        op.create_unique_constraint(name, table, list(new))
    for table, keys in _NEW_FKS.items():
        for local, parent in keys:
            op.create_foreign_key(
                _new_fk_name(table, local), table, parent, list(local), ["asin", "region"],
                ondelete="CASCADE",
            )
    for name, table, _columns in _DROPPED_INDEXES:
        op.drop_index(name, table_name=table)


# --- SQLite ------------------------------------------------------------------

def _rebuild(table: str, *, region_overrides: Sequence[str], operations) -> None:
    """Rebuilds `table` in batch mode with `operations(batch)` applied.

    The region columns are redeclared so the rebuilt table keeps the CHECK
    constraint a reflected column would lose, and takes NOT NULL where the
    new key needs it."""
    overrides = [sa.Column(name, _region(), nullable=False) for name in region_overrides]
    with warnings.catch_warnings():
        # Changing a table's primary key in batch mode makes SQLAlchemy note
        # that the copy's key differs from the reflected one, which is the
        # point of the rebuild.
        warnings.filterwarnings("ignore", message=".*as primary_key=True, not matching.*")
        with op.batch_alter_table(
            table, recreate="always", naming_convention=_FK_NAMING, reflect_args=overrides,
        ) as batch:
            operations(batch)


def _upgrade_sqlite() -> None:
    _backfill_and_check()

    def _books(batch):
        batch.drop_index("books_asin_index")
        batch.create_primary_key("books_pkey", ["asin", "region"])

    def _series(batch):
        batch.alter_column("region", existing_type=sa.String(2), nullable=False)
        batch.drop_index("series_asin_index")
        batch.create_primary_key("series_pkey", ["asin", "region"])

    # books.region and series.region already carry their CHECK constraint, which
    # a rebuild keeps, so neither is redeclared.
    _rebuild("books", region_overrides=[], operations=_books)
    _rebuild("series", region_overrides=[], operations=_series)

    for table in ("tracks", "author_book", "book_narrator", "book_genre", "book_series", "series_author"):
        added = [column for t, column, _k, _p in _REGION_COLUMNS if t == table]

        def _operations(batch, table=table):
            for local, parent, _remote in _OLD_FKS[table]:
                batch.drop_constraint(_new_fk_name(table, [local]), type_="foreignkey")
            if table == "tracks":
                batch.create_primary_key("tracks_pkey", ["asin", "region"])
            else:
                name, _old, new = _UNIQUES[table]
                batch.drop_constraint(name, type_="unique")
                batch.create_unique_constraint(name, list(new))
            for local, parent in _NEW_FKS[table]:
                batch.create_foreign_key(
                    _new_fk_name(table, local), parent, list(local), ["asin", "region"],
                    ondelete="CASCADE",
                )
            for name, index_table, _columns in _DROPPED_INDEXES:
                if index_table == table:
                    batch.drop_index(name)

        _rebuild(table, region_overrides=added, operations=_operations)


def upgrade() -> None:
    """Upgrade schema."""
    _add_confirmed_columns()
    _create_walk_results()
    if _is_postgres():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


# --- downgrade ---------------------------------------------------------------

def _refuse_if_regions_diverge() -> None:
    bind = op.get_bind()
    findings = []
    for table in _PRIMARY_KEYS:
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
    """Downgrade schema."""
    _refuse_if_regions_diverge()
    op.drop_table('walk_results')
    if _is_postgres():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


def _downgrade_postgresql() -> None:
    _drop_foreign_keys_onto_parents()
    for table in _PRIMARY_KEYS:
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
    for table, (name, old, _new) in _UNIQUES.items():
        op.drop_constraint(name, table, type_="unique")
        op.create_unique_constraint(name, table, list(old))
    for table, column, _key, _parent in _REGION_COLUMNS:
        op.alter_column(table, column, existing_type=_region(), nullable=True)
    op.alter_column('series', 'region', existing_type=_region(), nullable=True)
    for table in _PRIMARY_KEYS:
        op.create_primary_key(f"{table}_pkey", table, ["asin"])
    for table, keys in _OLD_FKS.items():
        for local, parent, remote in keys:
            op.create_foreign_key(
                _new_fk_name(table, [local]), table, parent, [local], [remote], ondelete="CASCADE",
            )
    for name, table, columns in _DROPPED_INDEXES:
        op.create_index(name, table, list(columns))
    for table, column in (
        ("authors", "confirmed_at"), ("series", "confirmed_at"),
        ("books", "chapters_confirmed_at"), ("books", "confirmed_at"),
    ):
        op.drop_column(table, column)
    for table in ("series", "books"):
        op.drop_column(table, "is_primary")


def _downgrade_sqlite() -> None:
    for table in ("tracks", "author_book", "book_narrator", "book_genre", "book_series", "series_author"):
        added = [column for t, column, _k, _p in _REGION_COLUMNS if t == table]

        def _operations(batch, table=table, added=added):
            for local, _parent in _NEW_FKS[table]:
                batch.drop_constraint(_new_fk_name(table, local), type_="foreignkey")
            for column in added:
                batch.alter_column(column, existing_type=sa.String(2), nullable=True)
            if table == "tracks":
                batch.create_primary_key("tracks_pkey", ["asin"])
            else:
                name, old, _new = _UNIQUES[table]
                batch.drop_constraint(name, type_="unique")
                batch.create_unique_constraint(name, list(old))
            for local, parent, remote in _OLD_FKS[table]:
                batch.create_foreign_key(
                    _new_fk_name(table, [local]), parent, [local], [remote], ondelete="CASCADE",
                )
            for name, index_table, columns in _DROPPED_INDEXES:
                if index_table == table:
                    batch.create_index(name, list(columns))

        _rebuild(table, region_overrides=[], operations=_operations)

    def _books(batch):
        batch.create_primary_key("books_pkey", ["asin"])
        batch.create_index("books_asin_index", ["asin"])
        batch.drop_column("chapters_confirmed_at")
        batch.drop_column("confirmed_at")
        batch.drop_column("is_primary")

    def _series(batch):
        batch.alter_column("region", existing_type=sa.String(2), nullable=True)
        batch.create_primary_key("series_pkey", ["asin"])
        batch.create_index("series_asin_index", ["asin"])
        batch.drop_column("confirmed_at")
        batch.drop_column("is_primary")

    _rebuild("books", region_overrides=[], operations=_books)
    _rebuild("series", region_overrides=[], operations=_series)
    op.drop_column("authors", "confirmed_at")
