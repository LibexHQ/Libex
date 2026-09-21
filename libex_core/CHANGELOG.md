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
