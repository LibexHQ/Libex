"""
ASIN validation and normalisation.

Pure regex logic with no framework dependency: what makes a string an
Audible ASIN, and what form of it Audible and the database both answer to.
It lives on its own because both the request surface, which validates a
path parameter before it ever reaches a route body, and the service layer,
which validates an ASIN found mid-response before using it to key a lookup,
need the same answer, and neither one is the natural owner of the other's
dependencies.
"""

# Standard library
import re

ASIN_PATTERN = re.compile(r'^[A-Z0-9]{10}$')


def normalise_asin(asin: str) -> str:
    """
    Returns the form of an ASIN that Audible and the database both answer to.

    Audible's catalogue is case-sensitive on the ASIN. A lowercase key returns
    the bare-ASIN shape Libex reads as a miss rather than the product, and
    stored keys are uppercase, so the database fallback misses the same way --
    a book that exists is reported absent. Callers may send either case, since
    is_valid_asin has always accepted both, which makes uppercasing the step
    that has to happen before the value is used for anything. It lives here
    beside the validator so no route has to remember it.
    """
    return asin.upper()


def is_valid_asin(asin: str) -> bool:
    """Validates that a string matches Audible ASIN format."""
    return bool(ASIN_PATTERN.fullmatch(normalise_asin(asin)))
