"""
Stack watcher: probes a stack's Audible exit, rotates it when it is blocked,
checks the stack's operational health, and tells the operator what happened.

Runs in its own sidecar container off scripts/stack_watch.py, never inside a
request. Nothing here imports app.db.session or any route. The only actuator
is the gluetun control server of the watcher's own stack; it never restarts a
container and never writes to the database. The one database access is a
read-only look at pg_stat_* views, through the watcher's own engine.

    gluetun    the control-server client and the rotation, which always ends
               with the VPN running
    gotify     notifications, sent direct and never through the proxy
    probes     the exit probe and the app probe
    checks     the operational checks and their one-alert-per-incident state
    heartbeat  the worker heartbeat file the seeder and backfill write and
               the watcher reads
"""
