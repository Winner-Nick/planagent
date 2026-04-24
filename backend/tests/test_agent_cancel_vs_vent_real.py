"""PR-I §G.4: real DeepSeek — cancel vs. vent distinction.

Two scenarios against the full agent orchestrator:

- Explicit cancel ("喝水那个先取消吧") → agent must call `cancel_plan`
  (NOT delete_plan, NOT mark_plan_complete) and the plan row must end up
  with status == cancelled.
- Venting ("不想喝水") → agent must NOT touch the plan. Status stays active.

Uses real DeepSeek; skipped if DEEPSEEK_API_KEY missing (see conftest).
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
    PlanStatus,
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.main import run_migrations
from planagent.wechat.constants import PENG
from planagent.wechat.protocol import (
    ITEM_TYPE_TEXT,
    InboundMessage,
    Item,
    TextItemPayload,
)


@pytest_asyncio.fixture
async def session_factory(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "cancel-vs-vent.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    yield db_mod.get_sessionmaker()
    await db_mod.dispose_engine()


async def _seed_group_and_plan(
    sf: async_sessionmaker, *, title: str
) -> tuple[str, str]:
    """Pre-seed a GroupContext + GroupMember for Peng + a single active plan.

    Returns (wechat_group_id, plan_id). Using real Peng wechat_user_id so the
    orchestrator's persona renders with actual display_name ("鹏鹏"), matching
    production behavior.
    """
    async with sf() as session:
        group = GroupContext(
            wechat_group_id="wx-grp-cancel-vs-vent",
            name="cancel vs vent",
        )
        session.add(group)
        await session.flush()
        session.add(
            GroupMember(
                group_id=group.id,
                wechat_user_id=PENG.wechat_user_id,
                display_name=PENG.display_name,
            )
        )
        plan = Plan(
            group_id=group.id,
            title=title,
            status=PlanStatus.active,
            owner_user_id=PENG.wechat_user_id,
        )
        session.add(plan)
        await session.commit()
        return group.wechat_group_id, plan.id


def _inbound(wechat_group_id: str, text: str, *, ctx_token: str) -> InboundMessage:
    return InboundMessage(
        from_user_id=PENG.wechat_user_id,
        to_user_id="bot",
        context_token=ctx_token,
        item_list=[Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))],
        group_id=wechat_group_id,
    )


async def _called_tools(sf: async_sessionmaker) -> list[str]:
    """Return every tool name invoked in this DB, in turn order."""
    async with sf() as session:
        res = await session.execute(
            select(ConversationTurn)
            .where(ConversationTurn.role == ConversationRole.assistant)
            .order_by(ConversationTurn.created_at.asc())
        )
        names: list[str] = []
        for turn in res.scalars().all():
            if not turn.tool_calls_json:
                continue
            for call in turn.tool_calls_json.get("tool_calls", []) or []:
                fn = (call or {}).get("function") or {}
                if fn.get("name"):
                    names.append(fn["name"])
        return names


@pytest.mark.real_api
async def test_explicit_cancel_invokes_cancel_plan(session_factory) -> None:
    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    wechat_group_id, plan_id = await _seed_group_and_plan(
        session_factory, title="喝水"
    )

    # Turn 1: initial cancel intent. The persona tells the agent to confirm
    # when the ask is ambiguous (reminder vs. whole plan), so one clarifying
    # round is acceptable.
    await handle_inbound(
        _inbound(wechat_group_id, "喝水那个先取消吧", ctx_token="ctx-cancel-1"),
        deepseek=deepseek,
        session_factory=session_factory,
        wechat_send=_send,
    )

    async with session_factory() as session:
        plan_mid = await session.get(Plan, plan_id)
    if plan_mid is None or plan_mid.status != PlanStatus.cancelled:
        # Turn 2: be unambiguous — "the whole plan, cancel it".
        await handle_inbound(
            _inbound(
                wechat_group_id,
                "对，整个喝水计划都取消，不做了。",
                ctx_token="ctx-cancel-2",
            ),
            deepseek=deepseek,
            session_factory=session_factory,
            wechat_send=_send,
        )

    tools_used = await _called_tools(session_factory)
    assert "cancel_plan" in tools_used, (
        f"expected cancel_plan in tools, got {tools_used}; assistant said: {sent}"
    )
    assert "delete_plan" not in tools_used
    assert "mark_plan_complete" not in tools_used

    async with session_factory() as session:
        plan = await session.get(Plan, plan_id)
        assert plan is not None, "plan row must still exist (audit)"
        assert plan.status == PlanStatus.cancelled, (
            f"expected cancelled, got {plan.status}; assistant said: {sent}"
        )


@pytest.mark.real_api
async def test_venting_does_not_touch_plan(session_factory) -> None:
    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    wechat_group_id, plan_id = await _seed_group_and_plan(
        session_factory, title="喝水"
    )

    # Pure venting — persona rule says: empathize, don't mutate.
    await handle_inbound(
        _inbound(wechat_group_id, "不想喝水", ctx_token="ctx-vent"),
        deepseek=deepseek,
        session_factory=session_factory,
        wechat_send=_send,
    )

    tools_used = await _called_tools(session_factory)
    # Agent is allowed to read state (list_plans / get_plan / peek_peer_state)
    # but MUST NOT mutate the plan.
    forbidden = {
        "cancel_plan",
        "delete_plan",
        "mark_plan_complete",
        "update_plan",
    }
    touched = [t for t in tools_used if t in forbidden]
    assert not touched, (
        f"venting must not mutate plan; touched {touched}; assistant said: {sent}"
    )

    async with session_factory() as session:
        plan = await session.get(Plan, plan_id)
        assert plan is not None
        assert plan.status == PlanStatus.active, (
            f"expected active, got {plan.status}; assistant said: {sent}"
        )

    # Agent must have spoken (reply or ask).
    assert sent, "expected at least one spoken reply"
