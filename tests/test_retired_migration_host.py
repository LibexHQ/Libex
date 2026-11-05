"""
The old public host's migration notice is gone. These tests pin two things:
the five MIGRATION_* env vars are reported as retired rather than silently
accepted, and setting them (including the old on-switch) changes nothing the
app sends.
"""

# Standard library
import importlib
from unittest.mock import MagicMock, patch

# Third party
import pytest
from fastapi.testclient import TestClient

# Local
import app.main as main_module
from app.core.config import RETIRED_ENV_VARS, check_retired_env_vars, get_settings

_MIGRATION_ENV = {
    "MIGRATION_NOTICE_ENABLED": "true",
    "MIGRATION_NEW_HOST": "https://libexdb.com",
    "MIGRATION_ANNOUNCED": "2026-08-06",
    "MIGRATION_SUNSET": "2026-11-04",
    "MIGRATION_INFO_URL": "https://github.com/LibexHQ/Libex/issues/999",
}


def _warnings_for(monkeypatch, env: dict) -> list[str]:
    for name in _MIGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    logger = MagicMock()
    with patch("app.core.logging.get_logger", return_value=logger):
        check_retired_env_vars()
    return [c.args[0] for c in logger.warning.call_args_list]


@pytest.mark.parametrize("name", list(_MIGRATION_ENV))
def test_each_migration_var_triggers_its_retired_warning(monkeypatch, name):
    assert name in RETIRED_ENV_VARS
    warnings = _warnings_for(monkeypatch, {name: _MIGRATION_ENV[name]})

    assert len(warnings) == 1
    assert warnings[0].startswith(f"Retired env var {name} is set.")
    # Only the one that is set warns -- the other four stay quiet.
    assert all(other not in warnings[0] for other in _MIGRATION_ENV if other != name)


def test_no_migration_var_set_means_no_warning(monkeypatch):
    assert _warnings_for(monkeypatch, {}) == []


@pytest.fixture
def app_with_old_switch_on(monkeypatch):
    for name, value in _MIGRATION_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    importlib.reload(main_module)
    try:
        yield main_module.app
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        importlib.reload(main_module)
        get_settings.cache_clear()


def test_old_switch_on_still_sends_no_notice_headers(app_with_old_switch_on):
    client = TestClient(app_with_old_switch_on)
    for path in ("/health", "/openapi.json", "/does-not-exist"):
        headers = {k.lower() for k in client.get(path).headers}
        assert not headers & {"deprecation", "sunset", "link"}, path


def test_old_switch_on_leaves_health_without_a_notice_and_openapi_without_servers(
    app_with_old_switch_on,
):
    client = TestClient(app_with_old_switch_on)
    assert "notice" not in client.get("/health").json()
    assert "servers" not in client.get("/openapi.json").json()
