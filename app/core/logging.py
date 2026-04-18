# Standard library
import datetime
import logging
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


class DirectAxiomHandler(logging.Handler):
    def __init__(self, client, dataset):
        super().__init__()
        self.client = client
        self.dataset = dataset

    def emit(self, record):
        try:
            extra_fields = {
                k: v for k, v in record.__dict__.items()
                if k not in {
                    'name', 'msg', 'args', 'levelname', 'levelno', 'pathname',
                    'filename', 'module', 'exc_info', 'exc_text', 'stack_info',
                    'lineno', 'funcName', 'created', 'msecs', 'relativeCreated',
                    'thread', 'threadName', 'processName', 'process', 'message',
                    'taskName'
                }
            }
            event = {
                "_time": datetime.datetime.utcnow().isoformat() + "Z",
                "level": record.levelname,
                "message": record.getMessage(),
                "logger": record.name,
                **extra_fields,
            }
            self.client.ingest_events(dataset=self.dataset, events=[event])
        except Exception:
            self.handleError(record)


def setup_logging() -> logging.Logger:
    settings = get_settings()

    logger = logging.getLogger("libex")
    logger.setLevel(logging.DEBUG if settings.debug else logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

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