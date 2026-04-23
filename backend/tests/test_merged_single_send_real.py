"""PR-G: orchestrator merges multiple spoken-tool calls into one outbound.

Real DeepSeek. Prompts that would typically elicit "greet + follow-up" are
ambiguous in practice, so we assert the invariant independent of whether the
model even splits it: the handler calls wechat_send AT MOST ONCE per
invocation. If it calls zero times, that's fine (the LLM may have needed
zero outbound — e.g. created a plan silently). If it calls more than once,
that's a regression.

We also assert: when more than one spoken-tool text is queued by the LLM,
the single outgoing message contains all of them (concatenation, not drop).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.orchestrator import _DeferredSender, handle_inbound
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
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "merged.db"
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
        context_token="ctx-merged",
        item_list=[Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))],
        group_id="wx-merged",
    )


@pytest.mark.real_api
async def test_single_outbound_send_per_handler_invocation(sm) -> None:
    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    # Prompt engineered to encourage both a greeting AND a follow-up
    # question (which would typically be two spoken-tool calls).
    msg = _inbound(
        user_id=PENG.wechat_user_id,
        text="嗨，顺便帮我把'每周日复盘 2 小时'这个计划记一下吧",
    )
    await handle_inbound(msg, deepseek=deepseek, session_factory=sm, wechat_send=_send)

    # At most one outbound per handler invocation (the PR-G merge rule).
    assert len(sent) <= 1, f"expected <=1 outbound, got {len(sent)}: {sent}"


def test_deferred_sender_concatenates() -> None:
    """Unit-level proof of the merge behavior. No network involved."""
    import asyncio

    underlying: list[str] = []

    async def _send(text: str) -> None:
        underlying.append(text)

    async def _drive() -> None:
        d = _DeferredSender(_send)
        await d("你好鹏鹏")
        await d("顺便问下，这个计划每次多少分钟合适？")
        merged = await d.flush()
        assert merged is not None
        assert "你好鹏鹏" in merged
        assert "每次多少分钟" in merged

    asyncio.run(_drive())
    assert len(underlying) == 1
    assert "你好鹏鹏" in underlying[0]
    assert "每次多少分钟" in underlying[0]
