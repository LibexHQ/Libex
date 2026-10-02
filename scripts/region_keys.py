"""
Online phase of the region-aware key change.

Books and series are identified by (asin, region), but the stored schema keys
them on asin alone and the link tables point at asin alone. Widening that in
one migration would mean rewriting every link row while the API is down, so
the slow, data-proportional work is done here, ahead of time, against the
running service, and the migration that ships afterwards is catalog-only.
This script changes no application behaviour: every step is additive, and the
old code keeps working on the expanded schema.

RUN IT from the API image (it needs nothing but DATABASE_URL), one mode at a
time:

    python -m scripts.region_keys expand
    python -m scripts.region_keys backfill [--batch-size 5000]
    python -m scripts.region_keys index
    python -m scripts.region_keys verify [--pre-window]
    python -m scripts.region_keys finalize --i-have-stopped-writers

expand     ADD COLUMN IF NOT EXISTS, nullable, no default, type region_enum:
           book_region on author_book, book_narrator, book_genre and
           book_series; series_region on book_series and series_author;
           region on tracks. Each statement runs under a lock_timeout and is
           retried with backoff when it cannot get its lock, so it can never
           queue behind a long reader and stall every query behind it.
backfill   Batched UPDATE..FROM books/series, keyset-paged on the asin column
           (the link tables have no id), one commit per batch. Resumable and
           idempotent by its IS NULL predicate: progress lives in the data,
           not in a cursor file that could be lost or go stale, so a crashed
           run simply finds the remaining NULLs on its next pass. Rows the old
           code inserts while this runs arrive with NULL and are picked up by
           the catch-up in finalize.
index      CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS for every target
           key (names in INDEXES below, which the schema revision that adopts
           them must reuse). A concurrent build that fails leaves an INVALID
           index that IF NOT EXISTS would then skip forever, so every index is
           checked in pg_index.indisvalid afterwards and an invalid one is
           dropped and rebuilt.
verify     Read-only report: column presence, NULL counts, nullability, index
           validity, row counts. Exits 1 unless the database is ready for the
           cut-over. --pre-window relaxes that to "columns exist and indexes
           are valid": NULLs and nullability are only reported, because old
           code is still inserting them.
finalize   MAINTENANCE WINDOW ONLY. Refuses to run without
           --i-have-stopped-writers: the API and every background writer must
           be stopped first, or the row-count and NULL assertions mean
           nothing. Catch-up backfill, then per column CHECK (col IS NOT NULL)
           NOT VALID -> VALIDATE -> SET NOT NULL -> drop the CHECK (the
           validated CHECK is what lets SET NOT NULL skip its own full scan),
           and the same for series.region. Per-table row counts must be equal
           before and after and no NULL may remain, or the run aborts loudly.
           No CHECK exists before this: a NOT VALID CHECK still rejects new
           rows, and old code inserts NULL.

SIZING (hosted, measured by the operator 2026-10-02): book_genre 10.6M rows,
author_book 2.5M, book_narrator 2.5M, tracks 2.0M, book_series 743k,
series_author 300k; books 2.1M, series 170k. --batch-size counts asins, not
rows, so a book_genre batch of 5000 is roughly 25k rows -- one commit each
keeps every transaction short and lets autovacuum reclaim the dead tuples the
UPDATEs leave. Each page is a keyset range scan on the leading asin column of
an existing index (book_genre_index, book_author_index, book_narrator_index,
book_series_index, series_author_index, the tracks primary key). The one
exception is book_series.series_region: nothing leads with series_asin on that
table, so each of its batches scans 743k rows; that is the smallest of the
link tables and the only cost. Every step logs its elapsed seconds so a
maintenance window can be timed from a rehearsal.

Hosted Postgres only; the script refuses any other backend before opening a
connection. It makes no Audible request and so carries no dedicated-exit
guard.

STOPPING. SIGTERM/SIGINT set a flag consulted between batches and between
statements; a batch in flight commits first, a CONCURRENTLY build already
running is left to finish (cancelling it is exactly what leaves an invalid
index). Every mode is safe to run again after a stop.

EXIT CODES.

    0   done (verify: ready).
    1   failed, an assertion fired, or verify says not ready.
    2   unusable arguments, or not Postgres.
    3   stopped by a signal before finishing; run the same mode again.

ENVIRONMENT.

    DATABASE_URL        required. Read through app.core.config.
    LOG_LEVEL           INFO    DEBUG, INFO, WARNING or ERROR.
    AXIOM_TOKEN         (unset) set to also ship logs to Axiom.
    AXIOM_DATASET       libex
    LOG_RETENTION_DAYS  7       0 keeps everything.
"""

