"""
Dump, precheck and verification tests.

No real pg_dump and no real database here -- the child process is replaced
at dump.py's own import location, so what is under test is the argv, the
child environment, the spool handling and the parsing. The real binaries
run against a real postgres:16 in
tests/integration/test_backup_dump_roundtrip.py, which is also where the
honest limit of verification is measured.

BE PRECISE ABOUT WHAT VERIFICATION PROVES, here and in every docstring
below. pg_restore --list reads the archive header and the table of
contents. It does not decompress a data block, so a truncated archive whose
header and TOC survive passes it -- measured, not theorised. What these
tests pin is the three things it genuinely establishes: pg_restore exits 0,
alembic_version is present BY NAME, and every table Base.metadata declares
is named in the TOC, derived from the metadata rather than compared against
a count that stops being true the day a migration adds a table.

The _parse_toc sample is real pg_restore --list output captured from a
custom-format dump of a postgres:16 database, not a hand-written
approximation -- the two-word DESC on TABLE DATA entries is the wrinkle
that a hand-written sample gets wrong.
"""

# Standard library
import asyncio
import os
import stat
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch

# Third party
import pytest

# Local
from app.db import models
from app.services.backup.artifact import BackupArtifact
from app.services.backup.dump import (
    SPOOL_LOCK_FILENAME,
    DumpAborted,
    DumpError,
    DumpTarget,
    PrecheckError,
    SpoolBusy,
    VerificationError,
    _child_env,
    _decode,
    _parse_toc,
    _read_capped,
    acquire_spool_lock,
    check_free_space,
    clear_spool,
    create_spool_file,
    discard_spool_file,
    parse_database_url,
    release_spool_lock,
    run_pg_dump,
    verify_artifact,
)


CREATED_AT = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)
ARTIFACT_NAME = "libex-20260906T030000Z.dump"

# Captured verbatim from `pg_restore --list` against a custom-format dump of
# a postgres:16 database holding books, tracks and alembic_version. Kept as
# real output rather than a summary: the entries this parser has to ignore
# are as much of the format as the ones it reads.
REAL_TOC = """;
; Archive created at 2026-09-06 01:10:28 MDT
;     dbname: libex
;     TOC Entries: 18
;     Compression: gzip
;     Dump Version: 1.15-0
;     Format: CUSTOM
;     Integer: 4 bytes
;     Offset: 8 bytes
;     Dumped from database version: 16.14 (Debian 16.14-1.pgdg13+1)
;     Dumped by pg_dump version: 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
;
;
; Selected TOC Entries:
;
218; 1259 16399 TABLE public alembic_version postgres
215; 1259 16385 TABLE public books postgres
217; 1259 16393 TABLE public tracks postgres
216; 1259 16392 SEQUENCE public tracks_id_seq postgres
3436; 0 0 SEQUENCE OWNED BY public tracks_id_seq postgres
3275; 2604 16396 DEFAULT public tracks id postgres
3429; 0 16399 TABLE DATA public alembic_version postgres
3426; 0 16385 TABLE DATA public books postgres
3428; 0 16393 TABLE DATA public tracks postgres
3437; 0 0 SEQUENCE SET public tracks_id_seq postgres
3282; 2606 16403 CONSTRAINT public alembic_version alembic_version_pkey postgres
3277; 2606 16391 CONSTRAINT public books books_pkey postgres
3280; 2606 16398 CONSTRAINT public tracks tracks_pkey postgres
3278; 1259 16404 INDEX public ix_books_title postgres
"""


def _toc_for(tables):
    """A TOC naming exactly `tables`, in the real format: a definition entry
    and a data entry for each, plus entries this parser must ignore."""
    lines = ["; Selected TOC Entries:", ";"]
    for index, table in enumerate(sorted(tables)):
        lines.append(f"{200 + index}; 1259 {16000 + index} TABLE public {table} postgres")
        lines.append(f"{3000 + index}; 0 {16000 + index} TABLE DATA public {table} postgres")
        lines.append(f"{3500 + index}; 2606 {17000 + index} CONSTRAINT public {table} {table}_pkey postgres")
    return "\n".join(lines) + "\n"


def _complete_toc():
    """Everything Base.metadata declares, plus alembic_version -- which is
    NOT in the metadata, because alembic creates and owns it."""
    return _toc_for(set(models.Base.metadata.tables) | {"alembic_version"})


class _FakeStream:
    """A pipe that yields `data` then EOF, in chunks, the way a real one
    does."""

    def __init__(self, data: bytes):
        self._data = data

    async def read(self, size: int) -> bytes:
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


