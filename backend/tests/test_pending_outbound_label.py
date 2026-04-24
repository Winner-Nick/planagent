"""PR-L bug #4: PendingOutbound delivery must be prefixed with author tag.

Before PR-L, a queued message from 鹏鹏 to 辰辰 landed in 辰辰's chat with
no signal — looked like 小计 spontaneously said it. We now prepend
"[{author_display_name} 让我转告] " (or "[系统] " for system-originated
rows).

We skip the DeepSeek agent loop by monkeypatching the orchestrator's inner
chat path; the test is about the flush, not the agent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.orchestrator import _flush_pending_outbound
from planagent.db.models import (
    GroupContext,
    PendingOutbound,
    PendingOutboundStatus,
)
from planagent.main import run_migrations
from planagent.wechat.constants import CHENCHEN, PENG


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "flush_label.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


async def test_pending_outbound_prefixed_with_author_tag(sm) -> None:
    # Seed: group + pending row authored by 鹏鹏 for 辰辰.
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-flush-label")
        session.add(group)
        await session.flush()
        gid = group.id
        session.add(
            PendingOutbound(
                group_id=gid,
                target_user_id=CHENCHEN.wechat_user_id,
                author_user_id=PENG.wechat_user_id,
                text="今晚一起复盘",
                status=PendingOutboundStatus.pending,
            )
        )
        await session.commit()

    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    delivered = await _flush_pending_outbound(
        sm,
        group_id=gid,
        target_user_id=CHENCHEN.wechat_user_id,
        wechat_send=_send,
    )
    assert delivered == 1
    assert len(sent) == 1
    assert sent[0].startswith("[鹏鹏 让我转告] ")
    assert "今晚一起复盘" in sent[0]

    # Row flipped to delivered.
    async with sm() as session:
        rows = (await session.execute(select(PendingOutbound))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == PendingOutboundStatus.delivered
        assert rows[0].delivered_at is not None


async def test_pending_outbound_system_origin_uses_system_tag(sm) -> None:
    """author_user_id=NULL → '[系统] ' prefix."""
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-flush-sys")
        session.add(group)
        await session.flush()
        gid = group.id
        session.add(
            PendingOutbound(
                group_id=gid,
                target_user_id=CHENCHEN.wechat_user_id,
                author_user_id=None,
                text="系统维护通知",
                status=PendingOutboundStatus.pending,
            )
        )
        await session.commit()

    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    delivered = await _flush_pending_outbound(
        sm,
        group_id=gid,
        target_user_id=CHENCHEN.wechat_user_id,
        wechat_send=_send,
    )
    assert delivered == 1
    assert sent[0].startswith("[系统] ")
    assert "系统维护通知" in sent[0]


async def test_pending_outbound_unknown_author_uses_system_tag(sm) -> None:
    """author_user_id set but not in known-humans roster → '[系统] ' prefix.

    Defensive: if a future deployment adds a third bot or cred, a stray
    author_user_id shouldn't leak as "[None 让我转告]" — fall back to 系统.
    """
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-flush-unknown")
        session.add(group)
        await session.flush()
        gid = group.id
        session.add(
            PendingOutbound(
                group_id=gid,
                target_user_id=CHENCHEN.wechat_user_id,
                author_user_id="some-unknown-uid@im.wechat",
                text="未知来源",
                status=PendingOutboundStatus.pending,
            )
        )
        await session.commit()

    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    await _flush_pending_outbound(
        sm,
        group_id=gid,
        target_user_id=CHENCHEN.wechat_user_id,
        wechat_send=_send,
    )
    assert sent[0].startswith("[系统] "), sent[0]
