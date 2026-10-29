"""
The one writer for a book entered by hand.

Every path that enters a delisted book calls enter_book with the same writer
dict the Audible path produces, so a hand-entered row is stored exactly as a
fetched one is and no input path keeps a field list of its own. It knows
nothing of HTTP: the caller parses and authenticates, and maps the result.

It owns its transaction. It never calls a commit-and-swallow entry point of
the writer module, because a failure here has to reach the caller, and a dry
run has to leave nothing behind.

Inserts or links only. A shared author, series, narrator or genre that is
already stored is linked to and never changed, so a hand entry cannot rewrite
a record Audible or another book supplied. Anything the input says about an
existing entity that the stored row does not already say is a conflict, and
nothing is written. The provenance stamp is set on insert and the stored-row
check below is what keeps it that way: an ASIN already stored for the region
is refused, so a manual entry never reaches the update branch of the upsert.
The caller must not probe Audible inside this transaction and must have
probed before it.
"""

# Standard library
from dataclasses import dataclass, field
from typing import Any

# Third party
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from libex_core.storage.models import Author, Book, Genre, Narrator, Series
from libex_core.storage.read import books as _read
from libex_core.storage.write import entities as _entities
from libex_core.storage.write.books import write_books
from libex_core.storage.write.support import dialect_of, lock_asins, utc_now

STORED = "stored_conflict"
ENTITY = "entity_conflict"
WRITTEN = "written"


class ManualEntryReadbackError(RuntimeError):
    """The rows just written could not be read back, so nothing is committed."""


@dataclass
class ManualEntryResult:
    """
    status is "written" (committed, or rolled back on a dry run),
    "stored_conflict" or "entity_conflict". conflicts carry a field path and a
    fixed reason code, never a value from the input. entities reports, per
    kind, whether each named entity is new or already stored. book and chapters
    are the same-session read-back, set only when status is "written".
    """

    status: str
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    entities: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    book: dict | None = None
    chapters: dict | None = None


def _status(existing: bool) -> str:
    return "existing" if existing else "new"


def _supplied_differs(supplied: dict, stored: dict) -> list[str]:
    """The keys the input supplied (not None) whose value is not the stored one."""
    return [key for key, value in supplied.items() if value is not None and stored.get(key) != value]


async def _guard_genres(session, genres, conflicts, entities) -> None:
    named = {g["asin"]: g for g in genres if g.get("asin") and g.get("name")}
    stored = {}
    if named:
        rows = await session.execute(select(Genre).where(Genre.asin.in_(list(named))))
        stored = {g.asin: g for g in rows.scalars()}
    for index, genre in enumerate(genres):
        asin = genre.get("asin")
        if asin not in named:
            continue
        row = stored.get(asin)
        if row is not None and (row.name != genre["name"] or row.type != genre.get("type", "Tags")):
            conflicts.append({"loc": ["book", "genres", index], "reason": "genre_differs"})
        entry = {"asin": asin, "name": genre["name"], "status": _status(row is not None)}
        if entry not in entities["genres"]:
            entities["genres"].append(entry)


async def _guard_narrators(session, narrators, entities) -> None:
    names = {(n.get("name") or "").strip() for n in narrators} - {""}
    stored = set()
    if names:
        rows = await session.execute(select(Narrator.name).where(Narrator.name.in_(list(names))))
        stored = set(rows.scalars())
    for name in sorted(names):
        entities["narrators"].append({"name": name, "status": _status(name in stored)})


async def _guard_series(session, series, region, conflicts, entities) -> list[dict]:
    """
    Returns the series entries to write: an entry naming a stored series is cut
    down to its link, because the series upsert would otherwise touch the
    stored row's timestamp.
    """
    keys = {(s["asin"], s.get("region") or region) for s in series if s.get("asin")}
    stored = {}
    if keys:
        rows = await session.execute(
            select(Series).where(Series.asin.in_({asin for asin, _ in keys}), Series.region == region)
        )
        stored = {(s.asin, s.region): s for s in rows.scalars()}
    out = []
    for index, entry in enumerate(series):
        key = (entry.get("asin"), entry.get("region") or region)
        row = stored.get(key)
        name = entry.get("name") or entry.get("title")
        if row is None:
            out.append(entry)
            if entry.get("asin") and name:
                item = {"asin": key[0], "name": name, "status": "new"}
                if item not in entities["series"]:
                    entities["series"].append(item)
            continue
        if name and row.title != name:
            conflicts.append({"loc": ["book", "series", index, "name"], "reason": "series_differs"})
        stored_values = {
            "description": row.description,
            "audibleExtras": row.audible_extras,
            "extrasWithheld": row.extras_withheld,
        }
        supplied = {k: entry.get(k) for k in stored_values}
        for k in _supplied_differs(supplied, stored_values):
            conflicts.append({"loc": ["book", "series", index, k], "reason": "series_differs"})
        out.append({"asin": entry["asin"], "region": key[1], "position": entry.get("position")})
        item = {"asin": key[0], "name": row.title, "status": "existing"}
        if item not in entities["series"]:
            entities["series"].append(item)
    return out


