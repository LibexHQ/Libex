"""
Shared filter query-parameter dependency for live (Audible-backed) list endpoints.

Exposes only the filters that filter_dicts (app/services/filtering.py) actually
applies to an in-memory book list — so what shows in the OpenAPI docs is exactly
what works. Heavy free-text filters live on /db/book instead, which has indexes
for them.

as_kwargs() returns plain keyword args keyed to match filter_dicts, so the route
layer stays the only place that knows about FastAPI.
"""

# Standard library
from typing import Annotated

# Third party
from fastapi import Query


class LiveBookFilters:
    """Filter params supported on live book-list endpoints."""

    def __init__(
        self,
        language: Annotated[str | None, Query(description="Filter by language (exact match)")] = None,
        book_format: Annotated[str | None, Query(description="Filter by book format (e.g. unabridged)")] = None,
        explicit: Annotated[bool | None, Query(description="Filter by explicit")] = None,
        whisper_sync: Annotated[bool | None, Query(description="Filter by Whispersync availability")] = None,
        has_pdf: Annotated[bool | None, Query(description="Filter by PDF companion availability")] = None,
        is_vvab: Annotated[bool | None, Query(description="Filter by VVAB (virtual voice audiobook) status")] = None,
        plan_name: Annotated[str | None, Query(description="Filter by Audible plan name (e.g. US Minerva)")] = None,
        rating_better_than: Annotated[float | None, Query(description="Minimum rating")] = None,
        rating_worse_than: Annotated[float | None, Query(description="Maximum rating")] = None,
        longer_than: Annotated[int | None, Query(description="Minimum length in minutes")] = None,
        shorter_than: Annotated[int | None, Query(description="Maximum length in minutes")] = None,
        genre: Annotated[str | None, Query(description="Filter by genre or tag name (partial match, e.g. 'fantasy')")] = None,
    ) -> None:
        self.language = language
        self.book_format = book_format
        self.explicit = explicit
        self.whisper_sync = whisper_sync
        self.has_pdf = has_pdf
        self.is_vvab = is_vvab
        self.plan_name = plan_name
        self.rating_better_than = rating_better_than
        self.rating_worse_than = rating_worse_than
        self.longer_than = longer_than
        self.shorter_than = shorter_than
        self.genre = genre

    def as_kwargs(self) -> dict:
        """Returns the filters as plain kwargs for filter_dicts."""
        return dict(vars(self))