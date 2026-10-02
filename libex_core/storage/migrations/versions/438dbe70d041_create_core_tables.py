"""create the core tables

Revision ID: 438dbe70d041
Revises:
Create Date: 2026-10-02

The first revision of the package's own chain: the twelve tables
`libex_core.storage.models` defines, on SQLite and on Postgres.

This is written out by hand, not read from the models, because a revision
describes the schema as it was when it shipped and the models move on. On
Postgres it produces exactly the schema the hosted app's chain does for these
tables, native enums and all (a test compares the two). On SQLite the enum
columns, which are plain text there, carry a CHECK constraint so a value
outside the set is refused as Postgres would refuse it.

The downgrade drops every table, and the data in them. The store never calls
it; it is here so the revision can be reversed by hand, and so the chain is
whole.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '438dbe70d041'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REGIONS = ("us", "ca", "uk", "au", "fr", "de", "jp", "it", "in", "es", "br")
_GENRE_TYPES = ("Genres", "Tags")

# jsonb on Postgres, JSON text on SQLite, with SQL NULL for a missing value.
_JSON = postgresql.JSONB().with_variant(sa.JSON(none_as_null=True), "sqlite")


def _enum(name: str, values: tuple[str, ...]):
    """A native enum on Postgres, whose type is created once by `upgrade`
    (create_type=False, so the three tables sharing region_enum do not each try
    to), and a text column with a CHECK constraint on SQLite."""
    return sa.Enum(*values, name=name, create_constraint=True).with_variant(
        postgresql.ENUM(*values, name=name, create_type=False), "postgresql"
    )


def _region():
    return _enum("region_enum", _REGIONS)


def _genre_type():
    return _enum("genre_type_enum", _GENRE_TYPES)


def _postgres_types(create: bool) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, values in (("region_enum", _REGIONS), ("genre_type_enum", _GENRE_TYPES)):
        enum = postgresql.ENUM(*values, name=name)
        if create:
            enum.create(bind, checkfirst=False)
        else:
            enum.drop(bind, checkfirst=False)


def upgrade() -> None:
    """Upgrade schema."""
    _postgres_types(create=True)
    op.create_table('authors',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('asin', sa.String(length=12), nullable=True),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('region', _region(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('image', sa.Text(), nullable=True),
    sa.Column('fetched_description', sa.Boolean(), nullable=False),
    sa.Column('last_seeded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('asin', 'region', 'name', name='authors_asin_region_name_unique')
    )
    op.create_index('authors_asin_region_name_index', 'authors', ['asin', 'region', 'name'], unique=False)
    op.create_index('authors_region_name_index', 'authors', ['region', 'name'], unique=False)
    op.create_index('uq_authors_name_region_null_asin', 'authors', ['name', 'region'], unique=True, postgresql_where=sa.text('asin IS NULL'), sqlite_where=sa.text('asin IS NULL'))
    op.create_table('books',
    sa.Column('asin', sa.String(length=12), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('subtitle', sa.Text(), nullable=True),
    sa.Column('region', _region(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('publisher', sa.Text(), nullable=True),
    sa.Column('copyright', sa.Text(), nullable=True),
    sa.Column('isbn', sa.String(length=16), nullable=True),
    sa.Column('language', sa.String(length=50), nullable=True),
    sa.Column('rating', sa.Double(), nullable=True),
    sa.Column('release_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('length_minutes', sa.Integer(), nullable=True),
    sa.Column('explicit', sa.Boolean(), nullable=False),
    sa.Column('whisper_sync', sa.Boolean(), nullable=False),
    sa.Column('has_pdf', sa.Boolean(), nullable=False),
    sa.Column('image', sa.Text(), nullable=True),
    sa.Column('book_format', sa.String(length=50), nullable=True),
    sa.Column('content_type', sa.String(length=100), nullable=True),
    sa.Column('content_delivery_type', sa.String(length=100), nullable=True),
    sa.Column('episode_number', sa.String(length=20), nullable=True),
    sa.Column('episode_type', sa.String(length=50), nullable=True),
    sa.Column('sku', sa.String(length=20), nullable=True),
    sa.Column('sku_group', sa.String(length=20), nullable=True),
    sa.Column('is_listenable', sa.Boolean(), nullable=False),
    sa.Column('is_buyable', sa.Boolean(), nullable=False),
    sa.Column('is_vvab', sa.Boolean(), nullable=False),
    sa.Column('plans', _JSON, nullable=True),
    sa.Column('num_ratings', sa.Integer(), nullable=True),
    sa.Column('num_reviews', sa.Integer(), nullable=True),
    sa.Column('publication_name', sa.Text(), nullable=True),
    sa.Column('publication_datetime', sa.DateTime(timezone=True), nullable=True),
    sa.Column('extended_product_description', sa.Text(), nullable=True),
    sa.Column('product_state', sa.Text(), nullable=True),
    sa.Column('audible_extras', _JSON, nullable=True),
    sa.Column('extras_withheld', _JSON, nullable=True),
    sa.Column('chapters_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('asin')
    )
    op.create_index('books_asin_index', 'books', ['asin'], unique=False)
    op.create_index('books_region_asin_index', 'books', ['region', 'asin'], unique=False)
    op.create_table('genres',
    sa.Column('asin', sa.String(length=12), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('type', _genre_type(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('asin')
    )
    op.create_table('narrators',
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('image', sa.Text(), nullable=True),
    sa.Column('website', sa.Text(), nullable=True),
    sa.Column('wikipedia_url', sa.Text(), nullable=True),
    sa.Column('languages', _JSON, nullable=True),
    sa.Column('accents', _JSON, nullable=True),
    sa.Column('gender', sa.String(length=20), nullable=True),
    sa.Column('genres_narrated', _JSON, nullable=True),
    sa.Column('audiobooks_produced', sa.String(length=50), nullable=True),
    sa.Column('cultural_heritage', sa.Text(), nullable=True),
    sa.Column('publishers', _JSON, nullable=True),
    sa.Column('social_links', _JSON, nullable=True),
    sa.Column('audio_samples', _JSON, nullable=True),
    sa.Column('source', sa.Text(), nullable=True),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    # The hosted database carries this default on this one column (the models
    # do not); it is kept so an insert that leaves the column out lands the same.
    sa.Column('fetched_description', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('last_seeded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('series',
    sa.Column('asin', sa.String(length=12), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('region', _region(), nullable=True),
    sa.Column('fetched_description', sa.Boolean(), nullable=False),
    sa.Column('last_seeded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('audible_extras', _JSON, nullable=True),
    sa.Column('extras_withheld', _JSON, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('asin')
    )
    op.create_index('series_asin_index', 'series', ['asin'], unique=False)
    op.create_index('series_region_index', 'series', ['region'], unique=False)
    op.create_table('author_book',
    sa.Column('author_id', sa.Integer(), nullable=False),
    sa.Column('book_asin', sa.String(length=12), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['authors.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['book_asin'], ['books.asin'], ondelete='CASCADE'),
    sa.UniqueConstraint('author_id', 'book_asin', name='uq_author_book')
    )
    op.create_index('book_author_index', 'author_book', ['book_asin', 'author_id'], unique=False)
    op.create_table('author_genre',
    sa.Column('author_id', sa.Integer(), nullable=False),
    sa.Column('genre_asin', sa.String(length=12), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['authors.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['genre_asin'], ['genres.asin'], ondelete='CASCADE'),
    sa.UniqueConstraint('author_id', 'genre_asin', name='uq_author_genre')
    )
    op.create_index('author_genre_index', 'author_genre', ['genre_asin', 'author_id'], unique=False)
    op.create_index('genre_author_index', 'author_genre', ['author_id', 'genre_asin'], unique=False)
    op.create_table('book_genre',
    sa.Column('book_asin', sa.String(length=12), nullable=False),
    sa.Column('genre_asin', sa.String(length=12), nullable=False),
    sa.ForeignKeyConstraint(['book_asin'], ['books.asin'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['genre_asin'], ['genres.asin'], ondelete='CASCADE'),
    sa.UniqueConstraint('book_asin', 'genre_asin', name='uq_book_genre')
    )
    op.create_index('book_genre_index', 'book_genre', ['book_asin', 'genre_asin'], unique=False)
    op.create_index('genre_book_index', 'book_genre', ['genre_asin', 'book_asin'], unique=False)
    op.create_table('book_narrator',
    sa.Column('narrator_name', sa.Text(), nullable=False),
    sa.Column('book_asin', sa.String(length=12), nullable=False),
    sa.ForeignKeyConstraint(['book_asin'], ['books.asin'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['narrator_name'], ['narrators.name'], ondelete='CASCADE'),
    sa.UniqueConstraint('book_asin', 'narrator_name', name='uq_book_narrator')
    )
    op.create_index('book_narrator_index', 'book_narrator', ['book_asin', 'narrator_name'], unique=False)
    op.create_table('book_series',
    sa.Column('book_asin', sa.String(length=12), nullable=False),
    sa.Column('series_asin', sa.String(length=12), nullable=False),
    sa.Column('position', sa.String(length=100), nullable=True),
    sa.ForeignKeyConstraint(['book_asin'], ['books.asin'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['series_asin'], ['series.asin'], ondelete='CASCADE'),
    sa.UniqueConstraint('book_asin', 'series_asin', name='uq_book_series')
    )
    op.create_index('book_series_index', 'book_series', ['book_asin', 'series_asin'], unique=False)
    op.create_table('series_author',
    sa.Column('series_asin', sa.String(length=12), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['authors.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['series_asin'], ['series.asin'], ondelete='CASCADE'),
    sa.UniqueConstraint('series_asin', 'author_id', name='uq_series_author')
    )
    op.create_index('author_series_index', 'series_author', ['author_id', 'series_asin'], unique=False)
    op.create_index('series_author_index', 'series_author', ['series_asin', 'author_id'], unique=False)
    op.create_table('tracks',
    sa.Column('asin', sa.String(length=12), nullable=False),
    sa.Column('chapters', _JSON, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['asin'], ['books.asin'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('asin')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('tracks')
    op.drop_table('series_author')
    op.drop_table('book_series')
    op.drop_table('book_narrator')
    op.drop_table('book_genre')
    op.drop_table('author_genre')
    op.drop_table('author_book')
    op.drop_table('series')
    op.drop_table('narrators')
    op.drop_table('genres')
    op.drop_table('books')
    op.drop_table('authors')
    _postgres_types(create=False)
