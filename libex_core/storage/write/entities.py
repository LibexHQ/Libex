"""
Writers for the single entities: genres, narrators, series, authors, an
author's profile and a book's chapters.

Given a session, each makes rows say what a response says, under the merge
rules in `libex_core.storage.merge`: a stored value is never replaced by less.
None of them commits, rolls back or swallows a failure -- a statement that
fails raises, and what that means for the transaction is the caller's to
decide. The one exception to "no transaction" is the SAVEPOINT the author
writers open around the races they can lose, which they release themselves.

Every function takes the dialect of its session from the session unless the
caller already knows it and passes `dialect`, which saves the lookup.
"""

# Third party
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from libex_core.storage import merge
from libex_core.storage.models import Author, Genre, Narrator, Track, author_genre
from libex_core.storage.write.params import series_params
from libex_core.storage.write.statements import statements_for
from libex_core.storage.write.support import (
    conflict_on_constraint,
    dialect_of,
    insert_for,
    utc_now,
)

_AUTHOR_UNIQUE = "authors_asin_region_name_unique"
_AUTHOR_UNIQUE_COLUMNS = ["asin", "region", "name"]


# ============================================================
# GENRE WRITER
# ============================================================

async def upsert_genre(
    session: AsyncSession, genre: dict, *, dialect: str | None = None
) -> str | None:
    """Upserts a single genre. Returns asin if successful."""
    asin = genre.get("asin")
    name = genre.get("name")
    genre_type = genre.get("type", "Tags")

    if not asin or not name:
        return None

    insert = insert_for(dialect or dialect_of(session))
    stmt = insert(Genre).values(
        asin=asin,
        name=name,
        type=genre_type,
        created_at=utc_now(),
        updated_at=utc_now(),
    ).on_conflict_do_update(
        index_elements=["asin"],
        set_={
            "name": merge.coalesce(name, Genre.name),
            "type": Genre.type,
            "updated_at": utc_now(),
        },
    )
    await session.execute(stmt)
    return asin


# ============================================================
# NARRATOR WRITER
# ============================================================

async def upsert_narrator(
    session: AsyncSession, narrator: dict, *, dialect: str | None = None
) -> str | None:
    """Upserts a single narrator. Returns name if successful."""
    name = narrator.get("name", "").strip()
    if not name:
        return None

    insert = insert_for(dialect or dialect_of(session))
    stmt = insert(Narrator).values(
        name=name,
        created_at=utc_now(),
        updated_at=utc_now(),
    ).on_conflict_do_nothing()
    await session.execute(stmt)
    return name


# ============================================================
# SERIES WRITER
# ============================================================

async def upsert_series(
    session: AsyncSession, series: dict, *, dialect: str | None = None
) -> str | None:
    """Upserts a series record. Returns asin if successful."""
    params = series_params(series, utc_now())
    if params is None:
        return None
    statements = statements_for(dialect or dialect_of(session))
    await session.execute(statements.series_upsert, [params])
    return params["asin"]


async def write_series_profile(
    session: AsyncSession, data: dict, *, dialect: str | None = None
) -> str | None:
    """
    Writes a full series profile fetched from the series endpoint, returning
    the series asin, or None when the profile carries no asin or no name.

    Writes through the same statement the book path writes series with, so the
    two cannot drift apart on how a description or a region merges. All this
    adds is a stricter guard: a profile fetch that answered without a name has
    failed, where a book's series relationship may legitimately carry the title
    under either key.
    """
    asin = data.get("asin")
    name = data.get("name")
    if not asin or not name:
        return None

    statements = statements_for(dialect or dialect_of(session))
    await session.execute(statements.series_upsert, [series_params(data, utc_now())])
    return asin


# ============================================================
# AUTHOR WRITER
# ============================================================

