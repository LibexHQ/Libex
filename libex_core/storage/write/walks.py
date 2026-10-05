"""
Writers for the stored walk snapshots: which book ASINs the last walk of an
author's or a series' books returned, and whether it reached its end.

Each function takes a session first, raises on failure and does not commit;
the caller owns the transaction. A snapshot is replaced whole, newest walk
winning, and the clock used is the caller's `at`, never the database's.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
from sqlalchemy import bindparam, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from libex_core.asin import is_valid_asin, normalise_asin
from libex_core.storage.models import REGION_ENUM, WalkResult
from libex_core.storage.walk_limits import (
    ASIN_LENGTH,
    MAX_WALK_ASINS,
    SKEW_SECONDS,
    WALK_KINDS,
)
from libex_core.storage.write.support import dialect_of, insert_for

__all__ = ["MAX_WALK_ASINS", "SKEW_SECONDS", "WALK_KINDS", "delete_walk_result", "write_walk_result"]

_SKEW = timedelta(seconds=SKEW_SECONDS)
_PRIMARY_KEY = ["kind", "asin", "region"]


def _check_key(kind: str, asin: str, region: str, at: datetime) -> tuple[str, str]:
    if kind not in WALK_KINDS:
        raise ValueError(f"kind must be one of {WALK_KINDS}, got {kind!r}")
    if region not in REGION_ENUM.enums:
        raise ValueError(f"unknown region {region!r}")
    if not isinstance(asin, str) or len(asin) > ASIN_LENGTH or not is_valid_asin(asin):
        raise ValueError("asin is not a valid ASIN")
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be a timezone-aware datetime")
    return normalise_asin(asin), region


def _clean_asins(book_asins: list[str]) -> tuple[list[str], bool]:
    """The valid, normalised, de-duplicated entries in order, and whether any
    entry had to be dropped (invalid, or beyond the cap)."""
    kept: list[str] = []
    seen: set[str] = set()
    dropped = False
    for entry in book_asins:
        if (
            not isinstance(entry, str)
            or len(entry) > ASIN_LENGTH
            or not is_valid_asin(entry)
        ):
            dropped = True
            continue
        entry = normalise_asin(entry)
        if entry in seen:
            continue
        if len(kept) >= MAX_WALK_ASINS:
            dropped = True
            continue
        seen.add(entry)
        kept.append(entry)
    return kept, dropped


def _replaceable(at: datetime):
    """True for a stored row the walk at `at` may replace: an older one, or
    one dated beyond the clock's tolerance (a stored future would otherwise
    block every later write)."""
    ceiling = datetime.now(timezone.utc) + _SKEW
    return or_(
        WalkResult.confirmed_at < bindparam("walk_at", at, type_=WalkResult.confirmed_at.type),
        WalkResult.confirmed_at > bindparam("walk_ceiling", ceiling, type_=WalkResult.confirmed_at.type),
    )


async def write_walk_result(
    session: AsyncSession,
    *,
    kind: str,
    asin: str,
    region: str,
    book_asins: list[str],
    complete: bool,
    incomplete_reasons: list[str],
    at: datetime,
) -> bool:
    """Records a walk as the snapshot for (kind, asin, region).

    One bound upsert on SQLite and Postgres. An existing row is replaced whole
    when `at` is strictly newer than its confirmed_at, or when its
    confirmed_at lies more than SKEW_SECONDS ahead of the local clock; an
    equal or older `at` leaves it alone. Returns True if the row now holds
    this walk, False if a newer one was kept.

    ValueError, before any statement runs, for a kind other than the two walk
    kinds, an unknown region, an invalid asin, or a naive `at`. Entries of
    `book_asins` are normalised and de-duplicated in order; one that is not a
    valid ASIN, or lies beyond MAX_WALK_ASINS, is dropped and the row is then
    written complete=False, so a shortened list is never recorded as a whole
    one.
    """
    asin, region = _check_key(kind, asin, region, at)
    kept, dropped = _clean_asins(book_asins)
    values = {
        "complete": bool(complete) and not dropped,
        "incomplete_reasons": [str(reason) for reason in incomplete_reasons],
        "book_asins": kept,
        "confirmed_at": at,
    }
    stmt = insert_for(dialect_of(session))(WalkResult).values(
        kind=kind, asin=asin, region=region, **values
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=_PRIMARY_KEY,
        set_={name: getattr(stmt.excluded, name) for name in values},
        where=_replaceable(at),
    ).returning(WalkResult.kind)
    return (await session.execute(stmt)).first() is not None


async def delete_walk_result(
    session: AsyncSession, *, kind: str, asin: str, region: str, at: datetime
) -> bool:
    """Removes the snapshot for (kind, asin, region) on the strength of a
    confirmed absence at `at`.

    Guarded like the write: the row goes only if it is strictly older than
    `at` or future-dated beyond SKEW_SECONDS, so a walk that finished after
    `at` is not undone by it. Returns True if a row was deleted. Same
    ValueError rules as write_walk_result.
    """
    asin, region = _check_key(kind, asin, region, at)
    stmt = (
        delete(WalkResult)
        .where(WalkResult.kind == kind, WalkResult.asin == asin, WalkResult.region == region)
        .where(_replaceable(at))
        .returning(WalkResult.kind)
    )
    return (await session.execute(stmt)).first() is not None
