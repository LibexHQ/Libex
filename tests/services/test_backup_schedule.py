"""
Schedule arithmetic and status-file tests.

Everything here needs a datetime and a temp directory, which is the payoff
for schedule.py being separate from the runner: the daylight-saving cases,
the day-31 clamp and the catch-up predicate would otherwise each need a
subprocess and a fake clock.

The two daylight-saving cases are the ones that only come round once a year
and are therefore never caught in time:

  - THE SPRING GAP. Europe/London on 2026-03-29 has no 01:30 -- the clocks
    go from 01:00 GMT to 02:00 BST. A schedule that skips the period is an
    annual missing backup that nothing reports, so the slot resolves to the
    first instant that does exist.
  - THE AUTUMN REPEAT. On 2026-10-25 01:30 happens twice, once at +01:00 and
    once at +00:00. It must run once, and the tests below pin the MECHANISM
    that makes that true rather than the outcome: success is recorded
    against a period key, both occurrences share a key, so the predicate is
    already false the second time. Nothing special-cases the date.
  - A GAP THAT RUNS TO MIDNIGHT. Two zones jump at 23:00, so resolving
    forward lands on the following local date -- an instant that belongs to
    the next period and that no `now` inside this one can ever reach. The
    period is then skipped with nothing logged, because nothing failed.
    That case has its own section at the foot of this file, with the two
    zones and the two configured times that reach it, and with the two
    historical gaps that bound how far the scan has to look.

Period arithmetic happens in the configured timezone, never in UTC. The
Pacific/Auckland case is what that costs when it is got wrong -- a 15:30Z
instant belongs to the 7th there and the 6th in UTC, and a run recorded
against the wrong day is a run that happens twice or not at all.
"""

# Standard library
import json
import os
import stat
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

# Third party
import pytest

# Local
from app.services.backup.schedule import (
    STATUS_FILENAME,
    BackupStatus,
    Schedule,
    ScheduleError,
    read_status,
    status_path,
    utc_iso,
    write_status,
)


def _settings(**overrides):
    """The BACKUP_* scalars Schedule.from_settings reads, at their config.py
    defaults unless a test says otherwise."""
    values = {
        "backup_period": "daily",
        "backup_time": "03:00",
        "backup_timezone": "UTC",
        "backup_day_of_week": "sunday",
        "backup_day_of_month": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


LONDON = ZoneInfo("Europe/London")
AUCKLAND = ZoneInfo("Pacific/Auckland")


# ============================================================
# CONSTRUCTION AND VALIDATION
# ============================================================

def test_from_settings_builds_the_configured_schedule():
    schedule = Schedule.from_settings(
        _settings(backup_period="weekly", backup_time="03:15", backup_timezone="Europe/London", backup_day_of_week="Sunday")
    )

    assert schedule.period == "weekly"
    assert (schedule.hour, schedule.minute) == (3, 15)
    assert schedule.timezone_name == "Europe/London"
    assert schedule.weekday == 6


@pytest.mark.parametrize(
    "overrides",
    [
        {"backup_period": "hourly"},
        {"backup_timezone": "Europe/Londun"},
        {"backup_time": "3pm"},
        {"backup_time": "24:00"},
        {"backup_time": "03:60"},
        {"backup_period": "weekly", "backup_day_of_week": "someday"},
        {"backup_period": "monthly", "backup_day_of_month": 0},
        {"backup_period": "monthly", "backup_day_of_month": 32},
    ],
)
def test_an_unusable_setting_raises_schedule_error(overrides):
    """
    Validated here rather than in Settings on purpose: a value pydantic
    rejects raises while Settings is being constructed, before any logging
    exists to report it, and under restart: unless-stopped that is a silent
    restart loop instead of a sentence.
    """
    with pytest.raises(ScheduleError):
        Schedule.from_settings(_settings(**overrides))


def test_a_weekday_name_is_only_required_for_a_weekly_schedule():
    """A daily deployment that never touched BACKUP_DAY_OF_WEEK must not be
    refused for a field its period ignores."""
    schedule = Schedule.from_settings(_settings(backup_day_of_week="whenever"))
    assert schedule.period == "daily"


def test_describe_carries_configuration_and_nothing_derived():
    weekly = Schedule.from_settings(_settings(backup_period="weekly", backup_day_of_week="friday"))
    assert weekly.describe() == {"period": "weekly", "at": "03:00", "timezone": "UTC", "day_of_week": "friday"}

    monthly = Schedule.from_settings(_settings(backup_period="monthly", backup_day_of_month=15))
    assert monthly.describe() == {"period": "monthly", "at": "03:00", "timezone": "UTC", "day_of_month": 15}


@pytest.mark.parametrize(
    ("period", "day", "expected"),
    [("monthly", 31, True), ("monthly", 29, True), ("monthly", 28, False), ("daily", 31, False)],
)
def test_clamps_reports_only_a_schedule_that_can_land_off_the_end_of_a_month(period, day, expected):
    schedule = Schedule.from_settings(_settings(backup_period=period, backup_day_of_month=day))
    assert schedule.clamps is expected


def test_schedule_error_is_not_on_the_api_exception_hierarchy():
    from app.core.exceptions import LibexException

    assert not issubclass(ScheduleError, LibexException)


# ============================================================
# DAYLIGHT SAVING: THE SPRING GAP
# ============================================================

def test_a_configured_time_that_does_not_exist_resolves_to_the_next_real_instant():
    """
    2026-03-29, Europe/London: 01:00 GMT becomes 02:00 BST, so no clock in
    that zone ever reads 01:30. The slot is the instant the gap ends.
    """
    schedule = Schedule.from_settings(_settings(backup_time="01:30", backup_timezone="Europe/London"))

    slot = schedule.slot_for(datetime(2026, 3, 29, 0, 5, tzinfo=timezone.utc))

    assert slot.strftime("%Y-%m-%d %H:%M") == "2026-03-29 02:00"
    assert slot.utcoffset() == timedelta(hours=1)
    assert slot.astimezone(timezone.utc) == datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)


