"""
Scheduled Postgres backups: dump, verify, upload, prune.

WHAT THIS PACKAGE EXPORTS, AND NOTHING ELSE. Everything below this module is
internal to it.

    run_backup(once=False) -> int    the whole thing; returns an exit code
    BackupRunner                     the class run_backup constructs

scripts/backup.py calls the first and nothing else. The class is exported
alongside it because it is what the runner's own tests construct, and an
__all__ that named one entry point while exporting two was the kind of
mismatch that gets "tidied" by deleting the wrong half.

app/main.py and every route import this package ZERO times, and that is
stricter than the seeder's entry-points-only rule rather than the same as
it. The API never runs a backup, never schedules one, and never needs to
know one exists: this runs in its own container, off its own entry point,
against a database it opens with pg_dump's own libpq connection and never
with the ORM. Nothing in here imports app.db.session, because doing so would
build a SQLAlchemy pool of ten connections plus ten overflow that this
process would never use, against a max_connections budget already spoken
for.

The pieces, and why they are separate files:

    artifact.py     naming and parsing. Ordering comes from the filename,
                    never from a remote modification time.
    retention.py    the three-tier keep set, pure. The most recent N, plus
                    the newest artefact at least M days old, plus the oldest
                    one not yet M days old -- which is what gives the second
                    tier anything to select.
    schedule.py     slot arithmetic in the configured timezone, the
                    catch-up predicate, and the status file.
    dump.py         precheck, pg_dump, the spool file, verification.
    destinations/   the transport seam: list, upload, delete. No policy.
    runner.py       the order of the steps and the failure policy.

retention.py and schedule.py are separate from runner.py deliberately. Both
are pure logic and both are where the expensive mistakes live -- deleting
the wrong artefact, skipping a period, running twice across a
daylight-saving boundary. Folded into the runner, every one of those cases
would need a subprocess, a filesystem and a fake clock to exercise. Kept
apart, they need a datetime and a list.

Logging is the entry point's job, not this package's: setup_logging() is
called there. Nothing here may call logging.basicConfig, which attaches a
root handler and in doing so un-mutes httpx and httpcore at INFO across the
whole process.
"""

# Services
from app.services.backup.runner import BackupRunner, run_backup


__all__ = ["BackupRunner", "run_backup"]
