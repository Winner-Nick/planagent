"""PR-G bug #2: agent must not leak chain-of-thought into user-visible content.

Real DeepSeek. Drives a simple inbound that should trigger a tool call
(create_plan_draft) and asserts:

- Every ConversationTurn whose `tool_calls_json` is non-empty has `content`
  stripped to the empty string (the CoT guard in orchestrator.py).
- Whatever ended up sent to the user (or persisted as a final assistant
  message) does NOT contain metacommentary strings like "我已经", "接下来",
  "我需要知道", "user_id".
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
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.main import run_migrations
from planagent.wechat.protocol import (
    ITEM_TYPE_TEXT,
    InboundMessage,
    Item,
    TextItemPayload,
)

# Metacommentary substrings the persona explicitly forbids. If any of these
# show up in outbound text the bot is monologuing.
_META_SUBSTRINGS = ["我已经", "现在我来", "接下来", "我需要知道", "user_id"]


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "no_leak.db"
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
        context_token="ctx-no-leak",
        item_list=[Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))],
        group_id="wx-no-leak",
    )


@pytest.mark.real_api
async def test_tool_call_turns_have_empty_content_and_no_meta(sm) -> None:
    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    msg = _inbound(
        user_id="o9cq807dznGxf81R2JoVl2pEx_T0@im.wechat",  # Peng's real uid
        text="帮我记一下：学 Rust 这个计划",
    )
    await handle_inbound(msg, deepseek=deepseek, session_factory=sm, wechat_send=_send)

    async with sm() as session:
        turns = (
            await session.execute(select(ConversationTurn))
        ).scalars().all()

    assistant_turns = [t for t in turns if t.role == ConversationRole.assistant]
    assert assistant_turns, "no assistant turns persisted"

    # Any assistant turn with tool_calls must have empty content.
    leaked = []
    for t in assistant_turns:
        tc = t.tool_calls_json or {}
        has_calls = bool(tc.get("tool_calls")) if isinstance(tc, dict) else False
        if has_calls and (t.content or "").strip():
            leaked.append(t.content)
    assert not leaked, f"tool-call turns leaked content: {leaked}"

    # All outbound texts must be free of metacommentary.
    for text in sent:
        for needle in _META_SUBSTRINGS:
            assert needle not in text, (
                f"outbound contained forbidden metacommentary {needle!r}: {text!r}"
            )
