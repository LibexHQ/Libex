"""
Tests for the worker heartbeat: the writer is atomic and never raises, the
reader is strict and fails toward "could not check", the name cannot carry a
path, and the directory is outside the rotating log handler's deletion set.
"""

# Standard library
import asyncio
import json
import logging
import os
import stat
import time

# Third party
import pytest

# Core
from app.core.logging import MultiProcessTimedRotatingFileHandler

# Services
from app.services.watch import heartbeat
from app.services.watch.heartbeat import Beat, Heartbeat, read

GOOD = {"alive_at": 1000.5, "progress_at": 900, "progress_due_s": 1800, "state": "running"}


def _put(tmp_path, data, name="seeder"):
    path = tmp_path / f"{name}.json"
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    return path


# --- reader: the correct answer ----------------------------------------------

def test_read_returns_the_beat_for_a_valid_file(tmp_path):
    _put(tmp_path, GOOD)
    assert read("seeder", str(tmp_path)) == Beat(1000.5, 900.0, 1800.0, "running")


@pytest.mark.parametrize("state", ["running", "stopped", "done"])
def test_read_accepts_each_state(tmp_path, state):
    _put(tmp_path, {**GOOD, "state": state})
    assert read("seeder", str(tmp_path)).state == state


# --- reader: wrong answers are "could not check", never healthy -----------------

def test_read_missing_file_is_none(tmp_path):
    assert read("seeder", str(tmp_path)) is None


def test_read_a_directory_in_place_of_the_file_is_none(tmp_path):
    (tmp_path / "seeder.json").mkdir()
    assert read("seeder", str(tmp_path)) is None


@pytest.mark.parametrize("data", [
    {**GOOD, "extra": 1},
    {**GOOD, "state_note": "x"},
    {k: v for k, v in GOOD.items() if k != "alive_at"},
    {k: v for k, v in GOOD.items() if k != "state"},
    {},
])
def test_read_rejects_extra_or_missing_keys(tmp_path, data):
    _put(tmp_path, data)
    assert read("seeder", str(tmp_path)) is None


@pytest.mark.parametrize("field", ["alive_at", "progress_at", "progress_due_s"])
@pytest.mark.parametrize("bad", ["12", None, True, False, [1], {"a": 1}, -1, -0.5])
def test_read_rejects_wrong_typed_or_negative_numbers(tmp_path, field, bad):
    _put(tmp_path, {**GOOD, field: bad})
    assert read("seeder", str(tmp_path)) is None


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_read_rejects_non_finite_numbers(tmp_path, literal):
    raw = ('{"alive_at": %s, "progress_at": 1, "progress_due_s": 1, "state": "running"}' % literal).encode()
    _put(tmp_path, raw)
    assert read("seeder", str(tmp_path)) is None


@pytest.mark.parametrize("state", ["paused", "RUNNING", "", None, 1, True, ["running"], ["running", "done"], [], {"running": 1}, {}])
def test_read_rejects_a_state_outside_the_fixed_set(tmp_path, state):
    _put(tmp_path, {**GOOD, "state": state})
    assert read("seeder", str(tmp_path)) is None


@pytest.mark.parametrize("data", [[], [GOOD], "running", 7, None, b"not json", b"", b"\xff\xfe\x00bad", b'{"alive_at":'])
def test_read_rejects_non_object_or_unparseable_content(tmp_path, data):
    _put(tmp_path, data if isinstance(data, bytes) else json.dumps(data).encode())
    assert read("seeder", str(tmp_path)) is None


def test_read_caps_the_file_at_4_kib(tmp_path):
    body = json.dumps(GOOD).encode()
    exactly = body + b" " * (heartbeat.MAX_BYTES - len(body))
    assert len(exactly) == 4096
    _put(tmp_path, exactly)
    assert read("seeder", str(tmp_path)) is not None  # the boundary itself is allowed
    _put(tmp_path, exactly + b" ")
    assert read("seeder", str(tmp_path)) is None  # one byte over is not


