"""
Database writer service.
Persists Audible API responses to relational tables.

Called after every successful Audible fetch to keep the DB in sync.
Writes are upserts — existing non-null values are never overwritten with null.
The DB is used as a fallback when Audible is unavailable.

Given a session, this module makes rows say what a response says. It owns the
statements and the merge rules. write_books, the batched path, owns no
transaction of its own — when it runs, how many run at once, and whose
transaction it shares all belong to persist_queue, which imports this module
and is never imported by it. upsert_book, upsert_track, upsert_author_profile
and upsert_series_profile are the exception: each is a single-entity entry
point that commits and swallows its own failure, so a caller must not wrap
one in a transaction of its own.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from asyncpg.exceptions import UniqueViolationError as AsyncpgUniqueViolation
from sqlalchemy.dialects.postgresql import insert, JSONB
from sqlalchemy import bindparam, select, func, update, case, cast, delete, tuple_

# Database
from app.db.models import (
    Book,
    Author,
    Cache,
    Genre,
    Narrator,
    Series,
    Track,
    CatalogGenre,
    author_book,
    author_genre,
    book_genre,
    book_narrator,
    book_series,
    series_author,
)

# Core
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger()
settings = get_settings()


# ============================================================
# HELPERS
# ============================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_release_date_for_db(iso_str: str | None) -> datetime | None:
    """Converts an ISO 8601 string back to a datetime for DB storage."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None


def _coalesce(new_value, existing_col):
    """Returns new_value if not null, otherwise keeps the existing column value."""
    return func.coalesce(new_value, existing_col)


def _longer_wins(new_value, existing_col):
    """
    Keeps whichever value carries more text, so a later, richer Audible
    response replaces a thinner stored one and never the reverse.

    Both lengths are floored to a sentinel rather than compared directly,
    because length(NULL) is NULL and a comparison against NULL is NULL, not
    false. A bare length(new) > length(existing) therefore falls to the ELSE
    branch whenever the stored value is NULL, and pins that NULL permanently:
    no incoming description, however long, could fill a column that was first
    written empty.

    An incoming value that is empty or entirely whitespace measures as absent,
    so it cannot displace a stored NULL. It carries no more information than
    NULL does, and writing it would make a column Audible has never answered
    indistinguishable from one it answered blank. Only the measurement is
    trimmed — the value written is the value received, verbatim.
    """
    absent = -1
    new_length = func.coalesce(func.length(func.nullif(func.btrim(new_value), "")), absent)
    existing_length = func.coalesce(func.length(existing_col), absent)
    return case(
        (new_length > existing_length, new_value),
        else_=existing_col,
    )


def _to_bool(value, default: bool = False) -> bool:
    """Converts string or bool to bool safely."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == 'true'
    return default


def _asserted_bool(value) -> bool | None:
    """
    Reads a boolean the way the shrinkage rule needs it read: True or False
    when Audible asserted one, None when it asserted nothing.

    For every other column "less data" means null against a value, and
    _coalesce settles it. A boolean has no null to fall back on — the column
    is NOT NULL — so the distinction that matters is asserted versus
    not-asserted, and it has to be carried in the bind rather than in the
    column. A response that simply omits isListenable binds None here, the
    insert takes its own default and the update keeps whatever is stored;
    a response that says false binds False and overwrites, because that is
    Audible answering rather than staying silent.

    Recognises exactly what _to_bool recognises, so the two never disagree
    about what a value means, only about what an unrecognised one costs:
    _to_bool substitutes its caller's default, this returns None and lets the
    statement decide. Anything that is neither a bool nor a string is not an
    answer in any form we can read, so it is treated as silence.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == 'true'
    return None


def _failure_fields(exc: BaseException) -> dict:
    """
    The only thing a failed write may say about itself: what kind it was,
    which SQLSTATE the server returned, and which schema object it was
    against — never the exception itself.

    Postgres puts the offending row into its own message text — a not-null
    violation reports "Failing row contains (...)" with every column in it —
    so str(exc), repr(exc) and exc_info all publish book data into the logs
    whatever hide_parameters is set to. schema/table/column/constraint name
    are schema metadata, not row content, and safe to carry alongside it.
    exc.code is deliberately absent: that is SQLAlchemy's documentation slug,
    not the pgcode, and reading it as one is how the wrong thing ends up in a
    dashboard.

    Lives here, not in persist_queue, so writer's own upsert functions can use
    it too without persist_queue importing back into writer — writer is
    stateless and never imports the module that imports it.
    """
    orig = getattr(exc, "orig", None)
    return {
        "error_type": type(exc).__name__,
        "sqlstate": getattr(orig, "sqlstate", None),
        "schema_name": getattr(orig, "schema_name", None),
        "table_name": getattr(orig, "table_name", None),
        "column_name": getattr(orig, "column_name", None),
        "constraint_name": getattr(orig, "constraint_name", None),
    }


