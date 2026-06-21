"""
Shared release-window query-parameter type for the release endpoints.
The same look-back / look-ahead day windows are offered by the DB and live
new-releases and coming-soon endpoints, so the enum is defined once here and
imported by both routers — no drift between the DB and live surfaces.
"""

# Standard library
from enum import IntEnum


class ReleaseWindow(IntEnum):
    """Allowed look-back / look-ahead windows (in days) for the release endpoints."""
    days_30 = 30
    days_60 = 60
    days_90 = 90
    days_120 = 120
    days_240 = 240
    days_365 = 365