#!/bin/sh
# Runs the migration once in the parent, then hands off to whatever command the
# container was given -- uvicorn for the app, a scripts/ module for the operator
# one-offs, which run from this same image and so come through here too.
#
# uvicorn spawns its workers rather than forking, so a child re-enters the
# interpreter and never re-executes this script -- which is the whole reason
# migrations live here. In the app's lifespan they would run once per worker,
# concurrently and unserialised, and alembic takes no lock of its own.
#
# A failed migration is logged and start continues, matching what the lifespan
# did; making it fatal is a separate decision with its own consequences.
alembic upgrade head
status=$?

if [ "$status" -eq 0 ]; then
    echo "Database migrations applied"
else
    echo "Database unavailable on startup: alembic upgrade head exited $status" >&2
fi

# Reported once here rather than once per worker, and only when uvicorn is what
# starts. WEB_CONCURRENCY is uvicorn's variable and nothing else reads it, so
# printing it for one of the operator script containers -- same image, same
# entrypoint, a different command -- names a number that governs nothing there,
# at an operator who copied the app stack's environment and has every reason to
# believe it did. Matched on the command itself, never on the arguments after
# it: a false positive is the defect being fixed, while a line missed for some
# unsupported route to uvicorn costs nothing. */uvicorn is the same command
# given as a resolved path.
#
# Nothing derives from this number -- the Audible and database pools are
# per-process constants it multiplies -- so a drifted count changes the totals
# silently. The arithmetic is in docker-compose.yml. Unset means a single worker.
case "$1" in
    uvicorn | */uvicorn)
        echo "Starting uvicorn with WEB_CONCURRENCY=${WEB_CONCURRENCY:-1}"
        ;;
esac

exec "$@"
