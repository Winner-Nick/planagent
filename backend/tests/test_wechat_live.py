"""Live smoke test for the ClawBot HTTP client.

Skipped automatically unless WECHAT_BOT_TOKEN is set in the environment.
Hits the real iLink server — intended for manual CI or local validation,
not for PR gates.
"""

from __future__ import annotations

import os

import pytest

from planagent.wechat.client import ClawBotClient


@pytest.mark.real_wechat
async def test_long_poll_empty_cursor_returns_shape() -> None:
    token = os.environ["WECHAT_BOT_TOKEN"]
    async with ClawBotClient() as client:
        resp = await client.long_poll(token, cursor="")
    # Shape-only assertions; actual `ret` value / message list vary.
    assert hasattr(resp, "ret")
    assert hasattr(resp, "get_updates_buf")
    assert isinstance(resp.msgs, list)