def test_the_spring_gap_does_not_skip_the_period():
    """The alternative to resolving forward is an annual missing backup that
    nothing reports, which is exactly the failure nobody notices."""
    schedule = Schedule.from_settings(_settings(backup_time="01:30", backup_timezone="Europe/London"))
    after_the_gap = datetime(2026, 3, 29, 3, 0, tzinfo=timezone.utc)

    assert schedule.is_due(after_the_gap, "") is True
    assert schedule.period_key(after_the_gap) == "2026-03-29"


def test_a_time_outside_the_gap_on_the_same_day_is_untouched():
    """The scan must not be reaching for a schedule that has no problem."""
    schedule = Schedule.from_settings(_settings(backup_time="04:30", backup_timezone="Europe/London"))

    slot = schedule.slot_for(datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc))

    assert slot.strftime("%H:%M") == "04:30"


# ============================================================
# DAYLIGHT SAVING: THE AUTUMN REPEAT
# ============================================================

def test_a_repeated_hour_resolves_to_the_first_occurrence():
    """
    2026-10-25, Europe/London: 01:30 happens at +01:00 and again an hour
    later at +00:00. fold=0 is the earlier one, which is the one an
    operator means by "the backup runs at 01:30".
    """
    schedule = Schedule.from_settings(_settings(backup_time="01:30", backup_timezone="Europe/London"))

    slot = schedule.slot_for(datetime(2026, 10, 25, 0, 5, tzinfo=timezone.utc))

    assert slot.utcoffset() == timedelta(hours=1)
    # Converted rather than compared across zones: an inter-zone comparison
    # ignores fold (PEP 495), so it would answer the same for either
    # occurrence and prove nothing about which one this is.
    assert slot.astimezone(timezone.utc) == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)


def test_both_occurrences_of_a_repeated_hour_share_one_period_key():
    """
    The mechanism, stated on its own. Running once is not a rule about
    October -- it is that success is recorded against a period, and both
    passes through 01:30 are the same local day.
    """
    schedule = Schedule.from_settings(_settings(backup_time="01:30", backup_timezone="Europe/London"))
    first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    second = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)

    assert first.astimezone(LONDON).strftime("%H:%M") == "01:30"
    assert second.astimezone(LONDON).strftime("%H:%M") == "01:30"
    assert schedule.period_key(first) == schedule.period_key(second) == "2026-10-25"


