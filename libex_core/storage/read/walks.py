"""
Reader for the stored walk snapshots.
"""

# Standard library
from typing import Any

# Third party
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from libex_core.storage.models import WalkResult


async def get_walk_result(
    session: AsyncSession, kind: str, asin: str, region: str
) -> dict[str, Any] | None:
    """The snapshot for (kind, asin, region), or None if there is none.

    Returns the row's values as stored and does not check them:
    {"book_asins", "complete", "incomplete_reasons", "confirmed_at"}. Whether
    book_asins is a list of valid ASINs, complete is a real boolean and
    confirmed_at is fresh is for the caller to judge strictly before it
    serves anything from the row; this reader never repairs, filters or
    truncates. Raises on a database failure, and a stored document that
    cannot be decoded raises too.

    Residual risk: an oversized JSON value is parsed in full before the
    caller can reject it. The database is the caller's own.
    """
    stmt = select(
        WalkResult.book_asins,
        WalkResult.complete,
        WalkResult.incomplete_reasons,
        WalkResult.confirmed_at,
    ).where(
        WalkResult.kind == kind,
        WalkResult.asin == asin,
        WalkResult.region == region,
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return {
        "book_asins": row.book_asins,
        "complete": row.complete,
        "incomplete_reasons": row.incomplete_reasons,
        "confirmed_at": row.confirmed_at,
    }
