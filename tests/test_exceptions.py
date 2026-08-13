"""
Exception hierarchy tests.
Ensures exceptions carry correct status codes and messages.
"""

# Third party
import pytest

# Local
from app.core.exceptions import (
    LibexException,
    NotFoundException,
    AudibleAPIException,
    CacheException,
    RegionException,
)


def test_libex_exception_default_status_code():
    """Base exception defaults to 500."""
    exc = LibexException("error")
    assert exc.status_code == 500


def test_libex_exception_custom_status_code():
    """Base exception accepts custom status code."""
    exc = LibexException("error", status_code=418)
    assert exc.status_code == 418


def test_libex_exception_message():
    """Base exception stores message."""
    exc = LibexException("something went wrong")
    assert exc.message == "something went wrong"


def test_not_found_exception_status_code():
    """NotFoundException has 404 status code."""
    exc = NotFoundException()
    assert exc.status_code == 404


def test_not_found_exception_default_message():
    """NotFoundException has default message."""
    exc = NotFoundException()
    assert exc.message == "Resource not found"


def test_not_found_exception_custom_message():
    """NotFoundException accepts custom message."""
    exc = NotFoundException("Book not found")
    assert exc.message == "Book not found"


def test_audible_api_exception_status_code():
    """AudibleAPIException has 502 status code."""
    exc = AudibleAPIException()
    assert exc.status_code == 502


def test_audible_api_exception_default_message():
    """AudibleAPIException has default message."""
    exc = AudibleAPIException()
    assert exc.message == "Audible API error"


def test_audible_api_exception_upstream_status_defaults_to_none():
    """upstream_status defaults to None when the caller doesn't pass one --
    the backfill relies on this default meaning 'no HTTP response happened'
    (a timeout or connection error), not 'unknown'."""
    exc = AudibleAPIException()
    assert exc.upstream_status is None


def test_audible_api_exception_stores_passed_upstream_status():
    """A passed upstream_status is stored as-is."""
    exc = AudibleAPIException("bad request", upstream_status=400)
    assert exc.upstream_status == 400


@pytest.mark.parametrize("upstream_status", [None, 400, 404, 429, 500, 503])
def test_audible_api_exception_status_code_stays_502_regardless_of_upstream_status(upstream_status):
    """status_code is always 502 -- Libex's own client-facing surface --
    no matter what upstream_status carries. This is the guard against
    collapsing the two fields into one, which would leak Audible's raw
    status (e.g. 400) back to Libex's own callers."""
    exc = AudibleAPIException("error", upstream_status=upstream_status)
    assert exc.status_code == 502


def test_cache_exception_status_code():
    """CacheException has 500 status code."""
    exc = CacheException()
    assert exc.status_code == 500


def test_region_exception_status_code():
    """RegionException has 400 status code."""
    exc = RegionException("xx")
    assert exc.status_code == 400


def test_region_exception_message_includes_region():
    """RegionException message includes the invalid region."""
    exc = RegionException("xx")
    assert "xx" in exc.message


def test_exceptions_are_subclass_of_libex_exception():
    """All custom exceptions inherit from LibexException."""
    assert issubclass(NotFoundException, LibexException)
    assert issubclass(AudibleAPIException, LibexException)
    assert issubclass(CacheException, LibexException)
    assert issubclass(RegionException, LibexException)


def test_exceptions_are_subclass_of_exception():
    """LibexException inherits from Exception."""
    assert issubclass(LibexException, Exception)