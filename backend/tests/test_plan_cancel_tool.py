"""PR-I §D: `cancel_plan` tool handler.

Direct handler test, no LLM. Verifies:
- The plan is marked `cancelled` (not `completed` / `draft`).
- The row still exists in the DB (NOT deleted — preserved for audit).
- Group ownership is enforced (cross-group plan_id looks 'not found').
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.tools import TOOL_REGISTRY, ToolContext
from planagent.db.models import GroupContext, Plan, PlanStatus
from planagent.main import run_migrations


@pytest_asyncio.fixture
async def session_factory(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "cancel.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    yield db_mod.get_sessionmaker()
    await db_mod.dispose_engine()


async def _noop_send(text: str) -> None:
    return None


async def test_cancel_plan_sets_status_and_preserves_row(session_factory) -> None:
    async with session_factory() as session:
        group = GroupContext(wechat_group_id="wx-cancel-1", name="cancel-test")
        session.add(group)
        await session.flush()
        plan = Plan(
            group_id=group.id,
            title="喝水",
            status=PlanStatus.active,
            owner_user_id="wx-user-1",
        )
        session.add(plan)
        await session.commit()
        group_id, plan_id = group.id, plan.id

    ctx = ToolContext(
        session_factory=session_factory,
        group_id=group_id,
        wechat_group_id="wx-cancel-1",
        wechat_send=_noop_send,
    )
    handler = TOOL_REGISTRY["cancel_plan"].handler
    result = await handler(ctx, plan_id=plan_id)

    assert result.get("id") == plan_id
    assert result.get("status") == "cancelled"

    # Row still exists.
    async with session_factory() as session:
        still_there = await session.get(Plan, plan_id)
        assert still_there is not None
        assert still_there.status == PlanStatus.cancelled


async def test_cancel_plan_rejects_cross_group_plan_id(session_factory) -> None:
    async with session_factory() as session:
        g1 = GroupContext(wechat_group_id="wx-grpA", name="A")
        g2 = GroupContext(wechat_group_id="wx-grpB", name="B")
        session.add_all([g1, g2])
        await session.flush()
        # Plan belongs to group A; ctx will point at group B.
        plan = Plan(
            group_id=g1.id,
            title="A's plan",
            status=PlanStatus.active,
            owner_user_id="wx-user-1",
        )
        session.add(plan)
        await session.commit()
        plan_id = plan.id
        other_group_id = g2.id

    ctx = ToolContext(
        session_factory=session_factory,
        group_id=other_group_id,
        wechat_group_id="wx-grpB",
        wechat_send=_noop_send,
    )
    handler = TOOL_REGISTRY["cancel_plan"].handler
    result = await handler(ctx, plan_id=plan_id)
    assert result.get("error") == "plan_not_found"

    async with session_factory() as session:
        preserved = await session.get(Plan, plan_id)
        assert preserved is not None
        assert preserved.status == PlanStatus.active
