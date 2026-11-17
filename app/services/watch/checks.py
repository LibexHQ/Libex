"""
The operational checks. They notify and never act.

Each check is on only when its own env is set and says so at startup; one that
cannot run says "could not check" once, never silently. Each alert fires once
per incident and is followed by "cleared" when it recovers.

Invariants:
  - Nothing here sends a log line, a query text, a URL or a token anywhere:
    errors are logged by type, the restart check counts startup lines and discards the
    lines, and the database checks read pg_stat_* views and never
    pg_stat_activity's statement text (SQL_STATEMENTS is asserted for that).
  - The database is reached through a private engine on WATCH_DATABASE_URL with
    one connection; app.db.session is never imported.
  - Direct egress only, no environment proxies, no redirects, bounded timeouts.
  - The restart check reads only the tail of the log files, read-only, and
    matches the whole line, so a request line that merely quotes the startup
    text is not a boot.
"""

# Standard library
import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

# Third party
import httpx
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

# Internal
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.backup.schedule import read_status, status_path
from app.services.watch import heartbeat
from app.services.watch.gotify import Notifier

logger = get_logger()

_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# Bounds a connect and each command, so a half-open connection reaches the unreachable alert.
_DB_TIMEOUT_S = 10.0

# The only statements this module sends to Postgres. None selects statement text.
SQL_BOOKS_TUPLES = (
    "SELECT n_live_tup, n_dead_tup, "
    "EXTRACT(EPOCH FROM (now() - last_autovacuum)) AS vacuum_age_s "
    "FROM pg_stat_user_tables WHERE schemaname = 'public' AND relname = 'books'"
)
SQL_BOOKS_AUTOVACUUM = (
    "SELECT EXTRACT(EPOCH FROM (now() - a.xact_start)) AS running_s "
    "FROM pg_stat_progress_vacuum p JOIN pg_stat_activity a ON a.pid = p.pid "
    "WHERE p.relid = 'public.books'::regclass AND a.backend_type = 'autovacuum worker'"
)
SQL_STATEMENTS = (SQL_BOOKS_TUPLES, SQL_BOOKS_AUTOVACUUM)

_ASCTIME = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}"
_ROTATED = re.compile(r"^libex\.log\.\d{4}-\d{2}-\d{2}$")


# --- configuration -----------------------------------------------------------

