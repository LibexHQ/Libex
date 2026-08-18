"""
Background persist queue unit tests.

Everything here is about the machinery around a write rather than the write
itself: which failures are worth another attempt, what a failure is allowed to
say about itself in a log, what is held while a backoff sleeps, and what stops
an unbounded backlog or a collected-mid-flight task from losing work silently.

None of it touches a database. What each attempt actually writes is proved
against real Postgres in tests/integration/test_persist_book_chunk.py.
"""

# Standard library
import asyncio
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from asyncpg.exceptions import (
    DeadlockDetectedError,
    SerializationError,
    UniqueViolationError,
)
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

# Local
from app.services.db import persist_queue as pq


REGION = "us"


def _books(count):
    return [{"asin": f"B0BOOK{i:05d}", "title": f"Book {i}"} for i in range(count)]


def _dbapi_error(orig):
    """A DBAPIError wrapping the driver exception the classifier reads."""
    return DBAPIError("SELECT 1", {}, orig)


class _FakeSessionCM:
    """
    Stands in for `_BackgroundSession()`, recording whether a session is
    currently open so a test can assert what a backoff is holding.
    """

    def __init__(self, ledger):
        self._ledger = ledger

    async def __aenter__(self):
        self._ledger["open"] += 1
        session = AsyncMock()
        session.rollback = AsyncMock()
        return session

    async def __aexit__(self, *exc_info):
        self._ledger["open"] -= 1
        return False


@pytest.fixture(autouse=True)
def reset_queue_state():
    """
    The backlog counter and the shed window are module state that outlives a
    test. Reset around every one so an earlier test's shed cannot silence a
    later test's window, or its backlog shed a batch that should be admitted.
    """
    pq._queued_books = 0
    pq._shed_books = 0
    pq._shed_events = 0
    pq._shed_last_logged = 0.0
    pq._inflight.clear()
    yield
    pq._queued_books = 0
    pq._shed_books = 0
    pq._shed_events = 0
    pq._shed_last_logged = 0.0
    pq._inflight.clear()


# ============================================================
# RETRY CLASSIFICATION
# ============================================================

def test_pool_exhaustion_is_retryable():
    """The condition this layer most exists to survive: every connection is
    checked out and a fresh attempt a moment later may well get one."""
    assert pq._is_retryable(PoolTimeoutError("pool")) is True


def test_pool_exhaustion_is_not_a_dbapi_error():
    """Why the classifier has to name it separately. sqlalchemy.exc.TimeoutError
    subclasses SQLAlchemyError directly, so a classifier written as
    `except (OperationalError, InterfaceError)` — the obvious shape, and the
    one that looks complete — misses pool exhaustion entirely."""
    assert isinstance(PoolTimeoutError("pool"), SQLAlchemyError)
    assert not isinstance(PoolTimeoutError("pool"), DBAPIError)


def test_a_serialization_failure_is_retryable():
    """40001. The transaction lost a race it can win on a second run."""
    assert pq._is_retryable(_dbapi_error(SerializationError("40001"))) is True


def test_a_deadlock_is_retryable():
    """40P01. Same class of failure, discriminated by the same base."""
    assert pq._is_retryable(_dbapi_error(DeadlockDetectedError("40P01"))) is True


def test_both_contention_failures_share_the_base_the_classifier_matches():
    """The classifier matches asyncpg's TransactionRollbackError rather than
    enumerating SQLSTATE strings, so a sibling condition added upstream is
    covered without an edit. This pins that the base really is common to both,
    since the whole approach rests on it."""
    from asyncpg.exceptions import TransactionRollbackError

    assert issubclass(SerializationError, TransactionRollbackError)
    assert issubclass(DeadlockDetectedError, TransactionRollbackError)


def test_an_invalidated_connection_is_retryable():
    """The connection died under the statement rather than the statement being
    wrong, and a new session takes a new connection."""
    failure = OperationalError("SELECT 1", {}, Exception("connection is closed"))
    failure.connection_invalidated = True
    assert pq._is_retryable(failure) is True


