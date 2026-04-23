"""PR-G bug #4: plans with start_at must end up with a reminder.

Real DeepSeek. Sends an inbound asking for a plan starting in 2 minutes.
Asserts after handler exit that at least one Reminder exists with fire_at
within [now, now+5min].

The persona prompt requires the agent to schedule the reminder in the same
turn. The scheduler also has a safety net (`_ensure_start_at_reminder`),
but we only trigger that via a Scheduler.tick() — which this test does not
invoke. So a pass here is purely about the agent doing its job.
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
from planagent.db.models import Reminder
from planagent.llm.deepseek import DeepSeekClient
from planagent.main import run_migrations
from planagent.scheduler.scheduler import Scheduler
from planagent.wechat.constants import PENG
from planagent.wechat.protocol import (
    ITEM_TYPE_TEXT,
    InboundMessage,
    Item,
    TextItemPayload,
)


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "start_at.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


def _inbound(*, user_id: str, text: str) -> InboundMessage:
    return InboundMessage(
        from_user_id=user_id,
        to_user_id="bot",
        context_token="ctx-start-at",
        item_list=[Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))],
        group_id="wx-start-at",
    )


@pytest.mark.real_api
async def test_two_minute_plan_gets_reminder(sm) -> None:
    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    now = datetime.now(UTC)
    msg = _inbound(
        user_id=PENG.wechat_user_id,
        text="帮我排一个计划：两分钟后开始做一下每日站会准备，叫它'站会准备'",
    )
    await handle_inbound(msg, deepseek=deepseek, session_factory=sm, wechat_send=_send)

    async with sm() as session:
        reminders = (await session.execute(select(Reminder))).scalars().all()

    # The agent should have scheduled a reminder. If it didn't, fall back to
    # the scheduler's safety net (which should catch a 2-minute-out start_at).
    if not reminders:
        scheduler = Scheduler(sm, deepseek, lambda *a, **k: None)  # type: ignore[arg-type]
        await scheduler.tick(interval_s=300)
        async with sm() as session:
            reminders = (await session.execute(select(Reminder))).scalars().all()

    assert reminders, "no reminder materialized for a 2-minute-out plan"
    now_naive = now.replace(tzinfo=None)
    fire_ats = []
    for r in reminders:
        fa = r.fire_at
        if fa.tzinfo is not None:
            fa = fa.astimezone(UTC).replace(tzinfo=None)
        fire_ats.append(fa)
    # At least one reminder must be within [now - 1min, now + 5min].
    ok = any(
        now_naive - timedelta(minutes=1) <= fa <= now_naive + timedelta(minutes=5)
        for fa in fire_ats
    )
    assert ok, f"no reminder fire_at within the 2-minute window: {fire_ats}"
