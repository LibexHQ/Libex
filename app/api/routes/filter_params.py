"""
Shared filter query-parameter dependency for live (Audible-backed) list endpoints.

Exposes only the filters that filter_dicts (libex_core.shaping) actually applies
to an in-memory book list — so what shows in the OpenAPI docs is exactly what
works. The parameters are built from libex_core.shaping.BOOK_FILTER_SPECS, so
the documented surface cannot drift from the applied one. Heavy free-text
filters live on /db/book instead, which has indexes for them.

as_kwargs() returns plain keyword args keyed to match filter_dicts, so the route
layer stays the only place that knows about FastAPI.
"""

# Standard library
import inspect
from typing import Annotated

# Third party
from fastapi import Query

# Core
from libex_core.shaping import BOOK_FILTER_SPECS


class LiveBookFilters:
    """Filter params supported on live book-list endpoints."""

    def __init__(self, **values) -> None:
        for spec in BOOK_FILTER_SPECS:
            setattr(self, spec.name, values.get(spec.name))

    def as_kwargs(self) -> dict:
        """Returns the filters as plain kwargs for filter_dicts."""
        return dict(vars(self))


# FastAPI reads the dependency's parameters from its signature, so publish the
# spec as one: every filter an optional query parameter, in spec order.
LiveBookFilters.__signature__ = inspect.Signature(
    [
        inspect.Parameter(
            spec.name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=None,
            annotation=Annotated[spec.type | None, Query(description=spec.description)],
        )
        for spec in BOOK_FILTER_SPECS
    ]
)
