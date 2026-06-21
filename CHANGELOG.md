# Changelog

All notable changes to Libex are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because Libex is a drop-in AudiMeta replacement, the wire format is a hard
contract: new fields, params, and endpoints are additive, and existing
response shapes are never broken or removed. Expect MINOR bumps for new
capabilities and PATCH bumps for fixes — MAJOR bumps should be rare.

## [1.2.0]

### Added

- **Upcoming-refresh seeder phase** (`SEEDER_REFRESH_ENABLED`, default off) — the
  new-releases worker can now re-fetch pre-orders already in the DB as their
  release date approaches, so evolving details (release date, cover, narrator,
  runtime) stay current. Refresh frequency is tiered by proximity to release —
  far-future titles are checked rarely, titles within two weeks are checked
  daily — and already-released books are left alone.

## [1.1.0]

Additive across the board — no existing endpoint, field, or response shape
changed. Everything here is new surface or new background behavior.

### Added

- **Sorting** on every DB list endpoint and the live author, series, and bulk
  book endpoints via `sort` and `order` params (title, releaseDate, rating,
  lengthMinutes, language, publisher, updatedAt).
- **Filtering** across all DB book endpoints (~25 filters), with a useful
  subset (rating, length, language, format, booleans, plan, genre) also
  available on the live book-list endpoints.
- **Genre/category filtering** with partial matching, plus `GET /db/genres`
  to discover the available genre and tag names.
- **Narrator filters** on `GET /db/narrator` — gender, language, source,
  cultural heritage, and audiobooks-produced bucket.
- **`GET /db/new-releases`** — recently released books from the local DB,
  windowed by day range, newest first.
- **`GET /db/coming-soon`** — upcoming books from the local DB, windowed by
  day range, soonest first.
- **`GET /new-releases`** and **`GET /coming-soon`** — live versions scanned
  fresh from Audible, cached until the next UTC midnight and refreshed lazily,
  so they serve the freshest possible list without re-scanning on every
  request.
- **Independent new-releases seeder worker** running on its own interval
  (`SEEDER_NEW_RELEASES_INTERVAL_HOURS`) separate from the main expansion
  cycle, with a configurable scan depth (`SEEDER_NEW_RELEASES_PAGES`).

### Changed

- The new-releases scan now runs as its own seeder worker rather than the last
  phase of the main cycle, so fresh releases are picked up without waiting
  behind author, series, and narrator expansion.

## [1.0.0]

Initial stable release — anonymous, public, drop-in AudiMeta-compatible
Audible metadata API. Book, author, series, narrator, and search endpoints;
local DB query surface; Postgres-backed cache; background seeder.

[1.2.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.2.0
[1.1.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.1.0
[1.0.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.0.0