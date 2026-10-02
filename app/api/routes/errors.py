"""
OpenAPI model for the error body Libex's own exception handlers return.

The body itself is built by the exception handlers in `app.main`; this class
only documents it, so the schema in /docs matches what those handlers send.
It does not cover everything: FastAPI's own 422 validation errors and
unknown-route 404s use its `{detail}` body, and an unhandled 500 carries no
`code`.
`error` and `status_code` are the shape AudiMeta clients already read, and
`code` is the machine-readable reason beside them.
"""

# Third-party
from pydantic import BaseModel, Field

# Core
from libex_core.exceptions import ErrorCode


class ErrorResponse(BaseModel):
    error: str = Field(description="Human-readable message describing what went wrong")
    status_code: int = Field(description="HTTP status code of the response")
    code: ErrorCode = Field(
        description=(
            "Why the request failed. "
            "not_in_libex: Libex's own store has no record. "
            "not_on_audible: Audible has no record. "
            "withheld: Libex received it but deliberately does not return it. "
            "upstream_unavailable: Libex could not find out right now; retry later. "
            "invalid_request: the request itself is malformed."
        )
    )


# Merged into a route's `responses=`. 404 is the only error status these routes
# raise on purpose; the `code` field is what tells the reasons apart.
ERROR_RESPONSES: dict[int | str, dict] = {
    404: {"model": ErrorResponse, "description": "Not found, or the request was rejected; see `code`"},
}
