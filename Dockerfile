# The Postgres 16 client binaries, lifted from the official Postgres image
# rather than installed with apt, because Debian trixie -- what this stage and
# the runtime base below both are (/etc/debian_version 13.6 and 13.5, glibc
# 2.41 on each) -- carries no postgresql-client-16 package at all. apt here can
# only give us postgresql-client 17, and 17 is the one version that must not be
# used: pg_dump 17 writes archive header 1.16, and postgres:16-alpine's
# pg_restore -- the binary that is present wherever one of these dumps is
# actually restored -- refuses it outright with "unsupported version (1.16) in
# file header". Reproduced end to end before this landed, not read off a
# release note.
#
# The coupling runs the other way too, and that direction is the one a routine
# upgrade walks into rather than a deliberate choice: move the postgres service
# in docker-compose.yml to 17 and this pg_dump refuses to dump it at all --
# "aborting because of server version mismatch", with "server version: <17.x>;
# pg_dump version: <16.x>" beside it (both strings are in the 16 binary). It is
# documented behaviour, not a defect: pg_dump "cannot dump from PostgreSQL
# servers newer than its own major version; it will refuse to even try, rather
# than risk making an invalid dump". It also fails loudly -- the abort goes to
# stderr, which app/services/backup/dump.py pipes and logs as pg_dump_stderr,
# so a backup that stops this way says why on the first attempt. The server
# major version and this stage's tag move together, in the one change.
#
# The copy resolves at all only because both images are the same Debian
# release. Moving the base off trixie means re-checking this stage against it:
# a drifted libc or libpq soname produces a binary that builds fine and fails
# the first time a backup runs.
FROM postgres:16-trixie@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94 AS pgclient

FROM python:3.12-slim@sha256:a39549e211a16149edf74e5fdc9ef03a6767e46cd987c5048b6659b6c9904c94

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    # what the pg_dump and pg_restore copied in below link against
    # (libpq.so.5). It is already here as a dependency of libpq-dev, and is
    # named anyway because dpkg has no idea the copied binaries exist, let
    # alone what they need: a later cleanup that drops libpq-dev, or
    # multi-stages the compiler out, autoremoves libpq5 with nothing failing
    # at build time and pg_dump failing at the first backup instead.
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# The two binaries, not the client package. Measured, because the estimate
# this started from was out by more than an order of magnitude in the
# direction that would have killed the idea: 725,248 B of binaries, landing as
# two layers of 496 kB and 266 kB, under 0.5% of a 159 MB image. apt's
# postgresql-client is 18 packages, 10.4 MiB to fetch and ~61 MiB unpacked,
# nearly all of it perl -- which arrives because postgresql-client-common
# depends on it, not because anything here wants perl.
#
# /usr/local/bin is already on PATH for every user, including the
# unprivileged libex the image drops to below.
COPY --from=pgclient /usr/lib/postgresql/16/bin/pg_dump    /usr/local/bin/pg_dump
COPY --from=pgclient /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/pg_restore

RUN pip install --no-cache-dir --upgrade "pip>=26.1.2"

# The lock, not requirements.txt: every package and every transitive
# dependency pinned to one version and verified against its recorded hashes,
# the same guarantee the digest on the base image above and the checksums in
# fetch_docs_assets.sh already give. --require-hashes is redundant with a lock
# that hashes every line -- pip enters that mode on its own the moment it sees
# one hash -- and is written out anyway so the build fails loudly if a future
# edit ever lands an unhashed line here rather than quietly installing it.
# Regenerate with:
#   uv pip compile requirements.txt -c constraints.txt --generate-hashes \
#     --python-version 3.12 -o requirements.lock
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY . .

# The interactive docs are served from assets Libex ships rather than from a
# CDN, so a visitor's IP address never reaches a third party to render them.
# Pinned versions, checksum-verified -- a substituted file fails the build
# instead of being served to a browser.
RUN sh scripts/fetch_docs_assets.sh

# /app/logs and /backup-spool are the two paths the image expects to be
# writable. /backup-spool is created here, owned by the runtime user, so the
# named volume docker-compose.yml mounts over it inherits that ownership:
# Docker seeds a fresh named volume from whatever is at the mount point in the
# image, permissions included (measured -- an image directory owned by uid 1000
# produces a volume owned by uid 1000). Without the directory here the volume
# is created root-owned and the unprivileged process below cannot write a dump
# into it.
RUN mkdir -p /app/logs /backup-spool \
    && chmod +x /app/docker-entrypoint.sh \
    && useradd -m -u 1000 libex \
    && chown -R libex:libex /app /backup-spool
USER libex

EXPOSE 3333

ENTRYPOINT ["/app/docker-entrypoint.sh"]

# No --workers flag, deliberately. The worker count is set in exactly one place,
# WEB_CONCURRENCY in docker-compose.yml, and uvicorn reads that variable only
# when --workers is absent. A flag here would not sit alongside it, it would
# silently beat it -- the running count baked into the image while compose,
# Portainer and every figure derived by hand still read the variable.
#
# --no-access-log is a privacy control, not a noise control. uvicorn's access
# formatter appends the query string verbatim, so without it every request
# writes a stdout line carrying the raw title=, author= and name= text a caller
# typed -- the values _redact_query strips from Libex's own line, which
# LoggingMiddleware already records with the query redacted.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3333", "--no-access-log"]
