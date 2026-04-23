"""Two sessions poll concurrently; each inbound routes with its session."""

from __future__ import annotations

import asyncio

import pytest

from planagent.wechat import runtime as rt
from planagent.wechat.protocol import GetUpdatesResponse, InboundMessage
from planagent.wechat.runtime import SessionPollSpec


class _MultiSessionFakeClient:
    """Test double for ClawBotClient keyed by bot_token.

    Emits a scripted sequence of GetUpdatesResponse objects per token. When
    a token's script is exhausted, subsequent long_poll calls return an
    empty update. The shared `stop` event is set once every session has
    consumed its script.
    """

    def __init__(
        self,
        scripts: dict[str, list[GetUpdatesResponse]],
        stop: asyncio.Event,
    ) -> None:
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self._stop = stop
        self._drained: set[str] = set()
        self.calls_per_token: dict[str, int] = {k: 0 for k in scripts}

    async def long_poll(self, bot_token: str, *, cursor: str = "") -> GetUpdatesResponse:
        self.calls_per_token[bot_token] = self.calls_per_token.get(bot_token, 0) + 1
        queue = self._scripts.get(bot_token, [])
        if queue:
            resp = queue.pop(0)
            if not queue:
                self._drained.add(bot_token)
                if self._drained == set(self._scripts.keys()):
                    # All scripts drained — let the loops exit.
                    self._stop.set()
            return resp
        # Drained: quiet response until stop fires.
        await asyncio.sleep(0.01)
        return GetUpdatesResponse.model_validate(
            {"ret": 0, "msgs": [], "get_updates_buf": cursor, "longpolling_timeout_ms": 1000}
        )

    async def aclose(self) -> None:
        return None


def _resp_with(msgs: list[InboundMessage]) -> GetUpdatesResponse:
    return GetUpdatesResponse.model_validate(
        {
            "ret": 0,
            "msgs": [m.model_dump() for m in msgs],
            "get_updates_buf": "cur",
            "longpolling_timeout_ms": 1000,
        }
    )


@pytest.mark.asyncio
async def test_run_all_sessions_routes_per_session() -> None:
    peng = SessionPollSpec(session_id="bs-peng", bot_token="tok-peng", name="peng")
    chen = SessionPollSpec(session_id="bs-chen", bot_token="tok-chen", name="chenchen")

    m_peng = InboundMessage.model_validate(
        {
            "from_user_id": "o9cq807dznGxf81R2JoVl2pEx_T0@im.wechat",
            "to_user_id": "aa55777501ab@im.bot",
            "message_type": 1,
            "context_token": "ctx-peng-1",
            "item_list": [{"type": 1, "text_item": {"text": "hi from peng"}}],
        }
    )
    m_chen = InboundMessage.model_validate(
        {
            "from_user_id": "o9cq80ydQIR4ZaYl6vXvDp_4KklQ@im.wechat",
            "to_user_id": "cdda5a00cb61@im.bot",
            "message_type": 1,
            "context_token": "ctx-chen-1",
            "item_list": [{"type": 1, "text_item": {"text": "hi from chenchen"}}],
        }
    )

    stop = asyncio.Event()
    fake = _MultiSessionFakeClient(
        {
            peng.bot_token: [_resp_with([m_peng])],
            chen.bot_token: [_resp_with([m_chen])],
        },
        stop,
    )

    routed: list[tuple[str, str]] = []

    async def on_message(spec: SessionPollSpec, msg: InboundMessage) -> None:
        text = msg.item_list[0].text_item.text if msg.item_list else ""
        routed.append((spec.name, text or ""))

    # Run with a short timeout as a safety net.
    await asyncio.wait_for(
        rt.run_all_sessions(
            [peng, chen],
            on_message,
            client=fake,
            stop_event=stop,
            backoff_s=0.0,
        ),
        timeout=5.0,
    )

    names_seen = {r[0] for r in routed}
    assert names_seen == {"peng", "chenchen"}, f"routed={routed}"
    texts_by_name = dict(routed)
    assert "peng" in texts_by_name["peng"]
    assert "chenchen" in texts_by_name["chenchen"]
    # Each token got at least its one scripted call.
    assert fake.calls_per_token[peng.bot_token] >= 1
    assert fake.calls_per_token[chen.bot_token] >= 1


@pytest.mark.asyncio
async def test_run_all_sessions_stamps_inbound_via_handler() -> None:
    """The handler is invoked per (session, msg); stamping timestamps is the
    caller's job — this test just verifies the wiring delivers both args."""
    peng = SessionPollSpec(session_id="bs-peng", bot_token="tok-peng", name="peng")

    m = InboundMessage.model_validate(
        {
            "from_user_id": "u1",
            "to_user_id": "b1",
            "message_type": 1,
            "context_token": "ctx-1",
            "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }
    )

    stop = asyncio.Event()
    fake = _MultiSessionFakeClient({peng.bot_token: [_resp_with([m])]}, stop)

    calls: list[tuple[SessionPollSpec, InboundMessage]] = []

    async def on_message(spec: SessionPollSpec, msg: InboundMessage) -> None:
        calls.append((spec, msg))

    await asyncio.wait_for(
        rt.run_all_sessions(
            [peng], on_message, client=fake, stop_event=stop, backoff_s=0.0
        ),
        timeout=3.0,
    )

    assert len(calls) == 1
    spec, msg = calls[0]
    assert spec is peng
    assert msg.context_token == "ctx-1"
