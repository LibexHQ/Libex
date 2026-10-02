"""
OpenAPI model for the error body Libex's own exception handlers return.

The body itself is built by the exception handlers in `app.main`; this class
only documents it, so the schema in /docs matches what those handlers send.
It does not cover everything: FastAPI's own 422 validation errors and
unknown-route 404s use its `{detail}` body, and an unhandled 500 carries no
`code`.
`error` and `status_code` are the shape AudiMeta clients already read, and
`code` is the machine-readable reason beside them. A 503 body also carries
`retryAfter`, the same number of seconds as the Retry-After header.
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
            "upstream_unavailable: Libex could not find out right now (a 503); retry later. "
            "invalid_request: the request itself is malformed."
        )
    )
    retryAfter: int | None = Field(
        default=None,
        description="Seconds to wait before retrying; present only on a 503, and equal to the Retry-After header",
        exclude_if=lambda v: v is None,
    )


# Merged into a route's `responses=`. This documents the 404 and 503 these
# routes return; other statuses from Libex's handlers (400, 500, 502) use the
# same body. The `code` field is what tells the reasons apart.
ERROR_RESPONSES: dict[int | str, dict] = {
    404: {"model": ErrorResponse, "description": "Not found, or the request was rejected; see `code`"},
    503: {
        "model": ErrorResponse,
        "description": (
            "Audible could not be reached and Libex had no stored or cached copy "
            "to answer from (code `upstream_unavailable`); retry after the "
            "Retry-After header's number of seconds"
        ),
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before retrying",
                "schema": {"type": "integer"},
            }
        },
    },
}
