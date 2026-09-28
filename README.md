<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="app/static/logo-dark.png">
  <img src="app/static/logo.png" alt="Libex" width="400">
</picture>

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://github.com/LibexHQ/Libex/actions/workflows/tests.yml/badge.svg)](https://github.com/LibexHQ/Libex/actions/workflows/tests.yml)
[![GHCR](https://img.shields.io/badge/ghcr.io-libexhq%2Flibex-blue)](https://github.com/LibexHQ/Libex/pkgs/container/libex)
[![Docker Hub](https://img.shields.io/badge/docker%20hub-sunbrolynk%2Flibex-blue)](https://hub.docker.com/r/sunbrolynk/libex)

[![Books](https://libexdb.com/db/stats/badge/books.svg)](https://libexdb.com/db/stats)
[![Books with Chapters](https://libexdb.com/db/stats/badge/booksWithChapters.svg)](https://libexdb.com/db/stats)
[![Authors](https://libexdb.com/db/stats/badge/authors.svg)](https://libexdb.com/db/stats)
[![Narrators](https://libexdb.com/db/stats/badge/narrators.svg)](https://libexdb.com/db/stats)
[![Series](https://libexdb.com/db/stats/badge/series.svg)](https://libexdb.com/db/stats)

Open, unrestricted Audible metadata API for the audiobook automation community.

</div>

> [!WARNING]
> **The public instance is moving to [libexdb.com](https://libexdb.com).**
>
> The old address `libex.lostcartographer.xyz` **stops serving on 4 November 2026**.
> If you use the public instance, update your configuration to `https://libexdb.com`.
>
> [Details and questions →](https://github.com/LibexHQ/Libex/issues/183)

> [!NOTE]
> **Self-hosting?** This doesn't affect you — you point at your own server. Nothing to do.

---

## Public Instance

A free public instance of Libex is available at [Libex](https://libexdb.com)

This instance is maintained by the Libex project and is free for community use. No API key required. No rate limits beyond what Audible naturally enforces.

If you rely on Libex for a project or tool, we recommend self-hosting your own instance for reliability and control.

---

## Regions

Libex serves all eleven Audible marketplaces. The counts are the public
instance's stored library, read live from `GET /db/stats?region=xx` — each
badge links to the call it comes from. Narrators isn't a column: that table has
no region column, so a scoped call returns the same figure as the global
Narrators badge above, for every region. Coverage grows with regional traffic —
every `?region=xx` request that fetches from Audible persists what it gets.

| Code | Region | Books | Authors | Series | Books w/ Chapters |
|---|---|---|---|---|---|
| `us` | United States | [![](https://libexdb.com/db/stats/badge/books.svg?region=us&label=false)](https://libexdb.com/db/stats?region=us) | [![](https://libexdb.com/db/stats/badge/authors.svg?region=us&label=false)](https://libexdb.com/db/stats?region=us) | [![](https://libexdb.com/db/stats/badge/series.svg?region=us&label=false)](https://libexdb.com/db/stats?region=us) | [![](https://libexdb.com/db/stats/badge/booksWithChapters.svg?region=us&label=false)](https://libexdb.com/db/stats?region=us) |
| `uk` | United Kingdom | [![](https://libexdb.com/db/stats/badge/books.svg?region=uk&label=false)](https://libexdb.com/db/stats?region=uk) | [![](https://libexdb.com/db/stats/badge/authors.svg?region=uk&label=false)](https://libexdb.com/db/stats?region=uk) | [![](https://libexdb.com/db/stats/badge/series.svg?region=uk&label=false)](https://libexdb.com/db/stats?region=uk) | [![](https://libexdb.com/db/stats/badge/booksWithChapters.svg?region=uk&label=false)](https://libexdb.com/db/stats?region=uk) |
| `ca` | Canada | [![](https://libexdb.com/db/stats/badge/books.svg?region=ca&label=false)](https://libexdb.com/db/stats?region=ca) | [![](https://libexdb.com/db/stats/badge/authors.svg?region=ca&label=false)](https://libexdb.com/db/stats?region=ca) | [![](https://libexdb.com/db/stats/badge/series.svg?region=ca&label=false)](https://libexdb.com/db/stats?region=ca) | [![](https://libexdb.com/db/stats/badge/booksWithChapters.svg?region=ca&label=false)](https://libexdb.com/db/stats?region=ca) |
| `au` | Australia | [![](https://libexdb.com/db/stats/badge/books.svg?region=au&label=false)](https://libexdb.com/db/stats?region=au) | [![](https://libexdb.com/db/stats/badge/authors.svg?region=au&label=false)](https://libexdb.com/db/stats?region=au) | [![](https://libexdb.com/db/stats/badge/series.svg?region=au&label=false)](https://libexdb.com/db/stats?region=au) | [![](https://libexdb.com/db/stats/badge/booksWithChapters.svg?region=au&label=false)](https://libexdb.com/db/stats?region=au) |
| `de` | Germany | [![](https://libexdb.com/db/stats/badge/books.svg?region=de&label=false)](https://libexdb.com/db/stats?region=de) | [![](https://libexdb.com/db/stats/badge/authors.svg?region=de&label=false)](https://libexdb.com/db/stats?region=de) | [![](https://libexdb.com/db/stats/badge/series.svg?region=de&label=false)](https://libexdb.com/db/stats?region=de) | [![](https://libexdb.com/db/stats/badge/booksWithChapters.svg?region=de&label=false)](https://libexdb.com/db/stats?region=de) |
| `fr` | France | [![](https://libexdb.com/db/stats/badge/books.svg?region=fr&label=false)](https://libexdb.com/db/stats?region=fr) | [![](https://libexdb.com/db/stats/badge/authors.svg?region=fr&label=false)](https://libexdb.com/db/stats?region=fr) | [![](https://libexdb.com/db/stats/badge/series.svg?region=fr&label=false)](https://libexdb.com/db/stats?region=fr) | [![](https://libexdb.com/db/stats/badge/booksWithChapters.svg?region=fr&label=false)](https://libexdb.com/db/stats?region=fr) |
| `it` | Italy | — | — | — | — |
| `es` | Spain | [![](https://libexdb.com/db/stats/badge/books.svg?region=es&label=false)](https://libexdb.com/db/stats?region=es) | [![](https://libexdb.com/db/stats/badge/authors.svg?region=es&label=false)](https://libexdb.com/db/stats?region=es) | [![](https://libexdb.com/db/stats/badge/series.svg?region=es&label=false)](https://libexdb.com/db/stats?region=es) | [![](https://libexdb.com/db/stats/badge/booksWithChapters.svg?region=es&label=false)](https://libexdb.com/db/stats?region=es) |
| `jp` | Japan | [![](https://libexdb.com/db/stats/badge/books.svg?region=jp&label=false)](https://libexdb.com/db/stats?region=jp) | [![](https://libexdb.com/db/stats/badge/authors.svg?region=jp&label=false)](https://libexdb.com/db/stats?region=jp) | [![](https://libexdb.com/db/stats/badge/series.svg?region=jp&label=false)](https://libexdb.com/db/stats?region=jp) | [![](https://libexdb.com/db/stats/badge/booksWithChapters.svg?region=jp&label=false)](https://libexdb.com/db/stats?region=jp) |
| `in` | India | — | — | — | — |
| `br` | Brazil | — | — | — | — |

`it`, `in` and `br` are supported like every other market but have seen almost
no traffic yet, so there's next to nothing stored for them. A dash means
there's too little stored there to be worth a badge, not that Libex can't
serve that region.

---

## Why Libex?

The audiobook automation community has long depended on metadata services to power tools like Readarr, Audiobookshelf, and custom managers. When those services disappear or restrict usage, every project depending on them breaks.

Libex exists to be a permanent, community-owned alternative:

- **MIT licensed** — no restrictions, fork it, build on it, use it however you want
- **No usage restrictions** — works with any software, any workflow
- **Drop-in replacement** — compatible with AudiMeta's API endpoints
- **Audible-first** — Audible is the source of truth; the local database is a fallback and a cache, not a crutch
- **Persistent local library** — every book, author, and series ever requested is stored and queryable
- **All regions** — full support for all Audible markets without language restrictions
- **Self-hostable** — one `docker compose up` and you're running

---

## Quick Start

Pull the image:
```bash
# GHCR
docker pull ghcr.io/libexhq/libex:latest

# Docker Hub
docker pull sunbrolynk/libex:latest
```

Deploy:
```bash
# 1. Create a directory
mkdir libex && cd libex

# 2. Download the compose file
curl -O https://raw.githubusercontent.com/LibexHQ/Libex/main/docker-compose.yml

# 3. Create your environment file
cp .env.example .env
# Edit .env — DB_PASSWORD and AUDIBLE_PROXY_URL are both required. For
# AUDIBLE_PROXY_URL, either fill in the six API_WG_* values to use the
# bundled example VPN sidecar, or point it at a proxy of your own — see
# VPN / Egress below. Everything else has a sensible default.

# 4. Start Libex
docker compose up -d

# 5. Verify
curl http://localhost:3333/health
```

This deploys the API stack — the only one you need to serve requests, and the
one to deploy first: it creates the `libex-db` and `libex-egress` Docker
networks the other four stacks below join by name. Each of the other four is
its own compose file, deployed separately with `docker compose -f <file> up
-d`: `docker-compose.seeder.yml` (expands the local library in the
background), `docker-compose.backfill.yml` (fills in chapters for stored
books that have none yet, then exits), `docker-compose.refresh.yml`
(re-fetches every stored book, then exits), and `docker-compose.backup.yml`
(scheduled Postgres backups). See Configuration and VPN / Egress below for
what each one needs.

Or copy the compose file directly:

```yaml
services:
  libex:
    image: ghcr.io/libexhq/libex:latest
    container_name: libex
    restart: unless-stopped
    # host side only; the container always listens on 3333.
    ports:
      - "${PORT:-3333}:3333"
    # an allowlist: a name missing here never reaches the app.
    environment:
      - DATABASE_URL=postgresql+asyncpg://${DB_USER:-libex}:${DB_PASSWORD}@postgres:5432/${DB_NAME:-libex}
      - CACHE_ENABLED=${CACHE_ENABLED:-true}
      - CACHE_TTL=${CACHE_TTL:-86400}
      - DEFAULT_REGION=${DEFAULT_REGION:-us}
      # required: this stack's own VPN exit.
      - AUDIBLE_PROXY_URL=${AUDIBLE_PROXY_URL:?set AUDIBLE_PROXY_URL to this stack's VPN proxy, e.g. http://libex-vpn:8888}
      # a literal, not a knob: per-process budgets are sized to it.
      - WEB_CONCURRENCY=6
      - LOG_RETENTION_DAYS=${LOG_RETENTION_DAYS:-7}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - AXIOM_TOKEN=${AXIOM_TOKEN:-}
      - AXIOM_DATASET=${AXIOM_DATASET:-libex}
      - SEED_SECRET=${SEED_SECRET:-}
      - MIGRATION_NOTICE_ENABLED=${MIGRATION_NOTICE_ENABLED:-false}
      - MIGRATION_NEW_HOST=${MIGRATION_NEW_HOST:-}
      - MIGRATION_ANNOUNCED=${MIGRATION_ANNOUNCED:-}
      - MIGRATION_SUNSET=${MIGRATION_SUNSET:-}
      - MIGRATION_INFO_URL=${MIGRATION_INFO_URL:-}
    volumes:
      - ${LOGS_PATH:-./logs}:/app/logs
    depends_on:
      postgres:
        condition: service_healthy
    # unhealthy is reported, never acted on.
    healthcheck:
      test: ["CMD", "curl", "-f", "http://${HEALTHCHECK_HOST:-localhost}:3333/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
    # json-file keeps everything unless capped.
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
    # default reaches postgres; libex-egress reaches the exit.
    # naming any network drops the implicit default -- keep it listed.
    networks:
      - default
      - libex-egress

  # example exit; replace or remove.
  libex-vpn:
    image: qmcgaw/gluetun:v3
    container_name: libex-vpn
    restart: unless-stopped
    cap_add:
      - NET_ADMIN
    environment:
      - VPN_SERVICE_PROVIDER=custom
      - VPN_TYPE=wireguard
      - WIREGUARD_PRIVATE_KEY=${API_WG_PRIVATE_KEY:-}
      - WIREGUARD_ADDRESSES=${API_WG_ADDRESS:-}
      - WIREGUARD_ENDPOINT_IP=${API_WG_ENDPOINT_IP:-}
      - WIREGUARD_ENDPOINT_PORT=${API_WG_ENDPOINT_PORT:-}
      - WIREGUARD_PUBLIC_KEY=${API_WG_PUBLIC_KEY:-}
      - WIREGUARD_PRESHARED_KEY=${API_WG_PRESHARED_KEY:-}
      - HTTPPROXY=on
      - HTTPPROXY_LOG=on
    # egress only. never publish 8888.
    networks:
      - libex-egress
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

  postgres:
    image: postgres:16-alpine
    container_name: libex-postgres
    restart: unless-stopped
    # six workers x 20, plus every job stack.
    command: ["postgres", "-c", "max_connections=200"]
    # parallel queries outgrow Docker's 64MB default.
    shm_size: ${DB_SHM_SIZE:-1gb}
    # loopback: the other stacks use libex-db, not this port.
    ports:
      - "${DB_BIND:-127.0.0.1}:5432:5432"
    environment:
      POSTGRES_DB: ${DB_NAME:-libex}
      POSTGRES_USER: ${DB_USER:-libex}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
    # default is libex's route here. never on egress.
    networks:
      - default
      - libex-db
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER:-libex}"]
      interval: 10s
      timeout: 5s
      retries: 5
    # STOPSIGNAL is SIGINT: fast shutdown, then a checkpoint; a kill mid-checkpoint
    # forces crash recovery. A max wait, not a delay.
    stop_grace_period: 120s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

networks:
  # pinned: the other stacks join these by name.
  libex-db:
    name: libex-db
    driver: bridge
  libex-egress:
    name: libex-egress
    driver: bridge
```

---

## Logging & Privacy

**Libex does not record who calls it.** No IP address is logged — not in full,
not truncated, not hashed — and no header or connection detail that would
identify a caller is written anywhere. Search text is stripped from the log
lines Libex writes about a request, with one exception noted below. See
[PRIVACY.md](PRIVACY.md) for the full policy.

The public instance uses [Axiom](https://axiom.co) for structured request
logging, so that broken endpoints and failing deploys are visible. This is
disclosed transparently.

**What is logged:**
- Request method, path, and status code
- Response time
- Query parameters — **names and values are allowlisted.** Structural params
  (`region`, `limit`, `page`, `sort`, filters) keep their values; anything a
  caller typed (`name`, `keywords`, `title`, `author`) keeps its name and loses
  its value to `REDACTED`; a name Libex doesn't recognise is dropped from the
  line entirely
- User agent — this names client *software*, not a person, and with no address
  logged beside it there is nothing to tie it back to an individual
- Cache hit/miss
- The process id of the worker that handled the request — a number belonging to
  the server, identical for every request that worker serves, and unrelated to
  who sent any of them
- A request id — a random value minted fresh for each request and echoed back
  in the `X-Request-Id` response header, so you can quote one request in a bug
  report. It's never taken from a header you send, and it isn't reused between
  requests
- The `Host` header — which hostname the request came in on, currently useful
  only for telling `libex.lostcartographer.xyz` traffic apart from
  `libexdb.com` traffic during the migration
- Whether the response was complete and, if not, why, plus where the data in
  it came from (Audible, cache, DB, or a mix) — the same `X-Libex-Complete`,
  `X-Libex-Incomplete-Reason` and `X-Libex-Source` values described under
  Response headers below, describing what Libex sent back rather than who
  asked for it
- Errors and exceptions. An unhandled error logs its message so a broken deploy
  can be diagnosed; if a message was built from something you sent, that text
  appears in that one line

**What is NOT logged:**
- Your IP address, in any form
- Anything you typed into a search, apart from the error case above
- Cookies, trackers, or fingerprinting of any kind — Libex sets none

**Why we log:**
To see which endpoints are failing and how fast they respond. Without
per-endpoint visibility, a bad release breaks things silently. None of that
requires knowing who you are.

**Who can see the logs:**
The instance maintainer is the only one with query access to the Axiom
dataset, but Axiom itself holds it too — a vendor storing your data on its own
infrastructure is a third party with access to it, not just the maintainer.
On the public instance, logs are retained for 30 days and then automatically
deleted by Axiom — a setting on that Axiom dataset, not a property of Libex
itself. The public instance also sits behind Cloudflare, which sees every
request in order to terminate TLS — including your real IP, which is outside
Libex's control. Axiom and Cloudflare receive this data only to provide those
services. They're not the only recipients of request data — see [Who receives
data](PRIVACY.md#who-receives-data) in the privacy notice for the complete
list, including Audible and the VPN provider, and what each can see.

**The API docs are served locally.** The interactive docs at `/docs` and
`/redoc` are rendered from assets Libex ships — not from a CDN. Opening them
contacts nothing but Libex.

**If you self-host:**
Logging is completely optional. Leave `AXIOM_TOKEN` empty and Libex logs to
stdout and a rotating file only — nothing leaves your server. Logs print at
`LOG_LEVEL` (default `INFO`); set it to `DEBUG` for more detail when
troubleshooting. Warnings and errors go to stderr, everything else to stdout,
and structured context (counts, IDs) is appended to each line so you can see
what a task actually did.

---

## API Behavior

**Response headers:** Every response carries `X-Request-Id`, a fresh id for quoting a specific request in a bug report. The book, series and author endpoints also carry `X-Libex-Complete` (whether the body holds everything you asked for) and, when it doesn't, `X-Libex-Incomplete-Reason`; most of them also carry `X-Libex-Source` (where the data in the body came from — Audible, cache, DB, or a mix). All four are exposed through CORS for browser JavaScript to read. Full contract in `/docs`.

**Caching:** The book, series and author endpoints, plus `/quick-search`, default to serving Libex's stored copy (`cache=true`), which can be up to `CACHE_TTL` seconds old. Pass `cache=false` on any of them to force a live Audible fetch instead.

**HTML content:** `description` and `summary` fields on book responses, `description` on author responses, and `description` on series responses are returned as plain text with HTML stripped.

**Image URLs:** Cover image URLs are returned with Audible size suffixes stripped, giving you the base high-resolution image URL.

**ASIN validation:** All ASIN parameters are validated against Audible's 10-character alphanumeric format. Invalid ASINs return a 404 with a clear error message.

**Region validation:** All region parameters are validated against supported Audible regions. Invalid regions return a 400 error.

**Local database:** Every successful Audible response is written to a persistent relational database. This powers the DB query endpoints and serves as a fallback when Audible is unavailable.

**Virtual Voice Audiobooks:** Book responses include `isVvab` (boolean indicating whether the book is a Virtual Voice Audiobook — AI-narrated rather than human-narrated).

**Audible plans:** Book responses include `plans` (list of Audible plan names such as `"US Minerva"` or `"AccessViaMusic"`), letting clients determine subscription availability programmatically.

**Everything else Audible sends:** Book responses include `audibleExtras`, every top-level field from Audible's response that no other field on the book already covers — so a field Audible adds later shows up here instead of being dropped. It's `null` when nothing has been captured for that book yet, `{}` when Audible had nothing extra to add, and a populated object otherwise; treat its contents as untrusted upstream data. The accumulation lives in the local database rather than in each response — the stored record unions the keys from every fetch it has seen, so a key Audible stops sending is still kept there and the `/db/*` endpoints are where you read it deliberately, while a book served from a live Audible fetch or from cache carries just that one fetch's blob. `extrasWithheld` is `null` unless something was left out (for example a podcast's per-episode entries, or a blob too large to keep), in which case it records what and why.

**Narrator profiles:** Narrator responses from `/db/narrator` include enrichment data sourced from [NarratorList.com](https://narratorlist.com) and [AussieNarrator.com](https://aussienarrator.com) where available. When profile data is present, the response includes `source`, `sourceUrl` (link to the narrator's full profile), `sourceUpdatedAt`, and an `attribution` string (e.g. `"Profile data provided by NarratorList.com, retrieved May 2026"`). Consumers displaying narrator data should include this attribution where practical.

---

## Audiobookshelf Configuration

Audiobookshelf's custom metadata provider calls `/{region}/search`, not `/search`. When configuring ABS, set your base URL to include the region:

```
http://YOUR-IP:3333/us
```

ABS will then call `/us/search?title=...&author=...` which returns the `{"matches": [...]}` format ABS expects. The flat `/search` endpoint returns a different format that ABS cannot parse.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/book/{asin}` | Get book by ASIN |
| GET | `/book` | Get multiple books by ASIN (comma-separated, max 1000) |
| GET | `/book/{asin}/chapters` | Get chapter information |
| GET | `/book/sku/{sku}` | Get all region variants of a book by SKU group |
| GET | `/author/{asin}` | Get author profile |
| GET | `/author/{asin}/books` | Get all books by author ASIN (legacy) |
| GET | `/author/books/{asin}` | Get all books by author ASIN |
| GET | `/author/books` | Get books by author name |
| GET | `/author` | Search authors by name |
| GET | `/series/{asin}` | Get series metadata |
| GET | `/series/{asin}/books` | Get all books in a series (legacy) |
| GET | `/series/books/{asin}` | Get all books in a series |
| GET | `/series` | Search series by name |
| GET | `/narrator/books` | Get books by narrator name |
| GET | `/search` | Search Audible catalog |
| GET | `/quick-search` | Quick search via suggestions |
| GET | `/new-releases` | Recently released books, scanned live from Audible, newest first. Scope to one `category` (from `/categories`); without it, returns a live sample |
| GET | `/coming-soon` | Upcoming books, scanned live from Audible, soonest first. Scope to one `category` (from `/categories`); without it, returns a live sample |
| GET | `/categories` | List Audible's genre categories for a region as a nested tree (up to five levels deep), or a flat list with `?flat=true`; limit the levels with `?depth=N` (`depth=1` for just the top-level parents) — the ids for the `category` param |
| GET | `/{region}/search` | Regional search for Audiobookshelf compatibility |
| GET | `/{region}/quick-search/search` | Regional quick search for Audiobookshelf compatibility |
| GET | `/db/book` | Query the local indexed book library |
| GET | `/db/book/{asin}` | Get a single book from local DB |
| GET | `/db/book/{asin}/chapters` | Get chapter data from local DB |
| GET | `/db/book/sku/{sku}` | Get books by SKU group from local DB |
| GET | `/db/plans` | Get all distinct Audible plan names from local DB |
| GET | `/db/plans/{plan_name}` | Get all books under a specific plan from local DB |
| GET | `/db/genres` | Get all distinct genre/tag names from local DB |
| GET | `/db/vvab` | Get all virtual voice audiobooks (AI-narrated) from local DB |
| GET | `/db/new-releases` | Get recently released books from local DB, newest first |
| GET | `/db/coming-soon` | Get upcoming books from local DB, soonest first |
| GET | `/db/stats` | Get counts of books, authors, narrators, series, and books with chapters in local DB. Pass `region` to scope books, authors, series, and booksWithChapters to one region — narrators has no region column so it stays global, and a scoped response also carries `seriesRegionUnknown` (series with no recorded region, excluded from every per-region count) |
| GET | `/db/stats/badge/{metric}.svg` | The same counts drawn as an SVG badge, for embedding in a page. `metric` is one of `books`, `booksWithChapters`, `authors`, `narrators`, `series`; `region` scopes it the same way, and `label=false` draws the bare number with no label |
| GET | `/db/author/{asin}` | Get author from local DB |
| GET | `/db/author/{asin}/books` | Get author's books from local DB |
| GET | `/db/narrator` | Search narrators by name from local DB |
| GET | `/db/narrator/books` | Get books by narrator name from local DB |
| GET | `/db/series/{asin}` | Get series from local DB |
| GET | `/db/series/{asin}/books` | Get series books from local DB |
| GET | `/health` | Health check |

Full interactive documentation available at `/docs` when running.

---

## DB Query Endpoints

`GET /db/book` queries books that have been fetched and stored locally without hitting Audible. Useful for searching your indexed library by metadata.

All parameters are optional but at least one filter (or a `sort`) must be provided. Supports pagination via `limit` (default 20, max 100) and `page` (default 1).

The same filter set is available on the other book-list DB endpoints too — `/db/vvab`, `/db/plans/{plan_name}`, `/db/author/{asin}/books`, `/db/series/{asin}/books`, and `/db/narrator/books` — minus whichever field is already the endpoint's scope (e.g. `/db/vvab` doesn't take `is_vvab`).

| Parameter | Type | Match |
|-----------|------|-------|
| `title` | string | ILIKE |
| `subtitle` | string | ILIKE |
| `author_name` | string | ILIKE (join) |
| `series_name` | string | ILIKE (join) |
| `description` | string | ILIKE |
| `summary` | string | ILIKE |
| `publisher` | string | ILIKE |
| `copyright` | string | ILIKE |
| `isbn` | string | ILIKE |
| `region` | string | exact |
| `language` | string | exact |
| `book_format` | string | exact |
| `content_type` | string | exact |
| `content_delivery_type` | string | exact |
| `rating_better_than` | float | >= |
| `rating_worse_than` | float | <= |
| `longer_than` | int | >= (minutes) |
| `shorter_than` | int | <= (minutes) |
| `explicit` | bool | exact |
| `whisper_sync` | bool | exact |
| `has_pdf` | bool | exact |
| `is_listenable` | bool | exact |
| `is_buyable` | bool | exact |
| `is_vvab` | bool | exact |
| `plan_name` | string | JSONB contains |
| `genre` | string | ILIKE against genre/tag names (e.g. `fantasy` matches "Science Fiction & Fantasy") |
| `category` | string | Exact match on a category id from `/categories` (e.g. `18580628011`), or a comma-separated list to match any of several (e.g. `18580628011,18573212011`). Use `genre` for broad name matching |

Use `/db/genres` to discover the genre/tag names for `genre` (optionally with `?search=`), or `/categories` to discover the category ids for `category`.

### Sorting

The DB list endpoints and the live book-list endpoints (`/author/{asin}/books`, `/series/{asin}/books`, `/author/books`, bulk `/book`) accept `sort` and `order` (`asc`/`desc`). Sortable fields: `title`, `releaseDate`, `rating`, `lengthMinutes`, `language`, `publisher`, `updatedAt`. Series book endpoints default to series position order unless a sort field is given. The live endpoints sort the returned set; sorting isn't offered on relevance-ranked search.

Those same live book-list endpoints also accept a subset of the filters — rating range, length range, `language`, `book_format`, the booleans, `plan_name`, and `genre` — applied to the returned set. The heavier free-text filters stay on `/db/book`, which has the indexes for them.

### Release windows (new releases & coming soon)

There are two pairs of endpoints for browsing by release date — a local DB pair and a live Audible pair. All four take a `days` window (one of 30, 60, 90, 120, 240, 365; default 30) and the full live filter set, plus `sort`/`order`. The live pair also takes an optional `category` (see below).

**Local DB** (instant, no Audible call):
- `GET /db/new-releases` — books released in the last `days`, newest first. Already-released only.
- `GET /db/coming-soon` — books releasing in the next `days`, soonest first. Future releases only; Audible's "no date yet" placeholder is excluded.

**Live** (scanned fresh from Audible):
- `GET /new-releases` — same look-back, newest first.
- `GET /coming-soon` — same look-ahead, soonest first.

Audible exposes no direct new-releases or coming-soon feed, and any single catalog query is capped at a few hundred results, so the live pair reconstructs each list by scanning the catalog. To stay fast, the live endpoints scan **one category at a time**: pass a `category` id (from `GET /categories`) to get the full window for that category. Without a `category`, the scan walks Audible's un-categoried catalog — which is capped — so the bare call returns a **live sample**, not the whole catalog. The results are date-based and can't change until the date rolls over, so they're cached until the next UTC midnight and refresh on the first request of the new day.

For the **complete** list across all categories, use the **DB** endpoints (`/db/new-releases`, `/db/coming-soon`) — the seeder walks every category in the background and keeps them current — or aggregate per-category `/new-releases` calls client-side. Use the **live** endpoints when you want the freshest data for a specific category straight from Audible, including brand-new pre-orders the seeder may not have picked up yet.

`GET /categories` returns the category ids (and names) you can pass as `category`, as a nested tree that mirrors Audible's full taxonomy — up to five levels deep and ragged (some branches stop early, some go the full depth), each node carrying its own children. It's fetched fresh from Audible on each call and reconciled into the local store, which mirrors Audible's current taxonomy — new categories are added and ones Audible has moved or dropped are pruned, so a reshuffle on their end doesn't leave stale entries behind. Pass `?flat=true` to get a flat list instead of the tree — each node carries its `ancestors` (the {id, name} chain from the top-level root down to its parent, in order), so its depth and lineage are still recoverable. Pass `?depth=N` to limit how many levels come back — `depth=1` returns just the top-level parents, `depth=2` the top two levels, and so on; this works with both the nested and flat forms. Note this is Audible's *category* taxonomy, which is different from `/db/genres` (the genre/tag *names* attached to stored books).

### Narrator filters

`GET /db/narrator` searches narrators by name and also filters on `gender`, `language` (matches a language the narrator works in), `audiobooks_produced` (one of the count buckets), `source`, and `cultural_heritage`.

---

## Configuration

### API stack (`docker-compose.yml`)

Copy `.env.example` to `.env` and configure:

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PASSWORD` | — | **Required.** PostgreSQL password |
| `DB_NAME` | `libex` | PostgreSQL database name |
| `DB_USER` | `libex` | PostgreSQL username |
| `DB_BIND` | `127.0.0.1` | Interface Postgres's published port binds to. Loopback only reaches from the same machine (or over an SSH tunnel); set to `0.0.0.0` to reach it from elsewhere — only the Postgres password guards it |
| `PORT` | `3333` | Host port the API is exposed on |
| `CACHE_TTL` | `86400` | Default cache TTL in seconds (24 hours); some endpoints use their own TTL |
| `LOG_LEVEL` | `INFO` | Log verbosity — `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `LOG_RETENTION_DAYS` | `7` | Days of rotated logs to keep. `0` = infinite, no rotation |
| `AXIOM_TOKEN` | — | Axiom API token (optional — leave blank for stdout only) |
| `AXIOM_DATASET` | `libex` | Axiom dataset name |
| `AUDIBLE_PROXY_URL` | — | **Required.** Proxy URL for outbound Audible requests only — the stack refuses to deploy with this blank, naming the missing variable. Only `http://` and `https://` are supported; any other scheme, or a value that doesn't parse as a proxy URL, also refuses to start rather than send traffic out unproxied. API serving, the database, and logging are unaffected either way. See VPN / Egress below for how to supply one |
| `SEED_SECRET` | — | PBKDF2 hash for the internal seed endpoint. Empty = endpoint disabled. Generate with `python -m app.api.routes.internal.router` |

`DATABASE_URL` is constructed automatically by docker-compose from `DB_NAME`, `DB_USER`, and `DB_PASSWORD`. Only set it manually if running outside of Docker — and whatever it points at must be PostgreSQL 14 or newer, see Self-Hosting Notes below.

This stack also creates two Docker networks, `libex-db` and `libex-egress`, which the seeder, backfill, refresh, and backup stacks below join by name — deploy this one first.

### Seeder stack (`docker-compose.seeder.yml`)

The seeder is not part of the API stack — it deploys as its own stack, with its own environment, on the **same Docker host** as the API stack (it reaches Postgres over a Docker network the API stack creates, which doesn't cross hosts). See **Database seeder** under Self-Hosting Notes below for what it does.

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PASSWORD` | — | **Required.** The same password the API stack's `DB_PASSWORD` carries |
| `SEEDER_PROXY_URL` | — | **Required.** The seeder's own outbound proxy, separate from the API stack's `AUDIBLE_PROXY_URL` so it never shares the API's exit IP. Its hostname must contain `seeder` or the seeder refuses to start. See VPN / Egress below |
| `DB_USER` | `libex` | PostgreSQL username |
| `DB_NAME` | `libex` | PostgreSQL database name |
| `CACHE_TTL` | `86400` | Shared with the API stack — both write into the same cache table |
| `SEEDER_INTERVAL_HOURS` | `24` | Hours between seeder cycles |
| `SEEDER_REQUEST_DELAY` | `1.0` | Seconds between Audible requests during seeding |
| `SEEDER_REGIONS` | `us` | Comma-separated regions to seed (e.g. `us,uk,de`) |
| `SEEDER_NEW_RELEASES_INTERVAL_HOURS` | `24` | Hours between new-releases worker runs |
| `SEEDER_REFRESH_ENABLED` | `false` | Re-fetch a book's details around its release date, from pre-order through 30 days after release |
| `SEEDER_MEM_LIMIT` | `1g` | Memory ceiling for the seeder container. Sized against its worst-case backlog, not measured against a live run — raise it if the container is repeatedly OOM-killed and restarted rather than assuming a bug |
| `LOG_RETENTION_DAYS` | `7` | Days of rotated logs to keep. `0` = infinite, no rotation |
| `LOG_LEVEL` | `INFO` | Log verbosity — `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `AXIOM_TOKEN` | — | Axiom API token (optional — leave blank for stdout only) |
| `AXIOM_DATASET` | `libex` | Axiom dataset name |

`DB_PASSWORD` and `SEEDER_PROXY_URL` have no default in `docker-compose.seeder.yml` — a missing one fails the stack deploy naming the variable, rather than starting a container that can't connect.

### Chapter backfill stack (`docker-compose.backfill.yml`)

A one-off job, not a long-running service: it fills in chapters for stored books that have none checked yet, then exits (`restart: "no"` — a finished run stays finished). Deploy after the API stack. See **Chapter backfill** under Self-Hosting Notes below.

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PASSWORD` | — | **Required.** The same password the API stack's `DB_PASSWORD` carries |
| `BACKFILL_PROXY_URL` | — | **Required.** This stack's own outbound proxy, separate from the other stacks'. Its hostname must contain `backfill` or the job refuses to start. See VPN / Egress below |
| `DB_USER` | `libex` | PostgreSQL username |
| `DB_NAME` | `libex` | PostgreSQL database name |
| `LOG_RETENTION_DAYS` | `7` | Days of rotated logs to keep. `0` = infinite, no rotation |
| `AXIOM_TOKEN` | — | Axiom API token (optional — leave blank for stdout only) |
| `AXIOM_DATASET` | `libex` | Axiom dataset name |

Log level is fixed at `INFO` in this stack, not operator-configurable — the mechanism that backs off when Audible starts rate-limiting reads its own `WARNING` lines.

### Corpus refresh stack (`docker-compose.refresh.yml`)

Another one-off job: re-fetches every stored book so a fix reaches existing rows, then exits. It ships running `--dry-run` (prints the plan and still reads the database, but never calls Audible) — edit the file to swap in the real command when you're ready to run it. Deploy after the API stack. See **Corpus refresh** under Self-Hosting Notes below.

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PASSWORD` | — | **Required.** The same password the API stack's `DB_PASSWORD` carries |
| `REFRESH_PROXY_URL` | — | **Required.** This stack's own outbound proxy, separate from the other stacks'. Its hostname must contain `refresh` or the job refuses to start. See VPN / Egress below |
| `REFRESH_RESUME_FROM` | — | ASIN to resume from after a stop. A clean stop prints `RESUME CURSOR: <asin>` as its last log line — put that here and redeploy to pick up after it. Blank starts from the beginning |
| `DB_USER` | `libex` | PostgreSQL username |
| `DB_NAME` | `libex` | PostgreSQL database name |
| `CACHE_TTL` | `86400` | Shared with the API stack — both write into the same cache table |
| `LOG_RETENTION_DAYS` | `7` | Days of rotated logs to keep. `0` = infinite, no rotation |
| `AXIOM_TOKEN` | — | Axiom API token (optional — leave blank for stdout only) |
| `AXIOM_DATASET` | `libex` | Axiom dataset name |

Log level is fixed at `INFO` in this stack — the resume cursor is an `INFO` line, and the aborts on sustained rate-limiting or server errors only fire on lines that level lets through. Stop it with `docker stop -t 610 libex-refresh-corpus` (or stop the stack; its `stop_grace_period` is already `610s`) — a stop can spend up to two 300-second write drains, and cutting it short rewinds the resume cursor by a whole page.

### Backup stack (`docker-compose.backup.yml`)

Its own stack, with no VPN — it talks only to Postgres and to your backup destination, never to Audible. Deploy after the API stack. See **Backup** under Self-Hosting Notes below for what it does when no destination is configured.

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PASSWORD` | — | **Required.** The same password the API stack's `DB_PASSWORD` carries |
| `DB_USER` | `libex` | PostgreSQL username |
| `DB_NAME` | `libex` | PostgreSQL database name |
| `BACKUP_TIMEZONE` | `UTC` | IANA timezone name (e.g. `Europe/London`) that `BACKUP_TIME` is local to |
| `BACKUP_PERIOD` | `daily` | `daily`, `weekly`, or `monthly` |
| `BACKUP_TIME` | `03:00` | Time of day to run, `HH:MM`, local to `BACKUP_TIMEZONE` |
| `BACKUP_DAY_OF_WEEK` | `sunday` | Used only when `BACKUP_PERIOD=weekly` |
| `BACKUP_DAY_OF_MONTH` | `1` | Used only when `BACKUP_PERIOD=monthly` |
| `BACKUP_RETENTION_RECENT` | `6` | How many of the most recent dumps to keep |
| `BACKUP_RETENTION_AGED_DAYS` | `30` | Also keeps the newest dump at least this many days old, plus the oldest one not yet this old — so a problem noticed late still has an old-enough copy to recover from. `0` disables both aged tiers |
| `BACKUP_DUMP_TIMEOUT_SECONDS` | `3600` | Ceiling on how long `pg_dump` is allowed to run |
| `BACKUP_DESTINATION_TIMEOUT_SECONDS` | `3600` | Ceiling on how long the upload to the destination is allowed to take |
| `BACKUP_FTPS_HOST` | — | FTPS server. Blank = no destination configured — the container comes up, says so in its log, and waits rather than exiting |
| `BACKUP_FTPS_PORT` | `21` | Explicit FTPS (upgraded with `AUTH TLS`), not implicit FTPS on port 990 — the two are not interchangeable |
| `BACKUP_FTPS_USER` | — | FTPS username |
| `BACKUP_FTPS_PASSWORD` | — | FTPS password |
| `BACKUP_FTPS_PATH` | — | Directory on the server to write dumps into. Required once a host is set — there's no way to mean "the login's default directory" other than setting this to `.` |
| `BACKUP_FTPS_CA_PATH` | `libex-backup-ca` (named volume) | Host directory holding the server's certificate, mounted read-only into the container. Set to a real path (e.g. `./ftps-ca`) if your destination presents a self-signed certificate, which is the common case for a NAS — there is no setting to skip verification |
| `BACKUP_FTPS_CA_BUNDLE` | — | Path to the certificate file inside the container, under the `BACKUP_FTPS_CA_PATH` mount |
| `BACKUP_FTPS_SERVER_HOSTNAME` | — | Set only if the certificate's name doesn't match the address you connect to (e.g. a NAS certificate for `nas.local` reached at its LAN IP) |
| `BACKUP_SPOOL_PATH` | `libex-backup-spool` (named volume) | Working area for one dump on its way to the destination. Change to a host path only if you need it on a particular disk — Docker owns the default volume's permissions for you, a bind-mounted path does not |
| `BACKUP_MEM_LIMIT` | `512m` | Memory ceiling for the backup container. Guards against a large dump being read into memory rather than streamed — raise it if the container is OOM-killed |
| `LOG_RETENTION_DAYS` | `7` | Days of rotated logs to keep. `0` = infinite, no rotation |
| `LOG_LEVEL` | `INFO` | Log verbosity — `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `AXIOM_TOKEN` | — | Axiom API token (optional — leave blank for stdout only) |
| `AXIOM_DATASET` | `libex` | Axiom dataset name |

There is no `BACKUP_ENABLED` — what turns backups on is configuration. Leave the FTPS settings blank and the stack runs with nothing to do; fill in a host, user, password, and path and it starts backing up to schedule. A misspelled `BACKUP_*` name sets nothing and raises no error — the container's startup log line names the destination it actually resolved, so check that after a change.

---

## VPN / Egress

Every stack that talks to Audible — the API, seeder, backfill, and refresh — requires an outbound proxy: `AUDIBLE_PROXY_URL`, `SEEDER_PROXY_URL`, `BACKFILL_PROXY_URL`, and `REFRESH_PROXY_URL` each fail that stack's deploy if left blank, naming the missing variable rather than starting a container that would send Audible traffic out unproxied. Only Audible requests are routed through it — serving, the database, and logging are unaffected. The backup stack has no such requirement; it never talks to Audible.

Any `http://` or `https://` CONNECT-capable proxy works. Each of the four stacks ships an example VPN sidecar (`qmcgaw/gluetun`, WireGuard, pinned to its `v3` release line) as a working default, not as the only option. Three ways to satisfy the requirement:

- **Use the bundled example sidecar** — fill in that stack's six `*_WG_*` values in `.env` (endpoint, keys) from your provider's WireGuard config.
- **Run your own VPN container in the same stack** and point the proxy variable at it, in place of the bundled sidecar.
- **Run a separate VPN stack** and join its container to that stack's egress network (`libex-egress`, `libex-seeder-egress`, `libex-backfill-egress`, or `libex-refresh-egress`) as `external: true`, deployed after the stack that creates that network.

Each egress network is created only by its own stack's compose file, which is why the **API stack deploys first** — it creates `libex-db` and `libex-egress`. A hand-made network with the same name is rejected ("incorrect label com.docker.compose.network"); remove it with `docker network rm <name>` and redeploy the stack that's supposed to own it.

**Bringing your own VPN instead of a bundled sidecar?**

1. It must be an HTTP(S) CONNECT proxy. SOCKS, and anything else, make the stack refuse to start rather than send traffic unproxied.
2. It needs its own kill switch. A plain tinyproxy/privoxy/squid container falls back to the open internet the moment its VPN connection drops, unless it shares the VPN container's network stack (`network_mode: service:<vpn>`). Verify the exit IP once it's up.
3. Percent-encode any `user:pass@` credentials in the proxy URL — and never paste `docker inspect` or `compose config` output anywhere public; both print the URL, credentials included, in plain text.
4. Never publish the proxy's port, and never attach the VPN container to `libex-db` or to the stack's `default` network — egress only.
5. Use a separate exit per stack. The hostname checks (`seeder`, `backfill`, `refresh`) only look at the proxy URL's name, not where it actually connects, so nothing technically stops two stacks sharing an exit — but sharing one defeats the point of separating them.
6. Only trusted containers belong on an egress network. If you run gluetun yourself, stay on `v3.40` or later and never set `HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE` to `{"auth":"none"}`.
7. No TLS-intercepting proxy — Libex validates Audible's certificate itself, and a proxy that intercepts TLS breaks that.
8. Whatever VPN you use, its operator can see which regional Audible host each stack calls, when, and how much data crosses each connection. The path, ASIN, and search terms travel over that same link, but encrypted — the operator can't read them.

---

## Migrating from AudiMeta

Libex is API-compatible with AudiMeta. To migrate:

1. Deploy Libex using the quick start above
2. Update your base URL from your AudiMeta instance to your Libex instance
3. That's it — no other changes required

---

## Self-Hosting Notes

- **PostgreSQL 14 or newer is required.** The compose file above pins `postgres:16-alpine`, so this only concerns you if you're pointing Libex at a database you already run. On an older server chapter writes fail — postgres rejects the jsonb subscript with a type error, not a syntax error — logged as a warning and nothing more: chapters never store, everything else keeps working, and nothing tells you the server is too old
- Libex uses PostgreSQL as both a persistent library and a cache — no Redis required
- Every book, author, series, narrator, and genre ever requested is stored in a full relational schema and survives cache expiry indefinitely
- The local library powers the `/db/book` and `/book/sku/{sku}` endpoints and serves as an automatic fallback when Audible is unavailable
- Cache TTL varies by what is cached, defaulting to `CACHE_TTL` seconds (default 24 hours) unless an endpoint sets its own; expired entries are purged automatically
- Logs directory: `./logs` (relative to your compose file) — Libex writes a rotating log file to `./logs/libex.log` on the host
- Log rotation is daily. `LOG_RETENTION_DAYS=7` keeps 7 days of backups. Set to `0` for infinite retention with no rotation
- **Database seeder:** Off by default, and not part of `docker-compose.yml` at all. It's a separate stack, `docker-compose.seeder.yml`, running its own container (`libex-seeder`) with its own VPN exit — deploy it as its own stack (`docker compose`, Portainer, or similar), after the API stack is up. (Both stacks run the same startup migration, and there's no ordering between separate stacks to prevent two migrations racing each other.) It expands the local DB so the `/db/*` endpoints have more to return, and runs two independent workers in that one container:
  - **Expansion** walks author, series, and narrator relationships to discover books you haven't requested yet. Each cycle compounds — a single book fetch can seed hundreds of related books over time. Runs every `SEEDER_INTERVAL_HOURS` (default 24).
  - **New releases** scans Audible's recent catalog by release date so fresh titles get picked up automatically. It runs on its own worker and its own interval (`SEEDER_NEW_RELEASES_INTERVAL_HOURS`, default 24), so you can have it run more often than the heavier expansion work without waiting behind it. It walks every category in Audible's taxonomy by release date, going as deep as the catalog allows per category.
  - **Release-window refresh** (optional, `SEEDER_REFRESH_ENABLED`, default off) re-fetches a book's details as its release date nears, since things like the date, cover, narrator, and runtime firm up over time — and keeps checking for 30 days after release, on a tapering cadence, since a title's data is still settling in the weeks just after it comes out. It refreshes more often the closer a book is to its release date on either side — roughly yearly when far out, down to daily right around release — before leaving it alone once the 30 days are up. Runs as a second phase of the new-releases worker.

  Both workers share the same regions and rate limit. They run independently and rate-limit themselves to one Audible request per `SEEDER_REQUEST_DELAY` seconds (default 1.0). Configure `SEEDER_REGIONS` to seed multiple markets (e.g. `us,uk,de`). See **Configuration** above for the seeder stack's full environment.

  Requires `SEEDER_PROXY_URL` — its hostname must contain `seeder`, or the seeder refuses to start rather than risk sending sustained, unattended traffic out through the API's own exit IP.

  To turn it off, stop or remove the `docker-compose.seeder.yml` stack. It's a separate stack, so this has no effect on the API.
- **Chapter backfill:** a one-off, not a service — `docker-compose.backfill.yml` fills in chapters for stored books that have none checked yet, then exits. Its restart policy is `"no"` on purpose: a finished run should stay finished rather than restart in a loop. Requires its own `BACKFILL_PROXY_URL`, hostname containing `backfill`. Re-run it by redeploying the stack (`docker compose -f docker-compose.backfill.yml up`).
- **Corpus refresh:** another one-off — `docker-compose.refresh.yml` re-fetches every stored book, for when a fix needs to reach rows Libex already has. It ships running `--dry-run`: it reads the database and prints what it would do, but never calls Audible, until you edit the file to swap in the real command. Requires its own `REFRESH_PROXY_URL`, hostname containing `refresh`. A clean stop can take up to two 300-second write drains, hence its `610s` grace period — cutting a stop short rewinds its resume cursor by a whole page; use `REFRESH_RESUME_FROM` to pick back up after a clean stop.
- **Backup:** its own stack, `docker-compose.backup.yml`, with no VPN exit — it never talks to Audible, only to Postgres and to whatever destination you configure. With no destination configured it comes up, logs that it has nothing to do, and waits, rather than exiting into a restart loop. Configure an FTPS host, user, password, and path and it starts dumping to schedule (daily at 03:00 UTC by default), keeping a recent tier of dumps plus an older one so a problem noticed late still has something to recover from. FTPS here means explicit FTPS on port 21 (upgraded with `AUTH TLS`), not implicit FTPS on port 990, and the server's certificate has to be supplied and is always verified — there's no setting to skip that.
- **VPN proxy:** every stack that calls Audible — API, seeder, backfill, refresh — needs its own outbound proxy, and none of them will start without one. See **VPN / Egress** above for what satisfies that and the warnings that apply if you bring your own.
- **Connection budget:** Postgres is started with `max_connections=200`. The API's 6 workers account for up to 120 of those; the seeder and corpus refresh each add up to 20 more while running, chapter backfill up to 16, and backup none at all (it only shells out to `pg_dump`). Running every stack at once still leaves headroom under 200 — raise `max_connections` in `docker-compose.yml`'s postgres command if you add more Libex stacks against the same database than that.

---

## Upgrading a Self-Hosted Instance

If you deployed Libex before the VPN requirement and the backup/backfill/refresh stacks existed — a single `docker-compose.yml` with an optional blank `AUDIBLE_PROXY_URL` and a bundled `libex-backup` service:

1. Set `AUDIBLE_PROXY_URL` in the API stack's `.env` — either the six `API_WG_*` values for the bundled example sidecar, or your own proxy. The stack now refuses to deploy without it.
2. Redeploy the seeder stack with `SEEDER_PROXY_URL` set the same way (`http://libex-seeder-vpn:8888` with the bundled example sidecar).
3. Backup moved out of `docker-compose.yml` into its own stack. Remove the old `libex-backup` container — `docker rm -f libex-backup`, or redeploy the API stack with `--remove-orphans` — then deploy `docker-compose.backup.yml` on its own.
4. The old deployment's `libex-backup-spool` and `libex-backup-ca` volumes are safe to remove once the new backup stack is running, unless you'd put a certificate directly into the CA volume rather than mounting it from a host path.
5. Relative paths (like `LOGS_PATH`) now resolve against each stack's own compose file rather than a shared project directory — if you relied on several stacks sharing one relative logs path, set the same absolute `LOGS_PATH` in each stack's `.env` instead.

---

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit conventions, and PR requirements.

---

## Disclaimer

Libex is a metadata tool that fetches publicly available information from Audible's API. It does not host, distribute, or provide access to copyrighted audio content. Users are responsible for ensuring their use complies with applicable laws and Audible's terms of service.

---

## Acknowledgements

**Audible** — All metadata is sourced from Audible's public API. Libex is an independent project and is not affiliated with, endorsed by, or sponsored by Audible or Amazon.

**[NarratorList.com](https://narratorlist.com)** — Narrator profile data including biographies, images, languages, and accent ratings is sourced from NarratorList.com. NarratorList is a community-built database where audiobook narrators curate their own profiles. Maintained by Amy Soakes.

**[AussieNarrator.com](https://aussienarrator.com)** — Additional narrator profile data for Australian and New Zealand narrators. A sister site to NarratorList.com, also maintained by Amy Soakes.

**[Axiom](https://axiom.co)** — Structured logging for the public instance. Axiom provides the observability layer that helps us monitor and improve Libex.

**[AudiMeta](https://github.com/Vito0912/AudiMeta)** — The original Audible metadata service that inspired Libex and demonstrated the community need for this tooling. Credit to Vito0912 for pioneering this space.

**[FastAPI](https://fastapi.tiangolo.com)** — The modern Python web framework powering Libex.

**[SQLAlchemy](https://www.sqlalchemy.org)** — Async database toolkit for Python powering Libex's full relational schema across books, authors, series, narrators, genres, and their relationships.

---

## License

MIT — see [LICENSE](LICENSE) for details.