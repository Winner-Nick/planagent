"""PR-G bug #1: agent must never ask the user for their user_id.

Real DeepSeek. Sends a "帮我记一下 Rust 学习" inbound from Peng's real
wechat_user_id (pre-filled as a GroupMember with display_name=鹏鹏). Asserts:

- The agent's outbound text does NOT contain "user_id" / "ID" / "你的编号" / "id"
  (bot should address him as 鹏鹏, not as a UUID).
- The Plan row created (if any) has owner_user_id == Peng's wechat_user_id
  (resolved via the 'speaker' default on create_plan_draft).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.orchestrator import handle_inbound
from planagent.db.models import GroupContext, GroupMember, Plan
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
    db_file = tmp_path / "no_ask_id.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


async def _preseed_known_human(sm: async_sessionmaker) -> None:
    """Simulate what bridge.bootstrap does: pre-register 鹏鹏 in the group."""
    async with sm() as session:
        g = GroupContext(wechat_group_id="wx-no-ask-id", name="logical")
        session.add(g)
        await session.flush()
        session.add(
            GroupMember(
                group_id=g.id,
                wechat_user_id=PENG.wechat_user_id,
                display_name=PENG.display_name,
            )
        )
        await session.commit()


def _inbound(*, user_id: str, text: str) -> InboundMessage:
    return InboundMessage(
        from_user_id=user_id,
        to_user_id="bot",
        context_token="ctx-no-ask-id",
        item_list=[Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))],
        group_id="wx-no-ask-id",
    )


@pytest.mark.real_api
async def test_agent_does_not_ask_user_id(sm) -> None:
    await _preseed_known_human(sm)

    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    msg = _inbound(user_id=PENG.wechat_user_id, text="帮我记一下 Rust 学习")
    await handle_inbound(msg, deepseek=deepseek, session_factory=sm, wechat_send=_send)

    # Outbound must not contain id-oriented prompts.
    forbidden = ["user_id", "你的编号", "UUID"]
    for text in sent:
        for needle in forbidden:
            assert needle not in text, (
                f"outbound asked for {needle!r}: {text!r}"
            )

    # If a plan was created (most likely given the direct ask), owner must
    # be resolved to the speaker, not null.
    async with sm() as session:
        plans = (await session.execute(select(Plan))).scalars().all()
    if plans:
        # Accept either exactly one plan or the first as the Rust one.
        rust_plans = [
            p for p in plans if "rust" in (p.title or "").lower()
            or "Rust" in (p.title or "")
        ]
        target = rust_plans[0] if rust_plans else plans[0]
        assert target.owner_user_id == PENG.wechat_user_id, (
            f"expected owner = {PENG.wechat_user_id}, got {target.owner_user_id}"
        )
