"""
`libex-core db upgrade` and `db status`, and what every command that needs the
store does when it cannot have one: storage off, the extra missing, and a store
in each state that is not ready. Every refusal is exit 5 with fixed text, and
no lookup asks Audible anything before its store is known to be usable.
"""

# Standard library
import os
import sqlite3
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.cli import store_state
from libex_core.cli.environment import STORAGE_VARIABLE
from libex_core.storage import upgrade as upgrade_module
from libex_core.storage.upgrade import VERSION_TABLE
from tests.libex_core._cli_lookup_support import CASE_IDS, CASES, EGRESS, install_session
from tests.libex_core._db_support import ALL_DB_COMMANDS, loads

HEAD = "438dbe70d041"


def _error(message, code):
    return f"libex-core: error: {message} (code: {code})\n"


def _stuck(monkeypatch):
    """A store whose recorded revision is one this library knows and is not
    the head, which the single-revision chain cannot otherwise produce."""

    class _Script:
        def get_current_head(self):
            return "0123456789ab"

        def get_revision(self, revision):
            return object()

    monkeypatch.setattr(upgrade_module, "_script", lambda connection: _Script())


def _foreign(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO notes (body) VALUES ('mine')")
    # The store warns about a file other users can read; that is not what
    # these tests are about.
    os.chmod(path, 0o600)


def _from_the_future(path):
    with sqlite3.connect(path) as conn:
        conn.execute(f"UPDATE {VERSION_TABLE} SET version_num = 'ffffffffffff'")


# ============================================================
# UPGRADE
# ============================================================

def test_upgrade_creates_the_store_and_prints_the_revision(run_cli, store_path):
    result = run_cli(["db", "upgrade"], env={STORAGE_VARIABLE: store_path})
    assert result.code == 0, result.err
    assert loads(result.out) == {"revision": HEAD}
    assert result.err == ""
    assert sqlite3.connect(store_path).execute(
        f"SELECT version_num FROM {VERSION_TABLE}"
    ).fetchall() == [(HEAD,)]


def test_upgrade_twice_is_the_same_answer_and_keeps_what_is_stored(run_cli, seeded_store):
    first = run_cli(["db", "upgrade"], env={STORAGE_VARIABLE: seeded_store})
    second = run_cli(["db", "upgrade"], env={STORAGE_VARIABLE: seeded_store})
    assert first.code == second.code == 0
    assert loads(first.out) == loads(second.out) == {"revision": HEAD}
    books = run_cli(["db", "stats"], env={STORAGE_VARIABLE: seeded_store})
    assert loads(books.out)["books"] == 15


def test_upgrade_refuses_a_foreign_database_and_leaves_it_alone(run_cli, store_path):
    _foreign(store_path)
    result = run_cli(["db", "upgrade"], env={STORAGE_VARIABLE: store_path})
    assert result.code == 5
    assert result.out == ""
    assert "did not create" in result.err
    assert result.err.endswith("(code: store_error)\n")
    tables = {r[0] for r in sqlite3.connect(store_path).execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"notes"}


def test_upgrade_refuses_a_store_from_a_newer_version(run_cli, empty_store):
    _from_the_future(empty_store)
    result = run_cli(["db", "upgrade"], env={STORAGE_VARIABLE: empty_store})
    assert result.code == 5
    assert "newer" in result.err
    assert result.out == ""


# ============================================================
# STATUS -- one state each, and the status that goes with it
# ============================================================

def test_status_ok_exits_0_with_state_and_revision(run_cli, empty_store):
    result = run_cli(["db", "status"], env={STORAGE_VARIABLE: empty_store})
    assert result.code == 0
    assert loads(result.out) == {"state": "ok", "revision": HEAD}
    assert result.err == ""


def test_status_not_initialised_exits_5(run_cli, store_path):
    result = run_cli(["db", "status"], env={STORAGE_VARIABLE: store_path})
    assert result.code == 5
    assert loads(result.out) == {"state": "not-initialised", "revision": None}
    assert result.err == _error(store_state.MESSAGES[store_state.NOT_INITIALISED], "store_error")


def test_status_does_not_create_the_file(run_cli, store_path, tmp_path):
    run_cli(["db", "status"], env={STORAGE_VARIABLE: store_path})
    assert list(tmp_path.iterdir()) == []


def test_status_outdated_exits_5(run_cli, empty_store, monkeypatch):
    _stuck(monkeypatch)
    result = run_cli(["db", "status"], env={STORAGE_VARIABLE: empty_store})
    assert result.code == 5
    assert loads(result.out) == {"state": "outdated", "revision": HEAD}
    assert result.err == _error(store_state.MESSAGES[store_state.OUTDATED], "store_error")


def test_status_ahead_exits_5(run_cli, empty_store):
    _from_the_future(empty_store)
    result = run_cli(["db", "status"], env={STORAGE_VARIABLE: empty_store})
    assert result.code == 5
    assert loads(result.out) == {"state": "ahead", "revision": "ffffffffffff"}
    assert result.err == _error(store_state.MESSAGES[store_state.AHEAD], "store_error")


def test_status_foreign_exits_5_and_changes_nothing(run_cli, store_path):
    _foreign(store_path)
    before = open(store_path, "rb").read()
    result = run_cli(["db", "status"], env={STORAGE_VARIABLE: store_path})
    assert result.code == 5
    assert loads(result.out) == {"state": "foreign", "revision": None}
    assert result.err == _error(store_state.MESSAGES[store_state.FOREIGN], "store_error")
    assert open(store_path, "rb").read() == before


def test_status_after_upgrade_is_ok_where_it_was_not_before(run_cli, store_path):
    env = {STORAGE_VARIABLE: store_path}
    assert run_cli(["db", "status"], env=env).code == 5
    assert run_cli(["db", "upgrade"], env=env).code == 0
    assert run_cli(["db", "status"], env=env).code == 0


# ============================================================
# EVERY READ COMMAND REFUSES A STORE THAT IS NOT READY
# ============================================================

READ_ONLY = [c for c in ALL_DB_COMMANDS if c[:2] not in (("db", "upgrade"), ("db", "status"))]


@pytest.mark.parametrize("command", READ_ONLY, ids=" ".join)
def test_a_store_with_no_schema_is_exit_5_with_the_upgrade_command_named(run_cli, store_path, command):
    result = run_cli(list(command), env={STORAGE_VARIABLE: store_path})
    assert result.code == 5
    assert result.out == ""
    assert result.err == _error(store_state.MESSAGES[store_state.NOT_INITIALISED], "store_error")
    assert "libex-core db upgrade" in result.err


@pytest.mark.parametrize("command", READ_ONLY, ids=" ".join)
def test_a_foreign_database_is_exit_5_and_is_never_read(run_cli, store_path, command):
    _foreign(store_path)
    result = run_cli(list(command), env={STORAGE_VARIABLE: store_path})
    assert result.code == 5
    assert result.out == ""
    assert result.err == _error(store_state.MESSAGES[store_state.FOREIGN], "store_error")


@pytest.mark.parametrize("command", READ_ONLY, ids=" ".join)
def test_a_store_behind_this_version_is_exit_5(run_cli, seeded_store, monkeypatch, command):
    _stuck(monkeypatch)
    result = run_cli(list(command), env={STORAGE_VARIABLE: seeded_store})
    assert result.code == 5
    assert result.out == ""
    assert result.err == _error(store_state.MESSAGES[store_state.OUTDATED], "store_error")


@pytest.mark.parametrize("command", READ_ONLY, ids=" ".join)
def test_a_store_from_a_newer_version_is_exit_5(run_cli, seeded_store, command):
    _from_the_future(seeded_store)
    result = run_cli(list(command), env={STORAGE_VARIABLE: seeded_store})
    assert result.code == 5
    assert result.out == ""
    assert result.err == _error(store_state.MESSAGES[store_state.AHEAD], "store_error")


# ============================================================
# STORAGE OFF
# ============================================================

@pytest.mark.parametrize("command", ALL_DB_COMMANDS, ids=" ".join)
@pytest.mark.parametrize("value", [None, "", "off"])
def test_every_db_command_with_storage_off_says_so_and_exits_5(run_cli, command, value):
    env = {} if value is None else {STORAGE_VARIABLE: value}
    result = run_cli(list(command), env=env)
    assert result.code == 5
    assert result.out == ""
    assert result.err == _error(store_state.STORAGE_OFF, "config_error")


def test_the_storage_off_text_names_the_variable_and_the_extra():
    assert STORAGE_VARIABLE in store_state.STORAGE_OFF
    assert "libex-core[storage]" in store_state.STORAGE_OFF


# ============================================================
# THE EXTRA IS MISSING
# ============================================================

@pytest.fixture
def no_storage_extra(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name in ("sqlalchemy", "aiosqlite") else real(name, *a),
    )


@pytest.mark.parametrize("command", ALL_DB_COMMANDS, ids=" ".join)
def test_every_db_command_without_the_extra_names_it_and_exits_5(run_cli, no_storage_extra, tmp_path, command):
    result = run_cli(list(command), env={STORAGE_VARIABLE: str(tmp_path / "x.db")})
    assert result.code == 5
    assert result.out == ""
    assert "libex-core[storage]" in result.err
    assert result.err.endswith("(code: config_error)\n")
    assert str(tmp_path) not in result.err


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_lookup_without_the_extra_stops_before_asking_audible(run_cli, no_storage_extra, tmp_path, monkeypatch, case):
    get = AsyncMock()
    install_session(monkeypatch, get)
    result = run_cli(list(case.argv), env={**EGRESS, STORAGE_VARIABLE: str(tmp_path / "x.db")})
    assert result.code == 5
    assert result.out == ""
    assert "libex-core[storage]" in result.err
    get.assert_not_called()


# ============================================================
# A LOOKUP WITH STORAGE ON BUT NOT READY
# ============================================================

@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_lookup_against_an_uninitialised_store_is_exit_5_and_asks_nothing(run_cli, store_path, monkeypatch, case):
    get = case.make_get()
    install_session(monkeypatch, get)
    result = run_cli(list(case.argv), env={**EGRESS, STORAGE_VARIABLE: store_path})
    assert result.code == 5
    assert result.out == ""
    assert result.err == _error(store_state.MESSAGES[store_state.NOT_INITIALISED], "store_error")
    assert not get.called


@pytest.mark.parametrize("state", [store_state.FOREIGN, store_state.AHEAD, store_state.OUTDATED])
def test_a_lookup_against_a_store_that_is_not_ready_names_why(run_cli, store_path, empty_store, monkeypatch, state):
    path = store_path
    if state == store_state.FOREIGN:
        _foreign(path)
    else:
        path = empty_store
        if state == store_state.AHEAD:
            _from_the_future(path)
        else:
            _stuck(monkeypatch)
    case = CASES[0]
    get = case.make_get()
    install_session(monkeypatch, get)
    result = run_cli(list(case.argv), env={**EGRESS, STORAGE_VARIABLE: path})
    assert result.code == 5
    assert result.err == _error(store_state.MESSAGES[state], "store_error")
    assert not get.called
