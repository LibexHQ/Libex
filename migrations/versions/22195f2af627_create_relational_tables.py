"""create relational tables

Revision ID: 22195f2af627
Revises: 9203c248b749
Create Date: 2026-04-12 21:21:45.633139

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '22195f2af627'
down_revision: Union[str, Sequence[str], None] = '9203c248b749'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create enum types safely — no-op if they already exist
    op.execute("DO $$ BEGIN CREATE TYPE region_enum AS ENUM ('us', 'ca', 'uk', 'au', 'fr', 'de', 'jp', 'it', 'in', 'es', 'br'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
    op.execute("DO $$ BEGIN CREATE TYPE genre_type_enum AS ENUM ('Genres', 'Tags'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;")

    # Get existing tables so we can skip already-created ones (idempotent for partial deployments)
    conn = op.get_bind()
    existing = conn.execute(sa.text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).fetchall()
    existing_tables = {row[0] for row in existing}

    if 'authors' not in existing_tables:
        op.create_table('authors',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('asin', sa.String(length=12), nullable=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('region', postgresql.ENUM('us', 'ca', 'uk', 'au', 'fr', 'de', 'jp', 'it', 'in', 'es', 'br', name='region_enum', create_type=False), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('image', sa.Text(), nullable=True),
        sa.Column('fetched_description', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('asin', 'region', 'name', name='authors_asin_region_name_unique')
        )
        op.create_index('authors_asin_region_name_index', 'authors', ['asin', 'region', 'name'], unique=False)
        op.create_index('authors_region_name_index', 'authors', ['region', 'name'], unique=False)

    if 'books' not in existing_tables:
        op.create_table('books',
        sa.Column('asin', sa.String(length=12), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('subtitle', sa.Text(), nullable=True),
        sa.Column('region', postgresql.ENUM('us', 'ca', 'uk', 'au', 'fr', 'de', 'jp', 'it', 'in', 'es', 'br', name='region_enum', create_type=False), nullable=False),
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
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('asin')
        )
        op.create_index('books_asin_index', 'books', ['asin'], unique=False)

    if 'genres' not in existing_tables:
        op.create_table('genres',
        sa.Column('asin', sa.String(length=12), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('type', postgresql.ENUM('Genres', 'Tags', name='genre_type_enum', create_type=False), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('asin')
        )

    if 'narrators' not in existing_tables:
        op.create_table('narrators',
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('name')
        )

    if 'series' not in existing_tables:
        op.create_table('series',
        sa.Column('asin', sa.String(length=12), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('region', postgresql.ENUM('us', 'ca', 'uk', 'au', 'fr', 'de', 'jp', 'it', 'in', 'es', 'br', name='region_enum', create_type=False), nullable=True),
        sa.Column('fetched_description', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('asin')
        )
        op.create_index('series_asin_index', 'series', ['asin'], unique=False)

    if 'author_book' not in existing_tables:
        op.create_table('author_book',
        sa.Column('author_id', sa.Integer(), nullable=False),
        sa.Column('book_asin', sa.String(length=12), nullable=False),
        sa.ForeignKeyConstraint(['author_id'], ['authors.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['book_asin'], ['books.asin'], ondelete='CASCADE')
        )
        op.create_index('book_author_index', 'author_book', ['book_asin', 'author_id'], unique=False)

    if 'author_genre' not in existing_tables:
        op.create_table('author_genre',
        sa.Column('author_id', sa.Integer(), nullable=False),
        sa.Column('genre_asin', sa.String(length=12), nullable=False),
        sa.ForeignKeyConstraint(['author_id'], ['authors.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['genre_asin'], ['genres.asin'], ondelete='CASCADE')
        )
        op.create_index('author_genre_index', 'author_genre', ['genre_asin', 'author_id'], unique=False)
        op.create_index('genre_author_index', 'author_genre', ['author_id', 'genre_asin'], unique=False)

    if 'book_genre' not in existing_tables:
        op.create_table('book_genre',
        sa.Column('book_asin', sa.String(length=12), nullable=False),
        sa.Column('genre_asin', sa.String(length=12), nullable=False),
        sa.ForeignKeyConstraint(['book_asin'], ['books.asin'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['genre_asin'], ['genres.asin'], ondelete='CASCADE')
        )
        op.create_index('book_genre_index', 'book_genre', ['book_asin', 'genre_asin'], unique=False)
        op.create_index('genre_book_index', 'book_genre', ['genre_asin', 'book_asin'], unique=False)

    if 'book_narrator' not in existing_tables:
        op.create_table('book_narrator',
        sa.Column('narrator_name', sa.Text(), nullable=False),
        sa.Column('book_asin', sa.String(length=12), nullable=False),
        sa.ForeignKeyConstraint(['book_asin'], ['books.asin'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['narrator_name'], ['narrators.name'], ondelete='CASCADE')
        )
        op.create_index('book_narrator_index', 'book_narrator', ['book_asin', 'narrator_name'], unique=False)

    if 'book_series' not in existing_tables:
        op.create_table('book_series',
        sa.Column('book_asin', sa.String(length=12), nullable=False),
        sa.Column('series_asin', sa.String(length=12), nullable=False),
        sa.Column('position', sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(['book_asin'], ['books.asin'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['series_asin'], ['series.asin'], ondelete='CASCADE')
        )
        op.create_index('book_series_index', 'book_series', ['book_asin', 'series_asin'], unique=False)

    if 'tracks' not in existing_tables:
        op.create_table('tracks',
        sa.Column('asin', sa.String(length=12), nullable=False),
        sa.Column('chapters', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['asin'], ['books.asin'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('asin')
        )

    # Drop cache index only if it exists — may not exist on older deployments
    op.drop_index(op.f('ix_cache_expires_at'), table_name='cache', if_exists=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('tracks')
    op.drop_index('book_series_index', table_name='book_series')
    op.drop_table('book_series')
    op.drop_index('book_narrator_index', table_name='book_narrator')
    op.drop_table('book_narrator')
    op.drop_index('genre_book_index', table_name='book_genre')
    op.drop_index('book_genre_index', table_name='book_genre')
    op.drop_table('book_genre')
    op.drop_index('genre_author_index', table_name='author_genre')
    op.drop_index('author_genre_index', table_name='author_genre')
    op.drop_table('author_genre')
    op.drop_index('book_author_index', table_name='author_book')
    op.drop_table('author_book')
    op.drop_index('series_asin_index', table_name='series')
    op.drop_table('series')
    op.drop_table('narrators')
    op.drop_table('genres')
    op.drop_index('books_asin_index', table_name='books')
    op.drop_table('books')
    op.drop_index('authors_region_name_index', table_name='authors')
    op.drop_index('authors_asin_region_name_index', table_name='authors')
    op.drop_table('authors')
    op.execute("DROP TYPE IF EXISTS region_enum")
    op.execute("DROP TYPE IF EXISTS genre_type_enum")