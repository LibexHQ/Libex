"""
Cycle order and failure-policy tests.

precheck -> dump -> verify -> fan out -> prune -> record. Every dump.* call
is replaced at the runner's own import location, so nothing here starts a
subprocess or opens a socket; what is under test is which steps happen,
which do not, and what is recorded afterwards.

The failure policy is easy to get subtly wrong and expensive in exactly one
direction, so it is asserted step by step:

  - Precheck refuses -> no dump.
  - Dump fails -> nothing is pruned anywhere.
  - Verification fails -> the artefact is NOT uploaded, NOT deleted, and
    nothing is pruned at any destination. Keeping it is the point: it is
    the evidence.
  - A destination fails -> that destination is not pruned. The others carry
    on.
  - Retention cannot compute a keep set -> that destination is not pruned,
    and the cycle is still a success.

PRUNING ONLY EVER FOLLOWS THAT DESTINATION'S OWN SUCCESSFUL UPLOAD. That is
the one mistake in this file that destroys data instead of costing a cycle,
and the two-destination test below is what pins it: deleting yesterday's
copy on the strength of an upload that landed somewhere else is the same
class of error as deleting it on the strength of one that did not land at
all.

The retention half of the story lives in test_backup_retention.py, which
proves the keep set is right. This file proves the runner ACTS on it -- and
that when it cannot compute one, it deletes nothing.
"""

# Standard library
import asyncio
import signal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.services.backup import dump, retention
from app.services.backup.artifact import BackupArtifact, build_name, remote_artifact
from app.services.backup.destinations.base import DestinationError
from app.services.backup.dump import DumpError, PrecheckError, VerificationError, VerifyResult
from app.services.backup.runner import _IDLE_REMINDER_SECONDS, BackupRunner
from app.services.backup.schedule import BackupStatus, Schedule, read_status, write_status


NOW = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)
PERIOD = "2026-09-06"
ARTIFACT_NAME = build_name(NOW)


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    """
    BackupRunner.run() installs SIGTERM and SIGINT handlers bound to its own
    event loop, which outlive the test that created them: every later test
    in the process would then be one signal away from a call into a loop
    that has already closed. Saved and put back rather than avoided, because
    installing them is part of what run() is being tested for.
    """
    saved = {received: signal.getsignal(received) for received in (signal.SIGTERM, signal.SIGINT)}
    yield
    for received, handler in saved.items():
        signal.signal(received, handler)


def _settings(spool_dir, **overrides):
    values = {
        "backup_spool_dir": str(spool_dir),
        "database_url": "postgresql+asyncpg://libex:pw@db:5432/libex",
        "backup_dump_timeout_seconds": 3600,
        "backup_destination_timeout_seconds": 3600,
        "backup_retention_recent": 6,
        "backup_retention_aged_days": 30,
        "backup_period": "daily",
        "backup_time": "03:00",
        "backup_timezone": "UTC",
        "backup_day_of_week": "sunday",
        "backup_day_of_month": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeDestination:
    """
    Records what the runner asked it to do. A destination that fails does so
    by raising DestinationError, never by returning quietly -- a failure
    reporting itself as success would let pruning run against a server that
    did not receive this cycle's artefact.
    """

    provider = "fake"

    def __init__(self, name="fake", listing=None, upload_error=None, list_error=None, delete_error_for=()):
        self.name = name
        self.uploaded = []
        self.deleted = []
        self.listed = 0
        self._listing = list(listing or [])
        self._upload_error = upload_error
        self._list_error = list_error
        self._delete_error_for = set(delete_error_for)

    async def list(self):
        self.listed += 1
        if self._list_error is not None:
            raise self._list_error
        return list(self._listing)

    async def upload(self, artifact):
        if self._upload_error is not None:
            raise self._upload_error
        self.uploaded.append(artifact.name)

    async def delete(self, name):
        self.deleted.append(name)
        if name in self._delete_error_for:
            raise DestinationError("fake delete failed", destination=self.name, provider=self.provider, phase="delete")


def _at(days_ago):
    """An artefact named for `days_ago` days before the cycle's instant."""
    return remote_artifact(build_name(NOW - timedelta(days=days_ago)))


# The measured retention listing, as it would sit at a destination that has
# just accepted today's artefact: six daily copies, one 45 days old and one
# 200 days old.
SEEDED_LISTING = [remote_artifact(ARTIFACT_NAME), *[_at(d) for d in (1, 2, 3, 4, 5)], _at(45), _at(200)]


def _runner(spool_dir, destinations, **overrides):
    settings = _settings(spool_dir, **overrides)
    runner = BackupRunner(settings)
    runner.schedule = Schedule.from_settings(settings)
    runner.destinations = list(destinations)
    return runner


def _artifact(spool_dir, size_bytes=2_376_454_048):
    path = spool_dir / ARTIFACT_NAME
    path.write_bytes(b"an archive")
    return BackupArtifact(name=ARTIFACT_NAME, path=str(path), created_at=NOW, size_bytes=size_bytes)


def _patched(spool_dir, dump_error=None, verify_error=None, precheck_error=None):
    """
    Every dump.* call the cycle makes, replaced at the runner's import
    location. Returns a context manager stack the tests enter.
    """
    artifact = _artifact(spool_dir)
    patches = {
        "check_free_space": patch(
            "app.services.backup.runner.dump.check_free_space",
            side_effect=precheck_error,
            return_value=None,
        ),
        "run_pg_dump": patch(
            "app.services.backup.runner.dump.run_pg_dump",
            new=AsyncMock(side_effect=dump_error, return_value=artifact),
        ),
        "verify_artifact": patch(
            "app.services.backup.runner.dump.verify_artifact",
            new=AsyncMock(side_effect=verify_error, return_value=VerifyResult(size_bytes=artifact.size_bytes)),
        ),
        "now": patch("app.services.backup.runner._now", return_value=NOW),
    }
    return artifact, patches


class _Cycle:
    """Enters every patch for one cycle and hands back what the runner saw."""

    def __init__(self, spool_dir, **kwargs):
        self.artifact, self._patches = _patched(spool_dir, **kwargs)
        self.mocks = {}

    def __enter__(self):
        for name, patcher in self._patches.items():
            self.mocks[name] = patcher.__enter__()
        return self

    def __exit__(self, *exc_info):
        for patcher in reversed(list(self._patches.values())):
            patcher.__exit__(*exc_info)
        return False


# ============================================================
# THE HAPPY PATH
# ============================================================

@pytest.mark.asyncio
async def test_a_successful_cycle_uploads_prunes_and_records(tmp_path):
    destination = _FakeDestination(listing=SEEDED_LISTING)
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path):
        assert await runner._cycle() is True

    assert destination.uploaded == [ARTIFACT_NAME]
    status = read_status(str(tmp_path))
    assert status.last_success_period == PERIOD
    assert status.last_artifact == ARTIFACT_NAME
    assert status.destinations_ok == ["fake"]