class _FakeProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self._returncode = returncode
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.returncode = None
        self.terminated = False
        self.killed = False

    async def wait(self):
        self.returncode = self._returncode
        return self._returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


# ============================================================
# PARSING THE DATABASE URL
# ============================================================

def test_a_percent_encoded_password_is_decoded():
    """
    urlsplit().password does not decode. Without unquote() a password with
    any reserved character in it authenticates with the wrong string, and
    what the operator sees is "password authentication failed" -- which
    reads exactly like a wrong password and sends them to change it in the
    one place it was already right.
    """
    target = parse_database_url("postgresql+asyncpg://libex:p%40ss%2Fword@db:5432/libex")

    assert target.password == "p@ss/word"


def test_the_username_and_database_name_are_decoded_too():
    target = parse_database_url("postgresql+asyncpg://lib%2Bex:pw@db:5432/libex%20prod")

    assert target.user == "lib+ex"
    assert target.dbname == "libex prod"


def test_the_driver_suffix_is_dropped():
    """pg_dump does not understand postgresql+asyncpg at all, and only the
    scheme carries it."""
    target = parse_database_url("postgresql+asyncpg://libex:pw@db:5432/libex")

    assert target == DumpTarget(host="db", port=5432, user="libex", dbname="libex", password="pw")


def test_a_url_without_a_port_gets_the_postgres_default():
    target = parse_database_url("postgresql+asyncpg://libex:pw@db/libex")

    assert target.port == 5432


def test_a_url_with_no_host_is_a_precheck_failure():
    with pytest.raises(PrecheckError):
        parse_database_url("postgresql+asyncpg:///libex")


def test_a_url_naming_no_database_is_a_precheck_failure():
    with pytest.raises(PrecheckError):
        parse_database_url("postgresql+asyncpg://libex:pw@db:5432/")


def test_the_password_is_kept_apart_from_the_rest_of_the_target():
    """A separate field so that no code path can format it into a command
    line by accident."""
    target = parse_database_url("postgresql+asyncpg://libex:hunter2@db:5432/libex")

    rendered = f"{target.host}{target.port}{target.user}{target.dbname}"
    assert "hunter2" not in rendered


# ============================================================
# READING THE TABLE OF CONTENTS
# ============================================================

def test_parse_toc_finds_the_tables_in_real_pg_restore_output():
    entries, tables = _parse_toc(REAL_TOC)

    assert tables == {"books", "tracks", "alembic_version"}
    assert entries == 14


def test_parse_toc_reads_both_the_definition_and_the_data_entry():
    """DESC is two words for table data and one for the definition, which is
    the only wrinkle in the format. Either kind counts as the table being
    present."""
    definition_only = "215; 1259 16385 TABLE public books postgres\n"
    data_only = "3426; 0 16385 TABLE DATA public books postgres\n"

    assert _parse_toc(definition_only)[1] == {"books"}
    assert _parse_toc(data_only)[1] == {"books"}


def test_parse_toc_ignores_everything_that_is_not_a_table():
    """A sequence, a default, a constraint and an index all carry a table
    name in a position a looser parser would read as one."""
    _, tables = _parse_toc(REAL_TOC)

    assert "tracks_id_seq" not in tables
    assert "books_pkey" not in tables
    assert "ix_books_title" not in tables


def test_parse_toc_ignores_comments_and_blank_lines():
    entries, tables = _parse_toc("; Archive created at 2026-09-06\n\n;     TOC Entries: 18\n")

    assert (entries, tables) == (0, set())


def test_parse_toc_ignores_a_line_that_does_not_start_with_a_dump_id():
    """The output of a child process is not a format this package controls,
    so a line it cannot read is skipped rather than guessed at."""
    entries, tables = _parse_toc("pg_restore: warning: something happened\n")

    assert (entries, tables) == (0, set())


def test_parse_toc_handles_empty_output():
    assert _parse_toc("") == (0, set())


# ============================================================
# VERIFICATION
# ============================================================

async def _verify(toc, returncode=0, stderr=b"", previous_size_bytes=0, size_bytes=2_376_454_048):
    artifact = BackupArtifact(
        name=ARTIFACT_NAME, path=f"/spool/{ARTIFACT_NAME}", created_at=CREATED_AT, size_bytes=size_bytes
    )
    process = _FakeProcess(returncode=returncode, stdout=toc.encode(), stderr=stderr)

    async def fake_exec(*argv, **kwargs):
        fake_exec.argv = argv
        fake_exec.kwargs = kwargs
        return process

    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", fake_exec):
        result = await verify_artifact(artifact, previous_size_bytes)
    return result, fake_exec


