"""Glue between the agent and the WeChat runtime.

Builds the `on_message` handler that `wechat.runtime.run_polling_loop`
expects. The returned callable resolves each inbound message through the
orchestrator and dispatches any outbound text via a ClawBotClient-bound
sender.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent.agent.service import AgentService
from planagent.agent.tools import WechatSend
from planagent.llm.deepseek import DeepSeekClient
from planagent.wechat import protocol as wxp
from planagent.wechat.client import ClawBotClient
from planagent.wechat.protocol import InboundMessage


def wechat_send_for(
    *,
    client: ClawBotClient,
    bot_token: str,
    current_msg_ref: Callable[[], InboundMessage | None],
) -> WechatSend:
    """Return a wechat_send closure bound to the message currently being handled.

    The orchestrator may issue multiple sends per inbound message; all of
    them must thread on the inbound's context_token / group_id / sender.
    """

    async def _send(text: str) -> None:
        msg = current_msg_ref()
        if msg is None:  # pragma: no cover — defensive
            return
        gid = wxp.group_id(msg)
        to = wxp.sender_id(msg) or ""
        if not text:
            return
        await client.send_text(
            bot_token,
            to_user_id=to,
            text=text,
            context_token=msg.context_token or "",
            group_id=gid,
        )

    return _send


def build_handler(
    *,
    deepseek: DeepSeekClient,
    session_factory: async_sessionmaker,
    client: ClawBotClient,
    bot_token: str,
) -> Callable[[InboundMessage], Awaitable[None]]:
    """Construct an on_message handler for `wechat.runtime.run_polling_loop`.

    Each inbound creates a short-lived closure-bound `wechat_send` that
    knows which message it's replying to.
    """

    async def _on_message(msg: InboundMessage) -> None:
        current: list[InboundMessage] = [msg]
        send = wechat_send_for(
            client=client,
            bot_token=bot_token,
            current_msg_ref=lambda: current[0],
        )
        service = AgentService(
            deepseek=deepseek,
            session_factory=session_factory,
            wechat_send=send,
        )
        await service.as_handler()(msg)

    return _on_message
