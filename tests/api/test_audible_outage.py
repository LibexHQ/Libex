"""
Tests for the wire seam between the service layer's two failure types and
this HTTP surface's one.

outage_as_not_found turns an AudibleAPIException a service call raises into
a NotFoundException -- an outage reads identically to a confirmed absence at
this layer, since an HTTP caller has no way to act on the distinction and
every route already has its own 404 for a genuine miss.
"""

# Third party
import pytest

# Local
from app.api.routes.audible_outage import outage_as_not_found
from libex_core.exceptions import AudibleAPIException, NotFoundException


async def _raise(exc):
    raise exc


async def _succeed(value):
    return value


@pytest.mark.asyncio
async def test_outage_as_not_found_passes_through_a_successful_result():
    result = await outage_as_not_found(_succeed(["B000000001"]))
    assert result == ["B000000001"]


@pytest.mark.asyncio
async def test_outage_as_not_found_no_message_uses_the_exceptions_own_message():
    """message left as None (the default at most call sites): the
    AudibleAPIException's own message, built by the service in
    as_audible_failure, is already the right thing for a caller to see
    verbatim."""
    with pytest.raises(NotFoundException) as exc:
        await outage_as_not_found(
            _raise(AudibleAPIException("Audible unavailable and no cached data found"))
        )
    assert exc.value.message == "Audible unavailable and no cached data found"
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_outage_as_not_found_explicit_message_overrides_the_exceptions_own():
    """A call site with its own literal for "found nothing" (e.g. /search's
    "No books found") gets that literal verbatim, not whatever the service
    happened to say about why it couldn't find out."""
    with pytest.raises(NotFoundException) as exc:
        await outage_as_not_found(
            _raise(AudibleAPIException("Audible search failed")),
            "No books found",
        )
    assert exc.value.message == "No books found"
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_outage_as_not_found_chains_from_the_original_exception():
    """`from exc` -- the original AudibleAPIException must survive as the
    NotFoundException's __cause__, not get discarded."""
    original = AudibleAPIException("Audible unavailable and no cached data found")
    with pytest.raises(NotFoundException) as exc:
        await outage_as_not_found(_raise(original))
    assert exc.value.__cause__ is original


@pytest.mark.asyncio
async def test_outage_as_not_found_lets_a_genuine_not_found_propagate_unchanged():
    """A real NotFoundException (Audible answered and said no) is not this
    helper's concern at all -- it propagates as itself, not converted or
    reworded."""
    original = NotFoundException("Book not found: B000000001")
    with pytest.raises(NotFoundException) as exc:
        await outage_as_not_found(_raise(original))
    assert exc.value is original