# ============================================================
# GENRE WRITER
# ============================================================

async def upsert_genre(session: AsyncSession, genre: dict) -> str | None:
    """Upserts a single genre. Returns asin if successful."""
    asin = genre.get("asin")
    name = genre.get("name")
    genre_type = genre.get("type", "Tags")

    if not asin or not name:
        return None

    stmt = insert(Genre).values(
        asin=asin,
        name=name,
        type=genre_type,
        created_at=_now(),
        updated_at=_now(),
    ).on_conflict_do_update(
        index_elements=["asin"],
        set_={
            "name": _coalesce(name, Genre.name),
            "type": Genre.type,
            "updated_at": _now(),
        },
    )
    await session.execute(stmt)
    return asin


# ============================================================
# NARRATOR WRITER
# ============================================================

async def upsert_narrator(session: AsyncSession, narrator: dict) -> str | None:
    """Upserts a single narrator. Returns name if successful."""
    name = narrator.get("name", "").strip()
    if not name:
        return None

    stmt = insert(Narrator).values(
        name=name,
        created_at=_now(),
        updated_at=_now(),
    ).on_conflict_do_nothing()
    await session.execute(stmt)
    return name


# ============================================================
# SERIES WRITER
# ============================================================

def _build_series_upsert():
    """
    The one series upsert, built once at import and executed with bound rows.

    Every value it writes arrives as a named bind parameter rather than being
    compiled into the statement, so one series and fifty cost one compile
    between them. That is the whole reason the statement is shaped this way:
    the postgresql Insert construct sets inherit_cache = False, so a statement
    carrying literals is recompiled on every execution no matter how many times
    the identical object has been executed before.
    """
    stmt = insert(Series).values(
        asin=bindparam("asin"),
        title=bindparam("title"),
        description=bindparam("description"),
        region=bindparam("region"),
        fetched_description=bindparam("fetched_description"),
        created_at=bindparam("created_at"),
        updated_at=bindparam("updated_at"),
    )
    return stmt.on_conflict_do_update(
        index_elements=["asin"],
        set_={
            "title": _coalesce(bindparam("title"), Series.title),
            "description": _longer_wins(bindparam("description"), Series.description),
            # Region is never updated: a series row belongs to the marketplace
            # that first wrote it, and excluded.region here would let any other
            # region's response move it.
            "region": Series.region,
            "fetched_description": Series.fetched_description | stmt.excluded.fetched_description,
            "updated_at": stmt.excluded.updated_at,
        },
    )


_SERIES_UPSERT = _build_series_upsert()


def _series_params(series: dict, now: datetime) -> dict | None:
    """
    Binds one series for _SERIES_UPSERT, or None when it carries too little to
    write — the same asin-and-name guard upsert_series has always applied.
    """
    asin = series.get("asin")
    name = series.get("name") or series.get("title")
    if not asin or not name:
        return None

    description = series.get("description")
    return {
        "asin": asin,
        "title": name,
        "description": description,
        "region": series.get("region"),
        "fetched_description": bool(description),
        "created_at": now,
        "updated_at": now,
    }


async def upsert_series(session: AsyncSession, series: dict) -> str | None:
    """Upserts a series record. Returns asin if successful."""
    params = _series_params(series, _now())
    if params is None:
        return None
    await session.execute(_SERIES_UPSERT, [params])
    return params["asin"]


# ============================================================
# AUTHOR WRITER
# ============================================================

