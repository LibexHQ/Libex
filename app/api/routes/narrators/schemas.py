"""
Narrators route schemas.

The narrator profile shapes live in libex_core.models beside the other
published models; they are re-exported here so the route and its OpenAPI
schema are unchanged.
"""

# Local
from libex_core.models import AudioSampleResponse, NarratorProfileResponse

__all__ = ["AudioSampleResponse", "NarratorProfileResponse"]
