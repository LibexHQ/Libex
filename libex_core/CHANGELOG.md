# Changelog

All notable changes to `libex_core` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
`libex_core` is not yet on 1.0, so this project borrows post-1.0 MAJOR
discipline into the MINOR slot rather than relying on SemVer's 0.x carve-out:
while pre-1.0, MINOR carries any breaking change, and PATCH is reserved for
fixes that have no effect on the package's public surface.

`libex_core` is not yet published to PyPI. It is packaged for it as the
`libex-core` distribution, and the first publish will be a 0.x release.
Entries below that predate publication are historical record for whoever
embeds this package, not evidence that anyone consumed a given version at the
time it was cut.

## [0.14.0]

### Added
- **The package can now look Audible data up, from code and from the command line.** A new public module, `libex_core.lookup`, has ten async functions: `get_book`, `get_books`, `get_chapters`, `get_series`, `get_series_books`, `search`, `quick_search`, `abs_search`, `abs_quick_search` and `narrator_books`. Each takes the request callable (an `AudibleGet`) first, then its arguments, then `region` as a keyword defaulting to `us`, and returns the published model the hosted route returns for the same Audible answer (`BookResponse`, `BulkBookResponse`, `ChapterResponse`, `SeriesResponse`, `AbsSearchResponse`, or a list of `BookResponse`). They are the hosted live path without the cache or the database: Audible's answer is normalized and settled, and nothing is stored. Earlier entries said the command line did not fetch Audible data and had no lookup commands; it now does.
- **Audible being unreachable is reported as an outage, never answered from a stored copy and never passed off as an empty result.** Because there is no stored copy to fall back on, a lookup that could not reach Audible raises `AudibleAPIException`, and `get_books` instead lists the affected ASINs in `notFetched` when other ASINs in the same call did come back. Audible confirming it has no record is `NotFoundException` (a bulk lookup lists the ASIN in `notFound`), and is kept apart from an outage. A search or narrator lookup that matches nothing is also `NotFoundException`. The compound `Author - Series - Title` fallback in `quick_search` searches Audible only; the stored-books step the hosted service adds after it is not part of the library.
- **Bulk lookups keep the hosted accounting.** `get_books` takes up to 1000 ASINs, each entry optionally comma-separated, and rejects the whole call with `NotFoundException` (code `invalid_request`) for an empty list, more than 1000, or any value that is not an ASIN. `notFound`, `placeholderRecords` and `notFetched` return the ASINs as the caller wrote them, in request order, and no ASIN appears in more than one of them or in `books`. A record Audible sent only as a placeholder is never returned as a book: `get_book` raises `NotFoundException` with code `withheld` for it, and `get_books` lists it in `placeholderRecords`. `get_series_books` leaves out members it cannot resolve and can return an empty list.
- **Paged lookups refuse out-of-range paging instead of clamping it.** `search` and `narrator_books` raise `ValueError` for a `limit` outside 1 to 50 or a `page` outside 0 to 9. The Audiobookshelf-shaped searches return at most five matches, and for them an unknown region is `NotFoundException` with code `invalid_request` rather than `RegionException`, as on the hosted routes. Search text and ASINs the caller typed are never repeated in an exception message or a log line.
- **New `libex-core` commands: `book get`, `book bulk`, `book chapters`, `series get`, `series books`, `search`, `quick-search`, `abs search`, `abs quick-search` and `narrator books`.** Each prints the result as JSON on standard output and takes `--region`, one of the eleven regions, default `us`. `search` takes `--title`, `--author`, `--narrator`, `--publisher`, `--keywords`, `--query` and `--sort-by`; `search` and `narrator books` take `--limit` (1 to 50, default 10) and `--page` (0 to 9, default 0). `book bulk` takes ASINs as arguments, from `--file PATH` (or `-` for standard input), or both, separated by commas or white space; a file larger than 1 MiB, or one that cannot be read as text, is refused as a usage error without repeating its path. These commands need the same proxy configuration as `libex-core config` reports: with neither `LIBEX_CORE_PROXY_URL` nor `LIBEX_CORE_ALLOW_DIRECT_EGRESS` set they exit 5 and send nothing.
- **Exit statuses follow the existing contract.** A lookup that succeeds exits 0, including a bulk result that is only partly complete: it is printed whole, with the ASINs not found, withheld or not fetched listed beside the books. A rejected argument or ASIN is 2, nothing found or no chapter list is 3 (many records have no chapter list), and Audible being unreachable is 4. No status was reassigned.

