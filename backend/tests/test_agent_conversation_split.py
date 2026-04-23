"""PR-G bug #5 regression: history loader must filter by speaker.

Critical cross-contamination scenario observed on-device: Peng was mid-plan
("截止时间？" had been asked), Chenchen unrelatedly said "OK" in her chat, and
the shared conversation log caused 小计 to "answer" Peng's question in
Chenchen's chat. This test seeds that exact DB state, invokes the private
history loader for each speaker in turn, and asserts zero leakage.

Pure unit test — no DeepSeek, no network. We drive the history loader
directly because the goal is the filtering invariant, not the full loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.orchestrator import _load_history_for_speaker
from planagent.db.models import (
    ConversationRole,
    ConversationTurn,
    GroupContext,
)
from planagent.main import run_migrations


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "split.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


async def _seed(sm) -> str:
    """Seed the bug-5 scenario. Returns group_id."""
    peng = "u-peng"
    chen = "u-chen"
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-split", name="split")
        session.add(group)
        await session.flush()
        gid = group.id

        # Peng starts a thread about 创业营报名.
        session.add(
            ConversationTurn(
                group_id=gid,
                role=ConversationRole.user,
                user_id=peng,
                target_user_id=peng,
                content="帮我记一下创业营报名的事",
            )
        )
        # 小计 asks peng a follow-up about 截止时间.
        session.add(
            ConversationTurn(
                group_id=gid,
                role=ConversationRole.assistant,
                user_id=None,
                target_user_id=peng,
                content="好嘞。这个报名截止时间是啥时候？",
            )
        )
        # Meanwhile Chenchen says something unrelated.
        session.add(
            ConversationTurn(
                group_id=gid,
                role=ConversationRole.user,
                user_id=chen,
                target_user_id=chen,
                content="OK",
            )
        )
        # And 小计's reply to Chenchen (imagine previous turn).
        session.add(
            ConversationTurn(
                group_id=gid,
                role=ConversationRole.assistant,
                user_id=None,
                target_user_id=chen,
                content="嗯嗯收到",
            )
        )
        await session.commit()
        return gid


async def test_history_loader_does_not_leak_across_speakers(sm) -> None:
    gid = await _seed(sm)

    peng_hist = await _load_history_for_speaker(
        sm, group_id=gid, speaker_user_id="u-peng", limit=50
    )
    chen_hist = await _load_history_for_speaker(
        sm, group_id=gid, speaker_user_id="u-chen", limit=50
    )

    peng_contents = [m.get("content", "") for m in peng_hist]
    chen_contents = [m.get("content", "") for m in chen_hist]

    # Peng's history sees peng's inbound + 小计's question TO peng.
    assert any("创业营" in c for c in peng_contents)
    assert any("截止时间" in c for c in peng_contents)
    # Peng's history does NOT contain anything that was targeted at chenchen.
    assert not any(c == "OK" for c in peng_contents)
    assert not any("嗯嗯收到" in c for c in peng_contents)

    # Chenchen's history must not see peng's creator-ship thread.
    assert not any("创业营" in c for c in chen_contents)
    assert not any("截止时间" in c for c in chen_contents)
    # Chenchen's history does see her own message and the reply to her.
    assert any(c == "OK" for c in chen_contents)
    assert any("嗯嗯收到" in c for c in chen_contents)


async def test_history_loader_empty_for_unknown_speaker(sm) -> None:
    gid = await _seed(sm)
    hist = await _load_history_for_speaker(
        sm, group_id=gid, speaker_user_id=None, limit=50
    )
    assert hist == []
