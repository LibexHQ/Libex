"""
Scheduled backup entry point.

Runs the backup as one process in its own container: dump, verify, upload,
prune, on a schedule, forever. This file is the whole of the entry point --
every decision about when a backup happens, what it costs, what is kept and
what is deleted lives in app/services/backup/, and the only call made into
that package is run_backup(). app/main.py and the routes import it zero
times; the API never runs a backup and never needs to know one exists.

RUN IT. docker-compose.yml's libex-backup service starts this and keeps it
up, with `entrypoint: []` and `command: ["python", "-m", "scripts.backup"]`
-- both together, never one without the other. docker-entrypoint.sh runs
`alembic upgrade head` before whatever it is handed, which is right for the
API and wrong here: this container reads the database with pg_dump's own
libpq connection, needs no schema of its own, and has no business migrating
anyone else's while the API container is racing it to the same lock.

For a supervised run against a live stack:

    docker compose run --rm --entrypoint "" libex-backup \\
      python -m scripts.backup --once

--once MEANS DUMP NOW. It bypasses both the schedule and the catch-up
predicate and takes a backup immediately, then exits with what happened in
the exit code. It deliberately does NOT mean "run one tick of the
scheduler": a tick is correctly a no-op at every minute of the day that is
not the configured slot, so that reading of the flag would leave an
operator watching a supervised run produce no backup, exit 0, and give them
no way to tell that from a broken one. The scheduled form is the
no-argument one the compose file runs.

    0   the backup was taken, verified and uploaded to every destination.
    1   no destination resolved, or the cycle failed. The runner's own log
        lines say which.
    2   the configured schedule cannot be used (BACKUP_PERIOD, BACKUP_TIME,
        BACKUP_TIMEZONE and the two day fields are read together).
    3   the spool is already claimed by another backup process -- almost
        always the scheduled container, mid-cycle. Nothing was dumped and
        nothing was touched. Wait for it to finish, or stop it first.

STOPPING. `docker stop libex-backup`. SIGTERM sets the runner's two stop
flags -- the asyncio Event that the sleeps and the wait for pg_dump both
watch, and the threading Event that a blocking upload running inside
asyncio.to_thread can poll from its transfer callback -- and every long step
consults one of them. A dump in progress terminates its pg_dump child rather
than abandoning it, and that part matters beyond this container: an orphaned
pg_dump holds its transaction snapshot open and its ACCESS SHARE locks with
it, so autovacuum reclaims nothing newer than that snapshot, and a migration
waiting on an ACCESS EXCLUSIVE queues behind it along with every reader that
arrives after.

Setting a flag is not stopping anything, which is the part that has to be
consulted rather than assumed: the flags were being set correctly while the
wait for pg_dump watched neither, and a stop mid-dump ran the dump to
completion regardless.

This process is PID 1 in that container, which is why nothing in this file
installs a signal handler of its own: PID 1 ignores any signal it has not
installed a handler for, so the runner's handlers are the only reason a
stop works at all, and a handler here would displace them. The stack's
stop_grace_period of 3900s is headroom for a dump already inside its 3600s
timeout to wind up -- it is a ceiling on how long Docker will wait, not a
delay anything spends.

WHAT THIS FILE DOES NOT DO. Four things the compose file states in comments
and cannot enforce are enforced inside the runner, not here, and this entry
point's contribution to each is to stay out of the way:

  - No destination resolved means idle, and take no dump. The runner logs
    the reason and stays up on a reminder loop instead of exiting, because
    `restart: unless-stopped` would turn a clean exit into a restart cycle;
    and it takes no dump, because nine minutes and 2.38 GB spent producing
    a file with nowhere to go is worse than nothing. Only --once exits.
  - The spool is claimed with an flock and only then cleared, in that
    order, before anything is written to it. A container killed mid-dump by
    a deploy, an OOM or a host reboot runs no exception handler, so a
    stranded partial can only be cleaned up by the next process to start --
    and the lock is what makes that clearance safe. Without it, a --once
    started while the scheduled cycle was dumping unlinked the file that
    dump was writing into: the child kept writing gigabytes into an inode
    with no name, exited 0, and the scheduled cycle failed with "dump
    artefact disappeared". Exit 3 is that collision now, refused up front.
  - The startup line names the destinations that resolved, and counts them.
    Settings is configured `extra="ignore"`, so a misspelled BACKUP_FTPS_
    name sets nothing and raises nothing -- that line is the only place the
    difference between configured and thought-I-configured is visible.
  - The artefact is never held in memory. mem_limit is 512m against a
    2.38 GB archive; pg_dump streams to the spool file and the upload
    streams from it. Nothing here reads it.

So this file logs nothing of its own. Every line the container emits comes
from the runner, which is where the facts are -- and one of those facts is
worth reading precisely: the verify step runs `pg_restore --list`, which
proves the archive opens, its header and table of contents parse, and the
expected tables are named in it. It does not decompress the data blocks. A
truncated archive whose header AND TABLE OF CONTENTS survived passes it,
measured -- the qualification is the whole of the claim, because the
truncation the check does catch is one that reached the TOC. The only thing
that proves a restore works is a restore.

AND WHEN YOU RUN ONE, ANALYZE AFTERWARDS. `pg_restore` loads rows and
rebuilds indexes; it does not carry pg_statistic across, so every table in
the restored database starts with no statistics at all and the planner works
from its built-in defaults. author_book drives /author/books and every
hydration join, and a nested loop chosen against a default estimate on a
table of that size is the difference between a fast API and an unusable one.
One statement, database-wide, before anything is pointed at it:

    VACUUM (ANALYZE);

An FTPS failure worth telling apart from the rest: a transfer that fails
after a successful login is not a TLS problem, even though the fix is in
the TLS layer. Servers configured to require TLS session reuse on the data
connection (vsftpd's `require_ssl_reuse`, on by default) answer 450 to a
data transfer whose session was not resumed from the control connection.
Removing the prot_p() call is the wrong fix and produces an unencrypted
transfer that appears to work.

ENVIRONMENT. app/services/backup reads all of it through app.core.config;
docker-compose.yml sets every name below with these defaults.

    DATABASE_URL                        required. SQLAlchemy form -- the
                                        runner drops the +asyncpg suffix
                                        before handing it to pg_dump.
    BACKUP_TIMEZONE             UTC     IANA name, read by zoneinfo.
    BACKUP_PERIOD               daily   daily, weekly or monthly.
    BACKUP_TIME                 03:00   HH:MM, local to BACKUP_TIMEZONE.
    BACKUP_DAY_OF_WEEK          sunday  weekly only; ignored otherwise.
    BACKUP_DAY_OF_MONTH         1       monthly only; ignored otherwise.
    BACKUP_RETENTION_RECENT     6       keep the last N artefacts.
    BACKUP_RETENTION_AGED_DAYS  30      plus the newest one at least this
                                        old, plus the oldest one not yet
                                        this old, held so the tier above it
                                        has a successor to promote when the
                                        current one is released. Eight
                                        artefacts at the defaults, not
                                        seven. 0 disables both.
    BACKUP_SPOOL_DIR            /backup-spool
    BACKUP_DUMP_TIMEOUT_SECONDS 3600    raising it means raising the
                                        stack's stop_grace_period too.
    BACKUP_DESTINATION_TIMEOUT_SECONDS  3600
    BACKUP_FTPS_HOST                    unset. The destination is inactive
    BACKUP_FTPS_PORT            21      until HOST, USER, PASSWORD and PATH
    BACKUP_FTPS_USER                    are all present; a partial set is
    BACKUP_FTPS_PASSWORD                inactive too, with a warning naming
    BACKUP_FTPS_PATH                    what is missing. Use PATH=. for the
    BACKUP_FTPS_CA_BUNDLE               login directory. Port 21 is explicit
    BACKUP_FTPS_SERVER_HOSTNAME         FTPS (AUTH TLS), not implicit on 990.
    LOG_LEVEL                   INFO    DEBUG, INFO, WARNING or ERROR.
    AXIOM_TOKEN                         unset. Set to also ship to Axiom.
    AXIOM_DATASET               libex
    LOG_RETENTION_DAYS          7       0 keeps everything.
"""

# Standard library
import argparse
import asyncio

# Core
from app.core.config import check_retired_env_vars
from app.core.logging import setup_logging

# Services
from app.services.backup import run_backup


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the scheduled Postgres backup: dump, verify, upload, prune."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Take a backup now, bypassing the schedule and the catch-up check, "
            "then exit -- not a single scheduler tick, which would usually do nothing."
        ),
    )
    args = parser.parse_args()

    # First, before anything else touches a logger, and never
    # logging.basicConfig. setup_logging is what attaches this process's
    # handlers at all -- without it a standalone script emits nothing -- and
    # what holds httpx and httpcore down to WARNING. basicConfig attaches a
    # root handler instead, which re-admits every third-party logger at INFO
    # across the whole process; that mute is the control keeping a URL and
    # its query string out of the logs, and it only holds if this runs.
    setup_logging()

    # After setup_logging, because a warning logged before the handlers are
    # attached goes nowhere, and before the run starts.
    check_retired_env_vars()

    exit_code = asyncio.run(run_backup(once=args.once))
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
