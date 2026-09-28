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
import logging
from unittest.mock import patch

# Third party
from fastapi.testclient import TestClient

# Local
import app.services.audible as audible_service
from app.main import app
from libex_core.audible.client import TransportSummary


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


# ============================================================
# STARTUP LOGS WHICH AUDIBLE TRANSPORT IS IN USE
#
# _hosted_client is built once, at app.services.audible's own import, from
# whatever AUDIBLE_PROXY_URL happened to be set the first time this test
# process imported it -- well before this file's own collection. Swapping in
# a fake here rather than an env var is the only way to exercise both the
# proxy and the direct shape in one process; app.main reads the attribute
# through the module object on every call (see its own lifespan comment),
# so replacing it on app.services.audible is exactly what a real transport
# change would look like from app.main's side.
# ============================================================

class _FakeTransportClient:
    """Stands in for LibexClient: only transport_summary() is exercised by
    the lifespan. proxy_url is never read by app.main -- it exists here so a
    test can prove that, by asserting the secret embedded in it never
    reaches a log record even though the fake client "holds" it."""

    def __init__(self, summary: TransportSummary, proxy_url: str = ""):
        self._summary = summary
        self.proxy_url = proxy_url

    def transport_summary(self) -> TransportSummary:
        return self._summary


def _audible_transport_records(caplog):
    return [
        r for r in caplog.records
        if r.name == "libex" and hasattr(r, "audible_transport_mode")
    ]


def test_lifespan_logs_audible_transport_mode_and_host_for_a_proxy(monkeypatch, caplog):
    monkeypatch.setattr(
        audible_service,
        "_hosted_client",
        _FakeTransportClient(
            TransportSummary(mode="proxy", host="libex-vpn"),
            proxy_url="http://libexuser:hunter2@libex-vpn:8888",
        ),
    )

    with caplog.at_level(logging.INFO):
        with TestClient(app):
            pass

    records = _audible_transport_records(caplog)
    assert records, "no Audible transport log record was emitted -- the line is inert"
    record = records[0]
    assert record.audible_transport_mode == "proxy"
    assert record.audible_transport_host == "libex-vpn"
    assert "hunter2" not in caplog.text
    assert "libexuser" not in caplog.text
    assert "8888" not in caplog.text


def test_lifespan_logs_audible_transport_mode_and_host_for_direct_egress(monkeypatch, caplog):
    monkeypatch.setattr(
        audible_service,
        "_hosted_client",
        _FakeTransportClient(TransportSummary(mode="direct", host=None)),
    )

    with caplog.at_level(logging.INFO):
        with TestClient(app):
            pass

    records = _audible_transport_records(caplog)
    assert records, "no Audible transport log record was emitted -- the line is inert"
    record = records[0]
    assert record.audible_transport_mode == "direct"
    assert record.audible_transport_host is None


def test_lifespan_runs_once_per_worker_process_so_the_line_logs_once_here(monkeypatch, caplog):
    """One TestClient start is one lifespan entry, which is one worker in the
    real process model (see app.main's own lifespan comment on why migrations
    are not run there) -- so exactly one record is expected per start, not
    zero and not several."""
    monkeypatch.setattr(
        audible_service,
        "_hosted_client",
        _FakeTransportClient(TransportSummary(mode="direct", host=None)),
    )

    with caplog.at_level(logging.INFO):
        with TestClient(app):
            pass

    assert len(_audible_transport_records(caplog)) == 1
