"""
Dump and verify against a real postgres:16, with the real binaries.

Three things here cannot be established by a mock, and each of them has
already been wrong once somewhere:

  1. THE ARCHIVE IS ONE pg_restore CAN ACTUALLY READ. A mocked
     create_subprocess_exec proves the argv and nothing about whether the
     bytes pg_dump produced parse -- and a version mismatch between the
     pg_dump that writes and the pg_restore that reads is precisely the
     class of failure verification exists to catch.

  2. THE PER-TABLE AUTOVACUUM RELOPTIONS SURVIVE A RESTORE. books and tracks
     carry autovacuum_vacuum_scale_factor = 0.02 and
     autovacuum_vacuum_insert_scale_factor = 0.02, set by migration
     c7a4e9f13b02 after the outage that made them necessary. If they do not
     survive, a restored database silently reverts to the default 0.2 and
     regresses to the configuration that caused it -- and nothing would say
     so. Settled here by reading pg_class.reloptions on the restored
     database rather than by reasoning about the archive format.

  3. WHAT VERIFICATION DOES NOT PROVE. pg_restore --list reads the header
     and the table of contents; it never decompresses a data block. A
     halved archive whose TOC survived the cut still verifies -- measured
     below, deliberately, so that no future reader can mistake "verified"
     for "restorable". The only thing that proves a restore works is a
     restore, which is why this file does one.

Marked integration and skipped when the client binaries are absent: the
whole point is the real pg_dump, so a mocked fallback would be a green test
asserting nothing.
"""

# Standard library
import os
import shutil
import subprocess
from datetime import datetime, timezone

# Third party
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Local
from app.db import models
from app.services.backup.artifact import BackupArtifact
from app.services.backup.dump import (
    VerificationError,
    _parse_toc,
    parse_database_url,
    run_pg_dump,
    verify_artifact,
)


pytestmark = pytest.mark.integration


def _tools_present() -> bool:
    return bool(shutil.which("pg_dump")) and bool(shutil.which("pg_restore"))


if not _tools_present():
    pytest.skip(
        "pg_dump/pg_restore not on PATH — skipping the real dump round trip",
        allow_module_level=True,
    )


CREATED_AT = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)

# The reloptions migration c7a4e9f13b02 sets, and the tables it sets them on.
RELOPTIONS = {"autovacuum_vacuum_scale_factor=0.02", "autovacuum_vacuum_insert_scale_factor=0.02"}
RELOPTION_TABLES = ("books", "tracks")


def _target():
    return parse_database_url(os.environ["DATABASE_URL"])


@pytest.fixture
def spool(tmp_path):
    return str(tmp_path)


@pytest.fixture
async def artifact(spool):
    """A real custom-format dump of the migrated container database."""
    return await run_pg_dump(_target(), spool, CREATED_AT, 300.0)


@pytest.fixture
async def padded_artifact(spool):
    """
    A dump whose data blocks are much larger than its table of contents,
    which is what the live archive looks like -- 2.38 GB of data behind a
    TOC of a few hundred entries.

    The padding is real rows in a real table, created and dropped around the
    dump. Without it the container's schema-only archive is mostly TOC, and
    a test that cuts it in half would be cutting the TOC rather than the
    data -- a different failure, and the one this file already covers
    separately.
    """
    target = _target()
    await _execute(
        target.dbname,
        "CREATE TABLE backup_dump_padding AS "
        "SELECT g AS id, repeat('x', 400) AS filler FROM generate_series(1, 20000) g",
    )
    try:
        yield await run_pg_dump(target, spool, CREATED_AT, 300.0)
    finally:
        await _execute(target.dbname, "DROP TABLE IF EXISTS backup_dump_padding")


async def test_pg_dump_produces_an_archive_this_pg_restore_can_read(artifact):
    result = await verify_artifact(artifact)

    assert result.has_alembic_version is True
    assert result.missing_tables == []
    assert result.size_bytes > 0


async def test_every_table_the_schema_declares_is_in_the_archive(artifact):
    """
    Derived from Base.metadata rather than from a count, so it stays true
    the day a migration adds a table. The list is also the assertion that
    the migrated container and the models agree, which is what makes the
    dump worth verifying at all.
    """
    listing = subprocess.run(
        ["pg_restore", "--list", artifact.path], capture_output=True, text=True, check=True
    )
    _, tables = _parse_toc(listing.stdout)

    assert set(models.Base.metadata.tables) <= tables
    assert "alembic_version" in tables