def test_a_constraint_violation_is_not_retryable():
    """Data the schema refuses fails identically on every attempt. Retrying it
    only delays the per-book replay, which is the only thing that can isolate
    the one book at fault."""
    assert pq._is_retryable(IntegrityError("INSERT", {}, UniqueViolationError("dupe"))) is False


def test_an_unrelated_exception_is_not_retryable():
    """A bug in the writer is not a transient condition."""
    assert pq._is_retryable(ValueError("nope")) is False


# ============================================================
# WHAT A FAILURE MAY SAY ABOUT ITSELF
# ============================================================

def test_failure_fields_report_classification_and_schema_metadata_only():
    """Classification and the schema object involved, never the exception."""
    fields = pq._failure_fields(_dbapi_error(SerializationError("40001")))
    assert set(fields) == {
        "error_type", "sqlstate", "schema_name", "table_name",
        "column_name", "constraint_name",
    }
    assert fields["error_type"] == "DBAPIError"


def test_failure_fields_carry_no_part_of_the_message():
    """Postgres puts the offending row into its own message text — a not-null
    violation reports "Failing row contains (...)" with every column in it — so
    str(exc) publishes book data into the logs whatever hide_parameters is set
    to. Nothing derived from the message may appear."""
    orig = UniqueViolationError("Failing row contains (B0SECRET, A Private Title)")
    fields = pq._failure_fields(IntegrityError("INSERT", {"title": "A Private Title"}, orig))

    rendered = " ".join(str(v) for v in fields.values())
    assert "Failing row" not in rendered
    assert "A Private Title" not in rendered
    assert "B0SECRET" not in rendered


def test_failure_fields_surface_the_schema_object_the_driver_named():
    """The widened fields carry real values when the driver supplies them, not
    just a wider key set that happens to read None everywhere. schema_name,
    table_name and column_name are what let a dashboard say which write is
    breaking without ever touching the row content that named it."""
    from asyncpg.exceptions import NotNullViolationError

    orig = NotNullViolationError("null value in column")
    orig.schema_name = "public"
    orig.table_name = "books"
    orig.column_name = "region"
    orig.constraint_name = None

    fields = pq._failure_fields(IntegrityError("INSERT", {}, orig))

    assert fields["schema_name"] == "public"
    assert fields["table_name"] == "books"
    assert fields["column_name"] == "region"


def test_failure_fields_survive_an_exception_with_no_driver_original():
    """A pool timeout has no `orig` at all, and the classifier must still be
    able to log it — this is the path a naive getattr chain would raise on."""
    assert pq._failure_fields(PoolTimeoutError("pool")) == {
        "error_type": "TimeoutError", "sqlstate": None,
        "schema_name": None, "table_name": None,
        "column_name": None, "constraint_name": None,
    }


# ============================================================
# THE RETRY LOOP
# ============================================================

async def _run_chunk(outcomes, chunk=None):
    """
    Drives _persist_book_chunk_background with _write_book_chunk stubbed to
    raise (or not) per attempt, and records what was held at each sleep.

    Returns the attempts made, the sleeps taken, whether the per-book replay
    ran, and how many sessions were open at each sleep.
    """
    chunk = chunk if chunk is not None else _books(3)
    ledger = {"open": 0}
    attempts = []
    sleeps = []
    open_during_sleep = []
    replayed = []

    async def _write(session, books, region):
        outcome = outcomes[len(attempts)]
        attempts.append(region)
        if outcome is not None:
            raise outcome

    async def _sleep(seconds):
        sleeps.append(seconds)
        open_during_sleep.append(ledger["open"])

    async def _replay(session, books, region):
        replayed.append(list(books))

    with patch.object(pq, "_BackgroundSession", lambda: _FakeSessionCM(ledger)), \
         patch.object(pq, "_write_book_chunk", side_effect=_write), \
         patch.object(pq, "_replay_book_chunk", side_effect=_replay), \
         patch.object(pq.asyncio, "sleep", side_effect=_sleep):
        await pq._persist_book_chunk_background(chunk, REGION)

    return attempts, sleeps, replayed, open_during_sleep


