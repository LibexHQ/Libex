"""
The small pieces every writer shares: which database a session is talking to,
the INSERT construct that database spells its upsert with, and the coercions
that turn a normalized response value into something a column takes.
"""

# Standard library
import logging
from datetime import datetime, timezone

# Third party
from sqlalchemy import bindparam, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("libex")

POSTGRESQL = "postgresql"
SQLITE = "sqlite"
SUPPORTED_DIALECTS = (POSTGRESQL, SQLITE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dialect_of(session: AsyncSession) -> str:
    """The name of the dialect the session's bind speaks ("postgresql",
    "sqlite", ...)."""
    return session.get_bind().dialect.name


# One statement takes every lock: the distinct ASINs are sorted inside a
# subquery that has an ORDER BY, which Postgres will not flatten away, so the
# locks are acquired in ASIN order. Two writers whose ASIN sets overlap then
# wait on each other one way round and cannot deadlock.
_LOCK_ASINS = text(
    "SELECT pg_advisory_xact_lock(hashtextextended(:kind || ':' || a, 0)) "
    "FROM (SELECT DISTINCT a FROM unnest(CAST(:asins AS text[])) AS a ORDER BY a) AS sorted"
).bindparams(bindparam("asins", type_=postgresql.ARRAY(postgresql.TEXT)))


async def lock_asins(
    session: AsyncSession, kind: str, asins: list[str], *, dialect: str | None = None
) -> None:
    """
    Serializes writers that are about to insert rows for the same ASINs, until
    the transaction ends.

    Whether a new book or series row is the primary one depends on whether the
    ASIN is already stored, and two transactions that each insert the ASIN
    for a different region cannot see each other's uncommitted row, so both
    would decide they are first. Taking this lock first makes the second wait
    for the first to commit; its insert then sees the committed row and is
    written as not primary. `kind` ("book" or "series") keeps the two tables'
    ASINs from contending with each other.

    A no-op on SQLite, where a write transaction already excludes every other.
    """
    if (dialect or dialect_of(session)) != POSTGRESQL or not asins:
        return
    await session.execute(_LOCK_ASINS, {"kind": kind, "asins": sorted(set(asins))})


def check_dialect(dialect: str) -> str:
    """Returns the dialect name if the writer has SQL for it, ValueError if
    not. The writer has two spellings and no third; a name it does not know
    must not quietly fall through to the Postgres one."""
    if dialect not in SUPPORTED_DIALECTS:
        raise ValueError(
            f"unsupported dialect {dialect!r}; the writer supports "
            + " and ".join(repr(name) for name in SUPPORTED_DIALECTS)
        )
    return dialect


def insert_for(dialect: str):
    """The INSERT construct that carries ON CONFLICT for a dialect.

    SQLite's has the same on_conflict_do_update / on_conflict_do_nothing /
    excluded surface Postgres's does, and renders the same ON CONFLICT clause
    (SQLite 3.24 or newer). Postgres gets the construct the hosted writer has
    always used. Any other dialect raises ValueError.
    """
    check_dialect(dialect)
    return sqlite.insert if dialect == SQLITE else postgresql.insert


def conflict_on_constraint(dialect: str, name: str, columns: list[str]) -> dict:
    """The conflict target for a named unique constraint, as keyword
    arguments for on_conflict_do_update.

    Postgres names the constraint (ON CONFLICT ON CONSTRAINT name). SQLite has
    no such form and takes the constraint's columns instead, which resolves to
    the same unique index. Any other dialect raises ValueError.
    """
    if check_dialect(dialect) == SQLITE:
        return {"index_elements": columns}
    return {"constraint": name}


def parse_release_date(iso_str: str | None) -> datetime | None:
    """Converts an ISO 8601 string back to a datetime for DB storage."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None


def parse_publication_datetime(raw, asin: str | None) -> datetime | None:
    """
    Converts Audible's publication_datetime to a datetime for DB storage.

    Not parse_release_date, and the difference is the last character. That
    function reverses the release-date normalizer, which reads a bare
    "%Y-%m-%d" and re-emits isoformat() with a numeric offset;
    publication_datetime arrives from Audible as a full instant ending in a
    literal Z, which is how the unreleased sentinel is written too
    ("2200-01-01T00:00:00Z"). Python's fromisoformat only learned to accept
    that Z in 3.11, and this module's except returns None, so reusing the
    release-date parser would leave the correctness of a whole column resting
    on the interpreter version: it would work, right up until the interpreter
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


def asserted_bool(value) -> bool | None:
    """
    Reads a boolean the way the shrinkage rule needs it read: True or False
    when Audible asserted one, None when it asserted nothing.

    For every other column "less data" means null against a value, and
    coalesce settles it. A boolean has no null to fall back on -- the column
    is NOT NULL -- so the distinction that matters is asserted versus
    not-asserted, and it has to be carried in the bind rather than in the
    column. A response that simply omits isListenable binds None here, the
    insert takes its own default and the update keeps whatever is stored;
    a response that says false binds False and overwrites, because that is
    Audible answering rather than staying silent.

    The one reader for every NOT NULL boolean the writer merges, tri-state
    (is_listenable, is_buyable, is_vvab) and plain (explicit, whisper_sync,
    has_pdf) alike -- the two groups differ only in what the insert side
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
