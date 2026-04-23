"""PR-H: queued cross-user messages must be delivered on the peer's inbound.

Seed a `PendingOutbound` row for Chenchen before she has said anything to
小计 this session. Then drive her next inbound through `handle_inbound` and
assert:

1. `wechat_send` was called at least TWICE: first the queued pending text,
   then 小计's own reply to Chenchen's inbound.
2. The pending row's `status` flipped to `delivered` and `delivered_at` is
   populated.

Real DeepSeek, fake `wechat_send` (I/O boundary only).
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
    GroupContext,
    GroupMember,
    PendingOutbound,
    PendingOutboundStatus,
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
    db_file = tmp_path / "pending.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


def _inbound(*, user_id: str, text: str, token: str = "ctx-pending") -> InboundMessage:
    return InboundMessage(
        from_user_id=user_id,
        to_user_id="bot",
        context_token=token,
        item_list=[Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))],
        group_id="wx-pending",
    )


@pytest.mark.real_api
async def test_pending_outbound_delivered_before_agent_reply(sm) -> None:
    # Seed: a GroupContext + both members, and a pending row for Chenchen.
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-pending", name="pending")
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
                PendingOutbound(
                    group_id=gid,
                    target_user_id=CHENCHEN.wechat_user_id,
                    author_user_id=PENG.wechat_user_id,
                    text="（来自鹏鹏）今晚一起复盘一下哈～",
                    status=PendingOutboundStatus.pending,
                ),
            ]
        )
        await session.commit()

    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    msg = _inbound(
        user_id=CHENCHEN.wechat_user_id,
        text="在吗？今天过得一般般。",
    )
    await handle_inbound(
        msg, deepseek=deepseek, session_factory=sm, wechat_send=_send
    )

    # First send must be the pending message, in its original form.
    assert sent, "no outbounds at all"
    assert "复盘" in sent[0], f"expected queued text first, got {sent!r}"
    # And there must be at least one more — the agent's own reply.
    assert len(sent) >= 2, f"expected ≥2 sends (pending + reply), got {sent!r}"

    async with sm() as session:
        rows = (await session.execute(select(PendingOutbound))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.status == PendingOutboundStatus.delivered
        assert row.delivered_at is not None
