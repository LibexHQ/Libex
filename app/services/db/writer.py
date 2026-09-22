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


def _parse_publication_datetime(raw, asin: str | None) -> datetime | None:
    """
    Converts Audible's publication_datetime to a datetime for DB storage.

    Not _parse_release_date_for_db, and the difference is the last character.
    That function reverses _parse_release_date, which reads a bare "%Y-%m-%d"
    and re-emits isoformat() with a numeric offset; publication_datetime
    arrives from Audible as a full instant ending in a literal Z, which is how
    the unreleased sentinel is written too ("2200-01-01T00:00:00Z"). Python's
    fromisoformat only learned to accept that Z in 3.11, and this module's
    except returns None, so reusing the release-date parser would leave the
    correctness of a whole column resting on the interpreter version: the
    Dockerfile pins python:3.12-slim and it would work, right up until the pin
    moved back, at which point every book would write NULL here and nothing
    would say so. The Z is rewritten to an explicit offset before parsing so
    that no version of Python is being relied on to recognise it.

    A value that still will not parse is logged rather than quietly nulled,
    for the same reason: this column is written on every book in the corpus,
    so a systematic failure has to be visible from the outside. That is
    affordable only because the version dependence above is gone -- what is
    left to fail is a genuinely malformed value, which is rare. It logs and
    returns None rather than raising: a chunk of fifty books shares one
    execution, and raising here would cost the other forty-nine their write
    over one bad date.

    The value itself never reaches the log. type is enough to tell a
    non-string apart from a malformed string, which is the whole diagnosis.

    A parsed value with no offset at all is given UTC explicitly rather than
    handed naive to a timestamptz column, where Postgres would read it in
    whatever the session's TimeZone happens to be.
    """
    if not raw:
        return None
    if not isinstance(raw, str):
        logger.warning(
            "Unreadable publication datetime",
            extra={"asin": asin, "value_type": type(raw).__name__},
        )
        return None

    candidate = raw.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        logger.warning(
            "Unreadable publication datetime",
            extra={"asin": asin, "value_type": type(raw).__name__},
        )
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


# Every character Unicode gives the White_Space property, for btrim's second
# argument. btrim(x) with no second argument trims U+0020 and nothing else --
# not a tab, not a newline, not the U+00A0 a copied web page leaves behind, not
# the U+3000 ideographic space that is ordinary in the Japanese catalogue -- so
# a value made of any of those reaches SQL as a non-empty string and passes for
# a real answer. Naming the set is what makes "blank" mean blank.
#
# Written as escapes rather than as the characters themselves: most of them are
# invisible on screen and several are indistinguishable from a plain space, so
# a literal set could be corrupted by an ordinary edit with nothing to show it.
#
# The set is exactly White_Space and stops there. Zero-width format characters
# -- U+200B, U+FEFF and their neighbours -- are not whitespace in Unicode and
# are not trimmed, so a value made only of those still reads as an answer.
# Deliberate: they are not what stray catalogue text carries, and a set that
# drifts past the standard has no definition left to check it against.
_BLANK_CHARS = (
    "\t\n\v\f\r "  # U+0009..U+000D, U+0020
    "\u0085"  # next line
    "\u00a0"  # no-break space
    "\u1680"  # ogham space mark
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029"  # line separator, paragraph separator
    "\u202f"  # narrow no-break space
    "\u205f"  # medium mathematical space
    "\u3000"  # ideographic space
)


def _coalesce(new_value, existing_col):
    """Returns new_value if not null, otherwise keeps the existing column value."""
    return func.coalesce(new_value, existing_col)


