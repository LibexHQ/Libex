<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="app/static/logo-dark.png">
  <img src="app/static/logo.png" alt="Libex" width="400">
</picture>

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://github.com/LibexHQ/Libex/actions/workflows/tests.yml/badge.svg)](https://github.com/LibexHQ/Libex/actions/workflows/tests.yml)
[![GHCR](https://img.shields.io/badge/ghcr.io-libexhq%2Flibex-blue)](https://github.com/LibexHQ/Libex/pkgs/container/libex)
[![Docker Hub](https://img.shields.io/badge/docker%20hub-sunbrolynk%2Flibex-blue)](https://hub.docker.com/r/sunbrolynk/libex)

[![Books](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats&query=%24.books&label=Books&color=orange&cacheSeconds=1800)](https://libexdb.com/db/stats)
[![Books with Chapters](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats&query=%24.booksWithChapters&label=Books%20with%20Chapters&color=b5179e&cacheSeconds=1800)](https://libexdb.com/db/stats)
[![Authors](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats&query=%24.authors&label=Authors&color=teal&cacheSeconds=1800)](https://libexdb.com/db/stats)
[![Narrators](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats&query=%24.narrators&label=Narrators&color=blue&cacheSeconds=1800)](https://libexdb.com/db/stats)
[![Series](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats&query=%24.series&label=Series&color=purple&cacheSeconds=1800)](https://libexdb.com/db/stats)

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
| `us` | United States | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dus&query=%24.books&label=&color=orange&cacheSeconds=1800)](https://libexdb.com/db/stats?region=us) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dus&query=%24.authors&label=&color=teal&cacheSeconds=1800)](https://libexdb.com/db/stats?region=us) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dus&query=%24.series&label=&color=purple&cacheSeconds=1800)](https://libexdb.com/db/stats?region=us) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dus&query=%24.booksWithChapters&label=&color=b5179e&cacheSeconds=1800)](https://libexdb.com/db/stats?region=us) |
| `uk` | United Kingdom | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Duk&query=%24.books&label=&color=orange&cacheSeconds=1800)](https://libexdb.com/db/stats?region=uk) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Duk&query=%24.authors&label=&color=teal&cacheSeconds=1800)](https://libexdb.com/db/stats?region=uk) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Duk&query=%24.series&label=&color=purple&cacheSeconds=1800)](https://libexdb.com/db/stats?region=uk) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Duk&query=%24.booksWithChapters&label=&color=b5179e&cacheSeconds=1800)](https://libexdb.com/db/stats?region=uk) |
| `ca` | Canada | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dca&query=%24.books&label=&color=orange&cacheSeconds=1800)](https://libexdb.com/db/stats?region=ca) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dca&query=%24.authors&label=&color=teal&cacheSeconds=1800)](https://libexdb.com/db/stats?region=ca) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dca&query=%24.series&label=&color=purple&cacheSeconds=1800)](https://libexdb.com/db/stats?region=ca) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dca&query=%24.booksWithChapters&label=&color=b5179e&cacheSeconds=1800)](https://libexdb.com/db/stats?region=ca) |
| `au` | Australia | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dau&query=%24.books&label=&color=orange&cacheSeconds=1800)](https://libexdb.com/db/stats?region=au) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dau&query=%24.authors&label=&color=teal&cacheSeconds=1800)](https://libexdb.com/db/stats?region=au) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dau&query=%24.series&label=&color=purple&cacheSeconds=1800)](https://libexdb.com/db/stats?region=au) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dau&query=%24.booksWithChapters&label=&color=b5179e&cacheSeconds=1800)](https://libexdb.com/db/stats?region=au) |
| `de` | Germany | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dde&query=%24.books&label=&color=orange&cacheSeconds=1800)](https://libexdb.com/db/stats?region=de) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dde&query=%24.authors&label=&color=teal&cacheSeconds=1800)](https://libexdb.com/db/stats?region=de) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dde&query=%24.series&label=&color=purple&cacheSeconds=1800)](https://libexdb.com/db/stats?region=de) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dde&query=%24.booksWithChapters&label=&color=b5179e&cacheSeconds=1800)](https://libexdb.com/db/stats?region=de) |
| `fr` | France | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dfr&query=%24.books&label=&color=orange&cacheSeconds=1800)](https://libexdb.com/db/stats?region=fr) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dfr&query=%24.authors&label=&color=teal&cacheSeconds=1800)](https://libexdb.com/db/stats?region=fr) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dfr&query=%24.series&label=&color=purple&cacheSeconds=1800)](https://libexdb.com/db/stats?region=fr) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Dfr&query=%24.booksWithChapters&label=&color=b5179e&cacheSeconds=1800)](https://libexdb.com/db/stats?region=fr) |
| `it` | Italy | — | — | — | — |
| `es` | Spain | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Des&query=%24.books&label=&color=orange&cacheSeconds=1800)](https://libexdb.com/db/stats?region=es) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Des&query=%24.authors&label=&color=teal&cacheSeconds=1800)](https://libexdb.com/db/stats?region=es) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Des&query=%24.series&label=&color=purple&cacheSeconds=1800)](https://libexdb.com/db/stats?region=es) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Des&query=%24.booksWithChapters&label=&color=b5179e&cacheSeconds=1800)](https://libexdb.com/db/stats?region=es) |
| `jp` | Japan | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Djp&query=%24.books&label=&color=orange&cacheSeconds=1800)](https://libexdb.com/db/stats?region=jp) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Djp&query=%24.authors&label=&color=teal&cacheSeconds=1800)](https://libexdb.com/db/stats?region=jp) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Djp&query=%24.series&label=&color=purple&cacheSeconds=1800)](https://libexdb.com/db/stats?region=jp) | [![](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Flibexdb.com%2Fdb%2Fstats%3Fregion%3Djp&query=%24.booksWithChapters&label=&color=b5179e&cacheSeconds=1800)](https://libexdb.com/db/stats?region=jp) |
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
# Edit .env — DB_PASSWORD is required, all other values have sensible defaults

# 4. Start Libex
docker compose up -d

# 5. Verify
curl http://localhost:3333/health
```

Or copy the compose file directly:

```yaml
services:
  libex:
    image: ghcr.io/libexhq/libex:latest
    container_name: libex
    restart: unless-stopped
    ports:
      - "${PORT:-3333}:3333"
    environment:
      - DATABASE_URL=postgresql+asyncpg://${DB_USER:-libex}:${DB_PASSWORD}@postgres:5432/${DB_NAME:-libex}
      - CACHE_TTL=${CACHE_TTL:-86400}
      - PORT=${PORT:-3333}
      - WEB_CONCURRENCY=6
      - LOG_RETENTION_DAYS=${LOG_RETENTION_DAYS:-7}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    volumes:
      - ./logs:/app/logs
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3333/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

  postgres:
    image: postgres:16-alpine
    container_name: libex-postgres
    restart: unless-stopped
    command: ["postgres", "-c", "max_connections=200"]
    # loopback by default — connect from the same machine, or over an SSH
    # tunnel, or set DB_BIND=0.0.0.0 to reach it from elsewhere.
    ports:
      - "${DB_BIND:-127.0.0.1}:5432:5432"
    environment:
      POSTGRES_DB: ${DB_NAME:-libex}
      POSTGRES_USER: ${DB_USER:-libex}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
    networks:
      - default
      - libex-db
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER:-libex}"]
      interval: 10s
      timeout: 5s
      retries: 5

