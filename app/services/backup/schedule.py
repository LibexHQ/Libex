"""
Slot arithmetic, the catch-up predicate, and the status file.

Separate from the runner deliberately. Fused into the cycle, every rule
below -- the day-31 clamp, both daylight-saving cases, catching up after
downtime -- would need a subprocess, a filesystem and a fake clock to
exercise. Here they need a datetime.

ALL PERIOD ARITHMETIC HAPPENS IN THE CONFIGURED TIMEZONE, never in UTC. A
run configured for 03:00 in Europe/London is 02:00 or 03:00 UTC depending on
the month, and a UTC "which day is it" question answers 2026-09-06 for a run
an operator would unhesitatingly call the 7th's. Period identity is a local
calendar fact, so it is computed on local calendar values and nowhere else.
Artefact names stay UTC (see artifact.py) -- that is an ordering fact, and
the two must not be confused.

ONE PREDICATE COVERS FOUR CASES:

    the current period has no recorded success, and its slot has passed
    -> run now

That is first boot, a period missed while the container was down, the second
pass through a repeated daylight-saving hour, and a redeploy after a
successful run earlier the same period. No separate first-boot flag, no
"missed run" detector, no DST special case. Four behaviours that would
otherwise be four pieces of code that can disagree with each other.

DAYLIGHT SAVING, both directions:

  - A configured time that does not exist on a given day (the spring gap)
    runs at the next instant that does exist ON THE SAME LOCAL DATE, and at
    the last instant that does exist on it when the gap swallows the rest of
    the day. Staying inside the date is not a detail: a slot resolved onto
    the following day makes `now >= slot_for(now)` unsatisfiable anywhere
    within the day it belongs to, so the period is skipped in silence -- no
    is_due, no catch-up line, last_success_period simply stepping over a
    date. Which is the annual missing backup that nothing reports, arriving
    through the code written to prevent it. Two live zones reach it:
    America/Nuuk and America/Scoresbysund, whose clocks jump at 23:00, with
    BACKUP_TIME at 23:00 or 23:30.
  - A configured time that happens twice (the autumn repeat) runs once, and
    that is true by construction rather than by a rule: success is recorded
    against the period, and the second pass through 01:30 is the same local
    day, so the predicate is already false.

DAY 31 IN FEBRUARY clamps to the last day of the month, so a monthly backup
configured for the 31st runs on the 28th, the 29th, or the 30th as the month
allows. The alternative -- skipping months without a 31st -- is seven
backups a year from a setting that reads like twelve.
"""

# Standard library
import calendar
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Core
from app.core.logging import get_logger


logger = get_logger()

PERIOD_DAILY = "daily"
PERIOD_WEEKLY = "weekly"
PERIOD_MONTHLY = "monthly"
_PERIODS = (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY)

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# How far past a nonexistent local time to look for a real one.
#
# The real bound is the local date, checked in _instant_for: a slot has to
# stay inside the period it belongs to, so the scan stops at midnight
# whatever this number says. A full day is therefore a limit that the date
# check always reaches first -- it is here to make the loop finite, not to
# express a belief about how long a gap can be.
#
# It used to be six hours, on the stated grounds that no IANA transition
# comes close. That is false and was measured false with this module's own
# _exists round trip: Pacific/Apia on 2011-12-30 needs 1,260 minutes and
# Antarctica/Vostok on 1994-11-01 needs 420. A sweep of all 498 zones bounds
# it -- 182 historical breaches, none dated later than 2040 -- so the
# fallback below is unreachable in practice, which is a different claim from
# the one that was written here, and a six-hour ceiling would have silently
# resolved both of those transitions backwards instead of forwards.
#
# The scan exists at all because there is no stdlib call for "the first real
# instant at or after this local time" -- zoneinfo will happily interpret a
# nonexistent time using the pre-transition offset and hand back a real
# instant somewhere past the gap, which is close enough to be tempting and
# is not the same answer.
_GAP_SCAN_LIMIT_MINUTES = 24 * 60

STATUS_FILENAME = "status.json"


