"""
Taking the dump: free-space precheck, pg_dump, the spool file, verification.

pg_dump is a subprocess with its own libpq connection, which is the reason
this whole package never touches app.db.session. Importing that module
builds a SQLAlchemy engine with pool_size=10 and max_overflow=10 against a
max_connections=200 budget that is already fully allocated to six API
workers and the seeder -- twenty connections reserved, permanently, by a
process that would never open one of them.

CREDENTIALS NEVER APPEAR IN ARGV. The connection details are passed as
-h/-p/-U/-d flags and the password only as PGPASSWORD in the child's
environment, because /proc/<pid>/cmdline is world-readable to every process
on the host while /proc/<pid>/environ is 0400 to the owning user. Passing
the whole URL as -d would put the password in the first of those.

THE CHILD ENVIRONMENT IS BUILT EXPLICITLY, not inherited. os.environ in this
process holds DATABASE_URL, AXIOM_TOKEN and whatever else the container was
given, and handing all of it to pg_dump copies every one of those secrets
into a second process's environ for the length of a nine-minute dump. It
also excludes SSLKEYLOGFILE, which OpenSSL honours from the environment and
which would write the session keys for the connection to a file.
"""

# Standard library
import asyncio
import fcntl
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import unquote, urlsplit

# Core
from app.core.logging import get_logger

# Database -- the models only, for their metadata. This import registers
# every table on Base.metadata and opens nothing; app.db.session, which is
# what builds a connection pool, is deliberately not imported anywhere in
# this package.
from app.db import models

# Services
from app.services.backup import artifact as artifact_names
from app.services.backup.artifact import BackupArtifact


logger = get_logger()

# How much of pg_dump's stderr is kept. Four kilobytes is several failures'
# worth of message and far short of anything that could fill a log line.
_STDERR_CAP_BYTES = 4096

# pg_restore --list output for this schema is a few hundred lines. The cap
# is a bound on a subprocess we do not control rather than a fitted size,
# and it is never logged -- only counted and matched against.
_TOC_CAP_BYTES = 8 * 1024 * 1024

# Assumed artefact size when there is no previous one to go on. A clean dump
# of the live database measured 2,376,454,048 bytes on 2026-09-06; three
# gibibytes is above that with room for growth, and it only ever applies to
# the first cycle after a fresh deploy.
_ARTIFACT_SIZE_FLOOR_BYTES = 3 * 1024 * 1024 * 1024

# Free space required, as a multiple of the expected artefact size. A dump
# that runs out of disk nine minutes in has cost the same nine minutes and
# left a partial file behind; refusing to start costs a log line.
_FREE_SPACE_HEADROOM = 1.5

# How long a terminated pg_dump is given to exit before it is killed.
_TERMINATE_GRACE_SECONDS = 30.0

# Below this fraction of the previous artefact, the new one is worth
# remarking on. It warns and never fails: a genuine shrink is possible --
# a purge of expired cache rows, a dropped column -- and refusing to keep a
# backup because it got smaller would throw away the only copy of whatever
# just happened.
_SHRINK_WARN_RATIO = 0.9

# Alembic's own bookkeeping table. It is checked by name because it is NOT
# in Base.metadata -- alembic creates and owns it -- and an artefact without
# it restores data into a database that cannot say which migration it is at.
# Every later `alembic upgrade head` against that restore either replays
# migrations that have already run or refuses to start.
_ALEMBIC_TABLE = "alembic_version"

# How long pg_restore --list is given. Reading the header and table of
# contents out of a 2.38 GB archive is seconds' work, so this is a bound on
# a subprocess nothing here controls rather than a fitted value. It exists
# because the dump timeout's stated purpose -- being shorter than one
# scheduling period, so a wedged child cannot collide with its successor --
# covered only half the pipeline: an unbounded wait here holds the cycle
# open forever and the next period never runs at all.
_VERIFY_TIMEOUT_SECONDS = 300.0

# The lock file that makes one process at a time the owner of this spool.
# The name is neither an artefact nor a partial, so clear_spool leaves it
# alone, and it is never unlinked: removing a file another process holds a
# lock on hands the next arrival a fresh inode and leaves two winners.
SPOOL_LOCK_FILENAME = ".spool.lock"