# Standard library
import argparse
import asyncio
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# Third party
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

# Core
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging

logger = get_logger()

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_STOPPED = 3

DEFAULT_BATCH_SIZE = 5000

# DDL that needs ACCESS EXCLUSIVE gives up after this long instead of queueing
# behind a long reader (and every query behind that), then backs off and tries
# again. The attempts and the waits between them bound how long a busy table
# can hold a run up before it fails loudly.
LOCK_TIMEOUT_MS = 3000
BATCH_LOCK_TIMEOUT_MS = 10000
BATCH_STATEMENT_TIMEOUT_MS = 120000
LOCK_ATTEMPTS = 8
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0

# SQLSTATEs worth another attempt: lock_not_available, deadlock_detected.
_RETRYABLE_SQLSTATES = frozenset({"55P03", "40P01"})


# --- what is being added -----------------------------------------------------

@dataclass(frozen=True)
class RegionColumn:
    """A new region column and where its value comes from."""

    table: str
    column: str
    key: str      # the column on `table` that holds the source asin
    source: str   # the table whose region it copies: books or series

    @property
    def label(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def check_name(self) -> str:
        return f"chk_{self.table}_{self.column}_not_null"


REGION_COLUMNS: tuple[RegionColumn, ...] = (
    RegionColumn("author_book", "book_region", "book_asin", "books"),
    RegionColumn("book_narrator", "book_region", "book_asin", "books"),
    RegionColumn("book_genre", "book_region", "book_asin", "books"),
    RegionColumn("book_series", "book_region", "book_asin", "books"),
    RegionColumn("book_series", "series_region", "series_asin", "series"),
    RegionColumn("series_author", "series_region", "series_asin", "series"),
    RegionColumn("tracks", "region", "asin", "books"),
)

# series.region already exists but is nullable. It gets no backfill (0 NULL
# rows is a precondition, not something to invent a value for) and is only
# made NOT NULL by finalize.
SERIES_REGION = RegionColumn("series", "region", "asin", "series")

# Every column finalize makes NOT NULL.
FINAL_COLUMNS: tuple[RegionColumn, ...] = REGION_COLUMNS + (SERIES_REGION,)

# Row counts compared across finalize.
COUNTED_TABLES: tuple[str, ...] = (
    "books",
    "series",
    "tracks",
    "author_book",
    "book_narrator",
    "book_genre",
    "book_series",
    "series_author",
)


@dataclass(frozen=True)
class IndexSpec:
    """A unique index the schema revision will adopt as a key."""

    name: str
    table: str
    columns: tuple[str, ...]


# These names are the contract with the revision and models that adopt the
# indexes (PK/UNIQUE ... USING INDEX renames nothing it did not choose, but the
# revision asserts on these names). Each widened pivot key is a superset of the
# existing unique, so building it can never fail on existing data.
INDEXES: tuple[IndexSpec, ...] = (
    IndexSpec("uq_books_asin_region", "books", ("asin", "region")),
    IndexSpec("uq_series_asin_region", "series", ("asin", "region")),
    IndexSpec("uq_tracks_asin_region", "tracks", ("asin", "region")),
    IndexSpec(
        "uq_author_book_region",
        "author_book",
        ("author_id", "book_asin", "book_region"),
    ),
    IndexSpec(
        "uq_book_narrator_region",
        "book_narrator",
        ("book_asin", "book_region", "narrator_name"),
    ),
    IndexSpec(
        "uq_book_genre_region",
        "book_genre",
        ("book_asin", "book_region", "genre_asin"),
    ),
    IndexSpec(
        "uq_book_series_region",
        "book_series",
        ("book_asin", "book_region", "series_asin", "series_region"),
    ),
    IndexSpec(
        "uq_series_author_region",
        "series_author",
        ("series_asin", "series_region", "author_id"),
    ),
)


class FinalizeAbort(Exception):
    """An assertion in finalize failed; the database is not cut-over ready."""


# --- graceful stop -----------------------------------------------------------

class _Stop:
    """Set by SIGTERM/SIGINT; consulted between units of work."""

    def __init__(self) -> None:
        self.requested = False

    def request(self) -> None:
        if not self.requested:
            logger.info("RegionKeys: stop requested, finishing the current step")
        self.requested = True


def _install_signal_handlers(stop: _Stop) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.request)
        except (NotImplementedError, RuntimeError):
            # Not the main thread / not a Unix loop; the flag is still honoured
            # when something else sets it.
            pass