@pytest.mark.asyncio
async def test_a_complete_archive_verifies():
    result, _ = await _verify(_complete_toc())

    assert result.has_alembic_version is True
    assert result.missing_tables == []
    assert result.size_bytes == 2_376_454_048


@pytest.mark.asyncio
async def test_the_expected_tables_are_derived_from_the_metadata():
    """
    Not a hardcoded count and not a hardcoded list. The day a migration adds
    a table, this assertion covers it without an edit -- which is the whole
    reason verify_artifact reads Base.metadata rather than a number.
    """
    result, _ = await _verify(_complete_toc())

    assert result.tables_found == len(set(models.Base.metadata.tables) | {"alembic_version"})
    assert set(models.Base.metadata.tables)


@pytest.mark.asyncio
async def test_a_missing_table_fails_verification_and_names_it():
    """The names in the error are ours, out of our own metadata -- nothing
    in that field came from the archive or from the network."""
    declared = sorted(models.Base.metadata.tables)
    absent = declared[0]
    toc = _toc_for((set(declared) - {absent}) | {"alembic_version"})

    with pytest.raises(VerificationError) as raised:
        await _verify(toc)

    assert raised.value.fields["missing_count"] == 1
    assert raised.value.fields["missing_tables"] == absent


@pytest.mark.asyncio
async def test_a_missing_alembic_version_fails_even_though_every_other_table_is_there():
    """
    BY NAME, never by counting. alembic_version is not in Base.metadata --
    alembic creates and owns it -- so an archive without it has exactly the
    number of tables the schema declares and is still unusable: the restore
    lands the data in a database that cannot tell Alembic where it stands,
    and every later `alembic upgrade head` either replays migrations or
    refuses to start.
    """
    toc = _toc_for(set(models.Base.metadata.tables))

    with pytest.raises(VerificationError) as raised:
        await _verify(toc)

    assert "alembic" in str(raised.value)


@pytest.mark.asyncio
async def test_a_nonzero_exit_fails_verification():
    with pytest.raises(VerificationError) as raised:
        await _verify(_complete_toc(), returncode=1, stderr=b"pg_restore: error: did not find magic string")

    assert raised.value.fields["returncode"] == 1
    assert "magic string" in raised.value.fields["pg_restore_stderr"]


@pytest.mark.asyncio
async def test_pg_restore_that_cannot_be_started_fails_verification():
    artifact = BackupArtifact(name=ARTIFACT_NAME, path="/spool/x", created_at=CREATED_AT, size_bytes=1)

    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", side_effect=OSError("no such binary")):
        with pytest.raises(VerificationError) as raised:
            await verify_artifact(artifact)

    assert raised.value.fields["error_type"] == "OSError"


@pytest.mark.asyncio
async def test_a_noticeably_smaller_artefact_warns_and_still_verifies():
    """A genuine shrink is possible -- a purge of expired cache rows, a
    dropped column -- and refusing to keep the backup because it got smaller
    would throw away the only copy of whatever just happened."""
    result, _ = await _verify(_complete_toc(), previous_size_bytes=1_000_000, size_bytes=500_000)

    assert result.shrank is True


@pytest.mark.asyncio
async def test_a_slightly_smaller_artefact_is_not_remarked_on():
    result, _ = await _verify(_complete_toc(), previous_size_bytes=1_000_000, size_bytes=950_000)

    assert result.shrank is False


@pytest.mark.asyncio
async def test_verification_reads_the_artefact_that_was_just_written():
    _, call = await _verify(_complete_toc())

    assert call.argv == ("pg_restore", "--list", f"/spool/{ARTIFACT_NAME}")


# ============================================================
# THE CHILD ENVIRONMENT AND THE COMMAND LINE
# ============================================================

def test_the_child_environment_is_built_rather_than_inherited(monkeypatch):
    """
    os.environ in this process holds DATABASE_URL and AXIOM_TOKEN, and
    handing all of it to pg_dump copies every one of those into a second
    process's environ for the length of a nine-minute dump. SSLKEYLOGFILE is
    the sharpest of them: OpenSSL honours it and would write the session
    keys for the connection to a file.
    """
    monkeypatch.setenv("AXIOM_TOKEN", "xaat-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://libex:pw@db/libex")
    monkeypatch.setenv("SSLKEYLOGFILE", "/tmp/keys.log")

    env = _child_env("hunter2")

    assert set(env) == {"PATH", "LC_ALL", "LANG", "PGCONNECT_TIMEOUT", "PGPASSWORD"}
    assert "AXIOM_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert "SSLKEYLOGFILE" not in env


