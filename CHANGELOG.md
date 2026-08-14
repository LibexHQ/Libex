# Changelog

All notable changes to Libex are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because Libex is a drop-in AudiMeta replacement, the wire format is a hard
contract: new fields, params, and endpoints are additive, and existing
response shapes are never broken or removed. Expect MINOR bumps for new
capabilities and PATCH bumps for fixes — MAJOR bumps should be rare.

## [1.12.0]

### Added
- **`/db/stats` reports a fifth count, `booksWithChapters`.** It counts books
  that actually have chapter data stored, not books that have merely been
  checked against Audible for chapters — a checked book can still be an
  ISBN-keyed record or a bundle ASIN that will never have chapters, and
  counting those would overstate what Libex actually holds. The README's
  stats badges gain a matching "Books with Chapters" badge.

### Changed
- **`/db/stats` is now cached for 300 seconds.** The endpoint previously ran
  a full, unqualified table count for every stat on every request — public,
  unauthenticated, and hit continuously by shields.io on every README render
  — and the new `booksWithChapters` count above would have added a fifth. A
  caller polling the endpoint may now see counts that don't change for up to
  five minutes; the counts themselves are still exact, never estimated. A
  cache read failure falls back to running the live counts, and a cache
  write failure never fails the request — either way the endpoint degrades
  to its old always-live behaviour rather than erroring.

## [1.11.0]

### Added
- **Author lookups return dramatically more books, including editions in
  every language rather than English only.** `/author/books/{asin}`,
  `/author/{asin}/books`, and the name-only `/author/books?name=` (used
  when no ASIN is available) previously relied on a single English-only
  catalog search by the author's resolved name. Discovery now combines
  several independent sources — Audible's Android author-detail listing,
  several passes over the standard catalog search, and whatever Libex
  already has stored for that author — and no longer filters out
  non-English editions, a filter that had been silently dropping roughly
  490 of Agatha Christie's ~1138 US titles. Measured in the US store,
  Christie's result went from 149 books to 1133. A result is matched to the
  author by Audible's own author id wherever a result carries one, falling
  back to a name match — which also catches an author Audible sometimes
  files a book under a second, alias id — only when it doesn't. List order
  for the ASIN endpoints is still the same release-date order, but an
  ASIN's *position* in that list is not stable across re-fetches: every
  book a caller already had is still guaranteed to come back and to stay in
  the same order relative to the other books it already had, but a
  re-fetch's newly-discovered titles are interleaved wherever their own
  release date puts them, not appended after everything already found — so
  a book that was previously at index 149 can land somewhere else entirely
  once a fuller result includes titles that release between it and its
  neighbours. A caller tracking books by ASIN is unaffected; a caller
  relying on list position is not. Applies to both the primary and legacy
  path forms of the ASIN endpoint.
- **A prolific, multi-genre author's catalog is now also searched by
  category, past a ceiling that turned out not to be about the author at
  all.** Audible caps how many results any single catalog search can page
  through at roughly 500, and that cap applies per search, not per author —
  a different sort order opens a different 500, and scoping the same search
  to one of the author's own genres opens another 500 again. For an author
  whose results plateau against that cap, discovery now also searches
  within whichever categories their own books actually carry. Measured
  against Arthur Conan Doyle, whose catalog Audible itself reports at
  roughly 4501 titles, this brings the result to 4164 books — far more
  complete than a single search could ever reach, but still short of
  Audible's own count, not exhaustive. It costs nothing for an author who
  was already under the cap: Christie's already-thorough result above is
  unchanged, and this never runs at all for a smaller catalog like Brandon
  Sanderson's roughly 200 titles.

### Changed
- **Outbound requests to Audible are now bounded, with author-book requests
  given a much wider allowance than routine background traffic.** There was
  previously no limit at all on how many outbound Audible requests could be
  in flight at once, from any source — a live request's own burst (fetching
  a prolific author's books can mean dozens of requests for one API call)
  competed directly with everything else, including the seeder's steady
  background work, for the same connection. Requests are now bounded, and
  author-book requests draw from a separate, wider lane than routine
  background traffic; five such requests running at once against a large
  catalog now finish in about 5.6 seconds combined, and the seeder's own
  pace is unaffected.
- **A result that couldn't be confirmed complete for a very large author is
  now cached briefly instead of not being cached at all, and two requests
  for the same author arriving at once now share a single lookup instead of
  each running their own.** The cache write for an author's book list is
  now always a merge with whatever's already stored, so it can only grow
  the list, never shrink it — which is what made withholding an incomplete
  result from the cache unnecessary. A confirmed-complete result still gets
  the usual full-day cache lifetime; an incomplete one gets fifteen
  minutes, so it refreshes soon rather than either never being cached or
  being trusted for a full day.