@pytest.mark.asyncio
async def test_pruning_deletes_the_retention_delete_set_and_keeps_the_aged_survivor(tmp_path):
    """
    The two-tier rule acted on, end to end and by name. The 45-day artefact
    is not one of the six most recent and is kept anyway; the 200-day one is
    the only thing that goes. A keep-count of six would delete both.
    """
    destination = _FakeDestination(listing=SEEDED_LISTING)
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path):
        await runner._cycle()

    assert destination.deleted == [_at(200).name]


@pytest.mark.asyncio
async def test_the_spool_copy_is_discarded_once_the_destinations_have_it(tmp_path):
    """The spool is scratch, sized for one artefact, never the archive of
    record -- keeping a copy would leave the next cycle's precheck refusing
    on space to preserve a file nothing reads."""
    runner = _runner(tmp_path, [_FakeDestination()])

    with _Cycle(tmp_path) as cycle:
        await runner._cycle()

    assert not (tmp_path / cycle.artifact.name).exists()


@pytest.mark.asyncio
async def test_a_destination_holding_nothing_yet_has_nothing_pruned(tmp_path):
    destination = _FakeDestination(listing=[])
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path):
        await runner._cycle()

    assert destination.deleted == []


# ============================================================
# PRUNING ONLY EVER FOLLOWS THIS DESTINATION'S OWN UPLOAD
# ============================================================

@pytest.mark.asyncio
async def test_a_destination_that_did_not_receive_the_artefact_is_not_pruned(tmp_path):
    """
    The mistake that destroys data rather than costing a cycle. One
    unreachable server must not have yesterday's copy deleted on the
    strength of an upload that landed somewhere else.
    """
    failed = _FakeDestination(
        name="unreachable",
        listing=SEEDED_LISTING,
        upload_error=DestinationError("ftps operation failed", destination="unreachable", provider="fake", phase="upload"),
    )
    succeeded = _FakeDestination(name="reachable", listing=SEEDED_LISTING)
    runner = _runner(tmp_path, [failed, succeeded])

    with _Cycle(tmp_path):
        assert await runner._cycle() is True

    assert failed.deleted == []
    assert failed.listed == 0
    assert succeeded.deleted == [_at(200).name]


@pytest.mark.asyncio
async def test_one_failing_destination_does_not_cost_the_backup(tmp_path):
    failed = _FakeDestination(
        name="unreachable",
        upload_error=DestinationError("ftps operation failed", destination="unreachable", provider="fake", phase="upload"),
    )
    succeeded = _FakeDestination(name="reachable")
    runner = _runner(tmp_path, [failed, succeeded])

    with _Cycle(tmp_path):
        await runner._cycle()

    status = read_status(str(tmp_path))
    assert status.destinations_ok == ["reachable"]
    assert status.destinations_failed == ["unreachable"]
    assert status.last_result == "success"


@pytest.mark.asyncio
async def test_every_destination_failing_is_not_a_successful_cycle(tmp_path):
    destination = _FakeDestination(
        upload_error=DestinationError("ftps operation failed", destination="fake", provider="fake", phase="upload")
    )
    write_status(str(tmp_path), BackupStatus(last_success_period="2026-09-05", last_artifact="libex-20260905T030000Z.dump"))
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path):
        assert await runner._cycle() is False

    status = read_status(str(tmp_path))
    assert status.last_result == "failed"
    assert status.last_success_period == "2026-09-05"
    assert status.last_artifact == "libex-20260905T030000Z.dump"


