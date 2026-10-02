# Standard library
import atexit
import datetime
import logging
import logging.handlers
import os
import queue
import sys
import time

# Local
from app.core.config import get_settings

# Standard library - optional, Unix only. Grouped with the other conditional
# imports rather than above, so the unconditional imports stay at the top.
fcntl = None

try:
    import fcntl  # type: ignore
except ImportError:
    pass

# Third party - optional
Client = None
AXIOM_AVAILABLE = False

try:
    from axiom_py import Client  # type: ignore
    AXIOM_AVAILABLE = True
except ImportError:
    pass


# Standard LogRecord attributes — anything on a record that isn't one of these
# was passed in via `extra=` and is worth surfacing in the logs.
#
# 'asctime' is listed even though a LogRecord is not born with it. Formatter
# .format() attaches it to the record while rendering, before ContextFormatter
# gets to read the extras, so it arrives looking exactly like a field a caller
# passed in. Left out, every line carrying any extra ends with a second copy of
# the timestamp it already starts with, and every Axiom event gains a string
# duplicating its own _time.
_STANDARD_RECORD_FIELDS = {
    'name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 'filename',
    'module', 'exc_info', 'exc_text', 'stack_info', 'lineno', 'funcName',
    'created', 'msecs', 'relativeCreated', 'thread', 'threadName',
    'processName', 'process', 'message', 'taskName', 'asctime',
}


def _extra_fields(record: logging.LogRecord) -> dict:
    """Returns the structured fields attached to a record via `extra=`."""
    return {
        k: v for k, v in record.__dict__.items()
        if k not in _STANDARD_RECORD_FIELDS
    }