class DumpError(Exception):
    """
    A dump that did not produce a usable artefact.

    Not a LibexException subclass -- see destinations/base.py for why
    nothing in this package may be on that hierarchy. The message is a
    fixed string; anything variable travels in .fields.
    """

    def __init__(self, message: str, **fields):
        super().__init__(message)
        self.fields = fields


class PrecheckError(DumpError):
    """A reason not to start a dump at all."""


class VerificationError(DumpError):
    """An artefact that exists but cannot be trusted. It is never uploaded."""


class DumpAborted(DumpError):
    """
    A child process stopped because this one was asked to shut down.

    Its own class so the status file records a stop as a stop rather than as
    an indistinguishable failure. It is still a DumpError and still a failed
    cycle -- no artefact was produced, and the period has not been backed up
    -- but the operator reading last_error_type after a deploy should be
    able to tell "the container was stopped mid-dump" from "pg_dump could
    not connect".
    """


class SpoolBusy(DumpError):
    """Another process already owns this spool directory."""


@dataclass(frozen=True)
class DumpTarget:
    """Where pg_dump connects. The password is separated from the rest so that no
    code path can accidentally format it into a command line."""

    host: str
    port: int
    user: str
    dbname: str
    password: str = ""


@dataclass(frozen=True)
class VerifyResult:
    size_bytes: int = 0
    toc_entries: int = 0
    tables_found: int = 0
    has_alembic_version: bool = False
    missing_tables: list[str] = field(default_factory=list)
    shrank: bool = False


