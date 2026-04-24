"""Long-poll runtime loop: pumps messages into a user-supplied handler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from planagent.logutil import log_handler_failed

from .client import ClawBotClient, ClawBotError
from .protocol import InboundMessage

log = logging.getLogger(__name__)

MessageHandler = Callable[[InboundMessage], Awaitable[None]]


@dataclass
class SessionPollSpec:
    """Minimal projection of BotSession needed by the polling loop.

    We don't take the ORM model directly so tests can construct specs
    without standing up a DB.
    """

    session_id: str
    bot_token: str
    name: str


SessionHandler = Callable[[SessionPollSpec, InboundMessage], Awaitable[None]]


async def run_polling_loop(
    on_message: MessageHandler,
    bot_token: str,
    *,
    client: ClawBotClient | None = None,
    stop_event: asyncio.Event | None = None,
    backoff_s: float = 2.0,
    session_name: str = "",
) -> None:
    """Loop forever (or until `stop_event`) dispatching messages to `on_message`.

    - Threads the cursor (`get_updates_buf`) across calls.
    - Isolates handler exceptions so one bad message can't kill the loop
      (a DB or API error in the handler emits `handler_failed` and we
      move on to the next inbound — the process survives).
    - On transport errors, logs and retries after a short backoff.
    """
    owns_client = client is None
    c = client or ClawBotClient()
    cursor = ""
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                resp = await c.long_poll(bot_token, cursor=cursor)
            except (httpx.HTTPError, TimeoutError, ClawBotError, OSError) as exc:
                log.warning("long_poll failed: %s", exc)
                await asyncio.sleep(backoff_s)
                continue

            cursor = resp.get_updates_buf or cursor

            for msg in resp.msgs:
                try:
                    await on_message(msg)
                except Exception as exc:  # noqa: BLE001 — handler must not kill loop
                    log.exception("on_message handler raised")
                    log_handler_failed(
                        session_name=session_name,
                        error=str(exc),
                        exc_type=type(exc).__name__,
                    )
    finally:
        if owns_client:
            await c.aclose()


async def run_all_sessions(
    sessions: list[SessionPollSpec],
    on_message: SessionHandler,
    *,
    client: ClawBotClient | None = None,
    stop_event: asyncio.Event | None = None,
    backoff_s: float = 2.0,
) -> None:
    """Drive one polling loop per session concurrently, sharing one client.

    A single HTTP client is reused across all sessions (httpx connection
    pool). Each session threads its own bot_token into every request, so
    they're fully independent on the wire. The `on_message` callback is
    invoked with the originating session so routing downstream knows
    "which user is talking".

    `stop_event` is propagated to every child loop. If any loop raises,
    we cancel siblings and re-raise.
    """
    owns_client = client is None
    c = client or ClawBotClient()
    stop = stop_event or asyncio.Event()

    async def _one(spec: SessionPollSpec) -> None:
        async def _per_session_handler(msg: InboundMessage) -> None:
            await on_message(spec, msg)

        await run_polling_loop(
            _per_session_handler,
            spec.bot_token,
            client=c,
            stop_event=stop,
            backoff_s=backoff_s,
            session_name=spec.name,
        )

    try:
        async with asyncio.TaskGroup() as tg:
            for spec in sessions:
                tg.create_task(_one(spec), name=f"poll:{spec.name}")
    finally:
        if owns_client:
            await c.aclose()
