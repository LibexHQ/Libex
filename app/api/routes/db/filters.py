"""
Reusable FastAPI dependency factories for DB list-endpoint query parameters.

The book filter fields are defined once in BOOK_FILTER_FIELDS. The
book_filters() factory builds a dependency class from them, optionally omitting
fields that are an endpoint's scope rather than a filter (e.g. the plan
endpoint omits "plan_name" because that's the set being looked at, and so its
name doesn't collide with the {plan_name} path parameter).

These live in the route layer (they use Query) and expose an as_kwargs() method
that returns plain keyword args for the reader services, so the service layer
stays unaware of FastAPI.
"""

# Standard library
import inspect
from enum import Enum
from typing import Annotated

# Third party
from fastapi import Query


class AudiobooksProduced(str, Enum):
    one_to_ten = "1 to 10"
    eleven_to_twenty = "11 to 20"
    twentyone_to_fifty = "21 to 50"
    fiftyone_to_hundred = "51 to 100"
    more_than_hundred = "More than 100"
    none_yet = "None yet"


# Book filter fields defined once: name -> (type, Query description).
# The book_filters() factory turns these into a FastAPI dependency.
BOOK_FILTER_FIELDS: dict[str, tuple[type, str]] = {
    "title": (str, "Filter by title"),
    "subtitle": (str, "Filter by subtitle"),
    "region": (str, "Filter by region"),
    "description": (str, "Filter by description"),
    "summary": (str, "Filter by summary"),
    "publisher": (str, "Filter by publisher"),
    "copyright": (str, "Filter by copyright"),
    "isbn": (str, "Filter by ISBN"),
    "author_name": (str, "Filter by author name"),
    "series_name": (str, "Filter by series name"),
    "language": (str, "Filter by language"),
    "rating_better_than": (float, "Minimum rating"),
    "rating_worse_than": (float, "Maximum rating"),
    "longer_than": (int, "Minimum length in minutes"),
    "shorter_than": (int, "Maximum length in minutes"),
    "explicit": (bool, "Filter by explicit"),
    "whisper_sync": (bool, "Filter by Whispersync availability"),
    "has_pdf": (bool, "Filter by PDF companion availability"),
    "book_format": (str, "Filter by book format"),
    "content_type": (str, "Filter by content type"),
    "content_delivery_type": (str, "Filter by content delivery type"),
    "is_listenable": (bool, "Filter by listenable status"),
    "is_buyable": (bool, "Filter by buyable status"),
    "is_vvab": (bool, "Filter by VVAB (virtual voice audiobook) status"),
    "plan_name": (str, "Filter by Audible plan name"),
    "genre": (str, "Filter by genre or tag name (partial match, e.g. 'fantasy')"),
}


class _BaseBookFilters:
    """Base for generated book-filter dependencies. Holds as_kwargs()."""

    _fields: tuple[str, ...] = ()

    def as_kwargs(self) -> dict:
        """Returns the active filter fields as plain kwargs for the readers."""
        return {name: getattr(self, name) for name in self._fields}


def book_filters(exclude: set[str] | None = None) -> type:
    """
    Builds a FastAPI dependency class exposing the book filter fields.

    Pass `exclude` to omit fields that are the endpoint's scope rather than a
    filter — this both keeps the field out of as_kwargs() and prevents its
    query-param name from colliding with a path/dependency param of the same
    name (e.g. {plan_name}).
    """
    exclude = exclude or set()
    fields = tuple(f for f in BOOK_FILTER_FIELDS if f not in exclude)

    def __init__(self, **kwargs) -> None:
        for name in fields:
            setattr(self, name, kwargs.get(name))

    # Build an explicit signature so FastAPI sees each field as a Query param.
    params = [
        inspect.Parameter(
            "self",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    annotations = {}
    for name in fields:
        ftype, desc = BOOK_FILTER_FIELDS[name]
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=Annotated[ftype | None, Query(description=desc)],
            )
        )
        annotations[name] = Annotated[ftype | None, Query(description=desc)]

    __init__.__signature__ = inspect.Signature(params)
    __init__.__annotations__ = annotations

    cls = type(
        "BookFilters",
        (_BaseBookFilters,),
        {"__init__": __init__, "_fields": fields},
    )
    return cls


class NarratorFilters:
    """Shared filter params for the narrator search endpoint."""

    def __init__(
        self,
        gender: Annotated[str | None, Query(description="Filter by gender (partial match, inclusive of any recorded value)")] = None,
        language: Annotated[str | None, Query(description="Filter by a language the narrator works in (e.g. 'English')")] = None,
        audiobooks_produced: Annotated[AudiobooksProduced | None, Query(description="Filter by audiobooks-produced bucket")] = None,
        source: Annotated[str | None, Query(description="Filter by enrichment source")] = None,
        cultural_heritage: Annotated[str | None, Query(description="Filter by cultural heritage (partial match)")] = None,
    ) -> None:
        self.gender = gender
        self.language = language
        self.audiobooks_produced = audiobooks_produced
        self.source = source
        self.cultural_heritage = cultural_heritage

    def as_kwargs(self) -> dict:
        """Returns the filters as a plain dict for passing to reader services."""
        return {
            "gender": self.gender,
            "language": self.language,
            "audiobooks_produced": self.audiobooks_produced.value if self.audiobooks_produced is not None else None,
            "source": self.source,
            "cultural_heritage": self.cultural_heritage,
        }