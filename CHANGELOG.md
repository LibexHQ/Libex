# Changelog

All notable changes to Libex are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because Libex is a drop-in AudiMeta replacement, the wire format is a hard
contract: new fields, params, and endpoints are additive, and existing
response shapes are never broken or removed. Expect MINOR bumps for new
capabilities and PATCH bumps for fixes — MAJOR bumps should be rare.

## [1.6.0]

### Added

- **`category` filter on the DB book endpoints.** `/db/book` (and the other
  `/db/*` book-list endpoints) now accept `?category=<id>` — an exact match on
  an Audible category id from `/categories`. This complements the existing
  `genre` filter, which matches genre/tag *names* broadly: use `category` for an
  exact id, `genre` for a partial name. The ids are the same taxonomy `/categories`
  exposes and the live `/new-releases`/`/coming-soon` endpoints scope by.

### Fixed

- **The new-releases seeder now persists at scale.** The genre-union scan finds
  tens of thousands of ASINs, and the missing-books check passed them all to a
  single `IN` query — which exceeds PostgreSQL's 32,767 bind-parameter limit, so
  the persist step failed and the scan wrote nothing (reported as `0 new` despite
  finding ~89k). The query is now chunked, so the scan persists everything it
  finds.

## [1.5.0]

### Fixed

- **`/new-releases` and `/coming-soon` no longer time out.** The 1.4.0 live scan
  walked every genre on each request to assemble the full catalog, which on a
  real catalog takes minutes — long enough that the request timed out at the
  gateway before returning. The live endpoints now scan a single catalog query
  per request (see Changed), so they return promptly.
- **The new-releases seeder no longer aborts on a single Audible hiccup.** One
  failed catalog request used to stop the entire scan and discard everything it
  had collected (reported as `0 new`). Each category is now walked
  independently — a failed one is logged and skipped, and the rest of the scan
  (and the books already found) is kept.
- **The seeder was undercounting.** It walked only leaf categories, but a parent
  category surfaces titles that none of its children do, so parent-only releases
  were being missed. The seeder now walks parents and leaves and unions the
  results, covering the full set.
- **Audible request failures are now diagnosable.** Some failures logged a blank
  reason (`Audible API request failed:` with nothing after it); the message now
  includes the error type and the URL.

### Added

- **`GET /categories`** — lists Audible's genre categories for a region as a
  nested tree of parents and their leaves. These are the ids you pass to the new
  `category` parameter. This is the Audible *category* taxonomy, distinct from
  `/db/genres` (the genre/tag *names* attached to stored books).
- **`category` parameter on `/new-releases` and `/coming-soon`** — scope the live
  scan to a single category (an id from `/categories`) and get the full window
  for it.

### Changed

- **The live `/new-releases` and `/coming-soon` are now single-scan.** With a
  `category`, the scan covers that one category in full. Without one, it walks
  Audible's un-categoried catalog, which Audible caps at a few hundred results —
  so the bare call returns a live *sample*, not the whole catalog. For the
  complete set, query a category, use the DB endpoints `/db/new-releases` and
  `/db/coming-soon` (kept current by the seeder), or aggregate per-category calls
  client-side.

## [1.4.0]

### Fixed

- **`/new-releases` and `/coming-soon` now return the full list.** Audible
  exposes no direct new-releases or coming-soon feed, and any single catalog
  query — even filtered to one category — caps out around 535 results, so the
  old scan could only ever surface a fraction of the window (in practice, just a
  handful of titles). Both live endpoints, and the new-releases seeder, now fan
  out across every genre's sub-categories, walk each by release date, and union
  the results — reconstructing the same set Audible's own new-releases and
  coming-soon pages show. The `days` window, the midnight caching, and the
  response shape are all unchanged. Note for self-hosters running the seeder:
  the wider scan makes more Audible requests per cycle and grows the local DB
  noticeably faster than before.
- **Date-sorted catalog reads were silently unsorted.** The catalog search used
  the wrong sort parameter, so requests meant to come back newest-first were
  returned in Audible's default order. This affected the release-window scans
  and `GET /author/books?name=` (books by author name). Now correctly sorted by
  release date.

### Changed

- **The new-releases seeder worker now collects everything it can reach.**
  Instead of a fixed-depth, recent-window scan, it walks every genre's
  sub-categories and ingests all reachable titles — upcoming pre-orders and
  recent releases alike — so both `/db/new-releases` and `/db/coming-soon` fill
  out from the same pass. It stays paced by `SEEDER_REQUEST_DELAY` and runs on
  its own `SEEDER_NEW_RELEASES_INTERVAL_HOURS` interval; the optional
  upcoming-refresh phase (`SEEDER_REFRESH_ENABLED`) is unchanged.

### Added

- **`catalog_genres` table.** A small table holding Audible's per-region genre
  list, used by the live release endpoints to avoid re-fetching the taxonomy on
  every scan and refreshed automatically about once a day. Created for you by
  the startup migration — no action needed.

### Removed

- **`SEEDER_NEW_RELEASES_PAGES`** and **`SEEDER_NEW_RELEASES_DAYS`** are retired.
  The new-releases scan now walks each genre to its catalog limit rather than a
  fixed page depth or day window, so neither knob applies. If either is still
  set, Libex logs a one-time warning at startup and ignores it — safe to remove
  from your environment.

## [1.3.0]

### Added

- **`LOG_LEVEL`** environment variable (default `INFO`) for granular log
  verbosity — `DEBUG`, `INFO`, `WARNING`, or `ERROR`.

### Changed

- Log lines now render their structured context inline (e.g. a seeder scan
  shows `... — 1000 found, 0 new, 20 pages scanned`), so what a task did is
  visible in stdout and file logs instead of only in Axiom.
- Warnings and errors now go to stderr; informational logs stay on stdout, so
  the two can be filtered separately.

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

[1.6.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.6.0
[1.5.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.5.0
[1.4.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.4.0
[1.3.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.3.0
[1.2.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.2.0
[1.1.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.1.0
[1.0.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.0.0