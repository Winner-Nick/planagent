"""Factory that wires the scheduler together with its collaborators."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from planagent.llm.deepseek import DeepSeekClient
from planagent.scheduler.scheduler import Scheduler, WechatSend


class SchedulerService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        wechat_send: WechatSend,
        deepseek: DeepSeekClient | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._deepseek = deepseek or DeepSeekClient()
        self._send = wechat_send

    def build(self) -> Scheduler:
        return Scheduler(self._sm, self._deepseek, self._send)


__all__ = ["SchedulerService"]