def test_read_never_reads_more_than_the_cap(tmp_path, monkeypatch):
    _put(tmp_path, GOOD)
    asked = []
    real_open = open

    def spy(path, mode="r", *a, **kw):
        handle = real_open(path, mode, *a, **kw)
        real_read = handle.read
        handle.read = lambda n=-1: (asked.append(n), real_read(n))[1]
        return handle

    monkeypatch.setattr("builtins.open", spy)
    read("seeder", str(tmp_path))
    assert asked == [heartbeat.MAX_BYTES + 1]


# --- the name is a constant, never a path ---------------------------------------

@pytest.mark.parametrize("name", ["seeder", "backfill", "a-1", "x"])
def test_valid_names(name):
    assert heartbeat.NAME_RE.fullmatch(name)


@pytest.mark.parametrize("name", ["", "../etc/passwd", "a/b", "A", "a.b", "a b", "a\n", "seeder.json", "é"])
def test_invalid_names_rejected_by_reader_and_writer(name, tmp_path):
    with pytest.raises(ValueError):
        read(name, str(tmp_path))
    with pytest.raises(ValueError):
        Heartbeat(name, 60, str(tmp_path))


# --- writer ------------------------------------------------------------------

async def test_start_writes_running_and_stop_writes_stopped(tmp_path):
    beat = Heartbeat("seeder", 7200, str(tmp_path)).start()
    got = read("seeder", str(tmp_path))
    assert got.state == "running" and got.progress_due_s == 7200.0
    await beat.stop()
    assert read("seeder", str(tmp_path)).state == "stopped"


async def test_stop_writes_done_when_the_run_finished(tmp_path):
    beat = Heartbeat("backfill", 1800, str(tmp_path)).start()
    beat.done = True
    await beat.stop()
    assert read("backfill", str(tmp_path)).state == "done"


async def test_done_wins_over_a_stopped_request(tmp_path):
    beat = Heartbeat("backfill", 1800, str(tmp_path)).start()
    beat.done = True
    await beat.stop("stopped")
    assert read("backfill", str(tmp_path)).state == "done"


async def test_stop_none_leaves_the_last_running_beat_so_a_crash_reads_as_dead(tmp_path):
    beat = Heartbeat("seeder", 7200, str(tmp_path)).start()
    await beat.stop(None)
    assert read("seeder", str(tmp_path)).state == "running"


async def test_stop_cancels_the_ticker(tmp_path):
    beat = Heartbeat("seeder", 7200, str(tmp_path)).start()
    await beat.stop()
    assert beat._ticker.cancelled() or beat._ticker.done()


async def test_ticker_refreshes_alive_but_not_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "TICK_SECONDS", 0.01)
    beat = Heartbeat("seeder", 7200, str(tmp_path))
    beat.progress_at = 5.0
    beat.start()
    first = read("seeder", str(tmp_path)).alive_at
    await asyncio.sleep(0.1)
    later = read("seeder", str(tmp_path))
    await beat.stop(None)
    assert later.alive_at > first
    assert later.progress_at == 5.0  # a ticker that moved this would hide a stall


async def test_progress_moves_progress_at(tmp_path):
    beat = Heartbeat("seeder", 7200, str(tmp_path))
    beat.progress_at = 5.0
    beat.progress()
    assert beat.progress_at > time.time() - 5


async def test_defer_moves_progress_at_to_the_end_of_the_pause(tmp_path):
    beat = Heartbeat("seeder", 1800, str(tmp_path))
    beat.progress_at = 5.0
    before = time.time()
    beat.defer(600)
    assert before + 600 <= beat.progress_at <= time.time() + 600


async def test_defer_never_moves_progress_at_backwards(tmp_path):
    beat = Heartbeat("seeder", 1800, str(tmp_path))
    beat.progress_at = time.time() + 10_000
    keep = beat.progress_at
    beat.defer(5)  # a short sleep inside a long pause must not shorten it
    assert beat.progress_at == keep


async def test_a_deferred_progress_at_is_what_the_file_carries(tmp_path):
    beat = Heartbeat("backfill", 1800, str(tmp_path))
    beat.progress_at = 5.0
    beat.defer(600)
    beat._write("running")
    assert read("backfill", str(tmp_path)).progress_at == beat.progress_at > time.time() + 500