async def upsert_author(session: AsyncSession, author: dict) -> int | None:
    """
    Upserts an author record. Returns the author's DB id if successful.

    When asin is null: match on (name, region, asin IS NULL) to avoid duplicates.
    When asin is not null:
      1. Check if a fully-upgraded row (asin, region, name) already exists —
         return its id immediately if so. This short-circuits concurrent requests
         that would otherwise race to upgrade the same null-asin row.
      2. If not, look for a null-asin row to upgrade in place, since PostgreSQL
         does not treat NULL = NULL in unique indexes.
      3. Fall through to standard INSERT ... ON CONFLICT if neither exists.
    """
    a_asin = author.get("asin")
    a_name = author.get("name", "").strip()
    a_region = author.get("region")

    if not a_name or not a_region:
        return None

    if a_asin:
        # Step 1: check if the fully-upgraded row already exists.
        # This is the common case after the first request upgrades the row.
        existing_result = await session.execute(
            select(Author.id).where(
                Author.asin == a_asin,
                Author.region == a_region,
                Author.name == a_name,
            )
        )
        existing_id = existing_result.scalar_one_or_none()
        if existing_id:
            return existing_id

        # Step 2: look for a null-asin row to upgrade. The unique constraint
        # doesn't cover null asins (Postgres treats NULLs as distinct), so a
        # concurrent-write race can leave more than one null-asin row for the
        # same (name, region) — order by id and take the oldest so every writer
        # converges on the same row instead of raising MultipleResultsFound.
        null_result = await session.execute(
            select(Author.id).where(
                Author.name == a_name,
                Author.region == a_region,
                Author.asin.is_(None),
            )
            .order_by(Author.id)
            .limit(1)
        )
        null_id = null_result.scalar_one_or_none()

        if null_id:
            # The UPDATE runs inside a SAVEPOINT so that losing the race undoes
            # only this statement. session.rollback() discards the whole
            # transaction, which is harmless when this function is the only
            # writer in it and silently destructive when it is not: a batched
            # persist writes many books per transaction, and a bare rollback
            # here would throw away every book already written alongside this
            # one, without raising anything for the caller to notice.
            nested = await session.begin_nested()
            try:
                await session.execute(
                    update(Author)
                    .where(Author.id == null_id)
                    .values(
                        asin=a_asin,
                        image=_coalesce(author.get("image"), Author.image),
                        description=_longer_wins(author.get("description"), Author.description),
                        updated_at=_now(),
                    )
                )
                await nested.commit()
            except (IntegrityError, AsyncpgUniqueViolation):
                # A concurrent request upgraded between our SELECT and UPDATE.
                # The data is correct — undo the failed statement and return
                # the existing id.
                await nested.rollback()
            return null_id

        # No null-asin row — standard upsert on the unique constraint.
        stmt = insert(Author).values(
            asin=a_asin,
            name=a_name,
            region=a_region,
            description=author.get("description"),
            image=author.get("image"),
            fetched_description=bool(author.get("description")),
            created_at=_now(),
            updated_at=_now(),
        ).on_conflict_do_update(
            constraint="authors_asin_region_name_unique",
            set_={
                "image": _coalesce(author.get("image"), Author.image),
                "description": _longer_wins(author.get("description"), Author.description),
                "updated_at": _now(),
            },
        ).returning(Author.id)

    else:
        # Same duplicate tolerance as the upgrade lookup above: take the
        # oldest null-asin row if the race ever left more than one.
        existing = await session.execute(
            select(Author.id).where(
                Author.name == a_name,
                Author.region == a_region,
                Author.asin.is_(None),
            )
            .order_by(Author.id)
            .limit(1)
        )
        existing_id = existing.scalar_one_or_none()
        if existing_id:
            return existing_id

        # Insert a fresh null-asin row. A partial unique index on
        # (name, region) WHERE asin IS NULL means a concurrent insert of the
        # same author now conflicts instead of quietly duplicating — catch it,
        # undo just the INSERT, and return the row the winner inserted. Same
        # SAVEPOINT reasoning as the upgrade path above: the loser of the race
        # must not take the caller's other work down with it.
        nested = await session.begin_nested()
        try:
            result = await session.execute(
                insert(Author).values(
                    asin=None,
                    name=a_name,
                    region=a_region,
                    description=author.get("description"),
                    image=author.get("image"),
                    fetched_description=False,
                    created_at=_now(),
                    updated_at=_now(),
                ).returning(Author.id)
            )
            row = result.fetchone()
            await nested.commit()
            return row[0] if row else None
        except (IntegrityError, AsyncpgUniqueViolation):
            await nested.rollback()
            winner = await session.execute(
                select(Author.id).where(
                    Author.name == a_name,
                    Author.region == a_region,
                    Author.asin.is_(None),
                )
                .order_by(Author.id)
                .limit(1)
            )
            return winner.scalar_one_or_none()

    result = await session.execute(stmt)
    row = result.fetchone()
    return row[0] if row else None


# ============================================================
# BOOK WRITER
# ============================================================