def test_the_second_pass_through_a_repeated_hour_does_not_run_again(tmp_path):
    """
    End to end through the real status file, because the file is what makes
    the mechanism true across a restart -- the process that ran at the first
    01:30 may not be the process that reaches the second.
    """
    schedule = Schedule.from_settings(_settings(backup_time="01:30", backup_timezone="Europe/London"))
    first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    second = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    spool = str(tmp_path)

    assert schedule.is_due(first, read_status(spool).last_success_period) is True

    write_status(
        spool,
        BackupStatus(last_success_period=schedule.period_key(first), last_success_at=utc_iso(first)),
    )

    assert schedule.is_due(second, read_status(spool).last_success_period) is False


def test_the_next_day_is_due_again_after_the_repeated_hour():
    """The period key is what suppresses the second pass, so it must stop
    suppressing at the day boundary."""
    schedule = Schedule.from_settings(_settings(backup_time="01:30", backup_timezone="Europe/London"))
    next_day = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)

    assert schedule.is_due(next_day, "2026-10-25") is True


# ============================================================
# PERIOD IDENTITY IS A LOCAL CALENDAR FACT
# ============================================================

def test_a_period_key_is_computed_in_the_configured_timezone_not_in_utc():
    """
    15:30Z on 2026-09-06 is 03:30 on the 7th in Auckland (UTC+12 that
    month). Keyed in UTC the run is attributed to the 6th, which is the day
    it did not happen on -- and the next day's run then looks already done.
    """
    moment = datetime(2026, 9, 6, 15, 30, tzinfo=timezone.utc)

    auckland = Schedule.from_settings(_settings(backup_timezone="Pacific/Auckland"))
    utc = Schedule.from_settings(_settings(backup_timezone="UTC"))

    assert moment.astimezone(AUCKLAND).date().isoformat() == "2026-09-07"
    assert auckland.period_key(moment) == "2026-09-07"
    assert utc.period_key(moment) == "2026-09-06"


def test_a_weekly_key_uses_the_iso_week_so_it_does_not_repeat_across_a_new_year():
    """
    2026-12-31 and 2027-01-01 are both in ISO week 2026-W53. Pairing an ISO
    week number with a calendar year would key them 2026-W53 and 2027-W53,
    which is a new period in the middle of one week -- a second backup on
    the 1st, every year.
    """
    schedule = Schedule.from_settings(_settings(backup_period="weekly"))
    old_year = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc)
    new_year = datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc)

    assert schedule.period_key(old_year) == "2026-W53"
    assert schedule.period_key(new_year) == "2026-W53"


def test_a_monthly_key_is_the_local_month():
    schedule = Schedule.from_settings(_settings(backup_period="monthly"))

    assert schedule.period_key(datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)) == "2026-09"


def test_arithmetic_on_a_naive_datetime_raises():
    schedule = Schedule.from_settings(_settings())

    with pytest.raises(ScheduleError):
        schedule.period_key(datetime(2026, 9, 6, 12, 0))


# ============================================================
# THE DAY-31 CLAMP
# ============================================================

def test_a_monthly_schedule_on_the_31st_clamps_to_the_last_day_of_february():
    """
    2027 is not a leap year, so February has 28 days. The alternative --
    skipping months without a 31st -- is seven backups a year from a
    setting that reads like twelve.
    """
    schedule = Schedule.from_settings(_settings(backup_period="monthly", backup_day_of_month=31))

    slot = schedule.slot_for(datetime(2027, 2, 10, 12, 0, tzinfo=timezone.utc))

    assert slot.date().isoformat() == "2027-02-28"
    assert slot.strftime("%H:%M") == "03:00"


def test_the_clamp_takes_the_leap_day_when_there_is_one():
    schedule = Schedule.from_settings(_settings(backup_period="monthly", backup_day_of_month=31))

    slot = schedule.slot_for(datetime(2028, 2, 10, 12, 0, tzinfo=timezone.utc))

    assert slot.date().isoformat() == "2028-02-29"


def test_a_month_that_has_the_configured_day_is_not_clamped():
    schedule = Schedule.from_settings(_settings(backup_period="monthly", backup_day_of_month=31))

    slot = schedule.slot_for(datetime(2027, 1, 10, 12, 0, tzinfo=timezone.utc))

    assert slot.date().isoformat() == "2027-01-31"