networks:
  libex-db:
    name: libex-db
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
Logs are retained for 30 days and then automatically deleted by Axiom. The
public instance also sits behind Cloudflare, which sees every request in
order to terminate TLS — including your real IP, which is outside Libex's
control. Axiom and Cloudflare receive this data only to provide those
services; we don't sell your data or hand it to anyone else.

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
| `AUDIBLE_PROXY_URL` | — | Proxy URL for outbound Audible requests only. Supports `http://`, `https://`, `socks5://`. API serving is unaffected |
| `SEED_SECRET` | — | PBKDF2 hash for the internal seed endpoint. Empty = endpoint disabled. Generate with `python -m app.api.routes.internal.router` |

`DATABASE_URL` is constructed automatically by docker-compose from `DB_NAME`, `DB_USER`, and `DB_PASSWORD`. Only set it manually if running outside of Docker — and whatever it points at must be PostgreSQL 14 or newer, see Self-Hosting Notes below.

### Seeder stack (`docker-compose.seeder.yml`)

The seeder is not part of the API stack — it deploys as its own stack, with its own environment, on the **same Docker host** as the API stack (it reaches Postgres over a Docker network the API stack creates, which doesn't cross hosts). See **Database seeder** under Self-Hosting Notes below for what it does.

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PASSWORD` | — | **Required.** The same password the API stack's `DB_PASSWORD` carries |
| `SEEDER_PROXY_URL` | — | **Required.** The seeder's own outbound proxy, separate from the API stack's `AUDIBLE_PROXY_URL` so it never shares the API's exit IP. Its hostname must contain `seeder` or the seeder refuses to start |
| `DB_USER` | `libex` | PostgreSQL username |
| `DB_NAME` | `libex` | PostgreSQL database name |
| `CACHE_TTL` | `86400` | Shared with the API stack — both write into the same cache table |
| `SEEDER_INTERVAL_HOURS` | `24` | Hours between seeder cycles |
| `SEEDER_REQUEST_DELAY` | `1.0` | Seconds between Audible requests during seeding |
| `SEEDER_REGIONS` | `us` | Comma-separated regions to seed (e.g. `us,uk,de`) |
| `SEEDER_NEW_RELEASES_INTERVAL_HOURS` | `24` | Hours between new-releases worker runs |
| `SEEDER_REFRESH_ENABLED` | `false` | Re-fetch upcoming pre-orders as their release date approaches |
| `SEEDER_MEM_LIMIT` | `1g` | Memory ceiling for the seeder container. Sized against its worst-case backlog, not measured against a live run — raise it if the container is repeatedly OOM-killed and restarted rather than assuming a bug |
| `LOG_RETENTION_DAYS` | `7` | Days of rotated logs to keep. `0` = infinite, no rotation |
| `LOG_LEVEL` | `INFO` | Log verbosity — `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `AXIOM_TOKEN` | — | Axiom API token (optional — leave blank for stdout only) |
| `AXIOM_DATASET` | `libex` | Axiom dataset name |

`DB_PASSWORD` and `SEEDER_PROXY_URL` have no default in `docker-compose.seeder.yml` — a missing one fails the stack deploy naming the variable, rather than starting a container that can't connect.

---

## Migrating from AudiMeta

Libex is API-compatible with AudiMeta. To migrate:

1. Deploy Libex using the quick start above
2. Update your base URL from your AudiMeta instance to your Libex instance
3. That's it — no other changes required

---

## Self-Hosting Notes

- **PostgreSQL 14 or newer is required.** The compose file above pins `postgres:16-alpine`, so this only concerns you if you're pointing Libex at a database you already run. On an older server chapter writes fail as a syntax error that's logged as a warning and nothing more — chapters never store, everything else keeps working, and nothing tells you the server is too old
- Libex uses PostgreSQL as both a persistent library and a cache — no Redis required
- Every book, author, series, narrator, and genre ever requested is stored in a full relational schema and survives cache expiry indefinitely
- The local library powers the `/db/book` and `/book/sku/{sku}` endpoints and serves as an automatic fallback when Audible is unavailable
- Cache TTL varies by what is cached, defaulting to `CACHE_TTL` seconds (default 24 hours) unless an endpoint sets its own; expired entries are purged automatically
- Logs directory: `./logs` (relative to your compose file) — Libex writes a rotating log file to `./logs/libex.log` on the host
- Log rotation is daily. `LOG_RETENTION_DAYS=7` keeps 7 days of backups. Set to `0` for infinite retention with no rotation
- **Database seeder:** Off by default, and not part of `docker-compose.yml` at all. It's a separate stack, `docker-compose.seeder.yml`, running its own container (`libex-seeder`) with its own VPN exit — deploy it as its own Portainer stack, after the API stack is up. (Both stacks run the same startup migration, and there's no ordering between separate stacks to prevent two migrations racing each other.) It expands the local DB so the `/db/*` endpoints have more to return, and runs two independent workers in that one container:
  - **Expansion** walks author, series, and narrator relationships to discover books you haven't requested yet. Each cycle compounds — a single book fetch can seed hundreds of related books over time. Runs every `SEEDER_INTERVAL_HOURS` (default 24).
  - **New releases** scans Audible's recent catalog by release date so fresh titles get picked up automatically. It runs on its own worker and its own interval (`SEEDER_NEW_RELEASES_INTERVAL_HOURS`, default 24), so you can have it run more often than the heavier expansion work without waiting behind it. It walks every category in Audible's taxonomy by release date, going as deep as the catalog allows per category.
  - **Upcoming refresh** (optional, `SEEDER_REFRESH_ENABLED`, default off) re-fetches pre-orders you already have as their release date nears, since details like the date, cover, narrator, and runtime firm up over time. It refreshes more often the closer a book gets — roughly yearly when far out, down to daily inside the last two weeks — and leaves already-released books alone. Runs as a second phase of the new-releases worker.

  Both workers share the same regions and rate limit. They run independently and rate-limit themselves to one Audible request per `SEEDER_REQUEST_DELAY` seconds (default 1.0). Configure `SEEDER_REGIONS` to seed multiple markets (e.g. `us,uk,de`). See **Configuration** above for the seeder stack's full environment.

  Requires `SEEDER_PROXY_URL` — its hostname must contain `seeder`, or the seeder refuses to start rather than risk sending sustained, unattended traffic out through the API's own exit IP.

  To turn it off, stop or remove the `docker-compose.seeder.yml` stack. It's a separate stack, so this has no effect on the API.
- **VPN proxy:** Set `AUDIBLE_PROXY_URL` (API stack) or `SEEDER_PROXY_URL` (seeder stack) to route that stack's outbound Audible requests through a proxy. Only Audible requests are affected — API serving, database connections, and logging are unaffected either way, and the two variables are independent so the stacks never share an exit IP. Any HTTP, HTTPS, or SOCKS5 proxy works. Add your VPN proxy container as a service in the same compose file as the stack that needs it — it's reachable there by service name over that stack's own default network, no extra network to create. Leave the variable blank to disable

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