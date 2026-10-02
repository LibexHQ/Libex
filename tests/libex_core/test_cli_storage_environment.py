"""
LIBEX_CORE_STORAGE, the one setting that says where the local store is: every
form that is accepted and what it resolves to, every form that is refused, the
per-platform path `sqlite` stands for, that no other variable is consulted, and
that the value, which may be a database password, never reaches any output.
"""

# Standard library
import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Third party
import pytest

# Local
from libex_core.cli import environment
from libex_core.cli.environment import (
    STORAGE_VARIABLE,
    ConfigError,
    default_database_path,
    load_config,
)
from tests.libex_core._cli_support import REPO_ROOT, clean_env, run_python

SECRET = "SECRETPW-9f3a"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (STORAGE_VARIABLE, "LIBEX_CORE_ALLOW_DIRECT_EGRESS", "XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)


def _storage(monkeypatch, value):
    monkeypatch.setenv(STORAGE_VARIABLE, value)
    return load_config().storage


# ============================================================
# ACCEPTED
# ============================================================

@pytest.mark.parametrize("value", [None, "", "   ", "off", "OFF", " Off "])
def test_off_empty_and_unset_all_mean_no_storage(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(STORAGE_VARIABLE, value)
    assert load_config().storage is None


@pytest.mark.parametrize("value", ["/var/lib/libex/libex.db", "/tmp/a b/x.db", "/x/y?z#w.db", "  /padded.db "])
def test_an_absolute_path_is_a_sqlite_file(monkeypatch, value):
    target = _storage(monkeypatch, value)
    assert target.path == value.strip()
    assert target.url is None


@pytest.mark.parametrize(
    "value",
    [
        "sqlite:////abs/x.db",
        "sqlite+aiosqlite:////abs/x.db",
        "postgresql://u:pw@h/d",
        "postgresql+asyncpg://u:pw@h:5433/d",
        "POSTGRESQL://u:pw@h/d",
    ],
)
def test_a_sqlite_or_postgresql_url_is_kept_whole(monkeypatch, value):
    target = _storage(monkeypatch, value)
    assert target.url == value
    assert target.path is None


@pytest.mark.parametrize("value", ["sqlite", "SQLite", " sqlite "])
def test_the_word_sqlite_is_the_default_file(monkeypatch, tmp_path, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = _storage(monkeypatch, value)
    assert target.path == str(default_database_path())
    assert target.url is None


# ============================================================
# REFUSED
# ============================================================

@pytest.mark.parametrize(
    "value",
    [
        "relative/path.db",
        "./x.db",
        "../x.db",
        "x.db",
        "~/x.db",
        "file:x.db",
        "sqlite:///relative.db",
        "sqlite+aiosqlite:///relative.db",
        "sqlite:///dir/relative.db",
        "postgres://u:" + SECRET + "@h/d",
        "mysql://u:" + SECRET + "@h/d",
        "http://u:" + SECRET + "@h/d",
        "mssql+pyodbc://u:" + SECRET + "@h/d",
        "on",
        "true",
        "1",
    ],
)
def test_anything_else_is_refused_with_the_fixed_message(monkeypatch, value):
    monkeypatch.setenv(STORAGE_VARIABLE, value)
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert str(caught.value) == environment._STORAGE_INVALID
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "value",
    [
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite+aiosqlite://",
        "sqlite+aiosqlite:///:memory:",
        "sqlite:///",
        "sqlite+aiosqlite:///",
        "SQLITE:///:MEMORY:",
        "sqlite+aiosqlite:///:Memory:",
    ],
)
def test_an_in_memory_database_is_refused_because_nothing_would_outlive_the_command(monkeypatch, value):
    monkeypatch.setenv(STORAGE_VARIABLE, value)
    with pytest.raises(ConfigError, match="in-memory"):
        load_config()


# ============================================================
# THE PLATFORM'S DATA DIRECTORY
# ============================================================

def _on(monkeypatch, platform, home):
    monkeypatch.setattr(environment, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setenv("HOME", str(home))


def test_linux_uses_an_absolute_xdg_data_home(monkeypatch, tmp_path):
    _on(monkeypatch, "linux", tmp_path / "home")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_database_path() == tmp_path / "xdg" / "libex-core" / "libex.db"


@pytest.mark.parametrize("xdg", [None, "", "relative/data", "./data"])
def test_linux_ignores_a_missing_or_relative_xdg_data_home(monkeypatch, tmp_path, xdg):
    _on(monkeypatch, "linux", tmp_path / "home")
    if xdg is not None:
        monkeypatch.setenv("XDG_DATA_HOME", xdg)
    assert default_database_path() == tmp_path / "home" / ".local" / "share" / "libex-core" / "libex.db"


def test_macos_uses_application_support_and_ignores_xdg(monkeypatch, tmp_path):
    _on(monkeypatch, "darwin", tmp_path / "home")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_database_path() == (
        tmp_path / "home" / "Library" / "Application Support" / "libex-core" / "libex.db"
    )


def test_windows_uses_localappdata_when_set(monkeypatch, tmp_path):
    _on(monkeypatch, "win32", tmp_path / "home")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_database_path() == tmp_path / "local" / "libex-core" / "libex.db"


@pytest.mark.parametrize("local", [None, ""])
def test_windows_falls_back_under_the_home_directory(monkeypatch, tmp_path, local):
    _on(monkeypatch, "win32", tmp_path / "home")
    if local is not None:
        monkeypatch.setenv("LOCALAPPDATA", local)
    assert default_database_path() == (
        tmp_path / "home" / "AppData" / "Local" / "libex-core" / "libex.db"
    )


def test_an_undeterminable_home_is_a_fixed_message_naming_the_alternative(monkeypatch):
    def no_home():
        raise RuntimeError("could not determine home with " + SECRET)

    monkeypatch.setattr(Path, "home", staticmethod(no_home))
    with pytest.raises(ConfigError) as caught:
        default_database_path()
    assert "absolute path" in str(caught.value)
    assert SECRET not in str(caught.value)


# ============================================================
# NO OTHER VARIABLE IS READ
# ============================================================

HOSTILE = {
    "DATABASE_URL": "postgresql+asyncpg://evil:" + SECRET + "@evil.example/evil",
    "PGHOST": "evil.example",
    "PGUSER": "evil",
    "PGPASSWORD": SECRET,
    "PGDATABASE": "evil",
    "PGPORT": "1",
    "PGSERVICE": "evil",
    "SQLALCHEMY_DATABASE_URI": "postgresql://evil:" + SECRET + "@evil.example/evil",
}


def test_the_hosted_databases_variables_are_never_read(monkeypatch):
    for name, value in HOSTILE.items():
        monkeypatch.setenv(name, value)
    assert load_config().storage is None


def test_the_hosted_databases_variables_do_not_change_a_set_target(monkeypatch, tmp_path):
    for name, value in HOSTILE.items():
        monkeypatch.setenv(name, value)
    target = _storage(monkeypatch, str(tmp_path / "x.db"))
    assert target.path == str(tmp_path / "x.db")


def test_a_command_with_storage_off_never_reaches_for_the_hosted_database(run_cli):
    result = run_cli(["db", "status"], env=HOSTILE)
    assert result.code == 5
    assert SECRET not in result.out + result.err
    assert "evil" not in result.out + result.err


def test_a_command_with_a_file_store_uses_that_file_not_the_hosted_database(run_cli, tmp_path):
    result = run_cli(["db", "upgrade"], env={**HOSTILE, STORAGE_VARIABLE: str(tmp_path / "s.db")})
    assert result.code == 0, result.err
    assert (tmp_path / "s.db").exists()
    assert SECRET not in result.out + result.err


# ============================================================
# THE VALUE IS NEVER PRINTED
# ============================================================

def test_the_storage_target_is_not_in_a_repr_or_a_comparison():
    one = environment.StorageTarget(url="postgresql://u:" + SECRET + "@h/d")
    two = environment.StorageTarget(url="postgresql://other:pw@h/d")
    assert SECRET not in repr(one)
    assert one == two
    config = environment.Config(proxy_url=None, allow_direct_egress=False, storage=one)
    assert SECRET not in repr(config)


_SECRET_VALUES = [
    "postgres://u:" + SECRET + "@h/d",
    "mysql://u:" + SECRET + "@h/d",
    "postgresql://u:" + SECRET + "@h/d?plugin=x",
    "postgresql://u:" + SECRET + "@h/d?sslmode=require&sslmode=disable",
    "postgresql://u:" + SECRET + "@127.0.0.1:1/d",
    "postgresql+asyncpg://u:" + SECRET + "@127.0.0.1:1/d",
    "relative/" + SECRET + ".db",
    "sqlite:///:memory:?x=" + SECRET,
    "SQLITE:///:MEMORY:",
]


@pytest.mark.parametrize("value", _SECRET_VALUES)
@pytest.mark.parametrize("verbosity", [[], ["-v"], ["-vv"]])
@pytest.mark.parametrize("command", [["db", "status"], ["db", "upgrade"], ["db", "stats"], ["book", "sku", "SG1"]])
def test_the_value_appears_in_no_output_error_or_traceback(run_cli, value, verbosity, command):
    result = run_cli([*verbosity, *command], env={STORAGE_VARIABLE: value})
    assert result.code == 5
    assert SECRET not in result.out
    assert SECRET not in result.err


@pytest.mark.parametrize("command", [["db", "status"], ["db", "stats"], ["book", "sku", "SG1"]])
def test_a_path_in_a_directory_that_does_not_exist_stays_out_of_every_read_command(run_cli, command):
    result = run_cli(["-vv", *command], env={STORAGE_VARIABLE: "/nonexistent-dir-" + SECRET + "/x.db"})
    assert result.code == 5
    assert SECRET not in result.out + result.err


@pytest.mark.parametrize("verbosity", [[], ["-vv"]])
def test_a_directory_that_cannot_be_made_is_a_config_failure_that_keeps_the_path_out(run_cli, verbosity):
    result = run_cli([*verbosity, "db", "upgrade"], env={STORAGE_VARIABLE: "/nonexistent-dir-" + SECRET + "/x.db"})
    assert result.code == 5
    assert SECRET not in result.out + result.err


@pytest.mark.parametrize("value", _SECRET_VALUES[:6])
def test_the_value_does_not_leave_the_process_in_a_real_run_either(value):
    done = run_python(
        ["-m", "libex_core", "-vv", "db", "status"],
        env=clean_env(**{STORAGE_VARIABLE: value}),
        cwd=REPO_ROOT,
    )
    assert done.returncode == 5
    assert SECRET.encode() not in done.stdout + done.stderr


def test_this_module_sees_the_platform_it_is_run_on():
    # The monkeypatched platform tests above are about branches, not this host.
    assert sys.platform == environment.sys.platform
    assert os.path.isabs(str(default_database_path()))


@pytest.mark.parametrize("command", [["db", "upgrade"], ["db", "status"], ["db", "stats"]])
def test_a_path_under_a_regular_file_is_a_config_failure_that_keeps_the_path_out(run_cli, tmp_path, command):
    blocker = tmp_path / ("blocker-" + SECRET)
    blocker.write_text("x")
    result = run_cli(["-vv", *command], env={STORAGE_VARIABLE: str(blocker / "x.db")})
    assert result.code == 5
    assert SECRET not in result.out + result.err