def parse_database_url(url: str) -> DumpTarget:
    """
    Turns the SQLAlchemy URL the container is given into libpq arguments.

    Two things this has to get right.

    The driver suffix: DATABASE_URL is postgresql+asyncpg://..., which
    pg_dump does not understand at all. Only the scheme carries it, and
    nothing else here reads the scheme, so it is simply dropped.

    Percent-decoding: urlsplit().password does NOT decode. Measured --
    'p%40ss%2Fword' comes back verbatim and unquote() turns it into
    'p@ss/word'. Without that call, a password containing any reserved
    character authenticates with the wrong string, and what the operator
    sees is "password authentication failed", which reads exactly like a
    wrong password and sends them to change it in the one place it was
    already right.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        raise PrecheckError("DATABASE_URL has no host", field_name="DATABASE_URL")

    dbname = unquote(parts.path.lstrip("/"))
    if not dbname:
        raise PrecheckError("DATABASE_URL names no database", field_name="DATABASE_URL")

    return DumpTarget(
        host=parts.hostname,
        port=parts.port or 5432,
        user=unquote(parts.username or ""),
        dbname=dbname,
        password=unquote(parts.password or ""),
    )


def clear_spool(spool_dir: str) -> int:
    """
    Removes every artefact and partial left in the spool, and returns how
    many there were.

    Called once at startup, and this is the real defence against a stranded
    partial rather than any amount of cleanup in an exception handler: a
    container killed mid-dump -- a deploy, an OOM, a host reboot -- runs no
    handler at all.

    Anything found here is by definition from a process that no longer
    exists, and it is the spool lock rather than O_EXCL that makes that
    true: this runs only once the lock is held, so no other backup process
    can be part-way through writing what it is about to delete. Called
    without the lock it is destructive -- see acquire_spool_lock.

    The status file is not touched. It is the schedule's memory of which
    periods have already succeeded, and deleting it would make every restart
    take an unnecessary dump. Neither is the lock file: its name is neither
    an artefact nor a partial, so the filter below passes over it.
    """
    removed = 0
    try:
        entries = os.listdir(spool_dir)
    except OSError as exc:
        logger.error(
            "Backup: spool directory could not be read",
            extra={"error_type": type(exc).__name__, "spool_dir": spool_dir},
        )
        return 0

    for entry in entries:
        if not (entry.startswith(artifact_names.ARTIFACT_PREFIX) or entry.endswith(artifact_names.PARTIAL_SUFFIX)):
            continue
        try:
            os.unlink(os.path.join(spool_dir, entry))
            removed += 1
        except OSError as exc:
            logger.warning(
                "Backup: could not remove a leftover spool file",
                extra={"error_type": type(exc).__name__},
            )
    return removed


def acquire_spool_lock(spool_dir: str) -> int:
    """
    Claims the spool for this process, and returns the descriptor holding
    the claim.

    THIS IS THE MUTEX BETWEEN A SCHEDULED RUN AND A SUPERVISED --once, and
    the O_EXCL on the artefact never was one. Two reasons, both measured.
    build_name is second-resolution, so the only collision O_EXCL can see at
    all is two dumps starting inside the same second. And startup clears the
    spool before anything else happens, which unlinks the artefact a running
    dump is writing into: pg_dump's descriptor survives the unlink, so it
    goes on writing 2.3 GB into an inode with no name -- real disk consumed,
    invisible to ls -- exits 0 having produced a complete archive nobody can
    open, and the cycle dies at getsize() with "dump artefact disappeared",
    a message that names the symptom and points nowhere near the cause.

    flock rather than a pid file, because the kernel releases it however the
    holder dies. A container killed by an OOM or a SIGKILL leaves no stale
    claim to time out and nothing to clean up, which is exactly the case a
    pid file gets wrong. LOCK_NB because the answer to "someone else has it"
    is to say so and stop, never to queue up behind a nine-minute dump.

    The descriptor IS the lock -- closing it releases it -- so it is held
    for the whole run and released once, in the runner's finally.
    """
    path = os.path.join(spool_dir, SPOOL_LOCK_FILENAME)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise PrecheckError(
            "the spool lock could not be opened",
            error_type=type(exc).__name__,
            spool_dir=spool_dir,
        ) from exc

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise SpoolBusy(
            "another backup process already holds this spool",
            error_type=type(exc).__name__,
            spool_dir=spool_dir,
        ) from exc

    return descriptor


def release_spool_lock(descriptor: int) -> None:
    """
    Releases the claim.

    Closing the descriptor is what actually releases the flock; the explicit
    LOCK_UN is there so the release reads as an act at the call site rather
    than as a side effect of a close. The file itself is left in place on
    purpose -- unlinking it would let the next process create a different
    inode and take a lock nobody else can see.
    """
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(descriptor)
    except OSError:
        pass


def check_free_space(spool_dir: str, previous_size_bytes: int = 0) -> None:
    """
    Refuses the cycle rather than starting one that cannot finish.

    The estimate is the previous artefact's size where there is one, and a
    measured floor where there is not, times a headroom factor -- the spool
    holds one artefact at a time, so this is not a retention calculation, it
    is "does the next one fit, with room for it to have grown".

    Raising here is the point. A dump that fills the disk fails nine minutes
    in, leaves a partial file behind, and takes the filesystem to zero free
    on the way -- on a shared volume that is somebody else's outage.
    """
    expected = max(previous_size_bytes, _ARTIFACT_SIZE_FLOOR_BYTES)
    required = int(expected * _FREE_SPACE_HEADROOM)
    try:
        free = shutil.disk_usage(spool_dir).free
    except OSError as exc:
        raise PrecheckError(
            "spool directory is unreadable",
            error_type=type(exc).__name__,
            spool_dir=spool_dir,
        ) from exc

    if free < required:
        raise PrecheckError(
            "not enough free space in the spool for another dump",
            free_bytes=free,
            required_bytes=required,
            spool_dir=spool_dir,
        )


def create_spool_file(path: str) -> int:
    """
    Creates the spool file and returns an open descriptor for it.

    O_EXCL with the mode passed to open(), never open-then-chmod. Three
    things follow from that one call, and each of them is the reason:

      - The file is 0600 from the instant it exists. open() then chmod()
        leaves it at 0644-minus-umask for the length of the window between
        them, and what is in it is the entire database.
      - A pre-planted symlink at this path is refused rather than followed,
        so nothing can redirect a dump into a file it chooses.
      - A leftover from a previous run is refused rather than clobbered.

    What that last point is NOT is the mutex against a concurrent
    `scripts/backup.py --once`. It was described as one and it cannot be:
    build_name has second resolution, so two cycles starting more than a
    second apart get different names and O_EXCL never sees them meet. The
    real mutex is the spool lock -- see acquire_spool_lock -- and this
    refusal is the backstop underneath it.
    """
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


async def run_pg_dump(
    target: DumpTarget,
    spool_dir: str,
    created_at: datetime,
    timeout_seconds: float,
    stop: asyncio.Event | None = None,
) -> BackupArtifact:
    """
    Runs pg_dump -Fc into the spool and returns the artefact it produced.

    The archive goes to the child's stdout, which is the spool descriptor
    we opened -- not to a -f path. pg_dump writes straight through the
    kernel into the file, so nothing crosses this process's memory (the
    container's mem_limit is 512m and the artefact is 2.38 GB), and there is
    no second open-by-path for anything to substitute between the O_EXCL
    creation and the write.

    asyncio.create_subprocess_exec and never subprocess.run. A blocking call
    here freezes the event loop for the length of the dump, which means
    SIGTERM is not handled until it finishes, which means Docker's grace
    period expires and SIGKILL lands on this process -- leaving pg_dump
    orphaned and still holding its transaction snapshot open. That snapshot
    pins the xmin horizon: autovacuum cannot reclaim any row version newer
    than it, on a 1.1M-row write-heavy table, for as long as the orphan
    lives. Being able to propagate the signal to the child is the whole
    reason for the async form.

    `stop` is what makes that propagation actually happen. The async form on
    its own only means the handler RUNS -- it sets a flag and returns, and
    for as long as nothing consults the flag the dump carries on to
    completion regardless. Measured before this argument existed: SIGTERM at
    one second into an eight-second child produced both flags set, the child
    still alive, and the cycle still running sixteen seconds later. Handing
    the Event down to _wait_for_child is what turns the flag into a
    terminated child, and it is the same guarantee the upload half already
    had from polling the abort flag inside storbinary's callback.
    """
    name = artifact_names.build_name(created_at)
    path = os.path.join(spool_dir, name)

    argv = [
        "pg_dump",
        "--format=custom",
        # Never prompt for a password. stdin is not a terminal here, so a
        # prompt would fail anyway -- this makes it fail immediately and
        # unambiguously instead of by way of a closed stdin.
        "--no-password",
        "--host", target.host,
        "--port", str(target.port),
        "--username", target.user,
        "--dbname", target.dbname,
    ]

    descriptor = create_spool_file(path)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=descriptor,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(target.password),
        )
    except OSError as exc:
        os.close(descriptor)
        discard_spool_file(path)
        raise DumpError(
            "pg_dump could not be started",
            error_type=type(exc).__name__,
        ) from exc

    # The child holds its own duplicate of this descriptor from the moment
    # it is spawned. Keeping ours open would leave the file un-reclaimable
    # if anything below removed it, and would keep a writable handle on the
    # artefact while it is being verified and uploaded.
    os.close(descriptor)

    stderr_task = asyncio.create_task(_read_capped(process.stderr, _STDERR_CAP_BYTES))
    try:
        returncode = await _wait_for_child(process, timeout_seconds, stop)
    except asyncio.TimeoutError:
        await _terminate(process)
        stderr_task.cancel()
        discard_spool_file(path)
        raise DumpError("pg_dump exceeded its timeout", timeout_seconds=timeout_seconds)
    except DumpAborted:
        # A stop request, noticed while the child was still running. The
        # child is killed before this propagates, for the snapshot reason
        # above -- an orphaned pg_dump outlives this process and keeps
        # holding autovacuum back -- and the half-written archive goes with
        # it, because nothing will ever upload it.
        await _terminate(process)
        stderr_task.cancel()
        discard_spool_file(path)
        raise
    except asyncio.CancelledError:
        # The same ending by a different route: something cancelled the task
        # this is running in. Kept alongside the branch above rather than
        # folded into it, because a cancellation must be re-raised as a
        # cancellation and never converted into an ordinary error.
        await _terminate(process)
        stderr_task.cancel()
        discard_spool_file(path)
        raise

    stderr_bytes, truncated = await stderr_task

    if returncode != 0:
        # pg_dump's stderr is the one thing in this package logged close to
        # verbatim, and the reasoning is written here so a reviewer can
        # re-check it rather than take it on trust: libpq never echoes
        # PGPASSWORD or any other credential into a diagnostic. Its
        # authentication failures read 'password authentication failed for
        # user "libex"' -- the user, never the password. What is in these
        # bytes is a connection error, a permission error, or a server
        # message, and without it a failed backup says only "exit 1".
        # Capped, decoded defensively, and carried as one structured field
        # rather than interpolated into the message.
        discard_spool_file(path)
        raise DumpError(
            "pg_dump failed",
            returncode=returncode,
            pg_dump_stderr=_decode(stderr_bytes),
            stderr_truncated=truncated,
        )

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise DumpError("dump artefact disappeared", error_type=type(exc).__name__) from exc

    if size == 0:
        discard_spool_file(path)
        raise DumpError("pg_dump produced an empty artefact")

    return BackupArtifact(name=name, path=path, created_at=created_at, size_bytes=size)


async def verify_artifact(
    artifact: BackupArtifact,
    previous_size_bytes: int = 0,
    stop: asyncio.Event | None = None,
) -> VerifyResult:
    """
    Reads the artefact back with pg_restore --list before anything uploads
    it.

    BE HONEST ABOUT WHAT THIS PROVES. pg_restore --list reads the archive
    header and the table of contents. It proves the file is a custom-format
    archive this pg_restore can open, that its TOC parses, and that the
    objects we expect are named in it. It does NOT decompress or read the
    data blocks, so it cannot tell you the rows are intact -- a truncated
    archive whose header and TOC survive would pass. The only thing that
    proves a restore works is a restore.

    What it does catch is the whole class of failure that actually happens:
    a dump cut short by a full disk, a killed child, or a version mismatch
    between the pg_dump that wrote it and the pg_restore that will read it.

    Three assertions:

      - pg_restore exits 0.
      - alembic_version is present BY NAME. Counting objects would not
        catch its absence, and without it a restore lands the data in a
        database that cannot tell Alembic where it stands.
      - Every table in Base.metadata is present in the TOC, derived from the
        metadata rather than compared against a hardcoded count, so this
        stays true the day a migration adds a table instead of failing on
        the number 14.

    Bounded by _VERIFY_TIMEOUT_SECONDS and by the stop request, for the same
    reason as the dump: an unbounded wait on a wedged pg_restore holds the
    cycle open forever, and the next period then never runs.

    Size shrinkage warns and never fails.
    """
    expected_tables = set(models.Base.metadata.tables)

    argv = ["pg_restore", "--list", artifact.path]
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
        )
    except OSError as exc:
        raise VerificationError(
            "pg_restore could not be started",
            error_type=type(exc).__name__,
        ) from exc

    stdout_task = asyncio.create_task(_read_capped(process.stdout, _TOC_CAP_BYTES))
    stderr_task = asyncio.create_task(_read_capped(process.stderr, _STDERR_CAP_BYTES))
    try:
        returncode = await _wait_for_child(process, _VERIFY_TIMEOUT_SECONDS, stop)
    except asyncio.TimeoutError:
        await _terminate(process)
        stdout_task.cancel()
        stderr_task.cancel()
        raise VerificationError(
            "pg_restore exceeded its timeout",
            timeout_seconds=_VERIFY_TIMEOUT_SECONDS,
        )
    except (DumpAborted, asyncio.CancelledError):
        await _terminate(process)
        stdout_task.cancel()
        stderr_task.cancel()
        raise

    toc_bytes, toc_truncated = await stdout_task
    stderr_bytes, truncated = await stderr_task

    if returncode != 0:
        raise VerificationError(
            "pg_restore could not read the artefact",
            returncode=returncode,
            pg_restore_stderr=_decode(stderr_bytes),
            stderr_truncated=truncated,
        )

    # Checked before the table comparison, and that order is the whole point
    # of checking it at all. A table of contents cut off at our own cap
    # parses into a set that is missing whatever came after the cut, and the
    # comparison below would then report "artefact is missing tables the
    # schema declares" -- sending someone to look at pg_dump for a limit
    # written on line _TOC_CAP_BYTES of this file.
    if toc_truncated:
        raise VerificationError(
            "the table of contents is larger than this verification will read",
            toc_cap_bytes=_TOC_CAP_BYTES,
        )

    toc_lines, tables = _parse_toc(_decode_full(toc_bytes))
    missing = sorted(expected_tables - tables)
    has_alembic = _ALEMBIC_TABLE in tables

    if missing:
        raise VerificationError(
            "artefact is missing tables the schema declares",
            missing_count=len(missing),
            # Our own table names, out of our own metadata. Nothing here
            # came from the archive or from the network.
            missing_tables=",".join(missing[:10]),
        )

    if not has_alembic:
        raise VerificationError(
            "artefact contains no alembic_version table",
            toc_entries=toc_lines,
        )

    shrank = bool(previous_size_bytes) and artifact.size_bytes < previous_size_bytes * _SHRINK_WARN_RATIO
    if shrank:
        logger.warning(
            "Backup: artefact is noticeably smaller than the previous one",
            extra={
                "size_bytes": artifact.size_bytes,
                "previous_size_bytes": previous_size_bytes,
                "artifact": artifact.name,
            },
        )

    return VerifyResult(
        size_bytes=artifact.size_bytes,
        toc_entries=toc_lines,
        tables_found=len(tables),
        has_alembic_version=has_alembic,
        missing_tables=missing,
        shrank=shrank,
    )


# ============================================================
# INTERNALS
# ============================================================

def _child_env(password: str = "") -> dict:
    """
    The child's entire environment. Built from nothing, never copied from
    os.environ -- see the module docstring.

    PATH is needed to find the binary. LC_ALL and LANG pin the messages to
    C, so diagnostics are stable English regardless of what the base image
    has installed, and so no locale lookup is attempted for files that are
    not in a slim image. PGCONNECT_TIMEOUT bounds the connection attempt: a
    database that is unreachable rather than slow should fail in ten
    seconds, not sit inside the hour-long dump timeout.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LC_ALL": "C",
        "LANG": "C",
        "PGCONNECT_TIMEOUT": "10",
    }
    if password:
        env["PGPASSWORD"] = password
    return env


