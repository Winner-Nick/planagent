"""PR-L: real DeepSeek intent test.

Prompt: "一分钟后告诉辰辰你好"
Expected behavior:
- Agent calls `schedule_message_to_peer`.
- NO Plan row is created (that would be semantic noise).

Flaky-by-nature: if DeepSeek still leans on the old plan+reminder path, we
retry once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.orchestrator import handle_inbound
from planagent.db.models import (
    ConversationTurn,
    Plan,
    ScheduledMessage,
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.main import run_migrations
from planagent.wechat.constants import PENG
from planagent.wechat.protocol import (
    ITEM_TYPE_TEXT,
    InboundMessage,
    Item,
    TextItemPayload,
)


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "prefer_sm.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


def _inbound(token: str) -> InboundMessage:
    return InboundMessage(
        from_user_id=PENG.wechat_user_id,
        to_user_id="bot",
        context_token=token,
        item_list=[
            Item(
                type=ITEM_TYPE_TEXT,
                text_item=TextItemPayload(text="一分钟后告诉辰辰你好"),
            )
        ],
        group_id="wx-prefer-sm",
    )


async def _run_once(sm, token: str) -> tuple[list[ScheduledMessage], list[Plan], list[str]]:
    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    await handle_inbound(
        _inbound(token), deepseek=deepseek, session_factory=sm, wechat_send=_send
    )
    async with sm() as session:
        sms = list(
            (await session.execute(select(ScheduledMessage))).scalars().all()
        )
        plans = list((await session.execute(select(Plan))).scalars().all())
    return sms, plans, sent


@pytest.mark.real_api
async def test_agent_uses_schedule_message_to_peer_and_skips_plan(sm) -> None:
    sms, plans, sent = await _run_once(sm, "ctx-prefer-1")

    # Retry once on flakiness.
    if not sms:
        # Wipe state and retry with a new context_token (dedup would block).
        async with sm() as session:
            for p in (await session.execute(select(Plan))).scalars().all():
                await session.delete(p)
            for t in (await session.execute(select(ConversationTurn))).scalars().all():
                await session.delete(t)
            await session.commit()
        sms, plans, sent = await _run_once(sm, "ctx-prefer-2")

    assert sms, (
        "agent did not call schedule_message_to_peer for "
        f"'一分钟后告诉辰辰你好'. sent={sent!r}"
    )
    assert not plans, (
        f"agent should not create a Plan for a one-off nudge; got {[p.title for p in plans]!r}"
    )

    # Print the reply so the PR body can quote it.
    print("AGENT_REPLY:", sent)