def _build_book_upsert():
    """
    The one book upsert, built once at import and executed with bound rows.

    Nothing about a book appears in the statement — every value it writes is a
    named bind parameter — so a chunk of fifty books is one compile and fifty
    parameter sets rather than fifty compiles. That is the entire performance
    argument for this shape, and it does not come from statement reuse:
    postgresql's Insert sets inherit_cache = False and OnConflictDoUpdate
    defines no traversal internals, so this object is recompiled on every
    execute() no matter how long it has been held. What is saved is a compile
    per book, not a compile per statement.

    It deliberately carries NO returning(). Adding one turns SQLAlchemy's
    use_insertmanyvalues on, which rewrites the executemany into batched
    multi-row VALUES groups — and one INSERT ... ON CONFLICT DO UPDATE may not
    affect the same row twice, so a chunk holding the same ASIN twice raises
    cardinality_violation (21000) and drops all fifty books to the per-book
    replay path this exists to avoid. Duplicate ASINs inside a chunk are
    ordinary, not hypothetical: an author walk pages a catalog whose sort
    windows shift under it. Without returning(), the statement stays one row
    per execution, a repeated ASIN simply upserts twice, and the second
    execution merges against the first exactly as a later request would.

    Measured on the pinned SQLAlchemy rather than assumed, and the measurement
    is worth keeping because the rewrite turns out to be conditional in a way
    that flatters this statement by accident. A set_ clause referencing a
    shared bind parameter cannot be renumbered across VALUES groups, so the
    compiler abandons the rewrite and falls back to one statement per row —
    the same statement with returning() and a set_ built only from excluded
    columns reproduced the 21000 on the first try, while this one merely lost
    its executemany and ran fifty times. The coalesce binds below are what put
    it on the safe side today, which makes them a coincidence rather than a
    guard: keeping returning() off is the part that holds no matter what the
    set_ is later rewritten to say.

    Three asymmetries in the merge are load-bearing and must survive any edit
    that regenerates this from the column list:

    - created_at is written on insert and absent from the update. Deriving the
      update from the insert's columns adds excluded.created_at and resets
      every book's real creation time on its next write, silently.
    - region updates to itself. A book ASIN belongs to one marketplace, and
      excluded.region would let a response fetched for another region move it.
    - title falls back to '' on insert (the column is NOT NULL) but to the
      stored title on update, so a response that omits it cannot blank one
      that is already stored. The bind carries None, never '', because
      coalesce('', books.title) is '' and would do exactly that.
    """
    stmt = insert(Book).values(
        asin=bindparam("asin"),
        title=_coalesce(bindparam("title"), ""),
        subtitle=bindparam("subtitle"),
        region=bindparam("region"),
        description=bindparam("description"),
        summary=bindparam("summary"),
        publisher=bindparam("publisher"),
        copyright=bindparam("copyright"),
        isbn=bindparam("isbn"),
        language=bindparam("language"),
        rating=bindparam("rating"),
        release_date=bindparam("release_date"),
        length_minutes=bindparam("length_minutes"),
        explicit=bindparam("explicit"),
        whisper_sync=bindparam("whisper_sync"),
        has_pdf=bindparam("has_pdf"),
        image=bindparam("image"),
        book_format=bindparam("book_format"),
        content_type=bindparam("content_type"),
        content_delivery_type=bindparam("content_delivery_type"),
        episode_number=bindparam("episode_number"),
        episode_type=bindparam("episode_type"),
        sku=bindparam("sku"),
        sku_group=bindparam("sku_group"),
        is_listenable=_coalesce(bindparam("is_listenable"), True),
        is_buyable=_coalesce(bindparam("is_buyable"), True),
        is_vvab=_coalesce(bindparam("is_vvab"), False),
        # none_as_null is not optional here. JSONB's default is to serialize a
        # Python None to the JSON value null, which is a value and not SQL
        # NULL — so coalesce below would take it in preference to the stored
        # array and empty the column on every response that carries no plans.
        # Pre-existing bug, not introduced by this rewrite: cast(None, JSONB)
        # binds JSON null the same way. This fixes the None case.
        plans=cast(bindparam("plans", type_=JSONB(none_as_null=True)), JSONB),
        created_at=bindparam("created_at"),
        updated_at=bindparam("updated_at"),
    )
    return stmt.on_conflict_do_update(
        index_elements=["asin"],
        set_={
            "title": _coalesce(bindparam("title"), Book.title),
            "subtitle": _coalesce(stmt.excluded.subtitle, Book.subtitle),
            "region": Book.region,
            "description": _longer_wins(bindparam("description"), Book.description),
            "summary": _longer_wins(bindparam("summary"), Book.summary),
            "publisher": _coalesce(stmt.excluded.publisher, Book.publisher),
            "copyright": _coalesce(stmt.excluded.copyright, Book.copyright),
            "isbn": _coalesce(stmt.excluded.isbn, Book.isbn),
            "language": _coalesce(stmt.excluded.language, Book.language),
            "rating": _coalesce(stmt.excluded.rating, Book.rating),
            "release_date": _coalesce(stmt.excluded.release_date, Book.release_date),
            "length_minutes": _coalesce(stmt.excluded.length_minutes, Book.length_minutes),
            "explicit": stmt.excluded.explicit,
            "whisper_sync": stmt.excluded.whisper_sync,
            "has_pdf": stmt.excluded.has_pdf,
            "image": _coalesce(stmt.excluded.image, Book.image),
            "book_format": _coalesce(stmt.excluded.book_format, Book.book_format),
            "content_type": _coalesce(stmt.excluded.content_type, Book.content_type),
            "content_delivery_type": _coalesce(
                stmt.excluded.content_delivery_type, Book.content_delivery_type
            ),
            "episode_number": _coalesce(stmt.excluded.episode_number, Book.episode_number),
            "episode_type": _coalesce(stmt.excluded.episode_type, Book.episode_type),
            "sku": _coalesce(stmt.excluded.sku, Book.sku),
            "sku_group": _coalesce(stmt.excluded.sku_group, Book.sku_group),
            # The three NOT NULL booleans merge on asserted-versus-silent, not
            # on true-versus-false: excluded here would carry the insert
            # default and overwrite a stored answer with one Audible never
            # gave. See _asserted_bool for why the bind is tri-state.
            "is_listenable": _coalesce(bindparam("is_listenable"), Book.is_listenable),
            "is_buyable": _coalesce(bindparam("is_buyable"), Book.is_buyable),
            "is_vvab": _coalesce(bindparam("is_vvab"), Book.is_vvab),
            "plans": _coalesce(stmt.excluded.plans, Book.plans),
            "updated_at": stmt.excluded.updated_at,
        },
    )