@pytest.mark.asyncio
async def test_a_chunk_that_writes_first_time_neither_retries_nor_replays():
    """The path taken almost every time costs exactly one attempt."""
    attempts, sleeps, replayed, _ = await _run_chunk([None])

    assert len(attempts) == 1
    assert sleeps == []
    assert replayed == []


@pytest.mark.asyncio
async def test_a_contended_chunk_is_retried_and_can_still_succeed():
    """A deadlock against the seeder is the ordinary case this exists for: the
    second attempt writes the whole chunk and the per-book replay never runs."""
    attempts, sleeps, replayed, _ = await _run_chunk(
        [_dbapi_error(DeadlockDetectedError("40P01")), None]
    )

    assert len(attempts) == 2
    assert len(sleeps) == 1
    assert replayed == []


@pytest.mark.asyncio
async def test_a_chunk_contended_every_time_falls_back_to_the_per_book_replay():
    """Retrying is bounded, and what bounds it is the attempt count — nothing
    else limits how long a retrying transaction's work stays in flight, since
    statement_timeout bounds a statement and a retrying transaction is never
    idle. When the attempts run out the chunk still gets written, a book at a
    time, so contention costs throughput and never data."""
    contended = _dbapi_error(SerializationError("40001"))
    attempts, sleeps, replayed, _ = await _run_chunk([contended, contended, contended])

    assert len(attempts) == pq._PERSIST_MAX_ATTEMPTS
    assert len(sleeps) == pq._PERSIST_MAX_ATTEMPTS - 1
    assert len(replayed) == 1


@pytest.mark.asyncio
async def test_a_non_retryable_failure_replays_immediately_without_sleeping():
    """One book carrying data the schema rejects fails identically on every
    attempt. Retrying it delays the replay that can isolate it and holds a
    connection while doing so."""
    attempts, sleeps, replayed, _ = await _run_chunk(
        [IntegrityError("INSERT", {}, UniqueViolationError("dupe"))]
    )

    assert len(attempts) == 1
    assert sleeps == []
    assert len(replayed) == 1


@pytest.mark.asyncio
async def test_the_backoff_holds_no_session_open():
    """Sleeping inside an open transaction is what makes a retry worse than no
    retry: past idle_in_transaction_session_timeout the server terminates the
    backend and the next statement fails on a closed connection, and until then
    the sleep is holding a pooled connection and the chunk's row locks — which
    guarantees the retry re-collides with whoever it collided with first."""
    contended = _dbapi_error(DeadlockDetectedError("40P01"))
    _, sleeps, _, open_during_sleep = await _run_chunk([contended, contended, contended])

    assert sleeps, "no backoff was taken, so this asserts nothing"
    assert open_during_sleep == [0] * len(sleeps)


@pytest.mark.asyncio
async def test_the_backoff_grows_between_attempts():
    """Backing off by the same amount twice is barely backing off at all when
    the contention is six workers arriving together.

    The jitter is pinned so this asserts the nominal doubling rather than one
    pair of draws. Left unpinned it fails about once every sixteen runs, and
    correctly so: attempt 1 spans [0.125, 0.375) and attempt 2 spans
    [0.250, 0.750), so the ranges overlap and a high first draw really can
    exceed a low second one. That overlap is the jitter doing its job rather
    than a defect -- test_the_backoff_is_jittered covers it from the other
    side -- so the ordering of two particular draws is not a property the
    implementation offers, and asserting it made this test flaky without
    making it capable of catching anything."""
    contended = _dbapi_error(SerializationError("40001"))
    with patch.object(pq.random, "random", return_value=0.5):
        _, sleeps, _, _ = await _run_chunk([contended, contended, contended])

    assert sleeps[1] == 2 * sleeps[0]


def test_the_backoff_is_jittered():
    """Six worker processes retrying a deadlock in lockstep re-collide on the
    same rows. Arriving at a different time from whoever won is the entire
    point of waiting, so two calls for the same attempt must differ."""
    assert len({pq._backoff_seconds(1) for _ in range(20)}) > 1


# ============================================================
# THE TASK REGISTRY
# ============================================================

