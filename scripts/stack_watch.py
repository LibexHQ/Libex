"""
Stack watcher entry point: one sidecar per stack, in the Libex image.

Probes the stack's Audible exit, rotates it through the stack's own gluetun
control server when it is blocked, and sends the operator a Gotify message on
every transition. It never fixes anything in the database and never restarts a
container; the control server is its only actuator.

RUN IT as a sidecar with `entrypoint: []` -- docker-entrypoint.sh would run
the migrations first, which this process has no business doing.

    python -m scripts.stack_watch

Configuration is the on switch: a check runs only when its env is set, and the
startup report says which are on and which are off. Read from the environment,
not Settings.

    GOTIFY_URL, GOTIFY_TOKEN   required; the watcher refuses to start without them.
    WATCH_STACK                name used in every message (default "stack")
    AUDIBLE_PROXY_URL          turns the exit probe on; its host is also the
                               gluetun control host
    GLUETUN_API_KEY            turns rotation on (with AUDIBLE_PROXY_URL)
    WATCH_CONTROL_PORT         8000
    WATCH_INTERVAL_S           120
    WATCH_FAILS_BEFORE_ROTATE  3      consecutive bad cycles; never below 3
    WATCH_MAX_ROTATIONS        3      per incident, then the cooldown
    WATCH_COOLDOWN_S           21600
    WATCH_LIBEX_URL            turns the app probe on (API stack only), and the
                               stats and health checks with it
    The operational checks (app/services/watch/checks.py) each turn on with their own
    env and read their thresholds from env; see OpsConfig there:
    BACKUP_SPOOL_DIR, WATCH_LOG_DIR, WATCH_HEARTBEAT_NAME, WATCH_DISK_PATH,
    WATCH_DATABASE_URL, WATCH_OPS_INTERVAL_S (300)
    LOG_LEVEL                  INFO
"""

# Standard library
import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Mapping

# Core
from app.core.logging import get_logger, setup_logging

# Services
from app.services.watch.checks import OpsConfig, load_ops_config, log_decision, report_ops, run_ops
from app.services.watch.gluetun import (
    CONTROL_PORT_DEFAULT,
    GluetunAuthError,
    GluetunControl,
    control_host,
    rotate,
)
from app.services.watch.gotify import Notifier, validate_url
from app.services.watch.probes import probe_app, probe_exit

logger = get_logger()

MIN_FAILS_BEFORE_ROTATE = 3


# --- configuration -----------------------------------------------------------

def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer") from None


@dataclass(frozen=True)
class WatchConfig:
    stack: str
    gotify_url: str
    gotify_token: str
    proxy_url: str | None
    control_host: str | None
    control_port: int
    gluetun_key: str | None
    libex_url: str | None
    interval_s: int
    fails_before_rotate: int
    max_rotations: int
    cooldown_s: int
    ops: OpsConfig = field(default_factory=OpsConfig)

    @property
    def rotation_on(self) -> bool:
        return bool(self.proxy_url and self.gluetun_key)


