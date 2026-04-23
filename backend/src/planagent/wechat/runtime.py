"""Long-poll runtime loop: pumps messages into a user-supplied handler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from .client import ClawBotClient, ClawBotError
from .protocol import InboundMessage

log = logging.getLogger(__name__)

MessageHandler = Callable[[InboundMessage], Awaitable[None]]


async def run_polling_loop(
    on_message: MessageHandler,
    bot_token: str,
    *,
    client: ClawBotClient | None = None,
    stop_event: asyncio.Event | None = None,
    backoff_s: float = 2.0,
) -> None:
    """Loop forever (or until `stop_event`) dispatching messages to `on_message`.

    - Threads the cursor (`get_updates_buf`) across calls.
    - Isolates handler exceptions so one bad message can't kill the loop.
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
                except Exception:  # noqa: BLE001 — handler must not kill loop
                    log.exception("on_message handler raised")
    finally:
        if owns_client:
            await c.aclose()