@pytest.mark.asyncio
async def test_a_destination_raising_something_unexpected_is_isolated_and_named_by_class(tmp_path, caplog):
    """
    Anything reaching here is a bug rather than a transport failure -- a
    transport failure arrives as a DestinationError. The exception is not
    str()'d: the class name is the whole of what is safe to say about an
    object nothing here has inspected.
    """
    broken = _FakeDestination(name="broken", upload_error=RuntimeError("secret-bearing detail"))
    healthy = _FakeDestination(name="healthy")
    runner = _runner(tmp_path, [broken, healthy])

    with caplog.at_level("ERROR"):
        with _Cycle(tmp_path):
            assert await runner._cycle() is True

    assert healthy.uploaded == [ARTIFACT_NAME]
    assert any(record.__dict__.get("error_type") == "RuntimeError" for record in caplog.records)
    assert not any("secret-bearing detail" in record.getMessage() for record in caplog.records)


# ============================================================
# RETENTION THAT CANNOT BE COMPUTED PRUNES NOTHING
# ============================================================

@pytest.mark.asyncio
async def test_a_listing_retention_refuses_deletes_nothing_at_that_destination(tmp_path):
    """
    A duplicate name means the listing is not what it appears to be, and
    RetentionUnsafe is what says so. The runner's answer is to prune
    nothing: backups accumulating for one extra day costs disk, and getting
    it wrong in the other direction costs the artefact the whole scheme
    exists to preserve.
    """
    duplicated = [*SEEDED_LISTING, SEEDED_LISTING[0]]
    destination = _FakeDestination(listing=duplicated)
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path):
        assert await runner._cycle() is True

    assert destination.deleted == []


@pytest.mark.asyncio
async def test_an_artefact_from_the_future_deletes_nothing_at_that_destination(tmp_path):
    """One of the two clocks is wrong, which makes the age of every artefact
    in the listing suspect -- and age is the entire input to the aged tier."""
    ahead = remote_artifact(build_name(NOW + timedelta(days=1)))
    destination = _FakeDestination(listing=[*SEEDED_LISTING, ahead])
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path):
        await runner._cycle()

    assert destination.deleted == []


@pytest.mark.asyncio
async def test_a_misconfigured_recent_tier_deletes_nothing_anywhere(tmp_path):
    """recent=0 is the configuration that would empty every archive at once,
    which is exactly why it raises rather than being honoured."""
    first = _FakeDestination(name="one", listing=SEEDED_LISTING)
    second = _FakeDestination(name="two", listing=SEEDED_LISTING)
    runner = _runner(tmp_path, [first, second], backup_retention_recent=0)

    with _Cycle(tmp_path):
        assert await runner._cycle() is True

    assert (first.deleted, second.deleted) == ([], [])


@pytest.mark.asyncio
async def test_retention_refusing_is_still_a_successful_cycle(tmp_path):
    """The backup was taken and stored, which is the thing this container
    exists to do. Failing to tidy is its own ERROR and its own problem."""
    destination = _FakeDestination(listing=[*SEEDED_LISTING, SEEDED_LISTING[0]])
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path):
        await runner._cycle()

    assert read_status(str(tmp_path)).last_result == "success"


@pytest.mark.asyncio
async def test_a_listing_that_failed_deletes_nothing(tmp_path):
    destination = _FakeDestination(
        list_error=DestinationError("ftps operation failed", destination="fake", provider="fake", phase="list")
    )
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path):
        assert await runner._cycle() is True

    assert destination.deleted == []


@pytest.mark.asyncio
async def test_one_failed_delete_does_not_stop_the_rest(tmp_path):
    """Every name in the delete list was already judged safe to remove by a
    keep set computed in full, so continuing deletes nothing that was not
    already decided."""
    listing = [remote_artifact(ARTIFACT_NAME), *[_at(d) for d in (1, 2)], _at(198), _at(199), _at(200)]
    destination = _FakeDestination(listing=listing, delete_error_for={_at(200).name})
    runner = _runner(tmp_path, [destination], backup_retention_recent=3, backup_retention_aged_days=30)

    with _Cycle(tmp_path):
        await runner._cycle()

    assert set(destination.deleted) == {_at(199).name, _at(200).name}


# ============================================================
# FAILURES BEFORE ANYTHING IS UPLOADED
# ============================================================

@pytest.mark.asyncio
async def test_a_precheck_refusal_takes_no_dump(tmp_path):
    """A dump that fills the disk fails nine minutes in and leaves a partial
    behind; refusing to start costs a log line."""
    destination = _FakeDestination()
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path, precheck_error=PrecheckError("not enough free space", free_bytes=1)) as cycle:
        assert await runner._cycle() is False

    cycle.mocks["run_pg_dump"].assert_not_awaited()
    assert destination.uploaded == []
    assert read_status(str(tmp_path)).last_error_phase == "precheck"