def load_config(env: Mapping[str, str]) -> WatchConfig:
    """Raises SystemExit, never echoing a value, when the config cannot be run."""
    gotify_url = env.get("GOTIFY_URL", "")
    gotify_token = env.get("GOTIFY_TOKEN", "")
    if not gotify_url or not gotify_token:
        raise SystemExit("GOTIFY_URL and GOTIFY_TOKEN are required")
    try:
        validate_url(gotify_url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    proxy_url = env.get("AUDIBLE_PROXY_URL") or None
    host = None
    if proxy_url:
        try:
            host = control_host(proxy_url)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None

    return WatchConfig(
        stack=env.get("WATCH_STACK") or "stack",
        gotify_url=gotify_url,
        gotify_token=gotify_token,
        proxy_url=proxy_url,
        control_host=host,
        control_port=_int(env, "WATCH_CONTROL_PORT", CONTROL_PORT_DEFAULT),
        gluetun_key=env.get("GLUETUN_API_KEY") or None,
        libex_url=env.get("WATCH_LIBEX_URL") or None,
        interval_s=max(1, _int(env, "WATCH_INTERVAL_S", 120)),
        fails_before_rotate=max(MIN_FAILS_BEFORE_ROTATE, _int(env, "WATCH_FAILS_BEFORE_ROTATE", 3)),
        max_rotations=max(1, _int(env, "WATCH_MAX_ROTATIONS", 3)),
        cooldown_s=max(0, _int(env, "WATCH_COOLDOWN_S", 21600)),
        ops=load_ops_config(env),
    )


def report_config(cfg: WatchConfig) -> None:
    """Says at startup which checks are on and which are off."""
    checks = {
        "exit_probe": bool(cfg.proxy_url),
        "rotation": cfg.rotation_on,
        "app_probe": bool(cfg.libex_url),
    }
    for name, on in checks.items():
        logger.info(
            "Watch: check %s" % ("on" if on else "off"),
            extra={"check": name, "stack": cfg.stack},
        )
    report_ops(cfg.ops, cfg.stack)
    logger.info(
        "Watch: starting",
        extra={
            "stack": cfg.stack,
            "interval_s": cfg.interval_s,
            "fails_before_rotate": cfg.fails_before_rotate,
            "max_rotations": cfg.max_rotations,
            "cooldown_s": cfg.cooldown_s,
        },
    )


# --- the decision table ------------------------------------------------------

class Watcher:
    """
    Holds the streaks and the incident state; cycle() is one pass of the table.

        exit blocked, app failing (or no app probe)  rotate; alert "exit blocked"
        exit ok, app failing                         alert "Libex stuck"; no action
        exit blocked, app ok                         one alert; no action
        everything ok after an open incident         alert "cleared"

    A streak must reach K cycles to count, and a 5xx or a 404 never reaches the
    exit streak at all (see probes.classify). With an app probe, a streak that
    is neither zero nor K holds the decision for another cycle.
    """

    def __init__(self, cfg: WatchConfig, notifier: Notifier, stop: asyncio.Event,
                 control: GluetunControl | None) -> None:
        self.cfg = cfg
        self.notifier = notifier
        self.stop = stop
        self.control = control
        self.exit_streak = 0
        self.app_streak = 0
        self.alerted: set[str] = set()
        self.rotations = 0
        self.cooldown_until: float | None = None
        self.rotation_disabled = False

    async def _alert(self, key: str, what: str, happened: str, did: str, needs_you: bool) -> None:
        if key in self.alerted:
            return
        # Marked only once delivered, so a failed send is retried next cycle.
        if await self.notifier.send(what, happened, did, needs_you):
            self.alerted.add(key)
            log_decision(self.cfg.stack, "opened", what, needs_you)

    async def check_vpn_running(self) -> None:
        if self.control is None or self.rotation_disabled:
            return
        try:
            if await self.control.status() != "stopped":
                return
            await self.control.ensure_running()
        except GluetunAuthError:
            await self._key_rejected()
            return
        except Exception as exc:
            logger.warning("Watch: VPN status check failed", extra={"error_type": type(exc).__name__})
            return
        logger.warning("Watch: VPN was stopped, started it")
        await self.notifier.send(
            "VPN was stopped",
            "the VPN was found stopped",
            "started it",
            False,
        )

    async def _key_rejected(self) -> None:
        self.rotation_disabled = True
        await self._alert(
            "key", "control key rejected",
            "the gluetun control server answered 401",
            "rotation is off for this process; probing continues", True,
        )

    async def cycle(self) -> None:
        cfg = self.cfg
        await self.check_vpn_running()

        exit_known = bool(cfg.proxy_url)
        if exit_known:
            probe = await probe_exit(cfg.proxy_url)
            self.exit_streak = self.exit_streak + 1 if probe.blocked else 0
            logger.info(
                "Watch: exit probe",
                extra={"stack": cfg.stack, "blocked": probe.blocked, "streak": self.exit_streak},
            )
        app_known = bool(cfg.libex_url)
        if app_known:
            app_ok = await probe_app(cfg.libex_url)
            self.app_streak = 0 if app_ok else self.app_streak + 1

        k = cfg.fails_before_rotate
        exit_bad = exit_known and self.exit_streak >= k
        app_bad = app_known and self.app_streak >= k
        app_holding = app_known and 0 < self.app_streak < k

        if exit_bad and not app_holding:
            if app_known and not app_bad:
                await self._alert(
                    "exit-only", "exit probe failing, Libex serving",
                    "the exit probe has failed for several cycles while the app answers",
                    "nothing", False,
                )
            else:
                await self._rotate_or_escalate()
        elif app_bad and not exit_bad:
            await self._stuck()
        elif self.alerted and self.exit_streak == 0 and self.app_streak == 0:
            log_decision(cfg.stack, "cleared", "cleared", False)
            await self.notifier.send(
                "cleared",
                "the exit probe and the app both answer again"
                + ("; rotation is still off until the watcher restarts" if self.rotation_disabled else ""),
                "nothing further", False,
            )
            self.alerted.clear()
            self.rotations = 0
            self.cooldown_until = None

    async def _stuck(self) -> None:
        await self._alert(
            "stuck", "Libex stuck",
            "the exit is fine but Libex's own search keeps failing",
            "nothing, no recovery action is taken", True,
        )

    async def _rotate_or_escalate(self) -> None:
        cfg = self.cfg
        if self.control is None or self.rotation_disabled:
            await self._alert(
                "no-rotate", "exit blocked",
                "the exit has been blocked for several cycles",
                "nothing, rotation is not available", True,
            )
            return
        now = time.monotonic()
        if self.cooldown_until is not None and now >= self.cooldown_until:
            self.rotations = 0
            self.cooldown_until = None
            self.alerted.discard("escalated")
        if self.rotations >= cfg.max_rotations:
            if self.cooldown_until is None:
                self.cooldown_until = now + cfg.cooldown_s
            await self._alert(
                "escalated", "still blocked",
                f"the exit is still blocked after {self.rotations} rotations",
                "stopped rotating until the cooldown ends", True,
            )
            return

        await self._alert(
            "blocked", "exit blocked",
            "the exit has been blocked for several cycles", "rotating the exit", False,
        )
        self.rotations += 1
        try:
            result = await rotate(self.control, self.stop)
        except GluetunAuthError:
            await self._key_rejected()
            return
        except Exception as exc:
            logger.error("Watch: rotation failed", extra={"error_type": type(exc).__name__})
            try:
                running = await self.control.status() == "running"
            except Exception:
                running = False
            await self.notifier.send(
                "rotation failed", "a rotation raised an error",
                "the VPN is running again" if running
                else "tried to start the VPN, but it is not confirmed running",
                not running,
            )
            return
        logger.info(
            "Watch: rotation finished",
            extra={"stack": cfg.stack, "ok": result.ok, "rotations": self.rotations},
        )
        await self.notifier.send(
            "rotated" if result.ok else "rotation failed",
            f"rotation {self.rotations} of {cfg.max_rotations}",
            f"exit {result.old_ip or '?'} -> {result.new_ip or '?'}"
            + ("" if result.ok else ", no new exit"),
            False,
        )
        if result.ok and not self.stop.is_set():
            self.exit_streak = 0 if not (await probe_exit(cfg.proxy_url)).blocked else self.exit_streak


# --- the run -----------------------------------------------------------------

def _ops_tasks(cfg: WatchConfig, notifier: Notifier, stop: asyncio.Event) -> list[asyncio.Task]:
    """The operational checks as one running task; it ends when `stop` is set."""
    if not cfg.ops.any_on:
        return []
    return [asyncio.create_task(run_ops(cfg.ops, notifier, cfg.stack, stop), name="watch-ops")]


async def _run(cfg: WatchConfig) -> int:
    report_config(cfg)
    notifier = Notifier(cfg.gotify_url, cfg.gotify_token, cfg.stack)
    control = (
        GluetunControl(cfg.control_host, cfg.control_port, cfg.gluetun_key)
        if cfg.rotation_on else None
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # loop.add_signal_handler, not signal.signal: the loop sleeps between
    # cycles, and only the loop's own handler wakes it at once.
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)

    watcher = Watcher(cfg, notifier, stop, control)
    ops = _ops_tasks(cfg, notifier, stop)
    code = 0
    try:
        while not stop.is_set():
            try:
                await watcher.cycle()
            except Exception as exc:
                logger.error("Watch: cycle failed", extra={"error_type": type(exc).__name__})
            try:
                await asyncio.wait_for(stop.wait(), cfg.interval_s)
            except asyncio.TimeoutError:
                pass
    except BaseException:
        code = 1
        raise
    finally:
        # The VPN is left running if this process was the one that stopped it.
        if control is not None and control.we_stopped:
            try:
                await control.ensure_running()
            except Exception as exc:
                logger.error("Watch: could not restart the VPN", extra={"error_type": type(exc).__name__})
                code = 1
        for task in ops:
            task.cancel()
        await asyncio.gather(*ops, return_exceptions=True)
        if control is not None:
            await control.aclose()
        await notifier.aclose()
        logger.info("Watch: stopped", extra={"stop_requested": stop.is_set()})
    return code


def main() -> None:
    # get_logger only fetches the logger; without this nothing is emitted.
    setup_logging()
    cfg = load_config(os.environ)
    exit_code = asyncio.run(_run(cfg))
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