class ScheduleError(Exception):
    """
    Raised when the configured schedule cannot be understood.

    Not a LibexException subclass, like every exception in this package: a
    LibexException carries a .message that the API's handler copies into an
    HTTP response body, and nothing here may ever be able to reach that
    path. The message is a fixed string, and any offending value travels in
    the caller's structured log fields rather than inside the exception.

    Settings itself does not validate these, on purpose: a value pydantic
    rejects raises while Settings is being constructed, before any logging
    exists to report it, and in a container with restart: unless-stopped
    that is a silent restart loop instead of a sentence. Validating here
    means an unusable schedule leaves the container up and complaining.
    """


@dataclass(frozen=True)
class Schedule:
    """
    A validated schedule. Constructed once at startup; every method is a
    pure function of its arguments and this.
    """

    period: str
    hour: int
    minute: int
    timezone_name: str
    tz: ZoneInfo
    weekday: int          # 0 = Monday. Weekly only.
    day_of_month: int     # Monthly only. Clamped per month at use.

    # --- construction ---------------------------------------------------

    @staticmethod
    def from_settings(settings) -> "Schedule":
        """
        Builds a Schedule from the BACKUP_* settings, raising ScheduleError
        on the first thing it cannot use.

        Every value arrives as a string or an int because config.py holds
        them as plain scalars -- see the note there. This is where they
        become a timezone, an hour and a weekday, and where an operator
        finds out they typed Europe/Londun.
        """
        period = str(settings.backup_period).strip().lower()
        if period not in _PERIODS:
            raise ScheduleError("backup period must be daily, weekly or monthly")

        try:
            tz = ZoneInfo(str(settings.backup_timezone).strip())
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ScheduleError("backup timezone is not a known IANA zone name") from exc

        hour, minute = _parse_hhmm(str(settings.backup_time))

        weekday_name = str(settings.backup_day_of_week).strip().lower()
        if period == PERIOD_WEEKLY and weekday_name not in _WEEKDAYS:
            raise ScheduleError("backup day of week must be a weekday name, e.g. sunday")
        weekday = _WEEKDAYS.get(weekday_name, 6)

        day_of_month = int(settings.backup_day_of_month)
        if period == PERIOD_MONTHLY and not 1 <= day_of_month <= 31:
            raise ScheduleError("backup day of month must be between 1 and 31")

        return Schedule(
            period=period,
            hour=hour,
            minute=minute,
            timezone_name=str(settings.backup_timezone).strip(),
            tz=tz,
            weekday=weekday,
            day_of_month=day_of_month,
        )

    # --- description ----------------------------------------------------

    def describe(self) -> dict:
        """
        Allowlisted fields for the startup log line. Everything here is
        configuration this process was given, never anything derived from a
        response or an exception.
        """
        fields = {
            "period": self.period,
            "at": f"{self.hour:02d}:{self.minute:02d}",
            "timezone": self.timezone_name,
        }
        if self.period == PERIOD_WEEKLY:
            fields["day_of_week"] = _weekday_name(self.weekday)
        if self.period == PERIOD_MONTHLY:
            fields["day_of_month"] = self.day_of_month
        return fields

    @property
    def clamps(self) -> bool:
        """
        Whether this schedule will ever land on a day its month does not
        have. True for a monthly backup on the 29th, 30th or 31st -- worth
        one INFO line at startup, because "the 31st" and "the last day of
        February" are the same setting and only one of them is what the
        operator typed.
        """
        return self.period == PERIOD_MONTHLY and self.day_of_month > 28

    # --- period identity -------------------------------------------------

    def period_key(self, moment: datetime) -> str:
        """
        The identity of the period containing `moment` -- the string success
        is recorded against, and the thing the catch-up predicate compares.

        Computed on the local calendar date, which is the point: near a UTC
        day boundary the same instant belongs to different days depending on
        which clock is asked, and the operator means theirs.

        Weekly uses the ISO week (%G-W%V, Monday-based) rather than
        year-plus-week-number, because the ISO year and the calendar year
        disagree in the last days of December and the first days of January,
        and pairing an ISO week with a calendar year produces a key that
        repeats across a new-year boundary.
        """
        local = self._local(moment)
        if self.period == PERIOD_DAILY:
            return local.strftime("%Y-%m-%d")
        if self.period == PERIOD_WEEKLY:
            iso = local.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        return local.strftime("%Y-%m")

    # --- slots ------------------------------------------------------------

    def slot_for(self, moment: datetime) -> datetime:
        """
        The instant this schedule fires within the period containing
        `moment`. Aware, in the configured timezone.
        """
        return self._instant_for(self._anchor_date(self._local(moment).date()))

    def next_slot_after(self, moment: datetime) -> datetime:
        """
        The next firing instant strictly after `moment` -- what the runner
        sleeps toward.

        Two candidates, because a slot in the current period may be either
        ahead of or behind `moment`: this period's, and the next period's.
        Anything already past is skipped rather than clamped to now, so a
        caller that sleeps until the returned instant never wakes into a
        busy loop.
        """
        current = self.slot_for(moment)
        if current > moment:
            return current
        return self._instant_for(self._anchor_date(self._next_period_date(self._local(moment).date())))

    def is_due(self, now: datetime, last_success_period: str) -> bool:
        """
        The whole schedule, in two lines.

        A recorded success for the current period means nothing to do,
        whatever the clock says. Otherwise the slot for this period having
        passed is the go-ahead -- which on first boot, with no recorded
        success at all, is true the moment the slot is behind us: a
        container started at 4pm with a 03:00 schedule dumps immediately
        rather than waiting until tomorrow. Deliberate, and the runner logs
        it distinctly, naming the period, so it reads as a decision rather
        than a mystery run.
        """
        if last_success_period and self.period_key(now) == last_success_period:
            return False
        return now >= self.slot_for(now)

    # --- internals --------------------------------------------------------

    def _local(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ScheduleError("schedule arithmetic requires a timezone-aware datetime")
        return moment.astimezone(self.tz)

    def _anchor_date(self, local_date: date) -> date:
        """
        The local date within `local_date`'s period on which this schedule
        fires: the day itself for daily, the configured weekday of that ISO
        week for weekly, the clamped day of that month for monthly.
        """
        if self.period == PERIOD_DAILY:
            return local_date
        if self.period == PERIOD_WEEKLY:
            monday = local_date - timedelta(days=local_date.weekday())
            return monday + timedelta(days=self.weekday)
        last_day = calendar.monthrange(local_date.year, local_date.month)[1]
        return local_date.replace(day=min(self.day_of_month, last_day))

    def _next_period_date(self, local_date: date) -> date:
        """A date inside the period after the one containing `local_date`."""
        if self.period == PERIOD_DAILY:
            return local_date + timedelta(days=1)
        if self.period == PERIOD_WEEKLY:
            monday = local_date - timedelta(days=local_date.weekday())
            return monday + timedelta(days=7)
        # The 1st of the following month: derived from the current month's
        # length rather than by adding 30 days, which lands in the wrong
        # month twice a year.
        last_day = calendar.monthrange(local_date.year, local_date.month)[1]
        return local_date.replace(day=last_day) + timedelta(days=1)

    def _instant_for(self, local_date: date) -> datetime:
        """
        The aware instant for this schedule's time on `local_date`.

        fold defaults to 0, which resolves a repeated hour to its first
        occurrence -- and the predicate's period-keyed success record is
        what stops the second occurrence running again.

        A nonexistent local time is detected by round-tripping through UTC:
        if converting to UTC and back does not reproduce the wall clock we
        asked for, the wall clock reading never happened that day. The scan
        then walks forward a minute at a time to the first reading that does
        exist, which is the instant the gap ends.

        THE ANSWER MUST STAY ON `local_date`, and that is the whole of the
        second scan. The caller's contract is "the instant this schedule
        fires within the period containing `moment`", and period identity is
        the local calendar date; an instant on the following day is not in
        this period at all. Walking forward out of a gap that runs to
        midnight returns one, and the consequence is not a late backup, it
        is no backup: is_due asks `now >= slot_for(now)`, slot_for(D) answers
        with an instant on D+1, and no `now` inside day D can satisfy it.
        The period is then skipped with nothing logged, because nothing
        failed -- the catch-up predicate only ever asks about the current
        period, so it cannot notice a period that never came due. Two live
        zones do this, America/Nuuk and America/Scoresbysund, whose clocks
        jump at 23:00.

        So when the gap swallows the rest of the day the scan reverses and
        takes the LAST real instant on the date instead. A backup a minute
        or two early, once, in one zone, in one year, against a period
        silently missed.

        BE HONEST ABOUT THE WINDOW THAT LEAVES. A slot resolved backwards
        sits in the final minute of the local day by construction -- there
        is nothing later that still belongs to the period -- so is_due is
        true for that minute and no longer. The runner sleeps toward the
        exact slot instant and wakes on it, so it takes that minute; what it
        does not have on that one day is the usual all-day catch-up window
        behind a missed slot. A cycle that overran across the minute would
        skip the period, which needs a run of more than a day inside a
        one-hour dump timeout.
        """
        naive = datetime.combine(local_date, time(self.hour, self.minute))
        candidate = naive.replace(tzinfo=self.tz)
        if _exists(candidate):
            return candidate

        for offset in range(1, _GAP_SCAN_LIMIT_MINUTES + 1):
            shifted = (naive + timedelta(minutes=offset)).replace(tzinfo=self.tz)
            if shifted.date() != local_date:
                break
            if _exists(shifted):
                return self._resolved(shifted, "forward")

        for offset in range(1, _GAP_SCAN_LIMIT_MINUTES + 1):
            shifted = (naive - timedelta(minutes=offset)).replace(tzinfo=self.tz)
            if shifted.date() != local_date:
                break
            if _exists(shifted):
                return self._resolved(shifted, "backward")

        # Both scans exhausted, which means no minute of this local date
        # exists at all -- Pacific/Apia's 2011-12-30, the day a zone skipped
        # whole. Returning the fold=0 reading rather than raising keeps a
        # backup happening at approximately the right time; there is no
        # instant that is the right answer, and refusing to schedule is
        # worse than approximating one.
        return candidate

    def _resolved(self, shifted: datetime, direction: str) -> datetime:
        """One line for a resolved gap, and one place it is written, so the
        two scans cannot describe the same event differently."""
        logger.info(
            "Backup: configured time does not exist on this date, using the nearest real instant",
            extra={
                "configured": f"{self.hour:02d}:{self.minute:02d}",
                "resolved": shifted.strftime("%H:%M"),
                "direction": direction,
                "timezone": self.timezone_name,
            },
        )
        return shifted


def _exists(moment: datetime) -> bool:
    """
    Whether this wall-clock reading actually occurred in its zone.

    zoneinfo interprets a nonexistent local time using the offset in force
    before the transition and returns a perfectly usable aware datetime, so
    there is nothing to catch -- the round trip is what exposes it. Converting
    to UTC and back reproduces the original reading for every real local
    time and cannot for one inside a gap, because no instant carries that
    reading.
    """
    return moment.astimezone(timezone.utc).astimezone(moment.tzinfo).replace(tzinfo=None) == moment.replace(tzinfo=None)


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ScheduleError("backup time must be HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ScheduleError("backup time must be HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError("backup time must be a real time of day, HH:MM")
    return hour, minute


def _weekday_name(weekday: int) -> str:
    for name, index in _WEEKDAYS.items():
        if index == weekday:
            return name
    return "sunday"


# ============================================================
# STATUS FILE
# ============================================================

@dataclass(frozen=True)
class BackupStatus:
    """
    What the last cycle did, as the next process needs to know it.

    This is the schedule's whole memory. There is deliberately no fallback
    that asks the destinations what they hold if the file is missing -- a
    listing answers "what is on the server", not "did this schedule already
    fire this period", and treating one as the other is how a backup gets
    skipped after a manual upload or run twice after a failed one.

    EVERY FIELD IS AN ALLOWLIST ENTRY. No URL, no hostname, no token, no
    DATABASE_URL, and above all no str(exception): the failure of a dump
    reaches this file as a phase name and an exception class name, both of
    which are strings this codebase wrote. The file sits on the spool volume
    at 0600, but it is treated as though someone will read it, because
    someone diagnosing a failed backup will.

    Datetimes are ISO-8601 UTC strings rather than datetimes so the file is
    plain JSON that a human can read over the operator's shoulder.
    """

    last_success_period: str = ""
    last_success_at: str = ""
    last_artifact: str = ""
    last_size_bytes: int = 0
    last_attempt_period: str = ""
    last_attempt_at: str = ""
    last_result: str = ""
    last_error_phase: str = ""
    last_error_type: str = ""
    destinations_ok: list[str] = field(default_factory=list)
    destinations_failed: list[str] = field(default_factory=list)


def status_path(spool_dir: str) -> str:
    return os.path.join(spool_dir, STATUS_FILENAME)


def read_status(spool_dir: str) -> BackupStatus:
    """
    Reads the status file, or returns an empty status if there is not a
    usable one.

    Missing, unreadable and corrupt all collapse to the same answer on
    purpose, and the direction that answer fails in is the safe one: an
    empty status means no recorded success for the current period, which
    means the predicate fires and a backup is taken. The cost of being
    wrong is one extra dump. The cost of the opposite default -- assuming
    success on an unreadable file -- is a backup silently not happening.

    Unknown keys are dropped rather than passed through, so a file written
    by a future version cannot introduce a field this one did not expect,
    and a hand-edited file cannot smuggle one in.
    """
    path = status_path(spool_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return BackupStatus()
    except (OSError, ValueError) as exc:
        logger.warning(
            "Backup: status file unreadable, treating this period as not yet run",
            extra={"error_type": type(exc).__name__},
        )
        return BackupStatus()

    if not isinstance(raw, dict):
        logger.warning(
            "Backup: status file is not an object, treating this period as not yet run",
            extra={"error_type": type(raw).__name__},
        )
        return BackupStatus()

    defaults = BackupStatus()
    values: dict = {}
    for name, default in asdict(defaults).items():
        candidate = raw.get(name, default)
        if isinstance(default, list):
            values[name] = [str(item) for item in candidate] if isinstance(candidate, list) else []
        elif isinstance(default, int) and not isinstance(default, bool):
            values[name] = candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else 0
        else:
            values[name] = candidate if isinstance(candidate, str) else ""
    return BackupStatus(**values)


def write_status(spool_dir: str, status: BackupStatus) -> None:
    """
    Writes the status file atomically: a fresh temp file in the same
    directory, then os.replace onto the final name.

    Same directory because os.replace is only atomic within a filesystem,
    and a temp directory elsewhere is a different one often enough to
    matter. Atomic because the alternative -- truncating the real file and
    writing into it -- has a window in which a container killed mid-write
    leaves a truncated JSON file, which read_status then treats as no
    recorded success, which takes an unnecessary dump. Not a disaster, and
    trivially avoidable.

    O_EXCL with the mode at creation rather than a chmod afterwards, for the
    same reason as the spool file in dump.py: open-then-chmod leaves the
    file readable to everyone for the length of the write.

    Never raises. A failure to record status is worth an ERROR -- the next
    cycle will take a redundant dump -- but it must not undo a backup that
    has already been taken and uploaded.
    """
    path = status_path(spool_dir)
    temp_path = f"{path}.{os.getpid()}.tmp"
    try:
        # Our own temp name in our own private directory; a leftover can
        # only be ours, from a process that died mid-write and whose pid
        # has been reused. O_EXCL below would refuse it, so it goes first.
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(status), handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        logger.error(
            "Backup: could not write the status file",
            extra={"error_type": type(exc).__name__},
        )
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def utc_iso(moment: datetime) -> str:
    """One rendering of an instant for the status file, so two fields cannot drift."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
