"""Multi-session WeChat bridge entrypoint.

Run with:

    python -m planagent.wechat.bridge

It:
  1. Loads `~/.planagent/*.json` credentials.
  2. Runs alembic migrations against the configured DB.
  3. Bootstraps one GroupContext + one BotSession per credential file.
  4. Starts a scheduler tick loop (reminders + LLM keep-alive) and a
     multi-session polling loop inside an `asyncio.TaskGroup`.
  5. Graceful shutdown on SIGTERM / SIGINT.

This is the "production" entrypoint; the FastAPI `main.py` remains the
REST surface for the frontend.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass

from sqlalchemy import select

from planagent import db as db_mod
from planagent.agent.wechat_bridge import build_handler_for_session
from planagent.config import get_settings
from planagent.db.models import BotSession
from planagent.llm.deepseek import DeepSeekClient
from planagent.main import run_migrations
from planagent.scheduler.scheduler import Scheduler
from planagent.wechat.client import ClawBotClient
from planagent.wechat.protocol import InboundMessage
from planagent.wechat.runtime import SessionPollSpec, run_all_sessions
from planagent.wechat.sessions import BootstrapService, load_all_sessions

log = logging.getLogger(__name__)


@dataclass
class BridgeArgs:
    scheduler_interval_s: int = 300


async def _per_session_send(client: ClawBotClient):
    """Return a 4-arg WechatSend bound to `client`."""
    async def _send(
        bot_token: str, to_user_id: str, text: str, context_token: str | None
    ) -> None:
        await client.send_text(
            bot_token,
            to_user_id=to_user_id,
            text=text,
            context_token=context_token or "",
            group_id=None,
        )

    return _send


async def _run(args: BridgeArgs) -> None:
    settings = get_settings()
    run_migrations(settings.db_url)
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
    log.info("starting bridge with %d session(s)", len(specs))

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        # Not available on Windows / some test harnesses.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(getattr(signal, sig_name), stop.set)

    deepseek = DeepSeekClient()
    client = ClawBotClient()

    send = await _per_session_send(client)
    scheduler = Scheduler(sm, deepseek, send)  # type: ignore[arg-type]

    async def _route(spec: SessionPollSpec, msg: InboundMessage) -> None:
        handler = build_handler_for_session(
            deepseek=deepseek,
            session_factory=sm,
            client=client,
            bot_session=spec,
        )
        await handler(msg)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                scheduler.run(interval_s=args.scheduler_interval_s, stop_event=stop),
                name="scheduler",
            )
            tg.create_task(
                run_all_sessions(specs, _route, client=client, stop_event=stop),
                name="poller",
            )

            async def _waiter() -> None:
                await stop.wait()

            tg.create_task(_waiter(), name="stop-waiter")
    finally:
        await client.aclose()
        await db_mod.dispose_engine()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="planagent multi-session WeChat bridge")
    parser.add_argument(
        "--scheduler-interval-s",
        type=int,
        default=300,
        help="Scheduler tick interval in seconds (default: 300).",
    )
    ns = parser.parse_args()
    asyncio.run(_run(BridgeArgs(scheduler_interval_s=ns.scheduler_interval_s)))


if __name__ == "__main__":
    main()