async def _read_capped(stream, cap: int) -> tuple[bytes, bool]:
    """
    Reads a pipe to EOF, keeping at most `cap` bytes and discarding the
    rest.

    Reading to the end matters as much as the cap does. A pipe nobody drains
    fills its 64 KB kernel buffer and blocks the child forever -- the child
    waits to write, this process waits for the child to exit, and the dump
    timeout is the only thing that ever ends it. Discarding the overflow
    rather than stopping the read is what keeps that from happening while
    still bounding what is kept.
    """
    kept = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return bytes(kept), truncated
        room = cap - len(kept)
        if room > 0:
            kept.extend(chunk[:room])
        if len(chunk) > max(room, 0):
            truncated = True


async def _wait_for_child(process, timeout_seconds: float, stop: asyncio.Event | None) -> int:
    """
    Waits for a child process, watching the stop request as well as the
    clock.

    THE STOP FLAG HAS TO BE CONSULTED HERE, not only between steps. A
    handler that sets a flag while this coroutine sits inside
    process.wait() changes nothing on its own: the await does not return
    until the child does, so a SIGTERM arriving during a nine-minute dump
    was ignored for the rest of the dump and Docker spent its whole grace
    period waiting for a process that had already been asked to stop --
    with the dump's ACCESS SHARE locks and its snapshot held for all of it.

    The asyncio Event rather than the threading one, because this runs on
    the event loop, where an Event can be awaited: the handler's
    call_soon_threadsafe(set) wakes this within a tick and nothing polls.
    The threading Event is for the transfer threads, which cannot await
    anything. Two waiters, two kinds of flag, deliberately.

    Raises DumpAborted on a stop and asyncio.TimeoutError on the deadline.
    The child is still running in both cases: terminating it is left to the
    caller, which is the only thing that knows whether the spool file it
    created has to go with it.
    """
    waiter = asyncio.ensure_future(process.wait())
    watched = {waiter}
    stopped = asyncio.ensure_future(stop.wait()) if stop is not None else None
    if stopped is not None:
        watched.add(stopped)

    try:
        done, _ = await asyncio.wait(watched, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            return waiter.result()
        if stopped is not None and stopped in done:
            raise DumpAborted("stopped before the child process finished")
        raise asyncio.TimeoutError
    finally:
        # Including the exit through `return`: the stop watcher is still
        # pending on every healthy dump, and a task left waiting on an Event
        # that is never set is a task the loop still holds at shutdown.
        for task in watched:
            if not task.done():
                task.cancel()


async def _terminate(process) -> None:
    """
    SIGTERM, then SIGKILL if it is ignored.

    Never left to the operating system on exit. An abandoned pg_dump keeps
    its transaction snapshot open, and until it dies autovacuum cannot
    reclaim any row version newer than that snapshot -- on a table taking
    continuous writes, that is bloat accumulating for as long as the orphan
    survives.

    A cancellation arriving while this waits is remembered and re-raised
    once the child is dead, never swallowed. Returning normally out of a
    cancelled await turns the caller's cancellation into an ordinary error
    -- here, a DumpError -- and loses the request to stop entirely. That was
    unreachable while nothing cancelled this, which is exactly the kind of
    latency that is only unreachable until it is not.
    """
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return

    cancelled = False
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), _TERMINATE_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        cancelled = True

    try:
        process.kill()
    except ProcessLookupError:
        pass
    else:
        try:
            await asyncio.shield(process.wait())
        except asyncio.CancelledError:
            cancelled = True

    if cancelled:
        raise asyncio.CancelledError