### Changed
- **The generated shell completions now complete the flags and values of the leaf commands.** Bash, zsh and fish previously completed only the top-level words; they now also complete nested commands (`book get`, `abs search`, and so on), each command's own flags, and the region names after `--region`.
- **The man page now documents each nested command under its full name**, such as `book get`, and escapes the hyphens in command names so they render and search as typed.

## [0.10.0]

### Added
- **The package can now filter and sort a list of book dictionaries.** A new public module, `libex_core.shaping`, exposes `filter_dicts` and `sort_dicts`, the in-memory filtering and sorting the live book lists use, with no database or web framework involved. `filter_dicts(items, filters)` takes a dictionary of filter name to value, ignores `None` values and unknown names, keeps the input order, and returns the input list itself when no filter is active. `sort_dicts(items, sort, order, allowed)` returns the list unchanged when `sort` is empty or not in `allowed`; otherwise it sorts ascending, or descending when `order` is `desc`, and books missing the field or holding `None` for it go to the end in either direction. `allowed` is any container of field names, such as a tuple or the keys of a dictionary; only membership is tested.
- **The filter and sort surface is published as data, so a front end can build its parameters from it.** `BOOK_FILTER_SPECS` is a tuple of `FilterSpec` entries (`name`, `type`, `description`) for the twelve filters: `language`, `book_format`, `explicit`, `whisper_sync`, `has_pdf`, `is_vvab`, `plan_name`, `rating_better_than`, `rating_worse_than`, `longer_than`, `shorter_than` and `genre`. `BOOK_FILTER_FIELDS` is the set of their names. `BOOK_SORT_FIELDS` is the tuple of sortable book fields (`title`, `releaseDate`, `rating`, `lengthMinutes`, `language`, `publisher`, `updatedAt`), `BookSortField` is an enum with exactly those members, and `SortOrder` is the `asc`/`desc` enum.
- **Filters are limited to what is cheap on a list already in memory.** Numeric ranges, equality on the format and boolean fields, plan membership, and a case-insensitive partial match on genre names. Free-text search on title or description is not offered. A book missing the field a range filter targets is excluded by that filter, so a book with no length is never "longer than" anything.

## [0.9.0]

### Added
- **The package can now search Audible, not only fetch by ASIN.** A new public module, `libex_core.audible.search`, exposes `build_search_params`, `fetch_search_products` and `fetch_suggestion_asins`, plus the constants `SEARCH_PATH`, `SEARCH_SUGGESTIONS_PATH` and `MAX_SEARCH_RESULTS` (50). `build_search_params` takes the filters (`title`, `author`, `keywords`, `narrator`, `publisher`, `products_sort_by`) and `limit` and `page`, keyword-only, and returns the parameters of a catalog search; a filter that is missing or an empty string is left out, and no filter at all is allowed. `fetch_search_products` sends them and returns the matched products raw, with the response groups and image sizes added so full product metadata comes back in one call. `fetch_suggestion_asins` asks Audible's search suggestions what a partial query resolves to and returns the ASINs of the book rows in the order Audible gave them, unvalidated. As with the other fetch functions, the request callable is the first argument, and the region is checked first (`RegionException` for one of the eleven it is not).
- **`build_search_params` raises `ValueError` for a `limit` outside 1 to `MAX_SEARCH_RESULTS` or a negative `page`.** The message names the bounds and never repeats any search text; the package neither inspects nor logs search text. Earlier hosted behaviour silently clamped an over-large `limit` to 50; the package does not clamp, it refuses. Audible stops returning results past page 9, and that is not enforced here: a `page` above 9 is accepted and sent.
- **The Audiobookshelf custom-metadata-provider models are now public in `libex_core.models`.** `AbsSeriesRef`, `AbsBookResponse` and `AbsSearchResponse` describe that format, which is deliberately narrower than the AudiMeta-derived shapes; the module docstring now says they are not derived from AudiMeta. `to_abs_book` converts a normalized book dictionary into an `AbsBookResponse`. Field names, optionality and defaults are unchanged from the hosted service's copy.

