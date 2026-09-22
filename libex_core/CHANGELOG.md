# Changelog

All notable changes to `libex_core` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
`libex_core` is not yet on 1.0, so this project borrows post-1.0 MAJOR
discipline into the MINOR slot rather than relying on SemVer's 0.x carve-out:
while pre-1.0, MINOR carries any breaking change, and PATCH is reserved for
fixes that have no effect on the package's public surface.

`libex_core` is not published to PyPI and will not be until it is extracted
into its own distribution. Entries below that predate publication are
historical record for whoever embeds this package later, not evidence that
anyone consumed a given version at the time it was cut.

## [0.3.0]

### Added
- **`BookResponse` gains eight optional fields: `numRatings`, `numReviews`,
  `publicationName`, `publicationDatetime`, `extendedProductDescription`,
  `productState`, `audibleExtras` and `extrasWithheld`.** Every one defaults
  to `None`, so existing construction sites keep working untouched. What
  does move for every embedder is the serialized shape: pydantic emits all
  eight by default, so a dump of `BookResponse` — and of
  `BulkBookResponse`, which contains it — now carries eight more keys,
  `null` unless something supplied them. A golden file or snapshot compared
  against a dump will differ on upgrade, which is the whole reason this is
  a MINOR rather than a PATCH.

- **Nothing in this package fills them.** `libex_core` defines the response
  shape; it does not fetch an Audible product or normalize one into this
  model. An embedder constructing a `BookResponse` decides what goes in
  these fields, so the contracts below are what the shape *means*, not
  behaviour the package performs. `numRatings` and `numReviews` are the
  counts behind the `rating` average, which on its own cannot distinguish
  4.8 from three ratings from 4.8 from two hundred thousand.
  `publicationName` and `publicationDatetime` place a periodical or podcast
  episode in its publication; the datetime is a full instant with a literal
  trailing `Z`, distinct from `releaseDate`, which is a bare calendar date.
  `extendedProductDescription` is the long-form description with its markup
  intact — `description` and `summary` are the flattened ones — and is
  upstream HTML, neither validated nor rewritten here, so encode it on
  output. `productState` is Audible's own state string; match it as an
  opaque string rather than modelling it as an enum, because the vocabulary
  is Audible's and can grow without notice.

- **`audibleExtras` is defined as a verbatim catch-all, and three details of
  that definition bind anyone who populates or reads it.** It holds every
  top-level key of Audible's product response that the named fields do not
  already reproduce, so a key Audible invents later surfaces on its own
  rather than disappearing between the fetch and the response. Verbatim
  means value for value, not byte for byte: the content is parsed JSON, so
  key order is not preserved, duplicate keys are already collapsed, and
  numeric spelling is normalized (`1e3` arrives as `1000.0`). Nothing in it
  is ever hoisted to the top level — no splat, no key set at runtime —
  which is what makes an upstream key named `asin` structurally unable to
  collide with the first-class field of that name; it stays nested and is
  read there. And it is tri-state: `null` means nothing was captured, `{}`
  means the product carried nothing beyond the named fields, and an object
  is content. Treat every value in it as untrusted input, including the
  URLs it will contain.

- **`extrasWithheld` is the record of what was left out of `audibleExtras`
  and why, so an omission is stated rather than inferred.** It sits beside
  the blob rather than inside it deliberately: the blob is documented as
  Audible's keys only, and a key invented here would collide with a real
  upstream one the day Audible ships a field by that name. Unlike
  `audibleExtras` it is not tri-state — `None` means nothing was withheld,
  and draws no distinction between an intact blob and no blob at all.

## [0.2.0]

### Security
- **`get_audible_url()` now rejects a `.` or `..` path segment anywhere
  between the path's slashes, not only a dot segment at the very front.** The
  existing guard only inspected the path's leading character, so a path like
  `/1.0/catalog/products/../../internal` passed it untouched; httpx then
  applies ordinary RFC 3986 dot-segment removal while parsing the URL this
  function builds, silently collapsing that path to `/1.0/internal` — an
  endpoint this function never intended to address. The guard that checks the
  finished URL's host, scheme and port could not catch it either, because
  none of those three change when only the path collapses. A segment that is
  `.` or `..`, one whose percent-encoding (`%2e`/`%2E`) decodes to either of
  those, or one containing an encoded separator — `%2f`/`%2F` for `/`, and
  now `%5c`/`%5C` for the backslash the guard already rejected in its literal
  form — raises `ValueError`. This is a breaking change for a caller of
  `LibexClient.get()` that was constructing a path containing one of these;
  no call site in this codebase ever has, every one interpolating a single
  bare id. The encoded spellings rest on weaker ground than the literal one
  and are refused anyway: httpx never turns an encoded separator or an
  encoded dot into a live one before the request leaves this process, so
  those forms are rejected for what a server might do with them after
  decoding, which is not something this library can see or verify.