@pytest.mark.asyncio
async def test_a_failed_dump_uploads_nothing_and_prunes_nothing(tmp_path):
    destination = _FakeDestination(listing=SEEDED_LISTING)
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path, dump_error=DumpError("pg_dump failed", returncode=1)):
        assert await runner._cycle() is False

    assert destination.uploaded == []
    assert destination.deleted == []
    assert read_status(str(tmp_path)).last_error_phase == "dump"


@pytest.mark.asyncio
async def test_a_concurrent_run_refused_by_the_spool_lock_is_recorded_as_a_dump_failure(tmp_path):
    """O_EXCL refusing the spool file is the concurrent-run case: a
    scheduled cycle and a manual --once meeting on the same artefact name.
    Two pg_dumps writing one file interleave into an archive that verifies
    as neither."""
    runner = _runner(tmp_path, [_FakeDestination()])

    with _Cycle(tmp_path, dump_error=FileExistsError("already there")):
        assert await runner._cycle() is False

    status = read_status(str(tmp_path))
    assert status.last_error_phase == "dump"
    assert status.last_error_type == "FileExistsError"


@pytest.mark.asyncio
async def test_a_failed_cycle_carries_the_previous_success_forward(tmp_path):
    """That record is what the schedule reads. Losing it would make the next
    start believe nothing has ever succeeded."""
    write_status(
        str(tmp_path),
        BackupStatus(
            last_success_period="2026-09-05",
            last_success_at="2026-09-05T03:00:00Z",
            last_artifact="libex-20260905T030000Z.dump",
            last_size_bytes=2_000_000_000,
        ),
    )
    runner = _runner(tmp_path, [_FakeDestination()])

    with _Cycle(tmp_path, dump_error=DumpError("pg_dump failed")):
        await runner._cycle()

    status = read_status(str(tmp_path))
    assert status.last_success_period == "2026-09-05"
    assert status.last_artifact == "libex-20260905T030000Z.dump"
    assert status.last_size_bytes == 2_000_000_000
    assert status.last_attempt_period == PERIOD


# ============================================================
# VERIFICATION FAILURE
# ============================================================

@pytest.mark.asyncio
async def test_an_unverified_artefact_is_never_uploaded(tmp_path):
    destination = _FakeDestination(listing=SEEDED_LISTING)
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path, verify_error=VerificationError("artefact is missing tables the schema declares", missing_count=1)):
        assert await runner._cycle() is False

    assert destination.uploaded == []


@pytest.mark.asyncio
async def test_an_unverified_artefact_is_never_the_reason_an_older_copy_is_pruned(tmp_path):
    destination = _FakeDestination(listing=SEEDED_LISTING)
    runner = _runner(tmp_path, [destination])

    with _Cycle(tmp_path, verify_error=VerificationError("artefact is missing tables the schema declares")):
        await runner._cycle()

    assert destination.deleted == []
    assert destination.listed == 0


@pytest.mark.asyncio
async def test_an_unverified_artefact_is_kept_because_it_is_the_evidence(tmp_path):
    """
    It occupies the spool, which may be what makes the next cycle's
    free-space precheck refuse. That is deliberate: this state wants a
    human, and the precheck's ERROR names the free and required bytes.
    """
    runner = _runner(tmp_path, [_FakeDestination()])

    with _Cycle(tmp_path, verify_error=VerificationError("artefact is missing tables")) as cycle:
        await runner._cycle()

    assert (tmp_path / cycle.artifact.name).exists()
    assert read_status(str(tmp_path)).last_error_phase == "verify"


# ============================================================
# WHAT A FAILURE IS ALLOWED TO SAY
# ============================================================

@pytest.mark.asyncio
async def test_a_recorded_failure_carries_a_phase_and_a_class_name_and_no_exception_text(tmp_path):
    """
    The status file sits on the spool volume at 0600 and is treated as
    though someone will read it, because someone diagnosing a failed backup
    will. Every field is an allowlist entry: no URL, no hostname, no token,
    and above all no str(exception).
    """
    runner = _runner(tmp_path, [_FakeDestination()])
    leaky = DumpError("pg_dump failed", pg_dump_stderr="FATAL: password authentication failed for user libex")

    with _Cycle(tmp_path, dump_error=leaky):
        await runner._cycle()

    status = read_status(str(tmp_path))
    assert status.last_error_type == "DumpError"
    assert status.last_error_phase == "dump"
    assert "password" not in str(status)


# ============================================================
# STARTING UP
# ============================================================

@pytest.mark.asyncio
async def test_a_run_with_no_destination_takes_no_dump_at_all(tmp_path):
    """Taking one with nowhere to put it spends nine minutes and gigabytes
    of disk to produce a file the next startup deletes."""
    runner = BackupRunner(_settings(tmp_path))

    with patch("app.services.backup.runner.build_destinations", return_value=[]):
        with patch("app.services.backup.runner.dump.run_pg_dump", new=AsyncMock()) as pg_dump:
            assert await runner.run(once=True) == 1

    pg_dump.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unusable_schedule_exits_rather_than_running_a_supervised_cycle(tmp_path):
    runner = BackupRunner(_settings(tmp_path, backup_timezone="Europe/Londun"))

    with patch("app.services.backup.runner.build_destinations", return_value=[_FakeDestination()]):
        assert await runner.run(once=True) == 2


