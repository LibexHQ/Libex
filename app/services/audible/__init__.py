"""
Configures the Audible transport before anything under this package can
reach it.

Every hosted path to Audible -- a book lookup, an author walk, a series
fetch, a search, the seeder -- imports some module somewhere under
app.services.audible, and Python always finishes initializing a package
before it runs any of that package's own submodules. Making the one call to
libex_core.audible.client.configure_transport() here, at this package's own
import time, is what guarantees it has already run by the time any of those
submodules could possibly call audible_get, without a "has this been
configured yet" check threaded through every one of them individually.

libex_core itself never reads settings or the environment; this is the one
seam where the hosted application's own configuration -- audible_proxy_url,
read through app.core.config -- crosses into it.

A malformed AUDIBLE_PROXY_URL makes configure_transport raise here, which
means it raises at import, before uvicorn ever starts serving a request. What
that becomes downstream depends on the process model. A single-worker process
and the seeder container exit on the raise, and restart-unless-stopped turns
that into a crash loop; the operator script containers exit once and stay
exited, because docker run starts them with Docker's default restart policy
of "no". The multi-worker API container does neither: measured at
WEB_CONCURRENCY=6, uvicorn 0.46.0's own supervisor respawns each worker that
dies on import every 0.5s indefinitely, burning roughly four cores doing it,
so the container itself stays "running" with RestartCount=0 and health
unhealthy -- Docker's restart policy never engages, because nothing ever
exits for it to notice. Either way port 3333 is never bound, so callers get
connection-refused rather than a request quietly going out over the wrong
egress -- the fail-closed behaviour a malformed value is meant to force holds
under both process models; only its outward shape differs.

allow_direct_egress=True is passed unconditionally, not only when
audible_proxy_url is blank. configure_transport() ignores it whenever a
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
Because the flag is always on, nothing about this line makes
configure_transport's blank-proxy refusal reachable from a hosted
deployment: a hosted process still either gets a valid proxy, a validated
direct choice, or the malformed-URL raise that already existed above.
"""

# Core
from libex_core.audible.client import configure_transport
from app.core.config import get_settings

configure_transport(get_settings().audible_proxy_url, allow_direct_egress=True)
