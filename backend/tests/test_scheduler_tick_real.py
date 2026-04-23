"""Real DeepSeek + real SQLite end-to-end scheduler tick test."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from planagent import db as db_mod
from planagent.db.models import (
    GroupContext,
    GroupMember,
    Plan,
    PlanStatus,
    Reminder,
    ReminderStatus,
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.main import run_migrations
from planagent.scheduler.scheduler import Scheduler


@pytest_asyncio.fixture
async def sessionmaker_fx(tmp_path, monkeypatch) -> AsyncIterator:
    db_file = tmp_path / "scheduler.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    sm = db_mod.get_sessionmaker()
    try:
        yield sm
    finally:
        await db_mod.dispose_engine()


async def _seed(sm) -> tuple[str, str, str]:
    """Seed: group with last_context_token + member, plan starting in ~1 min."""
    async with sm() as session:
        group = GroupContext(
            wechat_group_id="wx-grp-sched",
            name="Scheduler Test Group",
            last_context_token="ctx-seeded-123",
        )
        session.add(group)
        await session.flush()
        member = GroupMember(
            group_id=group.id,
            wechat_user_id="wx-user-1",
            display_name="Peng",
        )
        session.add(member)
        now_utc = datetime.now(UTC)
        plan = Plan(
            group_id=group.id,
            title="Daily Rust 30-minute session",
            description="Chapter reading + exercises.",
            status=PlanStatus.active,
            start_at=now_utc + timedelta(minutes=1),
            due_at=now_utc + timedelta(hours=2),
            expected_duration_per_session_min=30,
            owner_user_id="wx-user-1",
        )
        session.add(plan)
        await session.commit()
        return group.id, plan.id, group.wechat_group_id


@pytest.mark.real_api
async def test_tick_creates_reminder_is_idempotent_and_fires_when_due(
    sessionmaker_fx,
) -> None:
    sm = sessionmaker_fx
    group_id, plan_id, wechat_group_id = await _seed(sm)

    sent: list[tuple[str, str, str | None]] = []

    async def fake_send(grp: str, msg: str, token: str | None) -> None:
        sent.append((grp, msg, token))

    scheduler = Scheduler(sm, DeepSeekClient(), fake_send)

    # First tick: LLM should want a reminder soon; at minimum, a row is inserted.
    await scheduler.tick(interval_s=300)

    async with sm() as session:
        rows = (
            await session.execute(select(Reminder).where(Reminder.plan_id == plan_id))
        ).scalars().all()
    assert len(rows) == 1, f"expected exactly one reminder, got {len(rows)}"
    r = rows[0]
    assert r.message and r.message.strip()
    assert r.status in {ReminderStatus.pending, ReminderStatus.sent}

    # Second tick within the same minute should not duplicate.
    await scheduler.tick(interval_s=300)
    async with sm() as session:
        rows = (
            await session.execute(select(Reminder).where(Reminder.plan_id == plan_id))
        ).scalars().all()
    assert len(rows) == 1, "idempotency violated: reminder duplicated"

    # Advance the scheduler's clock by 90s; a pending reminder must now fire.
    if rows[0].status == ReminderStatus.pending:
        future = datetime.now(UTC) + timedelta(seconds=90)
        scheduler._now = lambda: future  # type: ignore[method-assign]
        await scheduler.tick(interval_s=300)

        async with sm() as session:
            r2 = (
                await session.execute(select(Reminder).where(Reminder.id == rows[0].id))
            ).scalar_one()
        assert r2.status == ReminderStatus.sent
        assert r2.fired_at is not None
        assert sent, "fake_send was never called"
        grp, msg, token = sent[-1]
        assert grp == wechat_group_id
        assert msg == r2.message
        assert token == "ctx-seeded-123"
    else:
        # Reminder was already sent on tick #1; verify send captured it.
        assert sent, "reminder marked sent but fake_send not called"
        grp, msg, token = sent[-1]
        assert grp == wechat_group_id
        assert token == "ctx-seeded-123"