def test_the_child_environment_has_no_password_when_there_is_none():
    assert "PGPASSWORD" not in _child_env()


def test_the_child_environment_pins_the_locale_and_bounds_the_connection():
    env = _child_env()

    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"
    assert env["PGCONNECT_TIMEOUT"] == "10"


@pytest.mark.asyncio
async def test_credentials_never_appear_in_argv(tmp_path):
    """
    /proc/<pid>/cmdline is world-readable to every process on the host,
    while /proc/<pid>/environ is 0400 to the owning user. The password goes
    in the environment for exactly that reason, and passing the whole URL as
    -d would put it in the first of those.
    """
    target = DumpTarget(host="db", port=5432, user="libex", dbname="libex", password="hunter2")
    call = await _run_dump(target, tmp_path, payload=b"archive")

    assert "hunter2" not in " ".join(call.argv)
    assert call.kwargs["env"]["PGPASSWORD"] == "hunter2"


@pytest.mark.asyncio
async def test_pg_dump_is_asked_for_a_custom_format_archive_and_never_to_prompt(tmp_path):
    """Custom format is what pg_restore --list can read a TOC out of; a
    plain .sql dump would verify only by being valid text. --no-password
    fails immediately rather than by way of a closed stdin."""
    call = await _run_dump(DumpTarget(host="db", port=5432, user="libex", dbname="libex"), tmp_path, payload=b"x")

    assert call.argv[0] == "pg_dump"
    assert "--format=custom" in call.argv
    assert "--no-password" in call.argv


async def _run_dump(target, tmp_path, payload=b"archive", returncode=0, stderr=b"", stop=None):
    process = _FakeProcess(returncode=returncode, stderr=stderr)

    async def fake_exec(*argv, **kwargs):
        fake_exec.argv = argv
        fake_exec.kwargs = kwargs
        # The child holds its own duplicate of the descriptor and writes the
        # archive through it, which is what this stands in for.
        os.write(kwargs["stdout"], payload)
        return process

    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", fake_exec):
        fake_exec.artifact = await run_pg_dump(target, str(tmp_path), CREATED_AT, 60.0, stop)
    return fake_exec


@pytest.mark.asyncio
async def test_a_successful_dump_returns_the_artefact_it_wrote(tmp_path):
    call = await _run_dump(DumpTarget(host="db", port=5432, user="libex", dbname="libex"), tmp_path, payload=b"12345")

    assert call.artifact.name == ARTIFACT_NAME
    assert call.artifact.size_bytes == 5
    assert os.path.exists(call.artifact.path)


@pytest.mark.asyncio
async def test_a_failed_dump_removes_the_spool_file_and_carries_the_stderr(tmp_path):
    process = _FakeProcess(returncode=1, stderr=b'pg_dump: error: connection to server failed\nFATAL:  no pg_hba entry')

    async def fake_exec(*argv, **kwargs):
        os.write(kwargs["stdout"], b"half an archive")
        return process

    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", fake_exec):
        with pytest.raises(DumpError) as raised:
            await run_pg_dump(DumpTarget(host="db", port=5432, user="libex", dbname="libex"), str(tmp_path), CREATED_AT, 60.0)

    assert raised.value.fields["returncode"] == 1
    assert "no pg_hba entry" in raised.value.fields["pg_dump_stderr"]
    assert not (tmp_path / ARTIFACT_NAME).exists()


@pytest.mark.asyncio
async def test_an_empty_artefact_is_a_failure_and_is_removed(tmp_path):
    with pytest.raises(DumpError):
        await _run_dump(DumpTarget(host="db", port=5432, user="libex", dbname="libex"), tmp_path, payload=b"")

    assert not (tmp_path / ARTIFACT_NAME).exists()


@pytest.mark.asyncio
async def test_a_pg_dump_that_cannot_be_started_removes_the_spool_file(tmp_path):
    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", side_effect=OSError("no such binary")):
        with pytest.raises(DumpError) as raised:
            await run_pg_dump(DumpTarget(host="db", port=5432, user="libex", dbname="libex"), str(tmp_path), CREATED_AT, 60.0)

    assert raised.value.fields["error_type"] == "OSError"
    assert not (tmp_path / ARTIFACT_NAME).exists()


