"""
The audibleExtras blob: everything Audible sends on a product that no
first-class field reproduces, carried through verbatim, together with the
record of anything that had to be withheld from it.

The blob is made safe to store and bounded in size here, where it is built,
so that every consumer of the normalized dict holds the same value. It is a
pure function of its input -- no I/O, no shared state beyond the counters
behind the windowed warnings -- so it is safe to run on a worker thread.
"""

# Standard library
import json
import logging
import math
import time
from typing import Any

# Core
from libex_core.log_safety import safe_asin_for_log, window_elapsed

logger = logging.getLogger("libex")

# relationship_type values stripped out of the blob's relationships array.
# A podcast show carries one child entry per episode, which is the whole of
# why this exists: measured live in us, B08JJND27B is a 448 KB product of
# which 440 KB is 4,412 episode entries and a single season entry. All eight
# podcasts in that sample carried both types and ran from 60 KB to 448 KB on
# the same shape. Stripped, that 448 KB product leaves a 5 KB blob, and the
# largest blob anywhere in the nineteen-product sample is 8 KB.
#
# The strip is unconditional rather than podcast-gated. Nothing else in the
# catalogue was observed carrying either type -- ordinary books relate to
# series and components -- so gating it on content_type would add a branch
# that changes no outcome while leaving a second, easily-missed way for
# these entries to arrive.
_STRIPPED_RELATIONSHIP_TYPES = frozenset({"episode", "season"})

# Caps on the blob, both deliberately far above anything observed so they
# can only fire on something pathological. 64 KB is eight times the largest
# blob in the sample above, measured the same way this cap measures; 32
# levels of nesting is more than five times the deepest product in it, which
# reached six.
#
# Exceeding either drops the blob whole. Pruning the largest keys instead
# would hand back something that looks complete and is not, which is the
# silent loss this whole mechanism exists to stop -- a caller can see an
# absent blob and an extrasWithheld entry saying why, and cannot see a key
# that was quietly removed from a blob that still arrived.
_EXTRAS_MAX_BYTES = 64 * 1024
_EXTRAS_MAX_DEPTH = 32

# The same nesting bound, published for the one other structure Libex carries
# that Audible can nest without limit: a chapter's sub-chapters.
MAX_NESTING_DEPTH = _EXTRAS_MAX_DEPTH

# The widest int that survives the whole path from this module to a jsonb
# column, in bits.
#
# There are two ceilings and Python's is by far the lower. Postgres jsonb
# stores every number as numeric, which holds 131,072 digits; CPython
# refuses outright to render an int wider than sys.get_int_max_str_digits(),
# 4,300 by default since 3.11, and json.dumps renders every int it is given.
# So an int of 5,000 digits is perfectly storable and still unprintable, and
# the bound that matters is Python's.
#
# Counted in bits rather than digits because counting digits means calling
# str(), which is the exact call that raises on the values this exists to
# catch -- the precise check would blow up on precisely its own subject
# matter, out of the normalizer and down the caller's DB ladder. 2**14283 is
# below 10**4300, so anything at or under this bit length always renders.
# An operator who lowers the limit below the default is still covered: the
# dump below is wrapped, and a raise there is recorded rather than thrown.
_INT_BITS_ALWAYS_RENDERABLE = 14283

# Why a blob, or part of one, did not survive. These are the values that
# reach the caller in extrasWithheld, so they are part of the response and
# not just log vocabulary.
_WITHHELD_SANITIZED = "sanitized"
_WITHHELD_DEPTH = "depth"
_WITHHELD_SIZE = "size"
_WITHHELD_UNSERIALIZABLE = "unserializable"


# How often the extras-withheld warning below actually logs for any one
# reason, once that reason starts firing repeatedly. The same windowing the
# unreadable-plans warning in books.py applies, for the same reason: every one of these can
# fire on every product in a page at once if what changed is upstream and
# systematic -- a shape Audible started sending, not one product being odd --
# and a line per product would bury the only thing worth reading, which is
# that it happened at all and how much.
_EXTRAS_LOG_INTERVAL_SECONDS = 60

_extras_incident_counts: dict[str, int] = {}
_extras_incident_last_logged: dict[str, float] = {}


