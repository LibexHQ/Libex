# Changelog

All notable changes to Libex are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because Libex is a drop-in AudiMeta replacement, the wire format is a hard
contract: new fields, params, and endpoints are additive, and existing
response shapes are never broken or removed. Expect MINOR bumps for new
capabilities and PATCH bumps for fixes — MAJOR bumps should be rare.

## [1.15.1]

### Changed
- **`/author/books/{asin}` makes fewer upstream requests for the same books.**
  Building an author's title list walks their catalogue one category at a
  time, and each category was then re-walked under four more sort orders to
  dig past the depth limit a single sort can reach. That limit applies per
  sort *and* category, so a category holding fewer titles than the limit was
  already read in full on the first pass — the four extra walks could only
  return the same titles in a different order. Those categories are now
  skipped, cutting up to four requests each.

  The set of books returned is unchanged. A category large enough to actually
  hit the limit is still walked under every sort, and a category whose first
  pass came back short — a page that failed to load, one the batch never
  returned, or one whose body came back unusable — is treated as unread and
  walked in full rather than assumed finished, rather than trusting a total
  that was recorded before the shortfall happened. Fewer requests means more
  authors finish inside the request's time budget, and a category the probe
  had already read in full is no longer wrongly counted as owed work when
  judging whether the request ran out of time — between the two,
  marked-incomplete responses should get rarer.

  **What this does not cover.** The one page this doesn't retry is a
  category's very first probe page, the one that decides whether the category
  is worth a second look at all — if that page itself fails to load or comes
  back unusable, the category is treated as having nothing new, the same as
  it would be if it genuinely had nothing, and it is not walked further. No
  endpoint, parameter, response shape or field changed.

