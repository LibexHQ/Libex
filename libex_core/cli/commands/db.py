"""
`libex-core db`: the local store, and reading what it holds.

One command per hosted /db route, answering from the store alone and printing
the same shape the route does. Always listed, whether or not storage is on, so
the help, the man page and the completions are the same everywhere; with
storage off a command says so and exits 5.
"""

import argparse
from typing import Any

from libex_core.cli._args import REGIONS, add_command, add_group
from libex_core.cli._db_args import (
    AUDIOBOOKS_PRODUCED,
    NARRATOR_SORT_FIELDS,
    add_book_filters,
    add_book_sort,
    add_db_paging,
    book_filter_kwargs,
    has_book_filter,
)
from libex_core.cli.commands.releases import WINDOWS

_REGION_HELP = "the marketplace of the author, default us"
_RECORD_REGION_HELP = "the record of this marketplace, default the one stored first"
_AUTHOR_BOOKS_EXCLUDED = frozenset({"region", "author_name"})
_SERIES_BOOKS_EXCLUDED = frozenset({"series_name"})


def asin_argument(text: str) -> str:
    """An ASIN, in the uppercase form it is stored in. The message is fixed so
    a rejected value is never echoed back."""
    from libex_core.asin import is_valid_asin, normalise_asin

    if not is_valid_asin(text):
        raise argparse.ArgumentTypeError("must be an ASIN")
    return normalise_asin(text)