## [0.8.0]

### Added
- **`BulkBookResponse.notFetched`, a list of the requested ASINs that could not be looked up because Audible was unreachable and no stored or cached copy covered them.** It defaults to an empty list, so code that builds a `BulkBookResponse` without it is unaffected, and it serializes always-present like `placeholderRecords`.

### Changed
- **The documented meaning of `BulkBookResponse.notFound` narrows to ASINs Audible confirmed it has no record of.** The field, its type and its position are unchanged, but it no longer holds ASINs that could not be fetched; those belong in `notFetched`. An embedder that fills `notFound` itself and treated it as "missing or unfetched" should move the unfetched ones to `notFetched`. The field description (visible in the generated schema) was reworded to match.

## [0.7.0]

### Added
- **The package now ships a `libex-core` command line.** It is installed as the `libex-core` script and also runs as `python -m libex_core`. Two commands exist so far: `libex-core config` prints, as JSON, whether requests would go through a proxy or leave directly, and the proxy host (never the proxy URL, and no request is made); `libex-core completion bash|zsh|fish` prints a completion script. Neither fetches Audible data yet. `--help` and `--version` work everywhere. By default the library's warnings print to standard error with no flag, `-q` prints only the final error line, `-v` asks for more detail (the package emits no INFO-level records today, so it adds nothing yet), and `-vv` adds debug output with tracebacks. Results go to standard output; logs and errors go to standard error.
- **The command line's exit status says what kind of failure it was.** 0 success, 1 unexpected error, 2 bad usage or a rejected argument, 3 not found, 4 Audible unavailable, 5 bad environment configuration, 130 interrupted, 141 the reader of standard output went away. A `LibexException` maps by its `code`: `invalid_request` is 2, `not_on_audible`, `not_in_libex` and `withheld` are 3, `upstream_unavailable` is 4, and a code added later that has no mapping yet is 1. The error line on standard error ends with the code; `config_error` and `unexpected_error` are the command line's own and are never raised by the library. These numbers are a public contract and will not be reassigned.
- **The command line reads two environment variables.** `LIBEX_CORE_PROXY_URL` is the http or https proxy every request goes through; it is an environment variable and not an option because it can carry credentials. `LIBEX_CORE_ALLOW_DIRECT_EGRESS` (`1`, `true`, `yes` or `on`; `0`, `false`, `no`, `off` or empty to refuse) lets requests leave from the machine's own address when no proxy is set. Any other value is a configuration error (exit 5), reported even when a proxy is set.
- **A man page and shell completions install with the wheel**, under the environment prefix.
- **The package is now buildable and publishable as `libex-core`.** It declares `httpx` (`>=0.28.1,<0.29`) and `pydantic` (`>=2.13.4,<3`) as its dependencies, requires Python 3.12 or later, and ships a `py.typed` marker so type checkers use its annotations.

### Changed
- **The library still reads no environment variable.** The command line's environment module is the only code in the package that touches the process environment, and only when a configuration is requested, never at import. An embedder's own environment cannot change what the library does. The package docstring now states this.

## [0.6.0]

