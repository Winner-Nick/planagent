"""Smoke test: bridge handler → real DeepSeek → DB mutations + captured outbound.

Boundary: we inject a fake ClawBotClient as the WeChat transport. Everything
else (DB, DeepSeek, orchestrator loop, tools) is real.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

from planagent import db as db_mod
from planagent.agent.wechat_bridge import build_handler_for_session
from planagent.db.models import BotSession, Plan, Reminder
from planagent.llm.deepseek import DeepSeekClient
from planagent.main import run_migrations
from planagent.wechat.protocol import (
    GetUpdatesResponse,
    InboundMessage,
    SendMessageResponse,
)
from planagent.wechat.runtime import SessionPollSpec
from planagent.wechat.sessions import SessionCredential, sync_sessions_to_db


class _CaptureClient:
    """Captures send_text calls; no network I/O."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(
        self,
        bot_token: str,
        *,
        to_user_id: str,
        text: str,
        context_token: str,
        group_id: str | None = None,
    ) -> SendMessageResponse:
        self.sent.append(
            {
                "bot_token": bot_token,
                "to_user_id": to_user_id,
                "text": text,
                "context_token": context_token,
                "group_id": group_id,
            }
        )
        return SendMessageResponse.model_validate({"ret": 0})

    async def long_poll(  # pragma: no cover — not used in this test
        self, bot_token: str, cursor: str = ""
    ) -> GetUpdatesResponse:
        return GetUpdatesResponse.model_validate(
            {"ret": 0, "msgs": [], "get_updates_buf": cursor}
        )

    async def aclose(self) -> None:
        return None


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator:
    db_file = tmp_path / "bridge.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


@pytest.mark.real_api
async def test_bridge_handler_creates_plan_and_sends_reply(sm) -> None:
    # Bootstrap one session for "peng".
    await sync_sessions_to_db(
        sm,
        [SessionCredential(name="stubby", bot_token="tok-peng", baseurl=None)],
    )
    async with sm() as session:
        bs = (await session.execute(select(BotSession))).scalar_one()
        session_db_id = bs.id

    client = _CaptureClient()
    deepseek = DeepSeekClient()
    spec = SessionPollSpec(
        session_id=session_db_id, bot_token="tok-peng", name="stubby"
    )
    handler = build_handler_for_session(
        deepseek=deepseek,
        session_factory=sm,
        client=client,  # type: ignore[arg-type]
        bot_session=spec,
    )

    # Simulated inbound: the user asks us to plan a Rust learning routine.
    inbound = InboundMessage.model_validate(
        {
            "from_user_id": "o9cq807dznGxf81R2JoVl2pEx_T0@im.wechat",
            "to_user_id": "aa55777501ab@im.bot",
            "message_type": 1,
            "context_token": "ctx-bridge-1",
            "item_list": [
                {
                    "type": 1,
                    "text_item": {
                        "text": "帮我安排下周一开始每天 30 分钟学 Rust，我是 owner。"
                    },
                }
            ],
        }
    )
    await handler(inbound)

    # Session state was learned from the inbound.
    async with sm() as session:
        bs = await session.get(BotSession, session_db_id)
        assert bs is not None
        assert bs.wechat_user_id == "o9cq807dznGxf81R2JoVl2pEx_T0@im.wechat"
        assert bs.bot_user_id == "aa55777501ab@im.bot"
        assert bs.last_inbound_at is not None
        # last_outbound_at stamped at handler exit (unconditional for now).
        assert bs.last_outbound_at is not None
        assert bs.last_context_token == "ctx-bridge-1"

        # The agent may either create a plan draft OR ask a clarifying
        # question (depending on whether it considers required fields still
        # missing). Either is a legitimate turn; assert at least ONE side
        # effect happened. Reminders are opportunistic — just verify the
        # FK invariant if any exist.
        plans = (await session.execute(select(Plan))).scalars().all()
        reminders = (await session.execute(select(Reminder))).scalars().all()
        plan_ids = {p.id for p in plans}
        for r in reminders:
            assert r.plan_id in plan_ids

    # At least one outbound captured — the agent must speak on every turn
    # (either reply, or ask_user_in_group, or the implicit-reply fallback).
    assert client.sent, f"expected at least one outbound send; got {client.sent!r}"
    # It must address the inbound user and carry the inbound's context_token.
    any_ok = any(
        s["to_user_id"] == "o9cq807dznGxf81R2JoVl2pEx_T0@im.wechat"
        and s["context_token"] == "ctx-bridge-1"
        and s["group_id"] is None
        for s in client.sent
    )
    assert any_ok, f"no send matched inbound routing expectations: {client.sent!r}"
    # If a Plan was created in-group, its text must be non-empty.
    if plans:
        assert plans[0].title and plans[0].title.strip()


@pytest.mark.real_api
async def test_bridge_handler_sets_last_inbound_within_recent_seconds(sm) -> None:
    """Subset of the above focused only on runtime stamping — cheaper."""
    await sync_sessions_to_db(
        sm,
        [SessionCredential(name="stubby", bot_token="tok-peng", baseurl=None)],
    )
    async with sm() as session:
        bs = (await session.execute(select(BotSession))).scalar_one()
        session_db_id = bs.id

    client = _CaptureClient()
    spec = SessionPollSpec(
        session_id=session_db_id, bot_token="tok-peng", name="stubby"
    )
    handler = build_handler_for_session(
        deepseek=DeepSeekClient(),
        session_factory=sm,
        client=client,  # type: ignore[arg-type]
        bot_session=spec,
    )

    inbound = InboundMessage.model_validate(
        {
            "from_user_id": "u1",
            "to_user_id": "b1",
            "message_type": 1,
            "context_token": "ctx-xyz",
            "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
        }
    )
    await handler(inbound)
    async with sm() as session:
        bs = await session.get(BotSession, session_db_id)
        assert bs is not None
        assert bs.last_inbound_at is not None
        now = datetime.now(UTC)
        lia = bs.last_inbound_at
        if lia.tzinfo is None:
            lia = lia.replace(tzinfo=UTC)
        assert abs((now - lia).total_seconds()) < 60
        # Sanity: the exact context_token threaded through.
        assert bs.last_context_token == "ctx-xyz"
        assert bs.wechat_user_id == "u1"
        # Note: time-delta window chosen > 30s to tolerate CI jitter.
        assert abs((now - lia).total_seconds()) >= 0