def _log_extras_incident(asin: str, region: str, reason: str, blob_bytes: int | None = None) -> None:
    """
    Reports that something was withheld from a product's extras blob, at most
    once per _EXTRAS_LOG_INTERVAL_SECONDS per reason.

    Windowed per reason rather than globally, so a flood of one kind cannot
    silence the first occurrence of another -- which is why the state here is
    a dict keyed by reason where the unreadable-plans warning needs only a
    pair of scalars, and why the two share the window_elapsed predicate rather
    than one recording function. asin names the one product that reopened the
    window, not every product the line speaks for; occurrences is how many
    the window that just closed covered, which is the number worth reading
    when this starts repeating.

    Nothing from the blob reaches this line. No upstream key name is used as
    a field name here either -- that would let Audible's response shape
    define Libex's log schema, so a change upstream would silently rewrite
    what every downstream query has to match on. reason is one of the
    _WITHHELD_* constants above, all of them Libex's own vocabulary.
    """
    count = _extras_incident_counts.get(reason, 0) + 1
    _extras_incident_counts[reason] = count
    now = time.monotonic()
    last = _extras_incident_last_logged.get(reason)
    if not window_elapsed(last, now, _EXTRAS_LOG_INTERVAL_SECONDS):
        return
    logger.warning("Audible extras withheld", extra={
        "asin": safe_asin_for_log(asin),
        "region": region,
        "withheld_reason": reason,
        "occurrences": count,
        "blob_bytes": blob_bytes,
    })
    _extras_incident_counts[reason] = 0
    _extras_incident_last_logged[reason] = now


def _strip_podcast_relationships(relationships: list) -> tuple[list, dict[str, int]]:
    """
    Removes the relationship entries named in _STRIPPED_RELATIONSHIP_TYPES,
    returning what is kept and a count per type of what was not.

    The counts are the whole point of returning them: they go into
    extrasWithheld, so the caller is told an episode list existed and how
    long it was. Without that the strip would be exactly the silent drop this
    blob was built to end, hidden inside the mechanism meant to prevent it.
    """
    kept = []
    stripped: dict[str, int] = {}
    for entry in relationships:
        relationship_type = entry.get("relationship_type") if isinstance(entry, dict) else None
        if relationship_type in _STRIPPED_RELATIONSHIP_TYPES:
            stripped[relationship_type] = stripped.get(relationship_type, 0) + 1
        else:
            kept.append(entry)
    return kept, stripped


def _is_unstorable_int(value: int) -> bool:
    """
    True when an int is too wide to render and store -- see
    _INT_BITS_ALWAYS_RENDERABLE for the two ceilings and why this is measured
    in bits.

    Conservative by design. A value just past the bound may well have been
    storable, and is withheld anyway rather than resolved exactly; that band
    begins at a 4,300-digit number, and a withholding that gets recorded is
    the better error than a write the database or the encoder refuses.
    """
    return value.bit_length() > _INT_BITS_ALWAYS_RENDERABLE