- **The app keeps answering other requests, including `/health`, while a
  large author's book list is being processed.** Turning Audible's raw
  response into Libex's response shape is real CPU work that used to run
  inline on the same thread that serves every request; for a big author
  (well over a thousand books at once) that could occupy the process long
  enough to stall everything else waiting on it. That work now happens on a
  separate thread once a batch is large enough to be worth the hop (roughly
  100+ books at once — every single-book, series, and search lookup, and
  any modest author, is unaffected). This costs the large request itself a
  small amount of extra time — a 1500-book batch takes on the order of
  180ms to process instead of 150ms — in exchange for the rest of the app
  staying responsive throughout.

### Fixed
- **Fetching the book details behind a listing no longer discards a whole
  request over one bad batch.** Book details come back from Audible in
  batches of 50 ASINs. Those batches used to run one after another, and if
  any single one failed — a timeout, a rate limit, a server error — the
  whole request gave up and fell back to whatever was already stored or
  cached, throwing away every book the batches around it had already
  fetched successfully. Batches now run concurrently, and every outbound
  request to Audible retries automatically on a rate-limit or server-error
  response (honoring Audible's `Retry-After` header when it sends one)
  before being treated as failed; a 404 stays terminal and is never
  retried. A batch that still fails after retries is now skipped rather
  than aborting the request, and every book from a batch that did succeed
  is still returned. This also makes fetching a long list of books
  noticeably faster, which matters more now that author lookups can return
  far more books than before.
- **A batch that still fails after retries no longer drops the books it
  already had stored just because other batches in the same request
  succeeded.** The database fallback above already covered a request that
  failed outright, but when only one batch of fifty ASINs failed
  transiently while the rest of a large author's books came back fine (or
  from cache), that batch's ASINs were never checked against the database
  at all — a single upstream timeout or rate limit inside a request for,
  say, 1500 books quietly removed the roughly fifty books that batch owned
  from the response, with nothing to say so. A batch that still fails after
  retries is now checked against the database regardless of how the rest of
  the request went, and whatever's already stored for it is returned
  alongside everything else the request found.
- **A prolific author's book list can now actually earn the same full-day
  cache lifetime as everyone else's, and hold onto it.** Two separate
  defects stood between a very large author and the caching change above.
  First, the completeness check that decides between the full-day and the
  fifteen-minute degraded lifetime treated the discovery wave's plateau —
  paging as far as Audible's own catalog grid goes, the mechanism the
  change above depends on — as an unconfirmed result on every author it
  fired for, so an author whose grid plateaus, like Christie or Doyle,
  could never satisfy "confirmed complete" and sat on the fifteen-minute
  lifetime permanently instead, forcing up to 96 full re-walks a day, each
  hundreds of requests to Audible. Second, even a run that did come back
  complete had its cache write apply its own lifetime outright regardless
  of what was already stored, so one later request that happened to hit a
  single failing page out of hundreds could silently shorten an
  already-earned full-day window back down to fifteen minutes, with
  nothing to ever lengthen it again. Plateauing is now recognized as this
  wave's designed handoff to the other two discovery sources rather than a
  failure, and a cache write now keeps whichever expiry is later — the one
  already stored or the one the current run is asking for — so a degraded
  run can never take back time an earlier, complete run had already
  earned.
- **A category search that turns up new titles on its first page but
  nothing further no longer gets misjudged as a dry category.** The
  category-scoped search added above (see the discovery entry) scores each
  category by how many new titles it turns up, moving on once several
  categories in a row add nothing new — but the score only ever counted a
  category's slower, later pages, not the fast first page fetched to decide
  whether those later pages were worth requesting at all. A category whose
  first page alone already held real new titles, with nothing further on
  the pages after it, was scored as if it had contributed nothing; a
  category whose first page came back with no result-count at all skipped
  its later pages entirely by construction, regardless of what that first
  page held. Both counted as dry, cutting the search short and silently
  truncating results for exactly the prolific, multi-genre authors this
  search exists to serve, with no error raised to say so. The score now
  counts both.
- **`?cache=true` on an author's book list now applies to the whole
  request, not just the first half of it.** The ASIN-based author-books
  endpoints pass the caller's `cache` flag through to discovery, but the
  hydration step that turns those ASINs into full book records ran live
  against Audible regardless of that flag — so a caller explicitly asking
  for a cached result got cached ASINs and then a fully live re-fetch of
  every one of those books' details. The flag is now honored end to end.
- **ASIN format validation now rejects trailing garbage.** The validity check
  matched from the start of the string but stopped at the pattern's `$`
  end-anchor, which treats a trailing newline as "end of string" — so a value
  shaped like a valid ASIN with a stray newline appended could still pass as
  valid and get forwarded to Audible instead of being rejected up front. It
  now requires the entire input to match the ASIN shape with nothing left
  over. Applies everywhere an ASIN is validated — author, book, series, and
  DB routes alike.
