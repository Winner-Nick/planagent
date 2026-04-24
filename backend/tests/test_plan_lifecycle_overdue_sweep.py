"""PR-I §B: scheduler lifecycle sweep.

Non-recurring active plans whose due_at fell more than `OVERDUE_GRACE_S`
ago must flip to `overdue` on the next tick. Recurring plans — where "due"
is per-occurrence — stay `active`. No LLM here; the sweep is pure DB.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from planagent import db as db_mod
from planagent.db.models import GroupContext, Plan, PlanStatus
from planagent.main import run_migrations
from planagent.scheduler.scheduler import OVERDUE_GRACE_S, Scheduler


class _StubDeepSeek:
    """Zero-touch stand-in: the sweep runs BEFORE any decide call and the
    plans we seed here are either already non-active post-sweep or have no
    reason to trigger decide work. Any accidental LLM call would fail the
    test loudly."""

    async def chat_completion(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("LLM should not be called in overdue-sweep test")


async def _noop_send(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
    return None


@pytest_asyncio.fixture
async def sessionmaker_fx(tmp_path, monkeypatch) -> AsyncIterator:
    db_file = tmp_path / "overdue.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    sm = db_mod.get_sessionmaker()
    try:
        yield sm
    finally:
        await db_mod.dispose_engine()


async def test_stale_nonrecurring_plan_ages_to_overdue(sessionmaker_fx) -> None:
    sm = sessionmaker_fx
    now_utc = datetime.now(UTC)

    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-grp-overdue", name="overdue-sweep")
        session.add(group)
        await session.flush()

        stale = Plan(
            group_id=group.id,
            title="一次性过期计划",
            status=PlanStatus.active,
            due_at=now_utc - timedelta(minutes=30),
            owner_user_id="wx-user-1",
        )
        session.add(stale)

        # Past due but inside the grace window — must stay active.
        fresh = Plan(
            group_id=group.id,
            title="刚过 2 分钟",
            status=PlanStatus.active,
            due_at=now_utc - timedelta(minutes=2),
            owner_user_id="wx-user-1",
        )
        session.add(fresh)

        # Recurring plan with past due_at — per-occurrence, must stay active.
        recurring = Plan(
            group_id=group.id,
            title="每天喝水",
            status=PlanStatus.active,
            due_at=now_utc - timedelta(hours=3),
            recurrence_cron="0 9 * * *",
            owner_user_id="wx-user-1",
        )
        session.add(recurring)

        # Draft with stale due_at — must NOT age (only `active` ages).
        stale_draft = Plan(
            group_id=group.id,
            title="还没齐的草稿",
            status=PlanStatus.draft,
            due_at=now_utc - timedelta(hours=2),
            owner_user_id="wx-user-1",
        )
        session.add(stale_draft)

        await session.commit()
        stale_id, fresh_id, recurring_id, draft_id = (
            stale.id,
            fresh.id,
            recurring.id,
            stale_draft.id,
        )

    sch = Scheduler(sm, _StubDeepSeek(), _noop_send, enable_wakeup=False)
    # Run only the sweep — full tick would call decide() on the recurring row.
    await sch._sweep_overdue(now_utc)

    async with sm() as session:
        assert (await session.get(Plan, stale_id)).status == PlanStatus.overdue
        assert (await session.get(Plan, fresh_id)).status == PlanStatus.active
        assert (await session.get(Plan, recurring_id)).status == PlanStatus.active
        assert (await session.get(Plan, draft_id)).status == PlanStatus.draft


async def test_overdue_grace_window_constant_is_ten_minutes() -> None:
    """Guardrail — the 10-minute grace is documented + called out in PR-I."""
    assert OVERDUE_GRACE_S == 600