# --- connections -------------------------------------------------------------

def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def _make_engine(url: str) -> AsyncEngine:
    # NullPool: every helper below sets session-level timeouts, and a pooled
    # connection would carry them into the next, unrelated, statement.
    return create_async_engine(url, hide_parameters=True, poolclass=NullPool)


async def _set_timeouts(conn: AsyncConnection, *, statement_ms: int, lock_ms: int, local: bool) -> None:
    await conn.execute(
        text("SELECT set_config('statement_timeout', :s, :l), set_config('lock_timeout', :k, :l)"),
        {"s": str(statement_ms), "k": str(lock_ms), "l": local},
    )


def _sqlstate(exc: BaseException) -> str | None:
    if isinstance(exc, DBAPIError):
        orig = exc.orig
        for candidate in (orig, getattr(orig, "__cause__", None)):
            state = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
            if state:
                return state
    return None


async def _retry[T](label: str, op: Callable[[], Awaitable[T]], stop: _Stop) -> T:
    """Runs op, retrying with backoff while it fails to get a lock."""
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        try:
            return await op()
        except DBAPIError as exc:
            if _sqlstate(exc) not in _RETRYABLE_SQLSTATES or attempt == LOCK_ATTEMPTS:
                raise
            delay = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS)
            logger.warning(
                "RegionKeys: lock not available, backing off",
                extra={"step": label, "attempt": attempt, "retry_in_s": delay},
            )
            if stop.requested:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def _ddl(engine: AsyncEngine, label: str, sql: str, stop: _Stop) -> None:
    """One DDL statement, autocommit, under lock_timeout, retried on lock waits."""

    async def run() -> None:
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await _set_timeouts(conn, statement_ms=0, lock_ms=LOCK_TIMEOUT_MS, local=False)
            await conn.execute(text(sql))

    started = time.monotonic()
    await _retry(label, run, stop)
    _log_elapsed(label, started)


def _log_elapsed(step: str, started: float) -> None:
    """One line per step with its wall time, so a maintenance window can be timed."""
    logger.info("RegionKeys: step done", extra={"step": step, "elapsed_s": round(time.monotonic() - started, 1)})


# --- catalog reads -----------------------------------------------------------

async def _column_info(conn: AsyncConnection, table: str, column: str) -> str | None:
    """Returns 'YES'/'NO' for is_nullable, or None if the column is absent."""
    result = await conn.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return result.scalar_one_or_none()


async def _constraint_exists(conn: AsyncConnection, table: str, name: str) -> bool:
    result = await conn.execute(
        text(
            "SELECT 1 FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid "
            "WHERE r.relname = :t AND c.conname = :n "
            "AND r.relnamespace = current_schema()::regnamespace"
        ),
        {"t": table, "n": name},
    )
    return result.first() is not None


async def _index_state(conn: AsyncConnection, name: str) -> tuple[bool, bool] | None:
    """Returns (valid, unique) for the named index, or None if it is absent."""
    result = await conn.execute(
        text(
            "SELECT i.indisvalid AND i.indisready, i.indisunique "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :n AND c.relnamespace = current_schema()::regnamespace"
        ),
        {"n": name},
    )
    row = result.first()
    return None if row is None else (bool(row[0]), bool(row[1]))


async def _null_count(conn: AsyncConnection, col: RegionColumn) -> int:
    result = await conn.execute(
        text(f"SELECT count(*) FROM {col.table} WHERE {col.column} IS NULL")
    )
    return result.scalar_one()


async def _row_counts(engine: AsyncEngine) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with engine.connect() as conn:
        await _set_timeouts(conn, statement_ms=0, lock_ms=0, local=False)
        for table in COUNTED_TABLES:
            result = await conn.execute(text(f"SELECT count(*) FROM {table}"))
            counts[table] = result.scalar_one()
    return counts


# --- expand ------------------------------------------------------------------

