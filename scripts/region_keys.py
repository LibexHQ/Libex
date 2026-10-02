"""
Online phase of the region-aware key change.

Books and series are identified by (asin, region), but the stored schema keys
them on asin alone and the link tables point at asin alone. Widening that in
one migration would mean rewriting every link row while the API is down, so
the slow, data-proportional work is done here against the running service, and
the schema revision that adopts the new keys has only catalog changes left to
make. Every step before finalize is additive and changes no application
behaviour: the 2.1.x application keeps running unmodified through expand,
backfill and index.

That guarantee stops at finalize. Finalize builds two unique indexes
(uq_books_asin_region, uq_series_asin_region) and sets NOT NULL on every new
column, and the 2.1.x writer cannot run against either: its upserts name
ON CONFLICT (asin), and a unique index that is not the arbiter raises on a
concurrent first insert instead of merging; and it inserts link rows, new
tracks and region-less series with NULL regions. Finalize is therefore a
one-way door for the running application, and unfinalize is the way back.

RUN IT from the API image (it needs nothing but DATABASE_URL), one mode at a
time:

    python -m scripts.region_keys expand
    python -m scripts.region_keys backfill [--batch-size 5000]
    python -m scripts.region_keys index
    python -m scripts.region_keys verify [--pre-window]
    python -m scripts.region_keys finalize --i-have-stopped-writers
    python -m scripts.region_keys unfinalize --i-have-stopped-writers

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
index      CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS for the online
           indexes: the six link-table keys and uq_tracks_asin_region (names
           in INDEXES below, which the schema revision that adopts them must
           reuse). The 2.1.x writer leaves the new columns NULL on those
           tables, and NULLs never conflict, so these indexes cannot make an
           old insert fail. A concurrent build that fails leaves an INVALID
           index that IF NOT EXISTS would then skip forever, so every index is
           checked in pg_index.indisvalid afterwards and an invalid one is
           dropped and rebuilt.
verify     Read-only report: column presence, NULL counts, nullability, index
           validity, row counts. Exits 1 unless the database is ready for the
           cut-over. --pre-window relaxes that to "columns exist and the
           online indexes are valid": NULLs, nullability and the two window
           indexes are only reported, because old code is still inserting
           NULLs and they are built inside finalize.
finalize   MAINTENANCE WINDOW ONLY. Refuses to run without
           --i-have-stopped-writers: the API and every background writer must
           be stopped first, or the row-count and NULL assertions mean
           nothing, and the two window indexes would break a live 2.1.x
           writer. Order: catch-up backfill; abort if any NULL region remains;
           CREATE UNIQUE INDEX CONCURRENTLY uq_books_asin_region and
           uq_series_asin_region; then per column CHECK (col IS NOT NULL)
           NOT VALID -> VALIDATE -> SET NOT NULL -> drop the CHECK (the
           validated CHECK is what lets SET NOT NULL skip its own full scan),
           and the same for series.region. Per-table row counts must be equal
           before and after and no NULL may remain, or the run aborts loudly.
           No CHECK exists before this: a NOT VALID CHECK still rejects new
           rows, and old code inserts NULL. The two indexes are built here
           rather than in a separate writers-stopped mode because the window
           already has writers stopped, so a second mode would add an
           operator step and a second way to run them in the wrong order
           without shortening the downtime; CONCURRENTLY is kept so readers
           are not blocked while they build.
unfinalize ROLLBACK ONLY. Also refuses without --i-have-stopped-writers. Drops
           NOT NULL on exactly the columns finalize set (every column in
           FINAL_COLUMNS, series.region included), drops any CHECK a partial
           finalize left behind, and drops uq_books_asin_region and
           uq_series_asin_region. The indexes must go: they are the reason a
           rolled-back 2.1.x writer's ON CONFLICT (asin) upserts would raise
           under concurrent first inserts. The columns, the backfilled
           values, the link-table indexes and uq_tracks_asin_region stay
           (harmless to 2.1.x), so finalize can be run again later. Idempotent.
           It is only valid until the schema revision that adopts these
           indexes has run; after that, roll back with that revision's
           downgrade, and an index owned by a constraint makes this mode fail
           rather than drop it.

SIZING (approximate magnitudes on the hosted instance): book_genre about 10M
rows, author_book and book_narrator about 2.5M each, tracks about 2M,
book_series about 750k, series_author about 300k; books about 2M, series about
170k. --batch-size counts asins, not rows, so a book_genre batch of 5000 is
roughly 25k rows -- one commit each keeps every transaction short and lets
autovacuum reclaim the dead tuples the UPDATEs leave. Each page is a keyset
range scan on the leading asin column of an existing index (book_genre_index,
book_author_index, book_narrator_index, book_series_index, series_author_index,
the tracks primary key). The one exception is book_series.series_region:
nothing leads with series_asin on that table, so each of its batches scans the
whole table; that is the smallest of the link tables and the only cost. Every
step logs its elapsed seconds so a maintenance window can be timed from a
rehearsal.

Hosted Postgres only; the script refuses any other backend before opening a
connection. It makes no Audible request and so carries no dedicated-exit
guard.

ONE RUN AT A TIME. Every mode except verify takes a session-level
pg_try_advisory_lock before doing anything and holds it on a dedicated
connection until it exits. A second run exits 1 with a message instead of
starting, because two runs can drop each other's in-progress index build.

STOPPING. SIGTERM/SIGINT set a flag consulted between batches and between
statements; a batch in flight commits first, a CONCURRENTLY build already
running is left to finish (cancelling it is exactly what leaves an invalid
index). A stop that arrives while a lock wait is backing off ends the run at
once. Every mode is safe to run again after a stop.

EXIT CODES.

    0   done (verify: ready).
    1   failed, an assertion fired, another run holds the lock, or verify says
        not ready.
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
import contextlib
import signal
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
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
# A backfill batch updates rows the live application may be writing. Waiting
# for one of its row locks longer than this means the batch is stuck behind a
# long transaction; it gives up, rolls back and retries instead of holding its
# own transaction open (which would pin dead tuples and block autovacuum).
BATCH_LOCK_TIMEOUT_MS = 10000
# Bounds a runaway batch, such as a plan that stops using the asin index. The
# whole batch rolls back and the failure surfaces, rather than one statement
# running for an unbounded time inside a transaction.
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
    # True when the 2.1.x writer cannot coexist with the index: books and
    # series have a populated region, so (asin, region) is a real second
    # unique key there and its ON CONFLICT (asin) upsert stops being safe.
    # Those are built inside finalize, with writers stopped.
    window: bool = False


# These names are the contract with the revision and models that adopt the
# indexes: the revision asserts on them and builds its keys on top of these
# indexes instead of building new ones. Each widened key is a superset of the
# existing unique, so building it can never fail on existing data.
INDEXES: tuple[IndexSpec, ...] = (
    IndexSpec("uq_books_asin_region", "books", ("asin", "region"), window=True),
    IndexSpec("uq_series_asin_region", "series", ("asin", "region"), window=True),
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

ONLINE_INDEXES: tuple[IndexSpec, ...] = tuple(i for i in INDEXES if not i.window)
WINDOW_INDEXES: tuple[IndexSpec, ...] = tuple(i for i in INDEXES if i.window)

# Session advisory lock key held for the length of any mutating run.
ADVISORY_LOCK_KEY = 0x52474B59


class FinalizeAbort(Exception):
    """An assertion failed; the database is not in the state the mode needs."""


class StopRequested(Exception):
    """A stop signal arrived while a step was waiting to retry."""


class AlreadyRunning(Exception):
    """Another run holds the advisory lock."""


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
                raise StopRequested(label) from exc
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


async def _build_index(engine: AsyncEngine, spec: IndexSpec) -> bool:
    """Builds one unique index and checks it. False when it is unusable."""
    async with engine.connect() as conn:
        for col in spec.columns:
            if await _column_info(conn, spec.table, col) is None:
                logger.error(
                    "RegionKeys: column missing, run expand first",
                    extra={"index": spec.name, "column": f"{spec.table}.{col}"},
                )
                return False
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
        return False
    logger.info("RegionKeys: index valid", extra={"index": spec.name, "table": spec.table})
    return True


async def index(engine: AsyncEngine, stop: _Stop) -> int:
    logger.info("RegionKeys: index starting", extra={"indexes": len(ONLINE_INDEXES)})
    for spec in ONLINE_INDEXES:
        if stop.requested:
            logger.info("RegionKeys: index stopped before finishing")
            return EXIT_STOPPED
        if not await _build_index(engine, spec):
            return EXIT_FAILED
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
            required = not (pre_window and spec.window)
            logger.info(
                "RegionKeys: verify index",
                extra={
                    "index": spec.name,
                    "present": state is not None,
                    "valid": bool(state and state[0]),
                    "unique": bool(state and state[1]),
                    "required": required,
                },
            )
            if not required:
                continue
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

    # Before any NOT NULL: a failed build leaves the columns nullable, which
    # is the state 2.1.x can still run against once the index is dropped.
    for spec in WINDOW_INDEXES:
        if stop.requested:
            logger.info("RegionKeys: finalize stopped before finishing")
            return EXIT_STOPPED
        if not await _build_index(engine, spec):
            raise FinalizeAbort(f"index {spec.name} could not be built")

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


# --- unfinalize --------------------------------------------------------------

async def unfinalize(engine: AsyncEngine, stop: _Stop) -> int:
    """Undoes finalize so a 2.1.x writer can run again. Writers must be stopped."""
    started = time.monotonic()
    logger.info("RegionKeys: unfinalize starting (writers must be stopped)")
    for col in FINAL_COLUMNS:
        if stop.requested:
            logger.info("RegionKeys: unfinalize stopped before finishing")
            return EXIT_STOPPED
        async with engine.connect() as conn:
            nullable = await _column_info(conn, col.table, col.column)
        if nullable is None:
            raise FinalizeAbort(f"{col.label} does not exist; nothing to roll back for it")
        # A partial finalize can leave its NOT VALID CHECK, which still
        # rejects the NULLs 2.1.x inserts.
        await _ddl(
            engine,
            f"drop check {col.label}",
            f"ALTER TABLE {col.table} DROP CONSTRAINT IF EXISTS {col.check_name}",
            stop,
        )
        await _ddl(
            engine,
            f"drop not null {col.label}",
            f"ALTER TABLE {col.table} ALTER COLUMN {col.column} DROP NOT NULL",
            stop,
        )
        logger.info("RegionKeys: column is nullable again", extra={"column": col.label})
    for spec in WINDOW_INDEXES:
        if stop.requested:
            logger.info("RegionKeys: unfinalize stopped before finishing")
            return EXIT_STOPPED
        await _ddl_concurrent(engine, f"drop {spec.name}", f"DROP INDEX CONCURRENTLY IF EXISTS {spec.name}")
    _log_elapsed("unfinalize (total)", started)
    logger.info("RegionKeys: unfinalize complete")
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

    p_unfin = sub.add_parser(
        "unfinalize", help="rollback only: undo finalize so the 2.1.x application can run again"
    )
    p_unfin.add_argument("--i-have-stopped-writers", action="store_true", dest="writers_stopped")
    return parser


@contextlib.asynccontextmanager
async def _single_run_lock(engine: AsyncEngine) -> AsyncIterator[None]:
    """Holds a session advisory lock on its own connection for the whole run."""
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})).scalar_one()
        if not got:
            raise AlreadyRunning()
        try:
            yield
        finally:
            # Closing the connection would release it too; being explicit
            # keeps the lifetime readable.
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})


async def _dispatch(args: argparse.Namespace, engine: AsyncEngine, stop: _Stop) -> int:
    if args.mode == "expand":
        return await expand(engine, stop)
    if args.mode == "backfill":
        return await backfill(engine, stop, args.batch_size)
    if args.mode == "index":
        return await index(engine, stop)
    if args.mode == "finalize":
        return await finalize(engine, stop, args.batch_size)
    if args.mode == "unfinalize":
        return await unfinalize(engine, stop)
    raise AssertionError(args.mode)


async def _run(args: argparse.Namespace, engine: AsyncEngine) -> int:
    stop = _Stop()
    _install_signal_handlers(stop)
    try:
        if args.mode == "verify":
            return await verify(engine, args.pre_window)
        async with _single_run_lock(engine):
            return await _dispatch(args, engine, stop)
    except AlreadyRunning:
        logger.error("RegionKeys: another run holds the lock, not starting", extra={"mode": args.mode})
        return EXIT_FAILED
    except StopRequested as exc:
        logger.info("RegionKeys: stopped while waiting to retry", extra={"mode": args.mode, "step": str(exc)})
        return EXIT_STOPPED
    except FinalizeAbort as exc:
        logger.error("RegionKeys: ABORTED", extra={"mode": args.mode, "reason": str(exc)})
        return EXIT_FAILED
    except Exception as exc:
        # Type and SQLSTATE only: a driver error's text can quote stored rows.
        logger.error(
            "RegionKeys: failed",
            extra={"mode": args.mode, "error_type": type(exc).__name__, "sqlstate": _sqlstate(exc)},
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
    if args.mode in ("finalize", "unfinalize") and not args.writers_stopped:
        logger.error(
            "RegionKeys: refused, --i-have-stopped-writers not given", extra={"mode": args.mode}
        )
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