### Added
- **The package can now fetch and normalize Audible books, chapters and series, not only describe them.** New public modules: `libex_core.audible.books` (`fetch_products`, `normalize_product`, `normalize_products`, `settle_flags`, `settle_flags_list`, `filter_products`, `is_placeholder_record`, and the response-group and image-size constants), `libex_core.audible.extras` (`build_extras`), `libex_core.audible.chapters` (`fetch_chapter_metadata`, `has_chapter_info`, `normalize_chapters`, `CHAPTERS_RESPONSE_GROUPS`) and `libex_core.audible.series` (`fetch_series`, `fetch_series_book_asins`, `normalize_series`). Normalized output is the same, key for key, as hosted Libex produced before the move; it is pinned by golden files. Earlier entries said this package did not fetch or normalize a product; that no longer holds.
- **Every fetch function takes the request callable as its first argument.** `AudibleGet`, a new `Protocol` in `libex_core.audible.client`, describes it, so how a request leaves the process stays the embedder's choice. The fetch functions check the region first (`RegionException` for one of the eleven it is not) and every ASIN (`ValueError`, with a message that never repeats the rejected value) before anything is sent. `validated_asin` is exposed for the same check; it accepts a lowercase ASIN and returns it uppercased, which is also the form the fetch functions send. Hosted Libex screens out values that are not ASINs before it calls `fetch_products`, so one bad value does not reject the rest of a batch; the core itself still raises `ValueError` for the whole call. `fetch_products` accepts at most 50 ASINs per call; it does not split a longer list for you.
- **`libex_core.log_safety`** exposes `is_safe_log_value`, `safe_asin_for_log` and `window_elapsed`, the checks the package uses to keep caller- or Audible-supplied text out of log lines and to rate-limit repeated warnings. `is_safe_log_value` is the same rule hosted Libex used; it is no longer importable from hosted Libex's own logging module.

### Changed
- **The transport's concurrency and retry internals moved into private modules.** Nothing public was removed: `author_books_concurrency` is still importable from `libex_core.audible.client`. Code that reached into the transport's other internals, such as its semaphore or its concurrency limit, now finds them in `libex_core.audible._concurrency`, a private module with no stability promise.
- **The malformed-author-ASIN warning redacts unsafe values.** It used to log the book's ASIN, the malformed author ASIN and the author name verbatim. The book's ASIN is now `REDACTED` unless it is a well-formed ASIN; the malformed author ASIN and the author name are logged as-is only if `is_safe_log_value` accepts them (short catalogue text), otherwise `REDACTED`. Other warnings naming an ASIN from Audible use the same well-formed-ASIN rule.

## [0.5.0]

### Added
- **New public `ErrorCode` enum, and every `LibexException` gains a `code` attribute.** `ErrorCode` is a `StrEnum` with `NOT_IN_LIBEX`, `NOT_ON_AUDIBLE`, `WITHHELD`, `UPSTREAM_UNAVAILABLE` and `INVALID_REQUEST`, its values being the lowercase names. Each exception class has a default: `NotFoundException` is `NOT_ON_AUDIBLE`, `RegionException` is `INVALID_REQUEST`, and `AudibleAPIException`, `CacheException` and the base `LibexException` are `UPSTREAM_UNAVAILABLE`. A raise site can override it with a new optional `code=` argument. Existing raises keep working untouched and keep their messages and status codes; the argument is keyword-optional and last, so no positional call changes meaning.

## [0.4.1]

### Changed
- **The package and its response models are no longer described as a drop-in AudiMeta replacement.** The shapes are derived from AudiMeta's and differ from them in places; the package docstring and the models module now say so. Documentation only: no model, field, default or function changed.

## [0.4.0]

### Added
- **`BulkBookResponse` gains `placeholderRecords: list[str]`, defaulting to an empty list.** Existing construction sites keep working untouched. What moves for every embedder is the serialized shape: a dump of `BulkBookResponse` now carries a `placeholderRecords` key, `[]` unless something supplied it, so a golden file or snapshot compared against a dump will differ on upgrade. That is why this is a MINOR rather than a PATCH. The field means requested ASINs for which Audible returned a record carrying its 2200-01-01 placeholder publication date, deliberately left out of `books` and never also in `notFound`. Nothing in this package fills it; the embedder decides what goes in.

### Changed
- **The `notFound` field description on `BulkBookResponse` is reworded; its type and default are unchanged.** It now points placeholder records at `placeholderRecords`, and says the list can also hold ASINs that could not be fetched in this request and had no stored copy. This changes the schema's description text in generated OpenAPI, nothing else.

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