def _num(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number") from None


@dataclass(frozen=True)
class OpsConfig:
    """Each check is off until its own env is set.

    A threshold of 0 also turns its check off for exactly these five fields:
    backup_max_age_h, stats_zero_s, health_fails, restart_window_s, disk_pct.
    On every other threshold (restart_boots, heartbeat_dead_s, dead_tuple_pct,
    vacuum_max_age_h, vacuum_run_h, db_fails) 0 is a literal value, not "off",
    and db_fails is clamped to at least 1.
    """

    interval_s: float = 300.0
    libex_url: str | None = None
    backup_spool_dir: str | None = None
    backup_max_age_h: float = 26.0
    stats_zero_s: float = 1800.0
    health_fails: int = 3
    log_dir: str | None = None
    log_info_visible: bool = True
    restart_boots: int = 2
    restart_window_s: float = 1800.0
    log_tail_mib: float = 4.0
    heartbeat_name: str | None = None
    heartbeat_dir: str | None = None
    heartbeat_dead_s: float = 300.0
    disk_path: str | None = None
    disk_pct: float = 85.0
    database_url: str | None = None
    dead_tuple_pct: float = 20.0
    vacuum_max_age_h: float = 24.0
    vacuum_run_h: float = 6.0
    db_fails: int = 3

    @property
    def any_on(self) -> bool:
        return any(on for on in self.states().values())

    def states(self) -> dict[str, bool]:
        api = bool(self.libex_url)
        return {
            "backup_stale": bool(self.backup_spool_dir) and self.backup_max_age_h > 0,
            "stats_badges": api and self.stats_zero_s > 0,
            "api_health": api and self.health_fails > 0,
            "restart_loop": bool(self.log_dir) and self.log_info_visible and self.restart_window_s > 0,
            "worker_heartbeat": bool(self.heartbeat_name),
            "db_disk": bool(self.disk_path) and self.disk_pct > 0,
            "books_vacuum": bool(self.database_url),
        }


def load_ops_config(env: Mapping[str, str]) -> OpsConfig:
    level = logging.getLevelName((env.get("LOG_LEVEL") or "INFO").upper())
    name = env.get("WATCH_HEARTBEAT_NAME") or None
    if name is not None and not heartbeat.NAME_RE.fullmatch(name):
        raise SystemExit("WATCH_HEARTBEAT_NAME must match ^[a-z0-9-]+$")
    return OpsConfig(
        interval_s=max(1.0, _num(env, "WATCH_OPS_INTERVAL_S", 300)),
        libex_url=env.get("WATCH_LIBEX_URL") or None,
        backup_spool_dir=env.get("BACKUP_SPOOL_DIR") or None,
        backup_max_age_h=_num(env, "WATCH_BACKUP_MAX_AGE_H", 26),
        stats_zero_s=_num(env, "WATCH_STATS_ZERO_S", 1800),
        health_fails=int(_num(env, "WATCH_HEALTH_FAILS", 3)),
        log_dir=env.get("WATCH_LOG_DIR") or None,
        log_info_visible=not isinstance(level, int) or level <= logging.INFO,
        restart_boots=int(_num(env, "WATCH_RESTART_BOOTS", 2 * max(1, int(_num(env, "WEB_CONCURRENCY", 1))))),
        restart_window_s=_num(env, "WATCH_RESTART_WINDOW_S", 1800),
        log_tail_mib=max(0.1, _num(env, "WATCH_LOG_TAIL_MIB", 4)),
        heartbeat_name=name,
        heartbeat_dir=env.get("WATCH_HEARTBEAT_DIR") or None,
        heartbeat_dead_s=_num(env, "WATCH_HEARTBEAT_DEAD_S", 300),
        disk_path=env.get("WATCH_DISK_PATH") or None,
        disk_pct=_num(env, "WATCH_DISK_PCT", 85),
        database_url=env.get("WATCH_DATABASE_URL") or None,
        dead_tuple_pct=_num(env, "WATCH_DEAD_TUPLE_PCT", 20),
        vacuum_max_age_h=_num(env, "WATCH_VACUUM_MAX_AGE_H", 24),
        vacuum_run_h=_num(env, "WATCH_VACUUM_RUN_H", 6),
        db_fails=max(1, int(_num(env, "WATCH_DB_FAILS", 3))),
    )


def report_ops(cfg: OpsConfig, stack: str) -> None:
    """Says at startup which ops checks are on and which are off."""
    for name, on in cfg.states().items():
        logger.info("Watch: check %s" % ("on" if on else "off"), extra={"check": name, "stack": stack})


# --- one alert per incident --------------------------------------------------

def log_decision(stack: str, event: str, what: str, needs_you: bool) -> None:
    """The one log line per alert decision: the title only, never a body or a secret."""
    logger.log(
        logging.WARNING if event == "opened" and needs_you else logging.INFO,
        "Watch: incident %s" % event,
        extra={"stack": stack, "alert": what, "needs_you": needs_you},
    )


class Alerts:
    """An incident opens once, sends once, and sends "cleared" when it ends."""

    def __init__(self, notifier: Notifier, stack: str = "") -> None:
        self._notifier = notifier
        self._stack = stack
        self._open: set[str] = set()
        self._blind: set[str] = set()

    async def incident(self, key: str, bad: bool, what: str, happened: str, did: str, needs_you: bool) -> None:
        self.seen(key)
        if bad and key not in self._open:
            # Open only once delivered, so a failed send is retried next pass.
            if await self._notifier.send(what, happened, did, needs_you):
                self._open.add(key)
                log_decision(self._stack, "opened", what, needs_you)
        elif not bad and key in self._open:
            self._open.discard(key)
            log_decision(self._stack, "cleared", what, False)
            await self._notifier.send(f"cleared: {what}", "it recovered", "nothing further", False)

    async def blind(self, key: str, why: str) -> None:
        """A check that cannot run says so once per episode."""
        if key in self._blind:
            return
        logger.warning("Watch: could not check", extra={"check": key, "why": why})
        # Mark blind only once delivered, so a failed send is retried next pass.
        if await self._notifier.send(f"could not check {key}", why, "nothing, the check is blind", True):
            self._blind.add(key)

    def seen(self, key: str) -> None:
        self._blind.discard(key)

    def quiet(self, key: str) -> None:
        """Closes an incident without a message (a worker that stopped on purpose)."""
        self._open.discard(key)


# --- pure helpers ------------------------------------------------------------

def _tail_text(path: str, max_bytes: int) -> str:
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        handle.seek(max(0, size - max_bytes))
        raw = handle.read(max_bytes)
    body = raw.decode("utf-8", errors="replace")
    # A tail that starts mid-file begins mid-line; that line is not whole.
    return body.split("\n", 1)[1] if size > max_bytes and "\n" in body else body


def boot_count(body: str, version: str, since: datetime) -> int:
    """Whole-line startup records at or after `since` (naive local time).

    Lines, not distinct pids: a restarted container reuses its pids (a single
    worker is pid 1 every time), so only the line count sees a crash loop.
    """
    pattern = re.compile(
        rf"^({_ASCTIME}) - libex - INFO - pid=(\d+) - Libex {re.escape(version)} starting up$"
    )
    count = 0
    for line in body.split("\n"):
        match = pattern.match(line)
        if match and datetime.strptime(match.group(1)[:19], "%Y-%m-%d %H:%M:%S") >= since:
            count += 1
    return count


def _log_files(log_dir: str) -> list[str]:
    files = [os.path.join(log_dir, "libex.log")]
    rotated = sorted(n for n in os.listdir(log_dir) if _ROTATED.match(n))
    return files + [os.path.join(log_dir, rotated[-1])] if rotated else files


def boots_in_logs(cfg: OpsConfig, version: str) -> int:
    """Startup lines in the log tails; only the count leaves this function."""
    since = datetime.fromtimestamp(time.time() - cfg.restart_window_s)
    total = 0
    for path in _log_files(cfg.log_dir or ""):
        try:
            total += boot_count(_tail_text(path, int(cfg.log_tail_mib * 1024 * 1024)), version, since)
        except FileNotFoundError:
            continue
    return total


def disk_percent(path: str) -> float:
    """Used share as df reports it: used / (used + available to the caller)."""
    st = os.statvfs(path)
    used = st.f_blocks - st.f_bfree
    total = used + st.f_bavail
    return 100.0 * used / total if total else 0.0


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _db_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1) if url.startswith("postgresql://") else url