@pytest.mark.asyncio
async def test_a_running_task_is_held_by_a_strong_reference():
    """asyncio holds only a weak reference to a running task, so a fire-and-
    forget task nothing else refers to can be garbage collected mid-await and
    simply stop, with no error raised anywhere. Retries make that worse rather
    than introducing it: a task now lives across up to three attempts and two
    backoffs instead of one round trip."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _work():
        started.set()
        await release.wait()

    pq._spawn(_work, 1)
    await started.wait()

    assert len(pq._inflight) == 1

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_finished_task_is_dropped_from_the_registry():
    """Holding strong references is only safe if they are released — a registry
    that only grows is the leak it was added to prevent, wearing a hat."""
    async def _work():
        return None

    pq._spawn(_work, 1)
    for _ in range(4):
        await asyncio.sleep(0)

    assert pq._inflight == set()


# ============================================================
# THE BOUNDED BACKLOG
# ============================================================

@pytest.mark.asyncio
async def test_a_batch_within_the_backlog_is_admitted():
    """The bound is a ceiling for a degraded database, not a throttle on normal
    traffic — an ordinary batch must never be shed."""
    ran = asyncio.Event()

    async def _work():
        ran.set()

    pq._spawn(_work, 50)
    await ran.wait()

    assert ran.is_set()


@pytest.mark.asyncio
async def test_a_batch_past_the_backlog_is_shed_without_starting_a_task():
    """When Postgres is degraded and the queue would otherwise grow without
    limit while every response still succeeds, memory exhaustion takes the
    whole worker down. Shedding costs some books their write; they persist on
    the next request that asks for them."""
    ran = []

    async def _work():
        ran.append(True)

    pq._queued_books = pq._PERSIST_BACKLOG_MAX_BOOKS
    pq._spawn(_work, 1)
    await asyncio.sleep(0)

    assert ran == []
    assert pq._inflight == set()


@pytest.mark.asyncio
async def test_a_shed_batch_leaves_no_coroutine_un_awaited():
    """_spawn takes a coroutine function rather than a coroutine precisely so a
    shed batch has nothing to abandon. Passing a coroutine and dropping it
    raises RuntimeWarning: coroutine was never awaited — noise in the logs
    during the exact incident the shedding is reporting."""
    made = []

    async def _work():
        made.append(True)

    pq._queued_books = pq._PERSIST_BACKLOG_MAX_BOOKS
    with patch.object(pq.asyncio, "create_task") as create_task:
        pq._spawn(_work, 1)

    create_task.assert_not_called()
    assert made == []


@pytest.mark.asyncio
async def test_the_backlog_is_released_when_the_task_finishes():
    """A reservation that is never released turns the ceiling into a permanent
    floor, and the queue sheds everything forever afterwards."""
    async def _work():
        return None

    pq._spawn(_work, 50)
    for _ in range(4):
        await asyncio.sleep(0)

    assert pq._queued_books == 0


@pytest.mark.asyncio
async def test_the_backlog_is_released_even_when_the_task_raises():
    """The failure path is the one that matters: a queue that only leaks its
    reservation when something goes wrong sheds progressively harder for the
    rest of the process's life, starting from the incident."""
    async def _work():
        raise RuntimeError("boom")

    pq._spawn(_work, 50)
    for _ in range(4):
        await asyncio.sleep(0)

    assert pq._queued_books == 0


def test_the_first_shed_is_reported_at_once():
    """Shedding starting is the news. Waiting out a window before saying so
    would hide the beginning of the incident, which is the part that dates it."""
    with patch.object(pq.logger, "warning") as warning:
        pq._record_shed(10)

    warning.assert_called_once()


def test_shedding_is_reported_once_per_window_not_once_per_batch():
    """The log pipeline drops silently once its queue passes 2000, so a warning
    per shed batch during the incident that causes shedding floods out the
    record of the incident it is reporting — including its own first line."""
    with patch.object(pq.logger, "warning") as warning:
        for _ in range(50):
            pq._record_shed(10)

    assert warning.call_count == 1


