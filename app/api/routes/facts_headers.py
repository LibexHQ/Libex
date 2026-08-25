"""
Shared X-Libex-Source / X-Libex-Complete response header contract.

One copy of the header vocabulary and the stamping helper, used by every
ResponseFacts-backed route across the books, series and authors packages,
so the contract can't drift the way three hand-typed copies did.
"""

# Standard library
from collections.abc import Collection, Mapping
from typing import Any

# Third party
from fastapi import Response

# Core
from app.core.response_headers import (
    HEADER_COMPLETE,
    HEADER_INCOMPLETE_REASON,
    HEADER_REQUEST_ID,
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
#
# X-Request-Id is stamped on every response LoggingMiddleware sees --
# 404s, 500s, and /health included -- not only the ones below, but there is
# no per-request hook this file (or middleware.py, which stamps it) can use
# to add it to the OpenAPI schema for a path it doesn't own; the schema is
# built once, at startup, from route decorators alone. Declaring it here
# documents it on every route that already opts into this shared block --
# every entity route with something to say about provenance or
# completeness, which is also where a caller is most likely to be reading
# when a request needs to be reported. It understates the header's real
# reach, deliberately: a caller who reads the description on one of these
# routes and assumes the header appears only there would be wrong, but a
# caller who never sees it documented anywhere would have no way to learn
# it exists at all. The description below says so directly rather than
# implying this list is exhaustive.
FACTS_RESPONSE_HEADERS = {
    HEADER_REQUEST_ID: {
        "description": (
            "A random id minted fresh for this response, never taken from "
            "anything the caller sends. Quote it back when reporting a "
            "problem -- it is the one handle that lets a specific request "
            "be found in Libex's own logs, without exposing anything about "
            "who made it. Stamped on every response Libex returns, not "
            "only the ones, like this one, that declare it in their "
            "documented headers."
        ),
        "schema": {"type": "string"},
    },
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
        "description": (
            "Whether the response body contains every element the caller "
            "asked for. \"true\" means it does. \"false\" means at least "
            "one requested element is missing from the body -- not merely "
            "that something went wrong somewhere internally on the way to "
            "a complete answer -- and X-Libex-Incomplete-Reason names why."
        ),
        "schema": {"type": "string", "enum": ["true", "false"]},
    },
    HEADER_INCOMPLETE_REASON: {
        "description": (
            "Present only when X-Libex-Complete is false. Comma-joined "
            "subset of the closed reason vocabulary, in this fixed order "
            "regardless of which was recorded first, one or more joined by "
            "\", \": "
            "hydration-not-found -- Audible has no record of the "
            "requested ASIN, or returned a titleless placeholder for it "
            "instead of a book. Also covers a title Audible does return in "
            "full but hasn't released yet, which Libex filters out of the "
            "body before this check can tell it apart from a genuinely "
            "nonexistent ASIN. Retrying will not surface a nonexistent "
            "title; one still pending release may appear on a later "
            "request once it is out. "
            "hydration-failed -- Libex could not reach Audible for part of "
            "the request, and neither its stored database copy nor its "
            "cache covered what was missed. Retrying later, once Audible "
            "is reachable again, may succeed where this one did not. "
            "hydration-deadline -- the request ran out of its time budget "
            "before every element could be fetched, and the same fallback "
            "that covers hydration-failed didn't cover this shortfall "
            "either. Not emitted by any route today -- the one caller that "
            "imposes such a deadline reports completeness through a "
            "separate, coarser check instead of this header. "
            "discovery-incomplete -- reserved for the catalogue walk that "
            "enumerates which elements exist ending before it finished, "
            "before any element it found was fetched. Not emitted by any "
            "route today -- nothing currently wires a walk's own shortfall "
            "into this header."
        ),
        "schema": {"type": "string"},
    },
}

# The two /author/books routes never build a ResponseFacts at all -- they
# mark completeness through _mark_completeness in authors/router.py, on the
# discovery walk's own success, not through anything this module's
# X-Libex-Source/X-Libex-Incomplete-Reason machinery tracks. Declaring the
# full FACTS_RESPONSE_HEADERS block on them would document two headers
# they never send. This is the subset they actually do -- X-Request-Id
# included, since that one is never tied to ResponseFacts at all and these
# two routes carry it exactly like every other response does.
COMPLETE_ONLY_RESPONSE_HEADERS = {
    HEADER_REQUEST_ID: FACTS_RESPONSE_HEADERS[HEADER_REQUEST_ID],
    HEADER_COMPLETE: FACTS_RESPONSE_HEADERS[HEADER_COMPLETE],
}


def stamp_facts_headers(
    response: Response,
    facts: ResponseFacts,
    *,
    has_entities: bool | None = None,
    entities: Collection[Mapping[str, Any]] | None = None,
) -> None:
    """
    Stamps X-Libex-Source and X-Libex-Complete from a ResponseFacts ledger
    the route just handed a service.

    A single-entity route passes `has_entities` directly -- one element has
    no sequence to derive a key from, so this keeps that call byte-identical
    to how it behaved before `entities` existed. A bulk/list route passes
    `entities` instead: the actual sequence of entity dicts it is about to
    send in its body, not a pre-derived flag or key set. This function
    derives both from it -- `has_entities` as `bool(entities)`, and the ASIN
    key list source_header_value_for restricts its tally to -- so every
    bulk route computes them the same way instead of each hand-rolling its
    own projection.

    That key list is a list, not a set: source_header_value_for counts one
    contributing source per element it iterates, so a set would collapse a
    repeated ASIN in the body into a single count and understate the tally
    -- the same undercount its own docstring already treats as a fabricated-
    provenance failure. No live route can actually produce a duplicate ASIN
    in `entities` today -- callers dedupe the ASINs they fetch before
    hydration, and the segments a bulk response assembles are disjoint -- so
    this is a contractual guarantee, not a fix for an observed undercount.

    X-Libex-Source is set only when `has_entities` is true (directly, or
    derived from a non-empty `entities`). A route with nothing to attribute
    -- an empty bulk result once notFound has consumed everything, for
    instance -- gets no header at all, exactly what
    source_header_value()/source_header_value_for() already refuse to
    fabricate for an unpopulated or unattributable tally.

    A route whose response can shrink an already-fetched set -- filtering or
    sorting a bulk list after facts recorded where every fetched element
    came from -- passes the body's own post-filter `entities`, restricting
    the tally to exactly what is being sent rather than what was fetched
    before a filter removed some of it. It is the body that must not lie,
    not the tally the fetch produced.

    X-Libex-Complete is set unconditionally once facts exists, with
    X-Libex-Incomplete-Reason alongside it only when incomplete. This
    function has no opinion on which value it renders -- that's decided
    entirely upstream, by whether the service call the route just awaited
    ever recorded an incomplete reason against this same facts object before
    handing it back. A service that only ever records a source leaves it
    "true"; one that also records a shortfall it couldn't fully make up for
    renders it "false", with that reason attached.
    """
    if entities is not None:
        body_keys = [entity["asin"] for entity in entities]
        has_entities = bool(body_keys)
    else:
        body_keys = None
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
