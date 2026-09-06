"""
Scheduled backup entry point: startup ordering, --once wiring and exit
codes.

scripts/backup.py is a thin file on purpose -- every decision about when a
backup happens, what it costs and what is deleted lives in
app/services/backup/, which has its own tests. What is left here is the
handful of things only the entry point can get wrong, and each of them has
gone wrong somewhere before:

  - SETUP_LOGGING RUNS FIRST, before anything touches a logger. It is what
    attaches this process's handlers at all -- without it a standalone
    script emits nothing -- and what holds httpx and httpcore down to
    WARNING. That mute is the control keeping a URL and its query string out
    of the logs, and it only holds if this runs before the run starts.
  - CHECK_RETIRED_ENV_VARS RUNS AFTER IT, because a warning logged before
    the handlers are attached goes nowhere, and before the run, because a
    stale name pasted into this container's environment has to be reported
    while someone is still reading.
  - THE EXIT CODE IS THE WHOLE OF WHAT A SUPERVISED RUN SAYS. Four distinct
    values, and collapsing any two of them sends an operator to the wrong
    place: 1 is nothing configured or a failed cycle, 2 is an unusable
    schedule, 3 is another backup process already holding the spool.
  - NO SIGNAL HANDLER OF ITS OWN. This process is PID 1 in its container,
    which ignores any signal it has not installed a handler for, so the
    runner's handlers are the only reason `docker stop` works -- and a
    handler here would displace them.

run_backup itself is replaced everywhere below: nothing here starts a
subprocess, opens a socket or touches a spool.
"""

# Standard library
import logging
import sys
from unittest.mock import MagicMock, patch

# Third party
import pytest

# Local
import scripts.backup as backup
from scripts.backup import main


# ============================================================
# STARTUP ORDERING
# ============================================================

def test_main_calls_setup_logging_before_running(monkeypatch):
    """
    The ordering that shipped broken once already, in another entry point.
    A run that starts before the handlers are attached emits nothing at all
    from a container nobody is watching, and un-mutes httpx for the rest of
    the process.
    """
    order = []
    monkeypatch.setattr(sys, "argv", ["backup.py"])

    def _fake_run_backup(once):
        order.append("run_backup-created")
        return "sentinel-coro"

    def _fake_asyncio_run(coro):
        order.append("asyncio.run")
        assert coro == "sentinel-coro"
        return 0

    with patch("scripts.backup.setup_logging", side_effect=lambda: order.append("setup_logging")), \
         patch("scripts.backup.run_backup", new=MagicMock(side_effect=_fake_run_backup)), \
         patch("scripts.backup.asyncio.run", side_effect=_fake_asyncio_run):
        main()

    assert order == ["setup_logging", "run_backup-created", "asyncio.run"]


def test_main_calls_check_retired_env_vars_after_logging_and_before_the_run(monkeypatch):
    """Its exact position, not merely its presence: after setup_logging,
    because a warning logged before the handlers are attached goes nowhere,
    and before the run, because a stale BACKUP_ name pasted into this
    container's environment has to be reported rather than discovered."""
    order = []
    monkeypatch.setattr(sys, "argv", ["backup.py"])

    def _fake_run_backup(once):
        order.append("run_backup-created")
        return "sentinel-coro"

    with patch("scripts.backup.setup_logging", side_effect=lambda: order.append("setup_logging")), \
         patch(
             "scripts.backup.check_retired_env_vars",
             side_effect=lambda: order.append("check_retired_env_vars"),
         ), \
         patch("scripts.backup.run_backup", new=MagicMock(side_effect=_fake_run_backup)), \
         patch("scripts.backup.asyncio.run", side_effect=lambda coro: order.append("asyncio.run") or 0):
        main()

    assert order == [
        "setup_logging", "check_retired_env_vars", "run_backup-created", "asyncio.run",
    ]


