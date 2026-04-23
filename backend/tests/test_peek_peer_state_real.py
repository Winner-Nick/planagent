"""PR-H: peek_peer_state surfaces peer activity into the LLM's reply.

Real DeepSeek. Setup:
- Peng is speaking.
- Chenchen owns 3 open plans; 1 of them is overdue.

When Peng asks "辰辰最近怎么样？", the agent should call `peek_peer_state`
(at least once) and the spoken reply should surface some of that context.

The assertion on reply content is intentionally loose (at least two of a
handful of domain-relevant keywords OR a digit) so non-determinism in
real-DeepSeek phrasing doesn't thrash the test. The strict assertion is
that `peek_peer_state` was actually called.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

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
    PlanStatus,
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.main import run_migrations
from planagent.wechat.constants import CHENCHEN, PENG
from planagent.wechat.protocol import (
    ITEM_TYPE_TEXT,
    InboundMessage,
    Item,
    TextItemPayload,
)


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "peek.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


def _inbound(*, user_id: str, text: str, token: str) -> InboundMessage:
    return InboundMessage(
        from_user_id=user_id,
        to_user_id="bot",
        context_token=token,
        item_list=[Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))],
        group_id="wx-peek",
    )


@pytest.mark.real_api
async def test_peek_peer_state_informs_reply(sm) -> None:
    now = datetime.now(UTC)
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-peek", name="peek")
        session.add(group)
        await session.flush()
        gid = group.id
        session.add_all(
            [
                GroupMember(
                    group_id=gid,
                    wechat_user_id=PENG.wechat_user_id,
                    display_name=PENG.display_name,
                ),
                GroupMember(
                    group_id=gid,
                    wechat_user_id=CHENCHEN.wechat_user_id,
                    display_name=CHENCHEN.display_name,
                ),
                # Chenchen: 3 open plans, 1 overdue.
                Plan(
                    group_id=gid,
                    title="英语晨读",
                    status=PlanStatus.active,
                    owner_user_id=CHENCHEN.wechat_user_id,
                    due_at=now + timedelta(days=30),
                ),
                Plan(
                    group_id=gid,
                    title="房租报税",
                    status=PlanStatus.active,
                    owner_user_id=CHENCHEN.wechat_user_id,
                    due_at=now - timedelta(hours=4),  # overdue
                ),
                Plan(
                    group_id=gid,
                    title="瑜伽体验课",
                    status=PlanStatus.draft,
                    owner_user_id=CHENCHEN.wechat_user_id,
                ),
            ]
        )
        await session.commit()

    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    msg = _inbound(
        user_id=PENG.wechat_user_id,
        text="辰辰最近怎么样？用 peek_peer_state 看下她的状态再告诉我。",
        token="ctx-peek-1",
    )
    await handle_inbound(
        msg, deepseek=deepseek, session_factory=sm, wechat_send=_send
    )

    # Assert the tool was actually called — look for a tool turn referencing
    # peek_peer_state's name via the assistant's tool_calls_json.
    async with sm() as session:
        turns = (
            await session.execute(
                select(ConversationTurn).where(
                    ConversationTurn.role == ConversationRole.assistant
                )
            )
        ).scalars().all()

    called_peek = False
    for t in turns:
        tc = t.tool_calls_json or {}
        calls = tc.get("tool_calls") if isinstance(tc, dict) else None
        if not calls:
            continue
        for c in calls:
            fn = ((c or {}).get("function") or {}).get("name")
            if fn == "peek_peer_state":
                called_peek = True
                break
        if called_peek:
            break

    if not called_peek:
        # One retry with an imperative.
        msg2 = _inbound(
            user_id=PENG.wechat_user_id,
            text='立刻调 peek_peer_state(peer="chenchen")，然后告诉我结果。',
            token="ctx-peek-2",
        )
        await handle_inbound(
            msg2, deepseek=deepseek, session_factory=sm, wechat_send=_send
        )
        async with sm() as session:
            turns = (
                await session.execute(
                    select(ConversationTurn).where(
                        ConversationTurn.role == ConversationRole.assistant
                    )
                )
            ).scalars().all()
        for t in turns:
            tc = t.tool_calls_json or {}
            calls = tc.get("tool_calls") if isinstance(tc, dict) else None
            if not calls:
                continue
            for c in calls:
                fn = ((c or {}).get("function") or {}).get("name")
                if fn == "peek_peer_state":
                    called_peek = True
                    break
            if called_peek:
                break

    assert called_peek, f"expected peek_peer_state to be called. sent={sent!r}"
    assert sent, "expected at least one spoken outbound"

    # Loose semantic assertion: reply contains at least two of these hints
    # (or a numeric digit, which covers "3 plans" / "1 overdue" phrasings).
    joined = " ".join(sent)
    keyword_hits = sum(
        1 for kw in ("辰辰", "计划", "逾期", "活跃") if kw in joined
    )
    has_digit = any(ch.isdigit() for ch in joined)
    assert keyword_hits >= 2 or has_digit, (
        f"reply lacked peer-context hints: {sent!r}"
    )