- **A path containing `?` or `#` is now rejected outright, whether or not a
  dot segment is visible in it.** This is the change most likely to reach an
  existing caller: code that inlined a query string into the `path` argument,
  rather than passing `get()`'s own `params`, now raises `ValueError` where it
  previously made a request. The reasoning is not the one above. Neither
  character belongs to the path component — each one ends it — so whatever
  sits immediately in front of one is the path's real final segment, which is
  somewhere a check that splits on `/` never looks. When that segment was `.`
  or `..`, the collapse had already happened in the bytes leaving this
  process: `/1.0/catalog/products/..?x` was transmitted with a path of
  `/1.0/catalog?x`, a level above the prefix the caller asked for, and
  `/1.0/catalog/products/.#x` as `/1.0/catalog/products`, the fragment never
  reaching a server at all. That is measured behaviour of this library, not an
  assumption about anybody else's. Refusing the two characters removes the
  whole shape rather than the individual spellings of it, and costs nothing
  legitimate: query parameters belong in `get()`'s `params` argument, which
  httpx appends to the URL this function returns, and a fragment is never sent
  to a server in the first place.
- **What this guard does not cover, deliberately.** It compares segments
  against fixed spellings rather than decoding them, so double- and
  nested-encoded forms (`%252e%252e`), unicode normalization, and overlong
  encodings such as `%c0%af` are still accepted, and no claim is made that
  they are caught. None of them decode into a separator in httpx before a
  request leaves this process, nothing in this package produces one, and
  matching every possible spelling of a dot is an arms race with no end. Read
  the guard as closing the shapes named above, not as general input
  sanitisation.

## [0.1.0]

First tracked version of `libex_core`, and the first entry in this file.
Everything below shipped in the same change that started this line, which is
why the line opens with a break rather than growing into one later.

### Removed
- **The module-level `configure_transport()` and `audible_get()` functions,
  and the process-wide transport state behind them, are gone.** Egress used
  to be a single, mutable choice for the whole process: call
  `configure_transport()` once at import, and every subsequent call to the
  module-level `audible_get()` read back whatever it had set. There is no
  compatibility shim for either name — code built against them breaks
  immediately on upgrade rather than continuing to work with a deprecation
  warning.

### Added
- **`LibexClient` replaces both: one instance per caller, whose transport is
  decided once at construction and never replaced for that instance's whole
  life.** Construct one with `LibexClient(proxy_url=..., allow_direct_egress=...)`
  and call its `get(region, path, ...)` method wherever code used to call the
  module-level `audible_get()`. `proxy_url` has no default — omitting it is a
  `TypeError` from Python's own argument checking, not a state this library
  has to notice and refuse at request time, so "nobody ever decided how this
  instance egresses" is not a state a `LibexClient` can be in. A blank or
  missing `proxy_url` still needs a separate `allow_direct_egress=True` to
  mean "leave on this machine's own address" — otherwise it's refused,
  because an empty proxy setting is indistinguishable, at the wire, from one
  simply left unset by mistake. That guard carries over unchanged from
  `configure_transport()`; only its scope moves from once-per-process to
  once-per-instance.
- **An instance can be closed permanently**, with `await client.aclose()` or
  by using it as `async with LibexClient(...) as client:`. Closing is
  terminal: every `get()` call afterward raises `RuntimeError` instead of
  silently rebuilding a client and egressing again, and a second `aclose()`
  is a no-op. `is_open` reports whether a live HTTP client currently exists
  for the instance — it reads `False` both before the first request and
  after `aclose()`, deliberately not the complement of the underlying HTTP
  library's own closed-state check.
- Concurrency, retry and backoff limits stay process-wide rather than
  becoming per-instance state: every `LibexClient` built in the same process
  still shares one exit IP and draws from the same two concurrency pools,
  regardless of how many instances exist.

### Security
- **A crafted request path could have redirected a request to a host other
  than Audible's own.** The URL builder concatenated a caller-supplied path
  directly onto the Audible hostname with no separator guarantee, so a path
  starting with `@` could turn the rest of the string into userinfo and push
  the intended host aside — a path like `"@evil.example/x"` resolved to
  `evil.example`, not Audible. A path is now rejected outright unless it
  starts with a single `/`, contains no backslash, and the finished URL
  still resolves to exactly the intended host, scheme, and port. This starts
  to matter only from this release on, because `get()` is the first published
  method that takes a path directly from whoever is embedding the library —
  every caller of the equivalent internal function before it always passed a
  fixed path, so the bug existed but had no way to be reached.
