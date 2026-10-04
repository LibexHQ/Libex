"""
Reading what a LocalStore holds, without asking Audible.

The live lookups answer from Audible and use the store as a fallback; these
read the store alone, each returning the model the live lookup returns for the
same record, wrapped in a Stored that says which region it is the record of
and when Audible last confirmed it. The wrapper carries confirmed_at so that
the published response models do not have to: it never appears in a
BookResponse, ChapterResponse, SeriesResponse or AuthorResponse.

confirmed_at is the moment a real Audible answer for that very record was
last written: a product fetch for a book, a series profile fetch for a series,
an author profile fetch for an author, a chapters answer (a listing, or a
404 or empty answer) for a book's chapters, readable through
chapters_confirmed_at even when there is no listing to return. None means no
such answer has been recorded, which is not the same as the record being stale or wrong; a
record a book merely mentioned, or one stored before the stamp existed, is
None. It is read from the store as it stands, so it never says more than the
store knows.

Region is required, with no default, and is the region of the record: a book
or series ASIN is region-specific, so nothing stored for another marketplace
is ever returned. An identifier that is not ASIN-shaped cannot be stored and
reads as a miss. A closed store raises StoreClosed before anything is read;
a read that fails is logged and reads as a miss, as it does for the lookups.
"""

# Standard library
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

# Core
from libex_core.asin import is_valid_asin, normalise_asin
from libex_core.audible.client import validate_region
from libex_core.lookup import _store
from libex_core.models import AuthorResponse, BookResponse, ChapterResponse, SeriesResponse

if TYPE_CHECKING:
    from libex_core.storage.store import LocalStore

__all__ = [
    "Stored",
    "chapters_confirmed_at",
    "stored_author",
    "stored_book",
    "stored_books",
    "stored_chapters",
    "stored_series",
]


@dataclass(frozen=True)
class Stored[T]:
    """A stored record, the region it belongs to, and when Audible last
    confirmed it (None when no confirmation has been recorded)."""

    value: T
    region: str
    confirmed_at: datetime | None


def _canonical(asin: Any) -> str | None:
    """The uppercase form of an ASIN, or None for a value that cannot be one
    and so cannot be stored."""
    if not isinstance(asin, str) or not is_valid_asin(asin):
        return None
    return normalise_asin(asin)


async def _prepare(store: "LocalStore", region: str) -> str:
    """The validated region, after the store has been checked usable."""
    region = validate_region(region)
    await _store.check(store)
    return region


async def _stamps(
    store: "LocalStore", what: str, asins: list[str], region: str, column: str
) -> dict[str, datetime | None]:
    """The confirmation stamp of each stored record, by ASIN, in chunks. For
    authors a row exists per spelling of a name, and the latest stamp of any of
    them is the one reported."""
    from sqlalchemy import select

    from libex_core.storage import models

    model = {
        "books": models.Book,
        "chapters": models.Book,
        "series": models.Series,
        "author": models.Author,
    }[what]
    field = getattr(model, column)

    async def read(session: Any) -> dict[str, datetime | None]:
        stamps: dict[str, datetime | None] = {}
        for start in range(0, len(asins), _store.READ_CHUNK_SIZE):
            chunk = asins[start:start + _store.READ_CHUNK_SIZE]
            result = await session.execute(
                select(model.asin, field).where(model.asin.in_(chunk), model.region == region)
            )
            for asin, stamp in result.all():
                held = stamps.get(asin)
                stamps[asin] = stamp if held is None or (stamp is not None and stamp > held) else held
        return stamps

    return await _store._read(store, f"{what} confirmation", read, {})


async def stored_books(
    store: "LocalStore", asins: list[str], *, region: str
) -> list[Stored[BookResponse]]:
    """The stored books for these ASINs in this region, in the order asked;
    those not stored are left out. Shaped as the live lookup shapes a book:
    settled, as BookResponse."""
    region = await _prepare(store, region)
    wanted = [a for a in map(_canonical, asins) if a]
    if not wanted:
        return []
    rows = {
        row["asin"]: row
        for row in await _store.stored_books(store, wanted, region)
    }
    stamps = await _stamps(store, "books", sorted(set(rows)), region, "confirmed_at")
    return [
        Stored(BookResponse(**rows[a]), region, stamps.get(a))
        for a in wanted
        if a in rows
    ]


async def stored_book(
    store: "LocalStore", asin: str, *, region: str
) -> Stored[BookResponse] | None:
    """The stored book in this region, or None when it is not stored."""
    found = await stored_books(store, [asin], region=region)
    return found[0] if found else None


async def stored_chapters(
    store: "LocalStore", asin: str, *, region: str
) -> Stored[ChapterResponse] | None:
    """The stored chapter listing of the book in this region, or None when
    there is none. confirmed_at is when Audible last answered with that
    listing. A confirmed absence (an answer with nothing to list) has no
    listing to return here; it is recorded in chapters_confirmed_at on the
    book and is read from there."""
    region = await _prepare(store, region)
    canonical = _canonical(asin)
    if canonical is None:
        return None
    listing = await _store.stored_track(store, canonical, region)
    if not listing:
        return None
    stamps = await _stamps(store, "chapters", [canonical], region, "chapters_confirmed_at")
    return Stored(ChapterResponse(**listing), region, stamps.get(canonical))


async def chapters_confirmed_at(
    store: "LocalStore", asin: str, *, region: str
) -> datetime | None:
    """When Audible last answered for the book's chapters in this region, or
    None when it never has or the book is not stored. Unlike stored_chapters
    it also speaks for an empty answer: a stamp with no stored listing is a
    confirmed absence of chapters, None is a question never asked."""
    region = await _prepare(store, region)
    canonical = _canonical(asin)
    if canonical is None:
        return None
    stamps = await _stamps(store, "chapters", [canonical], region, "chapters_confirmed_at")
    return stamps.get(canonical)


async def stored_series(
    store: "LocalStore", asin: str, *, region: str
) -> Stored[SeriesResponse] | None:
    """The stored series record in this region, or None when it is not
    stored."""
    region = await _prepare(store, region)
    canonical = _canonical(asin)
    if canonical is None:
        return None
    record = await _store.stored_series(store, canonical, region)
    if not record:
        return None
    stamps = await _stamps(store, "series", [canonical], region, "confirmed_at")
    return Stored(SeriesResponse(**record), region, stamps.get(canonical))


async def stored_author(
    store: "LocalStore", asin: str, *, region: str
) -> Stored[AuthorResponse] | None:
    """The stored author in this region, or None when not stored. The author's
    books are not part of it; the live author lookup does not return them
    either."""
    region = await _prepare(store, region)
    canonical = _canonical(asin)
    if canonical is None:
        return None
    record = await _store.stored_author(store, canonical, region)
    if not record:
        return None
    stamps = await _stamps(store, "author", [canonical], region, "confirmed_at")
    return Stored(AuthorResponse(**record), region, stamps.get(canonical))
