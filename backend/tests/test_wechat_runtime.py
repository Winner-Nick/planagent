"""Regression: the polling loop must survive httpx transport errors
(DNS, connect, read timeout) — a network blip shouldn't kill the bot.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from planagent.wechat import runtime as rt
from planagent.wechat.protocol import GetUpdatesResponse


class _FakeClient:
    def __init__(self, stop: asyncio.Event) -> None:
        self.calls = 0
        self._stop = stop

    async def long_poll(self, bot_token: str, *, cursor: str = "") -> GetUpdatesResponse:
        self.calls += 1
        if self.calls == 1:
            raise httpx.ConnectError("dns failure")
        if self.calls == 2:
            raise httpx.ReadTimeout("server slow")
        # Third call succeeds; signal the loop to exit.
        self._stop.set()
        return GetUpdatesResponse.model_validate(
            {"ret": 0, "msgs": [], "get_updates_buf": "cur", "longpolling_timeout_ms": 35000}
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_polling_loop_survives_httpx_errors() -> None:
    stop = asyncio.Event()
    fake = _FakeClient(stop)

    async def handler(_msg) -> None:  # noqa: ANN001 — test handler
        return None

    await rt.run_polling_loop(handler, "tok", client=fake, stop_event=stop, backoff_s=0.0)
    assert fake.calls == 3