async def expand(engine: AsyncEngine, stop: _Stop) -> int:
    started = time.monotonic()
    logger.info("RegionKeys: expand starting", extra={"columns": len(REGION_COLUMNS)})
    for col in REGION_COLUMNS:
        if stop.requested:
            logger.info("RegionKeys: expand stopped before finishing")
            return EXIT_STOPPED
        await _ddl(
            engine,
            f"add {col.label}",
            f"ALTER TABLE {col.table} ADD COLUMN IF NOT EXISTS {col.column} region_enum",
            stop,
        )
        logger.info("RegionKeys: column present", extra={"column": col.label})
    _log_elapsed("expand (total)", started)
    logger.info("RegionKeys: expand complete")
    return EXIT_OK


# --- backfill ----------------------------------------------------------------

async def _backfill_column(engine: AsyncEngine, col: RegionColumn, batch_size: int, stop: _Stop) -> tuple[int, bool]:
    """Fills one column. Returns (rows updated, whether it ran to the end)."""
    page_sql = text(
        f"SELECT {col.key} FROM {col.table} "
        f"WHERE {col.column} IS NULL AND {col.key} > :cursor "
        f"GROUP BY {col.key} ORDER BY {col.key} LIMIT :n"
    )
    update_sql = text(
        f"UPDATE {col.table} AS t SET {col.column} = s.region "
        f"FROM {col.source} AS s "
        f"WHERE s.asin = t.{col.key} AND t.{col.column} IS NULL AND t.{col.key} = ANY(:keys)"
    )
    cursor = ""
    total = 0
    batches = 0
    started = time.monotonic()
    while True:
        if stop.requested:
            return total, False

        async def one_batch(after: str = cursor) -> tuple[list[str], int]:
            async with engine.begin() as conn:
                await _set_timeouts(
                    conn,
                    statement_ms=BATCH_STATEMENT_TIMEOUT_MS,
                    lock_ms=BATCH_LOCK_TIMEOUT_MS,
                    local=True,
                )
                keys = [r[0] for r in (await conn.execute(page_sql, {"cursor": after, "n": batch_size})).all()]
                if not keys:
                    return keys, 0
                updated = (await conn.execute(update_sql, {"keys": keys})).rowcount
                return keys, updated

        keys, updated = await _retry(f"backfill {col.label}", one_batch, stop)
        if not keys:
            _log_elapsed(f"backfill {col.label}", started)
            return total, True
        cursor = keys[-1]
        total += updated
        batches += 1
        logger.info(
            "RegionKeys: backfill progress",
            extra={
                "column": col.label,
                "batch": batches,
                "keys": len(keys),
                "rows_updated": updated,
                "rows_total": total,
                "cursor": cursor,
                "elapsed_s": round(time.monotonic() - started, 1),
            },
        )