@pytest.mark.asyncio
async def test_a_dump_past_its_timeout_terminates_the_child_rather_than_orphaning_it(tmp_path):
    """
    An abandoned pg_dump keeps its transaction snapshot open, and until it
    dies autovacuum cannot reclaim any row version newer than that snapshot
    -- on a table taking continuous writes that is bloat accumulating for as
    long as the orphan survives.
    """
    process = _FakeProcess()
    signalled = asyncio.Event()

    async def waits_for_a_signal():
        await signalled.wait()
        return -15

    def terminate():
        process.terminated = True
        signalled.set()

    process.wait = waits_for_a_signal
    process.terminate = terminate

    async def fake_exec(*argv, **kwargs):
        os.write(kwargs["stdout"], b"partial")
        return process

    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", fake_exec):
        with pytest.raises(DumpError) as raised:
            await run_pg_dump(DumpTarget(host="db", port=5432, user="libex", dbname="libex"), str(tmp_path), CREATED_AT, 0.01)

    assert raised.value.fields["timeout_seconds"] == 0.01
    assert process.terminated is True
    assert not (tmp_path / ARTIFACT_NAME).exists()


# ============================================================
# THE SPOOL
# ============================================================

def test_the_spool_file_is_created_private(tmp_path):
    path = str(tmp_path / ARTIFACT_NAME)

    descriptor = create_spool_file(path)
    os.close(descriptor)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_spool_file_refuses_to_clobber_a_leftover(tmp_path):
    """
    The real mutex against a manual `--once` meeting the scheduled cycle on
    the same artefact name. Two pg_dumps writing one file interleave into an
    archive that verifies as neither.
    """
    path = str(tmp_path / ARTIFACT_NAME)
    (tmp_path / ARTIFACT_NAME).write_text("someone got here first")

    with pytest.raises(FileExistsError):
        create_spool_file(path)


def test_the_spool_file_refuses_to_follow_a_pre_planted_symlink(tmp_path):
    """O_EXCL refuses a symlink rather than following it, so nothing can
    redirect a dump into a file it chooses."""
    target = tmp_path / "elsewhere"
    link = tmp_path / ARTIFACT_NAME
    link.symlink_to(target)

    with pytest.raises(FileExistsError):
        create_spool_file(str(link))

    assert not target.exists()


def test_clear_spool_removes_artefacts_and_partials(tmp_path):
    (tmp_path / ARTIFACT_NAME).write_text("an artefact")
    (tmp_path / f"{ARTIFACT_NAME}.partial").write_text("half an artefact")

    assert clear_spool(str(tmp_path)) == 2
    assert list(tmp_path.iterdir()) == []


def test_clear_spool_leaves_the_status_file_alone(tmp_path):
    """It is the schedule's memory of which periods have already succeeded,
    and deleting it would make every restart take an unnecessary dump."""
    (tmp_path / "status.json").write_text("{}")
    (tmp_path / ARTIFACT_NAME).write_text("an artefact")

    assert clear_spool(str(tmp_path)) == 1
    assert (tmp_path / "status.json").exists()


def test_clear_spool_leaves_files_that_are_not_ours(tmp_path):
    (tmp_path / "somebody-elses.tar.gz").write_text("not ours")

    assert clear_spool(str(tmp_path)) == 0
    assert (tmp_path / "somebody-elses.tar.gz").exists()


def test_clear_spool_survives_an_unreadable_directory(tmp_path):
    assert clear_spool(str(tmp_path / "does-not-exist")) == 0


def test_discarding_a_spool_file_that_is_already_gone_is_silent(tmp_path):
    discard_spool_file(str(tmp_path / "never-existed"))


# ============================================================
# THE FREE-SPACE PRECHECK
# ============================================================

def test_the_precheck_refuses_when_the_next_artefact_would_not_fit(tmp_path):
    """A dump that fills the disk fails nine minutes in, leaves a partial
    behind, and takes the filesystem to zero free on the way -- on a shared
    volume that is somebody else's outage."""
    with patch("app.services.backup.dump.shutil.disk_usage", return_value=_usage(free=1024)):
        with pytest.raises(PrecheckError) as raised:
            check_free_space(str(tmp_path))

    assert raised.value.fields["free_bytes"] == 1024


def test_the_precheck_uses_the_previous_artefact_size_when_it_is_larger_than_the_floor(tmp_path):
    """The estimate is what the last one measured, with headroom for growth
    -- the spool holds one artefact at a time, so this is not a retention
    calculation."""
    previous = 10 * 1024 * 1024 * 1024

    with patch("app.services.backup.dump.shutil.disk_usage", return_value=_usage(free=int(previous * 1.4))):
        with pytest.raises(PrecheckError) as raised:
            check_free_space(str(tmp_path), previous)

    assert raised.value.fields["required_bytes"] == int(previous * 1.5)


