"""
One cycle, and the loop around it.

    precheck -> dump -> verify -> fan out -> prune -> record

The runner owns the order and the failure policy and nothing else. The
arithmetic that decides when to run is in schedule.py, the policy that
decides what to keep is in retention.py, and the work is in dump.py and the
destinations -- all of which is what lets this file read as a sequence of
steps rather than as the place every rule ended up.

THIS FILE IS NEVER IMPORTED BY THE API. app/main.py and every route import
this package zero times, which is stricter than the seeder's
entry-points-only convention: the API process must not so much as construct
a Schedule. Backup runs in its own container, off its own entry point.

It does not configure logging either. The entry point calls setup_logging();
nothing here may call logging.basicConfig, which attaches a root handler and
in doing so un-mutes httpx and httpcore at INFO for the whole process.

FAILURE POLICY, in one place because it is easy to get subtly wrong:

  - Precheck refuses -> no dump, ERROR, nothing else happens.
  - Dump fails -> no artefact, ERROR, nothing is pruned anywhere.
  - Verification fails -> the artefact is NOT uploaded, is NOT deleted, and
    nothing is pruned at any destination. Keeping it is the point: an
    artefact that failed verification is the evidence.
  - A destination fails -> that destination is not pruned. The others carry
    on; one unreachable server does not cost the backup.
  - Retention cannot compute a keep set -> that destination is not pruned,
    and the cycle is still a success. Prune-nothing is always safe.
  - A failed cycle waits before trying again. It does not retry straight
    away and it does not give the period up.

PRUNING ONLY EVER FOLLOWS THAT DESTINATION'S OWN SUCCESSFUL UPLOAD. Never
after a failed one, never after another destination's, and never before.
Deleting yesterday's copy on the strength of an upload that did not land is
the one mistake in this file that destroys data instead of costing a cycle.

A FAILED CYCLE MUST NOT BE RETRIED IMMEDIATELY, and the reason is the whole
of _back_off. is_due consults last_success_period, and a failed cycle
deliberately carries that field forward unchanged, so the predicate is still
true the microsecond the cycle returns. A loop that simply tries again spins
at the speed of whatever failed: measured at 294 cycles a second on a
malformed DATABASE_URL -- 25 million ERROR lines a day, each one fsynced to
the spool volume and shipped to Axiom -- and at one full 2.38 GB pg_dump
every nine minutes, forever, when the destination is merely unreachable.
Every one of those dumps holds a snapshot against production for its whole
run. So the retry is paced, and the pacing climbs.
"""

# Standard library
import asyncio
import os
import signal
import threading
from datetime import datetime, timedelta, timezone

# Core
from app.core.config import get_settings
from app.core.logging import get_logger

# Services
from app.services.backup import dump, retention
from app.services.backup.artifact import BackupArtifact
from app.services.backup.destinations import Destination, build_destinations
from app.services.backup.destinations.base import DestinationError
from app.services.backup.schedule import (
    BackupStatus,
    Schedule,
    ScheduleError,
    read_status,
    utc_iso,
    write_status,
)


logger = get_logger()

# How long the loop sleeps before re-checking the clock. The wait is
# recomputed from the wall clock every tick rather than slept through in one
# call, so an NTP step, a daylight-saving change or a container suspended
# and resumed is noticed within a minute instead of being slept straight
# past.
_TICK_SECONDS = 60.0

# How often an idle runner with nothing to do repeats itself. A container
# that is up and permanently useless has to keep saying so: the one line at
# startup scrolls out of a log window within a day, and the symptom of the
# thing it is warning about -- no backups -- is by nature invisible.
_IDLE_REMINDER_SECONDS = 6 * 60 * 60