def test_the_next_monthly_slot_after_a_clamped_one_is_the_following_month():
    """Derived from the current month's length rather than by adding 30
    days, which lands in the wrong month twice a year."""
    schedule = Schedule.from_settings(_settings(backup_period="monthly", backup_day_of_month=31))

    following = schedule.next_slot_after(datetime(2027, 2, 28, 12, 0, tzinfo=timezone.utc))

    assert following.date().isoformat() == "2027-03-31"


# ============================================================
# SLOTS AND THE CATCH-UP PREDICATE
# ============================================================

def test_the_next_slot_is_strictly_after_the_moment_given():
    """A slot clamped to now rather than skipped would let a caller that
    sleeps until it wake into a busy loop."""
    schedule = Schedule.from_settings(_settings())
    at_the_slot = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)

    assert schedule.next_slot_after(at_the_slot) == datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)


def test_a_slot_still_ahead_in_this_period_is_the_next_one():
    schedule = Schedule.from_settings(_settings())

    assert schedule.next_slot_after(datetime(2026, 9, 6, 1, 0, tzinfo=timezone.utc)) == datetime(
        2026, 9, 6, 3, 0, tzinfo=timezone.utc
    )


def test_a_weekly_slot_lands_on_the_configured_weekday():
    schedule = Schedule.from_settings(_settings(backup_period="weekly", backup_day_of_week="wednesday"))

    slot = schedule.slot_for(datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc))

    assert slot.date().isoformat() == "2026-09-02"
    assert slot.weekday() == 2


def test_first_boot_after_the_slot_runs_now_rather_than_waiting():
    """A container started at 4pm with an 03:00 schedule dumps immediately
    rather than waiting until tomorrow. No recorded success at all is what
    makes it true, which is the same predicate as catching up after
    downtime."""
    schedule = Schedule.from_settings(_settings())

    assert schedule.is_due(datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc), "") is True


def test_a_period_whose_slot_has_not_arrived_is_not_due():
    schedule = Schedule.from_settings(_settings())

    assert schedule.is_due(datetime(2026, 9, 6, 1, 0, tzinfo=timezone.utc), "") is False


def test_a_recorded_success_for_this_period_is_not_due_whatever_the_clock_says():
    schedule = Schedule.from_settings(_settings())

    assert schedule.is_due(datetime(2026, 9, 6, 23, 0, tzinfo=timezone.utc), "2026-09-06") is False


def test_a_period_missed_while_the_container_was_down_is_caught_up():
    """One predicate covers first boot, a missed period, the repeated hour
    and a redeploy -- four behaviours that would otherwise be four pieces of
    code able to disagree."""
    schedule = Schedule.from_settings(_settings())

    assert schedule.is_due(datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc), "2026-09-06") is True


# ============================================================
# THE STATUS FILE
# ============================================================

def test_a_missing_status_file_reads_as_no_recorded_success(tmp_path):
    """The safe direction: no recorded success means the predicate fires and
    a backup is taken. Costs one extra dump; the opposite default costs a
    backup that silently never happens."""
    assert read_status(str(tmp_path)) == BackupStatus()


def test_a_status_file_round_trips(tmp_path):
    status = BackupStatus(
        last_success_period="2026-09-06",
        last_success_at="2026-09-06T03:00:00Z",
        last_artifact="libex-20260906T030000Z.dump",
        last_size_bytes=2376454048,
        last_attempt_period="2026-09-06",
        last_attempt_at="2026-09-06T03:00:00Z",
        last_result="success",
        destinations_ok=["ftps"],
        destinations_failed=[],
    )

    write_status(str(tmp_path), status)

    assert read_status(str(tmp_path)) == status


def test_a_corrupt_status_file_reads_as_no_recorded_success(tmp_path):
    (tmp_path / STATUS_FILENAME).write_text("{not json at all")

    assert read_status(str(tmp_path)) == BackupStatus()


def test_a_status_file_that_is_not_an_object_reads_as_no_recorded_success(tmp_path):
    (tmp_path / STATUS_FILENAME).write_text("[1, 2, 3]")

    assert read_status(str(tmp_path)) == BackupStatus()


