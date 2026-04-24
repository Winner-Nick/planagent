"""PR-L bug #3: duplicate inbound must be skipped.

Call `handle_inbound` twice with the same (speaker_user_id, context_token)
within the dedup window. Assert:
- Only one ConversationTurn with role=user was persisted.
- wechat_send was only invoked once (from the first call).

We short-circuit DeepSeek with a dummy client that asserts if called more
than once — the duplicate call must return BEFORE the LLM round.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.orchestrator import handle_inbound
from planagent.db.models import ConversationRole, ConversationTurn
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
    db_file = tmp_path / "dedup.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


@dataclass
class _FakeToolCall:
    id: str = "t0"

    class function:  # noqa: N801 — mimics OpenAI SDK shape
        name = "reply_in_group"
        arguments = '{"text": "收到"}'


@dataclass
class _FakeChoiceMsg:
    content: str = ""
    tool_calls: list = None  # type: ignore[assignment]


class _FakeResp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]


class _OneShotDeepSeek:
    """Returns a single reply_in_group tool call on the first chat() call, then
    a terminal (no tool_calls) message on the second chat() in the same handler.
    Tracks total chat() invocations so the test can assert the duplicate-inbound
    path NEVER calls chat().
    """

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            tc = _FakeToolCall()
            msg = _FakeChoiceMsg(content="", tool_calls=[tc])
            return _FakeResp(msg)
        # Round 2: the orchestrator already saw reply_in_group spoken; but it
        # may still iterate once more. Return a terminal message.
        msg = _FakeChoiceMsg(content="", tool_calls=[])
        return _FakeResp(msg)


def _inbound(token: str = "ctx-dup-1") -> InboundMessage:
    return InboundMessage(
        from_user_id=PENG.wechat_user_id,
        to_user_id="bot",
        context_token=token,
        item_list=[Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text="你好"))],
        group_id="wx-dup",
    )


async def test_duplicate_inbound_is_skipped(sm) -> None:
    deepseek = _OneShotDeepSeek()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    msg = _inbound()
    await handle_inbound(
        msg, deepseek=deepseek, session_factory=sm, wechat_send=_send
    )
    calls_after_first = deepseek.calls

    # Second call with identical (speaker, context_token) within 5-minute
    # window must short-circuit entirely.
    await handle_inbound(
        msg, deepseek=deepseek, session_factory=sm, wechat_send=_send
    )
    assert deepseek.calls == calls_after_first, (
        "duplicate inbound invoked DeepSeek again; dedup is not working"
    )

    # Only ONE role=user turn in the log.
    async with sm() as session:
        user_turns = (
            await session.execute(
                select(ConversationTurn).where(
                    ConversationTurn.role == ConversationRole.user
                )
            )
        ).scalars().all()
    assert len(user_turns) == 1, (
        f"expected 1 user turn, got {len(user_turns)} — dedup is leaking"
    )

    # First call sent at least once; second call added nothing.
    assert sent, "first call should have sent something via reply_in_group"
    sends_after_first = len(sent)
    # Re-run to double check: another duplicate → no new send.
    await handle_inbound(
        msg, deepseek=deepseek, session_factory=sm, wechat_send=_send
    )
    assert len(sent) == sends_after_first, (
        "third duplicate added a send; dedup is not idempotent"
    )


async def test_different_context_token_is_not_deduped(sm) -> None:
    """Same speaker, different context_token (= a genuinely new inbound) is processed."""
    deepseek = _OneShotDeepSeek()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    await handle_inbound(
        _inbound("ctx-a"),
        deepseek=deepseek,
        session_factory=sm,
        wechat_send=_send,
    )
    first_calls = deepseek.calls
    # Reset the fake so the second turn gets its own fresh tool-call round.
    deepseek.calls = 0
    await handle_inbound(
        _inbound("ctx-b"),
        deepseek=deepseek,
        session_factory=sm,
        wechat_send=_send,
    )
    assert first_calls >= 1
    assert deepseek.calls >= 1, "different context_token should NOT be deduped"
