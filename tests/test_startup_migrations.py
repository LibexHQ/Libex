"""
Startup applies no schema migrations.

Migrations are applied once by the container entrypoint, before it execs
uvicorn. They are deliberately not applied by the application lifespan,
because the lifespan runs once per uvicorn worker and alembic serialises
nothing of its own -- concurrent workers would race the same DDL, with the
losers failing on already-applied statements.

That seam is invisible at a single worker and only bites in production, so it
is held here: these tests fail if an alembic upgrade is reintroduced into
startup, in either of the shapes an import can take.
"""

# Standard library
from unittest.mock import patch

# Third party
from fastapi.testclient import TestClient

# Local
from app.main import app


# ============================================================
# NO UPGRADE IS ISSUED DURING STARTUP
# ============================================================

def test_app_startup_issues_no_alembic_upgrade():
    """Entering the lifespan calls nothing on alembic.

    The spy is installed on the alembic package itself rather than on an
    import location in app.main, which is the deliberate inversion of the
    usual rule: the assertion is that *no* consumer reaches it, so there is no
    single consumer import site to patch.
    """
    import alembic.command

    with patch.object(alembic.command, "upgrade") as upgrade:
        with TestClient(app):
            pass

    upgrade.assert_not_called()


def test_app_serves_health_without_having_migrated():
    """Startup completes and the app serves with no migration run at all.

    Guards the other half of the claim: dropping the upgrade did not make a
    started app depend on one having happened first.
    """
    import alembic.command

    with patch.object(alembic.command, "upgrade") as upgrade:
        with TestClient(app) as c:
            response = c.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    upgrade.assert_not_called()


# ============================================================
# NOTHING FROM ALEMBIC IS BOUND AT IMPORT
# ============================================================

def test_app_main_binds_no_alembic_name():
    """app.main's namespace holds nothing that came from alembic.

    The call-site spy above cannot see `from alembic.command import upgrade`:
    that binds the real function into app.main at import time, which is before
    any patch in this file runs, so the upgrade would fire and the spy would
    still read zero calls. This checks the binding instead of the call, and
    covers all three import shapes -- `import alembic`, `from alembic import
    command`, and `from alembic.command import upgrade` -- because each leaves
    a value whose origin module is alembic or a submodule of it.
    """
    from app import main

    offenders = sorted(
        name
        for name, value in vars(main).items()
        if _origin_module(value) == "alembic"
        or _origin_module(value).startswith("alembic.")
    )

    assert offenders == [], (
        f"app.main binds {offenders} from alembic. Migrations run in the "
        "container entrypoint, not per worker in the lifespan."
    )


def _origin_module(value) -> str:
    """Module a bound value came from: __module__ for classes and functions,
    __name__ for a module object, which has no __module__."""
    origin = getattr(value, "__module__", None) or getattr(value, "__name__", "")
    return origin if isinstance(origin, str) else ""