@pytest.mark.asyncio
async def test_the_startup_line_names_the_destinations_rather_than_only_counting_them(tmp_path, caplog):
    """
    Settings uses extra="ignore", so a misspelled BACKUP_FTPS_ name leaves
    the destination silently absent with nothing anywhere reporting it. This
    line is the only place the difference between "configured" and "thought
    I configured" is visible.
    """
    runner = BackupRunner(_settings(tmp_path))

    with caplog.at_level("INFO"):
        with patch("app.services.backup.runner.build_destinations", return_value=[_FakeDestination(name="ftps")]):
            with _Cycle(tmp_path):
                await runner.run(once=True)

    starting = [r for r in caplog.records if r.getMessage() == "Backup: runner starting"]
    assert starting and starting[0].__dict__["destination_names"] == "ftps"


@pytest.mark.asyncio
async def test_a_supervised_run_reports_the_cycle_in_its_exit_code(tmp_path):
    destination = _FakeDestination()
    runner = BackupRunner(_settings(tmp_path))

    with patch("app.services.backup.runner.build_destinations", return_value=[destination]):
        with _Cycle(tmp_path):
            assert await runner.run(once=True) == 0

    assert destination.uploaded == [ARTIFACT_NAME]


@pytest.mark.asyncio
async def test_the_spool_is_cleared_at_startup(tmp_path):
    """
    The real defence against a stranded partial, rather than any amount of
    cleanup in an exception handler: a container killed mid-dump -- a
    deploy, an OOM, a host reboot -- runs no handler at all.
    """
    (tmp_path / "libex-20260905T030000Z.dump").write_bytes(b"leftover")
    (tmp_path / "libex-20260905T030000Z.dump.partial").write_bytes(b"half a leftover")
    runner = BackupRunner(_settings(tmp_path))

    with patch("app.services.backup.runner.build_destinations", return_value=[_FakeDestination()]):
        with _Cycle(tmp_path):
            await runner.run(once=True)

    assert not (tmp_path / "libex-20260905T030000Z.dump").exists()
    assert not (tmp_path / "libex-20260905T030000Z.dump.partial").exists()


@pytest.mark.asyncio
async def test_retention_unsafe_reaching_the_runner_is_the_prune_nothing_signal(tmp_path):
    """
    Stated on its own, without a cycle around it: keep_set raises, _prune
    returns, and delete is never called. Anything that turns this exception
    into a best guess turns "prune nothing" into "prune something".
    """
    destination = _FakeDestination(listing=SEEDED_LISTING)
    runner = _runner(tmp_path, [destination])

    with patch(
        "app.services.backup.runner.retention.keep_set",
        side_effect=retention.RetentionUnsafe("keep and delete sets overlap"),
    ):
        await runner._prune(destination, NOW)

    assert destination.deleted == []


# ============================================================
# THE LOOP AROUND THE CYCLE
# ============================================================
#
# Nothing above this line drives _loop -- every test calls _cycle() or
# run(once=True), which is the shape of coverage that let a hot spin at 294
# cycles a second ship green. The loop is where the pacing, the catch-up
# predicate and the in-process memory of the last success all live, and none
# of them is reachable through _cycle.
#
# _cycle, _back_off and _sleep_until are replaced per test rather than being
# allowed to run: the ladder under test is measured in minutes and hours, so
# what is asserted is the number _loop HANDS to the wait, never a wait that
# actually happens.

@pytest.mark.asyncio
async def test_a_failed_cycle_waits_before_trying_the_same_period_again(tmp_path):
    """
    is_due reads last_success_period, and a failed cycle deliberately
    carries that field forward unchanged, so the predicate is still true the
    microsecond the cycle returns. A loop that simply tries again spins at
    the speed of whatever failed -- measured at 294 cycles a second on a
    malformed DATABASE_URL, and at one full pg_dump every nine minutes when
    the destination is merely unreachable, each of those holding a snapshot
    against production for its whole run.
    """
    runner = _runner(tmp_path, [_FakeDestination()])
    waits = []

    async def record_backoff(self, seconds, period):
        waits.append(seconds)
        if len(waits) >= 6:
            runner._stop.set()

    runner._stop = asyncio.Event()
    with patch.object(BackupRunner, "_cycle", new=AsyncMock(return_value=False)) as cycle:
        with patch.object(BackupRunner, "_back_off", new=record_backoff):
            with patch("app.services.backup.runner._now", return_value=NOW):
                await runner._loop()

    assert cycle.await_count == 6
    assert waits == [300, 600, 1200, 2400, 3600, 3600]


