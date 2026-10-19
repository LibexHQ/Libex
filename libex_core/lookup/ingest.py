"""
Writing a Libex server's answers into a LocalStore.

A hosted or self-hosted Libex returns the published response models, already
settled for the wire, and these functions take those models, parsed, and write
them under the same merge rules the live lookups use: an existing value is
never replaced by less, and the relationship rows only ever grow. Nothing is
fetched, and nothing is stamped as confirmed: a server's answer may itself have
come from its cache, so confirmed_at and chapters_confirmed_at are left exactly
as they were and only a real Audible answer, through the lookups, moves them.

Region is required, with no default, and is the region of every record passed:
a record whose own region says otherwise is refused with ValueError before
anything is written, since filing it under the wrong marketplace would be
silent. A closed store raises StoreClosed first. A write that fails is logged
and reported through the return value, as it is for the lookups, and never
raised.

Settled values the writer must not take as facts. The writer needs the
tri-state flags as the normalizer produced them, where None means Audible said
nothing, and a response cannot carry that: settling turned every None into a
default, so the default is indistinguishable from an answer that happened to
agree with it. ingest_books therefore reads back only what a response can
still tell apart:

- explicit, hasPdf, whisperSync and isVvab settle to False. A False is
  therefore passed on as unknown (None); a True is an assertion and is kept.
- isListenable and isBuyable settle to True. A True is passed on as unknown;
  a False is an assertion and is kept. isAvailable is the same Audible value
  as isBuyable and the store keeps one column for both, so it is not read.
- plans settles to an empty list. An empty list is passed on as unknown (None),
  never as the fact that the book is in no plan; a non-empty list is kept.

A flag or plans the model was built without is unknown too, whatever default
the model fills in for it.

Unknown writes nothing over a stored value and takes the column's default on a
new row, which is what a first normalization of a silent product would have
stored. The cost is that a server's explicit False for the first four, or
True for the other two, cannot be recorded by a store that has none yet; the
direct lookups can.

Fields the response carries and the store derives or does not keep are not
written: a record's updatedAt and regions, an author's id, a series' position.
Chapter payloads are written with their null parts left out, so an
explicit null contentReference or contentUrl is stored as absent. An empty
chapter listing is not written: it says nothing about the book, and the
absence of chapters is Audible's to confirm.
"""

# Standard library
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

# Core
from libex_core.asin import is_valid_asin, normalise_asin
from libex_core.audible.client import validate_region
from libex_core.lookup import _store
from libex_core.models import AuthorResponse, BookResponse, ChapterResponse, SeriesResponse

if TYPE_CHECKING:
    from libex_core.storage.store import LocalStore

__all__ = [
    "ingest_author",
    "ingest_books",
    "ingest_chapters",
    "ingest_series",
]

logger = logging.getLogger("libex")

# The flags whose settled default is the given value; see the module docstring.
_SETTLES_TO_TRUE = ("isListenable", "isBuyable")
_SETTLES_TO_FALSE = ("explicit", "hasPdf", "whisperSync", "isVvab")

# Carried by the response, derived or unused by the store.
_BOOK_DROPPED = ("regions", "updatedAt", "isAvailable")


def _check_region(records: Sequence[Any], region: str) -> str:
    """The validated region; ValueError when a record names another."""
    region = validate_region(region)
    if any(r.region != region for r in records):
        raise ValueError("a record's region is not the region it is ingested for")
    return region


def _unsettled(book: BookResponse, region: str) -> dict[str, Any]:
    """The book as the writer takes it, with every settled default read back
    as unknown."""
    data = book.model_dump()
    for key in _BOOK_DROPPED:
        data.pop(key, None)
    for key in _SETTLES_TO_TRUE:
        if key not in book.model_fields_set or data[key] is True:
            data[key] = None
    for key in _SETTLES_TO_FALSE:
        if key not in book.model_fields_set or data[key] is False:
            data[key] = None
    if "plans" not in book.model_fields_set or not data["plans"]:
        data["plans"] = None
    for author in data["authors"]:
        author["region"] = author.get("region") or region
    return data


async def ingest_books(
    store: "LocalStore", books: Sequence[BookResponse], *, region: str
) -> set[str]:
    """
    Writes the books, with their authors, narrators, genres and series, and
    returns the ASINs that were committed. A book whose ASIN is not ASIN-shaped
    cannot be stored and is left out. Writes are made in chunks, so a chunk that
    fails is logged and its books are missing from the result while the others
    stand.
    """
    region = _check_region(books, region)
    await _store.check(store)
    prepared = []
    for book in books:
        if not is_valid_asin(book.asin):
            continue
        data = _unsettled(book, region)
        data["asin"] = normalise_asin(book.asin)
        prepared.append(data)
    if not prepared:
        return set()
    written, _ = await _store.persist_books(store, prepared, region)
    return written


async def ingest_chapters(
    store: "LocalStore", asin: str, chapters: ChapterResponse, *, region: str
) -> bool:
    """
    Writes a book's chapter listing under the book's record for the region,
    keeping the richer of the stored and the offered. False when nothing was
    written: the listing is empty, the book is not stored for the region (ingest
    the book first), or the write failed.
    """
    region = validate_region(region)
    await _store.check(store)
    if not chapters.chapters or not isinstance(asin, str) or not is_valid_asin(asin):
        return False
    return await _store.persist_track(
        store,
        normalise_asin(asin),
        chapters.model_dump(exclude_none=True),
        region,
    )


async def ingest_series(
    store: "LocalStore", series: SeriesResponse, *, region: str
) -> bool:
    """Writes a series profile. False when the write failed or the series has no
    ASIN or name to key it by."""
    region = _check_region([series], region)
    await _store.check(store)
    if not is_valid_asin(series.asin):
        return False
    data = {
        "asin": normalise_asin(series.asin),
        "name": series.name,
        "description": series.description,
        "region": region,
        "audibleExtras": series.audibleExtras,
        "extrasWithheld": series.extrasWithheld,
    }
    return await _store.persist_series(store, data, region)


async def ingest_author(
    store: "LocalStore", author: AuthorResponse, *, region: str
) -> bool:
    """Writes an author profile: name, description, image and genres. The
    author's books are not part of it. False when the write failed."""
    region = _check_region([author], region)
    await _store.check(store)
    if not is_valid_asin(author.asin):
        return False
    data = {
        "asin": normalise_asin(author.asin),
        "name": author.name,
        "description": author.description,
        "image": author.image,
        "region": region,
        "genres": [g for g in author.genres if isinstance(g, dict)],
    }
    return await _store.persist_author(store, data, region)