def _parse_toc(text: str) -> tuple[int, set[str]]:
    """
    Table names out of pg_restore --list output.

    The format comes from pg_dump's own _printTocEntry:

        <dumpId>; <tableoid> <oid> <DESC> <schema> <tag> <owner>

    so '215; 1259 16400 TABLE public books libex' and '3021; 0 16400 TABLE
    DATA public books libex'. DESC is two words for table data and one for
    the definition, which is the only wrinkle. Comment lines begin with ';'.

    Any of those entries counts as the table being present, and the reason
    is what this function is for rather than what it is not. It asserts that
    the archive CONTAINS the tables the schema declares; it does not assert
    which sections pg_dump chose to emit, and it is not a size check.

    Nothing here is one, either -- an earlier version of this note claimed a
    schema-only archive would "fail the size check long before this", and
    there is no size check to fail. run_pg_dump rejects an artefact of
    exactly zero bytes and nothing else, and the shrink comparison in
    verify_artifact only warns and cannot fire at all on the first cycle
    after a deploy, when there is no previous size to compare against.
    """
    tables: set[str] = set()
    entries = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        head, separator, rest = stripped.partition(";")
        if not separator or not head.isdigit():
            continue
        entries += 1
        # tableoid, oid, desc, schema, tag, owner. The tag is the table
        # name. DESC is one word for the definition and two for the entries
        # that carry a qualifier -- 'TABLE DATA' and 'TABLE ATTACH' -- which
        # pushes the tag one place to the right. ATTACH belongs on that list
        # with DATA: reading it as a one-word DESC takes the schema as the
        # tag and records a table called 'public'.
        parts = rest.split()
        if len(parts) < 5 or parts[2] != "TABLE":
            continue
        if parts[3] in ("DATA", "ATTACH"):
            if len(parts) >= 6:
                tables.add(parts[5])
        else:
            tables.add(parts[4])
    return entries, tables


def _decode(raw: bytes) -> str:
    """Bytes from a child process, made safe to put in a log field: replacement
    characters rather than an exception, and whitespace collapsed so a
    multi-line message stays one field."""
    return " ".join(raw.decode("utf-8", errors="replace").split())


def _decode_full(raw: bytes) -> str:
    """Bytes from a child process, kept line-structured for parsing. Never logged."""
    return raw.decode("utf-8", errors="replace")


def discard_spool_file(path: str) -> None:
    """Removes a spool file that will never be uploaded. Silent on failure --
    the caller is already raising, and clear_spool at the next startup is
    the backstop."""
    try:
        os.unlink(path)
    except OSError:
        pass
