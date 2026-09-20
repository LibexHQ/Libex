"""
Shared fixtures for scripts/ tests.
"""

# Third party
import pytest

# Local
import libex_core.audible.client as audible_client


@pytest.fixture
def restore_audible_transport():
    """
    Snapshots the module-level transport state before a test configures its
    own, and restores it afterward, regardless of pass/fail.

    The real transport is set exactly once per process, at
    app.services.audible's own import time, from whatever AUDIBLE_PROXY_URL
    the settings held then -- scripts/*.py never call configure_transport()
    themselves, they only read transport_summary(). A script-guard test that
    wants to exercise "unconfigured", "direct", or a specific proxy hostname
    has no way to do that except calling configure_transport() directly, and
    since this state lives in module globals shared across the whole pytest
    process, a test that changes it and doesn't put it back would leak into
    every test that runs after it -- in this file or another. Restoring the
    exact prior snapshot object and generation counter, rather than calling
    configure_transport() again with whatever the original value looked
    like, is what makes the restore exact rather than merely equivalent.
    """
    snapshot = audible_client._transport
    generation = audible_client._transport_generation
    yield
    audible_client._transport = snapshot
    audible_client._transport_generation = generation