def test_the_precheck_passes_when_there_is_room(tmp_path):
    with patch("app.services.backup.dump.shutil.disk_usage", return_value=_usage(free=100 * 1024 * 1024 * 1024)):
        check_free_space(str(tmp_path))


def test_an_unreadable_spool_is_a_precheck_failure(tmp_path):
    with patch("app.services.backup.dump.shutil.disk_usage", side_effect=OSError("gone")):
        with pytest.raises(PrecheckError) as raised:
            check_free_space(str(tmp_path))

    assert raised.value.fields["error_type"] == "OSError"


def _usage(free):
    """shutil.disk_usage's named tuple, with only the field under test set."""
    import shutil

    return shutil._ntuple_diskusage(total=free * 2, used=free, free=free)


# ============================================================
# READING A CHILD'S PIPES
# ============================================================

@pytest.mark.asyncio
async def test_a_pipe_is_read_to_the_end_even_once_the_cap_is_reached():
    """
    A pipe nobody drains fills its 64 KB kernel buffer and blocks the child
    forever: the child waits to write, this process waits for the child to
    exit, and the dump timeout is the only thing that ever ends it.
    Discarding the overflow rather than stopping the read is what avoids
    that while still bounding what is kept.
    """
    stream = _FakeStream(b"x" * 20000)

    kept, truncated = await _read_capped(stream, 100)

    assert kept == b"x" * 100
    assert truncated is True
    assert await stream.read(8192) == b""


@pytest.mark.asyncio
async def test_output_under_the_cap_is_kept_whole():
    kept, truncated = await _read_capped(_FakeStream(b"short"), 100)

    assert (kept, truncated) == (b"short", False)


def test_a_multi_line_message_is_collapsed_into_one_log_field():
    assert _decode(b"pg_dump: error: connection failed\nFATAL:  no entry\n") == (
        "pg_dump: error: connection failed FATAL: no entry"
    )


def test_undecodable_bytes_become_replacement_characters_rather_than_an_exception():
    assert _decode(b"\xff\xfe") == "��"


# ============================================================
# THE EXCEPTION HIERARCHY
# ============================================================

def test_dump_errors_are_not_on_the_api_exception_hierarchy():
    """A LibexException carries a .message the middleware copies into an HTTP
    response body. Nothing in this package may be able to reach that path."""
    from app.core.exceptions import LibexException

    for exception in (DumpError, PrecheckError, VerificationError):
        assert not issubclass(exception, LibexException)


def test_a_precheck_error_is_a_dump_error():
    assert issubclass(PrecheckError, DumpError)
    assert issubclass(VerificationError, DumpError)


# ============================================================
# THE STOP REQUEST REACHES THE CHILD
# ============================================================
#
# REAL CHILD PROCESSES HERE, not _FakeProcess. The thing under test is
# whether asyncio.wait(FIRST_COMPLETED) over process.wait() and stop.wait()
# actually returns while a child is still running, and a fake whose wait()
# is a coroutine the test controls proves only that the test can control it.
# These spawn a python that sleeps and then measure how long the wait
# actually took.
#
# The gap this closes: the signal handler set both flags correctly, dump.py
# had a CancelledError branch that terminated pg_dump properly, and none of
# it ran -- _cycle is awaited directly and nothing ever cancelled it, so the
# branch was dead code on the signal path and the await simply sat there.
# SIGTERM one second into an eight-second child left the cycle still running
# sixteen seconds later with pg_dump alive. Against a real dump that is up
# to 65 minutes of stop_grace_period spent holding ACCESS SHARE on every
# table, behind which a queued ACCESS EXCLUSIVE and every reader arriving
# after it both block.

_REAL_SUBPROCESS_EXEC = asyncio.create_subprocess_exec

# Long enough that a wait which ignores the stop flag cannot pass the timing
# assertion below, and short enough to stay well inside the suite's 30s
# per-test tripwire if something goes wrong.
_CHILD_SLEEP_SECONDS = 10


def _spawns_a_real_sleeper(seconds=_CHILD_SLEEP_SECONDS, stdout_to_descriptor=True):
    """
    Replaces create_subprocess_exec with one that starts a real, long-lived
    child instead of pg_dump -- same descriptor handling, same pipes, no
    postgres.
    """
    async def fake_exec(*argv, **kwargs):
        stdout = kwargs["stdout"] if stdout_to_descriptor else asyncio.subprocess.PIPE
        process = await _REAL_SUBPROCESS_EXEC(
            sys.executable,
            "-c",
            f"import time; time.sleep({seconds})",
            stdout=stdout,
            stderr=asyncio.subprocess.PIPE,
        )
        fake_exec.process = process
        return process

    return fake_exec