def test_the_next_report_carries_everything_the_window_swallowed():
    """One line per window is only enough if the line that ends the window
    accounts for every batch the window silenced, not just the one that
    happened to arrive after the clock ran out."""
    clock = [1000.0]
    with patch.object(pq.time, "monotonic", side_effect=lambda: clock[0]), \
         patch.object(pq.logger, "warning") as warning:
        pq._record_shed(10)                     # opens the window, reported at once
        for _ in range(5):
            pq._record_shed(10)                 # silenced by the window
        clock[0] += pq._SHED_LOG_INTERVAL_SECONDS + 1
        pq._record_shed(10)                     # window elapsed: reports the rest

    assert warning.call_count == 2
    assert warning.call_args.kwargs["extra"]["shed_books"] == 60
    assert warning.call_args.kwargs["extra"]["shed_batches"] == 6


def test_the_window_starts_again_from_zero_after_a_report():
    """A running total that is never reset makes every later line a cumulative
    figure that reads as a spike, so the second incident always looks worse
    than the first however small it was."""
    clock = [1000.0]
    with patch.object(pq.time, "monotonic", side_effect=lambda: clock[0]), \
         patch.object(pq.logger, "warning") as warning:
        pq._record_shed(10)
        clock[0] += pq._SHED_LOG_INTERVAL_SECONDS + 1
        pq._record_shed(7)

    assert warning.call_args.kwargs["extra"]["shed_books"] == 7


def test_the_windowed_report_names_no_book():
    """Which books were shed is not recoverable information anyway, so the
    report carries counts and nothing that identifies a book or a caller."""
    with patch.object(pq.logger, "warning") as warning:
        pq._record_shed(10)

    assert set(warning.call_args.kwargs["extra"]) == {
        "shed_books", "shed_batches", "queued_books", "backlog_limit",
    }


# ============================================================
# THE PER-LOOP SEMAPHORE
# ============================================================

@pytest.mark.asyncio
async def test_the_semaphore_is_reused_within_one_loop():
    """A long-lived process builds it once and keeps it, or the limit it exists
    to impose would reset on every acquire."""
    assert pq._get_bg_write_semaphore() is pq._get_bg_write_semaphore()


def test_the_semaphore_is_rebuilt_for_a_new_loop():
    """asyncio.Semaphore binds its waiter state to whichever loop first touches
    it, and a semaphore holding waiters queued against a closed loop does not
    warn — it hangs. The retry path is the first thing here that can put a
    waiter on the queue at all, which is what makes this load-bearing rather
    than tidy."""
    first = asyncio.run(_semaphore_in_this_loop())
    second = asyncio.run(_semaphore_in_this_loop())

    assert first is not second


async def _semaphore_in_this_loop():
    return pq._get_bg_write_semaphore()


@pytest.mark.asyncio
async def test_the_semaphore_admits_exactly_two_writers():
    """The limit itself, so a rebuild cannot quietly widen it: background
    writes must not starve the API's connection pool."""
    semaphore = pq._get_bg_write_semaphore()

    assert semaphore._value == 2


# ============================================================
# CHUNK SLICING FEEDS THE BACKLOG THE RIGHT COUNT
# ============================================================

@pytest.mark.asyncio
async def test_the_backlog_counts_books_not_tasks():
    """A task is anything from one book to a whole prolific catalog, so a bound
    measured in tasks tells you nothing about what is actually being held."""
    captured = {}

    def _fake_spawn(make_coro, books):
        captured["books"] = books
        make_coro().close()

    with patch.object(pq, "_spawn", side_effect=_fake_spawn):
        pq.persist_books_background(_books(137), REGION)

    assert captured["books"] == 137


@pytest.mark.asyncio
async def test_books_without_an_asin_are_not_counted_against_the_backlog():
    """They are dropped before the slicing, so reserving room for them would
    shed real books to hold space for ones that will never be written."""
    captured = {}

    def _fake_spawn(make_coro, books):
        captured["books"] = books
        make_coro().close()

    with patch.object(pq, "_spawn", side_effect=_fake_spawn):
        pq.persist_books_background(_books(3) + [{"title": "No ASIN"}], REGION)

    assert captured["books"] == 3
