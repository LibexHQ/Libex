"""
Response headers Libex adds beyond FastAPI's defaults: pure data and pure
functions, no framework import anywhere in this module, so it is trivially
unit-testable and cannot pull layering the wrong way -- a service reasoning
about what it has assembled so far has no business importing Starlette to do
it. `app.core.middleware` and `app.main` are the only two places anything
here ever reaches the wire.
"""

# Standard library
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

# ============================================================
# HEADER NAMES
# ============================================================

HEADER_REQUEST_ID = "X-Request-Id"
HEADER_SOURCE = "X-Libex-Source"
HEADER_COMPLETE = "X-Libex-Complete"
HEADER_INCOMPLETE_REASON = "X-Libex-Incomplete-Reason"

# The CORS allowlist all four are exposed through. X-Libex-Complete is
# already on the wire today -- authors/router.py sets it on both
# author-books routes -- and until this registry existed no browser caller
# could read it: fetch() and XMLHttpRequest hide every response header a
# server does not name in Access-Control-Expose-Headers, so it has been
# invisible to JS despite being present on every response. This registry
# fixes that live defect rather than adding a hypothetical one.
#
# Additive only: a name is appended here, never removed -- removing one
# un-exposes a header a caller may already depend on reading.
EXPOSED_HEADER_NAMES = (
    HEADER_REQUEST_ID,
    HEADER_SOURCE,
    HEADER_COMPLETE,
    HEADER_INCOMPLETE_REASON,
)

# ============================================================
# SOURCE TALLY
# ============================================================

SOURCE_AUDIBLE = "audible"
SOURCE_CACHE = "cache"
SOURCE_DB = "db"
SOURCE_MIXED = "mixed"

# The closed vocabulary a single element's source is drawn from. "mixed" is
# never recorded against an element -- it is only ever the word the
# formatter below produces once a tally has more than one of these three
# populated at once.
SOURCES = (SOURCE_AUDIBLE, SOURCE_CACHE, SOURCE_DB)


def format_source_header(counts: Mapping[str, int]) -> str:
    """
    Renders a source tally as an RFC 8941 item with parameters.

    A single populated source renders as the bare token -- "cache" -- with
    no parameters at all, whatever its count: for one source the token
    already carries the whole truth, and a count next to it would repeat the
    body length in a different unit and say nothing new. Two or more
    populated sources render as "mixed" with a parameter per source, counts
    included, because "mixed" alone would say a response was assembled from
    more than one place without saying how much came from where -- exactly
    the fact a caller reaches for this header to get.

    Zero-count entries are dropped before a tally is judged singular or
    mixed, so a tally that only ever incremented "cache" still renders
    "cache" even if it started as a dict naming all three sources at zero.
    Empty (or all-zero) renders as the empty string, which is a header a
    caller never sends -- there is no meaningful "source: " with nothing
    after it.
    """
    populated = {source: count for source, count in counts.items() if count}
    if not populated:
        return ""
    if len(populated) == 1:
        return next(iter(populated))
    parameters = "; ".join(
        f"{source}={populated[source]}" for source in SOURCES if source in populated
    )
    return f"{SOURCE_MIXED}; {parameters}"


# ============================================================
# INCOMPLETE REASON
# ============================================================

REASON_DISCOVERY_INCOMPLETE = "discovery-incomplete"
REASON_HYDRATION_DEADLINE = "hydration-deadline"
REASON_HYDRATION_FAILED = "hydration-failed"
REASON_HYDRATION_NOT_FOUND = "hydration-not-found"

# The closed vocabulary, and the fixed order it renders in when more than
# one reason applies to the same response. Order is this tuple's order, not
# recording order, so the header value for a given failure mode is always
# the same string regardless of which element happened to fail first.
INCOMPLETE_REASONS = (
    REASON_DISCOVERY_INCOMPLETE,
    REASON_HYDRATION_DEADLINE,
    REASON_HYDRATION_FAILED,
    REASON_HYDRATION_NOT_FOUND,
)


def format_incomplete_reason_header(reasons: Iterable[str]) -> str:
    """
    Comma-joins whichever of the closed incomplete-reason vocabulary is
    present, in INCOMPLETE_REASONS order, and returns the empty string when
    none apply -- the response is complete and there is nothing to report.
    """
    present = set(reasons)
    return ", ".join(reason for reason in INCOMPLETE_REASONS if reason in present)


# ============================================================
# RESPONSE FACTS -- the cross-lane seam
# ============================================================