async def _request_the_stop_after(stop, delay):
    await asyncio.sleep(delay)
    stop.set()


@pytest.mark.asyncio
async def test_a_stop_request_ends_the_dump_instead_of_running_it_to_completion(tmp_path):
    """
    The measurement, not the mechanism: the wait must return while the child
    is still alive. A ten-second child, stopped a fifth of a second in,
    finishes in well under a second -- where the version that consulted
    nothing took the full ten.
    """
    stop = asyncio.Event()
    exec_with_a_real_child = _spawns_a_real_sleeper()
    asyncio.ensure_future(_request_the_stop_after(stop, 0.2))

    started = time.monotonic()
    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", exec_with_a_real_child):
        with pytest.raises(DumpAborted):
            await run_pg_dump(
                DumpTarget(host="db", port=5432, user="libex", dbname="libex"),
                str(tmp_path),
                CREATED_AT,
                float(_CHILD_SLEEP_SECONDS * 10),
                stop,
            )
    elapsed = time.monotonic() - started

    assert elapsed < 3.0


@pytest.mark.asyncio
async def test_a_stopped_dump_kills_its_child_rather_than_orphaning_it(tmp_path):
    """
    An orphaned pg_dump holds its transaction snapshot open and its ACCESS
    SHARE locks with it, so autovacuum reclaims nothing newer than that
    snapshot and a migration waiting on an ACCESS EXCLUSIVE queues behind
    it along with every reader that arrives after. The child is dead before
    DumpAborted propagates, and its return code says a signal did it.
    """
    stop = asyncio.Event()
    exec_with_a_real_child = _spawns_a_real_sleeper()
    asyncio.ensure_future(_request_the_stop_after(stop, 0.2))

    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", exec_with_a_real_child):
        with pytest.raises(DumpAborted):
            await run_pg_dump(
                DumpTarget(host="db", port=5432, user="libex", dbname="libex"),
                str(tmp_path),
                CREATED_AT,
                float(_CHILD_SLEEP_SECONDS * 10),
                stop,
            )

    assert exec_with_a_real_child.process.returncode is not None
    assert exec_with_a_real_child.process.returncode < 0


@pytest.mark.asyncio
async def test_a_stopped_dump_takes_its_half_written_archive_with_it(tmp_path):
    """Nothing will ever upload it, and the spool is sized for one artefact.
    Leaving it behind is what makes the next cycle's free-space precheck
    refuse."""
    stop = asyncio.Event()
    asyncio.ensure_future(_request_the_stop_after(stop, 0.2))

    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", _spawns_a_real_sleeper()):
        with pytest.raises(DumpAborted):
            await run_pg_dump(
                DumpTarget(host="db", port=5432, user="libex", dbname="libex"),
                str(tmp_path),
                CREATED_AT,
                float(_CHILD_SLEEP_SECONDS * 10),
                stop,
            )

    assert not (tmp_path / ARTIFACT_NAME).exists()


@pytest.mark.asyncio
async def test_a_stop_is_recorded_as_its_own_class_rather_than_an_indistinguishable_failure(tmp_path):
    """The operator reading last_error_type after a deploy has to be able to
    tell "the container was stopped mid-dump" from "pg_dump could not
    connect". Both are DumpError and both are a failed cycle; only the class
    name separates them."""
    stop = asyncio.Event()
    stop.set()

    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", _spawns_a_real_sleeper()):
        with pytest.raises(DumpAborted) as raised:
            await run_pg_dump(
                DumpTarget(host="db", port=5432, user="libex", dbname="libex"),
                str(tmp_path),
                CREATED_AT,
                float(_CHILD_SLEEP_SECONDS * 10),
                stop,
            )

    assert type(raised.value).__name__ == "DumpAborted"
    assert isinstance(raised.value, DumpError)


@pytest.mark.asyncio
async def test_a_healthy_dump_leaves_nothing_still_waiting_on_the_stop_flag(tmp_path):
    """
    The stop watcher is pending on every dump that succeeds, which is all of
    them, and a task left waiting on an Event nobody will ever set is a task
    the loop still holds at shutdown -- one per cycle, forever. The cleanup
    is in a finally that the successful return also passes through, and this
    is what says so.
    """
    stop = asyncio.Event()
    before = asyncio.all_tasks()

    call = await _run_dump(
        DumpTarget(host="db", port=5432, user="libex", dbname="libex"),
        tmp_path,
        payload=b"archive",
        stop=stop,
    )
    await asyncio.sleep(0)

    assert call.artifact.size_bytes == 7
    assert asyncio.all_tasks() - before == set()


