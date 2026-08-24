"""
Shared X-Libex-Source / X-Libex-Complete response header contract.

One copy of the header vocabulary and the stamping helper, used by every
ResponseFacts-backed route across the books, series and authors packages,
so the contract can't drift the way three hand-typed copies did.
"""

# Standard library
from collections.abc import Collection

# Third party
from fastapi import Response

# Core
from app.core.response_headers import (
    HEADER_COMPLETE,
    HEADER_INCOMPLETE_REASON,
    HEADER_SOURCE,
    SOURCE_MIXED,
    SOURCES,
    ResponseFacts,
)

# The closed vocabulary for every header a ResponseFacts-backed route may
# send, documented once and reused on each -- built from
# SOURCES/SOURCE_MIXED rather than a hand-typed list, so this can't drift
# from what response_headers.py actually recognizes as valid, the same
# reason the sort/filter enums are built from their own service allow-lists
# rather than copied by hand.
FACTS_RESPONSE_HEADERS = {
    HEADER_SOURCE: {
        "description": (
            "Where the entity element(s) in this response came from: "
            f"one of {', '.join(SOURCES)} alone, or \"{SOURCE_MIXED}\" with "
            "a count per contributing source when more than one produced "
            "elements in the body. Absent whenever the response carries no "
            "entity element to attribute a source to, and also whenever "
            "attribution for the elements actually in the body could not "
            "be fully established -- a partial tally is never sent, since "
            "an undercount would misstate provenance as surely as "
            "crediting a source that contributed nothing."
        ),
        "schema": {"type": "string"},
    },
    HEADER_COMPLETE: {
        "description": "Whether this response is known to be complete.",
        "schema": {"type": "string", "enum": ["true", "false"]},
    },
    HEADER_INCOMPLETE_REASON: {
        "description": (
            "Present only when X-Libex-Complete is false. Comma-joined "
            "subset of the closed reason vocabulary: discovery-incomplete, "
            "hydration-deadline, hydration-failed, hydration-not-found -- "
            "in that fixed order, one or more joined by \", \"."
        ),
        "schema": {"type": "string"},
    },
}

# The two /author/books routes never build a ResponseFacts at all -- they
# mark completeness through _mark_completeness in authors/router.py, on the
# discovery walk's own success, not through anything this module's
# X-Libex-Source/X-Libex-Incomplete-Reason machinery tracks. Declaring the
# full FACTS_RESPONSE_HEADERS block on them would document two headers
# they never send. This is the subset they actually do.
COMPLETE_ONLY_RESPONSE_HEADERS = {
    HEADER_COMPLETE: FACTS_RESPONSE_HEADERS[HEADER_COMPLETE],
}


def stamp_facts_headers(
    response: Response,
    facts: ResponseFacts,
    *,
    has_entities: bool,
    body_keys: Collection[str] | None = None,
) -> None:
    """
    Stamps X-Libex-Source and X-Libex-Complete from a ResponseFacts ledger
    the route just handed a service.

    X-Libex-Source is set only when `has_entities` is True. A route passes
    False here when the body it is about to send carries no entity element
    at all -- an empty bulk result once notFound has consumed everything,
    for instance -- and attributing a source to nothing sent would be a
    fabricated fact, exactly what source_header_value()/source_header_value_for()
    already refuse to do for an unpopulated or unattributable tally.

    `body_keys` is None for every single-entity route, and stays None there
    forever -- one element has nothing to restrict a tally to, so the
    unrestricted facts.source_header_value() already states exactly what
    that one element's own source was. Passing None reproduces this
    function's behaviour before body_keys existed, byte for byte.

    A route whose response can shrink an already-fetched set --
    filtering or sorting a bulk list after facts recorded where every
    fetched element came from -- passes the body's own post-filter keys
    instead, and gets facts.source_header_value_for(body_keys): the tally
    restricted to exactly what is being sent, not what was fetched before a
    filter removed some of it. Passing the post-filter key set is
    deliberate rather than relying on facts being empty or unchanged:
    filtering can drop some (or all) of the fetched elements from the
    response after facts already recorded where all of them came from, and
    it is the body that must not lie, not the tally the fetch produced.

    X-Libex-Complete is set unconditionally once facts exists, with
    X-Libex-Incomplete-Reason alongside it only when incomplete -- no
    currently-wired service call across these routes ever records an
    incomplete reason, so this renders "true" today, but the header is real
    infrastructure for the day one does, not a currently-active signal on
    its own.
    """
    if has_entities:
        source_value = (
            facts.source_header_value_for(body_keys)
            if body_keys is not None
            else facts.source_header_value()
        )
        if source_value:
            response.headers[HEADER_SOURCE] = source_value
    response.headers[HEADER_COMPLETE] = "true" if facts.is_complete else "false"
    if not facts.is_complete:
        response.headers[HEADER_INCOMPLETE_REASON] = facts.incomplete_reason_header_value()
