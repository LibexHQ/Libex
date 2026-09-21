"""
Builds the one hosted LibexClient instance, and the audible_get delegator
every call site under this package resolves it through, before anything
under this package can reach Audible.

Every hosted path to Audible -- a book lookup, an author walk, a series
fetch, a search, the seeder -- imports some module somewhere under
app.services.audible, and every one of those modules' own top-level
`from app.services.audible import audible_get` runs only after Python has
finished initializing this package in full: importing a submodule always
imports its parent package to completion first, so there is no ordering in
which a submodule's import line could run ahead of this one. That is also
what makes it safe for this file to hold both the instance and the
delegator that reaches it by lookup on every call, despite every submodule
under this package importing from it: the reverse edge that would turn
this into a real import cycle would require this file to import one of
those submodules, and it does not -- checked directly against every file
in this package, not assumed.

libex_core itself never reads settings or the environment; this is the one
seam where the hosted application's own configuration -- audible_proxy_url,
read through app.core.config -- crosses into it.

A malformed AUDIBLE_PROXY_URL makes LibexClient's constructor raise here,
which means it still raises at import, before uvicorn ever starts serving a
request. What that becomes downstream depends on the process model. A
single-worker process and the seeder container exit on the raise, and
restart-unless-stopped turns that into a crash loop; the operator script
containers exit once and stay exited, because docker run starts them with
Docker's default restart policy of "no". The multi-worker API container
does neither: measured at WEB_CONCURRENCY=6, uvicorn 0.46.0's own
supervisor respawns each worker that dies on import every 0.5s
indefinitely, burning roughly four cores doing it, so the container itself
stays "running" with RestartCount=0 and health unhealthy -- Docker's
restart policy never engages, because nothing ever exits for it to notice.
Either way port 3333 is never bound, so callers get connection-refused
rather than a request quietly going out over the wrong egress -- the
fail-closed behaviour a malformed value is meant to force holds under both
process models; only its outward shape differs.

allow_direct_egress=True is passed unconditionally, not only when
audible_proxy_url is blank. LibexClient's constructor ignores it whenever a
proxy URL is actually supplied (see its own docstring), so this changes
nothing about the proxied case; it exists to cover the blank one. Leaving
AUDIBLE_PROXY_URL unset is a documented way to run hosted Libex
(README.md's self-hosting instructions) and was already ruled a deliberate
deployment choice, not an oversight libex_core should second-guess -- so
this seam is where that ruling gets encoded as the one explicit opt-in
libex_core now requires before it will egress unproxied. libex_core cannot
make that call itself: an embedder distributed across many separately
operated machines has no single operator to make it on their behalf, and a
blank proxy setting there is exactly as likely to be a forgotten one as a
deliberate one. Hosted Libex is the opposite -- one operator, one setting,
already read and already validated by the checks above -- so the decision
already exists by the time this line runs; passing the flag only tells
libex_core the decision was actually made rather than defaulted into.
Because the flag is always on, nothing about this line makes LibexClient's
blank-proxy refusal reachable from a hosted deployment: a hosted process
still either gets a valid proxy, a validated direct choice, or the
malformed-URL raise that already existed above.

_hosted_client below is this package's one hosted LibexClient instance --
there is exactly one, for the lifetime of the process, and nothing in this
package ever constructs a second one or reassigns this name. audible_get
resolves it by ordinary module-global lookup on every call rather than
closing over a copy taken at import, so there is never a second binding of
the instance itself sitting in some other module's namespace to drift from
this one -- only the audible_get function object is handed out, and every
call it makes still goes through this exact instance. The three operator
scripts under scripts/ read it directly as
app.services.audible._hosted_client, the same deliberate single-underscore
reach-in LibexClient._proxy exists to support (see that property's own
docstring) -- confirming, from outside this process's request path, which
transport a running instance actually holds. That reach-in is deliberate
and documented, not an oversight this module should "fix" by hiding the
name further.
"""

# Standard library
from typing import Any

# Core
from libex_core.audible.client import LibexClient
from app.core.config import get_settings

_hosted_client = LibexClient(
    proxy_url=get_settings().audible_proxy_url,
    allow_direct_egress=True,
)


async def audible_get(
    region: str,
    path: str,
    params: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """
    Forwards to _hosted_client above, this package's one hosted LibexClient
    instance. Every book, author, series, search and release fetch under
    this package, and the seeder's own discovery and expansion, calls this
    function rather than constructing or holding a LibexClient of its own --
    audible_get is the single name every one of those call sites resolves
    the hosted instance through, by ordinary module-global lookup on every
    call rather than a reference captured once at import (see this
    package's own docstring for why that matters).
    """
    return await _hosted_client.get(region, path, params, extra_headers)
