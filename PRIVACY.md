# Privacy Notice

This notice covers the public Libex API at [libexdb.com](https://libexdb.com)
and its former address, `libex.lostcartographer.xyz`, which serves the same
instance until 4 November 2026. It sets out what the service records about the
requests it receives, why, who else handles that data, how long it is kept, and
what you can ask me to do about it.

If you run Libex yourself, or build the `libex_core` library into an
application, you decide what is recorded, not me. See
[Self-hosted instances](#self-hosted-instances) and
[The embeddable library](#the-embeddable-library).

Libex is open source, so what the software records can be checked against its
source, mainly `app/core/middleware.py`, `app/core/logging.py` and the error
handler in `app/main.py`. Anything that depends on a setting in a vendor's
console rather than on code is marked as such. This notice describes how the
service behaves. It is not legal advice.

## Summary

- **No accounts, API keys, cookies or tracking.** Libex has no concept of a
  user.
- **No IP addresses are logged**, in full or in any shortened or hashed form.
- **What you type is not logged.** Search text is replaced with `REDACTED`. The
  one exception is an unexpected error whose message happens to quote your
  input ([Errors](#errors)).
- **Each request produces one request log line.** It describes the request
  (endpoint, status, timing, client software) and contains nothing that links
  it to your other requests.
- **Named third parties.** Cloudflare sits in front of the service and sees
  every request, including your IP address. Axiom stores the logs. Audible
  receives the lookups and searches needed to answer you, sent through a VPN
  and carrying nothing about you. Details are in
  [Who receives data](#who-receives-data).
- **Retention.** Axiom deletes records after 30 days. The log file on the
  server keeps 7 days by default. Container output on the server is covered,
  with the other details, in [Retention](#retention).
- **No selling or sharing** beyond the parties named in this notice.
- **The database holds no data about callers**, only Audible catalogue
  metadata.

## Who operates the service

The maintainer of Libex ([@SunBroLynk](https://github.com/SunBroLynk)) runs the
public instance as a free service. "I" in this notice means that person. The
instance runs on infrastructure operated within LibexHQ, which is the project's
own organisation, not an outside company. Libex has other contributors. They do
not operate the public instance and have no access to its logs.

**Contact:** open a [GitHub issue](https://github.com/LibexHQ/Libex/issues). For
anything you would rather not raise in public, use the email address in
[`SECURITY.md`](SECURITY.md).

## What is recorded

### The request log

Each request produces one log line with the fields below. The exceptions are
`/health`, browser preflights, and requests that end in an unexpected error,
all described further down.

| Field | Contents | Purpose |
|---|---|---|
| `method` | The HTTP method, e.g. `GET`. | Completeness. |
| `url` | The path only, e.g. `/book/B01234567`, exactly as sent, including a path that matches no route. Some path segments, such as the one in `/book/sku/{sku}`, are not format-checked, so a path can contain arbitrary text. | Which endpoints are used and which are failing. |
| `query` | Query parameter names, with values filtered as described in [Query parameters and search text](#query-parameters-and-search-text). | Which options clients use, so changes can be made safely. |
| `status` | The HTTP status returned. | Detecting failures. |
| `took` | Duration in milliseconds. | Performance. |
| `userAgent` | Your `User-Agent` header, verbatim (e.g. `python-httpx/0.27`). | Identifies the client software, so I know which clients a change will affect. |
| `host` | The `Host` header, i.e. which hostname you used. | Tells old-address traffic from new while both hostnames serve the same instance. |
| `source` | Where the response data came from: `cache`, `db`, `audible`, or `mixed` with a count per source. | Shows how much the cache absorbs, and when Libex falls back to its database because Audible is unavailable. |
| `complete` | `true` or `false`: whether the response contains everything requested. | Counting incomplete responses. |
| `incompleteReason` | When `complete` is `false`, one or more of `discovery-incomplete`, `hydration-deadline`, `hydration-failed` and `hydration-not-found`. | Separates "Audible was too slow" from "Audible doesn't have it". |
| `request_id` | A random identifier for this request, also returned to you. See [Request IDs](#request-ids). | Lets you and me refer to one specific request. |
| `pid` | The process number of the server worker that handled the request. | Tells one failing worker apart from several. |

About these fields:

- **`source`, `complete` and `incompleteReason` describe the response, not
  you.** They carry the same values as that response's `X-Libex-Source`,
  `X-Libex-Complete` and `X-Libex-Incomplete-Reason` headers, so the log holds
  nothing you weren't also sent. They are empty when a response doesn't report
  them, and the code limits them to the fixed values above, so they cannot
  carry anything you typed. A `cache` value does show that someone requested
  the same item recently. That says something about how popular a title is,
  not about who asked for it.
- **`pid` belongs to the server.** It is the same for every request a worker
  handles, changes only when the worker restarts, and the operating system
  decides which worker takes a request. Two lines with the same `pid` are
  therefore no evidence of the same caller. It appears on every line Libex
  writes, not only on request lines.
- **Only two of your request headers are read for logging:** `User-Agent` and
  `Host`.
- **`/health` is not logged.** The one exception is a health check that takes
  longer than a second. That writes a single warning containing the path
  `/health`, the status and the duration, and nothing else, not even a query
  string.
- **Browser preflight (`OPTIONS`) requests** are answered before logging runs
  and are not recorded.
- **Documentation pages and README badges are logged like any other request.**
  This covers `/docs`, `/redoc`, `/openapi.json`, the files under `/static`,
  and the statistics badges at `/db/stats/badge/`. The Libex README displays
  those badges, so viewing the README produces request lines here even though
  the viewer never knowingly called the API.

### Query parameters and search text

Query parameters pass through an allowlist that decides which names are kept as
well as which values:

- **Name and value kept:** structural parameters such as `region`, `limit`,
  `page`, `sort`, `order`, the catalogue filters, and switches that set the
  shape of a response, such as `flat` and `label`. Even here, a value is
  replaced with `REDACTED` if it is longer than 64 characters or contains `;`
  or `=`, since those are how a second parameter could be hidden inside one
  value.
- **Name kept, value replaced with `REDACTED`:** parameters whose value is text
  you choose, such as `name`, `keywords`, `title`, `author` and `narrator`, and
  also `asins`, the list of identifiers in a bulk lookup.
- **Dropped:** any parameter name Libex doesn't recognise. Such a name is
  usually a fragment of typed text, for example when an unencoded `&` splits a
  search in two, or a query string with no `=` arrives as one long name. It is
  discarded and replaced by a count, e.g. `_unrecognised=2`.

Because this is an allowlist, a parameter nobody has classified yet is withheld
by default rather than logged.

The search endpoints (`/search`, `/quick-search`, `/author/books?name=` and
similar) also write lines of their own. These record which fields were searched,
how long the query text was, how a compound query was split into segments, how
long Audible took, and how many results came back. They never record the text
itself.

Audible does receive your search terms, because a search cannot be answered
without asking it. See [Who receives data](#who-receives-data).

### Request IDs

Responses carry an `X-Request-Id` header, including `/health` and error
responses, with the two exceptions noted below.

- The server generates it for that one request. If you send an `X-Request-Id`,
  it is ignored.
- It is not derived from anything about you, is never reused, and is not
  stored against anything else, so it cannot link your requests to each other.
- If you quote it to me, I can find the matching log line. Normally that is the
  request line above. If the request failed with an unexpected error, it is the
  error line instead ([Errors](#errors)).
- Two kinds of request carry no ID: one that fails before an ID is assigned,
  and a browser preflight. No ID is made up after the fact.

### Errors

When a request fails in a way the code does not anticipate, no request line is
written. Instead Libex logs an error line with the error message and stack
trace, so the failure can be diagnosed. That line carries the request ID but
none of the request-line fields: no path, query, user agent or host.

If the error message quotes something you sent, as some library errors do with
the input they were given, that text is in the error line. This is the only way
text you typed can reach the logs, and it is incidental rather than deliberate
collection. A log line that records caller input on purpose is treated as a
defect and removed. Earlier lines that named a searched-for author, narrator or
series were removed on that basis. Error lines are deleted on the same schedule
as all other logs.

### Operational logs

Libex also logs its own work, whether or not anyone is calling it: cache
activity, database reads and writes, calls to Audible and how long they took,
background catalogue jobs, and startup and shutdown. These lines identify
catalogue items, never callers. An item is identified by its ASIN and region,
or by an author's or narrator's name read from Libex's own database when a
background job refreshes that entry. They carry no IP address, no user agent,
and nothing tied to your request. In Axiom their details are separate fields
that can be searched by name. Some are worth describing individually:

- **Bulk lookups during an outage.** If Audible is unreachable and
  `/books?asins=` falls back to Libex's database, the warning that records the
  fallback lists the ASINs that request asked for, in a field named `asins`.
  If the database read also fails, it lists them too. The same applies when the
  database fills in after a partial Audible failure. This is the one place the
  logs hold what a bulk request asked for, which the request line itself
  redacts. It is there so it is possible to tell which titles an outage
  affected. ASINs are checked to be ten-character catalogue identifiers before
  this point, so they cannot carry free text, and the line holds nothing about
  who asked.
- **Problems with individual titles.** If data from Audible can't be parsed or
  stored, a warning names that title's ASIN, sometimes its region, and the kind
  of problem. It does not include the value that caused the problem, except
  for a malformed author ASIN: that value and the author's name, both from
  Audible's catalogue, are logged, or `REDACTED` if either doesn't look like a
  short catalogue entry.
- **Database failures.** These name the item being read in its own field
  (`asin`, `author_asin`, `series_asin`, `plan_name` or `sku_group`), which
  repeats part of a path the request line already records. They include the
  database error code and the schema, table, column and constraint names. Most
  of them leave out the database's own error text, which can quote stored
  catalogue rows. One, a failed author read, still includes it. Database errors
  never include the values of a query's parameters, so search text passed to
  a database query cannot appear in them. Failure lines outside the database
  include the error's text.
- **How Libex reaches Audible.** At startup, the API and each background job
  that calls Audible log whether they connect to Audible directly or through a
  proxy. The API, the seeder and the chapter backfill also log the proxy's
  hostname, in the fields `audible_transport_host` (API) and `proxy_host`
  (seeder and backfill). The corpus refresh logs only whether a proxy is in
  use, unless it refuses to start because the proxy isn't the one set aside
  for it, in which case it logs the hostname it was given. The proxy's full
  address is never logged, because it can contain a password.

### IP addresses

Libex does not read your IP address from any header or from the connection, and
does not record it in any form. Versions before 1.13.0 (August 2026) logged the
full address, which fed a map of where requests came from. The map was
deliberately removed along with the logging. Records written by those versions
were not altered afterwards. They expire on the same schedules as every other
record ([Retention](#retention)).

Cloudflare still sees your address; see [Who receives data](#who-receives-data).

## What is not collected

- **Cookies.** Libex never sets or reads a cookie and sends no `Set-Cookie`
  header.
- **Accounts, logins, API keys and sessions.** There is no user record, because
  there are no users.
- **Analytics and tracking.** Libex uses no analytics service, tracking pixel,
  fingerprinting or error-reporting service. Axiom is the only service it sends
  log records to.
- **Anything that links your requests to each other.** Request IDs are
  per-request, and the worker `pid` is shared by every caller.
- **Request bodies.** Every endpoint open to the public is a `GET` and accepts
  no body. One undocumented maintenance endpoint accepts uploads from the
  operator's own tools and rejects everyone else.
- **`Authorization`, `Cookie` and `Referer` headers.** None of these is logged.
- **Caller data in the database.** The PostgreSQL database, and any backup of
  it, holds Audible catalogue metadata (books, authors, narrators, series,
  genres, chapters) and a cache of Audible responses. Cache entries are keyed by
  what was asked for, never by who asked: a region with an ASIN or list of
  ASINs, or a date window and category. Deleting caller data therefore means
  deleting logs. The database has none to delete.

## Who receives data

"I don't sell your data" and "nobody else has it" are different statements.
Only the first is true. These parties handle request data because they are part
of how the service runs:

| Recipient | What it receives | Why | Whose rules apply |
|---|---|---|---|
| **Cloudflare** | Every request in full, including your full IP address, before it reaches Libex. | Sits in front of the service and terminates TLS. | Cloudflare's. I cannot see, change or delete its logs. |
| **Axiom** | The log records described above. Each event also has a timestamp, level, logger name and message. Error messages include their stack trace. | Stores and indexes the logs so problems can be investigated. | Axiom's, as a hosted service. I am the only person with access to the dataset, but Axiom stores it on its own infrastructure. |
| **The server operator** (me) | The same log lines, in a rotating file and in the container output on the server. | Diagnosis. | Mine. See [Retention](#retention). |
| **Audible** | The lookups and search terms needed to answer your request. Nothing about you: not your IP address, user agent or host header. Libex sends its own fixed headers and connects through a VPN, so Audible sees the VPN's exit address, not mine and not yours. | Libex answers from Audible's API. | Audible's. |
| **The VPN provider** | Encrypted connections from Libex's server to Audible's regional API hosts. It can see which host, when, and how much data, but not the path, the title, the search terms, or anything about you. The connections start at my server, not at your device. | Carries Libex's outbound traffic to Audible. | The provider's. |

**The VPN client.** An open-source VPN client container runs alongside Libex
and carries this traffic. Based on how that software documents its behaviour
(not something Libex's source can confirm), it does two things worth knowing:

- It writes one line per connection it carries: the internal address of the
  Libex container and the Audible host. There is no path, title or caller
  address, because it can't see them. The line stays in Docker's log on the
  server and is never sent to Axiom.
- It makes a few connections of its own through the VPN: a lookup of its public
  address, encrypted DNS lookups through Cloudflare to find Audible's hosts, a
  periodic connectivity check, and a check on GitHub for newer releases of
  itself. None of these carries anything from your request.

**Documentation pages.** `/docs` and `/redoc` are built from files Libex serves
itself. Libex's own stylesheet, logo and favicon are committed to the
repository. The Swagger UI and ReDoc bundles are downloaded once, at pinned
versions, when the image is built, and each is checked against a recorded
checksum. Opening these pages contacts no third party: no CDN, font service or
externally hosted icon. The pages do contain ordinary outbound links, such as
an attribution link and specification URLs. These contact nobody unless you
click them.

**README badges.** The counters in the Libex README are images served by Libex
from `/db/stats/badge/`. Until recently, shields.io drew them and fetched the
numbers from Libex itself. Now the viewer's fetch comes here: it crosses
Cloudflare and is logged like any other request. GitHub says it fetches README
images through its own proxy, in which case Libex and Cloudflare see that proxy
rather than the viewer. That is GitHub's behaviour and cannot be confirmed
from this repository. The README's fixed badges, the licence and the two
container registries, are still served by shields.io.

**Nobody else.** I don't sell, rent, trade or share log data with advertisers,
data brokers or anyone else.

## Retention

| Where | How long | Where this is set |
|---|---|---|
| Log file on the server | Rotated daily at midnight. The previous `LOG_RETENTION_DAYS` days are kept, 7 by default. | Libex's code and configuration. |
| Container output (Docker's log), including the VPN client's connection log | The published compose files cap this at five 10 MB files per container, discarding the oldest first. The limit is on size, not age. The public instance does not yet run with these caps. Until it does, how long its container output is kept depends on the server's own Docker configuration. | The published compose files for deployments that use them. The server's Docker configuration for the public instance. |
| Axiom | 30 days, then deleted. | A setting I configured in Axiom's console. It is not in the code, so it can't be checked from this repository. If it changes, this notice will change with it. |
| Cloudflare | Set by Cloudflare. | Cloudflare. I have no visibility into it. |

## Your rights

Data protection laws such as the GDPR and UK GDPR give you rights over personal
data about you, including access, deletion, objection and correction. These
rights normally work by finding the records about you. Libex records nothing
that identifies you, so nothing you could tell me about yourself, including
your IP address, would let me find your requests. The exception is a request ID
you kept. This is what I can do:

- **Access.** If you give me an `X-Request-Id`, I can find that line while it
  is still kept and tell you exactly what it contains, which will be the fields
  described above. A request ID does not prove who made the request, since
  anyone who holds it can quote it.
- **Deletion.** Ordinary log lines contain nothing identifying to delete, and
  they expire on the schedule above. If you think an error line captured
  something identifying that you sent, tell me, and include the
  `X-Request-Id` if you have it. I will delete the copies on my own server (the
  log file and the container output). I cannot promise to remove a single
  record from Axiom: I use Axiom as a hosted service and have not established
  that it allows this, so that copy is deleted on its 30-day schedule. The same
  applies to records written before 1.13.0 that contain an IP address.
- **Objection, portability and correction.** Each of these needs data about you
  to act on, and Libex holds none.
- **Cloudflare** holds your IP address under its own policy. Requests about
  that data need to go to Cloudflare.

If you are in the EU or UK, you can also raise a concern with your local data
protection authority.

## Self-hosted instances

If you run your own instance, you are the operator. You decide what is logged
and where it goes, nothing from your instance reaches me, and this notice does
not describe your instance. If you offer your instance to other people, write
your own notice for them.

What the software does as shipped:

- **The same request logging**, using the same code, to stdout (warnings and
  errors to stderr) and to a rotating file at `./logs/libex.log`.
- **Nothing is sent to Axiom unless you set `AXIOM_TOKEN`.** Without it, no log
  record leaves your server.
- **No switch turns off the file or stdout logging.** To stop request lines,
  set `LOG_LEVEL` to `WARNING` or `ERROR`. The request line is written at
  `INFO`, so this removes it from every destination. Warnings and errors are
  still written, including the slow-`/health` line and error lines that can
  carry caller input, so a raised level does not guarantee that caller text
  never lands in your logs.
- **`LOG_RETENTION_DAYS`** sets how many days of rotated files are kept. `0`
  keeps them forever, not zero days. The compose files limit Docker's own log
  to five 10 MB files per container.
- **No Cloudflare**, unless you put it in front yourself.
- **How your instance reaches Audible depends on how you deploy it.** The
  published compose files will not start without a proxy for Audible traffic,
  and each includes an example VPN client container to provide one. Deployed
  that way, Audible sees your VPN's exit address, and your VPN provider sees
  which Audible host you connect to and when, but not the content. The example
  container logs and makes outbound connections as described under
  [Who receives data](#who-receives-data). It is only an example: any HTTP or
  HTTPS proxy will do, and choosing a VPN provider, and what that provider
  logs, is up to you. Libex itself does not require a proxy. Run it some other
  way with `AUDIBLE_PROXY_URL` blank and it connects to Audible directly from
  your server's address. The startup log line shows which mode is in use.
  Either way, search terms go to Audible and nothing about your users does.
- **Documentation pages** are served from your own copy of the assets, which
  `scripts/fetch_docs_assets.sh` downloads and verifies during the Docker
  build. If that step hasn't run, the pages come up blank rather than loading
  from a CDN.
- **README badges point at the public instance.** In a fork, anyone viewing
  your README fetches them from `libexdb.com`, and the fetch is logged there
  under this notice. Your instance serves the same routes, so point the badges
  at your own host or remove them.

## The embeddable library

`libex_core` is the part of Libex that runs inside another application. It
fetches from Audible in that application's own process, with no Libex server
involved. It is published on PyPI as `libex-core`. This section describes the
source as it currently stands; releases are listed in
`libex_core/CHANGELOG.md`, and an older release may differ from it. The
behaviour described is in `libex_core/audible/client.py`, with the logging of
individual titles in `libex_core/audible/books.py` and
`libex_core/audible/extras.py`, searching in `libex_core/audible/search.py`,
author lookups in `libex_core/audible/authors/`, browsing new releases,
coming soon and categories in `libex_core/audible/releases.py`, the lookup
functions in `libex_core/lookup/`, the optional storage in
`libex_core/storage/` (the store itself in `store.py`, with its reading in
`read/` and its writing in `write/`), and the command-line tool in
`libex_core/cli/`.

If you are using an application that contains this library, that
application's privacy policy is the one that applies to you. This section
covers only the part that belongs to Libex.

**Why it differs from the hosted service.** The public instance reaches Audible
from one address, shared by everyone who calls it. An embedded copy runs on
each user's own device. Without a proxy, Audible sees that device's address
together with the titles it looks up, the words searched for and the
categories browsed, which amounts to part of someone's reading history tied
to their home connection.
The library is built around preventing that from happening by accident:

- **It won't connect until someone decides how.** The client is created as
  `LibexClient(proxy_url=..., allow_direct_egress=...)`. `proxy_url` has no
  default, so leaving it out is an error. A blank proxy is refused unless
  `allow_direct_egress=True` is also passed. A malformed proxy URL raises an
  error rather than falling back to a direct connection. The library never
  looks for a proxy or supplies one of its own. This forces a decision but
  does not make one: an application that opts into direct connections sends
  its users' own addresses to Audible.
- **The environment can't redirect it.** It ignores `HTTPS_PROXY`,
  `ALL_PROXY`, `SSL_CERT_FILE` and `SSL_CERT_DIR`, so a setting left on a
  device by an employer, a local intercepting proxy or a forgotten tool cannot
  reroute its traffic or replace its trusted certificates.
- **Proxy credentials stay out of its errors and its own logs.** An error
  about an invalid proxy URL contains none of the value supplied. The only
  view of the connection settings it exposes is the mode and, for a proxy,
  its hostname and scheme. The proxy can be an HTTP, HTTPS or SOCKS5 proxy.
  Two things about SOCKS5 credentials are outside the library's control. A
  SOCKS5 username and password are sent to the proxy unencrypted, so anyone
  on the network between the device and the proxy can read them. The
  lookups themselves stay encrypted inside the tunnel to Audible. And the
  network library underneath, `httpcore`, writes the SOCKS5 username and
  password into its own debug log. That log is off unless the application
  turns on debug logging for `httpcore`, for example by setting all logging
  to debug. The `libex-core` command never does, even with `-vv`.
- **It talks only to Audible.** Requests go only to Audible's API host for the
  requested region. The path and the finished URL (host, scheme and port) are
  checked so that a crafted path cannot redirect the request elsewhere.
- **It adds nothing about the user.** Each request carries fixed headers: an
  Audible app user agent, the region's locale, and a random number in
  `X-ADP-SW` that is regenerated for every request. A quick search also
  sends a random session number, made fresh for each search, and the current
  time in UTC, which gives no time zone. The one constant that
  looks like a device ID is a device *type* ID, identical in every copy of
  Libex.
- **No telemetry.** It has no analytics, usage reporting, version check, crash
  reporting or any other call home. It cannot import the Axiom client, and a
  test fails if that ever changes.
- **Nothing is stored unless the application asks.** A lookup that isn't
  given a store writes no files, opens no database and keeps no cache, so
  nothing about it outlasts the call that made it. The library also has an
  optional storage part, `libex_core.storage`, installed separately as the
  `storage` extra, which can keep what was fetched in a SQLite file or a
  PostgreSQL database through `LocalStore`. Nothing in the library opens a
  store by itself: it does nothing until the application gives it a
  database's address, or a way of connecting to one, and the application
  decides where that database is.
  When the application passes that store to a lookup, the lookup writes what
  Audible returned into it (books, chapter lists, and series and author
  profiles) and returns the stored copy. A book or series is stored once for
  each region it is looked up in, so the same title looked up in two regions
  is kept twice. If Audible can't be reached, a lookup of a book, a chapter
  list, a series, an author, or a series' or an author's books answers from
  what the store already holds for that region. A search, a narrator's books,
  or a new releases or coming soon scan that Audible can't run is reported as
  Audible being unavailable rather than answered from the store, though books
  it found that Audible then can't give are filled in from the store where it
  holds them, and a quick search split into an author and a title tries the
  store's matching books when Audible's search for them fails or finds
  nothing. A chapter list is kept only for a book the store already holds for
  the same region. What is stored is Audible's
  catalogue data, the same kind the public instance's database holds, with no
  field about the person who looked it up. On someone's own device, though,
  the titles stored are the titles looked up, and every stored book, author,
  series, narrator, genre and chapter list records when it was first written
  and last updated, so a local store is part of that person's reading
  history, with dates. The database address, and any password in it, is
  never logged, repeated in an error, or shown in the store's `repr`.
  - **A SQLite file** is created only when the application runs the store's
    upgrade step (`libex-core db upgrade` on the command line). On Linux and
    macOS a new file is created readable and writable only by the user
    running the application (mode 0600). If the folder the file goes in is
    missing, that folder is created so only that user can open it (0700),
    but any missing folders above it get the system's usual permissions. A
    file that already exists is left as it is, with a warning, each time
    the store checks it, if other users have access to it. Libex does not
    encrypt the file, so unless the application opens it with an encrypted
    SQLite build (below), anything that can read it as that user can read
    what is in it.
  - **The set-up turns on write-ahead logging** for a SQLite file. Recent
    writes are kept in a second file beside the database, ending in `-wal`,
    with an index file ending in `-shm`, until they are moved into the main
    file. SQLite normally does that and removes both files when the last
    connection closes cleanly, and gives them the same permissions as the
    database file. That is SQLite's behaviour rather than Libex's code.
    Deleting only the main file can leave recent writes behind.
  - **For a PostgreSQL database**, unless the application makes the
    connection itself (below), the store connects to the address the
    application gives it, and refuses one that leaves out the host, the user
    or the database name. Its connection settings come from that address
    alone: it reads no `PG*` environment variable, no `~/.pgpass`, no
    PostgreSQL service file and nothing under `~/.postgresql`, and an address
    without a password connects without one.
  - **How a PostgreSQL connection is protected** is set by the address's
    `ssl` option. `verify-ca` and `verify-full` check the server's
    certificate against the system's trusted certificates. Unlike the
    Audible connection, that check takes its certificates from the
    `SSL_CERT_FILE` and `SSL_CERT_DIR` environment variables when they are
    set, so a setting left on the device can change which servers it
    trusts. `require` encrypts without checking who is at the other end. The
    default, `prefer`, does the same, but falls back to an unencrypted
    connection when the server refuses encryption. Someone able to interfere
    with the network between the device and the database can make it look as
    though the server refused, so `prefer` gives no protection against them;
    on a network that isn't trusted, use `verify-full`. A login that fails is
    never retried without encryption: only the server refusing encryption
    leads to the unencrypted attempt. `allow` tries unencrypted first and
    encrypts only if the server turns that down, and `disable` never
    encrypts.
  - **An application can make the connection itself.** `LocalStore` takes an
    optional `connect` function for a connection the library can't make, such
    as one through a tunnel or with a short-lived password. When it is used,
    the two points above no longer describe what happens: the library doesn't
    choose the PostgreSQL server, the login, the encryption or the certificate
    check, and can't see or check what the application chose. Unless the
    application prevents it, the PostgreSQL driver fills in anything left out
    from `PG*` environment variables, `~/.pgpass`, a PostgreSQL service file
    and the certificate and key files under `~/.postgresql`, and writes the
    connection's encryption keys to a file if `SSLKEYLOGFILE` is set. The
    address given to the store must then be the bare `postgresql+asyncpg://`,
    so no password is held in it. For SQLite, the library still refuses a symbolic link and
    creates a new file as described above, then hands that file's path to the
    application's function, which opens it; an application can use this to
    open an encrypted SQLite file, and in that case the file is encrypted
    however that application set it up, not by Libex. If the function fails,
    the error names only the kind of failure, never its message, which could
    contain a password. Either way the store logs one line saying the
    application supplies its connections, with nothing about where or as whom
    they connect.
  - **It refuses a database it didn't create.** A database with tables but
    none of this library's migration records, a hosted Libex database
    included, is refused and left untouched. Only the explicit upgrade step
    changes the tables.
  - **Searching the store.** With a store, a series search, and a quick
    search that is split into an author and a title, also look in the store
    using the words searched for. The words are used for that query and are
    not written to the store. A PostgreSQL server's own logs are configured
    by whoever runs it, and can record the queries it receives.
  - **Deleting it.** The library has no delete function, and the command-line
    tool has no delete command. To remove a SQLite store, delete the file
    together with any `-wal` and `-shm` files beside it, with nothing using
    it at the time. To remove a PostgreSQL store, drop its database or its
    tables. Turning storage off stops new writes but leaves what was stored
    in place. Copies made by backups or file-history tools are outside
    Libex.
- **The `libex-core` command reads its settings from the environment.** The
  command-line tool that comes with the library takes its proxy from
  `LIBEX_CORE_PROXY_URL`, its permission to connect directly from
  `LIBEX_CORE_ALLOW_DIRECT_EGRESS`, and where to store what it fetches from
  `LIBEX_CORE_STORAGE`, so a proxy or database password never has to be typed
  on a command line, where other programs on the machine can read it. To work
  out the default storage location it also reads the home directory and
  `XDG_DATA_HOME` (Linux and similar systems) or `LOCALAPPDATA` (Windows).
  No other part of the library's own code reads the environment, and a test
  fails if that changes. The one way the environment still reaches it is the
  PostgreSQL certificate check described above. The tool prints results as
  JSON to standard output. Errors, and the library's warnings listed below,
  go to standard error. Only each warning's message is printed there, not the
  details listed with it.
  `-v` adds the informational lines, `-vv` adds tracebacks, and `-q` leaves
  only the error line. It sends none of this anywhere else. Its lookup
  commands (books, chapters, series and their books, authors and their books,
  searches, narrators, new releases, coming soon and categories) send the
  ASINs, names, search terms and category IDs you give them to Audible, as
  described above, and print the answer. When an author's or a series' list
  of books may not be whole, `author books`, `author books-by-name` and
  `series books` also print one line to standard error naming the reasons,
  which come from a fixed list; `-q` leaves it out. A bulk lookup can read
  its list of ASINs from a file or standard input. That input is read once,
  is not kept, and its path is not repeated in an error. `libex-core config`
  prints only whether a proxy is in use and the proxy's hostname, says
  nothing about storage, and makes no request. Neither the tool's output nor
  its error messages contain the proxy URL, the storage setting, or the value
  of any of these variables.
- **The `libex-core` command stores nothing unless `LIBEX_CORE_STORAGE` is
  set.** Unset, empty or `off` is the default, and keeps nothing. `sqlite`
  uses a file named `libex.db` in a `libex-core` folder in the user's data
  directory:
  - Linux and similar systems: `$XDG_DATA_HOME/libex-core/libex.db`, or
    `~/.local/share/libex-core/libex.db` when `XDG_DATA_HOME` is not set to
    an absolute path;
  - macOS: `~/Library/Application Support/libex-core/libex.db`;
  - Windows: `%LOCALAPPDATA%\libex-core\libex.db`, or
    `~\AppData\Local\libex-core\libex.db` when `LOCALAPPDATA` is not set.

  An absolute path uses that SQLite file instead, and a `sqlite:///` or
  `postgresql://` URL uses that database. An in-memory database is refused.
  Nothing is created until `libex-core db upgrade` is run, and until then,
  with storage set, every lookup command stops before contacting Audible.
  Once the store is ready, every lookup command except `categories` writes
  what Audible returned into it and prints the stored copy, and the lookups
  described above answer from it when Audible can't be reached. Over time the store
  becomes an unencrypted, dated list of the books, authors and series that
  user looked up on that machine. The `db` commands and `book sku` read only
  the store, contact nobody, and need no proxy. To remove a store in the
  default location, delete `libex.db` and any `libex.db-wal` and
  `libex.db-shm` beside it, or the whole `libex-core` folder, as described
  under "Deleting it" above.
- **Its logs go where the application sends them.** It writes to the standard
  Python logger named `libex` and the loggers beneath it, such as
  `libex.storage`, so its records end up wherever the host
  application's logging is configured to send them. From fetching and
  reading Audible's answers:
  - closing a stale connection fails (debug): a traceback;
  - request throttled or degraded by Audible: status, region, API path (with the ASIN when one title, series or author is looked up), pool, attempt count, the wait Audible asked for;
  - malformed author ASIN: title ASIN, region, the malformed value, the author's name;
  - unreadable subscription plans: title ASIN, a count;
  - extra data cleaned up or held back: title or series ASIN, region, the reason, a count and, if oversized, its size; none of the data itself;
  - an author's book list from Audible's author page ended without a confirmed end: author ASIN, region, why it stopped, pages fetched, books found, Audible's own count, how much was cut off, and the error message if a page failed;
  - a search for an author's books by name lost its first page: region, the error message. Never the name;
  - a search for an author's books by name lost a later page: region, the page number, books found so far, the error message. Never the name.

  The subscription-plan and extra-data records log at most once a minute
  (extras, once a minute per reason), naming only the latest title.

  From the lookup functions:
  - a completed lookup (info): region, how long Audible took and, except for a single chapters, series or author lookup or an author search, counts of what was asked for and what came back, including, for books, how many were answered from the store; for a search, the names of the fields searched; for a quick search, the length of the text and, when a compound query was split, how many parts it made; for an author's books by ASIN, the author ASIN, how each of the two ways of finding them went, and whether the list is whole; for new releases and coming soon, the number of days and the category ID, if one was given;
  - an author's books by name that stopped before a confirmed end (info): region, why it stopped, pages fetched, books found;
  - a lookup Audible only partly answered (warning): region, counts, and the kind of error where one occurred, including how many identifiers were skipped for not being ASINs; for an author's books by ASIN, also the author ASIN and the error messages from the parts that failed;
  - Audible unavailable (warning, or error for a book or narrator search, a quick search, or a new releases or coming soon scan): region; except for a book lookup, the kind of error; except for a book lookup, a book or narrator search, a quick search or an author's books by ASIN, Audible's status where there was one; for a chapters, series, series' books, author or author's books lookup, the ASIN asked for, and for an author's books by ASIN, the error messages from the parts that failed and whether reading the store failed; for a search for a series or an author, or for an author's books by name, the length of the name; for new releases and coming soon, the category ID.

  From a local store, which is used only when an application passes one to a lookup or uses the storage part directly (on the command line, only when `LIBEX_CORE_STORAGE` is set):
  - books, a chapter list, a series or an author written (info): what was written, region, and for books, how many;
  - a chapter list not stored because the store doesn't hold its book for that region (info): title ASIN, region;
  - a series profile not stored because it has no ASIN, name or region (info): what was being written, the reason, which is a fixed phrase, and the region;
  - a write that failed (warning; the answer from Audible is still returned): what was being written, the kind of error, region, and for books, how many were in the batch;
  - a read that failed, which is treated as nothing stored (warning): what was being read, the kind of error. This includes the check, before a chapter list is stored, that its book is in the store; when that check fails, nothing is written;
  - an answer given from the store in place of Audible (warning, always "Answered from the store while Audible was unavailable"): what kind of answer it was and the region; for a chapter list, a series or an author, its ASIN; for a series' books, the series ASIN and how many books the store held; for books, how many were asked for and how many the store held; for a quick search split into an author and a title, how many stored books matched and whether Audible's search failed, since the same line is written when Audible's search worked but found nothing;
  - an existing SQLite file that other users have access to, whenever the store checks the file (opening it, upgrading it or asking its status) (warning): the file's name, not its folder;
  - an upgrade that finished (info): the database type, the revision reached and, for SQLite, whether the file was new and its name, not its folder;
  - a SQLite file that stayed locked while being switched to write-ahead logging (warning): the file's name, not its folder, and how long the store waited;
  - more than one stored row found for an author (warning): author ASIN, region, the number of rows;
  - unreadable publication date while storing a title (warning): title ASIN and the kind of value, not the value.

  Running the store's upgrade step also lets Alembic, the migration tool it uses, write routine lines under its own logger, `alembic`: the database type and the schema changes it applies. Those lines are Alembic's and contain no database address, no stored data and nothing about lookups.

  From the command-line tool:
  - a `libex-core` command fails (debug): a traceback;
  - an author's or a series' list of books may not be whole (warning): the reasons, which come from a fixed list.

  From the caller, these records take the region, the looked-up title, series
  or author ASIN, the names of searched fields, the length of quick-search
  text or of a searched name, the number of days and the category ID
  browsed, and how many items were asked for. Search text, and any name
  searched for, are never logged: query parameters, where they would appear,
  are left out, a looked-up ASIN is logged only once it has been checked to
  be one, a category ID only once it has been checked to be a number, and in
  the records from Audible's answers any ASIN, name or value not shaped like
  an ASIN or a short catalogue entry is logged as `REDACTED`. Most local-store
  records hold only fixed words, counts and the region. Of those that hold an
  ASIN, the chapter-list record and the record of an answer from the store
  log one that has already been checked, the author-row record logs its
  author ASIN and region only once both have matched rows already in the
  store, and the publication-date record takes its title ASIN from the data
  being stored, without the `REDACTED` check. The records that name a SQLite
  file give only the name the application chose for it, never its folder.
  An error message is whatever the request function raised. The library's
  own client builds its messages from Audible's host, the API path (which
  holds the ASIN when one title, series or author is looked up) and the
  network library's own error text, never the query string. An application
  that passes in a request function of its own decides what its messages
  contain. A title, series or author ASIN, or a category browsed, is still
  something someone looked up or searched for, so
  these records are part of their reading history.

An application that includes the library still has to answer three questions
for its own users. Does it connect directly or through a proxy, and if through
a proxy, who runs it? A proxy changes who sees the lookups rather than hiding
them. Does it keep what was looked up? Does it send its logs anywhere? If you
ship the library in something other people use, those answers, and the privacy
notice that states them, are yours.

## Changes

This notice is kept in the Libex repository, so every revision is in its public
history. When what is collected, or who receives it, changes, this notice is
updated in the same change as the code.
