"""folded search indexes for the ranked /db search

Revision ID: e6a1d4f93b27
Revises: a8c3e51f7d29
Create Date: 2026-11-06

The ranked search matches text lowercased and with accents removed, so it needs
that folding as an indexable expression. This adds the unaccent extension, the
function public.libex_fold that applies it, and the indexes the search reads:
trigram (GIN) indexes over the folded book title and subtitle, author name,
series title and narrator name, btrees over the folded author name and series
title for exact matches, the two popularity orders on books, and a btree on
book_narrator.narrator_name. The raw-column indexes from a8c3e51f7d29 are left
as they are; /db/book still reads them.

public.libex_fold is created here, never replaced. Every one of these indexes
depends on it, and replacing a function an index was built from would leave the
index describing the old one. If a function of that name already exists it is
checked against the definition below, owner included, and the upgrade raises on
any difference instead of adopting it. The same goes for an index that already
exists under one of these names: its definition must be the expected one.
unaccent has to live in the public schema, where the function and the queries
name it.

Each index is built CONCURRENTLY in an autocommit block, for the reasons
c4f7a2e9d1b3 gives. There is deliberately no lock_timeout, and an invalid
leftover from an interrupted build is dropped before the build. The tables are
analyzed afterwards so the planner has statistics for the new expressions.

Downgrade drops the indexes and the function, and keeps both extensions:
dropping unaccent would break every write to the four tables if anything still
used the function, and dropping it needs ownership the migration role does not
necessarily hold. Nothing here uses CASCADE.

Applying the same DDL out of band ahead of the deploy is safe; the upgrade then
finds each object in place and checks it.
"""
import logging
import time
from typing import Sequence, Union

from alembic import context, op
from sqlalchemy import text


revision: str = 'e6a1d4f93b27'
down_revision: Union[str, Sequence[str], None] = 'a8c3e51f7d29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The body is compared character for character against an existing function.
# Copies live in libex_core's models and in the core chain's revision, and a
# test holds them identical.
FOLD_FUNCTION_BODY = "SELECT pg_catalog.lower(public.unaccent('public.unaccent'::regdictionary, $1))"
FOLD_FUNCTION_DDL = (
    "CREATE FUNCTION public.libex_fold(text) RETURNS text LANGUAGE sql "
    f"IMMUTABLE STRICT PARALLEL SAFE AS $${FOLD_FUNCTION_BODY}$$"
)
EXTENSION_DDL = 'CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public'
# True when public.unaccent and its dictionary are the extension's own.
UNACCENT_IS_THE_EXTENSION_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_ts_dict WHERE dictname = 'unaccent' AND dictnamespace = 'public'::regnamespace) AND EXISTS ("
    "SELECT 1 FROM pg_depend d JOIN pg_proc p ON p.oid = d.objid "
    "WHERE d.classid = 'pg_proc'::regclass AND d.refclassid = 'pg_extension'::regclass "
    "AND d.deptype = 'e' AND d.refobjid = (SELECT oid FROM pg_extension WHERE extname = 'unaccent') "
    "AND p.proname = 'unaccent' AND p.pronamespace = 'public'::regnamespace)"
)

# (index, table, what follows "ON <table> ", the definition Postgres reports for
# it with the public schema qualifier removed)
INDEXES = (
    (
        'ix_books_fold_title_gin', 'books',
        "USING gin (public.libex_fold(title || ' ' || coalesce(subtitle, '')) public.gin_trgm_ops)",
        "CREATE INDEX ix_books_fold_title_gin ON books USING gin "
        "(libex_fold(((title || ' '::text) || COALESCE(subtitle, ''::text))) gin_trgm_ops)",
    ),
    (
        'ix_authors_fold_name_gin', 'authors',
        'USING gin (public.libex_fold(name) public.gin_trgm_ops)',
        'CREATE INDEX ix_authors_fold_name_gin ON authors USING gin (libex_fold(name) gin_trgm_ops)',
    ),
    (
        'ix_series_fold_title_gin', 'series',
        'USING gin (public.libex_fold(title) public.gin_trgm_ops)',
        'CREATE INDEX ix_series_fold_title_gin ON series USING gin (libex_fold(title) gin_trgm_ops)',
    ),
    (
        'ix_narrators_fold_name_gin', 'narrators',
        'USING gin (public.libex_fold(name) public.gin_trgm_ops)',
        'CREATE INDEX ix_narrators_fold_name_gin ON narrators USING gin (libex_fold(name) gin_trgm_ops)',
    ),
    (
        'ix_authors_fold_name_bt', 'authors',
        '(public.libex_fold(name))',
        'CREATE INDEX ix_authors_fold_name_bt ON authors USING btree (libex_fold(name))',
    ),
    (
        'ix_series_fold_title_bt', 'series',
        '(public.libex_fold(title))',
        'CREATE INDEX ix_series_fold_title_bt ON series USING btree (libex_fold(title))',
    ),
    (
        'ix_books_fold_pop', 'books',
        '(num_ratings DESC NULLS LAST, asin, region) WHERE is_primary',
        'CREATE INDEX ix_books_fold_pop ON books USING btree '
        '(num_ratings DESC NULLS LAST, asin, region) WHERE is_primary',
    ),
    (
        'ix_books_fold_region_pop', 'books',
        '(region, num_ratings DESC NULLS LAST, asin)',
        'CREATE INDEX ix_books_fold_region_pop ON books USING btree '
        '(region, num_ratings DESC NULLS LAST, asin)',
    ),
    (
        'ix_book_narrator_fold_narrator_name', 'book_narrator',
        '(narrator_name)',
        'CREATE INDEX ix_book_narrator_fold_narrator_name ON book_narrator USING btree (narrator_name)',
    ),
)

