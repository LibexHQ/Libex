"""
The wire seam for a failed Audible call on this HTTP surface.

The service layer distinguishes "Audible answered and said no"
(NotFoundException) from "Libex could not find out" (AudibleAPIException,
carrying the real upstream status when the failure came with one). Those are
different answers for an HTTP caller: a confirmed absence is a 404 and will
not change on retry, while an outage is a 503 and is worth retrying. Wrapping
a service call here keeps the two apart on the wire; the exception handler in
`app.main` turns the AudibleAPIException into the 503 with its Retry-After
header and the `upstream_unavailable` code.
"""

# Standard library
from typing import Awaitable, TypeVar

# Core
from libex_core.exceptions import AudibleAPIException

T = TypeVar("T")


async def outage_as_unavailable(call: Awaitable[T], message: str | None = None) -> T:
    """
    Awaits `call`, re-raising an AudibleAPIException it raises with the
    message the caller should see.

    message is left as None at a call site whose own AudibleAPIException
    message (built by the service, in `as_audible_failure`) is already the
    right thing for a caller to see verbatim. It is given explicitly at a
    call site whose route has its own literal for the failed lookup, so the
    outage reads word for word as that literal.
    """
    try:
        return await call
    except AudibleAPIException as exc:
        if message is None:
            raise
        raise AudibleAPIException(message, upstream_status=exc.upstream_status) from exc
