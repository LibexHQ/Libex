"""folded search indexes for the ranked search

Revision ID: c8d2a5e71f39
Revises: b5e9c3a7f184
Create Date: 2026-11-06

The same objects, under the same names and definitions, as the hosted chain's
e6a1d4f93b27 (the chains must build one schema): the unaccent extension, the
function public.libex_fold that lowercases and removes accents, trigram indexes
over the folded book title and subtitle, author name, series title and narrator
name, btrees over the folded author name and series title for exact matches, the
two popularity orders on books, and a btree on book_narrator.narrator_name. The
raw-column indexes from b5e9c3a7f184 are left as they are. Postgres only: the
revision does nothing on SQLite.

This chain runs in one transaction under an advisory lock, so the builds cannot
be CONCURRENTLY: each CREATE INDEX takes a SHARE lock on its table for the
length of the build, and reads carry on while writes to that table wait.

pg_trgm and unaccent must both live in the public schema, where the function and
the search queries name them. What the role cannot do is checked first and
raises with the command a privileged role must run, so the transaction rolls
back with no revision stamped. Run the function's command as the role that owns
the tables: an existing public.libex_fold is checked against the definition below,
owner included, and is never replaced.

Downgrade drops the indexes and the function and keeps both extensions, which
other objects may depend on and which dropping needs ownership of. Nothing here
uses CASCADE.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = 'c8d2a5e71f39'
down_revision: Union[str, Sequence[str], None] = 'b5e9c3a7f184'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The body is compared character for character against an existing function.
# Copies live in libex_core's models and in the hosted chain's revision, and a
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


def _scalar(sql: str):
    return op.get_bind().execute(text(sql)).scalar()


def _extension_schema(name: str):
    return _scalar(
        "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
        f"WHERE e.extname = '{name}'"
    )


def _check_extensions() -> None:
    """pg_trgm and unaccent installed in public, unaccent created if the role
    can and raising with the command if it cannot."""
    trgm = _extension_schema('pg_trgm')
    if trgm != 'public':
        raise RuntimeError(
            f'the search indexes use public.gin_trgm_ops, but pg_trgm is {"not installed" if trgm is None else f"installed in the {trgm} schema"}'
        )
    installed = _extension_schema('unaccent')
    if installed not in (None, 'public'):
        raise RuntimeError(
            f'unaccent is installed in the {installed} schema; public.libex_fold calls public.unaccent'
        )
    if installed is None:
        can_create = _scalar(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'unaccent') "
            "AND has_database_privilege(current_user, current_database(), 'CREATE') "
            "AND has_schema_privilege(current_user, 'public', 'CREATE')"
        )
        if not can_create:
            raise RuntimeError(
                'the search indexes need the unaccent extension, which is not installed and '
                'cannot be created by this role; a privileged role must run: ' + EXTENSION_DDL
            )
        op.execute(EXTENSION_DDL)
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
        if not _scalar("SELECT has_schema_privilege(current_user, 'public', 'CREATE')"):
            raise RuntimeError(
                'the search indexes need the function public.libex_fold, which cannot be created '
                'by this role; the role that owns the tables must run: ' + FOLD_FUNCTION_DDL
            )
        op.execute(FOLD_FUNCTION_DDL)
        return
    problems = _fold_function_problems()
    if problems:
        raise RuntimeError(
            'an existing libex_fold does not match the definition this revision expects, and is '
            'not replaced: ' + '; '.join(problems)
        )


def _index_definition(name: str):
    """The definition of an existing index with the public qualifier removed,
    or None when there is none."""
    row = op.get_bind().execute(
        text('SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = to_regclass(:name)'),
        {'name': name},
    ).first()
    return None if row is None else row[0].replace('public.', '')


def _index_is_usable(name: str) -> bool:
    """False for an index a failed build left behind (not valid or not ready)."""
    return bool(_scalar(
        f"SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid = to_regclass('{name}')"
    ))


def upgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    _check_extensions()
    _ensure_fold_function()
    for name, table, tail, expected in INDEXES:
        found = _index_definition(name)
        if found is None:
            op.execute(f'CREATE INDEX {name} ON {table} {tail}')
        elif found != expected:
            raise RuntimeError(
                f'{name} already exists with a different definition and is not replaced: '
                f'found {found!r}, expected {expected!r}'
            )
        elif not _index_is_usable(name):
            raise RuntimeError(
                f'{name} already exists but is not valid and ready, as a failed build leaves it; '
                'drop it and run the upgrade again'
            )
    for table in ANALYZED_TABLES:
        op.execute(f'ANALYZE {table}')


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    for name, _table, _tail, _expected in INDEXES:
        op.execute(f'DROP INDEX IF EXISTS {name}')
    op.execute('DROP FUNCTION IF EXISTS public.libex_fold(text)')