@dataclass
class ResponseFacts:
    """
    Ledger a router opens empty, passes keyword-only into a service, and
    reads back once the service returns, to stamp X-Libex-Source and
    X-Libex-Incomplete-Reason on the response the router sends. Everything
    on it is mutated in place -- accreting a tally across however many
    elements a response resolves is the entire reason it exists, so it is a
    real mutable object rather than a frozen value rebuilt and threaded back
    through every call.

    Takes no constructor argument -- `ResponseFacts()` is the only way one
    is ever built, starting empty and complete. What varies per call site is
    whether a caller has one at all: every service function that touches a
    ResponseFacts declares the parameter `*, facts: ResponseFacts | None =
    None`, and record_source/record_source_keys/record_incomplete below are
    the only way anything mutates one, precisely so each of them can be a
    no-op when facts is None instead of every call site growing its own `if
    facts is not None:` guard. That default is what keeps the seeder,
    internal routes, and every other caller that passes nothing
    byte-identical to how they behaved before this module existed.
    """

    source_counts: dict[str, int] = field(default_factory=lambda: {source: 0 for source in SOURCES})
    incomplete_reasons: set[str] = field(default_factory=set)
    source_by_key: dict[str, str] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        """True once nothing has recorded a reason the response fell short."""
        return not self.incomplete_reasons

    def source_header_value(self) -> str:
        """The X-Libex-Source value for what has been recorded so far."""
        return format_source_header(self.source_counts)

    def source_header_value_for(self, keys: Collection[str]) -> str:
        """
        The X-Libex-Source value restricted to `keys` -- the tally for
        exactly the elements a router is about to send, rather than every
        element a service call resolved before a filter removed some of
        them from the body.

        Returns the empty string -- the same "nothing to attribute" result
        source_header_value() gives for an empty tally -- whenever
        `sum(self.source_counts.values()) != len(self.source_by_key)`. That
        inequality means something on this ledger was recorded through
        record_source rather than record_source_keys: an aggregate count
        with no key behind it, so the per-key map cannot be trusted to
        account for the whole tally. Restricting an incomplete map to
        `keys` would either undercount (keys the map has no entry for look
        unsourced) or, worse, look complete by coincidence -- and an
        undercount is the same fabricated-provenance failure this method
        exists to prevent, just understating rather than overstating.
        Omission is the honest degradation here, and the header is
        additive, so its absence is always a compatible response for any
        caller.

        Once that check passes, every key present in `self.source_by_key`
        is known-good, so keys absent from it (present in `keys` but never
        recorded here) are silently skipped rather than treated as an
        error -- the same "attribute what is known, drop what isn't"
        posture, applied per key instead of to the ledger as a whole.
        """
        if sum(self.source_counts.values()) != len(self.source_by_key):
            return ""
        restricted = {source: 0 for source in SOURCES}
        for key in keys:
            source = self.source_by_key.get(key)
            if source is not None:
                restricted[source] += 1
        return format_source_header(restricted)

    def incomplete_reason_header_value(self) -> str:
        """The X-Libex-Incomplete-Reason value for what has been recorded so far."""
        return format_incomplete_reason_header(self.incomplete_reasons)


def record_source(facts: ResponseFacts | None, source: str, count: int = 1) -> None:
    """
    Adds `count` to `source`'s tally on `facts`, or does nothing at all if
    `facts` is None -- see ResponseFacts for why that is the no-accounting
    case rather than an error.

    `source` must be one of SOURCES. "mixed" is a rendering outcome the
    formatter produces, never something a caller records, so passing it here
    is a programming mistake and raises rather than silently miscounting.
    """
    if facts is None:
        return
    if source not in SOURCES:
        raise ValueError(f"not a recordable source: {source!r}")
    facts.source_counts[source] = facts.source_counts.get(source, 0) + count


def record_source_keys(facts: ResponseFacts | None, source: str, keys: Iterable[str]) -> None:
    """
    The batched, keyed sibling of record_source: adds `source`'s count
    against `facts.source_by_key` for each of `keys`, and increments the
    same aggregate tally record_source does, by len(keys), in the same
    call -- for a service call that resolves several keyed elements (each
    of several books hydrated in one pass, say) from a single source at
    once, rather than one anonymous element at a time.

    Does nothing at all if `facts` is None, the same no-accounting case
    record_source and record_incomplete already document.

    `source` must be one of SOURCES, checked and rejected the same way and
    for the same reason as record_source.
    """
    if facts is None:
        return
    if source not in SOURCES:
        raise ValueError(f"not a recordable source: {source!r}")
    keys = list(keys)
    facts.source_counts[source] = facts.source_counts.get(source, 0) + len(keys)
    for key in keys:
        facts.source_by_key[key] = source


def record_incomplete(facts: ResponseFacts | None, reason: str) -> None:
    """
    Marks `facts` incomplete for `reason`, or does nothing at all if `facts`
    is None -- see ResponseFacts for why that is the no-accounting case
    rather than an error.

    `reason` must be one of INCOMPLETE_REASONS; anything else is a
    programming mistake and raises rather than silently being dropped.
    """
    if facts is None:
        return
    if reason not in INCOMPLETE_REASONS:
        raise ValueError(f"not a recordable incomplete reason: {reason!r}")
    facts.incomplete_reasons.add(reason)
