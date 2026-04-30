<div align="center">

# Libex

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://github.com/LibexHQ/Libex/actions/workflows/tests.yml/badge.svg)](https://github.com/LibexHQ/Libex/actions/workflows/tests.yml)
[![GHCR](https://img.shields.io/badge/ghcr.io-libexhq%2Flibex-blue)](https://github.com/LibexHQ/Libex/pkgs/container/libex)
[![Docker Hub](https://img.shields.io/badge/docker%20hub-sunbrolynk%2Flibex-blue)](https://hub.docker.com/r/sunbrolynk/libex)

Open, unrestricted Audible metadata API for the audiobook automation community.

</div>

---

## Public Instance

A free public instance of Libex is available at [Libex](https://libex.lostcartographer.xyz)

This instance is maintained by the Libex project and is free for community use. No API key required. No rate limits beyond what Audible naturally enforces.

If you rely on Libex for a project or tool, we recommend self-hosting your own instance for reliability and control.

---

## Why Libex?

The audiobook automation community has long depended on metadata services to power tools like Readarr, Audiobookshelf, and custom managers. When those services disappear or restrict usage, every project depending on them breaks.

Libex exists to be a permanent, community-owned alternative:

- **MIT licensed** — no restrictions, fork it, build on it, use it however you want
- **No usage restrictions** — works with any software, any workflow
- **Drop-in replacement** — compatible with AudiMeta's API endpoints
- **Audible-first** — always fetches fresh data, the local database is a fallback not a crutch
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
      - CACHE_ENABLED=${CACHE_ENABLED:-true}
      - CACHE_TTL=${CACHE_TTL:-86400}
      - DEFAULT_REGION=${DEFAULT_REGION:-us}
      - PORT=${PORT:-3333}
      - LOG_RETENTION_DAYS=${LOG_RETENTION_DAYS:-7}
      - AXIOM_TOKEN=${AXIOM_TOKEN}
      - AXIOM_DATASET=${AXIOM_DATASET:-libex}
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
    ports:
      - "5432:5432"
    environment:
      POSTGRES_DB: ${DB_NAME:-libex}
      POSTGRES_USER: ${DB_USER:-libex}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER:-libex}"]
      interval: 10s
      timeout: 5s
      retries: 5
```

---

## Logging & Privacy

The public instance uses [Axiom](https://axiom.co) for structured request logging. This is disclosed transparently.

**What is logged:**
- Request path and parameters (e.g. which ASIN was requested, which region)
- Response time and status code
- IP address
- User agent
- Cache hit/miss
- Errors and exceptions

**What is NOT logged:**
- Any personally identifiable information beyond the above

**Why we log:**
Logging helps us understand how Libex is being used, identify broken endpoints, debug errors, and improve the service. Without visibility into what's failing, we can't fix it.

**Who can see the logs:**
Only the instance maintainer has access to the Axiom dataset. Logs are retained for 30 days and then automatically deleted by Axiom. No logs are shared with third parties.

**If you self-host:**
Logging is completely optional. Leave `AXIOM_TOKEN` empty and Libex logs to stdout only. Nothing leaves your server.

---

## API Behavior

**HTML content:** `description` and `summary` fields on book responses, `description` on author responses, and `description` on series responses are returned as plain text with HTML stripped.

**Image URLs:** Cover image URLs are returned with Audible size suffixes stripped, giving you the base high-resolution image URL.

**ASIN validation:** All ASIN parameters are validated against Audible's 10-character alphanumeric format. Invalid ASINs return a 404 with a clear error message.

**Region validation:** All region parameters are validated against supported Audible regions. Invalid regions return a 400 error.

**Local database:** Every successful Audible response is written to a persistent relational database. This powers the DB query endpoints and serves as a fallback when Audible is unavailable.

**Audible Plus fields:** Book responses include `isVvab` (boolean indicating Audible Plus availability) and `plans` (list of Audible plan names such as `"US Minerva"` or `"AccessViaMusic"`). These let clients determine subscription availability programmatically.

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
| GET | `/author/{asin}/books` | Get all books by author ASIN |
| GET | `/author/books/{asin}` | Get all books by author ASIN (legacy) |
| GET | `/author/books` | Get books by author name |
| GET | `/author` | Search authors by name |
| GET | `/series/{asin}` | Get series metadata |
| GET | `/series/{asin}/books` | Get all books in a series |
| GET | `/series/books/{asin}` | Get all books in a series (legacy) |
| GET | `/series` | Search series by name |
| GET | `/search` | Search Audible catalog |
| GET | `/quick-search` | Quick search via suggestions |
| GET | `/{region}/search` | Regional search for Audiobookshelf compatibility |
| GET | `/{region}/quick-search/search` | Regional quick search for Audiobookshelf compatibility |
| GET | `/db/book` | Query the local indexed book library |
| GET | `/db/book/{asin}` | Get a single book from local DB |
| GET | `/db/book/{asin}/chapters` | Get chapter data from local DB |
| GET | `/db/book/sku/{sku}` | Get books by SKU group from local DB |
| GET | `/db/author/{asin}` | Get author from local DB |
| GET | `/db/author/{asin}/books` | Get author's books from local DB |
| GET | `/db/series/{asin}` | Get series from local DB |
| GET | `/db/series/{asin}/books` | Get series books from local DB |
| GET | `/health` | Health check |

Full interactive documentation available at `/docs` when running.

---

## DB Query Endpoint

`GET /db/book` queries books that have been fetched and stored locally without hitting Audible. Useful for searching your indexed library by metadata.

All parameters are optional but at least one filter must be provided. Supports pagination via `limit` (default 20, max 100) and `page` (default 1).

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

---

## Supported Regions

| Code | Region |
|------|--------|
| `us` | United States |
| `uk` | United Kingdom |
| `ca` | Canada |
| `au` | Australia |
| `de` | Germany |
| `fr` | France |
| `it` | Italy |
| `es` | Spain |
| `jp` | Japan |
| `in` | India |
| `br` | Brazil |

---

## Configuration

Copy `.env.example` to `.env` and configure:

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PASSWORD` | — | **Required.** PostgreSQL password |
| `DB_NAME` | `libex` | PostgreSQL database name |
| `DB_USER` | `libex` | PostgreSQL username |
| `PORT` | `3333` | Host port the API is exposed on |
| `DEFAULT_REGION` | `us` | Default Audible region |
| `CACHE_ENABLED` | `true` | Enable or disable the cache |
| `CACHE_TTL` | `86400` | Cache TTL in seconds (default 24 hours) |
| `LOG_RETENTION_DAYS` | `7` | Days of rotated logs to keep. `0` = infinite, no rotation |
| `AXIOM_TOKEN` | — | Axiom API token (optional — leave blank for stdout only) |
| `AXIOM_DATASET` | `libex` | Axiom dataset name |

`DATABASE_URL` is constructed automatically by docker-compose from `DB_NAME`, `DB_USER`, and `DB_PASSWORD`. Only set it manually if running outside of Docker.

---

## Migrating from AudiMeta

Libex is API-compatible with AudiMeta. To migrate:

1. Deploy Libex using the quick start above
2. Update your base URL from your AudiMeta instance to your Libex instance
3. That's it — no other changes required

---

## Self-Hosting Notes

- Libex uses PostgreSQL as both a persistent library and a cache — no Redis required
- Every book, author, series, narrator, and genre ever requested is stored in a full relational schema and survives cache expiry indefinitely
- The local library powers the `/db/book` and `/book/sku/{sku}` endpoints and serves as an automatic fallback when Audible is unavailable
- Cache entries expire after `CACHE_TTL` seconds (default 24 hours); expired entries are purged automatically
- Logs directory: `./logs` (relative to your compose file) — Libex writes a rotating log file to `./logs/libex.log` on the host
- Log rotation is daily. `LOG_RETENTION_DAYS=7` keeps 7 days of backups. Set to `0` for infinite retention with no rotation

---

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit conventions, and PR requirements.

---

## Disclaimer

Libex is a metadata tool that fetches publicly available information from Audible's API. It does not host, distribute, or provide access to copyrighted audio content. Users are responsible for ensuring their use complies with applicable laws and Audible's terms of service.

---

## Acknowledgements

**Audible** — All metadata is sourced from Audible's public API. Libex is an independent project and is not affiliated with, endorsed by, or sponsored by Audible or Amazon.

**[Axiom](https://axiom.co)** — Structured logging for the public instance. Axiom provides the observability layer that helps us monitor and improve Libex.

**[AudiMeta](https://github.com/Vito0912/AudiMeta)** — The original Audible metadata service that inspired Libex and demonstrated the community need for this tooling. Credit to Vito0912 for pioneering this space.

**[FastAPI](https://fastapi.tiangolo.com)** — The modern Python web framework powering Libex.

**[SQLAlchemy](https://www.sqlalchemy.org)** — Async database toolkit for Python powering Libex's full relational schema across books, authors, series, narrators, genres, and their relationships.

---

## License

MIT — see [LICENSE](LICENSE) for details.