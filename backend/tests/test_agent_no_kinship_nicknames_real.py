"""PR-M: 小计 must not invent kinship nicknames even when earlier history
contains them.

Scenario:
1. Plant a fabricated earlier assistant turn that called 辰辰 "乖女儿".
2. Send a normal new inbound from 辰辰.
3. Real DeepSeek is the only caller in the loop — no mocks.
4. Assert the fresh reply does NOT contain any of the banned kinship forms.

The banned set covers the specific report ("乖女儿") plus the common
failure modes the persona now forbids.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.orchestrator import _append_turn, handle_inbound
from planagent.db.models import (
    ConversationRole,
    GroupContext,
    GroupMember,
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.main import run_migrations
from planagent.wechat.constants import CHENCHEN, PENG
from planagent.wechat.protocol import (
    ITEM_TYPE_TEXT,
    InboundMessage,
    Item,
    TextItemPayload,
)

_BANNED = ["乖女儿", "老婆", "媳妇", "宝宝", "宝贝", "亲爱的", "老公", "媳妇儿"]


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "no_kin.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


async def _preseed(sm: async_sessionmaker) -> str:
    """Create the group and register both humans; return internal group id."""
    async with sm() as session:
        g = GroupContext(wechat_group_id="wx-no-kin", name="logical")
        session.add(g)
        await session.flush()
        for who in (PENG, CHENCHEN):
            session.add(
                GroupMember(
                    group_id=g.id,
                    wechat_user_id=who.wechat_user_id,
                    display_name=who.display_name,
                )
            )
        await session.commit()
        return g.id


def _inbound(*, user_id: str, text: str) -> InboundMessage:
    return InboundMessage(
        from_user_id=user_id,
        to_user_id="bot",
        context_token="ctx-no-kin-new",
        item_list=[Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))],
        group_id="wx-no-kin",
    )


@pytest.mark.real_api
async def test_agent_does_not_invent_kinship_nicknames(sm) -> None:
    group_id = await _preseed(sm)

    # Plant the offending earlier exchange so DeepSeek sees "乖女儿" in
    # history and could plausibly drift back into that register.
    await _append_turn(
        sm,
        group_id=group_id,
        role=ConversationRole.user,
        content="今天有点累",
        user_id=CHENCHEN.wechat_user_id,
        target_user_id=CHENCHEN.wechat_user_id,
        context_token="ctx-past-1",
    )
    await _append_turn(
        sm,
        group_id=group_id,
        role=ConversationRole.assistant,
        content="辛苦啦乖女儿，早点歇着",
        user_id=None,
        target_user_id=CHENCHEN.wechat_user_id,
        context_token="ctx-past-1",
    )

    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    msg = _inbound(
        user_id=CHENCHEN.wechat_user_id,
        text="晚上想约鹏鹏一起吃饭",
    )
    await handle_inbound(msg, deepseek=deepseek, session_factory=sm, wechat_send=_send)

    assert sent, "agent produced no outbound text"
    joined = " || ".join(sent)
    for needle in _BANNED:
        assert needle not in joined, (
            f"agent drifted back into kinship nickname {needle!r}: {joined!r}"
        )