def _add_record_region(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--region", choices=REGIONS, help=_RECORD_REGION_HELP)


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    db = add_group(
        subparsers,
        "db",
        "set up the local store and read what it holds",
        "Work with the local store, a database of what earlier lookups "
        "returned. It is off unless LIBEX_CORE_STORAGE is set, and needs the "
        "storage extra. These commands answer from the store alone and make "
        "no request to Audible. Run db upgrade once to create it. Each read "
        "command matches the hosted service's /db route of the same name and "
        "prints the same shape; one that finds nothing exits 3. A command "
        "exits 5 when storage is off or the store is not ready.",
    )

    upgrade = add_command(
        db,
        "upgrade",
        "create the store or bring its schema up to date",
        "Create the store, or bring its schema to the one this version "
        "ships, and print the revision as JSON. It is the only command that "
        "changes the database's structure and it never runs on its own. It "
        "refuses a database that holds tables this tool did not create.",
    )
    upgrade.set_defaults(handler=run_upgrade)

    status = add_command(
        db,
        "status",
        "show whether the store is ready",
        "Print the state of the store as JSON: ok, not-initialised, "
        "outdated, ahead (made by a newer version) or foreign (not made by "
        "this tool), with the revision it records. Exits 0 only when the "
        "state is ok and 5 otherwise. Nothing is created or changed.",
    )
    status.set_defaults(handler=run_status)

    book = add_command(db, "book", "one stored book", "Print one stored book as JSON. A book is stored once per marketplace; "
        "--region picks one, and without it the record stored first is printed.")
    book.add_argument("asin", metavar="ASIN", type=asin_argument, help="ASIN of the book")
    _add_record_region(book)
    book.set_defaults(handler=run_book)

    books = add_command(
        db,
        "books",
        "search the stored books",
        "Print the stored books that match every filter given, as JSON. "
        "Give at least one filter or --sort.",
    )
    add_book_filters(books)
    add_book_sort(books)
    add_db_paging(books)
    books.set_defaults(handler=run_books)

    chapters = add_command(
        db, "chapters", "the stored chapters of a book", "Print a stored book's chapters as JSON. --region picks the marketplace's "
        "listing, and without it the one stored first is printed."
    )
    chapters.add_argument("asin", metavar="ASIN", type=asin_argument, help="ASIN of the book")
    _add_record_region(chapters)
    chapters.set_defaults(handler=run_chapters)

    sku = add_command(
        db,
        "sku",
        "stored books of one SKU group",
        "Print every stored book of a SKU group as JSON, which is usually "
        "the same title in several marketplaces.",
    )
    sku.add_argument("sku", metavar="SKU", help="SKU group identifier")
    sku.set_defaults(handler=run_sku)

    author = add_command(db, "author", "one stored author", "Print one stored author as JSON.")
    author.add_argument("asin", metavar="ASIN", type=asin_argument, help="ASIN of the author")
    author.add_argument("--region", choices=REGIONS, default="us", help=_REGION_HELP)
    author.set_defaults(handler=run_author)

    author_books = add_command(
        db,
        "author-books",
        "stored books of an author",
        "Print every stored book of an author as JSON, with every filter "
        "that applies. --region picks the author's marketplace and "
        "--book-region narrows the books.",
    )
    author_books.add_argument("asin", metavar="ASIN", type=asin_argument, help="ASIN of the author")
    author_books.add_argument("--region", choices=REGIONS, default="us", help=_REGION_HELP)
    author_books.add_argument(
        "--book-region", choices=REGIONS, help="only books of this marketplace"
    )
    add_book_filters(author_books, _AUTHOR_BOOKS_EXCLUDED)
    add_book_sort(author_books)
    author_books.set_defaults(handler=run_author_books)

    series = add_command(db, "series", "one stored series", "Print one stored series as JSON. --region picks the marketplace's "
        "record, and without it the one stored first is printed.")
    series.add_argument("asin", metavar="ASIN", type=asin_argument, help="ASIN of the series")
    _add_record_region(series)
    series.set_defaults(handler=run_series)

    series_books = add_command(
        db,
        "series-books",
        "stored books of a series",
        "Print every stored book of a series as JSON, in series order unless sorted.",
    )
    series_books.add_argument("asin", metavar="ASIN", type=asin_argument, help="ASIN of the series")
    add_book_filters(series_books, _SERIES_BOOKS_EXCLUDED)
    add_book_sort(series_books, note=", replacing series order")
    series_books.set_defaults(handler=run_series_books)

    narrators = add_command(
        db,
        "narrators",
        "search stored narrator profiles",
        "Print the stored narrator profiles whose name contains the text, as JSON.",
    )
    narrators.add_argument("name", metavar="NAME", help="text in the narrator's name")
    narrators.add_argument("--gender", help="only narrators whose gender contains this text")
    narrators.add_argument("--language", help="only narrators who work in this language")
    narrators.add_argument(
        "--audiobooks-produced",
        choices=tuple(AUDIOBOOKS_PRODUCED),
        help="only narrators in this audiobooks-produced bucket",
    )
    narrators.add_argument("--source", help="only profiles from this source")
    narrators.add_argument(
        "--cultural-heritage", help="only narrators whose cultural heritage contains this text"
    )
    narrators.add_argument("--sort", choices=NARRATOR_SORT_FIELDS, help="sort by this field")
    narrators.add_argument(
        "--order", choices=("asc", "desc"), default="asc", help="the direction of --sort, default asc"
    )
    add_db_paging(narrators)
    narrators.set_defaults(handler=run_narrators)

    narrator_books = add_command(
        db,
        "narrator-books",
        "stored books of a narrator",
        "Print the stored books read by the narrator, matched by exact name, as JSON.",
    )
    narrator_books.add_argument("name", metavar="NAME", help="name of the narrator")
    add_book_filters(narrator_books)
    add_book_sort(narrator_books)
    add_db_paging(narrator_books)
    narrator_books.set_defaults(handler=run_narrator_books)

    genres = add_command(
        db,
        "genres",
        "stored genre and tag names",
        "Print every distinct genre and tag name in the store as JSON.",
    )
    genres.add_argument("--search", help="only names that contain this text")
    genres.set_defaults(handler=run_genres)

    plans = add_command(
        db, "plans", "stored Audible plan names", "Print every distinct plan name in the store as JSON."
    )
    plans.set_defaults(handler=run_plans)

    plan = add_command(
        db, "plan", "stored books of one plan", "Print the stored books available under an Audible plan, as JSON."
    )
    plan.add_argument("name", metavar="NAME", help="plan name, as db plans prints it")
    add_book_filters(plan, frozenset({"plan_name"}))
    add_book_sort(plan)
    add_db_paging(plan)
    plan.set_defaults(handler=run_plan)

    vvab = add_command(
        db,
        "vvab",
        "stored virtual voice audiobooks",
        "Print the stored virtual voice (AI-narrated) audiobooks as JSON.",
    )
    add_book_filters(vvab, frozenset({"is_vvab"}))
    add_book_sort(vvab)
    add_db_paging(vvab)
    vvab.set_defaults(handler=run_vvab)

    for name, summary, word, order, handler in (
        ("new-releases", "stored books released recently", "back to look", "desc", run_new_releases),
        ("coming-soon", "stored books about to be released", "ahead to look", "asc", run_coming_soon),
    ):
        window = add_command(
            db,
            name,
            summary,
            "Print the stored books released in the window as JSON, "
            + ("newest" if order == "desc" else "soonest")
            + " first unless sorted. Titles that are not yet out, or are "
            "already out, and the no-date-yet placeholder are left out as "
            "the hosted route leaves them out.",
        )
        window.add_argument(
            "--days", choices=WINDOWS, default="30", help=f"how many days {word}, default 30"
        )
        add_book_filters(window)
        add_book_sort(window, order)
        add_db_paging(window)
        window.set_defaults(handler=handler)

    stats = add_command(
        db,
        "stats",
        "counts of what the store holds",
        "Print how many book records, distinct book ASINs, authors, narrators, series and books with "
        "chapters the store holds, as JSON. --region scopes all but "
        "narrators, and then seriesRegionUnknown counts the series that have "
        "no region.",
    )
    stats.add_argument("--region", choices=REGIONS, help="count only this marketplace, default all")
    stats.set_defaults(handler=run_stats)


# ------------------------------------------------------------
# Handlers
# ------------------------------------------------------------

def _found(value: Any, message: str) -> Any:
    """The value, or the not-found failure with a message that holds nothing
    the caller typed."""
    from libex_core.exceptions import ErrorCode, NotFoundException

    if not value:
        raise NotFoundException(message, code=ErrorCode.NOT_IN_LIBEX)
    return value


def _books(rows: list[dict[str, Any]], message: str = "no matching books in the local store") -> list[Any]:
    from libex_core.models import BookResponse

    return [BookResponse(**row) for row in _found(rows, message)]


def _shape(args: argparse.Namespace, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    return {
        **book_filter_kwargs(args, exclude),
        "sort": args.sort,
        "order": args.order,
    }


def _page(args: argparse.Namespace) -> dict[str, Any]:
    return {"limit": args.limit, "page": args.page}


def run_upgrade(args: argparse.Namespace) -> int:
    import asyncio

    from libex_core.cli.environment import load_config
    from libex_core.cli.exit_codes import ExitCode
    from libex_core.cli.output import emit_json
    from libex_core.cli.session import build_store, upgrade_store

    async def upgrade() -> str:
        store = build_store(load_config())
        try:
            return await upgrade_store(store)
        finally:
            await store.close()

    emit_json({"revision": asyncio.run(upgrade())})
    return ExitCode.OK


def run_status(args: argparse.Namespace) -> int:
    import asyncio

    from libex_core.cli import store_state
    from libex_core.cli.environment import load_config
    from libex_core.cli.exit_codes import ExitCode
    from libex_core.cli.output import emit_json
    from libex_core.cli.session import build_store, schema_state

    async def status() -> tuple[str, str | None]:
        store = build_store(load_config())
        try:
            return await schema_state(store)
        finally:
            await store.close()

    state, revision = asyncio.run(status())
    emit_json({"state": state, "revision": revision})
    if state != store_state.OK:
        raise store_state.StoreNotReady(store_state.MESSAGES[state])
    return ExitCode.OK


def run_book(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.models import BookResponse
        from libex_core.storage.read.books import get_book

        return BookResponse(**_found(await get_book(session, args.asin, region=args.region), "book not in the local store"))

    return run_stored(read)


def run_books(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.exceptions import ErrorCode, NotFoundException
        from libex_core.storage.read.books import search_books

        if not has_book_filter(args) and args.sort is None:
            raise NotFoundException(
                "give at least one filter or --sort", code=ErrorCode.INVALID_REQUEST
            )
        return _books(await search_books(session, **_shape(args), **_page(args)))

    return run_stored(read)


def run_chapters(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.models import ChapterResponse
        from libex_core.storage.read.books import get_track

        return ChapterResponse(**_found(await get_track(session, args.asin, region=args.region), "no chapter data in the local store for this book"))

    return run_stored(read)


def run_sku(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.books import get_books_by_sku

        return _books(await get_books_by_sku(session, args.sku))

    return run_stored(read)


def run_author(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.models import AuthorResponse
        from libex_core.storage.read.people import get_author

        return AuthorResponse(**_found(await get_author(session, args.asin, args.region), "author not in the local store"))

    return run_stored(read)


def run_author_books(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.people import get_author_books

        return _books(
            await get_author_books(
                session,
                args.asin,
                args.region,
                book_region=args.book_region,
                **_shape(args, _AUTHOR_BOOKS_EXCLUDED),
            )
        )

    return run_stored(read)


def run_series(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.models import SeriesResponse
        from libex_core.storage.read.series import get_series

        return SeriesResponse(**_found(await get_series(session, args.asin, region=args.region), "series not in the local store"))

    return run_stored(read)


def run_series_books(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.series import get_series_books

        return _books(
            await get_series_books(session, args.asin, **_shape(args, _SERIES_BOOKS_EXCLUDED))
        )

    return run_stored(read)


def run_narrators(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.models import NarratorProfileResponse
        from libex_core.storage.read.people import search_narrators

        rows = _found(
            await search_narrators(
                session,
                args.name,
                gender=args.gender,
                language=args.language,
                audiobooks_produced=AUDIOBOOKS_PRODUCED.get(args.audiobooks_produced),
                source=args.source,
                cultural_heritage=args.cultural_heritage,
                sort=args.sort,
                order=args.order,
                **_page(args),
            ),
            "no matching narrators in the local store",
        )
        return [NarratorProfileResponse(**row) for row in rows]

    return run_stored(read)


def run_narrator_books(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.people import get_narrator_books

        return _books(
            await get_narrator_books(session, args.name, **_shape(args), **_page(args))
        )

    return run_stored(read)


def run_genres(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.books import distinct_genres

        return _found(await distinct_genres(session, search=args.search), "no matching genres in the local store")

    return run_stored(read)


def run_plans(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.books import distinct_plans

        return _found(await distinct_plans(session), "no plans in the local store")

    return run_stored(read)


def run_plan(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.books import get_books_by_plan

        return _books(
            await get_books_by_plan(
                session,
                args.name,
                **_shape(args, frozenset({"plan_name"})),
                **_page(args),
            )
        )

    return run_stored(read)


def run_vvab(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.books import get_vvab_books

        return _books(
            await get_vvab_books(
                session, **_shape(args, frozenset({"is_vvab"})), **_page(args)
            )
        )

    return run_stored(read)


def run_new_releases(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.books import get_new_releases

        return _books(
            await get_new_releases(session, days=int(args.days), **_shape(args), **_page(args))
        )

    return run_stored(read)


def run_coming_soon(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.books import get_coming_soon

        return _books(
            await get_coming_soon(session, days=int(args.days), **_shape(args), **_page(args))
        )

    return run_stored(read)


def run_stats(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_stored

    async def read(session: Any) -> Any:
        from libex_core.storage.read.stats import count_stored

        counts = await count_stored(session, args.region)
        # The hosted route's response model: every field present, the two it
        # may not have counted null.
        return {
            "books": counts["books"],
            "distinctBookAsins": counts["distinctBookAsins"],
            "authors": counts["authors"],
            "narrators": counts["narrators"],
            "series": counts["series"],
            "booksWithChapters": counts["booksWithChapters"],
            "region": args.region,
            "seriesRegionUnknown": counts.get("seriesRegionUnknown"),
        }

    return run_stored(read)