async def test_write_is_temp_in_same_directory_then_os_replace(tmp_path, monkeypatch):
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((src, dst, os.path.exists(dst), json.loads(open(src).read())))
        return real_replace(src, dst)

    monkeypatch.setattr(heartbeat.os, "replace", spy)
    beat = Heartbeat("seeder", 60, str(tmp_path)).start()
    await beat.stop(None)
    src, dst, dst_existed, content = calls[0]
    assert os.path.dirname(src) == os.path.dirname(dst) == str(tmp_path)
    assert src != dst and dst == str(tmp_path / "seeder.json")
    assert dst_existed is False  # the final name only ever appears through the replace
    assert content["state"] == "running"  # the temp file was complete before it was swapped in
    assert [p.name for p in tmp_path.iterdir()] == ["seeder.json"]  # no temp left behind


async def test_file_is_world_readable_not_writable(tmp_path):
    beat = Heartbeat("seeder", 60, str(tmp_path)).start()
    await beat.stop(None)
    assert stat.S_IMODE(os.stat(tmp_path / "seeder.json").st_mode) == 0o644


async def test_failed_replace_never_raises_keeps_the_old_file_and_leaves_no_temp(tmp_path, monkeypatch, caplog):
    beat = Heartbeat("seeder", 60, str(tmp_path)).start()
    before = (tmp_path / "seeder.json").read_bytes()

    def boom(src, dst):
        raise PermissionError(f"denied {dst}")

    monkeypatch.setattr(heartbeat.os, "replace", boom)
    with caplog.at_level(logging.WARNING, logger="libex"):
        beat._write("stopped")
        beat._write("stopped")
    await beat.stop(None)
    assert (tmp_path / "seeder.json").read_bytes() == before  # the reader still sees a whole file
    assert [p.name for p in tmp_path.iterdir()] == ["seeder.json"]
    warned = [r for r in caplog.records if r.msg == "Heartbeat: write failed"]
    assert len(warned) == 1  # once, not per tick
    assert warned[0].error_type == "PermissionError"
    assert str(tmp_path) not in caplog.text


async def test_unwritable_directory_does_not_raise(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should be")
    beat = Heartbeat("seeder", 60, str(blocker / "sub")).start()
    await beat.stop()  # no exception is the assertion; the worker must survive its heartbeat


def test_default_directory_comes_from_the_env_then_the_constant(monkeypatch):
    monkeypatch.delenv("HEARTBEAT_DIR", raising=False)
    assert heartbeat.heartbeat_dir() == heartbeat.DEFAULT_DIR == "/app/logs/heartbeat"
    monkeypatch.setenv("HEARTBEAT_DIR", "/somewhere")
    assert heartbeat.heartbeat_dir() == "/somewhere"


# --- the directory survives the rotating handler's retention cleanup ------------

async def test_heartbeat_directory_survives_log_retention_cleanup(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    hb_dir = log_dir / os.path.basename(heartbeat.DEFAULT_DIR)
    beat = Heartbeat("seeder", 60, str(hb_dir)).start()
    await beat.stop()

    old = [log_dir / f"libex.log.2020-01-0{n}" for n in range(1, 6)]
    for path in old:
        path.write_text("old")
    handler = MultiProcessTimedRotatingFileHandler(str(log_dir / "libex.log"), when="midnight", backupCount=1)
    try:
        assert handler.getFilesToDelete(), "control: retention must have something to delete"
        handler.doRollover()
    finally:
        handler.close()

    assert not any(p.exists() for p in old[:-1])  # control: the cleanup really ran
    assert read("seeder", str(hb_dir)).state == "stopped"


def test_default_directory_is_a_subdirectory_outside_the_libex_log_prefix():
    assert os.path.dirname(heartbeat.DEFAULT_DIR) == "/app/logs"
    assert not os.path.basename(heartbeat.DEFAULT_DIR).startswith("libex.log")
