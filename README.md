<div align="center">

# Libex

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://github.com/LibexHQ/Libex/actions/workflows/tests.yml/badge.svg)](https://github.com/LibexHQ/Libex/actions/workflows/tests.yml)
[![Docker](https://img.shields.io/badge/docker-ghcr.io%2Flibexhq%2Flibex-blue)](https://github.com/LibexHQ/Libex/pkgs/container/libex)

Open, unrestricted Audible metadata API for the audiobook automation community.

</div>

---

## Public Instance

A free public instance of Libex is available at:
```
https://libex.lostcartographer.xyz
```

This instance is maintained by the Libex project and is free for community use. No API key required. No rate limits beyond what Audible naturally enforces.

If you rely on Libex for a project or tool, we recommend self-hosting your own instance for reliability and control.

---

## Why Libex?

The audiobook automation community has long depended on metadata services to power tools like Readarr, Audiobookshelf, and custom managers. When those services disappear or restrict usage, every project depending on them breaks.

Libex exists to be a permanent, community-owned alternative:

- **MIT licensed** — no restrictions, fork it, build on it, use it however you want
- **No usage restrictions** — works with any software, any workflow
- **Drop-in replacement** — compatible with AudiMeta's API endpoints
- **Audible-first** — always fetches fresh data, cache is a fallback not a crutch
- **All regions** — full support for all Audible markets without language restrictions
- **Self-hostable** — one `docker compose up` and you're running

---

## Quick Start
```bash
# 1. Create a directory for Libex
mkdir libex && cd libex

# 2. Download the compose file
curl -O https://raw.githubusercontent.com/LibexHQ/Libex/main/docker-compose.yml

# 3. Create your environment file
cp .env.example .env
# Edit .env and set DB_PASSWORD and DATABASE_URL

# 4. Start Libex
docker compose up -d

# 5. Verify it's running
curl http://localhost:8080/health
```

---

## Logging & Privacy

The public instance uses [Axiom](https://axiom.co) for structured request logging. This is disclosed transparently.

**What is logged:**
- Request path and parameters (e.g. which ASIN was requested, which region)
- Response time and status
- Cache hit/miss
- Errors and exceptions

**What is NOT logged:**
- IP addresses
- User agents
- Any personally identifiable information

**Why we log:**
Logging helps us understand how Libex is being used, identify broken endpoints, debug errors, and improve the service. Without visibility into what's failing, we can't fix it.

**Who can see the logs:**
Only the instance maintainer has access to the Axiom dataset. Logs are retained for 30 days and then automatically deleted by Axiom. No logs are shared with third parties.

**If you self-host:**
Logging is completely optional. Leave `AXIOM_TOKEN` empty and Libex logs to stdout only. Nothing leaves your server.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/book/{asin}` | Get book by ASIN |
| GET | `/book` | Get multiple books by ASIN (comma-separated, max 1000) |
| GET | `/book/{asin}/chapters` | Get chapter information |
| GET | `/author/{asin}` | Get author profile |
| GET | `/author/books/{asin}` | Get all books by author ASIN |
| GET | `/author/books` | Get books by author name |
| GET | `/author/search` | Search authors by name |
| GET | `/series/{asin}` | Get series metadata |
| GET | `/series/books/{asin}` | Get all books in a series |
| GET | `/series/search` | Search series by name |
| GET | `/search` | Search Audible catalog |
| GET | `/quick-search` | Quick search via suggestions |
| GET | `/health` | Health check |

Full interactive documentation available at `/docs` when running.

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
```bash
# Database
DB_PASSWORD=your_secure_password
DATABASE_URL=postgresql+asyncpg://libex:your_secure_password@postgres:5432/libex

# Cache TTL in seconds (default 24 hours)
CACHE_TTL=86400

# Default region
DEFAULT_REGION=us

# Axiom logging (optional - leave empty to use stdout only)
AXIOM_TOKEN=
AXIOM_DATASET=libex
```

---

## Logging

Libex logs to stdout by default. Optionally, structured logging via [Axiom](https://axiom.co) is supported by setting `AXIOM_TOKEN` and `AXIOM_DATASET` in your `.env`.

This is disclosed transparently — if you run a public instance and configure Axiom, your instance's request logs go to your Axiom dataset. No data is sent anywhere by default.

---

## Migrating from AudiMeta

Libex is API-compatible with AudiMeta. To migrate:

1. Deploy Libex using the quick start above
2. Update your base URL from your AudiMeta instance to your Libex instance
3. That's it — no other changes required

---

## Self-Hosting Notes

- Libex uses PostgreSQL for caching — no Redis required
- Cache entries expire after `CACHE_TTL` seconds (default 24 hours)
- Expired entries are purged automatically
- Data directory: `./data` (relative to your compose file)
- Logs directory: `./logs`

---

## Contributing

Contributions are welcome. Fork it, improve it, open a PR.

Planned features:
- Normalized book/author/series database for community dataset
- Request analytics
- Additional metadata sources

---

## Disclaimer

Libex is a metadata tool that fetches publicly available information from Audible's API. It does not host, distribute, or provide access to copyrighted audio content. Users are responsible for ensuring their use complies with applicable laws and Audible's terms of service.

---

## Acknowledgements

**Audible** — All metadata is sourced from Audible's public API. Libex is an independent project and is not affiliated with, endorsed by, or sponsored by Audible or Amazon.

**[Axiom](https://axiom.co)** — Structured logging for the public instance. Axiom provides the observability layer that helps us monitor and improve Libex.

**[AudiMeta](https://github.com/Vito0912/AudiMeta)** — The original Audible metadata service that inspired Libex and demonstrated the community need for this tooling. Credit to Vito0912 for pioneering this space.

**[FastAPI](https://fastapi.tiangolo.com)** — The modern Python web framework powering Libex.

**[SQLAlchemy](https://www.sqlalchemy.org)** — Database toolkit for Python used for cache persistence.

---

## License

MIT — see [LICENSE](LICENSE) for details.