### Fixed
- **A slow `/author/books/{asin}` request now returns what it has instead of
  timing out with nothing.** The endpoint had a time budget, but it was set
  longer than the gateway in front of it waits — so it could never actually
  take effect, and a request that ran long was abandoned by the gateway before
  the budget noticed. Worse, the budget only ever covered the first half of the
  work (finding the author's titles); fetching the books themselves was
  unbounded, so the real worst case was the budget plus however long that took.

  Both halves now share one budget for the whole request, set inside the
  gateway's window. A request that reaches it returns the books it managed to
  fetch, marked `X-Libex-Complete: false` and `Cache-Control: no-store`, rather
  than failing outright. Books it could not fetch in time are served from
  Libex's own stored copy where one exists.

  **What this does not do.** If the shortfall was in fetching the books rather
  than in finding them, nothing re-fetches the missing ones afterwards — the
  author's title list is still completed in the background, so the next
  request starts from a warm list, but it attempts the books live again and
  can fall short again. An author whose catalogue is simply too large to
  hydrate inside the window will keep returning marked-incomplete until that
  work is reduced. The legacy `/author/{asin}/books` route changes
  identically. No endpoint, parameter, response shape or field changed.
- **The bundled compose file now passes through the Audible proxy and Axiom
  logging settings it has always documented.** `AUDIBLE_PROXY_URL`,
  `AXIOM_TOKEN` and `AXIOM_DATASET` have long been in `.env.example` and the
  README, and the app reads all three — but `docker-compose.yml` never named
  any of them in the service's `environment:` block, and Compose does not
  inject a host `.env` value into the container unless it is named there.
  Setting any of the three in `.env` for a deployment run from this repo's
  compose file as-is had no effect: Audible traffic left by the container's
  own address instead of the configured proxy, and nothing shipped to Axiom no
  matter what token was set. All three now pass through, defaulting to the
  same off/empty behaviour as an unset value always had. This is a deployment
  fix only — no endpoint, parameter or response changed, and it has no effect
  on a deployment that already sets these at the stack or environment level
  rather than through this file.

## [1.15.0]

### Changed
- **A book's `isListenable`, `isAvailable` and `isBuyable` now read `true`
  when Audible does not say otherwise.** Previously a response that simply
  omitted the field was recorded and returned as `false`, which claimed a
  book was not listenable on no evidence at all. Silence is now carried as
  "unknown" through the write path and resolved to `true` on the way out,
  matching how the upstream catalogue is read elsewhere. This affects
  `/book`, `/search`, `/new-releases` and `/coming-soon`.

  **One caveat worth knowing if you consume these fields.** Books already
  stored before this release kept whatever the old rule wrote, and a later
  response that omits the field no longer overwrites what is stored — so a
  book recorded as `false` under the old rule stays `false` until Audible
  states otherwise, even though a live lookup of the same book now reports
  `true`. Nothing rewrites those rows.
- **The `category` parameter on `/new-releases` and `/coming-soon` now
  rejects anything that is not a number.** A non-numeric value previously
  travelled on into the cache key and the upstream request; it now returns
  `422`.
- **Writing a batch of freshly fetched books issues far fewer statements per
  book.** 1.13.4 already grouped a background write into transactions of
  fifty books instead of one; within each of those transactions, every book
  still ran its own insert, its own genre and narrator statements, and so on.
  Books in a transaction are now written with one statement per kind of row —
  one for all fifty book rows, one for their genres, one for their pivots —
  instead of one of each per book, cutting a prolific author's catalog from
  roughly ten statements a book to close to one. Every write is still the
  same upsert with the same merge rules; this only changes how many
  statements carry them.
- **A background write that fails from database contention is now retried
  before falling back to writing the batch one book at a time.** A lock
  conflict with the seeder or a dropped connection used to send the whole
  transaction straight to the slow per-book path. It now gets up to two more
  attempts, each in a fresh transaction and spaced out with a short,
  randomized delay so that several workers backing off the same conflict
  don't collide again at the same moment. Only failures a retry can plausibly
  fix are retried — a lock conflict or a dropped connection, not a value the
  schema rejects — so a genuinely bad book still falls through to the
  per-book path on the first attempt.
- **The backlog of books waiting to be written in the background is now
  capped, and a full backlog is shed rather than left to grow.** Previously
  nothing bounded how many background writes could be queued at once,
  including while Postgres itself is unavailable, so a sustained outage could
  build an unbounded amount of queued work in memory. Once roughly 5,000
  books' worth is queued or in flight, a further batch is skipped instead of
  queued, and a warning is logged summarizing how many books and how many
  batches were shed. A shed book is not lost: every request still gets its
  answer, and the book is written on the next request that fetches it. This
  only engages while the database is degraded; ordinary traffic never
  approaches the cap.
- **Complete author-books responses can now be cached by a CDN, and expire
  there at the same moment Libex's own copy does.** Libex previously sent no
  caching instructions at all, so a cache sitting in front of it had nothing to
  act on and forwarded every request. A complete response now carries
  `Cache-Control: public, max-age=300, s-maxage=<seconds>`, where the shared
  figure is the exact time remaining on Libex's own stored copy — so the two
  lapse together rather than the CDN holding an answer after Libex has moved
  on. A response assembled by a fresh walk gets a short five-minute shared TTL
  instead: its copy is stored in the background and may not have been written
  yet, so there is no remaining life that can honestly be quoted. Incomplete
  responses continue to carry `no-store` and are never cached anywhere.

- **A partial author-books result now says so, and is never cached as though
  it were complete.** An author's catalogue is assembled by walking Audible
  across several sources, and for a very prolific author that walk can run out
  of time before it finishes. Previously the shortened list was returned with a
  plain `200` and no indication that anything was missing, and it was written to
  the cache — so once the cache began serving the default path, a partial answer
  could be handed to everyone for up to fifteen minutes with nothing saying it
  was partial.

  Three things change. Every successful response now carries an
  `X-Libex-Complete` header, `true` or `false`, so a caller can tell. It
  describes the books actually returned, not merely whether the catalogue
  walk finished — a request whose books could not all be fetched is reported
  incomplete even when the list of them was whole. An incomplete response also carries
  `Cache-Control: no-store`, so a cache in front of Libex cannot hold a partial
  answer and serve it on Libex's behalf. And an unfinished walk is no longer
  written to the cache at all — instead it is finished in the background, with
  a time budget no caller is waiting on, and the **complete** result is what
  gets stored. The next request for that author is then served a whole
  catalogue rather than the shortened one.

  The status code stays `200` and the response body is unchanged — still the
  same list of books, in the same shape, with the same fields. A client that
  ignores the new header behaves exactly as it did before. If you rely on
  receiving a complete catalogue, check `X-Libex-Complete`.

  One case is deliberately left expensive rather than made quietly wrong: if the
  background walk cannot finish either, after two attempts that author is simply
  not cached, and every request re-walks and returns a marked-incomplete result.
  Storing the partial would be cheaper but would mean a cached answer could no
  longer be trusted to be complete, which is the guarantee the header exists to
  make.
- **`/author/books/{asin}` now serves cached results by default.** The `cache`
  query parameter defaulted to `false`, so the 24-hour cache only ever served
  callers who explicitly passed `?cache=true` — which meant there was no such
  thing as a warm request on the default path. Every request without the flag
  walked Audible in full, and for a prolific author that walk runs to hundreds
  of upstream requests and frequently exceeded the proxy's timeout, returning a
  504. The result was already being written to the cache on every walk;
  only the read was switched off, so it was being stored for almost nobody.
  The default is now `true`: a request that does not name the parameter is
  served from cache when a fresh entry exists, and still performs the full live
  walk when one does not. `?cache=false` is unchanged and still forces a fresh
  walk — and such a response is now also marked `no-store`, so a cache
  between you and Libex cannot answer the next identical request on its
  behalf and quietly undo the very thing the flag asked for.

  **What this changes for callers.** No endpoint, parameter, response shape or
  field moved — the difference is the freshness of what you get when you do not
  pass the flag. A cached answer is at most 24 hours old, and is always a
  complete catalogue — an unfinished walk is never cached at all, as described
  in the entry above. A cache miss is unaffected and still returns live data.
  If you require a guaranteed-live answer on every request, pass `?cache=false`
  explicitly. The legacy `/author/{asin}/books` route changes identically.
  `/author/{asin}`, which returns a single author profile rather than walking a
  catalog, is deliberately unchanged.
- **The expired-cache purge stops on a batch that reports no row count, not
  only on one that reports zero.** The purge deletes in batches and ends the
  pass when a batch removes nothing. A database driver is not obliged to
  report how many rows a statement touched — the convention for "unavailable"
  is `-1` — and an exit written against zero alone would never fire against
  that, leaving a background worker issuing deletes against a table it had
  already emptied. The driver Libex uses reports these counts faithfully, so
  nothing was doing this; the guard is against a future driver or dialect
  change rather than an observed fault.

### Fixed
- **A book's VVAB (virtual voice audiobook) status was never being saved.**
  `isVvab` has had a column, a filter and an `/db/vvab` endpoint for months,
  but nothing in the write path actually read it from Audible's response — a
  new row was always inserted with it false, and an existing row's value was
  never touched on a later write. It is now merged the same careful way as
  the other flags below, and a fresh write correctly sets it from Audible's
  answer. This does not repair anything already stored: every book written
  before this release still reads false regardless of its real status, and
  nothing here rewrites those rows. The database only sees a book again when
  something asks for it, and the seeder never revisits a released title once
  it has one, so `/db/vvab` will keep under-reporting until each affected book
  happens to be requested again.
- **An explicit `null` for `isListenable` or `isBuyable` was saved differently
  depending on whether the book was new or already stored.** Inserting a book
  with the field explicitly null wrote `true`; updating one with the same
  input wrote `false`, because the two paths read the missing value through
  different defaults. Both now read it the same way: silence keeps whatever
  is already stored, and an explicit `true` or `false` from Audible always
  overwrites.

### Security
- **A locally built image no longer copies the whole working directory into
  itself.** There was no `.dockerignore`, so `COPY . .` swept in whatever
  happened to sit beside the source — including `.env`. Published images were
  never affected, because CI builds from a clean checkout where those files do
  not exist, but that was luck rather than design. The build now admits only
  what the image actually needs.
- **Every dependency in the published image is now pinned and cryptographically
  verified at install time.** The image and CI previously installed from a list
  that pinned Libex's own direct dependencies by version but left everything
  those pull in — the large majority of what actually ends up installed —
  free to resolve to whatever the index offered at build time, with nothing
  checking that what arrived was what the maintainers published. Both now
  install from a generated lock that pins every package, direct and indirect,
  to one version and verifies each against recorded SHA-256 hashes; a
  substituted or altered archive fails the build instead of shipping. This
  matches how the base image (pinned by digest) and the bundled documentation
  assets (checksum-verified) were already handled. No dependency changed
  version as part of this — the lock records what was already resolving.
- **The test runners no longer ship inside the published image.** `pytest` and
  its plugins were declared alongside the application's own dependencies, so
  they — and the packages they pull in — were installed into the image that
  runs in production, which has no tests to run. They now live with the rest
  of the development tooling and are installed only where tests actually
  execute. Six packages left the image; nothing the application imports at
  runtime changed.
- **The tools CI uses to lint and audit are now pinned and verified too, and
  run isolated from the application.** The audit tool in particular was
  previously installed unpinned and unverified — the one step responsible for
  reporting known vulnerabilities was the least protected install in the
  build, and an unpinned version also meant an upstream release could fail
  every branch with nothing in the repository having changed. It is now
  version-pinned and hash-verified like everything else, and installed into
  an environment of its own: its own dependencies overlap the application's,
  so sharing an environment let it quietly replace packages that had just
  been verified, and then audit the result rather than what actually ships.
  The audit now names the locks directly, so what is checked is exactly what
  is shipped.


## [1.14.0]

Nothing here changes the API itself: no endpoint, parameter, response shape,
field or status code moved, and every request returns exactly what it did
before. What changed is how Libex runs, what it logs, how much of the machine
it asks for, and — the part worth reading before upgrading if you share an
outbound address — how much it asks of Audible at once.

### Added
- **Libex can now serve requests from several worker processes instead of
  one.** A new `WEB_CONCURRENCY` setting controls how many, and the bundled
  compose file sets it to `6`; `1` restores the previous single-process
  behaviour exactly, from the stack's environment alone with no rebuild. This
  answers a specific fault. Libex handles every request on a single event
  loop, and when one request held that loop long enough, everything behind it
  waited. `/health` showed it most plainly: it reads no database, no cache and
  nothing over the network, so it has nothing of its own to be slow about, and
  it was seen taking more than twenty seconds — past the thirty the reverse
  proxy in front of it waits before giving up. Both public hostnames were
  reported down in the same moment while the container stayed up and was never
  restarted, and it recurred day after day. With more than one worker, all of
  them accept from the same listening socket, so a worker whose loop is
  blocked stops accepting and the others take the new connections.

  **What this does not do is prevent that, or recover from it.** Requests
  already accepted by a blocked worker hang exactly as they did before, and
  nothing brings that worker back: the supervisor asks each worker whether it
  is alive on a separate thread which goes on answering while the loop is
  stuck, so a wedged worker is never replaced, and the container's health
  check is answered by whichever worker is still healthy. The underlying fault
  happens at the same rate it always did. Fewer callers meet it.

  Raising this above `6` is not free, and neither is leaving it there: the
  worker count multiplies what Libex can have in flight at Audible, so read
  the note on outbound requests below before you upgrade. Leave it at `1`
  while the seeder is
  enabled: the seeder starts inside every worker and nothing coordinates them,
  so six workers walk the same books six times, which is sustained unattended
  traffic from your address for no extra coverage.

  Both the worker count and the raised Postgres connection limit it needs live
  in the bundled compose file. If you run Libex's image from a stack or
  compose file of your own, upgrading gives you neither until you set them
  there: the image passes no worker count of its own, so an unset
  `WEB_CONCURRENCY` means one worker and everything below reads as it did
  before.

- **Every log line and Axiom event now records the process id of the worker
  that produced it.** With one process this was noise; with several it is the
  only way to tell one worker repeatedly in trouble from several occasionally
  in trouble, which are indistinguishable once the lines are merged. The value
  belongs to the server, is identical for every request a given worker
  handles, and says nothing about who sent any of them. Anything parsing the
  plain-text logs should note where it sits: lines now read
  `... - INFO - pid=7 - Request completed` where they previously ran straight
  from the level into the message.

- **A slow `/health` now logs a warning.** `/health` was not logged at all, on
  the reasoning that a check a minute is a day of lines saying nothing. But
  because it does no work of its own, how long it takes is a direct reading of
  whether the event loop is keeping up, and that reading was being discarded.
  A single line is now written at warning level when a health check takes a
  second or longer — high enough above ordinary scheduling jitter to cost no
  false lines, and far below the point at which any caller notices, so a
  healthy process still logs nothing for this path. The line carries the path,
  the status and the duration and nothing else — no user agent, no host header,
  no query string: `/health` takes no input, so there is nothing a caller
  supplied to record.

### Changed
- **Schema migrations now run once before the workers start, rather than
  during application startup.** They are applied by the container's entry
  point, immediately before the server is started. Application startup ran
  them previously, which was safe while there was one process and would not
  have been with six: startup runs once in every worker, and alembic
  serialises nothing of its own — the installed version issues no advisory
  lock, no table lock and no locking select anywhere — so six workers would
  have raced the same schema changes, with the losers failing on objects the
  winner had already created. Behaviour on failure is deliberately unchanged:
  a start against a database that is not reachable yet still boots and serves
  rather than restart-looping. The one visible difference is where that
  failure is reported — it now appears on the container's own output instead
  of in Libex's log stream.

- **Each worker holds half as many database connections, and the bundled
  Postgres is configured for more of them.** The pool is per process, so six
  workers each holding the previous twenty plus twenty of overflow would have
  wanted two hundred and forty. Each process now holds at most ten plus ten,
  which is a hundred and twenty across six workers, and the bundled compose
  file starts Postgres with `max_connections=200` instead of leaning on the
  image default of 100. That raise is load-bearing rather than precautionary:
  after the slots PostgreSQL holds back for superusers, the default 100 leaves
  97 for Libex's own role, which a hundred and twenty does not fit inside at
  all, while 200 leaves 197 and fits it with seventy-seven to spare. If you
  run a single worker this is a straight halving — twenty connections where
  you had forty — which is ample for one process but worth knowing if you had
  tuned anything around the old figure. If you raise `WEB_CONCURRENCY`, or
  point Libex at a Postgres you configure yourself, raise the connection limit
  alongside it.

- **Libex can now have six times as many requests in flight to Audible as it
  could before.** This is the consequence of the worker count that is worth
  weighing before upgrading, and the one you cannot see from the outside.
  Libex bounds its own outbound traffic with two limits: ten concurrent
  requests on the general pool every lookup uses — a figure settled by an
  incident in which sustained traffic from a shared address drew throttling —
  and twenty-five on a wider pool reserved for a single author-books
  request's own fan-out. A call takes
  one or the other and never both, so one process holds at most thirty-five at
  once. Both limits live inside a process and neither is divided by the worker
  count, so what actually reaches Audible is thirty-five multiplied by it:
  thirty-five before this release, two hundred and ten at the default of six.
  If you reach Audible from an address you share with anyone — a VPN exit, an
  office, anything behind NAT — that is the number this release changes, and
  `WEB_CONCURRENCY` is the only setting that moves it.

  Neither limit is divided among the workers, deliberately. Ten is not only a
  share of what a shared address will take; it is also the width one request's
  own fan-out has to pass through. A thousand-ASIN `/books` lookup goes out as
  twenty chunks, which is two rounds at ten permits and ten rounds at two, and
  a gate narrower than a single request's fan-out is exactly what produced
  measured gateway timeouts here before. Holding the deployment-wide total
  flat as workers are added would have meant paying that price again.

  Two hundred and ten is a worst case rather than a working level: it needs
  every worker to be walking a prolific author's catalog in the same moment.
  What that worst case was weighed against: a ladder of
  10, 25, 50, 100, 125, 150, 175, 200 and 250 concurrent requests to Audible
  returned 1122 responses to 1122 requests, every one of them a 200 — no
  throttling, no server errors, no transport failures — and found no ceiling,
  because the run stopped at its own request budget rather than at anything
  Audible did. Two hundred and ten is 84% of the largest figure tried.
  **That ladder ran on a direct path to Audible, not through the proxy the
  public instance actually leaves by**, whose exit address is shared with
  strangers and whose own headroom has never been measured. So the number is
  bounded by a measurement taken on a different path from the one that runs,
  and 250 is the largest figure tried rather than an observed limit. If your
  own outbound path is one you share, treat six as a figure to lower rather
  than a floor.

### Fixed
- **What a caller searched for is no longer written to the container's
  output.** The web server Libex runs on keeps an access log of its own, one
  line per request, and Libex had left it switched on. That line carried the
  query string exactly as it arrived, so `title=`, `keywords=`, `name=` and
  anything else a caller typed appeared in full — beside Libex's own line for
  the same request, where those values are replaced with `REDACTED`. The work
  in 1.13.0 that stopped Libex recording anything a caller typed rebuilt
  Libex's own line and did not touch this one. The access log is now switched
  off at the source, so the text is never assembled rather than assembled and
  thrown away, and it cannot be turned back on from the environment. Anything
  reading those lines out of your container's output will stop finding them:
  Libex's own request line, which records the method, path, status and
  duration with the query redacted, is what remains.

  Two things bound what this exposed, and both matter for judging whether it
  affected you. That line went to the container's standard output only: it was
  never written to Libex's log file and was never shipped to Axiom or any
  other service, so its reach is whatever collects your container's output —
  for the public instance, the server's own logs and nothing beyond them. And
  the address on it was the host that opened the connection, which behind a
  reverse proxy is the proxy rather than the caller, and is what the public
  instance recorded. A deployment that has configured the server to trust a
  forwarded address is the exception: on that setup the line carried callers'
  real addresses beside their search terms.

- **A lookup no longer holds a database connection open while it waits on
  Audible.** Three paths — a bulk book lookup, a series lookup, and fetching
  an author — read from the database first and then went out to Audible with
  the transaction that read opened still in progress. The connection was
  unavailable to anything else for the whole of that wait, which is bounded by
  Audible rather than by Libex: a lookup at the thousand-ASIN limit is twenty
  batches draining through a fixed number of permits. Each worker holds twenty
  connections, so enough of those at once and other requests queued for one
  and gave up after thirty seconds, and any single wait that ran past a minute
  had the database close that connection underneath the request, failing
  whatever database work it had left to do. It also kept an open read
  transaction on the database for that time, which pins the point autovacuum
  can clean up to and so held cleanup off a large, write-heavy table. Each of
  those paths now finishes with the database before it starts waiting on the
  network, and takes a connection again when it next needs one. What comes
  back is identical.

- **Rotating the log file no longer loses lines when more than one process is
  writing to it.** The standard rotating handler does not survive multiple
  writers, and it fails without saying so: each process reaches the rollover
  moment on its own clock, the first renames the file, and every later one
  takes an early exit that leaves it holding an open handle on the file that
  was just renamed and its next rollover time already in the past. It carries
  on appending to the previous day's file, and once retention deletes that
  file the writes land where nothing can open them again. Nothing raises and
  nothing appears on any stream. Rotation is now serialised across processes
  with a lock file, and every process returns to the live file afterwards.
  Nobody running Libex before this release lost anything to it, because there
  was only ever one writer; it is fixed here because that is no longer true.
  `LOG_RETENTION_DAYS=0` turns rotation off entirely and was already safe,
  since every process simply appends to one file that is never renamed.

- **Log lines carrying structured detail no longer print the timestamp
  twice.** Libex renders a line's structured fields inline after the message.
  The timestamp is attached to a line while it is being rendered, which left
  it indistinguishable from a field passed in by the code doing the logging,
  so every line that carried any structured detail at all ended with a second
  copy of the timestamp it already began with — and every Axiom event gained a
  string field duplicating its own event time. This predates everything above
  and affected the plain-text and Axiom output alike.

- **A departing request could cancel an author's catalog walk for everyone
  else waiting on it.** When several requests ask for the same author's books
  at once, one of them performs the walk and the rest wait on its result
  rather than starting their own. That wait was direct, and cancelling a
  coroutine which is awaiting a task cancels the task as well — so a waiting
  request that went away, through a shutdown or a timeout wrapped around it,
  took the shared walk down with it and discarded everything already fetched,
  for itself and for every other request attached to it. A waiting request is
  now insulated and costs nothing but its own place in the queue when it
  leaves. Cancellation still travels the other way by design: if the request
  performing the walk is itself cancelled, the walk ends and the waiters end
  with it, because that walk runs on that request's own database session and
  leaving it running against a session nobody owns would strand a connection
  from a pool shared with the seeder and every background writer.

## [1.13.4]

### Changed
- **Libex now holds a connection to Audible open for two minutes between
  requests instead of five seconds.** Libex keeps a pool of connections to
  Audible so that a burst of outbound calls — fetching a prolific author's
  books can mean dozens — reuses an already-negotiated connection rather than
  setting up a new one each time. The HTTP library discards an idle connection
  after five seconds by default, and that is shorter than the gap *between*
  requests rather than the gap inside one: a lookup arriving more than five
  seconds after the last Audible traffic found the pool empty and negotiated a
  connection from scratch for every call it was allowed to have in flight at
  once, which for one author's books is twenty-five. Two minutes is the longest
  idle gap measured to still find a live connection waiting at Audible's end —
  gaps of one, six, twenty, thirty, sixty and a hundred and twenty seconds all
  reused one, where at the old five-second default a six-second gap discarded
  the entire pool — and the value sits there rather than higher because holding
  a connection past the far end's own idle limit only produces ones it has
  already closed. What this avoids is setup work, not lookup work: no endpoint,
  response shape, field or status code moved, and no request returns different
  data than it did before. The saving was not resolvable above run-to-run
  variation when measured on a direct path to Audible, and it has not been
  measured on the public instance, whose outbound traffic takes a longer route
  where setting up a connection costs considerably more. Expect it to help most
  where it was hardest to measure, and treat the gain as expected rather than
  demonstrated.
- **Libex now writes a batch of freshly fetched books to its database in far
  fewer transactions.** After a bulk lookup, Libex stores what it fetched in
  the background. It did that one book at a time and committed twice for each
  — once for the book, once for its cached copy — so putting away a prolific
  author's thousand-book catalog meant two thousand separate transactions.
  Books are now written fifty to a transaction with their cached copies
  alongside them in the same one, which for that catalog is twenty commits
  rather than two thousand. Two places that settle a race between simultaneous
  writes of the same author used to abandon the entire transaction in order to
  undo a single failed statement; they now undo just that statement, which is
  what makes it safe for fifty books to share one. What survives a failure is
  unchanged: if a transaction is lost — a lock conflict with the seeder, a
  dropped connection, one book carrying data the database rejects — that group
  of fifty is written again one book at a time down the original path, so no
  book is lost by having been grouped with a book that failed. Every write
  involved is an upsert, so replaying one already stored writes it to the same
  values. This is background work throughout: no request returns different
  data, and none returns at a different time.
- **A bulk book request checks its cache in one lookup instead of one per
  ASIN.** Asking for many ASINs at once made a separate database query for
  every ASIN before deciding which of them still had to be fetched from
  Audible, and the endpoint accepts up to a thousand in a single request — a
  thousand queries before any work began, and a thousand more on the path that
  falls back to the cache when Audible cannot be reached. Both now ask once
  for the whole list. The same entries count as hits: one that is absent or
  expired is a miss exactly as before, except that expiry is now judged at a
  single instant for the whole request rather than a fractionally later one
  for each ASIN in turn, so a request can no longer treat an entry as live
  near the front of its list and expired near the back.
- **An author's stored catalog is read with one query for its series positions
  rather than one per book.** When Libex answers a request for an author's
  books from its own database, it looked up each book's position within its
  series separately — one query per book, across a catalog that has no page
  limit and for a prolific author runs to thousands of rows. Those positions
  are now fetched for the whole catalog at once. The books, their order, and
  their contents are identical. This covers that one path only: the other
  endpoints that list books out of the database still look up series positions
  a book at a time, and are unchanged here.

### Fixed
- **A description or summary that was missing the first time Libex saw a
  record could never be filled in afterwards.** Libex keeps whichever version
  of a description carries more text, so that a later, richer answer from
  Audible replaces a thinner stored one. The comparison deciding that was
  written so that measuring a stored value which was already null produced
  neither a yes nor a no but nothing at all, and the rule then fell through to
  keeping what was there. The effect was that a book description or summary,
  an author description, or a series description that was null when the record
  was first stored stayed null permanently: no later answer, however long,
  could displace it, because the emptiness was itself what made the comparison
  unanswerable. Those fields now compare correctly and fill on the next write.
  This governed what Libex had stored, and so what it returned whenever it
  answered out of its own database rather than passing on a live answer from
  Audible. Nothing is backfilled by this release — a record that reads null
  today stays null until Audible is next asked about it and returns text,
  which for most records means the next lookup or the next pass of the seeder.
- **A value made only of spaces could overwrite stored text.** Because the
  rule above keeps whichever value is longer, an incoming description
  consisting of nothing but whitespace won whenever it happened to be longer
  than what was stored — ten spaces replacing a six-character description, and
  reading back as those ten spaces. An incoming value is now measured with its
  surrounding whitespace ignored, so one that is empty or entirely whitespace
  counts as absent and cannot displace anything. Only the measurement is
  trimmed: a value that does win is stored exactly as Audible sent it,
  spacing included.

## [1.13.3]

### Fixed
- **The Swagger UI page no longer prints Libex's name twice.** The logo put on
  `/docs` in 1.13.2 is a wordmark, and the heading beneath it spells the name
  out again, so since that release the page has read "Libex" directly under a
  picture of the word "Libex". That heading's text is now collapsed to nothing,
  and the version badges beside it are re-seated: they carried a five-pixel
  lift, tuned to sit against a full-size title, which with the title gone only
  pulled them out of their own line and tightened the gap under the logo. The
  badges — the API version and `OAS 3.1` — and the link to `/openapi.json` are
  all still shown, and so is the name itself to anything reading the page
  aloud. The text is collapsed rather than removed or hidden, so it is still
  announced; that matters because the logo carries no text alternative of its
  own, being a background image, which leaves the heading as where the name
  lives for anything not reading the picture. `/redoc` is untouched, and the
  change is confined to a stylesheet Libex serves for that one page: no
  endpoint, response shape, field or status code moved anywhere.

## [1.13.2]

### Changed
- **The Swagger UI page carries the logo too.** `/docs` previously showed none:
  the OpenAPI document names one, but that key is ReDoc's, so the artwork added
  in 1.13.1 appeared on `/redoc` and nowhere else. It now sits above the title
  on `/docs` as well, placed by a stylesheet Libex serves itself rather than by
  any change to the page. That stylesheet pulls in the Swagger UI sheet the
  build already fetches rather than replacing it, so the bundle is still styled
  by the CSS that shipped with it and nothing outside Libex is contacted; the
  cost is one extra round trip, because a browser cannot begin fetching the
  second sheet until it has read the first, and both hold up the first paint.
- **Both documentation pages take a new browser-tab icon.** The old one was a
  placeholder — a plain drawn letter with no relation to Libex's artwork — and
  it is replaced by the mark from the logo, padded square, at 32 pixels rather
  than the conventional 16 because the mark is three elements and renders as
  mush at 16. `/docs` and `/redoc` take it from the same place, so neither page
  can quietly end up with a different icon from the other. The old icon's URL
  is gone with it: `/static/favicon.svg` now returns 404. Nothing else in Libex
  referenced it and it only existed from 1.13.0, but anything that linked it
  directly wants `/static/favicon.png` instead. No endpoint moved, no response
  shape or field changed, and no status code changed anywhere else.

### Fixed
- **The privacy policy and the README no longer claim every documentation
  asset is checksum-verified.** Both said the files behind `/docs` and `/redoc`
  are served "at pinned and checksum-verified versions". That was true while
  all of them came from the build-time fetch, and stopped being true once Libex
  began committing artwork of its own. Of the seven files served from
  `/static`, three are the Swagger UI and ReDoc bundles, which the image build
  downloads at a pinned version and refuses to ship unless the recorded hash
  matches — and four are Libex's own: the two logo variants, the icon and the
  stylesheet above, committed to the repository, with no upstream version to
  pin and no checksum to verify. `PRIVACY.md` now describes the two groups
  separately, provenance being its subject; the README drops the claim instead,
  because the question it answers there is narrower — whether opening the docs
  hands a visitor to a third party — and the answer to that was and remains no.
  `PRIVACY.md` also corrects a second line that told self-hosters there is
  "nothing to serve" without running the asset-fetch script; what is missing
  without it is the downloaded bundles, the rest being in the checkout already.

## [1.13.1]

### Added
- **The API docs carry Libex's own logo.** The OpenAPI document now names one
  in `info.x-logo`, which is where ReDoc looks for it, so `/redoc` draws it at
  the top of its sidebar where the page previously showed nothing. The Swagger
  UI page at `/docs` is unchanged — it has no equivalent. The artwork is served
  from `/static`, the same path the docs assets already use, and both variants
  are committed to the repository rather than fetched during the image build,
  so a self-hosted instance has them with nothing extra to run and no new build
  step. The docs are pinned to the dark-on-light artwork, which is the one that
  reads against ReDoc's near-white sidebar; the page cannot pick between the
  two files on the visitor's colour scheme the way the README does.

### Changed
- **The README leads with the logo in place of its title.** The `# Libex`
  heading the artwork duplicated is gone, so the page now starts at its section
  headings, and it switches to a light-on-dark version of the logo for anyone
  reading with a dark colour scheme. Nothing in the repository linked to the
  old heading's anchor, but an outside link to it will no longer resolve.
- **No endpoint's response changed.** `info.x-logo` is an extension on the
  OpenAPI document's info object — the description of the API rather than
  anything it returns — and no field, response shape or status code moved
  anywhere else.

## [1.13.0]

### Added
- **`PRIVACY.md`**, the first privacy policy for the public instance, linked
  from the README. It states what is recorded and what is not, names every
  party that sees a request — Cloudflare, Axiom, Audible and the operator of
  the server — and is honest about the limits: Cloudflare still sees real
  addresses, an unhandled error still logs its own message, which can contain
  text a caller sent, and the database-fallback warning still lists the ASINs
  a request asked for.

### Changed
- **Libex no longer records anything that identifies a caller.** The client IP
  address is not logged in any form — not in full, not truncated, not hashed.
  It is read from no header and from no connection. The query string is now
  rebuilt before a log line is written rather than recorded as it arrived.
  Structural options — `region`, `limit`, `page`, `sort` and the catalogue
  filters — keep their values, because they describe how a caller asked
  rather than what they typed. Every other parameter Libex recognises keeps
  its name and has its value replaced with `REDACTED`, so a line still shows
  that `name=` or `keywords=` was used without showing the contents. That
  covers anything a caller typed — `name`, `keywords`, `title`, `author`,
  `narrator`, `publisher`, `query`, `search` — and, because the rule is an
  allowlist and not a judgement call, it covers identifiers too: a bulk
  `asins` list passed as a query parameter is redacted along with everything
  else, so the request line no longer says which books were asked for. The
  database-fallback path still says so, deliberately: when Audible is
  unreachable and Libex falls back to its own database, the warning recording
  that fallback lists the ASINs the request asked for, and so does the
  warning raised if that database read then fails as well, so which books an
  outage affected stays answerable. Those are catalogue identifiers checked
  against a strict ten-character format before they reach that code, so they
  cannot carry anything a caller typed. A parameter name Libex does not
  recognise is dropped whole — neither the name nor the value is written —
  and the line carries a single `_unrecognised` count in their place. Names
  get dropped rather than redacted because an unrecognised one is usually not
  a parameter name at all: an unencoded `&` inside something typed splits it
  mid-value and lands the remainder in name position, and a query string
  containing no `=` arrives as one long name. A structural parameter is also
  redacted, rather than kept, when its value runs past 64 characters or
  contains a character that has no place in a catalogue facet. What belongs
  there is judged by Unicode category rather than by a list of permitted
  characters, so a facet name survives whatever script it is written in:
  letters in any alphabet, the combining marks those letters carry, numbers,
  punctuation, symbols and spaces are all kept. That is what keeps the `&` in
  an English genre name, the middle dot in a Japanese one, the curly
  apostrophe in a French or Italian one and the vowel signs in a Hindi one
  out of the redaction — a list of allowed characters would have had to be
  extended for every taxonomy a marketplace adds, and the one it missed next
  would have been invisible from a US test run. The 64-character bound counts
  characters rather than bytes, so a name in a multi-byte script is not
  charged for its encoding; measured across all eleven marketplaces, the
  longest of 6,787 genre names is 62 characters. Two characters are excluded
  by name, `;` and `=`, which is how a second query would be smuggled inside
  a value that is otherwise allowed to be logged, and the control, format and
  line-separator characters are excluded too, since any of them would put
  part of a value on a log line of its own. The search service no longer logs
  search text either — its log lines now record how long a query was, how
  many parts a compound query split into, and which fields were searched on,
  never their contents. An allowlist rather than a blocklist, deliberately: a
  blocklist leaks every parameter added after it was written, silently and by
  default.
- **The lookup routes stopped logging caller text behind the request line
  too.** Asking for an author's books by name previously wrote the name that
  was typed into the log line for the successful call, and again into the
  warning raised when a page of that lookup failed — a warning the background
  seeder raises too, so its lines lose the name as well, the two being
  indistinguishable at the point it is written. Three database-read
  warnings — narrator search, a narrator's books, and series search — quoted
  the name that had been searched for. None of them do now. Every operational
  field is kept, including the underlying error text on the failures, so
  diagnosing those routes is no harder: how many books were found, how many
  pages were fetched, how long it took, and which region. An author's name is
  still logged where it was resolved from an ASIN rather than typed by a
  caller, because at that point it is catalogue data and not something anyone
  sent.
- **Database errors no longer print the values a query was run with.** Taking
  the searched-for name out of those warning messages was not enough on its
  own: a failed statement's exception text carries the values the statement was
  run with, so a search that timed out re-emitted what the caller typed through
  the error itself even though the message beside it had been cleaned. The
  database engine now suppresses that rendering for every statement, including
  ones not yet written. Anyone running their own instance will see the
  difference: a database error now reads `[SQL parameters hidden due to
  hide_parameters=True]` where the values used to appear. The driver's own
  message and the full statement are still logged, so a failure is still
  diagnosable down to the query that caused it — only the literal values are
  gone. The same switch covers `DATABASE_ECHO`, which when turned on had been
  printing the values of every successful query, not only failing ones.
- **The HTTP client's own request logging is muted, so it cannot start
  printing search text later.** Libex forwards a caller's search terms to
  Audible inside the request URL, and `httpx` logs every request URL it
  makes — query string and all — at info level. Nothing was routing those
  records anywhere: Libex attaches its handlers to its own logger and never
  to the root one, so on any normal instance those lines were discarded and
  no search text was ever written by them. That was an accident of how
  logging happened to be configured rather than a decision, and it would have
  ended the moment anyone attached a handler to the root logger or started
  the server with their own `--log-config` — with no code change and no sign
  that it had happened. `httpx` and `httpcore` are now held at warning level
  explicitly, so those records are never emitted no matter who else
  configures logging. Anyone self-hosting who had wired up a root handler to
  capture those per-request client lines will no longer see them.
- **Per-endpoint observability is unchanged.** Method, path, status, duration,
  user agent and host header are all still logged, so failure rates and
  latency remain visible per endpoint. The user agent stays because it names
  client software rather than a person, and with no address recorded beside it
  there is nothing to tie it back to an individual. The fields themselves did
  change, though, so anyone querying their own logs should expect it: `ip` is
  gone entirely, and the search lines carry a length, a part count and a list
  of field names where they used to carry `keywords`, `search_params` and the
  parsed segments.
- **No API response changed.** This affects only what the server writes about
  a request, never what it returns.
- **The interactive API docs are served from local assets.** `/docs` and
  `/redoc` previously had the browser fetch Swagger UI and ReDoc from
  `cdn.jsdelivr.net`, a favicon from `fastapi.tiangolo.com`, and — on the
  ReDoc page only — a web font stylesheet from `fonts.googleapis.com`, so
  opening the docs sent a visitor's real IP address to three third parties.
  Those assets are now fetched at build time at pinned, checksum-verified
  versions and served by Libex itself, and the ReDoc page is rendered with its
  web font stylesheet switched off, so it uses fonts already on the machine.
  A fourth recipient outlived all of that: the ReDoc bundle asks the browser
  for a logo from `cdn.redoc.ly` as it draws the page, so serving the file
  unmodified would still have handed Redocly the address of everyone who
  opened `/redoc`. That URL is now rewritten to an inlined transparent pixel
  as the file is fetched, and the build fails outright if the reference is not
  found exactly once beforehand or if any reference to that host survives
  afterwards. Redocly's attribution link and its "API docs by Redocly" text
  are deliberately left intact. The Swagger UI page also turns off its
  spec-validator badge by name. Swagger UI's own default points that badge at
  `validator.swagger.io`, and the badge is not mounted in the pinned bundle,
  so nothing was ever sent there — but that was a property of the version
  being served rather than of how Libex configures it, and a later bundle
  that did mount the badge would have reintroduced the request with nothing
  to catch it. Loading the docs now contacts nothing but Libex. Pinning also
  closes a standing supply-chain exposure: the previous URLs tracked floating
  major tags, so a visitor's browser executed whatever the CDN resolved them
  to that day. Serving them adds one new path, `/static`, which is where
  those files and the favicon are now published; no existing endpoint moved
  or changed.
- **Self-hosters have one new build step.** The docs assets are fetched during
  the image build and are deliberately not committed to the repository, so a
  normal Docker build picks them up with nothing to do — and fails outright if
  a checksum doesn't match, rather than shipping a substituted file. Running
  from a local checkout instead, `scripts/fetch_docs_assets.sh` has to be run
  once by hand; without it `/docs` and `/redoc` return an empty page, because
  the scripts that draw them are the files that weren't fetched. Nothing else
  about the instance is affected, and the docs still contact no third party
  either way.

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

[1.14.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.14.0
[1.13.4]: https://github.com/LibexHQ/Libex/releases/tag/v1.13.4
[1.13.3]: https://github.com/LibexHQ/Libex/releases/tag/v1.13.3
[1.13.2]: https://github.com/LibexHQ/Libex/releases/tag/v1.13.2
[1.13.1]: https://github.com/LibexHQ/Libex/releases/tag/v1.13.1
[1.13.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.13.0
[1.12.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.12.0
[1.11.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.11.0
[1.10.5]: https://github.com/LibexHQ/Libex/releases/tag/v1.10.5
[1.10.1]: https://github.com/LibexHQ/Libex/releases/tag/v1.10.1
[1.10.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.10.0
[1.9.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.9.0
[1.8.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.8.0
[1.7.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.7.0
[1.6.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.6.0
[1.5.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.5.0
[1.4.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.4.0
[1.3.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.3.0
[1.2.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.2.0
[1.1.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.1.0
[1.0.0]: https://github.com/LibexHQ/Libex/releases/tag/v1.0.0