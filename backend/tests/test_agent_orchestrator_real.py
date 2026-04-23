"""End-to-end orchestrator test against the real DeepSeek API.

NO mocks of domain logic. `wechat_send` is a fake (an in-memory list) only
because outbound WeChat transport needs a scanned-in bot to work.
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
    ConversationRole,
    ConversationTurn,
    GroupContext,
    GroupMember,
    Plan,
    Reminder,
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.main import run_migrations
from planagent.wechat.protocol import (
    ITEM_TYPE_TEXT,
    InboundMessage,
    Item,
    TextItemPayload,
)


@pytest_asyncio.fixture
async def session_factory(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "orchestrator.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    yield db_mod.get_sessionmaker()
    await db_mod.dispose_engine()


def _inbound(
    *, group_id: str, user_id: str, text: str, context_token: str = "ctx-1"
) -> InboundMessage:
    return InboundMessage(
        from_user_id=user_id,
        to_user_id="bot",
        context_token=context_token,
        item_list=[
            Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))
        ],
        group_id=group_id,
    )


@pytest.mark.real_api
async def test_end_to_end_minimal_conversation(session_factory) -> None:
    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    # Seed two users so the agent sees a real group roster after the first msg.
    # (GroupContext gets created by the orchestrator on first inbound.)

    # Turn 1: an incomplete plan request — expect at minimum a create_plan_draft
    # AND (because required fields are missing) an ask_user_in_group, or the
    # agent may batch more tool calls. We assert structural facts, not text.
    msg1 = _inbound(
        group_id="wx-rust",
        user_id="u-peng",
        text="帮我安排下周一开始学 Rust，每天 30 分钟",
    )
    await handle_inbound(
        msg1,
        deepseek=deepseek,
        session_factory=session_factory,
        wechat_send=_send,
    )

    async with session_factory() as session:
        plans = (await session.execute(select(Plan))).scalars().all()
        assert len(plans) >= 1, "agent should have created at least one plan"
        assert any("rust" in (p.title or "").lower() for p in plans), (
            f"expected a Rust-ish plan title, got {[p.title for p in plans]}"
        )

    # Turn 2: provide more context. Feed the likely-missing fields in one shot.
    msg2 = _inbound(
        group_id="wx-rust",
        user_id="u-peng",
        text=(
            "我叫 Peng，owner 就是我 (u-peng)。每周一到周五执行，目标两个月。"
            "每次 30 分钟。第一次提醒设定在 2099-05-04 20:00（Asia/Shanghai）。"
        ),
        context_token="ctx-2",
    )
    await handle_inbound(
        msg2,
        deepseek=deepseek,
        session_factory=session_factory,
        wechat_send=_send,
    )

    async with session_factory() as session:
        plans = (await session.execute(select(Plan))).scalars().all()
        assert len(plans) >= 1
        rust_plans = [p for p in plans if "rust" in (p.title or "").lower()]
        assert rust_plans, f"no Rust plan in {[p.title for p in plans]}"
        # Status should be draft or active — agent may or may not activate yet.
        assert rust_plans[0].status.value in {"draft", "active"}

        reminders = (await session.execute(select(Reminder))).scalars().all()
        assert len(reminders) >= 1, "expected at least one reminder scheduled"

        # ConversationTurn rows must include user + assistant + tool turns.
        roles = {
            t.role
            for t in (
                await session.execute(select(ConversationTurn))
            ).scalars().all()
        }
        assert ConversationRole.user in roles
        assert ConversationRole.assistant in roles
        assert ConversationRole.tool in roles

        # Group + member upserted.
        groups = (await session.execute(select(GroupContext))).scalars().all()
        assert len(groups) == 1
        members = (await session.execute(select(GroupMember))).scalars().all()
        assert any(m.wechat_user_id == "u-peng" for m in members)

    # The fake outbound should have captured at least one spoken line.
    assert sent, "expected at least one outbound message via wechat_send"


@pytest.mark.real_api
async def test_two_distinct_users_share_group(session_factory) -> None:
    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    msg_a = _inbound(
        group_id="wx-team",
        user_id="u-alice",
        text="@bot 记一下：周五团队复盘，我主持。",
    )
    msg_b = _inbound(
        group_id="wx-team",
        user_id="u-bob",
        text="@bot 我是 Bob，帮我列一下现有的 plans。",
        context_token="ctx-b",
    )

    await handle_inbound(
        msg_a, deepseek=deepseek, session_factory=session_factory, wechat_send=_send
    )
    await handle_inbound(
        msg_b, deepseek=deepseek, session_factory=session_factory, wechat_send=_send
    )

    async with session_factory() as session:
        members = (await session.execute(select(GroupMember))).scalars().all()
        uids = {m.wechat_user_id for m in members}
        assert {"u-alice", "u-bob"} <= uids
