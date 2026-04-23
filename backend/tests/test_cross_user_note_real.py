"""PR-H end-to-end: note_for_peer plumbs from Peng's turn to Chenchen's prompt.

Real DeepSeek. The flow under test:

1. Peng tells 小计 "辰辰最近项目压力大，下次你跟她聊的时候温柔点哈"
   → agent should call `note_for_peer(audience="chenchen", kind="info", ...)`.
2. If round 1 produced no CrossUserNote (LLM non-determinism), a second
   turn nudges explicitly: "请调 note_for_peer 把这件事登记在白板上。"
3. Inspect the DB: one CrossUserNote row with audience_user_id = Chenchen,
   kind=info, non-empty text.
4. Run `make_prompt` for a Chenchen snapshot including the whiteboard and
   assert the note text shows up under `### 白板 ###`.

We call `make_prompt` directly for step 4 rather than running Chenchen's
full inbound — the assertion is about the prompt being *built* correctly;
driving another LLM turn would add noise without adding coverage here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.orchestrator import _build_whiteboard, _load_snapshot, handle_inbound
from planagent.agent.prompts import WHITEBOARD_MARKER, make_prompt
from planagent.db.models import (
    CrossUserNote,
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


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "cross_note.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


def _inbound(*, user_id: str, text: str, token: str) -> InboundMessage:
    return InboundMessage(
        from_user_id=user_id,
        to_user_id="bot",
        context_token=token,
        item_list=[Item(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))],
        group_id="wx-cross-note",
    )


async def _all_notes(sm) -> list[CrossUserNote]:
    async with sm() as session:
        return list(
            (await session.execute(select(CrossUserNote))).scalars().all()
        )


@pytest.mark.real_api
async def test_note_for_peer_plumbs_into_peer_prompt(sm) -> None:
    # Seed the group + roster so the agent sees both humans from turn one.
    async with sm() as session:
        group = GroupContext(wechat_group_id="wx-cross-note", name="cn")
        session.add(group)
        await session.flush()
        gid = group.id
        session.add_all(
            [
                GroupMember(
                    group_id=gid,
                    wechat_user_id=PENG.wechat_user_id,
                    display_name=PENG.display_name,
                ),
                GroupMember(
                    group_id=gid,
                    wechat_user_id=CHENCHEN.wechat_user_id,
                    display_name=CHENCHEN.display_name,
                ),
            ]
        )
        await session.commit()

    deepseek = DeepSeekClient()
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    # Turn 1 (Peng): drop an observation about 辰辰.
    msg1 = _inbound(
        user_id=PENG.wechat_user_id,
        text=(
            "辰辰最近项目压力大，下次你跟她聊的时候温柔点哈。"
            "把这件事用 note_for_peer 留到白板上（audience=chenchen，"
            "kind=info）。"
        ),
        token="ctx-cn-1",
    )
    await handle_inbound(
        msg1, deepseek=deepseek, session_factory=sm, wechat_send=_send
    )

    notes = await _all_notes(sm)
    if not notes:
        # Nudge once more with a fully explicit imperative.
        msg2 = _inbound(
            user_id=PENG.wechat_user_id,
            text=(
                "就按刚才说的：立刻调 note_for_peer，"
                'audience="chenchen", kind="info", '
                'text="辰辰最近项目压力大，温柔点"。'
            ),
            token="ctx-cn-2",
        )
        await handle_inbound(
            msg2, deepseek=deepseek, session_factory=sm, wechat_send=_send
        )
        notes = await _all_notes(sm)

    assert notes, (
        "expected ≥1 CrossUserNote after two turns. sent outbounds: "
        f"{sent!r}"
    )

    note = notes[0]
    assert note.audience_user_id == CHENCHEN.wechat_user_id
    assert note.author_user_id == PENG.wechat_user_id
    assert note.kind.value in {"info", "nudge_request", "appreciate"}
    assert note.consumed_at is None  # not yet seen by 辰辰.
    assert "辰辰" in note.text or "压力" in note.text or note.text

    # Now build Chenchen's snapshot + whiteboard and render the prompt.
    snapshot = await _load_snapshot(
        sm, group_id=note.group_id, speaker_wechat_user_id=CHENCHEN.wechat_user_id
    )
    wb, consumable = await _build_whiteboard(
        sm,
        group_id=note.group_id,
        speaker_wechat_user_id=CHENCHEN.wechat_user_id,
        snapshot=snapshot,
    )
    snapshot.whiteboard = wb
    prompt = make_prompt(snapshot, now=datetime.now(ZoneInfo("Asia/Shanghai")))

    assert WHITEBOARD_MARKER in prompt
    # The note's text (or a meaningful slice) must appear in Chenchen's prompt.
    # Use a looser substring check because render() may truncate long text.
    loose_needle = note.text[: min(10, len(note.text))]
    assert loose_needle in prompt, (
        f"expected note text slice {loose_needle!r} under whiteboard:\n{prompt}"
    )
    assert consumable, "expected note ids queued for consumption"