async def _guard_authors(session, authors, region, conflicts, entities) -> None:
    named = [(i, a, (a.get("name") or "").strip()) for i, a in enumerate(authors)]
    named = [(i, a, n) for i, a, n in named if n]
    if not named:
        return
    asins = {a["asin"] for _, a, _ in named if a.get("asin")}
    names = {n for _, _, n in named}
    clauses = [Author.name.in_(list(names)) & Author.asin.is_(None)]
    if asins:
        clauses.append(Author.asin.in_(list(asins)))
    rows = await session.execute(select(Author).where(Author.region == region, or_(*clauses)))
    stored = list(rows.scalars())
    seen = set()
    for index, author, name in named:
        asin = author.get("asin")
        if asin:
            by_asin = [r for r in stored if r.asin == asin]
            row = next((r for r in by_asin if r.name == name), None)
            if row is None and by_asin:
                conflicts.append({"loc": ["book", "authors", index, "name"], "reason": "author_differs"})
            elif row is None and any(r.asin is None and r.name == name for r in stored):
                # Writing it would upgrade the asinless row in place.
                conflicts.append({"loc": ["book", "authors", index, "asin"], "reason": "author_asinless_row"})
        else:
            row = next((r for r in stored if r.asin is None and r.name == name), None)
        if row is not None:
            stored_values = {"description": row.description, "image": row.image}
            supplied = {k: author.get(k) for k in stored_values}
            for k in _supplied_differs(supplied, stored_values):
                conflicts.append({"loc": ["book", "authors", index, k], "reason": "author_differs"})
        key = (asin, name)
        if key not in seen:
            seen.add(key)
            entities["authors"].append({"asin": asin, "name": name, "status": _status(row is not None)})


async def _lock_entities(session, book: dict, region: str, dialect: str) -> None:
    """Takes the entity locks in the namespaces and order write_books uses."""
    genres = [g["asin"] for g in book.get("genres", []) if g.get("asin") and g.get("name")]
    narrators = [(n.get("name") or "").strip() for n in book.get("narrators", [])]
    series = [s["asin"] for s in book.get("series", []) if s.get("asin")]
    authors = []
    for author in book.get("authors", []):
        name = (author.get("name") or "").strip()
        authors += [k for k in (author.get("asin"), f"{region}:{name}") if k and name]
    await lock_asins(session, "genre", genres, dialect=dialect)
    await lock_asins(session, "narrator", [n for n in narrators if n], dialect=dialect)
    await lock_asins(session, "series", series, dialect=dialect)
    await lock_asins(session, "author", authors, dialect=dialect)


async def _enter(session, book, chapters, region, dialect) -> ManualEntryResult:
    asin = book["asin"]
    if book.get("region") != region:
        raise ValueError("book region does not match the entry region")
    data = {**book, "manualEntry": True}
    data["authors"] = [{**a, "region": region} for a in book.get("authors", [])]

    await lock_asins(session, "book", [asin], dialect=dialect)
    held = await session.execute(select(Book.asin).where(Book.asin == asin, Book.region == region))
    if held.first() is not None:
        return ManualEntryResult(STORED, [{"loc": ["book", "asin"], "reason": "stored"}])

    await _lock_entities(session, data, region, dialect)
    conflicts: list[dict] = []
    entities: dict[str, list] = {"authors": [], "narrators": [], "series": [], "genres": []}
    await _guard_genres(session, data.get("genres", []), conflicts, entities)
    await _guard_narrators(session, data.get("narrators", []), entities)
    data["series"] = await _guard_series(session, data.get("series", []), region, conflicts, entities)
    await _guard_authors(session, data["authors"], region, conflicts, entities)
    if conflicts:
        return ManualEntryResult(ENTITY, conflicts, entities)

    await write_books(session, [data], dialect=dialect, confirm=False)
    if chapters:
        stored = await _entities.write_track(
            session, asin, chapters, region=region, dialect=dialect, confirm=False
        )
        if stored is None:
            raise ManualEntryReadbackError("chapters were not stored")
        await session.execute(
            update(Book)
            .where(Book.asin == asin, Book.region == region)
            .values(chapters_checked_at=utc_now())
            .execution_options(synchronize_session=False)
        )

    # The core readers, on this session: the guarded app readers roll it back.
    stored_book = await _read.get_book(session, asin, region=region)
    stored_chapters = await _read.get_track(session, asin, region=region) if chapters else None
    if stored_book is None or (chapters and stored_chapters is None):
        raise ManualEntryReadbackError("the written rows could not be read back")
    return ManualEntryResult(WRITTEN, [], entities, stored_book, stored_chapters)


async def enter_book(
    session: AsyncSession,
    book: dict,
    chapters: dict | None,
    *,
    region: str,
    dry_run: bool,
) -> ManualEntryResult:
    """
    Stores one hand-entered book, its pivots and optional chapters in a single
    transaction, and commits it unless dry_run, which rolls everything back
    after the read-back so the result shows what a commit would store.

    book is the writer dict (the shape write_books takes) for the given region;
    manualEntry is set here. chapters is the dumped chapters payload or None.
    A stored_conflict or entity_conflict result has written nothing. Any other
    failure rolls back and raises.
    """
    try:
        result = await _enter(session, book, chapters, region, dialect_of(session))
        if result.status == WRITTEN and not dry_run:
            await session.commit()
        else:
            await session.rollback()
    except BaseException:
        await session.rollback()
        raise
    return result