def test_unknown_keys_are_dropped_rather_than_carried(tmp_path):
    """A file written by a future version, or hand-edited, cannot introduce
    a field this one did not expect."""
    (tmp_path / STATUS_FILENAME).write_text(
        json.dumps({"last_success_period": "2026-09-06", "database_url": "postgresql://user:pw@host/db"})
    )

    status = read_status(str(tmp_path))

    assert status.last_success_period == "2026-09-06"
    assert not hasattr(status, "database_url")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"last_size_bytes": "2376454048"}, BackupStatus(last_size_bytes=0)),
        ({"last_size_bytes": True}, BackupStatus(last_size_bytes=0)),
        ({"last_success_period": 20260906}, BackupStatus(last_success_period="")),
        ({"destinations_ok": "ftps"}, BackupStatus(destinations_ok=[])),
        ({"destinations_ok": [1, 2]}, BackupStatus(destinations_ok=["1", "2"])),
    ],
)
def test_a_field_of_the_wrong_type_falls_back_to_its_default(tmp_path, raw, expected):
    (tmp_path / STATUS_FILENAME).write_text(json.dumps(raw))

    assert read_status(str(tmp_path)) == expected


def test_the_status_file_is_created_private(tmp_path):
    """0600 at creation, never open-then-chmod: the window between the two
    is what leaves it readable to everyone."""
    write_status(str(tmp_path), BackupStatus(last_success_period="2026-09-06"))

    mode = os.stat(status_path(str(tmp_path))).st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_the_status_file_is_replaced_atomically_and_leaves_no_temp_behind(tmp_path):
    write_status(str(tmp_path), BackupStatus(last_success_period="2026-09-05"))
    write_status(str(tmp_path), BackupStatus(last_success_period="2026-09-06"))

    assert read_status(str(tmp_path)).last_success_period == "2026-09-06"
    assert [entry.name for entry in tmp_path.iterdir()] == [STATUS_FILENAME]


def test_a_leftover_temp_file_from_a_dead_process_is_replaced(tmp_path):
    """Our own temp name in our own private directory, so a leftover can only
    be ours -- from a process that died mid-write and whose pid was reused.
    O_EXCL would refuse it, so it is removed first."""
    temp = tmp_path / f"{STATUS_FILENAME}.{os.getpid()}.tmp"
    temp.write_text("half a file")

    write_status(str(tmp_path), BackupStatus(last_success_period="2026-09-06"))

    assert read_status(str(tmp_path)).last_success_period == "2026-09-06"
    assert not temp.exists()


def test_a_failure_to_write_status_never_raises(tmp_path):
    """A backup that has already been taken and uploaded must not be undone
    by a failure to record it. The next cycle takes a redundant dump, which
    is the cheap end of this."""
    with patch("app.services.backup.schedule.os.open", side_effect=OSError("no space")):
        write_status(str(tmp_path), BackupStatus(last_success_period="2026-09-06"))

    assert read_status(str(tmp_path)) == BackupStatus()


def test_status_path_is_inside_the_spool():
    assert status_path("/backup-spool") == f"/backup-spool/{STATUS_FILENAME}"


def test_utc_iso_renders_one_way_for_every_field():
    assert utc_iso(datetime(2026, 9, 6, 3, 0, tzinfo=LONDON)) == "2026-09-06T02:00:00Z"
    assert utc_iso(datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)) == "2026-09-06T03:00:00Z"


# ============================================================
# DAYLIGHT SAVING: A GAP THAT SWALLOWS THE REST OF THE DAY
# ============================================================
#
# The failure is not a late backup, it is no backup. slot_for's contract is
# "the instant this schedule fires within the period containing `moment`",
# and period identity is the local calendar date -- so an instant on the
# following day is not in this period at all. is_due asks
# `now >= slot_for(now)`, slot_for(D) answers with an instant on D+1, and no
# `now` inside day D can satisfy it. The period is then skipped with nothing
# logged, because nothing failed: the catch-up predicate only ever asks
# about the current period, so it cannot notice one that never came due.
#
# Two live zones do it, both jumping at 23:00, and the four schedules below
# are every combination of those zones with the two half-hourly times the
# jump swallows. 2026-03-28 is the transition date for both in that year;
# the same pair recurs every March through 2040.

NUUK = "America/Nuuk"
SCORESBYSUND = "America/Scoresbysund"


