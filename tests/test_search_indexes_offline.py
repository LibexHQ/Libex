"""
The hosted search revision rendered as SQL, for a DBA to apply by hand: every
index is built CONCURRENTLY and IF NOT EXISTS, after a drop of any invalid
leftover, and the function is created with the exact text the checks expect.
"""

# Standard library
import io
from pathlib import Path

# Third party
from alembic import command
from alembic.config import Config

# Local
from libex_core.storage.models import FOLD_FUNCTION_DDL
from tests.libex_core.test_fold_function_text import HOSTED

ROOT = Path(__file__).resolve().parent.parent


def _render(action, span):
    config = Config(str(ROOT / "alembic.ini"))
    config.output_buffer = io.StringIO()
    getattr(command, action)(config, span, sql=True)
    return config.output_buffer.getvalue()


def test_the_upgrade_renders_every_build_as_concurrent_and_idempotent():
    sql = _render("upgrade", "a8c3e51f7d29:e6a1d4f93b27")
    assert FOLD_FUNCTION_DDL in sql
    assert "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public" in sql
    for name, table, tail, _ in HOSTED.INDEXES:
        build = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {tail}"
        assert build in sql
        assert f"DROP INDEX CONCURRENTLY IF EXISTS {name}" in sql
        # The drop of a leftover comes before the build.
        assert sql.index(f"DROP INDEX CONCURRENTLY IF EXISTS {name}") < sql.index(build)
    assert sql.index(FOLD_FUNCTION_DDL) < sql.index("CREATE INDEX CONCURRENTLY")
    assert "CREATE OR REPLACE" not in sql and "CASCADE" not in sql


def test_the_downgrade_drops_indexes_then_the_function_and_keeps_the_extensions():
    sql = _render("downgrade", "e6a1d4f93b27:a8c3e51f7d29")
    drops = [name for name, *_ in HOSTED.INDEXES]
    for name in drops:
        assert f"DROP INDEX CONCURRENTLY IF EXISTS {name}" in sql
    assert sql.index("DROP FUNCTION IF EXISTS public.libex_fold(text)") > max(
        sql.index(f"DROP INDEX CONCURRENTLY IF EXISTS {n}") for n in drops
    )
    assert "DROP EXTENSION" not in sql and "CASCADE" not in sql
