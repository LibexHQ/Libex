"""
Custom exceptions for Libex.
"""

from enum import StrEnum

__all__ = [
    "AudibleAPIException",
    "CacheException",
    "ErrorCode",
    "LibexException",
    "NotFoundException",
    "RegionException",
]


class ErrorCode(StrEnum):
    """Machine-readable reason carried in the error envelope's `code` field.

    The status code says how the request failed; this says whose gap it is.
    The vocabulary lives only here so the handlers and every raise site agree.

    The values are public: callers may branch on them. The set is additive,
    so new values may appear, but an existing value never changes meaning.

    not_in_libex: Libex's own store has no record of it.
    not_on_audible: Audible has no such record.
    withheld: Libex received it but deliberately does not return it.
    upstream_unavailable: Libex could not find out right now; retry later.
    invalid_request: the request itself is malformed.
    """
    NOT_IN_LIBEX = "not_in_libex"
    NOT_ON_AUDIBLE = "not_on_audible"
    WITHHELD = "withheld"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    INVALID_REQUEST = "invalid_request"


class LibexException(Exception):
    """Base exception for all Libex errors.

    Each subclass sets a default `code`; a raise site passes `code=` when the
    default would misdescribe the failure.
    """
    code: ErrorCode = ErrorCode.UPSTREAM_UNAVAILABLE

    def __init__(self, message: str, status_code: int = 500, code: ErrorCode | None = None):
        self.message = message
        self.status_code = status_code
        if code is not None:
            self.code = code
        super().__init__(self.message)


class NotFoundException(LibexException):
    """Raised when a requested resource is not found."""
    code = ErrorCode.NOT_ON_AUDIBLE

    def __init__(self, message: str = "Resource not found", code: ErrorCode | None = None):
        super().__init__(message, status_code=404, code=code)


class AudibleAPIException(LibexException):
    """Raised when the Audible API returns an unexpected response.

    `status_code` stays 502 for anyone embedding the library, and never carries
    the upstream value. The hosted HTTP app answers this exception as 503
    (temporary, retry later) with a Retry-After header. `upstream_status` is
    what Audible itself reported, kept separately so callers can distinguish a
    permanent 4xx from a transient failure. It is `None` when there was no HTTP response at all
    (timeouts, connection errors), and that absence is itself meaningful: no
    status means the failure could not have been a deliberate rejection.
    """
    code = ErrorCode.UPSTREAM_UNAVAILABLE

    def __init__(
        self,
        message: str = "Audible API error",
        upstream_status: int | None = None,
        code: ErrorCode | None = None,
    ):
        super().__init__(message, status_code=502, code=code)
        self.upstream_status = upstream_status


class CacheException(LibexException):
    """Raised when cache operations fail."""
    code = ErrorCode.UPSTREAM_UNAVAILABLE

    def __init__(self, message: str = "Cache error", code: ErrorCode | None = None):
        super().__init__(message, status_code=500, code=code)


class RegionException(LibexException):
    """Raised when an invalid region is provided."""
    code = ErrorCode.INVALID_REQUEST

    def __init__(self, region: str, code: ErrorCode | None = None):
        super().__init__(f"Invalid region: {region}", status_code=400, code=code)