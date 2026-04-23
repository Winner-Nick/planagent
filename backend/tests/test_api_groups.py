from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC

import httpx
import pytest_asyncio

from planagent import db as db_mod
from planagent.db.models import ConversationRole, ConversationTurn, GroupContext
from planagent.main import create_app, run_migrations


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    db_file = tmp_path / "groups.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await db_mod.dispose_engine()


async def test_list_and_get_groups(client: httpx.AsyncClient) -> None:
    sm = db_mod.get_sessionmaker()
    async with sm() as session:
        g1 = GroupContext(wechat_group_id="g1", name="one")
        g2 = GroupContext(wechat_group_id="g2", name="two")
        session.add_all([g1, g2])
        await session.commit()
        gid = g1.id

    r = await client.get("/api/v1/groups")
    assert r.status_code == 200
    assert {g["wechat_group_id"] for g in r.json()} == {"g1", "g2"}

    r = await client.get(f"/api/v1/groups/{gid}")
    assert r.status_code == 200
    assert r.json()["name"] == "one"

    r = await client.get("/api/v1/groups/missing")
    assert r.status_code == 404


async def test_conversation_turns_persist_and_order(client: httpx.AsyncClient) -> None:
    sm = db_mod.get_sessionmaker()
    async with sm() as session:
        g = GroupContext(wechat_group_id="g-conv")
        session.add(g)
        await session.commit()
        gid = g.id

        # add in deterministic creation order
        from datetime import datetime, timedelta

        base = datetime.now(UTC)
        turns = [
            ConversationTurn(
                group_id=gid,
                role=ConversationRole.user,
                user_id="u1",
                content="hello",
                created_at=base,
            ),
            ConversationTurn(
                group_id=gid,
                role=ConversationRole.assistant,
                content="hi there",
                created_at=base + timedelta(seconds=1),
            ),
            ConversationTurn(
                group_id=gid,
                role=ConversationRole.tool,
                tool_call_id="call_1",
                tool_calls_json={"name": "lookup", "args": {"q": "x"}},
                created_at=base + timedelta(seconds=2),
            ),
        ]
        session.add_all(turns)
        await session.commit()

    r = await client.get(f"/api/v1/groups/{gid}/conversations")
    assert r.status_code == 200
    data = r.json()
    # oldest-first chronological
    contents = [t["content"] for t in data]
    roles = [t["role"] for t in data]
    assert contents == ["hello", "hi there", None]
    assert roles == ["user", "assistant", "tool"]
    assert data[2]["tool_calls_json"] == {"name": "lookup", "args": {"q": "x"}}

    # limit keeps newest N then returns oldest-first within that slice
    r = await client.get(f"/api/v1/groups/{gid}/conversations", params={"limit": 2})
    assert [t["role"] for t in r.json()] == ["assistant", "tool"]

    r = await client.get("/api/v1/groups/missing/conversations")
    assert r.status_code == 404
