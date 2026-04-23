"""Agent wiring.

`AgentService` bundles the DeepSeek client, DB session factory, and a
wechat_send closure into a `handle_inbound(msg)` coroutine. Dependency-
injectable so tests can swap each piece.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent.agent.orchestrator import handle_inbound
from planagent.agent.tools import WechatSend
from planagent.llm.deepseek import DeepSeekClient
from planagent.wechat.protocol import InboundMessage

HandleInbound = Callable[[InboundMessage], Awaitable[None]]


@dataclass
class AgentService:
    deepseek: DeepSeekClient
    session_factory: async_sessionmaker
    wechat_send: WechatSend

    def as_handler(self) -> HandleInbound:
        async def _handler(msg: InboundMessage) -> None:
            await handle_inbound(
                msg,
                deepseek=self.deepseek,
                session_factory=self.session_factory,
                wechat_send=self.wechat_send,
            )

        return _handler
