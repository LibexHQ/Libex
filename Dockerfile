FROM python:3.12-slim@sha256:a39549e211a16149edf74e5fdc9ef03a6767e46cd987c5048b6659b6c9904c94

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

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

RUN mkdir -p /app/logs \
    && chmod +x /app/docker-entrypoint.sh \
    && useradd -m -u 1000 libex \
    && chown -R libex:libex /app
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
