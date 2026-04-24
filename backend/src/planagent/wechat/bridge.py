"""Multi-session WeChat bridge entrypoint.

Run with:

    python -m planagent.wechat.bridge

It:
  1. Loads `~/.planagent/*.json` credentials.
  2. Runs alembic migrations against the configured DB.
  3. Bootstraps one GroupContext + one BotSession per credential file.
  4. Starts a scheduler tick loop (reminders + LLM keep-alive) and a
     multi-session polling loop inside an `asyncio.TaskGroup`.
  5. Graceful shutdown on SIGTERM / SIGINT, bounded by ``SHUTDOWN_TIMEOUT_S``.

A `--health-check` subcommand reads the running bridge's PID file
(`/tmp/planagent-bridge.pid`) and prints a JSON summary + exits 0/1.

This is the "production" entrypoint; the FastAPI `main.py` remains the
REST surface for the frontend.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from planagent import db as db_mod
from planagent.agent.wechat_bridge import build_handler_for_session
from planagent.config import get_settings
from planagent.db.models import BotSession, PendingOutbound, PendingOutboundStatus
from planagent.llm.deepseek import DeepSeekClient
from planagent.logutil import log_event, setup_json_logging
from planagent.main import run_migrations
from planagent.scheduler.scheduler import Scheduler
from planagent.wechat.client import ClawBotClient
from planagent.wechat.protocol import InboundMessage
from planagent.wechat.runtime import SessionPollSpec, run_all_sessions
from planagent.wechat.sessions import BootstrapService, load_all_sessions

log = logging.getLogger(__name__)

PID_FILE = Path("/tmp/planagent-bridge.pid")
SHUTDOWN_TIMEOUT_S = 5.0


@dataclass
class BridgeArgs:
    scheduler_interval_s: int = 300


async def _per_session_send(
    client: ClawBotClient,
    *,
    session_names_by_token: dict[str, str] | None = None,
):
    """Return a 4-arg WechatSend bound to `client`.

    The resulting callable is what the Scheduler uses for reminder +
    wake-up deliveries. Each successful send emits a structured
    `outbound_sent` event tagged with the originating session's name
    when we can resolve it from the bot_token.
    """
    names = session_names_by_token or {}

    async def _send(
        bot_token: str, to_user_id: str, text: str, context_token: str | None
    ) -> None:
        resp = await client.send_text(
            bot_token,
            to_user_id=to_user_id,
            text=text,
            context_token=context_token or "",
            group_id=None,
        )
        if getattr(resp, "ret", 0) == 0:
            client_id = ""
            extra = getattr(resp, "__pydantic_extra__", None) or {}
            if isinstance(extra, dict):
                client_id = extra.get("client_id") or ""
            from planagent.logutil import log_outbound_sent

            log_outbound_sent(
                session_name=names.get(bot_token, "scheduler"),
                target_user_id=to_user_id,
                text=text,
                client_id=client_id,
            )

    return _send


# --- PID file helpers -------------------------------------------------------


def _write_pid_file(path: Path | None = None) -> None:
    target = path or PID_FILE
    try:
        target.write_text(str(os.getpid()))
    except OSError as exc:
        log.warning("could not write pid file %s: %s", target, exc)


def _remove_pid_file(path: Path | None = None) -> None:
    target = path or PID_FILE
    with contextlib.suppress(FileNotFoundError, OSError):
        target.unlink()


def _read_pid_from_file(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_is_alive(pid: int) -> bool:
    """Signal-0 probe. True only if the process exists AND we can signal it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but not ours — treat as alive.
        return True
    except OSError:
        return False
    return True


# --- Health check -----------------------------------------------------------