## [1.10.5]

### Changed
- **Request logs now record the query string alongside the path.** Only the path
  was recorded before, so there was no way to answer which parameters callers
  actually use — whether anyone passes a region, which sort and filter options
  see real traffic, or whether the caching parameter is used at all. Those
  questions could not be answered even in principle. Nothing sensitive travels
  in a query string here: the internal endpoints authenticate on a header, and
  search terms were already logged separately.

## [1.10.4]

### Fixed
- **`/db/book/{asin}` returned nothing at all for some books.** A book stored
  without any plan data has `plans` recorded as null, and the response model
  requires a list there — as it always has. Rather than a normal error, the
  mismatch aborted the connection before any response was written, so the caller
  saw a gateway timeout rather than anything explaining what went wrong. The
  same path is used as the fallback when Audible is unreachable, so the fallback
  meant to keep things working was failing for exactly those books too. A book
  with no plans now returns an empty list, which is what the documented response
  shape has always promised.

## [1.10.3]

### Fixed
- **Requests queued behind each other under concurrent load.** The database
  connection pool was never configured, so it ran on the library's defaults of
  fifteen connections total — shared by the API, both seeder loops, and every
  background write. Past fifteen simultaneous requests the rest simply waited,
  and measured against the live service twenty concurrent requests to
  `/db/stats` stretched from 1.5 to over 4 seconds purely from queueing. There
  was also no limit on how long a single query could run, so one stuck
  statement held its connection indefinitely with nothing to reclaim it, and
  the pool bled down until the container restarted. The pool is now sized
  explicitly, and both a per-query timeout and an idle-transaction timeout are
  set so a connection can no longer be held forever.

## [1.10.2]

### Fixed
- **The API could stop responding under load, including on `/health`.** Every
  request shipped a log line to Axiom with a blocking network call made on the
  same event loop that serves requests, and that call had no time limit. Libex
  runs a single worker, so while a log line was in flight the API served nobody
  — and because Axiom slows down once its ingest limits are crossed, more
  traffic meant longer stalls, which meant more requests waiting, which meant
  longer stalls again. Past a threshold that tips from "occasionally slow" to
  "timing out constantly" rather than degrading gradually. `/health` was
  affected too despite doing no work of its own, which is the clearest sign the
  process itself was blocked rather than any one endpoint being slow. Log lines
  now go onto a bounded queue that a background thread drains, so a request
  hands its line over and continues; if the queue fills, lines are dropped
  rather than making anyone wait. Log shipping also now gives up properly when
  Axiom is failing instead of retrying on every record forever, and the client
  has a timeout where it previously had none. What gets logged, at what level,
  and with which fields is unchanged.

## [1.10.1]

### Changed
- **Migration notice is more visible, not new.** The README banner now
  renders as GitHub `[!WARNING]`/`[!NOTE]` alerts instead of a plain
  blockquote, and the OpenAPI `description` — shown only when the notice is
  switched on — leads with a `## Migration notice` heading and a `---` rule
  ahead of the normal blurb instead of trailing after it, and now spells out
  "update your configuration" explicitly rather than only implying it. Same
  host, same sunset date, same "stops serving" wording as 1.10.0 — this is a
  presentation pass on an already-shipped notice, not a new one. Self-hosted
  instances are unaffected either way: with the notice off (the default),
  `openapi.json`'s `info.description` is byte-identical to before.

## [1.10.0]

### Added
- **Public-instance migration notice, off by default.** Five new settings
  (`migration_notice_enabled` and friends) control an announcement that the
  public instance is moving from `libex.lostcartographer.xyz` to
  `libexdb.com`. Every one defaults to off/empty, and unless you explicitly
  set them the migration middleware is not even registered. The only wire
  change that ships regardless is described below — a self-hosted instance
  is otherwise byte-identical, `/health` included. If you run your own
  server, nothing changes and there is nothing to do.
- **`Deprecation`, `Sunset`, and `Link` response headers, when the notice is
  enabled.** Follows RFC 9745 and RFC 8594: `Deprecation` marks the old host
  deprecated as of 2026-08-06, `Sunset` gives the date it stops serving —
  2026-11-04 — and `Link` points at the new host (`rel="canonical"`) and, if
  configured, a details page (`rel="deprecation"`). Applied to every response
  served on the old hostname, including an unhandled 500 — the one moment a
  moving consumer most needs the pointer. The same container also answers on
  the new host during the migration window, and a request that arrives there
  gets none of this: no headers, and no `/health` notice below — the new
  host never announces its own retirement to the consumers being asked to
  move onto it. Separately, and regardless of whether the notice is switched
  on at all, `Access-Control-Expose-Headers` now names all three on any
  response that carries an `Origin` header, so a browser can already read
  them once they do start appearing. That naming is the one wire change a
  self-hosted instance can't opt out of; a request without an `Origin`
  header sees no difference at all.
