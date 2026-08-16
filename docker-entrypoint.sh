#!/bin/sh
# Runs the migration once in the parent, then hands off to uvicorn.
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

# Reported once here rather than once per worker. Nothing derives from this
# number -- the Audible and database pools are per-process constants it
# multiplies -- so a drifted count changes the totals silently. The
# arithmetic is in docker-compose.yml. Unset means a single worker.
echo "Starting uvicorn with WEB_CONCURRENCY=${WEB_CONCURRENCY:-1}"

exec "$@"