async def _gather_health_snapshot(start_ts: float) -> dict[str, object]:
    """Best-effort DB snapshot for the health endpoint.

    Runs against the configured DB — this is why the health subcommand
    stands up its own engine rather than trying to introspect the
    running process. We never let a DB error bubble out: if anything
    goes wrong, the relevant fields are omitted and `db_error` is set.
    """
    snapshot: dict[str, object] = {
        "uptime_seconds": max(0.0, time.monotonic() - start_ts),
        "now": datetime.now(UTC).isoformat(timespec="seconds"),
        "sessions": [],
        "num_sessions": 0,
        "open_pending_outbounds": 0,
    }
    try:
        settings = get_settings()
        db_mod.init_engine(settings.db_url)
        sm = db_mod.get_sessionmaker()
        async with sm() as session:
            rows = (await session.execute(select(BotSession))).scalars().all()
            sessions_payload = []
            for r in rows:
                sessions_payload.append(
                    {
                        "name": r.name,
                        "wechat_user_id": r.wechat_user_id or "",
                        "last_inbound_at": r.last_inbound_at.isoformat()
                        if r.last_inbound_at
                        else None,
                        "last_outbound_at": r.last_outbound_at.isoformat()
                        if r.last_outbound_at
                        else None,
                    }
                )
            snapshot["sessions"] = sessions_payload
            snapshot["num_sessions"] = len(sessions_payload)
            pending = await session.execute(
                select(PendingOutbound).where(
                    PendingOutbound.status == PendingOutboundStatus.pending
                )
            )
            snapshot["open_pending_outbounds"] = len(list(pending.scalars().all()))
    except Exception as exc:  # noqa: BLE001
        snapshot["db_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await db_mod.dispose_engine()
    return snapshot


def run_health_check(pid_file: Path = PID_FILE) -> int:
    """Print a JSON health summary. Return 0 if the bridge is alive, 1 otherwise.

    "Alive" means the PID file exists and points at a live process. We
    still print the snapshot even when the bridge is down so an operator
    can at least see DB-side state.
    """
    pid = _read_pid_from_file(pid_file)
    alive = pid is not None and _pid_is_alive(pid)
    start_ts = time.monotonic()
    snapshot = asyncio.run(_gather_health_snapshot(start_ts))
    snapshot["pid"] = pid
    snapshot["alive"] = alive
    snapshot["scheduler_alive"] = alive  # bridge + scheduler share a process
    print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
    return 0 if alive else 1


# --- Main run loop ----------------------------------------------------------


async def _run(args: BridgeArgs) -> None:
    settings = get_settings()
    run_migrations(settings.db_url)
    # Alembic's env.py calls `logging.config.fileConfig(...)`, which wipes
    # our JSON formatter off the root handler. Re-install it.
    setup_json_logging(level=logging.INFO)
    db_mod.init_engine(settings.db_url)
    sm = db_mod.get_sessionmaker()

    # Bootstrap: sync credential files → DB.
    creds = load_all_sessions()
    if not creds:
        log.warning("no credential files found in ~/.planagent/; nothing to do")
    bootstrap = BootstrapService(sm)
    await bootstrap.sync_sessions_to_db()

    # Read back active sessions.
    async with sm() as session:
        rows = (await session.execute(select(BotSession))).scalars().all()
        specs = [
            SessionPollSpec(session_id=r.id, bot_token=r.bot_token, name=r.name)
            for r in rows
        ]
        names_by_token = {r.bot_token: r.name for r in rows}
    log_event("bridge_startup", num_sessions=len(specs))
    log.info("starting bridge with %d session(s)", len(specs))

    # PID file: written on startup, removed in finally below.
    _write_pid_file()

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()

    def _signal_stop(sig_name: str) -> None:
        log_event("bridge_signal", signal=sig_name)
        stop.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        # Not available on Windows / some test harnesses.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(
                getattr(signal, sig_name), _signal_stop, sig_name
            )

    deepseek = DeepSeekClient()
    client = ClawBotClient()

    send = await _per_session_send(client, session_names_by_token=names_by_token)
    scheduler = Scheduler(sm, deepseek, send)  # type: ignore[arg-type]

    async def _route(spec: SessionPollSpec, msg: InboundMessage) -> None:
        handler = build_handler_for_session(
            deepseek=deepseek,
            session_factory=sm,
            client=client,
            bot_session=spec,
        )
        try:
            await handler(msg)
        except Exception as exc:  # noqa: BLE001 — resilience is the goal
            # Never let a bad inbound kill the bridge. The runtime's
            # per-message try/except already guards this, but we log a
            # structured `handler_failed` event before re-raising so
            # errors still show up for the outer `on_message` logger too.
            from planagent.logutil import log_handler_failed

            log_handler_failed(
                session_name=spec.name,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            # Swallow: the runtime loop logs at exception level already,
            # and re-raising here would be redundant noise.

    try:
        # We deliberately don't use a TaskGroup here — a TaskGroup waits
        # for every child to finish on its own, which would stall when
        # the poller is blocked inside a 60 s long-poll. Instead we drive
        # two tasks explicitly and cancel them once `stop` flips, giving
        # us a hard SHUTDOWN_TIMEOUT_S ceiling on shutdown latency.
        scheduler_task = asyncio.create_task(
            scheduler.run(interval_s=args.scheduler_interval_s, stop_event=stop),
            name="scheduler",
        )
        poller_task = asyncio.create_task(
            run_all_sessions(specs, _route, client=client, stop_event=stop),
            name="poller",
        )
        tasks = [scheduler_task, poller_task]
        try:
            await stop.wait()
        finally:
            log_event(
                "bridge_shutdown_begin",
                timeout_seconds=SHUTDOWN_TIMEOUT_S,
            )
            # Cancel any in-flight long-poll / DB await so everyone gets
            # a CancelledError and unwinds their finally blocks.
            for t in tasks:
                t.cancel()
            # Drain with a hard deadline.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=SHUTDOWN_TIMEOUT_S,
                )
    finally:
        _remove_pid_file()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.aclose(), timeout=SHUTDOWN_TIMEOUT_S)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                db_mod.dispose_engine(), timeout=SHUTDOWN_TIMEOUT_S
            )
        log_event("bridge_shutdown_complete")


def main(argv: list[str] | None = None) -> int:
    global PID_FILE

    # Opt-in daily-rotated file logs (see README "Log rotation"). Defaults
    # to stderr-only so local `python -m ...` runs stay quiet on disk.
    enable_file = os.environ.get("PLANAGENT_LOG_TO_FILE", "") in {"1", "true", "yes"}
    setup_json_logging(level=logging.INFO, enable_file_rotation=enable_file)
    parser = argparse.ArgumentParser(description="planagent multi-session WeChat bridge")
    parser.add_argument(
        "--scheduler-interval-s",
        type=int,
        default=300,
        help="Scheduler tick interval in seconds (default: 300).",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help=(
            "Print a JSON health summary for the running bridge "
            f"(PID file at {PID_FILE}) and exit 0 if alive, 1 otherwise."
        ),
    )
    parser.add_argument(
        "--pid-file",
        default=str(PID_FILE),
        help=(
            "Override the bridge PID file path. Primarily for tests; "
            "production should use the default."
        ),
    )
    ns = parser.parse_args(argv)

    if ns.health_check:
        return run_health_check(Path(ns.pid_file))

    # Non-health path: stamp the chosen pid file into the module global so
    # `_run` picks it up.
    PID_FILE = Path(ns.pid_file)
    try:
        asyncio.run(_run(BridgeArgs(scheduler_interval_s=ns.scheduler_interval_s)))
    except KeyboardInterrupt:
        log_event("bridge_keyboard_interrupt")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
