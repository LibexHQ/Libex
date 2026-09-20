"""
app.services.audible's own package init: the one seam where the hosted
application's config -- audible_proxy_url, read through app.core.config --
crosses into libex_core.audible.client.configure_transport().

Run as a subprocess against a fresh interpreter for both tests below, not
against a plain patch of get_settings() in this process: get_settings() is
lru_cache'd and app.services.audible's module-level configure_transport()
call runs exactly once, at import, so once either has already run in this
test process (as it has, well before this file's own collection, via
tests/conftest.py's app imports) there is no way to make either run again
with a different AUDIBLE_PROXY_URL. Only a child process that has imported
nothing yet proves what happens at that one import.
"""

# Standard library
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_CHILD_SCRIPT = """
import app.services.audible
import libex_core.audible.client as client

summary = client.transport_summary()
print("MODE:" + summary.mode)
print("HOST:" + (summary.host or ""))
"""


def _run_with_proxy_url(tmp_path, proxy_url: str | None) -> subprocess.CompletedProcess:
    """Runs the child script from an empty working directory so no .env in
    the repo root is picked up by Settings' own env_file=".env" -- only
    PYTHONPATH (to find app and libex_core) and, when proxy_url is not
    None, AUDIBLE_PROXY_URL are in the child's environment. A None
    proxy_url leaves the variable entirely unset, the documented way to run
    hosted Libex without a proxy (README.md), rather than setting it to an
    empty string -- both reach Settings' own "" default, but leaving it
    unset is the shape a self-hoster's compose file actually produces."""
    env = {"PYTHONPATH": str(REPO_ROOT)}
    if proxy_url is not None:
        env["AUDIBLE_PROXY_URL"] = proxy_url
    return subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ============================================================
# UNSET AUDIBLE_PROXY_URL -- resolves to direct egress, never raises
#
# README.md's self-hosting instructions document leaving AUDIBLE_PROXY_URL
# blank as how to run without a proxy -- a settled product decision, not an
# oversight for libex_core to second-guess. This is the one property that
# keeps every self-hoster who runs without a VPN working: nothing in the
# tree exercised it before this test, and it is exactly what a later
# "tidying up" of the unconditional allow_direct_egress=True at this seam
# would silently break.
# ============================================================

def test_unset_audible_proxy_url_resolves_to_direct_egress_without_raising(tmp_path):
    result = _run_with_proxy_url(tmp_path, None)

    assert result.returncode == 0, result.stderr
    assert "MODE:direct" in result.stdout
    assert "HOST:" in result.stdout


def test_blank_audible_proxy_url_resolves_to_direct_egress_without_raising(tmp_path):
    """AUDIBLE_PROXY_URL="" (set but empty) reaches the same Settings
    default as leaving it unset -- checked separately since a compose file
    that sets the variable to an empty string, rather than omitting it, is
    also a real deployment shape."""
    result = _run_with_proxy_url(tmp_path, "")

    assert result.returncode == 0, result.stderr
    assert "MODE:direct" in result.stdout
    assert "HOST:" in result.stdout


# ============================================================
# MALFORMED AUDIBLE_PROXY_URL -- still raises at import, unchanged
#
# allow_direct_egress is ignored whenever proxy_url is non-empty (see
# configure_transport's own docstring), so a malformed value must fail
# exactly as it did before this seam started passing the flag.
# ============================================================

def test_malformed_audible_proxy_url_still_raises_at_import(tmp_path):
    result = _run_with_proxy_url(tmp_path, "socks5://libex-seeder-vpn:8888")

    assert result.returncode != 0
    assert "ValueError" in result.stderr
    assert "proxy URL must use the http or https scheme" in result.stderr
    assert "MODE:" not in result.stdout