async def backfill(engine: AsyncEngine, stop: _Stop, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    logger.info("RegionKeys: backfill starting", extra={"batch_size": batch_size})
    async with engine.connect() as conn:
        for col in REGION_COLUMNS:
            if await _column_info(conn, col.table, col.column) is None:
                logger.error("RegionKeys: column missing, run expand first", extra={"column": col.label})
                return EXIT_FAILED
    for col in REGION_COLUMNS:
        total, finished = await _backfill_column(engine, col, batch_size, stop)
        async with engine.connect() as conn:
            remaining = await _null_count(conn, col)
        logger.info(
            "RegionKeys: backfill column pass",
            extra={"column": col.label, "rows_updated": total, "null_remaining": remaining, "finished": finished},
        )
        if not finished:
            logger.info("RegionKeys: backfill stopped before finishing")
            return EXIT_STOPPED
    logger.info("RegionKeys: backfill complete")
    return EXIT_OK


# --- index -------------------------------------------------------------------

async def _ddl_concurrent(engine: AsyncEngine, label: str, sql: str) -> None:
    # No statement or lock timeout: a concurrent build legitimately waits for
    # old transactions and scans the whole table, and cancelling it is what
    # leaves an invalid index behind.
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await _set_timeouts(conn, statement_ms=0, lock_ms=0, local=False)
        started = time.monotonic()
        await conn.execute(text(sql))
    _log_elapsed(label, started)


async def index(engine: AsyncEngine, stop: _Stop) -> int:
    logger.info("RegionKeys: index starting", extra={"indexes": len(INDEXES)})
    for spec in INDEXES:
        if stop.requested:
            logger.info("RegionKeys: index stopped before finishing")
            return EXIT_STOPPED
        async with engine.connect() as conn:
            for col in spec.columns:
                if await _column_info(conn, spec.table, col) is None:
                    logger.error(
                        "RegionKeys: column missing, run expand first",
                        extra={"index": spec.name, "column": f"{spec.table}.{col}"},
                    )
                    return EXIT_FAILED
            state = await _index_state(conn, spec.name)
        if state is not None and not state[0]:
            logger.warning("RegionKeys: invalid index, dropping to rebuild", extra={"index": spec.name})
            await _ddl_concurrent(engine, f"drop invalid {spec.name}", f"DROP INDEX CONCURRENTLY IF EXISTS {spec.name}")
        await _ddl_concurrent(
            engine,
            f"build {spec.name}",
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {spec.name} "
            f"ON {spec.table} ({', '.join(spec.columns)})",
        )
        async with engine.connect() as conn:
            state = await _index_state(conn, spec.name)
        if state is None or not state[0] or not state[1]:
            logger.error(
                "RegionKeys: index not valid and unique after build",
                extra={"index": spec.name, "state": state},
            )
            return EXIT_FAILED
        logger.info("RegionKeys: index valid", extra={"index": spec.name, "table": spec.table})
    logger.info("RegionKeys: index complete")
    return EXIT_OK


# --- verify ------------------------------------------------------------------

async def verify(engine: AsyncEngine, pre_window: bool = False) -> int:
    started = time.monotonic()
    problems: list[str] = []
    async with engine.connect() as conn:
        await _set_timeouts(conn, statement_ms=0, lock_ms=0, local=False)
        for col in FINAL_COLUMNS:
            nullable = await _column_info(conn, col.table, col.column)
            if nullable is None:
                problems.append(f"{col.label} is missing")
                logger.warning("RegionKeys: verify column missing", extra={"column": col.label})
                continue
            nulls = await _null_count(conn, col)
            logger.info(
                "RegionKeys: verify column",
                extra={"column": col.label, "nulls": nulls, "nullable": nullable == "YES"},
            )
            if not pre_window:
                if nulls:
                    problems.append(f"{col.label} has {nulls} NULL")
                if nullable == "YES":
                    problems.append(f"{col.label} is still nullable")
        for spec in INDEXES:
            state = await _index_state(conn, spec.name)
            logger.info(
                "RegionKeys: verify index",
                extra={
                    "index": spec.name,
                    "present": state is not None,
                    "valid": bool(state and state[0]),
                    "unique": bool(state and state[1]),
                },
            )
            if state is None:
                problems.append(f"index {spec.name} is missing")
            elif not state[0]:
                problems.append(f"index {spec.name} is invalid")
            elif not state[1]:
                problems.append(f"index {spec.name} is not unique")
        for col in FINAL_COLUMNS:
            if await _constraint_exists(conn, col.table, col.check_name):
                logger.info("RegionKeys: verify leftover CHECK", extra={"constraint": col.check_name})
                if not pre_window:
                    problems.append(f"CHECK {col.check_name} was left behind")
    counts = await _row_counts(engine)
    logger.info("RegionKeys: verify row counts", extra=counts)
    _log_elapsed("verify", started)
    if problems:
        logger.error(
            "RegionKeys: NOT ready for cut-over",
            extra={"problems": "; ".join(problems), "pre_window": pre_window},
        )
        return EXIT_FAILED
    logger.info("RegionKeys: ready", extra={"pre_window": pre_window})
    return EXIT_OK


# --- finalize ----------------------------------------------------------------

async def _make_not_null(engine: AsyncEngine, col: RegionColumn, stop: _Stop) -> None:
    async with engine.connect() as conn:
        nullable = await _column_info(conn, col.table, col.column)
        has_check = await _constraint_exists(conn, col.table, col.check_name)
    if nullable is None:
        raise FinalizeAbort(f"{col.label} does not exist; run expand first")
    if nullable == "NO" and not has_check:
        logger.info("RegionKeys: already NOT NULL", extra={"column": col.label})
        return
    if not has_check:
        await _ddl(
            engine,
            f"add check {col.label}",
            f"ALTER TABLE {col.table} ADD CONSTRAINT {col.check_name} "
            f"CHECK ({col.column} IS NOT NULL) NOT VALID",
            stop,
        )
    # VALIDATE takes only SHARE UPDATE EXCLUSIVE and scans the table; it
    # needs no lock_timeout of its own beyond the DDL helper's.
    await _ddl(
        engine, f"validate {col.label}", f"ALTER TABLE {col.table} VALIDATE CONSTRAINT {col.check_name}", stop
    )
    if nullable == "YES":
        await _ddl(
            engine, f"set not null {col.label}", f"ALTER TABLE {col.table} ALTER COLUMN {col.column} SET NOT NULL", stop
        )
    await _ddl(
        engine, f"drop check {col.label}", f"ALTER TABLE {col.table} DROP CONSTRAINT {col.check_name}", stop
    )
    logger.info("RegionKeys: column is NOT NULL", extra={"column": col.label})


async def finalize(engine: AsyncEngine, stop: _Stop, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    started = time.monotonic()
    before = await _row_counts(engine)
    _log_elapsed("finalize row counts (before)", started)
    logger.info("RegionKeys: finalize starting (writers must be stopped)", extra=before)

    # Catch-up for rows old code inserted with NULL since the main backfill.
    code = await backfill(engine, stop, batch_size)
    if code != EXIT_OK:
        return code

    async with engine.connect() as conn:
        stragglers = {c.label: await _null_count(conn, c) for c in FINAL_COLUMNS}
    stragglers = {k: v for k, v in stragglers.items() if v}
    if stragglers:
        raise FinalizeAbort(
            "NULL region values remain after the catch-up (orphaned link rows "
            f"or series with no region); nothing was constrained: {stragglers}"
        )

    for col in FINAL_COLUMNS:
        if stop.requested:
            logger.info("RegionKeys: finalize stopped before finishing")
            return EXIT_STOPPED
        await _make_not_null(engine, col, stop)

    after = await _row_counts(engine)
    if after != before:
        raise FinalizeAbort(f"row counts changed during finalize: before={before} after={after}")
    async with engine.connect() as conn:
        for col in FINAL_COLUMNS:
            nulls = await _null_count(conn, col)
            nullable = await _column_info(conn, col.table, col.column)
            if nulls or nullable != "NO":
                raise FinalizeAbort(f"{col.label} is not NOT NULL with zero NULLs after finalize")
            if await _constraint_exists(conn, col.table, col.check_name):
                raise FinalizeAbort(f"CHECK {col.check_name} was left behind")
    _log_elapsed("finalize (total)", started)
    logger.info("RegionKeys: finalize complete", extra=after)
    return EXIT_OK


# --- entry point -------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Online phase of the region-aware key change (see the module docstring)."
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("expand", help="add the nullable region columns")

    p_back = sub.add_parser("backfill", help="fill the new columns in batches")
    p_back.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="asins per batch")

    sub.add_parser("index", help="build and validate the target unique indexes")

    p_ver = sub.add_parser("verify", help="read-only readiness report")
    p_ver.add_argument("--pre-window", action="store_true", help="only require columns and valid indexes")

    p_fin = sub.add_parser("finalize", help="maintenance window only: make the columns NOT NULL")
    p_fin.add_argument("--i-have-stopped-writers", action="store_true", dest="writers_stopped")
    p_fin.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="asins per catch-up batch")
    return parser


