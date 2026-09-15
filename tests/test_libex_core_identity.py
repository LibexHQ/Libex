"""
The wiring that binds the application to libex_core, checked by identity
rather than by shape.

A duplicate class with the same name and the same fields validates the same
data and would pass every behavioral test the original passes, so nothing
short of `is` proves that a route, an exception handler, or a `try`/`except`
still resolves to the one object libex_core actually defines rather than to
a look-alike shadowing it locally.
"""

# Standard library
import typing

# Third party
from fastapi import FastAPI

# Local
import libex_core.models as _models
from app.api.routes import audible_outage
from app.main import app
from libex_core.exceptions import AudibleAPIException, LibexException


# ============================================================
# ROUTE RESPONSE MODELS ARE THE libex_core CLASS, NOT A LOOK-ALIKE
# ============================================================

# Every BaseModel subclass libex_core.models actually defines, keyed by
# name -- what a route's response_model is compared against below. Excludes
# BaseModel itself, which the module imports rather than defines.
_LIBEX_CORE_MODELS_BY_NAME = {
    name: value
    for name, value in vars(_models).items()
    if isinstance(value, type)
    and issubclass(value, _models.BaseModel)
    and value is not _models.BaseModel
}


def _leaf_model(annotation):
    """Unwraps a route's response_model down to the model class itself --
    through list[X] and X | None -- so `list[BookResponse]` and
    `BookResponse` are both checked against the same class."""
    args = typing.get_args(annotation)
    if not args:
        return annotation
    for arg in args:
        if arg is not type(None):
            return _leaf_model(arg)
    return annotation


def _response_model_routes(fastapi_app: FastAPI):
    for route in fastapi_app.routes:
        response_model = getattr(route, "response_model", None)
        if response_model is not None:
            yield route.path, response_model


def test_every_route_response_model_named_after_a_libex_core_model_is_that_class():
    """For every route whose response_model unwraps to a class sharing a
    name with something libex_core.models defines, the route's class must
    be that exact object. A same-named duplicate defined and imported
    elsewhere -- a leftover in a route schemas module, say -- would satisfy
    every other check a route/schema test could make and still be the wrong
    class to serialise against."""
    checked_names = set()
    mismatches = []
    for path, response_model in _response_model_routes(app):
        leaf = _leaf_model(response_model)
        if not isinstance(leaf, type):
            continue
        expected = _LIBEX_CORE_MODELS_BY_NAME.get(leaf.__name__)
        if expected is None:
            continue
        checked_names.add(leaf.__name__)
        if leaf is not expected:
            mismatches.append((path, leaf.__name__))

    assert mismatches == [], (
        f"routes whose response_model is a same-named class other than "
        f"libex_core.models's own: {mismatches}"
    )
    # Non-vacuousness: this must actually have found and checked the
    # well-known response models that route to libex_core.models, not
    # merely found zero routes worth checking.
    assert {"BookResponse", "ChapterResponse", "BulkBookResponse", "SeriesResponse"} <= checked_names


# ============================================================
# THE EXCEPTION HANDLER IS REGISTERED FOR THE SHARED CLASS
# ============================================================

def test_libex_exception_handler_is_registered_for_the_libex_core_class():
    """app.main registers its catch-all handler against LibexException as
    imported from libex_core.exceptions. Dict lookup on app.exception_handlers
    uses the class object as the key, so this only passes if app.main bound
    the same object -- a shadowing redefinition anywhere along the way would
    register the handler under a different key and this lookup would miss."""
    assert LibexException in app.exception_handlers


# ============================================================
# audible_outage CATCHES THE SHARED AudibleAPIException CLASS
# ============================================================

def test_audible_outage_module_binds_the_same_audibleapiexception_object():
    """The except clause in outage_as_not_found can only catch instances of
    whatever class name `AudibleAPIException` is bound to in this module's
    own namespace. Checking that binding by identity is what proves the
    behavioral tests in tests/api/test_audible_outage.py are exercising
    libex_core's own class and not a duplicate that merely shares its name."""
    assert audible_outage.AudibleAPIException is AudibleAPIException
