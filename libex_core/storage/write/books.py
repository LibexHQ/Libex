"""
The batched book writer: a list of normalized books and every row that hangs
off them, in a fixed handful of statements.
"""

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from libex_core.storage.write.entities import upsert_author
from libex_core.storage.write.params import book_params, series_params
from libex_core.storage.write.statements import statements_for
from libex_core.storage.write.support import dialect_of, utc_now


async def resolve_author_ids(
    session: AsyncSession,
    books: list[dict],
    *,
    dialect: str | None = None,
    conflict_errors: tuple[type[BaseException], ...] = (),
) -> dict[tuple[str, str | None], list[int]]:
    """
    Resolves every book's authors to DB ids, keyed by the book's (asin, region)
    -- the same ASIN in two marketplaces is two books with their own authors --
    calling upsert_author once per distinct author rather than once per book
    that names them.

    upsert_author is the one write here that cannot be a bound row: it reads
    before it writes, upgrades null-asin rows in place, and opens SAVEPOINTs
    around the races that entails. So it stays a statement-per-author -- but a
    prolific author's fifty-book chunk names the same author fifty times, and
    each of those repeats costs at minimum a SELECT that can only return what
    the previous one already returned, inside a single transaction that reads
    its own writes.

    The memo key is the whole author payload the writer acts on, not just the
    identity it matches by, so two entries that would merge different
    descriptions or images are still both written. Only a genuinely identical
    repeat is skipped.
    """
    memo: dict[tuple, int | None] = {}
    ids_by_book: dict[tuple[str, str | None], list[int]] = {}

    for data in books:
        ids: list[int] = []
        for author in data.get("authors", []):
            key = (
                author.get("asin"),
                author.get("name", "").strip(),
                author.get("region"),
                author.get("description"),
                author.get("image"),
            )
            if key in memo:
                author_id = memo[key]
            else:
                author_id = await upsert_author(
                    session, author, dialect=dialect, conflict_errors=conflict_errors
                )
                memo[key] = author_id
            if author_id and author_id not in ids:
                ids.append(author_id)
        # Accumulated, not assigned. A chunk can legitimately carry the same
        # ASIN twice -- the catalog's sort windows shift between page fetches,
        # so the same product arrives in two windows -- and the two copies can
        # name different contributors. Assigning here let the second copy
        # replace the first copy's resolved ids, so an author present only on
        # the first copy lost their author_book link entirely: the author row
        # was written, the book row was written, and the relationship between
        # them silently was not. Every sibling pivot below already unions on
        # an (asin, region, x) key.
        merged = ids_by_book.setdefault((data["asin"], data.get("region")), [])
        merged.extend(i for i in ids if i not in merged)

    return ids_by_book