def test_the_entry_point_never_configures_logging_itself(monkeypatch):
    """
    logging.basicConfig attaches a root handler, which re-admits every
    third-party logger at INFO across the whole process. setup_logging is
    the only thing allowed to attach handlers here, and the mute it installs
    is what keeps a URL and its query string out of the logs.
    """
    monkeypatch.setattr(sys, "argv", ["backup.py"])

    with patch("scripts.backup.setup_logging"), \
         patch("scripts.backup.check_retired_env_vars"), \
         patch("scripts.backup.run_backup", new=MagicMock(return_value="sentinel-coro")), \
         patch("scripts.backup.asyncio.run", return_value=0), \
         patch.object(logging, "basicConfig") as basic_config:
        main()

    basic_config.assert_not_called()


def test_the_entry_point_installs_no_signal_handler_of_its_own():
    """
    This process is PID 1 in its container, which ignores any signal it has
    not installed a handler for -- so the runner's handlers are the only
    reason `docker stop` stops anything, and a handler installed here would
    displace them. A dump would then run to completion after SIGTERM, with
    its transaction snapshot and its ACCESS SHARE locks held for all of it.

    Asserted as the absence of the module, which is what a handler would
    need: importing signal here is the first step of the mistake and is
    visible before the handler exists.
    """
    assert not hasattr(backup, "signal")


# ============================================================
# --once WIRING
# ============================================================

def test_main_defaults_once_to_false(monkeypatch):
    """The no-argument form is what the compose file runs, and it is the
    scheduled one."""
    monkeypatch.setattr(sys, "argv", ["backup.py"])
    run_backup = MagicMock(return_value="sentinel-coro")

    with patch("scripts.backup.setup_logging"), \
         patch("scripts.backup.check_retired_env_vars"), \
         patch("scripts.backup.run_backup", run_backup), \
         patch("scripts.backup.asyncio.run", return_value=0):
        main()

    run_backup.assert_called_once_with(once=False)


def test_main_passes_once_true_when_the_flag_is_given(monkeypatch):
    """
    --once means dump now. It bypasses both the schedule and the catch-up
    predicate, deliberately rather than meaning "run one tick of the
    scheduler" -- a tick is correctly a no-op at every minute of the day
    that is not the configured slot, so that reading would leave an operator
    watching a supervised run produce no backup, exit 0, and have no way to
    tell that from a broken one.
    """
    monkeypatch.setattr(sys, "argv", ["backup.py", "--once"])
    run_backup = MagicMock(return_value="sentinel-coro")

    with patch("scripts.backup.setup_logging"), \
         patch("scripts.backup.check_retired_env_vars"), \
         patch("scripts.backup.run_backup", run_backup), \
         patch("scripts.backup.asyncio.run", return_value=0):
        main()

    run_backup.assert_called_once_with(once=True)


# ============================================================
# EXIT CODES
# ============================================================

@pytest.mark.parametrize(
    "exit_code",
    [
        # 1: no destination resolved, or the cycle failed.
        1,
        # 2: the configured schedule cannot be used.
        2,
        # 3: the spool is already claimed by another backup process.
        3,
    ],
)
def test_main_raises_systemexit_with_the_run_exit_code(monkeypatch, exit_code):
    """
    Every non-zero code reaches the process exit unchanged. Collapsing them
    into a single failure would tell an operator that something went wrong
    and nothing about where to look -- and the three differ in what they ask
    of them: fix the configuration, fix the schedule, or wait for the other
    process.
    """
    monkeypatch.setattr(sys, "argv", ["backup.py", "--once"])

    with patch("scripts.backup.setup_logging"), \
         patch("scripts.backup.check_retired_env_vars"), \
         patch("scripts.backup.run_backup", new=MagicMock(return_value="sentinel-coro")), \
         patch("scripts.backup.asyncio.run", return_value=exit_code):
        with pytest.raises(SystemExit) as raised:
            main()

    assert raised.value.code == exit_code


def test_main_does_not_raise_on_a_clean_exit_code(monkeypatch):
    """A successful supervised run exits 0 by returning, never by raising
    SystemExit(0) -- which prints nothing but is a different thing for
    anything wrapping this call."""
    monkeypatch.setattr(sys, "argv", ["backup.py", "--once"])

    with patch("scripts.backup.setup_logging"), \
         patch("scripts.backup.check_retired_env_vars"), \
         patch("scripts.backup.run_backup", new=MagicMock(return_value="sentinel-coro")), \
         patch("scripts.backup.asyncio.run", return_value=0):
        main()  # must not raise
