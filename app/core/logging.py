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
            logger.addHandler(axiom_handler)
            logger.info("Axiom logging enabled")
        except Exception as e:
            logger.warning(f"Axiom logging failed to initialize: {e}")

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("libex")