# --- the runner --------------------------------------------------------------

class OpsChecks:
    def __init__(self, cfg: OpsConfig, notifier: Notifier, stack: str) -> None:
        self.cfg = cfg
        self.alerts = Alerts(notifier, stack)
        self.stack = stack
        self.on = cfg.states()
        self.health_streak = 0
        self.health_ok = True
        self.zero_since: float | None = None
        self.db_streak = 0
        self.engine = None

    async def run_once(self, http: httpx.AsyncClient | None) -> None:
        """One pass. A check that raises is blind this pass; it never stops the others."""
        steps = (
            ("backup_stale", self.backup_stale),
            ("api_health", lambda: self.api_health(http)),
            ("stats_badges", lambda: self.stats_badges(http)),
            ("restart_loop", self.restart_loop),
            ("worker_heartbeat", self.worker_heartbeat),
            ("db_disk", self.db_disk),
            ("books_vacuum", self.books_vacuum),
        )
        for key, step in steps:
            if not self.on[key]:
                continue
            try:
                await step()
            except Exception as exc:
                logger.error("Watch: check failed", extra={"check": key, "error_type": type(exc).__name__})
                await self.alerts.blind(key, f"it raised {type(exc).__name__}")

    async def backup_stale(self) -> None:
        spool = self.cfg.backup_spool_dir or ""
        path = status_path(spool)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                json.load(handle)
        except (OSError, ValueError) as exc:
            await self.alerts.blind("backup_stale", f"the status file could not be read ({type(exc).__name__})")
            return
        self.alerts.seen("backup_stale")
        status = await asyncio.to_thread(read_status, spool)
        try:
            age_h = (datetime.now(timezone.utc) - _parse_ts(status.last_success_at)).total_seconds() / 3600
        except ValueError:
            age_h = float("inf")
        await self.alerts.incident(
            "backup_stale", age_h > self.cfg.backup_max_age_h,
            "backup stale",
            f"no successful backup in over {self.cfg.backup_max_age_h:g} hours"
            if age_h != float("inf") else "no successful backup is recorded",
            "nothing", True,
        )

    async def api_health(self, http: httpx.AsyncClient | None) -> None:
        ok = False
        try:
            ok = http is not None and (await http.get("/health")).is_success
        except Exception as exc:
            logger.info("Watch: health probe failed", extra={"error_type": type(exc).__name__})
        self.health_streak = 0 if ok else self.health_streak + 1
        self.health_ok = ok
        await self.alerts.incident(
            "api_health", self.health_streak >= self.cfg.health_fails,
            "API unhealthy", f"/health has failed {self.health_streak} times in a row", "nothing", True,
        )

    async def stats_badges(self, http: httpx.AsyncClient | None) -> None:
        if not self.health_ok:
            return  # an API that is down is the health check's alert, not a blind stats check
        response = await http.get("/db/stats")  # type: ignore[union-attr]
        response.raise_for_status()
        data = response.json()
        books = data.get("books")
        if not isinstance(books, int) or isinstance(books, bool):
            raise ValueError("unexpected stats shape")
        self.alerts.seen("stats_badges")
        now = time.monotonic()
        if books == 0:
            self.zero_since = self.zero_since if self.zero_since is not None else now
        else:
            self.zero_since = None
        zero_for = 0.0 if self.zero_since is None else now - self.zero_since
        await self.alerts.incident(
            "stats_badges", zero_for > self.cfg.stats_zero_s,
            "books badge at zero",
            f"/db/stats has reported zero books for over {self.cfg.stats_zero_s / 60:g} minutes",
            "nothing", True,
        )

    async def restart_loop(self) -> None:
        version = get_settings().app_version
        boots = await asyncio.to_thread(boots_in_logs, self.cfg, version)
        self.alerts.seen("restart_loop")
        await self.alerts.incident(
            "restart_loop", boots > self.cfg.restart_boots,
            "API restarting",
            f"{boots} startups were logged in {self.cfg.restart_window_s / 60:g} minutes "
            f"(limit {self.cfg.restart_boots})",
            "nothing", True,
        )

    async def worker_heartbeat(self) -> None:
        name = self.cfg.heartbeat_name or ""
        beat = await asyncio.to_thread(heartbeat.read, name, self.cfg.heartbeat_dir)
        if beat is None:
            await self.alerts.blind("worker_heartbeat", "the heartbeat file is missing or not valid")
            return
        self.alerts.seen("worker_heartbeat")
        if beat.state != "running":
            self.alerts.quiet(f"{name}-dead")
            self.alerts.quiet(f"{name}-stalled")
            return
        now = time.time()
        dead = now - beat.alive_at > self.cfg.heartbeat_dead_s
        stalled = not dead and now - beat.progress_at > beat.progress_due_s
        await self.alerts.incident(
            f"{name}-dead", dead, f"{name} dead",
            f"{name} has not refreshed its heartbeat for over {self.cfg.heartbeat_dead_s / 60:g} minutes",
            "nothing, no restart is attempted", True,
        )
        await self.alerts.incident(
            f"{name}-stalled", stalled, f"{name} stalled",
            f"{name} is alive but has made no progress for over {beat.progress_due_s / 3600:.1f} hours",
            "nothing", True,
        )

    async def db_disk(self) -> None:
        pct = await asyncio.to_thread(disk_percent, self.cfg.disk_path or "")
        self.alerts.seen("db_disk")
        await self.alerts.incident(
            "db_disk", pct > self.cfg.disk_pct, "database disk filling",
            f"the database filesystem is {pct:.0f}% used (limit {self.cfg.disk_pct:g}%)", "nothing", True,
        )

    async def books_vacuum(self) -> None:
        try:
            if self.engine is None:
                self.engine = create_async_engine(
                    _db_url(self.cfg.database_url or ""), pool_size=1, max_overflow=0, echo=False,
                    connect_args={"timeout": _DB_TIMEOUT_S, "command_timeout": _DB_TIMEOUT_S},
                )
            async with self.engine.connect() as conn:
                tuples = (await conn.execute(text(SQL_BOOKS_TUPLES))).one_or_none()
                running = (await conn.execute(text(SQL_BOOKS_AUTOVACUUM))).scalars().all()
        except (OSError, asyncio.TimeoutError, OperationalError, InterfaceError) as exc:
            logger.warning("Watch: database unreachable", extra={"error_type": type(exc).__name__})
            self.db_streak += 1
            await self.alerts.incident(
                "postgres_unreachable", self.db_streak >= self.cfg.db_fails,
                "Postgres unreachable",
                f"the watcher could not reach Postgres for {self.db_streak} checks in a row", "nothing", True,
            )
            return
        self.db_streak = 0
        await self.alerts.incident("postgres_unreachable", False, "Postgres unreachable", "", "", False)
        self.alerts.seen("books_vacuum")
        if tuples is None:
            await self.alerts.blind("books_vacuum", "the books table is not in pg_stat_user_tables")
            return
        live, dead, vacuum_age = tuples
        dead_pct = 100.0 * dead / live if live else (100.0 if dead else 0.0)
        no_recent = vacuum_age is None or vacuum_age > self.cfg.vacuum_max_age_h * 3600
        await self.alerts.incident(
            "books_dead_tuples", dead_pct > self.cfg.dead_tuple_pct and no_recent,
            "books table bloating",
            f"dead tuples are {dead_pct:.0f}% of live and autovacuum has not run in over "
            f"{self.cfg.vacuum_max_age_h:g} hours", "nothing", True,
        )
        longest = max((float(s) for s in running if s is not None), default=0.0)
        await self.alerts.incident(
            "books_autovacuum_stuck", longest > self.cfg.vacuum_run_h * 3600,
            "books autovacuum stuck",
            f"an autovacuum on books has been running over {self.cfg.vacuum_run_h:g} hours", "nothing", True,
        )

    async def aclose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()


async def run_ops(cfg: OpsConfig, notifier: Notifier, stack: str, stop: asyncio.Event) -> None:
    checks = OpsChecks(cfg, notifier, stack)
    http = (
        httpx.AsyncClient(base_url=cfg.libex_url, timeout=_HTTP_TIMEOUT, follow_redirects=False, trust_env=False)
        if cfg.libex_url else None
    )
    try:
        while not stop.is_set():
            await checks.run_once(http)
            try:
                await asyncio.wait_for(stop.wait(), cfg.interval_s)
            except asyncio.TimeoutError:
                pass
    finally:
        if http is not None:
            await http.aclose()
        await checks.aclose()