def _answered(new_value, existing_col):
    """
    Keeps the incoming value only where Audible actually answered, so a blank
    response cannot blank a stored one.

    _coalesce is the right merge wherever "no answer" reaches SQL as NULL, and
    for most of the book row it does. It is the wrong merge for the text
    columns it guards below, because a blank from Audible is not always a
    null: a response group that carries a field with nothing to put in it
    sends an empty string, and coalesce('', books.publisher) is '' — SQL sees
    a value and takes it, so a stored publisher is replaced by nothing. That
    is the shrinkage rule failing inside the merge written to enforce it,
    silently, on an ordinary refresh of a book that already had a publisher.

    Emptiness is measured after trimming every character Unicode calls
    whitespace — _BLANK_CHARS spells the set out and records what it leaves
    alone — so an all-whitespace value counts as no answer too; it carries
    exactly what '' carries.

    Only the measurement is trimmed. What gets written is the value received,
    verbatim, and that division is about layering rather than tidiness:
    trimming here would put a normalization rule in the module that merges
    rows, where nobody would think to look for one, and would leave the stored
    text differing from what app/services/audible/ produced with nothing to
    say which of the two is the record. Cleaning what Audible sends belongs to
    the fetch layer. This module chooses between two values and alters
    neither.

    NULL keeps behaving as it always did. btrim(NULL, ...) is NULL, NULL != ''
    is NULL rather than true, and the CASE falls to its ELSE, which is the
    stored value. This is _coalesce plus one more case, never less.

    Not a rule for every column, and deliberately not applied as one. It fits
    only where a blank cannot be an assertion; where Audible could mean "none"
    by sending nothing, swallowing the blank would pin a stale value in place
    forever. Which columns qualify is set out at the merge itself, together
    with the argument for plans — the one column there that pointedly does
    not, because an empty plans array is Audible asserting a book has left
    the Plus catalogue rather than declining to answer.
    """
    return case(
        (func.btrim(new_value, _BLANK_CHARS) != "", new_value),
        else_=existing_col,
    )


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
    indistinguishable from one it answered blank. Whitespace means the full
    Unicode set _answered measures against, not btrim's bare default, which
    trims the space character alone and would have read a lone tab as text.
    Only the measurement is trimmed — the value written is the value received,
    verbatim.
    """
    absent = -1
    new_length = func.coalesce(
        func.length(func.nullif(func.btrim(new_value, _BLANK_CHARS), "")), absent
    )
    existing_length = func.coalesce(func.length(existing_col), absent)
    return case(
        (new_length > existing_length, new_value),
        else_=existing_col,
    )


def _chapter_count(payload):
    """
    Counts the chapters carried by a TrackContentDto payload, without ever
    raising on one that carries none.

    jsonb_array_length raises 22023 on anything that is not a JSON array — an
    object, a string, a number, a json null — and neither side of the
    comparison can promise it is looking at one: the incoming payload is
    whatever Audible answered, and a stored one may have been written by any
    earlier version of the normalizer. The inner CASE does not test the value
    so much as replace it. Both of its arms are arrays, so what reaches the
    length function is always an array and the error is structurally
    unreachable rather than merely guarded against.

    The equivalent AND — typeof(x) = 'array' AND jsonb_array_length(x) > 0 —
    would in fact survive in both positions this helper is used from today.
    Boolean expressions short-circuit at execution in a scalar CASE arm and in
    a RETURNING list, so neither the ON CONFLICT SET nor the returned count
    can reach the length function with a non-array. It is quals the planner
    reorders: measured on postgresql 16.14, the same AND inside a WHERE failed
    with 22023 against plain built-ins of equal cost, with EXPLAIN showing the
    source order inverted. This is a reusable helper, already called from both
    chapter writers, so the first "find tracks with no chapters" — a WHERE —
    turns that into a live error against precisely the malformed rows it
    exists to tolerate. A green suite is therefore not grounds to flatten this
    into the AND: every test there is exercises a position where the AND is
    safe, and none of them can fail on the position where it is not.

    payload["chapters"] compiles to postgresql's jsonb subscript rather than
    ->, which needs postgresql 14 or newer and is a syntax error before it.
    docker-compose pins postgres:16-alpine, so the deployed path is clear —
    but this is the first jsonb key extraction in the writer, the floor is new
    for the file, and a self-hoster on an older server is who it would
    surprise.
    """
    chapters = payload["chapters"]
    return func.jsonb_array_length(
        case(
            (func.jsonb_typeof(chapters) == "array", chapters),
            else_=func.jsonb_build_array(),
        )
    )


def _chaptered_wins(new_value, existing_col):
    """
    Keeps whichever chapter payload actually lists chapters, so a response
    that carries none cannot erase one that is already stored.

    The floor is emptiness and nothing finer, deliberately. An empty list is
    the JSONB analogue of the NULL _coalesce guards: it asserts nothing, so
    preferring the stored payload loses no answer Audible gave. Two non-empty
    lists are two real answers, and the shorter one is not necessarily the
    poorer — a reissue can genuinely re-cut a title into fewer, longer
    chapters, and a count or duration floor would pin the first list we ever
    saw and refuse every correction after it. Merging the two is meaningless:
    chapters are an ordered whole, not a set of independently sourced fields.

    The whole payload moves together, not just the list. A chapterless
    response reads as runtimeLengthMs 0 and brandIntroDurationMs 0 because
    Audible omits those fields and _normalize_chapters supplies the zero, not
    because Audible asserted one; writing them beside a retained list would
    leave a row that disagrees with itself.

    When neither payload lists chapters the incoming one is taken, which means
    a stored runtimeLengthMs can be replaced by a defaulted 0. That is a
    named consequence rather than an oversight: keeping it would mean merging
    the payload field by field, and a chapter payload is an ordered whole. It
    is also no worse than the unguarded write this replaces, which overwrote
    in every case, chapters included.

    Both counts are measured rather than tested for null because the column is
    NOT NULL on both sides — the thing being guarded against here is a present,
    well-formed payload with nothing in it, which is precisely what a truthy
    but chapterless chapter_info normalizes into.
    """
    return case(
        (_chapter_count(new_value) > 0, new_value),
        (_chapter_count(existing_col) > 0, existing_col),
        else_=new_value,
    )


def _extras_union(new_value, existing_col):
    """
    Merges two extras blobs key by key, so a thin response adds to a rich
    stored blob and can never replace it.

    None of the four merges already in this file is right for this column,
    and which one it is not is the argument for what it is.

    Not _answered, which is settled by type rather than by argument: it
    measures emptiness with btrim, and btrim(jsonb, unknown) does not resolve
    at all -- postgresql has no implicit cast from jsonb to text. It is the
    merge for the text columns this same statement uses it on, and it cannot
    reach this one.

    Not _coalesce. The blob is not all-or-nothing from Audible's side: the
    same ASIN returns 49 top-level keys while it is purchasable and 32 once
    it reads NOT_AVAILABLE_FOR_PURCHASE, losing product_images, plans,
    publisher_summary, isbn and runtime_length_min among them. Taking the
    incoming blob whole would be the shrinkage rule failing inside the merge
    written to enforce it.

    Not _longer_wins. Byte length is not a measure of information here -- one
    long editorial review outweighs ten dropped keys on the scale that
    function measures, and the ten keys are the data.

    Not _chaptered_wins, and this is the instructive one. That merge refuses
    to combine its two payloads because chapters are an ordered whole rather
    than a set of independently sourced fields. This blob is the exact
    opposite: different response groups populate different top-level keys, so
    the keys genuinely are independently sourced and combining them is the
    only merge that keeps what each response actually contributed.

    Both NULL arms are load-bearing. The obvious shorthand --
    coalesce(stored, '{}') || coalesce(incoming, '{}') -- turns a column that
    has never been written into an empty object, and that is not cosmetic:
    NULL here means "no response has written this row yet", which is what an
    operator reads to see how much of the corpus has been rewritten since the
    column landed. Nothing selects on it automatically, and the migration
    turns down an index for it on exactly that ground. A NULL incoming blob
    means the extras were dropped whole at normalization (size cap, depth
    cap, unserializable) rather than that Audible sent nothing, so it must
    leave the stored value exactly as it found it.

    The containment arm is a write guard, not a tidiness one, and it is worth
    26x. Measured per row: an unguarded || wrote 9,463 bytes of WAL, and the
    same merge short-circuited to the stored datum wrote 368. Postgres does
    not update a toasted value in place, so returning the stored datum
    unchanged reuses its toast pointer and writes nothing out of line, while
    returning a rebuilt object rewrites every chunk of it. It does nothing for
    the one-off backfill, where every row is NULL and takes the second arm --
    it is every seeder walk after that which it protects, at roughly 0.66GB
    per full pass instead of 17GB.

    Two limits of || that a reader should not have to discover. It is shallow:
    if a key's value is an object and the incoming one has fewer sub-keys, the
    whole sub-object is replaced and the shrinkage rule is defeated one level
    down. Postgres has no deep jsonb merge built in, and writing one is
    complexity this has not earned -- but it is a real hole rather than an
    accepted invariant. And the result is a union over time, not a snapshot: a
    key Audible genuinely stops sending is never removed from the blob. That
    is the same posture as the additive pivot inserts, and a consumer reading
    the blob as "what Audible said last" will be wrong about it.

    extras_withheld is merged by this same function, and the argument above
    transfers whole rather than by analogy: it is a record whose top-level
    keys are independently sourced too, describing this same blob, and
    merging the two columns differently gave them different spans of time
    while presenting them to a caller as one picture. Its merge site says
    what that leaves the column meaning.
    """
    return case(
        (new_value.is_(None), existing_col),
        (existing_col.is_(None), new_value),
        (existing_col.contains(new_value), existing_col),
        else_=existing_col.op("||", return_type=JSONB)(new_value),
    )


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

    The one reader for every NOT NULL boolean this writer merges, tri-state
    (is_listenable, is_buyable, is_vvab) and plain (explicit, whisper_sync,
    has_pdf) alike — the two groups differ only in what the insert side
    coalesces a silent None to (True, True, False for the tri-state three;
    False, matching the column default, for the other three), never in how
    the bind is read. Anything that is neither a bool nor a string is not an
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
                        # Same answered-versus-blank merge the book row's
                        # image gets, and for the same reason: a portrait is
                        # replaced by another URL, never withdrawn to
                        # nothing, so a blank is a thin response rather than
                        # an assertion. No blank can reach these two author
                        # writers today — _parse_authors builds every author
                        # this path sees with image None — but that is a
                        # constant in another module, invisible from here
                        # and free to change, and upsert_author_profile
                        # below is already reachable by one.
                        image=_answered(author.get("image"), Author.image),
                        description=_longer_wins(author.get("description"), Author.description),
                        updated_at=_now(),
                    )
                )
                await nested.commit()
            except (IntegrityError, AsyncpgUniqueViolation):
                # A concurrent request already upgraded a row (or inserted a
                # new one) to this exact (asin, region, name) between our
                # SELECT and this UPDATE, colliding with
                # authors_asin_region_name_unique. Our UPDATE lost the race
                # and was rolled back — null_id still has asin IS NULL, so
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
                "image": _answered(author.get("image"), Author.image),
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
      that is already stored. The update reads that bind through _answered,
      which counts '' and whitespace as no answer alongside NULL. It used to
      read it through a plain coalesce and rely on the bind never carrying
      '', which nothing in this module was in a position to guarantee.
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
        explicit=_coalesce(bindparam("explicit"), False),
        whisper_sync=_coalesce(bindparam("whisper_sync"), False),
        has_pdf=_coalesce(bindparam("has_pdf"), False),
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
        num_ratings=bindparam("num_ratings"),
        num_reviews=bindparam("num_reviews"),
        publication_name=bindparam("publication_name"),
        publication_datetime=bindparam("publication_datetime"),
        extended_product_description=bindparam("extended_product_description"),
        product_state=bindparam("product_state"),
        # none_as_null is not optional on either of these, for the reason
        # spelled out at plans above and with the same consequence: without
        # it a Python None serializes to the JSON value null, which is a
        # value rather than SQL NULL, and the merge would prefer it to the
        # stored blob and empty the column on every response whose extras
        # were dropped whole.
        #
        # It is also what makes the three-state column work rather than
        # merely what keeps it safe. A genuinely empty extras dict is {} in
        # Python, which is not None, so it serializes to a real written
        # empty object and records that Audible was asked and had nothing to
        # add -- distinct from the NULL of a row no response has touched
        # since the column was added.
        audible_extras=cast(bindparam("audible_extras", type_=JSONB(none_as_null=True)), JSONB),
        extras_withheld=cast(bindparam("extras_withheld", type_=JSONB(none_as_null=True)), JSONB),
        created_at=bindparam("created_at"),
        updated_at=bindparam("updated_at"),
    )
    return stmt.on_conflict_do_update(
        index_elements=["asin"],
        set_={
            # Sixteen text columns merge on answered-versus-blank rather
            # than on NULL alone. Audible has no vocabulary for retracting
            # any of them — no response means "this book no longer has a
            # publisher" — so an empty string is Audible declining to answer,
            # never Audible asserting none, and _answered keeps what is
            # already stored. Each was decided on its own grounds, not by
            # applying one rule across the row. Fourteen are argued here;
            # publication_name and product_state are the other two, argued
            # in the scalar block further down beside the columns they
            # arrived with:
            #
            #   title                   The edition's own name. A reissue
            #                           renames a book; nothing un-names one.
            #                           title is NOT NULL besides, so a blank
            #                           there is the worst loss on the row.
            #   subtitle                Goes with title but stands on weaker
            #                           ground, and the difference is worth
            #                           stating rather than borrowing: a
            #                           second edition genuinely dropping its
            #                           subtitle is a real thing, so a blank
            #                           here could be an answer in a way a
            #                           blank title could not. It is guarded
            #                           anyway because the tie-break for this
            #                           row is already settled — stale but
            #                           rich beats fresh but empty — not
            #                           because title's argument covers it.
            #   publisher, copyright,   Catalogue identity, fixed at
            #   isbn, language, sku,    publication. A blank is a response
            #   sku_group               group that came back thin, not a fact
            #                           that changed underneath us.
            #   image                   A cover is superseded by another URL,
            #                           never withdrawn to nothing.
            #   book_format,            Classification labels, guarded on the
            #   content_type,           same ground as the rest of the row.
            #   content_delivery_type,  The stronger argument — that each
            #   episode_type            draws from a fixed vocabulary that
            #                           does not contain '', putting a blank
            #                           outside the answer set rather than in
            #                           it — is unverified and should not be
            #                           relied on: nothing in this repo
            #                           enumerates any of the four, and no
            #                           live probe has established them.
            #   episode_number          Reaches this statement only through
            #                           _normalize_product, which already
            #                           turns a falsy episode number into
            #                           None, so what a guard adds today is
            #                           the whitespace-only case alone.
            #                           Guarded regardless: this merge cannot
            #                           see that upstream truthiness test,
            #                           and a column whose safety lives in
            #                           another module is one edit away from
            #                           the defect the other thirteen had.
            "title": _answered(bindparam("title"), Book.title),
            "subtitle": _answered(stmt.excluded.subtitle, Book.subtitle),
            "region": Book.region,
            "description": _longer_wins(bindparam("description"), Book.description),
            "summary": _longer_wins(bindparam("summary"), Book.summary),
            "publisher": _answered(stmt.excluded.publisher, Book.publisher),
            "copyright": _answered(stmt.excluded.copyright, Book.copyright),
            "isbn": _answered(stmt.excluded.isbn, Book.isbn),
            "language": _answered(stmt.excluded.language, Book.language),
            "rating": _coalesce(stmt.excluded.rating, Book.rating),
            "release_date": _coalesce(stmt.excluded.release_date, Book.release_date),
            "length_minutes": _coalesce(stmt.excluded.length_minutes, Book.length_minutes),
            # Same asserted-versus-silent merge as is_listenable/is_buyable/
            # is_vvab below, and for the same reason: the column is NOT NULL,
            # so a response that omits explicit/whisperSync/hasPdf cannot be
            # told apart from one asserting false unless the bind itself
            # carries that distinction. _asserted_bool reads the raw payload
            # for these three exactly as it does for the other three; reading
            # through stmt.excluded here instead of the bindparam would take
            # the insert side's own coalesce-to-False rather than the bind
            # Audible actually sent, making a response that omits the field
            # indistinguishable from one asserting false and silently
            # discarding a stored true.
            "explicit": _coalesce(bindparam("explicit"), Book.explicit),
            "whisper_sync": _coalesce(bindparam("whisper_sync"), Book.whisper_sync),
            "has_pdf": _coalesce(bindparam("has_pdf"), Book.has_pdf),
            "image": _answered(stmt.excluded.image, Book.image),
            "book_format": _answered(stmt.excluded.book_format, Book.book_format),
            "content_type": _answered(stmt.excluded.content_type, Book.content_type),
            "content_delivery_type": _answered(
                stmt.excluded.content_delivery_type, Book.content_delivery_type
            ),
            "episode_number": _answered(stmt.excluded.episode_number, Book.episode_number),
            "episode_type": _answered(stmt.excluded.episode_type, Book.episode_type),
            "sku": _answered(stmt.excluded.sku, Book.sku),
            "sku_group": _answered(stmt.excluded.sku_group, Book.sku_group),
            # The other three NOT NULL booleans merge the same way, on
            # asserted-versus-silent rather than true-versus-false: excluded
            # here would carry the insert default and overwrite a stored
            # answer with one Audible never gave. See _asserted_bool for why
            # the bind is tri-state.
            "is_listenable": _coalesce(bindparam("is_listenable"), Book.is_listenable),
            "is_buyable": _coalesce(bindparam("is_buyable"), Book.is_buyable),
            "is_vvab": _coalesce(bindparam("is_vvab"), Book.is_vvab),
            # plans stays on the NULL-only merge, and that is a decision
            # rather than an omission. It is the one column here where a
            # blank is a real answer: an empty plans array is how a book that
            # has left the Plus catalogue reports itself, and Audible is
            # entitled to assert exactly that. Guarding it would hold a book
            # in a catalogue it no longer belongs to — trading a silent
            # shrink for a silent staleness, which is not self-evidently the
            # better bargain. Which way that trade should go is a product
            # question about the plans field, not a question about this
            # merge, and it is open.
            #
            # Open, but not unattended, and a reader deciding from this
            # statement alone would not know that. _parse_plans has already
            # ruled on the same column from the other end: None for a
            # response carrying no plans key, [] only for an explicitly
            # empty one, and — the case that matters here — None again when
            # entries are present but none of them yields a readable
            # plan_name. That third fold is what keeps an upstream rename
            # from emptying this column across the corpus, which is the
            # damage a guard here would otherwise be needed for. What is
            # left open is narrower than it looks: only whether Libex should
            # keep believing Audible when Audible says, clearly, none.
            "plans": _coalesce(stmt.excluded.plans, Book.plans),
            # The six scalar columns beside the blob each get the merge its
            # own field argues for, not the one its type suggests.
            #
            #   num_ratings,            Plain NULL merges. Both normalize to
            #   num_reviews             None rather than 0 when Audible does
            #                           not answer, which is what keeps this
            #                           coalesce honest -- a bound 0 is a
            #                           value, and would overwrite a stored
            #                           thirty thousand with nothing.
            #   publication_datetime    Plain NULL merge. A publication
            #                           instant is fixed at publication; a
            #                           later response either restates it or
            #                           omits it.
            #   publication_name        _answered rather than coalesce: this
            #                           is one of the fields a thin response
            #                           group returns as '' rather than
            #                           omitting, and coalesce('', stored)
            #                           is '' -- SQL sees a value and takes
            #                           it.
            #   extended_product_       Same family as description and
            #   description             summary, and merged the same way. It
            #                           is the long-form text of the row, it
            #                           grows as response groups fill in, and
            #                           a plain coalesce would let a shorter
            #                           later response win.
            #   product_state           _answered, which is the only one of
            #                           the three that clears both hazards
            #                           this column has. A thin response
            #                           group sends it as '' rather than
            #                           omitting it, and coalesce('', stored)
            #                           is '' -- a blank would blank a state
            #                           the row already knew. And a length
            #                           measure is wrong for a different
            #                           reason worth keeping in view: this is
            #                           a state that legitimately changes, a
            #                           book moving between AVAILABLE,
            #                           AVAILABLE_FOR_PREORDER and
            #                           NOT_AVAILABLE_FOR_PURCHASE over its
            #                           life, so _longer_wins would pin
            #                           NOT_AVAILABLE_FOR_PURCHASE (26
            #                           characters) permanently over
            #                           AVAILABLE (9) and leave the row
            #                           asserting a book is unbuyable forever.
            "num_ratings": _coalesce(stmt.excluded.num_ratings, Book.num_ratings),
            "num_reviews": _coalesce(stmt.excluded.num_reviews, Book.num_reviews),
            "publication_name": _answered(
                stmt.excluded.publication_name, Book.publication_name
            ),
            "publication_datetime": _coalesce(
                stmt.excluded.publication_datetime, Book.publication_datetime
            ),
            "extended_product_description": _longer_wins(
                bindparam("extended_product_description"), Book.extended_product_description
            ),
            "product_state": _answered(stmt.excluded.product_state, Book.product_state),
            # Read through excluded rather than the bindparam deliberately:
            # excluded carries the insert side's cast to JSONB, and @> and ||
            # both need the operand to be typed jsonb to resolve at all.
            "audible_extras": _extras_union(stmt.excluded.audible_extras, Book.audible_extras),
            # The record of what was left out of the blob, merged the way the
            # blob itself is, because the two are read as one picture and a
            # pair covering different spans of time cannot be read that way.
            #
            # This was a plain coalesce, which made the column "whatever the
            # most recent fetch that withheld anything happened to withhold"
            # while audible_extras beside it accumulated key by key. A podcast
            # fetch records relationships {episode: 4412}; a later fetch that
            # strips one NUL character replaces the whole record, and the
            # episode count is gone while the relationships key it described
            # is still sitting in the blob. That is the shrinkage rule failing
            # inside a merge, for the identical reason _extras_union exists:
            # the top-level keys are independently sourced. relationships
            # comes from the podcast strip, sanitized from the jsonb
            # sanitizer, audibleExtras from the depth, encode and size checks
            # -- three producers that fire independently, so one of them
            # firing must not erase another's finding.
            #
            # What the column means now: per kind of withholding, the record
            # left by the most recent fetch that withheld that kind -- unless
            # that fetch's account was already contained in the stored one,
            # which the containment arm below leaves standing rather than
            # rewriting, so the fuller entry survives a thinner later one. A
            # union over time, never cleared, exactly as the blob is -- so
            # "this key is in the blob" and "this was withheld from it" are
            # claims about the same span, and the caller can hold them
            # together.
            #
            # It is deliberately not a snapshot of what is missing from the
            # row as it stands, because no merge available here can make it
            # one: a key withheld once and supplied by a later fetch leaves a
            # note that outlives what it describes. Keeping that stale note is
            # the accepted side of the trade, unchanged from before -- the
            # alternative is a fetch that said nothing erasing the only record
            # that anything was ever dropped.
            #
            # Clearing on a clean fetch is not available either, and that is a
            # property of the input rather than a choice made here. The
            # normalizer omits extrasWithheld when nothing was withheld, so
            # "nothing withheld this time" and "this write has no opinion"
            # both arrive as NULL and are indistinguishable at this point.
            # Reading NULL as "clear it" would clear the record on every
            # ordinary write that never looked.
            "extras_withheld": _extras_union(
                stmt.excluded.extras_withheld, Book.extras_withheld
            ),
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
    one execution, one such row costs all fifty their transaction. The six
    NOT NULL booleans — is_listenable, is_buyable, is_vvab, explicit,
    whisper_sync, has_pdf — are the deliberate exception: their None is
    answered by a coalesce on both sides of the statement and never reaches
    the column.
    """
    return {
        "asin": data["asin"],
        # Bound as received, '' included. The update merges it through
        # _answered, which reads a blank title as no answer at all; the
        # normalizer drops titleless products upstream, but that filter
        # lives in another module and the merge does not lean on it.
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
        "explicit": _asserted_bool(data.get("explicit")),
        "whisper_sync": _asserted_bool(data.get("whisperSync")),
        "has_pdf": _asserted_bool(data.get("hasPdf")),
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
        "num_ratings": data.get("numRatings"),
        "num_reviews": data.get("numReviews"),
        "publication_name": data.get("publicationName"),
        "publication_datetime": _parse_publication_datetime(
            data.get("publicationDatetime"), data.get("asin")
        ),
        "extended_product_description": data.get("extendedProductDescription"),
        "product_state": data.get("productState"),
        "audible_extras": data.get("audibleExtras"),
        # Absent from the normalized dict altogether when nothing was
        # withheld, rather than present and empty, so None is the ordinary
        # case here rather than a sign anything went wrong.
        "extras_withheld": data.get("extrasWithheld"),
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
    series: dict[str, dict] = {}
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
            # Deduped like every sibling collection here. Fifty books of
            # one series otherwise issued fifty identical upserts against
            # the same row, each re-taking its row lock — the exact case
            # book_series_links collapses a few lines below, and the case
            # the docstring above already claimed was collapsed.
            series.setdefault(params["asin"], params)
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
        await session.execute(_SERIES_UPSERT, list(series.values()))
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
    keeps an ordinary bad book's failure to itself.

    That is most of "one bad book costs only itself" and not all of it, which
    is worth stating exactly, because the missing part used to read here as
    settled. The rollback below is unguarded — nothing catches it — so a
    connection that has died under the statement raises there instead, and the
    failure leaves this function after all. The property holds because the
    caller carries the rest of it: _replay_book_chunk guards this call and
    clears the session before it reaches the next book. Neither half is
    sufficient alone.

    The batched persist calls write_books directly on its normal path.

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
    """
    Upserts chapter data for a book, keeping the richer of the two payloads.

    The merge is decided in the SET clause rather than by reading the row
    first: several fetch paths can be refreshing the same ASIN at once, and a
    read-compare-write would let two of them agree the stored row was empty
    before either had written. _chaptered_wins settles it inside the one
    statement, against the row as postgresql has it locked.

    updated_at is bumped either way. It records that the row was reconsidered,
    which is true whether or not the payload changed, and nothing reads it for
    staleness.

    Unlike the batched book upsert, this statement may carry returning():
    that statement's hazard is the insertmanyvalues rewrite, which only
    applies to an executemany, and this is a single row with literal values.
    The count comes back so a suppressed overwrite can be logged — a write
    that silently declines is no easier to diagnose than the silent overwrite
    it replaces, and no one is watching this path.
    """
    try:
        stmt = insert(Track).values(
            asin=asin,
            chapters=chapters_data,
            created_at=_now(),
            updated_at=_now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["asin"],
            set_={
                "chapters": _chaptered_wins(stmt.excluded.chapters, Track.chapters),
                "updated_at": _now(),
            },
        ).returning(_chapter_count(Track.chapters))

        result = await session.execute(stmt)
        stored_count = result.scalar() or 0
        await session.commit()

        offered = chapters_data.get("chapters") if isinstance(chapters_data, dict) else None
        offered_count = len(offered) if isinstance(offered, list) else 0

        if offered_count == 0 and stored_count > 0:
            logger.warning(
                "Kept stored chapters over an empty response",
                extra={"asin": asin, "stored_chapters": stored_count},
            )
        else:
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
                    # The one author path a blank can actually arrive on.
                    # _normalize_author passes the contributors response's
                    # profile_image_url straight through, unfiltered and
                    # unstripped, so a contributor whose image field comes
                    # back empty reaches this merge as '' — and coalesce
                    # would take it and blank a stored portrait on an
                    # ordinary profile refresh.
                    "image": _answered(data.get("image"), Author.image),
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