@pytest.mark.parametrize("zone", [NUUK, SCORESBYSUND])
@pytest.mark.parametrize("configured", ["23:00", "23:30"])
def test_a_gap_running_to_midnight_resolves_backwards_and_stays_on_its_own_date(zone, configured):
    """
    Resolving forward out of this gap lands on 2026-03-29, which belongs to
    the next period. So the scan reverses and takes the last real instant on
    the date instead: 22:59 local, a minute early, once, in one zone, in one
    year -- against a period silently missed.
    """
    schedule = Schedule.from_settings(_settings(backup_time=configured, backup_timezone=zone))

    slot = schedule._instant_for(date(2026, 3, 28))

    assert slot.date() == date(2026, 3, 28)
    assert slot.strftime("%H:%M") == "22:59"
    assert schedule.period_key(slot) == "2026-03-28"


@pytest.mark.parametrize("zone", [NUUK, SCORESBYSUND])
@pytest.mark.parametrize("configured", ["23:00", "23:30"])
def test_the_period_a_midnight_gap_would_have_skipped_still_comes_due(zone, configured):
    """
    The consequence, stated as the predicate rather than as the instant. A
    slot resolved backwards sits in the final minute of the local day by
    construction -- there is nothing later that still belongs to the period
    -- so is_due is true for that minute and no longer.
    """
    schedule = Schedule.from_settings(_settings(backup_time=configured, backup_timezone=zone))
    slot = schedule._instant_for(date(2026, 3, 28))

    assert schedule.is_due(slot, "") is True
    assert schedule.is_due(slot - timedelta(minutes=1), "") is False


def test_the_backward_scan_is_not_reached_by_a_gap_the_day_can_absorb(caplog):
    """
    Europe/London's 01:30 gap has the rest of the day behind it, so the
    forward scan finds 02:00 and the reversal never runs. The two scans are
    told apart by the direction they log, which is the only place the choice
    is visible.
    """
    schedule = Schedule.from_settings(_settings(backup_time="01:30", backup_timezone="Europe/London"))

    with caplog.at_level("INFO"):
        schedule._instant_for(date(2026, 3, 29))

    resolved = [
        r for r in caplog.records
        if r.getMessage() == "Backup: configured time does not exist on this date, using the nearest real instant"
    ]
    assert resolved and resolved[0].__dict__["direction"] == "forward"


def test_the_midnight_gap_logs_the_reversal_as_its_own_direction(caplog):
    """One line, one place it is written, so the two scans cannot describe
    the same event differently -- and an operator seeing a backup a minute
    early has something to search for."""
    schedule = Schedule.from_settings(_settings(backup_time="23:00", backup_timezone=NUUK))

    with caplog.at_level("INFO"):
        schedule._instant_for(date(2026, 3, 28))

    resolved = [
        r for r in caplog.records
        if r.getMessage() == "Backup: configured time does not exist on this date, using the nearest real instant"
    ]
    assert resolved
    assert resolved[0].__dict__["direction"] == "backward"
    assert resolved[0].__dict__["configured"] == "23:00"
    assert resolved[0].__dict__["resolved"] == "22:59"
    assert resolved[0].__dict__["timezone"] == NUUK


def test_a_gap_longer_than_six_hours_is_still_crossed():
    """
    Antarctica/Vostok on 1994-11-01 has no local time between 00:00 and
    07:00 -- 420 minutes, past the six-hour ceiling the scan limit used to
    carry on the stated grounds that no IANA transition came close. It does,
    and a 00:00 schedule under that ceiling would have given up and returned
    the nonexistent reading rather than 07:00.
    """
    schedule = Schedule.from_settings(_settings(backup_time="00:00", backup_timezone="Antarctica/Vostok"))

    slot = schedule._instant_for(date(1994, 11, 1))

    assert slot.strftime("%Y-%m-%d %H:%M") == "1994-11-01 07:00"
    assert slot.date() == date(1994, 11, 1)


def test_a_local_date_that_never_happened_at_all_approximates_rather_than_refuses():
    """
    Pacific/Apia skipped 2011-12-30 whole when it crossed the date line, so
    neither scan can find a real instant on it and there is no answer that
    is the right one. Returning the fold reading keeps a backup happening at
    approximately the right time; refusing to schedule would be worse, and
    raising here would take the container down on a date nobody configured.
    """
    schedule = Schedule.from_settings(_settings(backup_time="03:00", backup_timezone="Pacific/Apia"))

    slot = schedule._instant_for(date(2011, 12, 30))

    assert slot.strftime("%Y-%m-%d %H:%M") == "2011-12-30 03:00"