async def write_books(
    session: AsyncSession,
    books: list[dict],
    *,
    dialect: str | None = None,
    conflict_errors: tuple[type[BaseException], ...] = (),
) -> None:
    """
    Issues every statement for a list of books -- their rows plus their genre,
    narrator, series and author relationships -- and nothing else.

    One statement per KIND of row rather than one per book: the whole list's
    book rows go through a single executemany, then the whole list's genres,
    then its book-genre links, and so on. Fifty books cost a fixed handful of
    statements plus one per distinct author, against roughly ten per book
    before. The saving is compile time on the event loop, not round trips --
    see the book upsert for why holding the statement object is not enough on
    its own.

    Owns no transaction: it neither commits nor rolls back, so the caller
    decides whether one book or fifty share a transaction. Every statement is
    an idempotent upsert, which is what lets a caller whose transaction was
    lost replay the same books without double-counting anything. A failing
    statement raises.

    Existing non-null values are never overwritten with null. Pivot
    relationships (genres, narrators, authors) are additive -- never shrink.
    Series position is kept current via upsert.

    A book is identified by (asin, region): every collection below keys on the
    pair, and every link row carries the region of the book (and of the series)
    it points at, so the same ASIN fetched for two marketplaces writes two
    books with two sets of links rather than one overwriting the other.

    Rows are ordered so a table is written before anything referencing it, and
    duplicates are collapsed in Python before binding: the ON CONFLICT DO
    NOTHING sets keep the first of a repeat, matching what a per-row loop
    would have left, and the DO UPDATE sets keep the last, matching the same.
    """
    if not books:
        return

    dialect = dialect or dialect_of(session)
    statements = statements_for(dialect)
    now = utc_now()

    await session.execute(statements.book_upsert, [book_params(book, now) for book in books])

    genres: dict[str, dict] = {}
    book_genres: dict[tuple, dict] = {}
    narrators: dict[str, dict] = {}
    book_narrators: dict[tuple, dict] = {}
    series: dict[tuple, dict] = {}
    book_series_links: dict[tuple, dict] = {}

    for data in books:
        asin = data["asin"]
        region = data.get("region")

        for genre in data.get("genres", []):
            g_asin = genre.get("asin")
            g_name = genre.get("name")
            if not g_asin or not g_name:
                continue
            genres.setdefault(g_asin, {
                "asin": g_asin,
                "name": g_name,
                "type": genre.get("type", "Tags"),
                "created_at": now,
                "updated_at": now,
            })
            book_genres.setdefault((asin, region, g_asin), {
                "book_asin": asin, "book_region": region, "genre_asin": g_asin,
            })

        for narrator in data.get("narrators", []):
            name = narrator.get("name", "").strip()
            if not name:
                continue
            narrators.setdefault(name, {"name": name, "created_at": now, "updated_at": now})
            book_narrators.setdefault((asin, region, name), {
                "book_asin": asin, "book_region": region, "narrator_name": name,
            })

        for entry in data.get("series", []):
            params = series_params(entry, now, region)
            if params is None:
                continue
            # Deduped like every sibling collection here. Fifty books of
            # one series otherwise issued fifty identical upserts against
            # the same row, each re-taking its row lock -- the exact case
            # book_series_links collapses a few lines below, and the case
            # the docstring above already claimed was collapsed.
            series.setdefault((params["asin"], params["region"]), params)
            book_series_links[(asin, region, params["asin"], params["region"])] = {
                "book_asin": asin,
                "book_region": region,
                "series_asin": params["asin"],
                "series_region": params["region"],
                "position": entry.get("position"),
            }

    if genres:
        await session.execute(statements.genre_insert, list(genres.values()))
        await session.execute(statements.book_genre_insert, list(book_genres.values()))

    if narrators:
        await session.execute(statements.narrator_insert, list(narrators.values()))
        await session.execute(statements.book_narrator_insert, list(book_narrators.values()))

    if series:
        await session.execute(statements.series_upsert, list(series.values()))
        await session.execute(statements.book_series_upsert, list(book_series_links.values()))

    ids_by_book = await resolve_author_ids(
        session, books, dialect=dialect, conflict_errors=conflict_errors
    )

    author_books: dict[tuple, dict] = {}
    series_authors: dict[tuple, dict] = {}
    for data in books:
        asin = data["asin"]
        region = data.get("region")
        author_ids = ids_by_book.get((asin, region), [])
        for author_id in author_ids:
            author_books.setdefault((author_id, asin, region), {
                "author_id": author_id, "book_asin": asin, "book_region": region,
            })
        for entry in data.get("series", []):
            s_asin = entry.get("asin")
            if not s_asin:
                continue
            s_region = entry.get("region") or region
            for author_id in author_ids:
                series_authors.setdefault((s_asin, s_region, author_id), {
                    "series_asin": s_asin, "series_region": s_region, "author_id": author_id,
                })

    if author_books:
        await session.execute(statements.author_book_insert, list(author_books.values()))
    if series_authors:
        await session.execute(statements.series_author_insert, list(series_authors.values()))