_BOOK_UPSERT = _build_book_upsert()


def _build_pivot_insert(table, *columns):
    """
    An additive pivot insert: one row per execution, conflicts ignored.

    Every pivot Libex writes is a link that may already exist and must never
    be removed, so DO NOTHING is the whole merge rule and there is nothing to
    parameterise beyond the row itself.
    """
    return insert(table).values(
        **{column: bindparam(column) for column in columns}
    ).on_conflict_do_nothing()


_GENRE_INSERT = _build_pivot_insert(Genre, "asin", "name", "type", "created_at", "updated_at")
_NARRATOR_INSERT = _build_pivot_insert(Narrator, "name", "created_at", "updated_at")
_BOOK_GENRE_INSERT = _build_pivot_insert(book_genre, "book_asin", "genre_asin")
_BOOK_NARRATOR_INSERT = _build_pivot_insert(book_narrator, "book_asin", "narrator_name")
_AUTHOR_BOOK_INSERT = _build_pivot_insert(author_book, "author_id", "book_asin")
_SERIES_AUTHOR_INSERT = _build_pivot_insert(series_author, "series_asin", "author_id")


def _build_book_series_upsert():
    """
    The book-to-series link, which unlike the other pivots carries a value:
    position moves as Audible restates it, so this one updates rather than
    ignoring the conflict — but only from a non-null incoming position, so a
    response that omits it leaves the stored one standing.
    """
    stmt = insert(book_series).values(
        book_asin=bindparam("book_asin"),
        series_asin=bindparam("series_asin"),
        position=bindparam("position"),
    )
    return stmt.on_conflict_do_update(
        index_elements=["book_asin", "series_asin"],
        set_={"position": _coalesce(stmt.excluded.position, book_series.c.position)},
    )


_BOOK_SERIES_UPSERT = _build_book_series_upsert()


def _book_params(data: dict, now: datetime) -> dict:
    """
    Binds one book for _BOOK_UPSERT.

    Every coercion the values clause used to perform in SQL happens here
    instead, because a bind that reaches a NOT NULL column as None is not a
    quiet fallback but an aborted statement — and with a whole chunk sharing
    one execution, one such row costs all fifty their transaction. The three
    tri-state booleans are the deliberate exception: their None is answered by
    a coalesce on both sides of the statement and never reaches the column.
    """
    return {
        "asin": data["asin"],
        # Never '': the update merges this bind with coalesce, and
        # coalesce('', books.title) would blank a stored title.
        "title": data.get("title"),
        "subtitle": data.get("subtitle"),
        "region": data.get("region"),
        "description": data.get("description"),
        "summary": data.get("summary"),
        "publisher": data.get("publisher"),
        "copyright": data.get("copyright"),
        "isbn": data.get("isbn"),
        "language": data.get("language"),
        "rating": data.get("rating"),
        "release_date": _parse_release_date_for_db(data.get("releaseDate")),
        "length_minutes": data.get("lengthMinutes"),
        "explicit": _to_bool(data.get("explicit")),
        "whisper_sync": _to_bool(data.get("whisperSync")),
        "has_pdf": _to_bool(data.get("hasPdf")),
        "image": data.get("imageUrl"),
        "book_format": data.get("bookFormat"),
        "content_type": data.get("contentType"),
        "content_delivery_type": data.get("contentDeliveryType"),
        "episode_number": data.get("episodeNumber"),
        "episode_type": data.get("episodeType"),
        "sku": data.get("sku"),
        "sku_group": data.get("skuGroup"),
        "is_listenable": _asserted_bool(data.get("isListenable")),
        "is_buyable": _asserted_bool(data.get("isBuyable")),
        "is_vvab": _asserted_bool(data.get("isVvab")),
        "plans": data.get("plans"),
        "created_at": now,
        "updated_at": now,
    }


