"""
The wire seam between the service layer's two failure types and this HTTP
surface's one.

The service layer distinguishes "Audible answered and said no"
(NotFoundException) from "Libex could not find out" (AudibleAPIException,
carrying the real upstream status when the failure came with one). An HTTP
caller of this API has no way to act on that distinction and every route
already has its own 404 for a confirmed absence, so the two have to read
identically here: an outage on one of these calls comes back exactly like
the confirmed absence it sits beside, while an in-process caller further up
the stack can still catch AudibleAPIException on its own terms.
"""

# Standard library
from typing import Awaitable, TypeVar

# Core
from libex_core.exceptions import AudibleAPIException, NotFoundException

T = TypeVar("T")


async def outage_as_not_found(call: Awaitable[T], message: str | None = None) -> T:
    """
    Awaits `call`, turning an AudibleAPIException it raises into a
    NotFoundException.

    message is left as None at a call site whose own AudibleAPIException
    message (built by the service, in `as_audible_failure`) is already the
    right thing for a caller to see verbatim. It is given explicitly at a
    call site whose route already has its own literal for "found nothing" --
    an outage there has to come back word for word as that literal, not as
    whatever the service happened to say about why it couldn't find out.
    """
    try:
        return await call
    except AudibleAPIException as exc:
        raise NotFoundException(message if message is not None else exc.message) from exc
