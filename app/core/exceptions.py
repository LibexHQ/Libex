"""
Custom exceptions for Libex.
"""


class LibexException(Exception):
    """Base exception for all Libex errors."""
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class NotFoundException(LibexException):
    """Raised when a requested resource is not found."""
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, status_code=404)


class AudibleAPIException(LibexException):
    """Raised when the Audible API returns an unexpected response.

    `status_code` is always 502 — it is what Libex returns to its own callers
    and is part of the AudiMeta-compatible response surface, so it never
    carries the upstream value. `upstream_status` is what Audible itself
    reported, kept separately so callers can distinguish a permanent 4xx from
    a transient failure. It is `None` when there was no HTTP response at all
    (timeouts, connection errors), and that absence is itself meaningful: no
    status means the failure could not have been a deliberate rejection.
    """
    def __init__(self, message: str = "Audible API error", upstream_status: int | None = None):
        super().__init__(message, status_code=502)
        self.upstream_status = upstream_status


class CacheException(LibexException):
    """Raised when cache operations fail."""
    def __init__(self, message: str = "Cache error"):
        super().__init__(message, status_code=500)


class RegionException(LibexException):
    """Raised when an invalid region is provided."""
    def __init__(self, region: str):
        super().__init__(f"Invalid region: {region}", status_code=400)