def _sanitize_for_jsonb(extras: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """
    Returns a copy of the blob that Postgres jsonb will actually accept, plus
    a count of each thing that had to be changed. Returns None for the copy
    when the blob is nested deeper than _EXTRAS_MAX_DEPTH.

    Three values Python's json parses and re-emits happily that jsonb
    refuses, all of which would otherwise turn one odd product into a failed
    write: U+0000 anywhere in a key or a value, a non-finite float (1e999
    parses to inf, and json.dumps writes it back as the bare token Infinity,
    which is not valid JSON at all), and an int too wide to store or even to
    render (see _is_unstorable_int). A NUL is stripped out of the string
    rather than costing the key or the product -- losing a whole book
    permanently over one invisible byte is the worse of the two outcomes --
    and a number that cannot be stored becomes null. Every one of those is
    counted, and the counts reach the caller in extrasWithheld, because a
    sanitization nothing records is itself a silent drop.

    This lives with the builder rather than at any one place the value is
    stored, because the normalized dict reaches more than one store -- a
    check at a single writer would leave another holding a value the database
    had already rejected, and the two answering differently for the same book.

    Pure, and iterative on an explicit stack. Pure because normalize_products
    hands whole batches to a worker thread past NORMALIZE_THREAD_THRESHOLD;
    iterative because a recursive walk over deeply nested input is itself the
    stack overflow the depth cap exists to prevent, so a recursive
    implementation of this check would be the bug it is checking for.
    """
    counts = {"nulCharacters": 0, "nonFiniteNumbers": 0, "oversizedNumbers": 0}
    root: dict[str, Any] = {}
    # (source container, the copy being built from it, that copy's depth,
    #  counting the blob itself as 1)
    stack: list[tuple[Any, Any, int]] = [(extras, root, 1)]

    while stack:
        source, target, depth = stack.pop()
        pairs = source.items() if isinstance(source, dict) else enumerate(source)
        for key, value in pairs:
            if isinstance(key, str) and "\x00" in key:
                counts["nulCharacters"] += key.count("\x00")
                key = key.replace("\x00", "")

            if isinstance(value, (dict, list)):
                if depth + 1 > _EXTRAS_MAX_DEPTH:
                    return None, counts
                child: Any = {} if isinstance(value, dict) else []
                stack.append((value, child, depth + 1))
            elif isinstance(value, str) and "\x00" in value:
                counts["nulCharacters"] += value.count("\x00")
                child = value.replace("\x00", "")
            elif isinstance(value, float) and not math.isfinite(value):
                counts["nonFiniteNumbers"] += 1
                child = None
            elif isinstance(value, int) and not isinstance(value, bool) and _is_unstorable_int(value):
                # The bool exclusion is not decorative: bool subclasses int,
                # so without it True is measured as a number.
                counts["oversizedNumbers"] += 1
                child = None
            else:
                child = value

            # A list is rebuilt by appending, which holds its order because
            # the whole source container is walked in one pass here and only
            # its children are deferred to the stack.
            if isinstance(target, list):
                target.append(child)
            else:
                target[key] = child

    return root, counts


def bound_extras(blob: dict[str, Any], asin: str, region: str) -> tuple[dict[str, Any] | None, dict[str, int], str | None]:
    """
    Makes one verbatim Audible blob safe to store and bounded in size, and
    says what that cost: (the blob, or None when it is withheld whole; a count
    of each value sanitized; the reason it was withheld whole, or None).

    The one place the sanitizing and both caps are applied. build_extras runs
    a product's passthrough through it, and the chapter normalizer runs each
    of its verbatim groups through it, so a book, a series and a chapter
    listing cannot answer differently for the same input. The caller owns the
    name the reason is filed under in extrasWithheld, because a product has
    one blob and a chapter listing has several. Incidents are logged here, at
    most once per window per reason; the sanitized and withheld logs are
    separate, as they always were.
    """
    sanitized, counts = _sanitize_for_jsonb(blob)
    hits = {name: total for name, total in counts.items() if total}
    if hits:
        _log_extras_incident(asin, region, _WITHHELD_SANITIZED)

    if sanitized is None:
        _log_extras_incident(asin, region, _WITHHELD_DEPTH)
        return None, hits, _WITHHELD_DEPTH

    try:
        # allow_nan=False so anything non-finite that somehow survived above
        # raises here and is recorded, instead of being written out as the
        # Infinity token and becoming invalid JSON nothing would catch until
        # a reader choked on it.
        encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        _log_extras_incident(asin, region, _WITHHELD_UNSERIALIZABLE)
        return None, hits, _WITHHELD_UNSERIALIZABLE

    blob_bytes = len(encoded.encode("utf-8"))
    if blob_bytes > _EXTRAS_MAX_BYTES:
        _log_extras_incident(asin, region, _WITHHELD_SIZE, blob_bytes=blob_bytes)
        return None, hits, _WITHHELD_SIZE

    return sanitized, hits, None


def build_extras(passthrough: dict[str, Any], asin: str, region: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """
    Builds a product's audibleExtras blob and the record of anything withheld
    from it.

    passthrough is every top-level key of the raw product that no first-class
    field reproduces; it is copied, never modified. It goes in verbatim, bar
    the podcast episode entries and what had to be sanitized, all of which are
    recorded. That is the point: the key Audible invents next
    month surfaces on its own, rather than vanishing between the fetch and
    the response with nobody in a position to notice it was ever there. Two
    of the keys that ride along today, social_media_images and
    relationships[].url, are URLs; they are data, and nothing anywhere
    fetches them.

    Returns (None, record) when the blob is dropped whole -- None rather than
    an empty dict, so a consumer merging with what it already holds can tell
    "Libex has nothing to say" from "Audible sent nothing extra", the same
    tri-state the plans field keeps. An empty record means nothing was withheld, and the
    caller omits extrasWithheld entirely in that case.
    """
    extras = dict(passthrough)
    withheld: dict[str, Any] = {}

    relationships = extras.get("relationships")
    if isinstance(relationships, list):
        kept, stripped = _strip_podcast_relationships(relationships)
        if stripped:
            extras["relationships"] = kept
            withheld["relationships"] = stripped

    sanitized, hits, reason = bound_extras(extras, asin, region)
    if hits:
        withheld[_WITHHELD_SANITIZED] = hits
    if reason is not None:
        withheld["audibleExtras"] = reason
    return sanitized, withheld