@pytest.mark.asyncio
async def test_the_retry_wait_is_capped_at_the_next_scheduled_slot(tmp_path, caplog):
    """
    The schedule outranks the retry. A period that has been failing for
    hours must not still be sitting on an hour-long backoff when the next
    period's slot arrives -- that turns a retry policy into a missed backup.

    Half an hour before the 03:00 slot, an hour's backoff resolves to 03:00
    and not to 03:30.
    """
    runner = _runner(tmp_path, [_FakeDestination()])
    runner._stop = asyncio.Event()
    half_hour_before = datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc)
    slept = []

    async def stop_after_one(self, seconds):
        slept.append(seconds)
        runner._stop.set()

    with patch.object(BackupRunner, "_sleep_seconds", new=stop_after_one):
        with patch("app.services.backup.runner._now", return_value=half_hour_before):
            with caplog.at_level("WARNING"):
                await runner._back_off(3600.0, PERIOD)

    waited = [r for r in caplog.records if r.getMessage() == "Backup: the cycle failed, waiting before trying this period again"]
    assert waited[0].__dict__["retry_at"] == "2026-09-06T03:00:00Z"
    # And the tick, not the whole remaining span: the wait is recomputed from
    # the wall clock every minute, so an NTP step or a resumed container is
    # noticed rather than slept straight past.
    assert slept == [60.0]


@pytest.mark.asyncio
async def test_a_backoff_shorter_than_the_next_slot_is_left_alone(tmp_path, caplog):
    """The cap must not be reaching for a retry that has no problem. Five
    minutes after a 03:00 slot, the first backoff is five minutes, not the
    twenty-three and a half hours until tomorrow."""
    runner = _runner(tmp_path, [_FakeDestination()])
    runner._stop = asyncio.Event()
    just_after = datetime(2026, 9, 6, 3, 5, tzinfo=timezone.utc)

    async def stop_immediately(self, seconds):
        runner._stop.set()

    with patch.object(BackupRunner, "_sleep_seconds", new=stop_immediately):
        with patch("app.services.backup.runner._now", return_value=just_after):
            with caplog.at_level("WARNING"):
                await runner._back_off(300.0, PERIOD)

    waited = [r for r in caplog.records if r.getMessage() == "Backup: the cycle failed, waiting before trying this period again"]
    assert waited[0].__dict__["retry_at"] == "2026-09-06T03:10:00Z"


@pytest.mark.asyncio
async def test_the_backoff_starts_over_when_the_period_changes(tmp_path):
    """A period that failed all day does not get to start the next one
    throttled. The operator who fixes the destination overnight is entitled
    to a five-minute retry, not an hour of the previous day's escalation."""
    runner = _runner(tmp_path, [_FakeDestination()])
    runner._stop = asyncio.Event()
    waits = []

    async def record_backoff(self, seconds, period):
        waits.append((period, seconds))
        if len(waits) >= 4:
            runner._stop.set()

    day_one = datetime(2026, 9, 6, 3, 10, tzinfo=timezone.utc)
    day_two = datetime(2026, 9, 7, 3, 10, tzinfo=timezone.utc)
    clock = iter([day_one, day_one, day_one, day_two])

    with patch.object(BackupRunner, "_cycle", new=AsyncMock(return_value=False)):
        with patch.object(BackupRunner, "_back_off", new=record_backoff):
            with patch("app.services.backup.runner._now", side_effect=lambda: next(clock)):
                await runner._loop()

    assert waits == [
        ("2026-09-06", 300),
        ("2026-09-06", 600),
        ("2026-09-06", 1200),
        ("2026-09-07", 300),
    ]


@pytest.mark.asyncio
async def test_a_successful_period_is_not_run_again_even_when_the_status_write_was_lost(tmp_path):
    """
    write_status never raises, so a spool volume gone read-only makes a
    SUCCESSFUL cycle look unrun: the file records nothing, is_due stays
    true, and the loop uploads and prunes again immediately. Measured
    against the real keep set -- thirty cycles inside one hour turned six
    days of history into six copies from the last ten minutes, and every
    daily between the aged artefact and today was destroyed.

    The loop's own memory of the period it just backed up is what stops
    that, which is why the status file is never written here and the cycle
    still runs exactly once.
    """
    runner = _runner(tmp_path, [_FakeDestination()])
    runner._stop = asyncio.Event()

    async def stop_on_the_first_sleep(self, target):
        runner._stop.set()

    with patch.object(BackupRunner, "_cycle", new=AsyncMock(return_value=True)) as cycle:
        with patch.object(BackupRunner, "_sleep_until", new=stop_on_the_first_sleep):
            with patch("app.services.backup.runner._now", return_value=NOW):
                await runner._loop()

    assert cycle.await_count == 1
    assert read_status(str(tmp_path)).last_success_period == ""


