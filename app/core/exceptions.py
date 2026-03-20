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
    """Raised when the Audible API returns an unexpected response."""
    def __init__(self, message: str = "Audible API error"):
        super().__init__(message, status_code=502)


class CacheException(LibexException):
    """Raised when cache operations fail."""
    def __init__(self, message: str = "Cache error"):
        super().__init__(message, status_code=500)


class RegionException(LibexException):
    """Raised when an invalid region is provided."""
    def __init__(self, region: str):
        super().__init__(f"Invalid region: {region}", status_code=400)