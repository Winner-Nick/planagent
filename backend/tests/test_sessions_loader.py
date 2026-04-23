"""Unit tests for the multi-session credentials loader + DB bootstrap."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy import select

from planagent import db as db_mod
from planagent.db.models import BotSession, GroupContext, GroupMember
from planagent.main import run_migrations
from planagent.wechat.sessions import (
    BootstrapService,
    SessionCredential,
    load_all_sessions,
    sync_sessions_to_db,
)


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator:
    db_file = tmp_path / "sess.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


def _write(dir_: Path, name: str, payload: dict) -> None:
    (dir_ / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_load_all_sessions_skips_legacy_and_junk(tmp_path) -> None:
    _write(tmp_path, "peng", {"bot_token": "tok-p", "baseurl": "https://example.com"})
    _write(tmp_path, "chenchen", {"bot_token": "tok-c"})
    _write(tmp_path, "credentials", {"bot_token": "legacy"})  # excluded
    (tmp_path / "MEMORY.md").write_text("not json", encoding="utf-8")  # excluded
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")  # wrong ext, excluded

    creds = load_all_sessions(cred_dir=tmp_path)
    names = sorted(c.name for c in creds)
    assert names == ["chenchen", "peng"]
    by_name = {c.name: c for c in creds}
    assert by_name["peng"].bot_token == "tok-p"
    assert by_name["peng"].baseurl == "https://example.com"
    assert by_name["chenchen"].baseurl is None


def test_load_all_sessions_skips_malformed(tmp_path) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path, "empty", {})  # no bot_token
    _write(tmp_path, "peng", {"bot_token": "ok"})
    creds = load_all_sessions(cred_dir=tmp_path)
    assert [c.name for c in creds] == ["peng"]


async def test_sync_sessions_to_db_is_idempotent(sm) -> None:
    creds = [
        SessionCredential(name="peng", bot_token="tok-p", baseurl=None),
        SessionCredential(name="chenchen", bot_token="tok-c", baseurl="https://x"),
    ]
    ids1 = await sync_sessions_to_db(sm, creds)
    assert len(ids1) == 2

    # One group, two sessions, two members.
    async with sm() as session:
        groups = (await session.execute(select(GroupContext))).scalars().all()
        assert len(groups) == 1
        sessions_rows = (await session.execute(select(BotSession))).scalars().all()
        assert {s.name for s in sessions_rows} == {"peng", "chenchen"}
        members = (await session.execute(select(GroupMember))).scalars().all()
        assert {m.display_name for m in members} == {"peng", "chenchen"}
        assert all(m.wechat_user_id is None for m in members)

    # Re-run with a rotated token — existing rows refreshed, no duplicates.
    creds2 = [
        SessionCredential(name="peng", bot_token="tok-p-NEW", baseurl=None),
        SessionCredential(name="chenchen", bot_token="tok-c", baseurl="https://x"),
    ]
    ids2 = await sync_sessions_to_db(sm, creds2)
    assert set(ids1) == set(ids2)
    async with sm() as session:
        sessions_rows = (await session.execute(select(BotSession))).scalars().all()
        peng = next(s for s in sessions_rows if s.name == "peng")
        assert peng.bot_token == "tok-p-NEW"


async def test_bootstrap_service_scans_dir(sm, tmp_path) -> None:
    _write(tmp_path, "peng", {"bot_token": "tok-p"})
    _write(tmp_path, "chenchen", {"bot_token": "tok-c"})
    svc = BootstrapService(sm)
    creds = await svc.sync_sessions_to_db(cred_dir=tmp_path)
    assert {c.name for c in creds} == {"peng", "chenchen"}
    async with sm() as session:
        count = len((await session.execute(select(BotSession))).scalars().all())
        assert count == 2
