# Standard library
import logging
import sys

# Local
from app.core.config import get_settings

# Third party - optional
AxiomHandler = None
Client = None
AXIOM_AVAILABLE = False

try:
    from axiom_py import AxiomHandler, Client  # type: ignore
    AXIOM_AVAILABLE = True
except ImportError:
    pass

JsonFormatter = None

try:
    from pythonjsonlogger.json import JsonFormatter  # type: ignore
    JSON_LOGGER_AVAILABLE = True
except ImportError:
    JSON_LOGGER_AVAILABLE = False


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

    if AXIOM_AVAILABLE and Client and AxiomHandler and settings.axiom_token and settings.axiom_dataset:
        try:
            client = Client(token=settings.axiom_token)
            axiom_handler = AxiomHandler(
                client=client,
                dataset=settings.axiom_dataset,
            )
            if JSON_LOGGER_AVAILABLE and JsonFormatter:
                json_formatter = JsonFormatter(
                    "%(asctime)s %(name)s %(levelname)s %(message)s"
                )
                axiom_handler.setFormatter(json_formatter)
            logger.addHandler(axiom_handler)
            logger.info("Axiom logging enabled")
        except Exception as e:
            logger.warning(f"Axiom logging failed to initialize: {e}")

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("libex")