async def _run(args: argparse.Namespace, engine: AsyncEngine) -> int:
    stop = _Stop()
    _install_signal_handlers(stop)
    try:
        if args.mode == "expand":
            return await expand(engine, stop)
        if args.mode == "backfill":
            return await backfill(engine, stop, args.batch_size)
        if args.mode == "index":
            return await index(engine, stop)
        if args.mode == "verify":
            return await verify(engine, args.pre_window)
        if args.mode == "finalize":
            return await finalize(engine, stop, args.batch_size)
        raise AssertionError(args.mode)
    except FinalizeAbort as exc:
        logger.error("RegionKeys: ABORTED", extra={"mode": args.mode, "reason": str(exc)})
        return EXIT_FAILED
    except Exception as exc:
        logger.error(
            "RegionKeys: failed",
            extra={"mode": args.mode, "error_type": type(exc).__name__, "error": str(exc)},
        )
        return EXIT_FAILED
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # get_logger only fetches the logger. Without this the handlers are never
    # attached and a standalone script emits nothing at all.
    setup_logging()

    if getattr(args, "batch_size", 1) < 1:
        parser.error("--batch-size must be at least 1")
    if args.mode == "finalize" and not args.writers_stopped:
        logger.error("RegionKeys: finalize refused, --i-have-stopped-writers not given")
        raise SystemExit(EXIT_USAGE)

    url = get_settings().database_url
    if not _is_postgres(url):
        logger.error(
            "RegionKeys: refusing to run, hosted Postgres only",
            extra={"backend": make_url(url).get_backend_name()},
        )
        raise SystemExit(EXIT_USAGE)

    code = asyncio.run(_run(args, _make_engine(url)))
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