async def _resolve_author_ids(
    session: AsyncSession, books: list[dict]
) -> dict[str, list[int]]:
    """
    Resolves every book's authors to DB ids, calling upsert_author once per
    distinct author rather than once per book that names them.

    upsert_author is the one write here that cannot be a bound row: it reads
    before it writes, upgrades null-asin rows in place, and opens SAVEPOINTs
    around the races that entails. So it stays a statement-per-author — but a
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
    ids_by_book: dict[str, list[int]] = {}

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
                author_id = await upsert_author(session, author)
                memo[key] = author_id
            if author_id and author_id not in ids:
                ids.append(author_id)
        # Accumulated, not assigned. A chunk can legitimately carry the same
        # ASIN twice — the catalog's sort windows shift between page fetches,
        # so the same product arrives in two windows — and the two copies can
        # name different contributors. Assigning here let the second copy
        # replace the first copy's resolved ids, so an author present only on
        # the first copy lost their author_book link entirely: the author row
        # was written, the book row was written, and the relationship between
        # them silently was not. Every sibling pivot below already unions on
        # an (asin, x) key; this was the one keyed by asin alone.
        merged = ids_by_book.setdefault(data["asin"], [])
        merged.extend(i for i in ids if i not in merged)

    return ids_by_book


async def write_books(session: AsyncSession, books: list[dict]) -> None:
    """
    Issues every statement for a list of books — their rows plus their genre,
    narrator, series and author relationships — and nothing else.

    One statement per KIND of row rather than one per book: the whole list's
    book rows go through a single executemany, then the whole list's genres,
    then its book-genre links, and so on. Fifty books cost a fixed handful of
    statements plus one per distinct author, against roughly ten per book
    before. The saving is compile time on the event loop, not round trips —
    see _build_book_upsert for why holding the statement object is not enough
    on its own.

    Owns no transaction: it neither commits nor rolls back, so the caller
    decides whether one book or fifty share a transaction. Every statement is
    an idempotent upsert, which is what lets a caller whose transaction was
    lost replay the same books without double-counting anything.

    Existing non-null values are never overwritten with null. Pivot
    relationships (genres, narrators, authors) are additive — never shrink.
    Series position is kept current via upsert.

    Rows are ordered so a table is written before anything referencing it, and
    duplicates are collapsed in Python before binding: the ON CONFLICT DO
    NOTHING sets keep the first of a repeat, matching what a per-row loop
    would have left, and the DO UPDATE sets keep the last, matching the same.
    """
    if not books:
        return

    now = _now()

    await session.execute(_BOOK_UPSERT, [_book_params(book, now) for book in books])

    genres: dict[str, dict] = {}
    book_genres: dict[tuple, dict] = {}
    narrators: dict[str, dict] = {}
    book_narrators: dict[tuple, dict] = {}
    series: list[dict] = []
    book_series_links: dict[tuple, dict] = {}

    for data in books:
        asin = data["asin"]

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
            book_genres.setdefault((asin, g_asin), {"book_asin": asin, "genre_asin": g_asin})

        for narrator in data.get("narrators", []):
            name = narrator.get("name", "").strip()
            if not name:
                continue
            narrators.setdefault(name, {"name": name, "created_at": now, "updated_at": now})
            book_narrators.setdefault((asin, name), {"book_asin": asin, "narrator_name": name})

        for entry in data.get("series", []):
            params = _series_params(entry, now)
            if params is None:
                continue
            series.append(params)
            book_series_links[(asin, params["asin"])] = {
                "book_asin": asin,
                "series_asin": params["asin"],
                "position": entry.get("position"),
            }

    if genres:
        await session.execute(_GENRE_INSERT, list(genres.values()))
        await session.execute(_BOOK_GENRE_INSERT, list(book_genres.values()))

    if narrators:
        await session.execute(_NARRATOR_INSERT, list(narrators.values()))
        await session.execute(_BOOK_NARRATOR_INSERT, list(book_narrators.values()))

    if series:
        await session.execute(_SERIES_UPSERT, series)
        await session.execute(_BOOK_SERIES_UPSERT, list(book_series_links.values()))

    ids_by_book = await _resolve_author_ids(session, books)

    author_books: dict[tuple, dict] = {}
    series_authors: dict[tuple, dict] = {}
    for data in books:
        asin = data["asin"]
        author_ids = ids_by_book.get(asin, [])
        for author_id in author_ids:
            author_books.setdefault((author_id, asin), {"author_id": author_id, "book_asin": asin})
        for entry in data.get("series", []):
            s_asin = entry.get("asin")
            if not s_asin:
                continue
            for author_id in author_ids:
                series_authors.setdefault(
                    (s_asin, author_id), {"series_asin": s_asin, "author_id": author_id}
                )

    if author_books:
        await session.execute(_AUTHOR_BOOK_INSERT, list(author_books.values()))
    if series_authors:
        await session.execute(_SERIES_AUTHOR_INSERT, list(series_authors.values()))


async def _write_book(session: AsyncSession, data: dict) -> None:
    """
    Issues every statement for one book, as a batch of one.

    Kept as its own name because the single-book callers read better for it,
    and routed through write_books so the one-book and fifty-book paths cannot
    drift apart in what they write or how they merge it.
    """
    await write_books(session, [data])


async def upsert_book(session: AsyncSession, data: dict) -> None:
    """
    Upserts a book and all its relationships to the relational DB, in a
    transaction of its own.

    The single-book entry point, and the per-book replay path for a chunk whose
    shared transaction was lost: it wraps _write_book in a commit of its own and
    swallows the failure, so one bad book costs only itself. The batched persist
    calls write_books directly on its normal path.

    Existing non-null values are never overwritten with null.
    Pivot relationships (genres, narrators, authors) are additive — never shrink.
    Series position is kept current via upsert.
    """
    asin = data.get("asin")
    if not asin:
        return

    try:
        await _write_book(session, data)
        await session.commit()
        logger.info(f"DB write: book {asin}")

    except Exception as e:
        logger.warning(
            "DB write failed for book",
            extra={"asin": asin, **_failure_fields(e)},
        )
        await session.rollback()


# ============================================================
# TRACK WRITER
# ============================================================

async def upsert_track(session: AsyncSession, asin: str, chapters_data: dict) -> None:
    """Upserts chapter data for a book."""
    try:
        stmt = insert(Track).values(
            asin=asin,
            chapters=chapters_data,
            created_at=_now(),
            updated_at=_now(),
        ).on_conflict_do_update(
            index_elements=["asin"],
            set_={"chapters": chapters_data, "updated_at": _now()},
        )
        await session.execute(stmt)
        await session.commit()
        logger.info(f"DB write: track {asin}")

    except Exception as e:
        logger.warning(
            "DB write failed for track",
            extra={"asin": asin, **_failure_fields(e)},
        )
        await session.rollback()


# ============================================================
# AUTHOR PROFILE WRITER
# ============================================================

async def upsert_author_profile(session: AsyncSession, data: dict) -> None:
    """
    Upserts a full author profile fetched from the contributors endpoint.
    Updates description and image which aren't available from book data alone.
    Also writes author genres to author_genre pivot.
    Author genres are additive — never delete.
    """
    asin = data.get("asin")
    name = data.get("name", "").strip()
    region = data.get("region")

    if not name or not region:
        return

    try:
        if asin:
            stmt = insert(Author).values(
                asin=asin,
                name=name,
                region=region,
                description=data.get("description"),
                image=data.get("image"),
                fetched_description=True,
                created_at=_now(),
                updated_at=_now(),
            ).on_conflict_do_update(
                constraint="authors_asin_region_name_unique",
                set_={
                    "description": _longer_wins(data.get("description"), Author.description),
                    "image": _coalesce(data.get("image"), Author.image),
                    "fetched_description": True,
                    "updated_at": _now(),
                },
            ).returning(Author.id)
            result = await session.execute(stmt)
            row = result.fetchone()
            author_id = row[0] if row else None

            # Author genres — additive, never delete
            if author_id and data.get("genres"):
                for genre in data["genres"]:
                    g_asin = await upsert_genre(session, genre)
                    if g_asin:
                        await session.execute(
                            insert(author_genre).values(author_id=author_id, genre_asin=g_asin)
                            .on_conflict_do_nothing()
                        )

        await session.commit()
        logger.info(f"DB write: author {asin} ({name})")

    except Exception as e:
        logger.warning(
            "DB write failed for author",
            extra={"asin": asin, **_failure_fields(e)},
        )
        await session.rollback()


# ============================================================
# SERIES PROFILE WRITER
# ============================================================

async def upsert_series_profile(session: AsyncSession, data: dict) -> None:
    """
    Upserts a full series profile fetched from the series endpoint.
    Updates description which isn't always available from book relationship data.

    Writes through the same statement the book path writes series with, so the
    two cannot drift apart on how a description or a region merges. All this
    adds is a transaction of its own and a stricter guard: a profile fetch that
    answered without a name has failed, where a book's series relationship may
    legitimately carry the title under either key.
    """
    asin = data.get("asin")
    name = data.get("name")
    if not asin or not name:
        return

    try:
        await session.execute(_SERIES_UPSERT, [_series_params(data, _now())])
        await session.commit()
        logger.info(f"DB write: series {asin} ({name})")

    except Exception as e:
        logger.warning(
            "DB write failed for series",
            extra={"asin": asin, **_failure_fields(e)},
        )
        await session.rollback()


# ============================================================
# CACHE WRITER
# ============================================================

async def _cache_set_many(
    session: AsyncSession,
    entries: list[tuple[str, dict]],
    ttl_seconds: int | None = None,
) -> None:
    """
    Writes many cache entries in one statement and does not commit — the
    caller's transaction owns that.

    Same row shape, same TTL rule, and the same last-write-wins upsert as
    cache.set. What it does not do is spend a commit per key, which is the
    only reason it exists: the batched book persist would otherwise pay one
    transaction per cached book on top of one per written book. It takes a
    single `now` for the whole batch rather than letting it drift key by key,
    matching cache.get_many's single point in time across a batch.

    ttl_seconds carries cache.set's signature rather than fixing the default,
    because TTL in Libex is a property of the value and not of the key: the
    date-derived scans expire at UTC midnight, the stats key has its own
    constant, and an incomplete author catalogue is deliberately stored for
    less time than a complete one. A batch primitive that could only write the
    default would silently promote any of those to the full TTL the first time
    someone batched them, which for the degraded-catalogue case means serving
    known-incomplete data as though it were whole.

    Duplicate keys are collapsed last-wins before the statement is built:
    Postgres rejects an ON CONFLICT DO UPDATE that would touch the same row
    twice within one INSERT, and last-wins is exactly what a per-key loop over
    those same duplicates would have left stored.

    Unchunked, where cache.get_many is chunked: the row shape binds four
    parameters per entry into a single INSERT, so 8192 entries reach asyncpg's
    32,767 cap. The one caller is the batched book persist, bounded by
    _PERSIST_CHUNK_SIZE at 50 entries and 200 binds. A caller passing a list it
    does not bound is what puts a chunk loop here. It is the only multi-row
    VALUES the persist path issues — every other statement in it binds one row
    per execution and so cannot reach the cap at any chunk size.
    """
    if not entries:
        return

    ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl
    now = _now()
    expires_at = now + timedelta(seconds=ttl)
    deduped = dict(entries)

    stmt = insert(Cache).values([
        {"key": key, "value": value, "created_at": now, "expires_at": expires_at}
        for key, value in deduped.items()
    ])
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "value": stmt.excluded.value,
                "created_at": stmt.excluded.created_at,
                "expires_at": stmt.excluded.expires_at,
            },
        )
    )
    logger.info("Cache set batch", extra={
        "entries": len(deduped),
        "ttl": ttl,
    })


# ============================================================
# CATALOG GENRE WRITER
# ============================================================

async def upsert_genres(
    session: AsyncSession, region: str, genres: list[dict[str, str]]
) -> None:
    """
    Stores the catalog genre list for a region, stamping last_checked=now on
    every row so the stored set's freshness can be tracked. Upserts by
    (region, genre_id, parent_id): new nodes are inserted, existing ones get
    their name and last_checked refreshed. Each node carries a parent_id ("" for
    a top-level parent, the parent's id for a leaf), so a leaf that appears under
    two parents is stored once per parent. No-ops on an empty list.
    """
    if not genres:
        return
    now = _now()
    for genre in genres:
        parent_id = genre.get("parent_id", "")
        stmt = insert(CatalogGenre).values(
            region=region,
            genre_id=genre["genre_id"],
            parent_id=parent_id,
            name=genre["name"],
            last_checked=now,
        ).on_conflict_do_update(
            index_elements=["region", "genre_id", "parent_id"],
            set_={"name": genre["name"], "last_checked": now},
        )
        await session.execute(stmt)


async def reconcile_genres(
    session: AsyncSession, region: str, genres: list[dict[str, str]]
) -> None:
    """
    Makes the stored taxonomy for a region mirror the given set. Upserts every
    node (insert new, refresh name and last_checked), then prunes — deletes any
    stored node for the region whose (genre_id, parent_id) is not in the given
    set.

    Unlike upsert_genres, which is additive and never deletes, this prunes — so
    it must only be called with a COMPLETE taxonomy, i.e. the single live
    /categories fetch that returns the whole tree at once. Pruning is what lets
    the tree self-heal when Audible restructures: when a category moves to a new
    parent, an additive upsert leaves the old (id, old_parent) row behind as a
    ghost (e.g. a category that's no longer top-level still showing at the root).
    Reconcile removes those stale placements so the stored tree matches Audible's
    current one. No-ops on an empty list.
    """
    if not genres:
        return
    now = _now()
    fresh_keys = [(g["genre_id"], g.get("parent_id", "")) for g in genres]
    for genre in genres:
        parent_id = genre.get("parent_id", "")
        stmt = insert(CatalogGenre).values(
            region=region,
            genre_id=genre["genre_id"],
            parent_id=parent_id,
            name=genre["name"],
            last_checked=now,
        ).on_conflict_do_update(
            index_elements=["region", "genre_id", "parent_id"],
            set_={"name": genre["name"], "last_checked": now},
        )
        await session.execute(stmt)
    # Prune stale placements — e.g. a category's old parent_id after Audible
    # moves it. Everything in the fresh (complete) fetch is kept; anything stored
    # for this region but absent from it is removed.
    await session.execute(
        delete(CatalogGenre).where(
            CatalogGenre.region == region,
            tuple_(CatalogGenre.genre_id, CatalogGenre.parent_id).notin_(fresh_keys),
        )
    )