@pytest.mark.asyncio
async def test_a_period_already_recorded_as_a_success_is_not_run_at_all(tmp_path):
    """The status file is the memory that survives a restart, as the loop's
    own field is the one that survives a failed write. A container restarted
    an hour after a successful backup waits for tomorrow."""
    write_status(str(tmp_path), BackupStatus(last_success_period=PERIOD))
    runner = _runner(tmp_path, [_FakeDestination()])
    runner._stop = asyncio.Event()

    async def stop_on_the_first_sleep(self, target):
        runner._stop.set()

    with patch.object(BackupRunner, "_cycle", new=AsyncMock(return_value=True)) as cycle:
        with patch.object(BackupRunner, "_sleep_until", new=stop_on_the_first_sleep):
            with patch("app.services.backup.runner._now", return_value=NOW):
                await runner._loop()

    cycle.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stop_request_ends_the_loop_without_starting_another_cycle(tmp_path):
    """Setting a flag is not stopping anything unless something consults it.
    The loop's condition is the first of the several places that has to."""
    runner = _runner(tmp_path, [_FakeDestination()])
    runner._stop = asyncio.Event()
    runner._stop.set()

    with patch.object(BackupRunner, "_cycle", new=AsyncMock(return_value=True)) as cycle:
        with patch("app.services.backup.runner._now", return_value=NOW):
            await runner._loop()

    cycle.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_sleep_wakes_on_a_stop_rather_than_running_out_the_clock(tmp_path):
    """Six hours is the idle reminder interval. A container asked to stop
    must not sit in one of those -- Docker's grace period is 3900 seconds and
    what follows it is SIGKILL."""
    runner = _runner(tmp_path, [_FakeDestination()])
    runner._stop = asyncio.Event()

    async def request_the_stop():
        await asyncio.sleep(0)
        runner._stop.set()

    asyncio.ensure_future(request_the_stop())
    await asyncio.wait_for(runner._sleep_seconds(_IDLE_REMINDER_SECONDS), 5)


@pytest.mark.asyncio
async def test_an_idle_runner_keeps_saying_why_rather_than_exiting(tmp_path, caplog):
    """
    docker-compose.yml sets restart: unless-stopped, so a clean exit is a
    restart loop that logs the same complaint at whatever rate Docker's
    backoff allows. And the one line at startup scrolls out of a log window
    within a day, while the symptom of the thing it warns about -- no
    backups -- is by nature invisible.
    """
    runner = _runner(tmp_path, [])
    runner._stop = asyncio.Event()
    reminders = []

    async def count_reminders(self, seconds):
        reminders.append(seconds)
        if len(reminders) >= 3:
            runner._stop.set()

    with patch.object(BackupRunner, "_sleep_seconds", new=count_reminders):
        with caplog.at_level("WARNING"):
            assert await runner._idle("no destination is configured") == 0

    idle = [r for r in caplog.records if r.getMessage() == "Backup: idle and taking no backups"]
    assert len(idle) == 3
    assert idle[0].__dict__["reason"] == "no destination is configured"
    assert reminders == [_IDLE_REMINDER_SECONDS] * 3


# ============================================================
# THE SPOOL LOCK
# ============================================================
#
# A real flock on a real file, never a mocked one. What is under test is
# whether two arrivals can both be past run()'s acquire, and a fake lock
# that returns whatever the test told it to answers a different question.
#
# The defect being closed: startup clears the spool before anything else
# happens, which unlinks the artefact a running dump is writing into.
# pg_dump's descriptor survives the unlink, so it goes on writing gigabytes
# into an inode with no name, exits 0 having produced a complete archive
# nobody can open, and the scheduled cycle dies at getsize() with "dump
# artefact disappeared" -- a message that names the symptom and points
# nowhere near the cause. So the ordering is the assertion: the lock is
# taken, and only then is anything removed.

def _hold_the_spool(spool_dir):
    """Claims the spool the way another backup process would, and returns
    the descriptor to release afterwards."""
    return dump.acquire_spool_lock(str(spool_dir))


@pytest.mark.asyncio
async def test_a_supervised_run_refused_by_the_spool_lock_exits_three(tmp_path):
    """Exit 3 rather than 1: nothing was dumped and nothing was touched, and
    the operator's next move is to wait or to stop the other process, not to
    go looking at their destination configuration."""
    held = _hold_the_spool(tmp_path)
    runner = BackupRunner(_settings(tmp_path))

    try:
        with patch("app.services.backup.runner.build_destinations", return_value=[_FakeDestination()]):
            with patch("app.services.backup.runner.dump.run_pg_dump", new=AsyncMock()) as pg_dump:
                assert await runner.run(once=True) == 3
    finally:
        dump.release_spool_lock(held)

    pg_dump.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_run_leaves_the_in_flight_artefact_where_it_is(tmp_path):
    """
    THE DEFECT, stated directly. A --once started while the scheduled cycle
    was dumping used to unlink the file that dump was writing into, because
    clear_spool ran before anything established whether this process owned
    the spool. The refused arrival must remove nothing at all.
    """
    in_flight = tmp_path / "libex-20260906T025959Z.dump"
    in_flight.write_bytes(b"a dump another process is still writing")
    held = _hold_the_spool(tmp_path)
    runner = BackupRunner(_settings(tmp_path))

    try:
        with patch("app.services.backup.runner.build_destinations", return_value=[_FakeDestination()]):
            assert await runner.run(once=True) == 3
    finally:
        dump.release_spool_lock(held)

    assert in_flight.read_bytes() == b"a dump another process is still writing"


@pytest.mark.asyncio
async def test_a_scheduled_run_refused_by_the_spool_lock_idles_rather_than_exiting(tmp_path):
    """restart: unless-stopped turns a clean exit into a restart loop. The
    second container stays up saying why, which is also what makes the
    collision visible for longer than one line."""
    held = _hold_the_spool(tmp_path)
    runner = BackupRunner(_settings(tmp_path))
    idled = []

    async def stop_after_one_reminder(self, reason):
        idled.append(reason)
        return 0

    try:
        with patch("app.services.backup.runner.build_destinations", return_value=[_FakeDestination()]):
            with patch.object(BackupRunner, "_idle", new=stop_after_one_reminder):
                assert await runner.run(once=False) == 0
    finally:
        dump.release_spool_lock(held)

    assert idled == ["the spool is claimed by another process"]