async def test_a_truncated_archive_fails_verification_when_the_cut_reaches_the_toc(artifact, spool):
    """
    The failure verification genuinely does catch: a dump cut short by a
    full disk or a killed child, where the table of contents itself did not
    survive.
    """
    truncated_path = os.path.join(spool, "truncated.dump")
    with open(artifact.path, "rb") as source:
        head = source.read(512)
    with open(truncated_path, "wb") as sink:
        sink.write(head)

    truncated = BackupArtifact(
        name="libex-20260906T030000Z.dump", path=truncated_path, created_at=CREATED_AT, size_bytes=len(head)
    )

    with pytest.raises(VerificationError):
        await verify_artifact(truncated)


async def test_a_halved_archive_still_verifies_which_is_the_limit_of_this_check(padded_artifact, spool):
    """
    MEASURED, NOT THEORISED, and the reason no docstring in this package may
    claim verification proves a restore. pg_restore --list reads the header
    and the TOC, both of which sit at the front of a custom-format archive,
    so half the data blocks can be missing and the check still exits 0 with
    every table named.

    This test is here to fail if anyone ever "strengthens" the wording
    around verification without strengthening the check. What would actually
    prove the data is intact is a restore, which the next test does.
    """
    halved_path = os.path.join(spool, "halved.dump")
    size = os.path.getsize(padded_artifact.path)
    with open(padded_artifact.path, "rb") as source:
        head = source.read(size // 2)
    with open(halved_path, "wb") as sink:
        sink.write(head)

    halved = BackupArtifact(
        name="libex-20260906T030000Z.dump", path=halved_path, created_at=CREATED_AT, size_bytes=len(head)
    )

    result = await verify_artifact(halved)

    assert result.has_alembic_version is True
    assert result.missing_tables == []


async def test_the_autovacuum_reloptions_survive_a_real_restore(artifact):
    """
    The question db-reviewer raised, settled by measurement rather than by
    reading the archive format. books and tracks carry 0.02 vacuum
    thresholds because the defaults caused an outage; a restore that
    silently dropped them would put the restored database back into the
    configuration that caused it, with nothing anywhere reporting the
    change.
    """
    restored = await _restore_into("libex_restore_reloptions", artifact.path)

    rows = await _query(restored, "SELECT relname, reloptions FROM pg_class WHERE relname = ANY(:names)", {"names": list(RELOPTION_TABLES)})
    reloptions = {row[0]: row[1] for row in rows}

    for table in RELOPTION_TABLES:
        assert reloptions[table] is not None, f"{table} lost its storage parameters in the restore"
        assert RELOPTIONS <= set(reloptions[table])


async def test_the_restored_database_holds_the_tables_the_schema_declares(artifact):
    """
    The thing --list cannot tell you. A restore is the only check that
    touches the data blocks, and it is cheap enough here to be worth doing
    once.
    """
    restored = await _restore_into("libex_restore_tables", artifact.path)

    rows = await _query(restored, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    tables = {row[0] for row in rows}

    assert set(models.Base.metadata.tables) <= tables
    assert "alembic_version" in tables


# ============================================================
# RESTORING INTO A DATABASE OF OUR OWN
# ============================================================

def _url_for(dbname):
    target = _target()
    return f"postgresql+asyncpg://{target.user}:{target.password}@{target.host}:{target.port}/{dbname}"


async def _restore_into(dbname, path):
    """
    A fresh database, then a real pg_restore into it. CREATE DATABASE cannot
    run inside a transaction block, so the admin statements go through an
    autocommit connection of their own rather than through the session
    fixture's.
    """
    target = _target()
    admin = create_async_engine(_url_for(target.dbname), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
            await connection.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await admin.dispose()

    completed = subprocess.run(
        [
            "pg_restore",
            "--no-password",
            "--host", target.host,
            "--port", str(target.port),
            "--username", target.user,
            "--dbname", dbname,
            path,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PGPASSWORD": target.password},
    )
    assert completed.returncode == 0, completed.stderr
    return dbname


async def _execute(dbname, sql):
    """One statement on its own autocommit connection."""
    engine = create_async_engine(_url_for(dbname), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(sql))
    finally:
        await engine.dispose()


async def _query(dbname, sql, params=None):
    engine = create_async_engine(_url_for(dbname))
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(sql), params or {})
            return result.fetchall()
    finally:
        await engine.dispose()
