"""
The worker heartbeat: a small JSON file the seeder and the backfill write and
the watcher reads, so a dead or stalled worker is noticed from outside it.

    {"alive_at": ..., "progress_at": ..., "progress_due_s": ..., "state": ...}

alive_at is refreshed by an asyncio ticker, so a worker whose loop is blocked
or gone stops refreshing it. progress_at moves only when the worker reports a
unit of work, so a worker that is up but doing nothing goes stale past
progress_due_s. A worker that sleeps on purpose calls defer(seconds), which
moves progress_at forward so the pause is not read as a stall. state is
running, stopped (a clean SIGTERM) or done (a finished run); the watcher
stays silent on the last two.

Invariants:
  - The name is a code constant matching NAME_RE, never taken from input, so it
    can never carry a path.
  - Every write is a temp file in the same directory and an os.replace, so a
    reader never sees half a file. The directory is a subdirectory of the log
    directory, outside the rotating handler's deletion set (it only collects
    "libex.log." names in the log directory itself).
  - A write that fails is logged by error type and never raises: a heartbeat
    must not take down the worker it reports on.
  - The reader caps the file at 4 KiB and enforces a strict schema. Anything
    else returns None, which the watcher reports as "could not check" and never
    as healthy.
"""

# Standard library
import asyncio
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass

# Internal
from app.core.logging import get_logger

logger = get_logger()

DEFAULT_DIR = "/app/logs/heartbeat"
TICK_SECONDS = 60.0
MAX_BYTES = 4096
NAME_RE = re.compile(r"^[a-z0-9-]+$")
STATES = frozenset({"running", "stopped", "done"})
_KEYS = frozenset({"alive_at", "progress_at", "progress_due_s", "state"})


def heartbeat_dir() -> str:
    return os.environ.get("HEARTBEAT_DIR") or DEFAULT_DIR


def _check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise ValueError("heartbeat name must match ^[a-z0-9-]+$")
    return name


@dataclass(frozen=True)
class Beat:
    alive_at: float
    progress_at: float
    progress_due_s: float
    state: str


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def read(name: str, directory: str | None = None) -> Beat | None:
    """The parsed heartbeat, or None for anything missing, oversized, malformed or off-schema."""
    path = os.path.join(directory or heartbeat_dir(), _check_name(name) + ".json")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != _KEYS:
        return None
    if not (_number(data["alive_at"]) and _number(data["progress_at"]) and _number(data["progress_due_s"])):
        return None
    if not isinstance(data["state"], str) or data["state"] not in STATES:
        return None
    return Beat(float(data["alive_at"]), float(data["progress_at"]), float(data["progress_due_s"]), data["state"])


class Heartbeat:
    def __init__(self, name: str, progress_due_s: float, directory: str | None = None) -> None:
        self.name = _check_name(name)
        self.progress_due_s = float(progress_due_s)
        self.directory = directory or heartbeat_dir()
        self.progress_at = time.time()
        self.done = False
        self._ticker: asyncio.Task | None = None
        self._warned = False

    def start(self) -> "Heartbeat":
        self._write("running")
        self._ticker = asyncio.get_running_loop().create_task(self._tick(), name=f"heartbeat-{self.name}")
        return self

    def progress(self) -> None:
        self.progress_at = time.time()

    def defer(self, seconds: float) -> None:
        """A deliberate sleep: the next progress is not due until it ends, plus progress_due_s."""
        self.progress_at = max(self.progress_at, time.time() + seconds)

    async def stop(self, state: str | None = "stopped") -> None:
        """Ends the ticker. state=None leaves the file as is, so a crash reads as dead."""
        if self._ticker is not None:
            self._ticker.cancel()
            await asyncio.gather(self._ticker, return_exceptions=True)
        if state is not None:
            self._write("done" if self.done else state)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            self._write("running")

    def _write(self, state: str) -> None:
        payload = {
            "alive_at": time.time(),
            "progress_at": self.progress_at,
            "progress_due_s": self.progress_due_s,
            "state": state,
        }
        tmp = None
        try:
            os.makedirs(self.directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{self.name}.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.chmod(tmp, 0o644)
            os.replace(tmp, os.path.join(self.directory, self.name + ".json"))
            tmp = None
        except OSError as exc:
            if not self._warned:
                self._warned = True
                logger.warning("Heartbeat: write failed", extra={"error_type": type(exc).__name__})
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