@pytest.mark.asyncio
async def test_verification_stops_with_the_container_rather_than_finishing_first(tmp_path):
    """
    pg_restore --list is bounded by the stop request for the same reason the
    dump is: an unbounded wait on a wedged child holds the cycle open, and a
    stop that waits for it spends the grace period doing nothing.
    """
    artifact = BackupArtifact(
        name=ARTIFACT_NAME, path=str(tmp_path / ARTIFACT_NAME), created_at=CREATED_AT, size_bytes=10
    )
    stop = asyncio.Event()
    exec_with_a_real_child = _spawns_a_real_sleeper(stdout_to_descriptor=False)
    asyncio.ensure_future(_request_the_stop_after(stop, 0.2))

    started = time.monotonic()
    with patch("app.services.backup.dump.asyncio.create_subprocess_exec", exec_with_a_real_child):
        with pytest.raises(DumpAborted):
            await verify_artifact(artifact, 0, stop)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert exec_with_a_real_child.process.returncode is not None
    assert exec_with_a_real_child.process.returncode < 0


# ============================================================
# THE SPOOL LOCK
# ============================================================
#
# A real fcntl.flock on a real file. Two separate os.open calls produce two
# open file descriptions, which conflict with each other even inside one
# process, so the collision between a scheduled container and a supervised
# --once is reproducible here without a second process.
#
# THE O_EXCL ON THE ARTEFACT NEVER WAS THIS MUTEX and was described as one.
# build_name has second resolution, so the only collision O_EXCL can see at
# all is two dumps starting inside the same second; and startup clears the
# spool before anything else happens, which unlinks the artefact a running
# dump is writing into.

def test_a_spool_already_claimed_is_refused_rather_than_queued_behind(tmp_path):
    """LOCK_NB, because the answer to "someone else has it" is to say so and
    stop, never to wait out a nine-minute dump."""
    held = acquire_spool_lock(str(tmp_path))

    try:
        with pytest.raises(SpoolBusy) as raised:
            acquire_spool_lock(str(tmp_path))
    finally:
        release_spool_lock(held)

    assert raised.value.fields["error_type"] == "BlockingIOError"
    assert raised.value.fields["spool_dir"] == str(tmp_path)


def test_a_spool_busy_is_a_dump_error_so_the_runner_records_it_as_one():
    """run() catches dump.DumpError around the acquire. A SpoolBusy outside
    that hierarchy would travel past the handler and out of the process
    with no status file written."""
    assert issubclass(SpoolBusy, DumpError)


def test_releasing_the_lock_lets_the_next_arrival_claim_it(tmp_path):
    """The descriptor IS the lock, so releasing has to be an act at the call
    site rather than a side effect of something else closing."""
    first = acquire_spool_lock(str(tmp_path))
    release_spool_lock(first)

    second = acquire_spool_lock(str(tmp_path))
    release_spool_lock(second)


def test_the_lock_file_is_left_in_place_rather_than_unlinked(tmp_path):
    """Removing a file another process holds a lock on hands the next
    arrival a fresh inode and leaves two winners."""
    held = acquire_spool_lock(str(tmp_path))
    release_spool_lock(held)

    assert (tmp_path / SPOOL_LOCK_FILENAME).exists()


def test_clear_spool_does_not_remove_the_lock_file(tmp_path):
    """
    The filter passes over it because its name is neither an artefact nor a
    partial -- and that is load-bearing rather than incidental. clear_spool
    runs immediately after the lock is taken, so a filter that matched the
    lock file would delete the inode this process is holding and hand the
    next arrival a new one.
    """
    held = acquire_spool_lock(str(tmp_path))
    (tmp_path / "libex-20260906T030000Z.dump").write_bytes(b"leftover")

    try:
        removed = clear_spool(str(tmp_path))
    finally:
        release_spool_lock(held)

    assert removed == 1
    assert (tmp_path / SPOOL_LOCK_FILENAME).exists()


def test_a_spool_lock_that_cannot_be_opened_is_a_precheck_failure(tmp_path):
    """A missing or unwritable spool directory. Refused before the dump
    rather than discovered nine minutes in."""
    with pytest.raises(PrecheckError) as raised:
        acquire_spool_lock(str(tmp_path / "does-not-exist"))

    assert raised.value.fields["error_type"] == "FileNotFoundError"


def test_releasing_a_descriptor_twice_is_silent(tmp_path):
    """The runner releases in a finally that every path out passes through,
    including ones that have already unwound part way."""
    held = acquire_spool_lock(str(tmp_path))
    release_spool_lock(held)
    release_spool_lock(held)