@pytest.mark.asyncio
async def test_the_lock_is_released_when_the_run_ends(tmp_path):
    """The descriptor IS the lock, held across one unbroken region and
    released in run()'s finally. A run that kept it would make the next
    container start impossible rather than merely the next --once."""
    runner = BackupRunner(_settings(tmp_path))

    with patch("app.services.backup.runner.build_destinations", return_value=[_FakeDestination()]):
        with _Cycle(tmp_path):
            assert await runner.run(once=True) == 0

    # No exception means the spool was free again.
    dump.release_spool_lock(_hold_the_spool(tmp_path))


@pytest.mark.asyncio
async def test_a_failed_supervised_run_still_releases_the_lock(tmp_path):
    """The finally, not the happy path. A cycle that fails must not leave
    the spool claimed by a process that has already exited -- although the
    kernel would release it at exit, the ordering is what this pins for the
    case where the run is one call inside a longer-lived process."""
    runner = BackupRunner(_settings(tmp_path))

    with patch("app.services.backup.runner.build_destinations", return_value=[_FakeDestination()]):
        with _Cycle(tmp_path, dump_error=DumpError("pg_dump failed")):
            assert await runner.run(once=True) == 1

    dump.release_spool_lock(_hold_the_spool(tmp_path))


@pytest.mark.asyncio
async def test_the_scheduled_container_holds_the_lock_for_its_whole_life_not_only_mid_cycle(tmp_path):
    """
    Measured rather than assumed, because it decides what a supervised
    --once does. The lock is taken in run() before the spool is cleared and
    released only in run()'s finally, and _loop sits inside that region --
    so a scheduled container waiting quietly for tomorrow's slot, having
    already recorded today as a success, still owns the spool.

    The consequence is that `docker compose run --rm ... --once` against a
    live stack exits 3 at every hour of the day, not merely during the nine
    minutes a dump is running. That is the collision being refused up front
    and it is the safe answer, but it is not the narrow window the exit
    code's description suggests, so it is pinned here rather than left to be
    rediscovered.
    """
    write_status(str(tmp_path), BackupStatus(last_success_period=PERIOD))
    runner = BackupRunner(_settings(tmp_path))
    entered = asyncio.Event()

    async def announce_and_wait(self, target):
        entered.set()
        await self._stop.wait()

    with patch("app.services.backup.runner.build_destinations", return_value=[_FakeDestination()]):
        with patch.object(BackupRunner, "_sleep_until", new=announce_and_wait):
            with patch("app.services.backup.runner._now", return_value=NOW):
                scheduled = asyncio.ensure_future(runner.run(once=False))
                await asyncio.wait_for(entered.wait(), 5)

                with pytest.raises(dump.SpoolBusy):
                    dump.acquire_spool_lock(str(tmp_path))

                runner._stop.set()
                assert await asyncio.wait_for(scheduled, 5) == 0


@pytest.mark.asyncio
async def test_an_idling_runner_with_no_destination_still_owns_the_spool(tmp_path):
    """
    The diagnostic cost of holding the lock across _idle, pinned as it
    behaves rather than as it reads best. A stack with nothing configured
    idles holding the spool, so an operator running --once against it is
    told the spool is claimed (exit 3) and not that no destination is
    configured (exit 1) -- the second of which is the answer to their actual
    problem, and the one they would get if no other container were up.

    Exit 3 is still the truthful report of what stopped this process: the
    lock genuinely was held, nothing was dumped, and nothing was removed.
    The refusal is not papered over here, it is recorded, so that a change
    to either exit code is a decision rather than a drift.
    """
    idling = BackupRunner(_settings(tmp_path))
    reminded = asyncio.Event()

    async def announce_and_wait(self, seconds):
        reminded.set()
        await self._stop.wait()

    with patch("app.services.backup.runner.build_destinations", return_value=[]):
        with patch.object(BackupRunner, "_sleep_seconds", new=announce_and_wait):
            first = asyncio.ensure_future(idling.run(once=False))
            await asyncio.wait_for(reminded.wait(), 5)

            supervised = BackupRunner(_settings(tmp_path))
            with patch("app.services.backup.runner.build_destinations", return_value=[]):
                assert await supervised.run(once=True) == 3

            idling._stop.set()
            assert await asyncio.wait_for(first, 5) == 0


@pytest.mark.asyncio
async def test_a_supervised_run_with_nothing_else_holding_the_spool_reports_the_missing_destination(tmp_path):
    """The other half of the pair above: with the spool free, the same
    command reaches the destination check and exits 1. Which of the two an
    operator sees depends entirely on whether another container is up."""
    runner = BackupRunner(_settings(tmp_path))

    with patch("app.services.backup.runner.build_destinations", return_value=[]):
        assert await runner.run(once=True) == 1
