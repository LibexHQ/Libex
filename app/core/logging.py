# Standard library
import datetime
import logging
import logging.handlers
import os
import sys

# Local
from app.core.config import get_settings

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
_STANDARD_RECORD_FIELDS = {
    'name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 'filename',
    'module', 'exc_info', 'exc_text', 'stack_info', 'lineno', 'funcName',
    'created', 'msecs', 'relativeCreated', 'thread', 'threadName',
    'processName', 'process', 'message', 'taskName',
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


class DirectAxiomHandler(logging.Handler):
    def __init__(self, client, dataset):
        super().__init__()
        self.client = client
        self.dataset = dataset

    def emit(self, record):
        try:
            event = {
                "_time": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
                "level": record.levelname,
                "message": record.getMessage(),
                "logger": record.name,
                **_extra_fields(record),
            }
            self.client.ingest_events(dataset=self.dataset, events=[event])
        except Exception:
            self.handleError(record)


def _resolve_level(settings) -> int:
    """
    Resolves the log level. LOG_LEVEL takes precedence; DEBUG mode forces
    DEBUG; otherwise INFO. An unrecognized LOG_LEVEL falls back to INFO.
    """
    if settings.debug:
        return logging.DEBUG
    level = logging.getLevelName((settings.log_level or "INFO").upper())
    return level if isinstance(level, int) else logging.INFO


def setup_logging() -> logging.Logger:
    settings = get_settings()

    logger = logging.getLogger("libex")
    logger.setLevel(_resolve_level(settings))

    if logger.handlers:
        return logger

    formatter = ContextFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
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
            file_handler: logging.Handler = logging.FileHandler(log_file)
        else:
            file_handler = logging.handlers.TimedRotatingFileHandler(
                log_file,
                when="midnight",
                backupCount=settings.log_retention_days,
            )

        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as e:
        logger.warning(f"File logging unavailable, skipping: {e}")

    # Axiom handler (optional)
    if AXIOM_AVAILABLE and Client and settings.axiom_token and settings.axiom_dataset:
        try:
            client = Client(token=settings.axiom_token)
            axiom_handler = DirectAxiomHandler(client=client, dataset=settings.axiom_dataset)
            logger.addHandler(axiom_handler)
            logger.info("Axiom logging enabled")
        except Exception as e:
            logger.warning(f"Axiom logging failed to initialize: {e}")

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("libex")