async def upsert_author(
    session: AsyncSession,
    author: dict,
    *,
    dialect: str | None = None,
    conflict_errors: tuple[type[BaseException], ...] = (),
) -> int | None:
    """
    Upserts an author record. Returns the author's DB id if successful.

    When asin is null: match on (name, region, asin IS NULL) to avoid duplicates.
    When asin is not null:
      1. Check if a fully-upgraded row (asin, region, name) already exists --
         return its id immediately if so. This short-circuits concurrent requests
         that would otherwise race to upgrade the same null-asin row.
      2. If not, look for a null-asin row to upgrade in place, since neither
         Postgres nor SQLite treats NULL = NULL in unique indexes.
      3. Fall through to standard INSERT ... ON CONFLICT if neither exists.

    A lost race is recognised by sqlalchemy's IntegrityError. A caller whose
    driver can raise something else for the same collision names it in
    conflict_errors.
    """
    a_asin = author.get("asin")
    a_name = author.get("name", "").strip()
    a_region = author.get("region")

    if not a_name or not a_region:
        return None

    dialect = dialect or dialect_of(session)
    insert = insert_for(dialect)
    lost_race = (IntegrityError, *conflict_errors)

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
        # doesn't cover null asins (the database treats NULLs as distinct), so
        # a concurrent-write race can leave more than one null-asin row for the
        # same (name, region) -- order by id and take the oldest so every writer
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
                        # Same answered-versus-blank merge the book row's
                        # image gets, and for the same reason: a portrait is
                        # replaced by another URL, never withdrawn to
                        # nothing, so a blank is a thin response rather than
                        # an assertion. No blank can reach these two author
                        # writers today -- the book author parser
                        # (libex_core.audible.books) builds every author
                        # this path sees with image None -- but that is a
                        # constant in another module, invisible from here
                        # and free to change, and write_author_profile
                        # below is already reachable by one.
                        image=merge.answered(author.get("image"), Author.image),
                        description=merge.longer_wins(author.get("description"), Author.description),
                        updated_at=utc_now(),
                    )
                )
                await nested.commit()
            except lost_race:
                # A concurrent request already upgraded a row (or inserted a
                # new one) to this exact (asin, region, name) between our
                # SELECT and this UPDATE, colliding with
                # authors_asin_region_name_unique. Our UPDATE lost the race
                # and was rolled back -- null_id still has asin IS NULL, so
                # returning it would link the caller's book to a permanent
                # asin-less duplicate instead of the row the winner produced.
                # Re-query for the winner's row and return that id instead.
                await nested.rollback()
                winner = await session.execute(
                    select(Author.id).where(
                        Author.asin == a_asin,
                        Author.region == a_region,
                        Author.name == a_name,
                    )
                )
                return winner.scalar_one_or_none()
            return null_id

        # No null-asin row -- standard upsert on the unique constraint.
        stmt = insert(Author).values(
            asin=a_asin,
            name=a_name,
            region=a_region,
            description=author.get("description"),
            image=author.get("image"),
            fetched_description=bool(author.get("description")),
            created_at=utc_now(),
            updated_at=utc_now(),
        ).on_conflict_do_update(
            **conflict_on_constraint(dialect, _AUTHOR_UNIQUE, _AUTHOR_UNIQUE_COLUMNS),
            set_={
                "image": merge.answered(author.get("image"), Author.image),
                "description": merge.longer_wins(author.get("description"), Author.description),
                "updated_at": utc_now(),
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
        # same author now conflicts instead of quietly duplicating -- catch it,
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
                    created_at=utc_now(),
                    updated_at=utc_now(),
                ).returning(Author.id)
            )
            row = result.fetchone()
            await nested.commit()
            return row[0] if row else None
        except lost_race:
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


async def write_author_profile(
    session: AsyncSession, data: dict, *, dialect: str | None = None
) -> int | None:
    """
    Writes a full author profile fetched from the contributors endpoint and
    returns the author's id, or None when nothing was written.

    Updates description and image which aren't available from book data alone.
    Also writes author genres to author_genre pivot. Author genres are
    additive -- never delete. A profile without an asin writes nothing.
    """
    asin = data.get("asin")
    name = data.get("name", "").strip()
    region = data.get("region")

    if not name or not region or not asin:
        return None

    dialect = dialect or dialect_of(session)
    insert = insert_for(dialect)

    stmt = insert(Author).values(
        asin=asin,
        name=name,
        region=region,
        description=data.get("description"),
        image=data.get("image"),
        fetched_description=True,
        created_at=utc_now(),
        updated_at=utc_now(),
    ).on_conflict_do_update(
        **conflict_on_constraint(dialect, _AUTHOR_UNIQUE, _AUTHOR_UNIQUE_COLUMNS),
        set_={
            "description": merge.longer_wins(data.get("description"), Author.description),
            # The one author path a blank can actually arrive on. The
            # contributors normalizer passes the response's profile_image_url
            # straight through, unfiltered and unstripped, so a contributor
            # whose image field comes back empty reaches this merge as '' --
            # and coalesce would take it and blank a stored portrait on an
            # ordinary profile refresh.
            "image": merge.answered(data.get("image"), Author.image),
            "fetched_description": True,
            "updated_at": utc_now(),
        },
    ).returning(Author.id)
    result = await session.execute(stmt)
    row = result.fetchone()
    author_id = row[0] if row else None

    # Author genres -- additive, never delete
    if author_id and data.get("genres"):
        for genre in data["genres"]:
            g_asin = await upsert_genre(session, genre, dialect=dialect)
            if g_asin:
                await session.execute(
                    insert(author_genre).values(author_id=author_id, genre_asin=g_asin)
                    .on_conflict_do_nothing()
                )

    return author_id


# ============================================================
# TRACK WRITER
# ============================================================

async def write_track(
    session: AsyncSession,
    asin: str,
    chapters_data: dict,
    *,
    dialect: str | None = None,
) -> int:
    """
    Writes chapter data for a book, keeping the richer of the two payloads, and
    returns how many chapters the row holds afterwards.

    The merge is decided in the SET clause rather than by reading the row
    first: several fetch paths can be refreshing the same ASIN at once, and a
    read-compare-write would let two of them agree the stored row was empty
    before either had written. chaptered_wins settles it inside the one
    statement, against the row as the database has it locked.

    updated_at is bumped either way. It records that the row was reconsidered,
    which is true whether or not the payload changed, and nothing reads it for
    staleness.

    Unlike the batched book upsert, this statement may carry returning():
    that statement's hazard is the insertmanyvalues rewrite, which only
    applies to an executemany, and this is a single row with literal values.
    The count comes back so a caller can tell a suppressed overwrite -- fewer
    chapters offered than are held -- from an ordinary one.
    """
    insert = insert_for(dialect or dialect_of(session))
    stmt = insert(Track).values(
        asin=asin,
        chapters=chapters_data,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["asin"],
        set_={
            "chapters": merge.chaptered_wins(stmt.excluded.chapters, Track.chapters),
            "updated_at": utc_now(),
        },
    ).returning(merge.chapter_count(Track.chapters))

    result = await session.execute(stmt)
    return result.scalar() or 0