class ContextFormatter(logging.Formatter):
    """
    Formats log lines with their structured context appended in a readable way.

    A line logged with extra fields, e.g.
        logger.info("Scan complete", extra={"found": 1000, "new": 0})
    renders as
        2026-06-21 18:39:39 - libex - INFO - Scan complete (found: 1000, new: 0)

    Lines with no extra fields render normally, so ordinary messages stay clean.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = _extra_fields(record)
        if not extras:
            return base
        context = ", ".join(f"{k}: {v}" for k, v in extras.items())
        return f"{base} ({context})"


class MaxLevelFilter(logging.Filter):
    """Allows only records at or below a maximum level (used to keep
    warnings and errors off stdout so they can go to stderr instead)."""

    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def _rotation_lock_path(base_filename: str) -> str:
    """
    Returns the lock file guarding rotation of base_filename.

    Deliberately not base_filename + a suffix. TimedRotatingFileHandler's
    getFilesToDelete() collects every name beginning with "libex.log." and
    keeps the ones whose remainder parses as its date suffix, so a lock file
    sharing that prefix sits inside the retention scan and relies on never
    matching the date pattern to escape being counted or deleted. A leading
    dot puts it outside the prefix entirely, under any `when` setting.
    """
    directory, name = os.path.split(base_filename)
    return os.path.join(directory, f".{name}.rotate.lock")


class MultiProcessTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """
    A TimedRotatingFileHandler that survives more than one process writing to
    the same file.

    The stock handler does not, and it fails silently. Each process reaches
    the rollover time on its own clock and renames the log out from under the
    others. The first wins; every later one hits the "already rolled over"
    early return in doRollover(), which leaves it holding an open descriptor
    on the file that was just renamed and leaves its rolloverAt in the past.
    It carries on appending to the previous day's file. When retention later
    deletes that file, the descriptor stays valid and the writes land on an
    inode nothing can open again, so the lines are simply gone. Nothing
    raises, and nothing appears on any stream.

    Measured against the stock handler: two processes logging 40 lines each
    against backupCount=1 ended with 49 of the 80 unrecoverable and no error
    reported anywhere.

    Two things fix it. Rotation is serialised across processes with an flock,
    so exactly one process performs the rename; afterwards every process
    reopens the base name and repairs its rolloverAt, so the ones that did not
    rotate return to the live file instead of the renamed one.

    Only rotation takes the lock, never emit(). Records are appended to a file
    opened O_APPEND, where the kernel already makes each write atomic, and
    locking per record would put a syscall pair in front of every line on the
    caller's thread -- which for this application is the event loop itself.
    """

    def doRollover(self) -> None:
        lock_fd = None
        if fcntl is not None:
            try:
                lock_fd = os.open(
                    _rotation_lock_path(self.baseFilename),
                    os.O_CREAT | os.O_RDWR,
                    0o644,
                )
            except OSError:
                # Nothing to escalate: without the lock this is exactly as
                # safe as the stock handler, which is what would have run
                # here anyway. The reopen below still recovers the common case.
                lock_fd = None
        try:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            super().doRollover()
            self._reclaim_base_file()
        finally:
            if lock_fd is not None:
                # flock is released by the kernel when the descriptor closes,
                # so a process killed mid-rotation cannot strand the lock.
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def _reclaim_base_file(self) -> None:
        """
        Puts this handler back onto the live log file after a rollover.

        Unconditional, because from here the two cases are indistinguishable
        and the cost is one extra open per rotation: the process that rotated
        is already on the new file, the process that did not is on the renamed
        one, and reopening both is cheaper than working out which this is.

        rolloverAt is repaired for a sharper reason. The early return skips
        the recomputation, leaving the time in the past, and shouldRollover()
        would then fire on every subsequent record -- turning a once-a-day
        lock into one taken per log line.
        """
        if not self.delay:
            if self.stream:
                self.stream.close()
            self.stream = self._open()
        now = int(time.time())
        if self.rolloverAt <= now:
            self.rolloverAt = self.computeRollover(now)


# axiom_py's sync Client (0.10.0) has no timeout argument anywhere in its
# constructor or in ingest_events — checked directly against the installed
# package, not assumed. Its session is a plain requests Session, so a default
# is applied to that instead (see _apply_default_timeout). This bounds the
# one background thread that ever calls it; it does not need to be long,
# since a slow-but-alive endpoint and a hung one look the same to a caller
# that only cares about not blocking forever.
_AXIOM_TIMEOUT_SECONDS = 5.0

# DirectAxiomHandler only ever runs on a QueueListener's own thread (see
# _start_axiom_listener), never on the caller's. This bounds how many queued
# records can pile up while that thread is blocked inside a single
# ingest_events call (up to _AXIOM_TIMEOUT_SECONDS) before new records start
# being dropped instead of queued. Each event is a small JSON dict — a
# message plus a handful of extra fields — so even a full queue is at most a
# few MB resident, and it drains in a fraction of a second once the blocked
# call returns (or once the breaker below opens and emit becomes a no-op).
_AXIOM_QUEUE_MAXSIZE = 2000


def _apply_default_timeout(client) -> None:
    """
    Wraps client.session.request so any call through it that doesn't already
    carry an explicit timeout gets _AXIOM_TIMEOUT_SECONDS instead. This is
    the only timeout knob axiom_py's Client exposes — there is no
    constructor argument and no way to inject a pre-configured
    transport, only the already-built `.session` attribute to adjust after
    the fact.
    """
    session = getattr(client, "session", None)
    if session is None:
        return
    original_request = session.request

    def _request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", _AXIOM_TIMEOUT_SECONDS)
        return original_request(method, url, **kwargs)

    session.request = _request_with_timeout


class DirectAxiomHandler(logging.Handler):
    """
    Ships a record to Axiom via a synchronous, blocking ingest_events call.

    Never attach this to a logger directly — it is only ever driven by a
    QueueListener on its own background thread (see _start_axiom_listener),
    so the network call here never runs on the thread that called
    logger.info(...). On the API's single event loop, that caller is the
    loop itself, and the whole point of the queue is that it must never be
    the one blocked on Axiom.
    """

    def __init__(self, client, dataset):
        super().__init__()
        self.client = client
        self.dataset = dataset
        self._ingest_failed = False

    def emit(self, record):
        if self._ingest_failed:
            # The circuit is open and stays open for the life of the process:
            # a transient blip and a dead endpoint look identical from here,
            # and retrying with a backoff is meaningfully more state for a
            # best-effort side channel whose loss degrades nothing else —
            # stdout, stderr, and the file handler are all unaffected. Log
            # shipping comes back on the next deploy.
            return
        try:
            event = {
                "_time": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
                "level": record.levelname,
                "message": record.getMessage(),
                "logger": record.name,
                # Which worker emitted this. Axiom is where these records are
                # actually queried, so without it a multi-worker deployment
                # cannot tell one process repeatedly in trouble from several
                # occasionally in trouble -- the two look identical once the
                # lines are merged into one dataset.
                "pid": record.process,
                **_extra_fields(record),
            }
            self.client.ingest_events(dataset=self.dataset, events=[event])
        except Exception as e:
            # Shipping logs to Axiom is best-effort. Warn once, straight to
            # stderr (never through the logger, which would recurse back into
            # this handler), then open the circuit above so ingest_events is
            # never called again this process.
            self._ingest_failed = True
            print(
                f"Axiom log shipping failed ({e}); disabling further Axiom "
                "ingestion for this process. Other log handlers are unaffected.",
                file=sys.stderr,
            )


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    """
    A record that can't be queued without blocking is dropped instead of
    waiting — the same trade the network call itself makes: nothing here may
    slow the caller down. The base QueueHandler already uses put_nowait, but
    its default error path (handleError) prints a full traceback per dropped
    record, which is its own flood under sustained overload; a full queue is
    treated as a normal, silent drop instead.
    """

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass


_axiom_listener: logging.handlers.QueueListener | None = None


def _start_axiom_listener(direct_handler: DirectAxiomHandler) -> logging.Handler:
    """
    Starts a QueueListener that owns direct_handler on its own background
    thread, and returns the (non-blocking) handler the logger should attach
    in its place.

    Registered with atexit rather than the app lifespan: this module has no
    reference to the FastAPI app, and reaching for one would be a layering
    inversion for a logging module. atexit gives the same guarantee a
    lifespan shutdown hook would — the listener is stopped and its queue
    drained on normal interpreter exit — without one. stop_axiom_listener()
    is also exposed directly for a caller (tests, or a future lifespan hook)
    that wants a deterministic shutdown point instead of waiting for exit.
    """
    global _axiom_listener
    record_queue: queue.Queue = queue.Queue(maxsize=_AXIOM_QUEUE_MAXSIZE)
    listener = logging.handlers.QueueListener(record_queue, direct_handler)
    listener.start()
    _axiom_listener = listener
    atexit.register(listener.stop)
    return _DroppingQueueHandler(record_queue)


def stop_axiom_listener() -> None:
    """Stops the Axiom queue listener, if one was started, flushing any
    queued records to the (real or mocked) Axiom client first. Also drops the
    atexit hook registered in _start_axiom_listener — QueueListener.stop() is
    not itself idempotent (it joins and clears its worker thread), so leaving
    the hook registered would crash atexit on process exit after an already
    -stopped listener."""
    global _axiom_listener
    if _axiom_listener is not None:
        atexit.unregister(_axiom_listener.stop)
        _axiom_listener.stop()
        _axiom_listener = None


def _resolve_level(settings) -> int:
    """
    Resolves the log level. LOG_LEVEL takes precedence; DEBUG mode forces
    DEBUG; otherwise INFO. An unrecognized LOG_LEVEL falls back to INFO.
    """
    if settings.debug:
        return logging.DEBUG
    level = logging.getLevelName((settings.log_level or "INFO").upper())
    return level if isinstance(level, int) else logging.INFO


# httpx logs "HTTP Request: GET <url>" at INFO, with the full URL and its query
# string. Libex forwards a caller's search text to Audible in exactly those
# query strings, so at INFO that logger prints the keywords, title, author and
# narrator someone typed -- the content the rest of this codebase goes to some
# length never to record. httpcore is muted with it: it logs connection and
# stream trace at DEBUG, which turns on with LOG_LEVEL=DEBUG and is noise Libex
# has no use for either way.
#
# Muted explicitly because the alternative is luck. Nothing today attaches a
# handler to the root logger or calls basicConfig, so those records are
# currently discarded at the root's default WARNING -- an accident of
# configuration that ends the moment anyone adds a root handler or passes
# --log-config to uvicorn, with no sign that it did. A level set on the logger
# itself does not depend on who else configures logging.
_MUTED_THIRD_PARTY_LOGGERS = ("httpx", "httpcore")


def setup_logging() -> logging.Logger:
    settings = get_settings()

    for name in _MUTED_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    logger = logging.getLogger("libex")
    logger.setLevel(_resolve_level(settings))

    if logger.handlers:
        return logger

    # The pid, not the process name: it is the identifier every other view of
    # this server reports -- ps, the container logs, and uvicorn's own
    # "Started server process [N]" line -- so a log line here cross-references
    # against all three directly. processName does tell uvicorn's workers apart
    # (they arrive as SpawnProcess-1, SpawnProcess-2, ...), but that ordinal is
    # bookkeeping internal to one process tree, and nothing outside it reports
    # the same value to match against.
    #
    # It describes the server process and never the caller. The value is
    # identical for every request a given worker handles and changes only when
    # the process restarts, so it says nothing about who sent anything.
    formatter = ContextFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - pid=%(process)d - %(message)s"
    )

    # Stdout handler — INFO and below (DEBUG, INFO). Warnings and errors are
    # routed to stderr instead so they can be filtered separately.
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(MaxLevelFilter(logging.INFO))
    logger.addHandler(stdout_handler)

    # Stderr handler — WARNING and above.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(logging.WARNING)
    logger.addHandler(stderr_handler)

    # File handler — writes to /app/logs/libex.log
    # Skipped gracefully if the directory cannot be created (e.g. CI, non-Docker environments).
    log_dir = "/app/logs"
    try:
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, "libex.log")

        if settings.log_retention_days == 0:
            # No rotation to coordinate, so the plain handler is already
            # multi-process safe here: every process appends to one never
            # renamed file, and O_APPEND makes each write atomic.
            file_handler: logging.Handler = logging.FileHandler(log_file)
        else:
            file_handler = MultiProcessTimedRotatingFileHandler(
                log_file,
                when="midnight",
                backupCount=settings.log_retention_days,
            )

        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as e:
        logger.warning(f"File logging unavailable, skipping: {e}")

    # Axiom handler (optional). The logger only ever gets the queue-backed
    # handler below — DirectAxiomHandler itself runs on the listener's
    # background thread, never on the caller's.
    if AXIOM_AVAILABLE and Client and settings.axiom_token and settings.axiom_dataset:
        try:
            client = Client(token=settings.axiom_token)
            _apply_default_timeout(client)
            direct_handler = DirectAxiomHandler(client=client, dataset=settings.axiom_dataset)
            logger.addHandler(_start_axiom_listener(direct_handler))
            logger.info("Axiom logging enabled")
        except Exception as e:
            logger.warning(f"Axiom logging failed to initialize: {e}")

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("libex")