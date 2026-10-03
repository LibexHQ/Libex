"""
Scratch databases at an older revision, for tests of what runs while the
schema is still that old.

The container the integration suite shares sits at the current head. The
region-key script and the revision that adopts its work are about the schema
before that revision, which the head no longer has, so they clone a database
from a template built at the revision before it.
"""

# Standard library
import os
import uuid
from pathlib import Path

# Third party
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# Local
from app.core import config as app_config

ROOT = Path(__file__).resolve().parent.parent.parent


def urls(database: str) -> tuple[str, str]:
    """The sync and async URLs of `database` on the shared container."""
    url = make_url(os.environ["DATABASE_URL"]).set(database=database)
    return (
        url.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False),
        url.render_as_string(hide_password=False),
    )


def admin():
    return create_engine(urls("postgres")[0], isolation_level="AUTOCOMMIT")


def run_alembic(async_url: str, action: str, target: str) -> None:
    """Runs one alembic command against `async_url`. env.py reads the database
    from settings, so the settings are pointed there for the duration."""
    saved = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = async_url
    app_config.get_settings.cache_clear()
    try:
        config = Config(str(ROOT / "alembic.ini"))
        getattr(command, action)(config, target)
    finally:
        os.environ["DATABASE_URL"] = saved
        app_config.get_settings.cache_clear()


def build_template(revision: str) -> str:
    """Creates a database upgraded to `revision` and returns its name."""
    name = f"rk_template_{uuid.uuid4().hex[:8]}"
    engine = admin()
    with engine.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()
    run_alembic(urls(name)[1], "upgrade", revision)
    return name


def drop_database(name: str) -> None:
    engine = admin()
    with engine.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    engine.dispose()


def clone(template: str) -> tuple[str, str, str]:
    """A fresh database cloned from `template`: (name, sync url, async url)."""
    name = f"rk_{uuid.uuid4().hex[:10]}"
    engine = admin()
    with engine.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    engine.dispose()
    sync_url, async_url = urls(name)
    return name, sync_url, async_url
