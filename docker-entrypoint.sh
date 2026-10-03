#!/bin/sh
# Migrates the database once, in the parent, and only when the command is the
# app's own uvicorn -- then hands off to whatever command the container was
# given. The operator one-offs under scripts/ run from this same image and so
# come through here too, but they never migrate.
#
# uvicorn spawns its workers rather than forking, so a child re-enters the
# interpreter and never re-executes this script -- which is the whole reason
# migrations live here. In the app's lifespan they would run once per worker,
# concurrently and unserialised, and alembic takes no lock of its own.
#
# The same missing lock is why only the app migrates. Every container started
# from a new image -- the seeder, the backfill, the refresh -- would otherwise
# race the API to the same DDL. And some of those commands must run against a
# schema the migration is refusing: scripts/region_keys.py prepares the
# database the region-key revision checks for, and its unfinalize is only
# valid while that revision has not run, so a container that migrated first
# would either refuse to start it or apply the key swap underneath it. An
# explicit `alembic ...` command is not preceded by a second upgrade either,
# so a downgrade is never undone by its own container. Deploy the API first;
# a script started on a new image before the API has migrated meets the old
# schema.
#
# A failed migration is fatal: the container exits with alembic's status and
# nothing is served against a schema that is not at head. Under the stack's
# restart policy that is a restart loop, which also covers Postgres not being
# up yet -- the next attempt finds it. The way out is the previous image, not
# a variable here.
case "$1" in
    uvicorn | */uvicorn)
        alembic upgrade head
        status=$?
        if [ "$status" -ne 0 ]; then
            echo "Refusing to start: alembic upgrade head exited $status, the schema is not at head" >&2
            exit "$status"
        fi
        echo "Database migrations applied"

        # Reported once here rather than once per worker. WEB_CONCURRENCY is
        # uvicorn's variable and nothing else reads it, so printing it for one
        # of the operator script containers -- same image, same entrypoint, a
        # different command -- names a number that governs nothing there, at
        # an operator who copied the app stack's environment and has every
        # reason to believe it did. Matched on the command itself, never on the
        # arguments after it: */uvicorn is the same command given as a
        # resolved path, and a route to uvicorn this misses starts without
        # migrating, so the image's CMD is the one supported way in.
        #
        # Nothing derives from this number -- the Audible and database pools
        # are per-process constants it multiplies -- so a drifted count changes
        # the totals silently. The arithmetic is in app/db/session.py. Unset
        # means a single worker.
        echo "Starting uvicorn with WEB_CONCURRENCY=${WEB_CONCURRENCY:-1}"
        ;;
esac

exec "$@"