- **`/health` gains an optional `notice` field.** Present when the migration
  notice is enabled and the request arrived on the old hostname; suppressed
  on the new host for the same reason the headers above are. `/health` is
  not part of the AudiMeta DTO contract, so this is a safe additive field —
  unlike the book/author/series responses, an extra key here can't trip up a
  strict deserializer.
- **OpenAPI docs carry the notice when enabled.** The `/docs` description
  gains a migration paragraph, and the OpenAPI `servers` entry points at the
  new host — but only when the notice is switched on. Left unset otherwise, so
  a self-hoster's "Try it out" and any generated SDK still target their own
  server, not the public instance.


## [1.9.0]

### Added
- **`?depth=N` on `/categories`.** Limit how many levels of the taxonomy come
  back: `depth=1` returns just the top-level categories (the parents), `depth=2`
  the top two levels, and so on; omit for the full tree. Works with both the
  nested and flat (`?flat=true`) forms. Useful for pulling the top-level parent
  set live rather than hardcoding it — handy since Audible reorganizes the top
  level from time to time.

### Fixed
- **`/categories` now reconciles the stored taxonomy instead of only adding to
  it.** The store was additive-only, which was correct when Audible *added* a
  category but wrong when Audible *moved* one: the node appeared under its new
  parent while the old placement lingered as a ghost (e.g. a category that's no
  longer top-level still showing at the root). On a complete fetch the stored
  tree is now reconciled to match Audible's current one — added, refreshed, and
  pruned — so a restructure no longer leaves stale entries. Guarded so a
  truncated or failed fetch only adds and never prunes, and the seeder (which
  never wrote the taxonomy table) is unaffected. Clearing `catalog_genres` after
  an Audible reshuffle is no longer necessary.

## [1.8.0]

### Added
- **Flat option on `/categories`.** Pass `?flat=true` to get a flat list instead
  of the nested tree. Every node at every level comes back as a single entry
  carrying its `ancestors` — the {id, name} chain from the top-level root down to
  its immediate parent, in order — so a node's depth and lineage are recoverable
  without walking a tree. A node under more than one parent appears once per
  placement, each with that placement's own ancestry. The default response is
  unchanged (the nested tree).
- **The `category` filter on the `/db/*` book endpoints accepts multiple ids.**
  `?category=` now takes a comma-separated list (e.g. `18580628011,18573212011`)
  and matches a book in any of them (a union). Omitting it still returns every
  category, so the default is unchanged. Applies across all the DB book
  endpoints, since they share the filter. The live `/new-releases` and
  `/coming-soon` stay single-category — their Audible scan can't union without
  walking each category separately.

## [1.7.0]

### Added
- **`/categories` now returns the full taxonomy depth.** It previously stopped at
  two levels (top-level parents and their immediate children); it now mirrors
  Audible's full tree, which runs up to five levels deep and is ragged (some
  branches stop early, some go the full depth). Each node carries its own
  children, so deeply-nested categories — grandchildren and below — are now
  addressable. The ids work anywhere a `category` is accepted: the `/db/*`
  `?category=` filter and the live `/new-releases`/`/coming-soon` scope.
- **The seeder walks every taxonomy level.** The new-releases seeder previously
  walked only parents and leaves; it now walks every node at every level. Each
  level deeper surfaces titles the level above misses (every catalog query caps
  at ~535 results, so a shallower walk leaves most of a branch's books
  unreached), so the deeper walk meaningfully improves catalog coverage. Cycles
  take correspondingly longer.

### Changed
- **`/categories` is always fresh and additive.** It now fetches the taxonomy
  from Audible on every call rather than serving a once-a-day cached copy, and
  stores it additively — new nodes are added, existing ones refreshed, nothing
  is ever removed. The response is the accumulated union, so it never shrinks
  even if a fetch comes back partial, and an Audible hiccup falls back to the
  stored set. This removes the need to clear the category table after an update.

### Fixed
- **Axiom log shipping no longer floods the log on failure.** When the Axiom
  handler couldn't ship a line (a bad or expired token, a network blip), it
  called the default handler error path, which prints a full traceback for every
  log record — turning a misconfigured token into hundreds of tracebacks. It now
  warns once and stays silent, and the other log handlers (stdout, file) are
  unaffected. Axiom is optional and best-effort; a problem shipping to it never
  disrupts the rest of logging.

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