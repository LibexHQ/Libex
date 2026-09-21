"""
Shared fixtures for scripts/ tests.
"""

# Third party
import pytest

# Core
from libex_core.audible.client import LibexClient

# Services
import app.services.audible as audible_service


@pytest.fixture
def restore_audible_transport():
    """
    Snapshots app.services.audible._hosted_client -- by object identity --
    before a test swaps it for a LibexClient of its own, and restores the
    original instance afterward, regardless of pass/fail.

    The real hosted instance is built exactly once per process, at
    app.services.audible's own import time, from whatever AUDIBLE_PROXY_URL
    the settings held then. There is no module-level configure_transport()
    left to call and no separate transport snapshot to poke: every
    scripts/*.py _verify_dedicated_proxy check, and every audible_get call
    in the process, resolves this exact instance by reading
    app.services.audible._hosted_client, so a script-guard test that wants
    to exercise direct egress or a specific proxy hostname has to install a
    LibexClient of its own as that name (see set_hosted_transport below) and
    this fixture is what restores the original object afterward. Restoring
    the exact prior instance -- by identity, not a freshly-built one that
    merely looks equivalent -- is what stops a leaked swap from changing
    which exit real traffic uses for every test that runs after it, not
    merely what a summary reports.
    """
    original = audible_service._hosted_client
    yield
    audible_service._hosted_client = original


def set_hosted_transport(proxy_url, *, allow_direct_egress=False):
    """
    Builds a fresh LibexClient with the given transport and installs it as
    app.services.audible._hosted_client -- the one name every
    _verify_dedicated_proxy check and every audible_get call in the process
    resolves through. Every caller of this helper must also depend on the
    restore_audible_transport fixture above, or the swap leaks into whatever
    test runs next.
    """
    audible_service._hosted_client = LibexClient(
        proxy_url=proxy_url, allow_direct_egress=allow_direct_egress
    )
