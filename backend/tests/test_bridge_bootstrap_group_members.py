"""PR-G bug #1 support: bridge bootstrap pre-fills the two known humans.

Asserts that calling `sync_sessions_to_db` on an empty DB creates GroupMember
rows for 鹏鹏 + 辰辰 with their stable wechat_user_ids and real Chinese
display names — so the orchestrator's prompt can address them by name from
turn one instead of having the agent ask for a user_id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select

from planagent import db as db_mod
from planagent.db.models import GroupMember
from planagent.main import run_migrations
from planagent.wechat.constants import CHENCHEN, KNOWN_HUMANS, PENG
from planagent.wechat.sessions import SessionCredential, sync_sessions_to_db


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator:
    db_file = tmp_path / "bootstrap.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


async def test_bootstrap_prefills_known_humans(sm) -> None:
    creds = [
        SessionCredential(name=PENG.cred_name, bot_token="tok-p", baseurl=None),
        SessionCredential(name=CHENCHEN.cred_name, bot_token="tok-c", baseurl=None),
    ]
    await sync_sessions_to_db(sm, creds)

    async with sm() as session:
        members = (await session.execute(select(GroupMember))).scalars().all()

    by_uid = {m.wechat_user_id: m for m in members}
    for h in KNOWN_HUMANS:
        assert h.wechat_user_id in by_uid, (
            f"known human {h.display_name} not pre-filled: {list(by_uid)}"
        )
        assert by_uid[h.wechat_user_id].display_name == h.display_name

    # No duplicate member rows: one per known human, nothing extra.
    assert len(members) == len(KNOWN_HUMANS)


async def test_bootstrap_is_idempotent_on_members(sm) -> None:
    creds = [
        SessionCredential(name=PENG.cred_name, bot_token="tok-p", baseurl=None),
        SessionCredential(name=CHENCHEN.cred_name, bot_token="tok-c", baseurl=None),
    ]
    await sync_sessions_to_db(sm, creds)
    await sync_sessions_to_db(sm, creds)

    async with sm() as session:
        members = (await session.execute(select(GroupMember))).scalars().all()
    # Still exactly two.
    assert len(members) == len(KNOWN_HUMANS)
