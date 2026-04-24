"""PR-L: direct handler + scheduler-dispatch coverage for schedule_message_to_peer.

Two tiers:
1. Tool handler inserts a ScheduledMessage row (no LLM).
2. Scheduler tick picks up the due row, dispatches to target's BotSession
   with the "[author 让我转告]" prefix.

Plain SQLite — no DeepSeek.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.tools import TOOL_REGISTRY, ToolContext
from planagent.db.models import (
    BotSession,
    GroupContext,
    Plan,
    ScheduledMessage,
    ScheduledMessageStatus,
)
from planagent.main import run_migrations
from planagent.scheduler.scheduler import Scheduler
from planagent.wechat.constants import CHENCHEN, PENG


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "sched_msg.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


class _NullDeepSeek:
    def chat(self, *args, **kwargs):  # pragma: no cover
        raise RuntimeError("decide() should not be called in this test path")


async def _seed_group_and_sessions(sm) -> str:
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-sm-test")
        session.add(group)
        await session.flush()
        gid = group.id
        session.add_all(
            [
                BotSession(
                    group_id=gid,
                    name="peng",
                    wechat_user_id=PENG.wechat_user_id,
                    bot_token="peng-token",
                    last_context_token="ctx-peng",
                ),
                BotSession(
                    group_id=gid,
                    name="chenchen",
                    wechat_user_id=CHENCHEN.wechat_user_id,
                    bot_token="chenchen-token",
                    last_context_token="ctx-chenchen",
                ),
            ]
        )
        await session.commit()
    return gid


async def test_handler_inserts_scheduled_message_no_plan_created(sm) -> None:
    gid = await _seed_group_and_sessions(sm)

    async def _fake_send(_text: str) -> None:  # pragma: no cover
        return

    ctx = ToolContext(
        session_factory=sm,
        group_id=gid,
        wechat_group_id="wx-sm-test",
        wechat_send=_fake_send,
        sender_user_id=PENG.wechat_user_id,
        peer_user_id=CHENCHEN.wechat_user_id,
    )

    fire_at = (datetime.now(UTC) + timedelta(minutes=1)).astimezone(
        __import__("zoneinfo").ZoneInfo("Asia/Shanghai")
    ).isoformat()

    result = await TOOL_REGISTRY["schedule_message_to_peer"].handler(
        ctx, peer="chenchen", fire_at=fire_at, text="记得吃药啦"
    )
    assert result.get("scheduled") is True
    assert result.get("scheduled_message_id")

    # One ScheduledMessage row, zero Plan rows.
    async with sm() as session:
        rows = (await session.execute(select(ScheduledMessage))).scalars().all()
        plans = (await session.execute(select(Plan))).scalars().all()
    assert len(rows) == 1
    r = rows[0]
    assert r.author_user_id == PENG.wechat_user_id
    assert r.target_user_id == CHENCHEN.wechat_user_id
    assert r.text == "记得吃药啦"
    assert r.status == ScheduledMessageStatus.pending
    assert not plans, "schedule_message_to_peer must NOT create a Plan"


async def test_scheduler_dispatches_due_scheduled_message_with_prefix(sm) -> None:
    gid = await _seed_group_and_sessions(sm)

    # Seed a ScheduledMessage with fire_at already in the past.
    async with sm() as session:
        row = ScheduledMessage(
            group_id=gid,
            author_user_id=PENG.wechat_user_id,
            target_user_id=CHENCHEN.wechat_user_id,
            fire_at=datetime.now(UTC) - timedelta(seconds=5),
            text="记得吃药啦",
            status=ScheduledMessageStatus.pending,
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    captured: list[tuple[str, str, str, str | None]] = []

    async def fake_send(bot_token, to_user_id, text, context_token):
        captured.append((bot_token, to_user_id, text, context_token))

    scheduler = Scheduler(sm, _NullDeepSeek(), fake_send, enable_wakeup=False)
    await scheduler.tick(interval_s=300)

    assert len(captured) == 1, f"expected 1 send, got {captured!r}"
    bot_token, to_user_id, text, ctx_token = captured[0]
    assert to_user_id == CHENCHEN.wechat_user_id
    assert bot_token == "chenchen-token"
    # Prefixed with author display name.
    assert text.startswith("[鹏鹏 让我转告] ")
    assert "记得吃药啦" in text
    assert ctx_token == "ctx-chenchen"

    async with sm() as session:
        r = await session.get(ScheduledMessage, row_id)
        assert r is not None
        assert r.status == ScheduledMessageStatus.sent
        assert r.fired_at is not None


async def test_scheduler_skips_scheduled_message_without_target_session(sm) -> None:
    """Target user has no BotSession → log + no send, row still flips."""
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-sm-no-target")
        session.add(group)
        await session.flush()
        gid = group.id
        # Only peng has a session.
        session.add(
            BotSession(
                group_id=gid,
                name="peng",
                wechat_user_id=PENG.wechat_user_id,
                bot_token="peng-token",
            )
        )
        row = ScheduledMessage(
            group_id=gid,
            author_user_id=PENG.wechat_user_id,
            target_user_id=CHENCHEN.wechat_user_id,
            fire_at=datetime.now(UTC) - timedelta(seconds=5),
            text="x",
            status=ScheduledMessageStatus.pending,
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    captured: list = []

    async def fake_send(bot_token, to_user_id, text, context_token):
        captured.append((bot_token, to_user_id, text, context_token))

    scheduler = Scheduler(sm, _NullDeepSeek(), fake_send, enable_wakeup=False)
    await scheduler.tick(interval_s=300)

    assert not captured
    async with sm() as session:
        r = await session.get(ScheduledMessage, row_id)
        assert r is not None
        assert r.status == ScheduledMessageStatus.sent
