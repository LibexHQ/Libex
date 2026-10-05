"""
Store-first lists: the shared logic behind max_age on the series and author
book lookups.

With a store, every live walk of a list records which book ASINs it returned
and whether it reached its end (a snapshot, replaced whole by the newest walk).
A caller who passes max_age may then be answered from that snapshot with no
request to Audible, but only when the snapshot is complete, fresh, well formed,
and every book it names is stored for exactly the region asked. Anything else
is a miss and the walk is made live. A confirmed absence on Audible removes the
snapshot and is never answered from the store.

The snapshot is read back as untrusted input. Every failure to read or judge it
is a miss, never an error to the caller, and no ASIN read from it is ever sent
to Audible: the answer is made wholly from the store or discarded. Logs carry
counts, the kind, the region, the ASIN of the lookup and error types, never what
a row held.

Residual risk, accepted: a stored JSON value is parsed in full before it can be
rejected, so an oversized one costs memory and time first. The database is the
caller's own.
"""

# Standard library
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

# Core
from libex_core.asin import is_valid_asin, normalise_asin
from libex_core.lookup import _store
from libex_core.storage.walk_limits import ASIN_LENGTH, MAX_WALK_ASINS, SKEW_SECONDS

if TYPE_CHECKING:
    from libex_core.lookup.books import Hydration
    from libex_core.storage.store import LocalStore

logger = logging.getLogger("libex")

_SKEW = timedelta(seconds=SKEW_SECONDS)


class _Miss(Exception):
    """A snapshot that cannot be served; carries only the reason, one of
    absent, stale, incomplete, malformed or unreadable."""


def check_max_age(max_age: Any, store: "LocalStore | None") -> None:
    """ValueError, before anything is sent, for a max_age that is not a positive
    timedelta or that is given without a store to read a snapshot from."""
    if max_age is None:
        return
    if not isinstance(max_age, timedelta):
        raise ValueError("max_age must be a datetime.timedelta")
    if max_age <= timedelta(0):
        raise ValueError("max_age must be greater than zero")
    if store is None:
        raise ValueError("max_age needs a store to read a stored list from")


def _validated_asins(value: Any) -> list[str]:
    """The snapshot's ASINs, normalised and de-duplicated in order. Any entry
    that is not a valid ASIN spoils the whole snapshot; none is filtered out."""
    if type(value) is not list or not 0 < len(value) <= MAX_WALK_ASINS:
        raise _Miss("malformed")
    asins: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if type(entry) is not str or len(entry) > ASIN_LENGTH or not is_valid_asin(entry):
            raise _Miss("malformed")
        entry = normalise_asin(entry)
        if entry not in seen:
            seen.add(entry)
            asins.append(entry)
    return asins


def _age(confirmed_at: Any, max_age: timedelta) -> timedelta:
    """How old the snapshot is, or a stale/malformed miss. Computed as an age
    against the local clock, never as a cutoff, so no max_age can overflow."""
    if not isinstance(confirmed_at, datetime) or confirmed_at.utcoffset() is None:
        raise _Miss("malformed")
    age = datetime.now(timezone.utc) - confirmed_at
    if not -_SKEW <= age <= max_age:
        raise _Miss("stale")
    return age


async def _judged(
    store: "LocalStore", kind: str, asin: str, region: str, max_age: timedelta
) -> tuple[list[dict[str, Any]], datetime, timedelta]:
    row = await _store.stored_walk(store, kind, asin, region)
    if row is _store._READ_FAILED:
        raise _Miss("unreadable")
    if row is None:
        raise _Miss("absent")
    if not isinstance(row, dict):
        raise _Miss("malformed")
    complete = row.get("complete")
    if complete is False:
        raise _Miss("incomplete")
    if complete is not True:
        raise _Miss("malformed")
    confirmed_at = row.get("confirmed_at")
    age = _age(confirmed_at, max_age)
    asins = _validated_asins(row.get("book_asins"))
    books = await _store.stored_books(store, asins, region)
    if len(books) != len(asins):
        raise _Miss("incomplete")
    return books, confirmed_at, age


async def stored_list(
    store: "LocalStore", kind: str, asin: str, region: str, max_age: timedelta
) -> tuple[list[dict[str, Any]], datetime] | None:
    """The books of a fresh, complete snapshot with when it was confirmed, or
    None for a miss. The books are the stored rows, settled, in the snapshot's
    order and before any filtering. Never raises for anything the store holds."""
    try:
        books, confirmed_at, age = await _judged(store, kind, asin, region, max_age)
    except _Miss as miss:
        reason = str(miss)
    except Exception as exc:
        logger.warning("Stored list unusable", extra={
            "kind": kind,
            "region": region,
            "list_asin": asin,
            "error_type": type(exc).__name__,
        })
        reason = "malformed"
    else:
        logger.info("Answered from a stored list", extra={
            "kind": kind,
            "region": region,
            "list_asin": asin,
            "book_num": len(books),
            "age_seconds": round(age.total_seconds(), 1),
        })
        return books, confirmed_at
    logger.info("Stored list not used", extra={
        "kind": kind,
        "region": region,
        "list_asin": asin,
        "reason": reason,
    })
    return None


async def record_walk(
    store: "LocalStore",
    kind: str,
    asin: str,
    region: str,
    result: Any,
    hydration: "Hydration",
    at: datetime,
) -> Any:
    """Records a live BookList as the snapshot for its walk and returns it, with
    store_write_failed set when the snapshot could not be written.

    The ASINs are those of hydration.books before filtering and sorting, in the
    order served. The snapshot is complete only when the list was, nothing in it
    was answered from the store in place of Audible, and every book was
    written, since only then does the store hold the whole list as Audible gave
    it. An incomplete walk is recorded all the same, replacing an older
    snapshot, so that one that no longer describes the list is never served.
    """
    book_asins = [b["asin"] for b in hydration.books if b.get("asin")]
    complete = (
        bool(result.complete)
        and not hydration.from_store
        and not result.store_write_failed
    )
    written = await _store.persist_walk(
        store,
        kind,
        asin,
        region,
        book_asins,
        complete=complete,
        incomplete_reasons=result.incomplete_reasons,
        at=at,
    )
    return result if written else replace(result, store_write_failed=True)


async def forget_walk(
    store: "LocalStore", kind: str, asin: str, region: str, at: datetime
) -> None:
    """Drops the snapshot after Audible confirmed there are no books."""
    await _store.forget_walk(store, kind, asin, region, at=at)