# The tables the indexes sit on.
ANALYZED_TABLES = ('books', 'authors', 'series', 'narrators', 'book_narrator')

logger = logging.getLogger('alembic.runtime.migration')


def _scalar(sql: str):
    return op.get_bind().execute(text(sql)).scalar()


def _check_unaccent_schema() -> None:
    schema = _scalar(
        "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
        "WHERE e.extname = 'unaccent'"
    )
    if schema not in (None, 'public'):
        raise RuntimeError(
            f'unaccent is installed in the {schema} schema; public.libex_fold calls public.unaccent'
        )


def _check_unaccent_is_the_extension() -> None:
    """public.unaccent and its dictionary must be the extension's own."""
    ok = _scalar(UNACCENT_IS_THE_EXTENSION_SQL)
    if not ok:
        raise RuntimeError(
            'public.unaccent is not provided by the unaccent extension; public.libex_fold needs it'
        )


def _fold_function_problems() -> list[str]:
    """What differs between the existing libex_fold and the definition above.
    Empty when there is exactly one, and it matches."""
    functions = op.get_bind().execute(text(
        "SELECT p.prosrc, l.lanname, p.provolatile::text, p.proisstrict, p.proparallel::text, "
        "p.prorettype::regtype::text, p.prosecdef, p.proconfig IS NOT NULL, p.proretset, "
        "p.pronamespace::regnamespace::text, pg_get_function_identity_arguments(p.oid), "
        "p.proowner = (SELECT relowner FROM pg_class WHERE oid = 'public.books'::regclass) "
        "FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang WHERE p.proname = 'libex_fold'"
    )).all()
    if len(functions) != 1:
        return [f'{len(functions)} functions named libex_fold exist, expected exactly one']
    (body, lang, volatile, strict, parallel, returns, definer, has_config, is_set,
     namespace, args, same_owner) = functions[0]
    expected = {
        'body': (body, FOLD_FUNCTION_BODY),
        'language': (lang, 'sql'),
        'volatility': (volatile, 'i'),
        'strict': (strict, True),
        'parallel': (parallel, 's'),
        'return type': (returns, 'text'),
        'security definer': (definer, False),
        'a SET clause': (has_config, False),
        'returns a set': (is_set, False),
        'schema': (namespace, 'public'),
        'arguments': (args, 'text'),
        "the owner being books' owner": (same_owner, True),
    }
    return [f'{what}: found {found!r}, expected {want!r}' for what, (found, want) in expected.items() if found != want]


def _ensure_fold_function() -> None:
    if _scalar("SELECT count(*) FROM pg_proc WHERE proname = 'libex_fold'") == 0:
        op.execute(FOLD_FUNCTION_DDL)
        return
    problems = _fold_function_problems()
    if problems:
        raise RuntimeError(
            'an existing libex_fold does not match the definition this revision expects, and is '
            'not replaced: ' + '; '.join(problems)
        )


def _index_state(name: str):
    """(valid, definition with the public qualifier removed), or None when absent."""
    row = op.get_bind().execute(
        text('SELECT indisvalid, pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = to_regclass(:name)'),
        {'name': name},
    ).first()
    return None if row is None else (row[0], row[1].replace('public.', ''))


def upgrade() -> None:
    offline = context.is_offline_mode()
    if not offline:
        _check_unaccent_schema()
    with op.get_context().autocommit_block():
        op.execute(EXTENSION_DDL)
        if offline:
            op.execute(FOLD_FUNCTION_DDL)
        else:
            _check_unaccent_is_the_extension()
            _ensure_fold_function()
        for name, table, tail, expected in INDEXES:
            # Offline there is no database to ask, so the drop is unconditional.
            state = None if offline else _index_state(name)
            if offline or (state is not None and not state[0]):
                if not offline:
                    logger.warning('dropping the invalid leftover %s from an interrupted build', name)
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}')
                state = None
            elif state is not None and state[1] != expected:
                raise RuntimeError(
                    f'{name} already exists with a different definition and is not replaced: '
                    f'found {state[1]!r}, expected {expected!r}'
                )
            if state is None and not offline:
                logger.info('building %s on %s', name, table)
            started = time.monotonic()
            op.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {tail}')
            if state is None and not offline:
                logger.info('built %s in %.1fs', name, time.monotonic() - started)
        for table in ANALYZED_TABLES:
            op.execute(f'ANALYZE {table}')


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _tail, _expected in INDEXES:
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}')
        op.execute('DROP FUNCTION IF EXISTS public.libex_fold(text)')
