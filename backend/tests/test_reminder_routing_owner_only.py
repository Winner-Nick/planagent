"""PR-L bug #1: reminders must route to the plan's owner ONLY.

Before PR-L, `_claim_due_reminders` fanned every due Reminder to every
BotSession in the group — so 鹏鹏 got pinged about 辰辰's reminders too.
This test seeds a plan owned by 辰辰 with an already-due pending reminder
and a full two-session group; one tick must deliver exactly ONE send,
addressed to 辰辰's wechat_user_id.

Plain SQLite, no LLM — the claim path doesn't need real DeepSeek.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.db.models import (
    BotSession,
    GroupContext,
    Plan,
    PlanStatus,
    Reminder,
    ReminderStatus,
)
from planagent.main import run_migrations
from planagent.scheduler.scheduler import Scheduler
from planagent.wechat.constants import CHENCHEN, PENG


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "routing.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


class _NullDeepSeek:
    """No-op client — we never exercise the decide() path in this test.

    The tick's `_one(plan)` invocation will call `decide()`, but with no
    active plans we short-circuit before that. We DO have one plan, but
    its reminder is already pending+due, so even if decide() raises, the
    claim pass is independent and will still run.
    """

    def chat(self, *args, **kwargs):  # pragma: no cover
        raise RuntimeError("decide() should not be called in this test path")


async def test_due_reminder_routes_to_owner_session_only(sm) -> None:
    # Seed: group + 2 BotSessions (鹏鹏 and 辰辰) + a plan owned by 辰辰 + a
    # pending reminder whose fire_at is already in the past.
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-route", name="routing-test")
        session.add(group)
        await session.flush()
        gid = group.id
        peng_bs = BotSession(
            group_id=gid,
            name="peng",
            wechat_user_id=PENG.wechat_user_id,
            bot_token="peng-token",
            last_context_token="ctx-peng",
            display_name=PENG.display_name,
        )
        chenchen_bs = BotSession(
            group_id=gid,
            name="chenchen",
            wechat_user_id=CHENCHEN.wechat_user_id,
            bot_token="chenchen-token",
            last_context_token="ctx-chenchen",
            display_name=CHENCHEN.display_name,
        )
        session.add_all([peng_bs, chenchen_bs])
        plan = Plan(
            group_id=gid,
            title="辰辰的英语打卡",
            status=PlanStatus.active,
            owner_user_id=CHENCHEN.wechat_user_id,
        )
        session.add(plan)
        await session.flush()
        rem = Reminder(
            plan_id=plan.id,
            fire_at=datetime.now(UTC) - timedelta(seconds=5),
            message="该打卡啦～",
            status=ReminderStatus.pending,
        )
        session.add(rem)
        await session.commit()
        rem_id = rem.id

    captured: list[tuple[str, str, str, str | None]] = []

    async def fake_send(bot_token, to_user_id, text, context_token):
        captured.append((bot_token, to_user_id, text, context_token))

    scheduler = Scheduler(sm, _NullDeepSeek(), fake_send, enable_wakeup=False)
    await scheduler.tick(interval_s=300)

    # Exactly ONE send, to 辰辰 only — not 鹏鹏.
    assert len(captured) == 1, f"expected 1 send (owner only), got {captured!r}"
    bot_token, to_user_id, text, ctx_token = captured[0]
    assert to_user_id == CHENCHEN.wechat_user_id
    assert bot_token == "chenchen-token"
    assert text == "该打卡啦～"
    assert ctx_token == "ctx-chenchen"

    # Reminder is flipped to sent.
    async with sm() as session:
        r = await session.get(Reminder, rem_id)
        assert r is not None
        assert r.status == ReminderStatus.sent
        assert r.fired_at is not None


async def test_due_reminder_with_missing_owner_session_is_logged_and_skipped(
    sm, caplog
) -> None:
    """No BotSession for the plan's owner → log + no send, reminder still flips.

    This scenario matches "owner never had an inbound yet" — BotSession with
    a NULL wechat_user_id exists for 鹏鹏 but we own the plan to 辰辰 whose
    session hasn't been created. We should see the `reminder_dropped_no_owner_session`
    warning rather than silently fanning to 鹏鹏.
    """
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-route-missing")
        session.add(group)
        await session.flush()
        gid = group.id
        peng_bs = BotSession(
            group_id=gid,
            name="peng",
            wechat_user_id=PENG.wechat_user_id,
            bot_token="peng-token",
            last_context_token="ctx-peng",
        )
        session.add(peng_bs)
        plan = Plan(
            group_id=gid,
            title="辰辰的事",
            status=PlanStatus.active,
            owner_user_id=CHENCHEN.wechat_user_id,
        )
        session.add(plan)
        await session.flush()
        rem = Reminder(
            plan_id=plan.id,
            fire_at=datetime.now(UTC) - timedelta(seconds=5),
            message="x",
            status=ReminderStatus.pending,
        )
        session.add(rem)
        await session.commit()
        rem_id = rem.id

    captured: list = []

    async def fake_send(bot_token, to_user_id, text, context_token):
        captured.append((bot_token, to_user_id, text, context_token))

    scheduler = Scheduler(sm, _NullDeepSeek(), fake_send, enable_wakeup=False)
    caplog.set_level("WARNING")
    await scheduler.tick(interval_s=300)

    assert not captured, "no session for owner → no send"
    # Reminder still flipped (avoids re-claiming on every tick).
    async with sm() as session:
        r = await session.get(Reminder, rem_id)
        assert r is not None
        assert r.status == ReminderStatus.sent
    assert any(
        "reminder_dropped_no_owner_session" in rec.getMessage()
        for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]