# What a failed cycle waits before trying the same period again, and the
# ceiling that wait climbs to as it keeps failing.
#
# Five minutes is long enough that nothing can spin and short enough that a
# transient failure -- a destination rebooting, a database still coming up
# -- costs one retry rather than a day. Doubling to an hour is what keeps a
# permanent failure from becoming its own outage: at the cap a broken
# deployment logs its complaint 24 times a day instead of 25 million, and
# takes at most 24 dumps rather than 160.
#
# The state lives in the loop, not in the status file. A restart resets it
# on purpose: a human who has just redeployed the container is entitled to
# an immediate attempt, and the thing being throttled is a tight loop within
# one process, which a restart has already ended.
_RETRY_BACKOFF_START_SECONDS = 5 * 60.0
_RETRY_BACKOFF_CAP_SECONDS = 60 * 60.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BackupRunner:
    """
    Holds what a run needs: the settings, the schedule, the destinations,
    and the two stop flags.

    Two flags rather than one because there are two kinds of waiter. _stop
    is an asyncio.Event, waited on by the sleep loop and by the wait for
    pg_dump; _abort is a threading.Event handed to every destination at
    construction, which is what a blocking transfer inside asyncio.to_thread
    can actually check. Cancelling the await would abandon that thread, not
    stop it -- and the interpreter then waits for it at exit.

    Both long phases of a cycle consult one of them. The upload half always
    did, inside storbinary's transfer callback; the dump half now does too,
    by handing _stop down to dump.run_pg_dump.
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self.spool_dir = self.settings.backup_spool_dir
        self._abort = threading.Event()
        self._stop: asyncio.Event | None = None
        self.schedule: Schedule | None = None
        self.destinations: list[Destination] = []

    # --- entry ------------------------------------------------------------

    async def run(self, once: bool = False) -> int:
        """
        Starts the runner. Returns a process exit code.

        In the scheduled form this does not return until it is asked to
        stop, INCLUDING when there is nothing it can usefully do. Exiting
        cleanly would be worse than idling: docker-compose.yml sets
        restart: unless-stopped, which turns a clean exit into a restart
        loop that logs the same complaint at whatever rate Docker's backoff
        allows. Idling leaves one container, up, saying exactly what is
        wrong.

        --once is the supervised form, where a human is watching, so it says
        what happened in the exit code instead.
        """
        self._stop = asyncio.Event()
        self._install_signal_handlers()

        try:
            self.schedule = Schedule.from_settings(self.settings)
        except ScheduleError as exc:
            logger.error(
                "Backup: the configured schedule cannot be used",
                extra={"error_type": type(exc).__name__, "detail": str(exc)},
            )
            if once:
                return 2
            return await self._idle("the configured schedule cannot be used")

        self.destinations = build_destinations(self.settings, self._abort)

        # Before the spool is touched, and before anything else. Everything
        # this process does to that directory -- clearing it, writing an
        # artefact into it, writing the status file -- assumes it is the
        # only backup process there, and until this call nothing enforced
        # that. See dump.acquire_spool_lock for what went wrong without it.
        try:
            lock = dump.acquire_spool_lock(self.spool_dir)
        except dump.DumpError as exc:
            logger.error(
                "Backup: the spool could not be claimed, this process will take no backups",
                extra={
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                    "spool_dir": self.spool_dir,
                    **dict(getattr(exc, "fields", {})),
                },
            )
            if once:
                return 3
            return await self._idle("the spool is claimed by another process")

        try:
            return await self._run_locked(once)
        finally:
            dump.release_spool_lock(lock)

    async def _run_locked(self, once: bool) -> int:
        """
        Everything from clearing the spool onward, with this process holding
        the spool lock for all of it.

        Split from run() so the lock has one acquire and one release around
        an unbroken region, rather than being taken and dropped around each
        step. Every path out of here -- an exit code, a stop, an exception
        -- passes through run()'s finally.
        """
        removed = dump.clear_spool(self.spool_dir)
        logger.info(
            "Backup: runner starting",
            extra={
                **self.schedule.describe(),
                "destinations": len(self.destinations),
                # Named, not just counted. Settings uses extra="ignore", so
                # a misspelled BACKUP_FTPS_ name leaves the destination
                # silently absent with nothing anywhere reporting it -- this
                # line is the only place the difference between "configured"
                # and "thought I configured" is visible.
                "destination_names": ",".join(d.name for d in self.destinations) or "none",
                "spool_cleared": removed,
                "spool_dir": self.spool_dir,
            },
        )

        if self.schedule.clamps:
            logger.info(
                "Backup: the configured day of month does not exist in every month and will clamp to the last day",
                extra={"day_of_month": self.schedule.day_of_month},
            )

        if not self.destinations:
            # No dump either, deliberately. Taking one with nowhere to put
            # it spends nine minutes and 2.38 GB of disk to produce a file
            # the next startup deletes.
            logger.error(
                "Backup: no destination is configured, no backups will be taken",
                extra={"destinations": 0},
            )
            if once:
                return 1
            return await self._idle("no destination is configured")

        if once:
            return 0 if await self._cycle(forced=True) else 1

        await self._loop()
        return 0

    # --- the loop ---------------------------------------------------------

    async def _loop(self) -> None:
        """
        Waits for the slot, runs the cycle, and paces the retry when it
        fails.

        Two pieces of state, both deliberately in-process and neither in the
        status file.

        `backoff` is how long the next failed attempt waits. It resets on a
        success and on a new period -- a period that failed all day does not
        get to start the next one throttled.

        `succeeded_period` is this process's own memory of the last period
        it backed up, and it exists because write_status never raises. A
        spool volume that has gone read-only makes a SUCCESSFUL cycle look
        unrun: the file records nothing, is_due stays true, and the loop
        uploads and prunes again immediately. Measured against the real keep
        set -- thirty cycles inside one hour turned six days of history into
        six copies from the last ten minutes, and every daily between the
        aged artefact and today was destroyed. The status file is the memory
        that survives a restart; this is the memory that survives a failed
        write, and one period is not backed up twice because a file could
        not be written.
        """
        backoff = _RETRY_BACKOFF_START_SECONDS
        backoff_period = ""
        succeeded_period = ""

        while not self._stop.is_set():
            status = read_status(self.spool_dir)
            now = _now()
            period = self.schedule.period_key(now)

            if succeeded_period != period and self.schedule.is_due(now, status.last_success_period):
                if backoff_period != period:
                    backoff_period = period
                    backoff = _RETRY_BACKOFF_START_SECONDS

                if not status.last_success_period:
                    # Worth its own line: a container started at 4pm with a
                    # 03:00 schedule dumps now rather than waiting until
                    # tomorrow. Named so it reads as a decision instead of a
                    # mystery run.
                    logger.info(
                        "Backup: first run, taking a backup now rather than waiting for the next slot",
                        extra={"period": period},
                    )
                elif status.last_success_period != period:
                    logger.info(
                        "Backup: this period has no recorded success and its slot has passed, catching up",
                        extra={"period": period, "last_success_period": status.last_success_period},
                    )

                if await self._cycle():
                    succeeded_period = period
                    backoff = _RETRY_BACKOFF_START_SECONDS
                    continue

                await self._back_off(backoff, period)
                backoff = min(backoff * 2, _RETRY_BACKOFF_CAP_SECONDS)
                continue

            await self._sleep_until(self.schedule.next_slot_after(_now()))

    async def _idle(self, reason: str) -> int:
        """Stays up, doing nothing, repeating why. See run()."""
        while not self._stop.is_set():
            logger.warning("Backup: idle and taking no backups", extra={"reason": reason})
            await self._sleep_seconds(_IDLE_REMINDER_SECONDS)
        return 0

    async def _back_off(self, seconds: float, period: str) -> None:
        """
        Waits after a failed cycle: `seconds`, or until the next scheduled
        slot, whichever comes first.

        Capped at the next slot because the schedule outranks the retry. A
        period that has been failing for hours must not still be sitting on
        an hour-long backoff when the next period's slot arrives -- that
        would turn a retry policy into a missed backup.
        """
        resume = min(_now() + timedelta(seconds=seconds), self.schedule.next_slot_after(_now()))
        logger.warning(
            "Backup: the cycle failed, waiting before trying this period again",
            extra={
                "period": period,
                "backoff_seconds": int(seconds),
                "retry_at": utc_iso(resume),
            },
        )
        while not self._stop.is_set():
            remaining = (resume - _now()).total_seconds()
            if remaining <= 0:
                return
            await self._sleep_seconds(min(remaining, _TICK_SECONDS))

    async def _sleep_until(self, target: datetime) -> None:
        logger.info(
            "Backup: waiting for the next scheduled run",
            extra={"next_run": utc_iso(target), "period": self.schedule.period_key(target)},
        )
        while not self._stop.is_set():
            remaining = (target - _now()).total_seconds()
            if remaining <= 0:
                return
            await self._sleep_seconds(min(remaining, _TICK_SECONDS))

    async def _sleep_seconds(self, seconds: float) -> None:
        """Sleeps, but wakes immediately on a stop request."""
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    # --- one cycle --------------------------------------------------------

    async def _cycle(self, forced: bool = False) -> bool:
        """
        precheck -> dump -> verify -> fan out -> prune -> record.

        Returns whether the cycle produced a backup that reached at least
        one destination. The status file is written on every path out,
        including every failure: a cycle that leaves no trace is one nobody
        can diagnose.
        """
        started = _now()
        period = self.schedule.period_key(started)
        previous = read_status(self.spool_dir)
        artifact: BackupArtifact | None = None
        verified = False

        logger.info(
            "Backup: cycle starting",
            extra={"period": period, "forced": forced, "destinations": len(self.destinations)},
        )

        try:
            dump.check_free_space(self.spool_dir, previous.last_size_bytes)
        except dump.PrecheckError as exc:
            return self._record_failure(previous, period, started, "precheck", exc)

        try:
            target = dump.parse_database_url(self.settings.database_url)
        except dump.PrecheckError as exc:
            return self._record_failure(previous, period, started, "precheck", exc)

        try:
            artifact = await dump.run_pg_dump(
                target,
                self.spool_dir,
                started,
                float(self.settings.backup_dump_timeout_seconds),
                self._stop,
            )
        except dump.DumpError as exc:
            # Including dump.DumpAborted, which is a stop request noticed
            # while pg_dump was still running. It is a failed cycle and is
            # recorded as one -- no artefact exists and the period has not
            # been backed up -- and its own class name in the status file is
            # what tells an operator the container was stopped rather than
            # that the database was unreachable.
            return self._record_failure(previous, period, started, "dump", exc)
        except OSError as exc:
            # O_EXCL refusing the spool file. The spool lock makes this
            # nearly unreachable -- two backup processes cannot both be past
            # run()'s acquire -- so what is left here is a genuine
            # filesystem refusal, or a leftover this run's clear_spool could
            # not remove. Recorded rather than swallowed either way.
            return self._record_failure(previous, period, started, "dump", exc)

        logger.info(
            "Backup: dump complete",
            extra={
                "artifact": artifact.name,
                "size_bytes": artifact.size_bytes,
                "seconds": round((_now() - started).total_seconds(), 1),
            },
        )

        try:
            result = await dump.verify_artifact(artifact, previous.last_size_bytes, self._stop)
            verified = True
            logger.info(
                "Backup: artefact verified",
                extra={
                    "artifact": artifact.name,
                    "toc_entries": result.toc_entries,
                    "tables_found": result.tables_found,
                    "shrank": result.shrank,
                },
            )
        except dump.DumpError as exc:
            # DumpError rather than VerificationError alone, because this
            # step can also end in a stop request or its own timeout, and
            # neither of those is a verdict on the artefact. All three are
            # treated the same way, which is the conservative one.
            #
            # The file stays where it is. It is the only evidence of what
            # went wrong, nothing is uploaded, and nothing is pruned at any
            # destination -- an unverified artefact must never be the reason
            # an older good one is deleted. The next startup's clear_spool
            # removes it, and until then it occupies the spool, which may be
            # what makes the next cycle's free-space precheck refuse. That
            # is deliberate: this state wants a human, and the precheck's
            # ERROR names the free and required bytes.
            return self._record_failure(previous, period, started, "verify", exc, artifact=artifact)

        try:
            succeeded, failed, error_types = await self._fan_out(artifact, _now())
        except Exception as exc:
            # _fan_out gathers with return_exceptions=True and is not meant
            # to raise. If it ever does, the cycle must still leave a
            # record: without this the exception travelled out of _cycle,
            # past _loop and past run(), and the one attempt nobody could
            # diagnose was the one that failed in an unexpected way.
            return self._record_failure(previous, period, started, "fanout", exc, artifact=artifact)
        finally:
            if verified:
                # Removed whatever the destinations did. The spool is sized
                # for one artefact and is scratch, never the archive of
                # record; keeping a failed cycle's copy would leave the next
                # cycle's precheck refusing on space to preserve a file
                # nothing reads. A dump is reproducible -- the next cycle
                # takes a fresh one.
                dump.discard_spool_file(artifact.path)

        status = BackupStatus(
            last_success_period=period if succeeded else previous.last_success_period,
            last_success_at=utc_iso(started) if succeeded else previous.last_success_at,
            last_artifact=artifact.name if succeeded else previous.last_artifact,
            last_size_bytes=artifact.size_bytes if succeeded else previous.last_size_bytes,
            last_attempt_period=period,
            last_attempt_at=utc_iso(started),
            last_result="success" if succeeded else "failed",
            last_error_phase="" if succeeded else "fanout",
            # The class name of the first destination that failed, never a
            # fixed string. This field held the literal "DestinationError"
            # regardless of what actually happened, which meant a
            # destination failing with anything else -- the case _fan_out
            # explicitly handles and logs by its real class -- was recorded
            # here under a class it never raised. The category label is
            # last_error_phase, immediately above; this one is a class name
            # everywhere else in the file and is one here too.
            last_error_type="" if succeeded else (error_types[0] if error_types else ""),
            destinations_ok=succeeded,
            destinations_failed=failed,
        )
        write_status(self.spool_dir, status)

        logger.info(
            "Backup: cycle finished",
            extra={
                "period": period,
                "artifact": artifact.name,
                "size_bytes": artifact.size_bytes,
                "destinations_ok": ",".join(succeeded) or "none",
                "destinations_failed": ",".join(failed) or "none",
                "seconds": round((_now() - started).total_seconds(), 1),
            },
        )
        return bool(succeeded)

    async def _fan_out(
        self, artifact: BackupArtifact, now: datetime
    ) -> tuple[list[str], list[str], list[str]]:
        """
        Uploads to every destination at once, then prunes each one that
        accepted it.

        Returns the names that succeeded, the names that failed, and the
        class name of each failure in the same order as the second list --
        which is what the status file records, rather than a fixed string
        standing in for whatever went wrong.

        Concurrent because the destinations are independent and a slow one
        should not delay a fast one -- and because each opens its own handle
        on the spool file, which is exactly why BackupArtifact carries a
        path rather than an open file object: one shared handle has one
        shared read position, and two destinations reading from it would
        each send part of the archive and both report success.

        Deliberately not wrapped in asyncio.wait_for. A timeout here would
        cancel the await while the blocking transfer carried on inside its
        thread, unwatched, holding the file open, with the interpreter
        waiting for it at exit. The bound lives inside the destination,
        where the work is.
        """
        results = await asyncio.gather(
            *(self._to_destination(destination, artifact, now) for destination in self.destinations),
            return_exceptions=True,
        )

        succeeded: list[str] = []
        failed: list[str] = []
        error_types: list[str] = []
        for destination, result in zip(self.destinations, results, strict=True):
            if result is True:
                succeeded.append(destination.name)
                continue
            failed.append(destination.name)
            if isinstance(result, BaseException) and not isinstance(result, DestinationError):
                # Anything that reached here is a bug rather than a
                # transport failure -- a transport failure arrives as a
                # DestinationError, already logged with its fields. The
                # exception is not str()'d: the class name is the whole of
                # what is safe to say about an object nothing here has
                # inspected.
                logger.error(
                    "Backup: destination raised an unexpected error",
                    extra={
                        "destination": destination.name,
                        "provider": destination.provider,
                        "error_type": type(result).__name__,
                    },
                )
            # A plain False is _to_destination's own report of an upload
            # that raised DestinationError, which it caught and logged; that
            # is the only way it returns False, so the class is known here
            # without the exception object.
            error_types.append(
                type(result).__name__ if isinstance(result, BaseException) else DestinationError.__name__
            )
        return succeeded, failed, error_types

    async def _to_destination(self, destination: Destination, artifact: BackupArtifact, now: datetime) -> bool:
        """
        One destination's whole share of the cycle: upload, then prune.

        Returns True if the artefact landed. A pruning failure does not make
        it False -- the backup was taken and stored, which is the thing this
        container exists to do; the failure to tidy is its own ERROR and its
        own problem.
        """
        try:
            await destination.upload(artifact)
        except DestinationError as exc:
            logger.error("Backup: upload failed", extra=exc.fields)
            return False

        logger.info(
            "Backup: artefact uploaded",
            extra={
                "destination": destination.name,
                "provider": destination.provider,
                "artifact": artifact.name,
                "size_bytes": artifact.size_bytes,
            },
        )

        try:
            await self._prune(destination, now)
        except Exception as exc:
            # The sentence above -- a pruning failure does not make this
            # False -- was true of DestinationError and of nothing else.
            # Anything wider propagated out of here, reached _fan_out as an
            # unexpected error, and recorded a destination whose upload
            # SUCCEEDED as failed, which then fed the retry. Retention's own
            # arithmetic could reach it: an enormous aged_days raised
            # OverflowError straight past _prune's RetentionUnsafe handler.
            # That is fixed at its source as well; this is what makes the
            # sentence true for the next one.
            logger.error(
                "Backup: pruning raised an unexpected error, nothing further was deleted at this destination",
                extra={
                    "destination": destination.name,
                    "provider": destination.provider,
                    "error_type": type(exc).__name__,
                },
            )
        return True

    async def _prune(self, destination: Destination, now: datetime) -> None:
        """
        Applies the keep set to one destination, having just uploaded to it.

        The whole keep set is computed before a single delete is issued.
        There is no walk-and-delete form: a walk that fails halfway has
        already destroyed part of the archive on the strength of a decision
        it never finished making.

        Every uncertainty ends the same way -- prune nothing. A listing that
        failed, a clock that disagrees, a duplicate name: none of them is
        allowed to be the reason the aged artefact disappears, because that
        artefact is the single copy this whole scheme exists to preserve.

        EVERY DELETE IS NAMED IN THE LOG. This is the only step in the
        package that destroys anything, and counts alone left no way to
        reconstruct afterwards what had been removed -- including whether
        the one artefact the whole scheme protects was among it. The names
        are safe to log because they are not server strings: every one comes
        back through artifact.remote_artifact(), which returns None for
        anything that does not match our own pattern, so what is printed
        here was written by this codebase. Entries that are not ours never
        reach this function at all, and the destination logs those as a
        count.
        """
        fields = {"destination": destination.name, "provider": destination.provider}
        try:
            listing = await destination.list()
        except DestinationError as exc:
            logger.error("Backup: listing failed, pruning nothing at this destination", extra=exc.fields)
            return

        try:
            keep, delete = retention.keep_set(
                listing,
                now,
                recent=int(self.settings.backup_retention_recent),
                aged_days=int(self.settings.backup_retention_aged_days),
            )
        except retention.RetentionUnsafe as exc:
            logger.error(
                "Backup: retention could not be computed with confidence, pruning nothing at this destination",
                extra={**fields, "error_type": type(exc).__name__, "detail": str(exc), "listed": len(listing)},
            )
            return

        if not delete:
            logger.info("Backup: nothing to prune", extra={**fields, "kept": len(keep)})
            return

        removed = 0
        for artifact in delete:
            try:
                await destination.delete(artifact.name)
                removed += 1
                # Per artefact rather than only in the summary below: a
                # prune interrupted part way through never reaches the
                # summary, and that is exactly the run whose record is
                # needed.
                logger.info("Backup: deleted an old artefact", extra={**fields, "artifact": artifact.name})
            except DestinationError as exc:
                # One failed delete does not stop the rest. Every name in
                # this list was already judged safe to remove by a keep set
                # computed in full, so continuing deletes nothing that was
                # not already decided.
                logger.error(
                    "Backup: could not delete an old artefact",
                    extra={**exc.fields, "artifact": artifact.name},
                )

        logger.info(
            "Backup: pruned old artefacts",
            extra={
                **fields,
                "kept": len(keep),
                "deleted": removed,
                "delete_failed": len(delete) - removed,
                # What is still there, by name. Bounded by the keep set
                # itself -- the recent tier plus at most two -- so this is a
                # short line, and it is the one that says whether the aged
                # artefact survived the night.
                "kept_artifacts": ",".join(artifact.name for artifact in keep),
            },
        )

    # --- failure recording -------------------------------------------------

    def _record_failure(
        self,
        previous: BackupStatus,
        period: str,
        started: datetime,
        phase: str,
        exc: BaseException,
        artifact: BackupArtifact | None = None,
    ) -> bool:
        """
        Logs a failed cycle and writes it to the status file.

        The exception object itself never appears. What goes out is the
        phase, the class name, and whatever allowlisted fields the exception
        chose to carry -- a fixed set of scalars written by this codebase,
        never a connection string and never a str() of anything.

        ONE OF THOSE FIELDS IS A SERVER RESPONSE, and it is deliberate
        rather than an oversight: dump.py puts a capped, whitespace-
        collapsed copy of pg_dump's stderr in pg_dump_stderr, because
        without it a failed backup says only "exit 1". libpq does not echo
        PGPASSWORD or any other credential into a diagnostic -- its
        authentication failures name the user and never the password -- and
        the reasoning is written out at the raise site so it can be
        re-checked rather than taken on trust. Nothing else in this package
        carries remote text: destination failures reach here as a class
        name, a phase and an integer reply code.

        The previous success is carried forward untouched. A failed cycle
        must not overwrite the record of the last one that worked: that
        record is what the schedule reads, and losing it would make the next
        start believe nothing has ever succeeded. It is also why the loop
        has to pace its retry -- carrying the record forward leaves is_due
        true, so nothing else would stop it trying again immediately.
        """
        fields = dict(getattr(exc, "fields", {}))
        logger.error(
            "Backup: cycle failed",
            extra={
                "period": period,
                "phase": phase,
                "error_type": type(exc).__name__,
                "detail": str(exc) if isinstance(exc, (dump.DumpError, retention.RetentionUnsafe)) else "",
                **fields,
            },
        )
        write_status(
            self.spool_dir,
            BackupStatus(
                last_success_period=previous.last_success_period,
                last_success_at=previous.last_success_at,
                last_artifact=previous.last_artifact,
                last_size_bytes=previous.last_size_bytes,
                last_attempt_period=period,
                last_attempt_at=utc_iso(started),
                last_result="failed",
                last_error_phase=phase,
                last_error_type=type(exc).__name__,
                destinations_ok=[],
                destinations_failed=[d.name for d in self.destinations],
            ),
        )
        if artifact is not None:
            logger.error(
                "Backup: the failed artefact has been kept for inspection and will be removed at the next start",
                extra={"artifact": artifact.name, "spool_dir": self.spool_dir},
            )
        return False

    # --- stopping ----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """
        SIGTERM and SIGINT: set both stop flags and let the current step
        finish or abort itself.

        The handler runs in the main thread, between bytecodes, so it does
        the smallest possible amount of work: setting the threading.Event
        that blocking transfers poll, and asking the loop to set its own
        Event on the next pass. Nothing here cancels a task.

        SETTING A FLAG IS NOT STOPPING ANYTHING. Every long-running step has
        to consult one of the two, and each of them does:

          - the sleeps wait on _stop rather than on a timer
          - the FTPS transfer polls _abort inside storbinary's callback,
            which is the only code of ours that runs during a STOR
          - the wait for pg_dump and for pg_restore watches _stop alongside
            the child, and terminates the child when it fires

        That last one was missing, and the shape of the gap is the lesson.
        The handler set both flags correctly, dump.py had a CancelledError
        branch that terminated pg_dump properly, and none of it ran: _cycle
        is awaited directly and nothing ever cancelled it, so the branch was
        dead code on the signal path and the await simply sat there. SIGTERM
        at one second into an eight-second child left the cycle still
        running sixteen seconds later, with pg_dump alive. Against a real
        dump that is up to 65 minutes of stop_grace_period spent holding
        ACCESS SHARE on every table -- behind which a queued ACCESS
        EXCLUSIVE and every innocent reader arriving after it both block.
        """
        loop = asyncio.get_running_loop()

        def handle(*_args) -> None:
            if not self._abort.is_set():
                logger.info("Backup: stop requested")
            self._abort.set()
            loop.call_soon_threadsafe(self._stop.set)

        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, handle)


async def run_backup(once: bool = False, settings=None) -> int:
    """The one call the entry point makes. Returns a process exit code."""
    if settings is None:
        settings = get_settings()
    os.makedirs(settings.backup_spool_dir, exist_ok=True)
    return await BackupRunner(settings).run(once=once)
