"""Per-tool unit tests against a real SQLite DB. No mocks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.tools import TOOL_REGISTRY, ToolContext
from planagent.db.models import (
    GroupContext,
    Plan,
    PlanStatus,
    Reminder,
    ReminderStatus,
)
from planagent.main import run_migrations


@pytest_asyncio.fixture
async def session_factory(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "agent.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    yield db_mod.get_sessionmaker()
    await db_mod.dispose_engine()


@pytest_asyncio.fixture
async def group_id(session_factory) -> str:
    async with session_factory() as session:
        g = GroupContext(wechat_group_id="wx-group-tool", name="Tool Test")
        session.add(g)
        await session.commit()
        return g.id


async def _make_ctx(session_factory, group_id: str) -> tuple[ToolContext, list[str]]:
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    return (
        ToolContext(
            session_factory=session_factory,
            group_id=group_id,
            wechat_group_id="wx-group-tool",
            wechat_send=_send,
        ),
        sent,
    )


async def test_create_plan_draft_and_get(session_factory, group_id: str) -> None:
    ctx, _ = await _make_ctx(session_factory, group_id)
    result = await TOOL_REGISTRY["create_plan_draft"].handler(
        ctx, title="Learn Rust", owner_user_id="u-peng"
    )
    assert result["title"] == "Learn Rust"
    assert result["status"] == "draft"
    assert result["group_id"] == group_id
    plan_id = result["id"]

    got = await TOOL_REGISTRY["get_plan"].handler(ctx, plan_id=plan_id)
    assert got["id"] == plan_id
    assert got["owner_user_id"] == "u-peng"

    missing = await TOOL_REGISTRY["get_plan"].handler(ctx, plan_id="does-not-exist")
    assert missing["error"] == "plan_not_found"


async def test_list_plans_filters(session_factory, group_id: str) -> None:
    ctx, _ = await _make_ctx(session_factory, group_id)
    await TOOL_REGISTRY["create_plan_draft"].handler(ctx, title="A")
    p2 = await TOOL_REGISTRY["create_plan_draft"].handler(ctx, title="B")
    await TOOL_REGISTRY["update_plan"].handler(
        ctx, plan_id=p2["id"], fields={"status": "active"}
    )

    everything = await TOOL_REGISTRY["list_plans"].handler(ctx)
    assert {p["title"] for p in everything} == {"A", "B"}

    active_only = await TOOL_REGISTRY["list_plans"].handler(ctx, status="active")
    assert [p["title"] for p in active_only] == ["B"]


async def test_update_plan_merges_metadata_and_rejects_bad(
    session_factory, group_id: str
) -> None:
    ctx, _ = await _make_ctx(session_factory, group_id)
    p = await TOOL_REGISTRY["create_plan_draft"].handler(ctx, title="X")

    r1 = await TOOL_REGISTRY["update_plan"].handler(
        ctx,
        plan_id=p["id"],
        fields={"metadata_json": {"a": 1}, "priority": 3},
    )
    assert r1["metadata_json"] == {"a": 1}
    assert r1["priority"] == 3

    r2 = await TOOL_REGISTRY["update_plan"].handler(
        ctx, plan_id=p["id"], fields={"metadata_json": {"b": 2}}
    )
    assert r2["metadata_json"] == {"a": 1, "b": 2}

    bad = await TOOL_REGISTRY["update_plan"].handler(
        ctx, plan_id=p["id"], fields={"status": "not_a_status"}
    )
    assert bad["error"] == "validation_error"


async def test_mark_plan_complete_and_delete(session_factory, group_id: str) -> None:
    ctx, _ = await _make_ctx(session_factory, group_id)
    p = await TOOL_REGISTRY["create_plan_draft"].handler(ctx, title="Done soon")

    done = await TOOL_REGISTRY["mark_plan_complete"].handler(ctx, plan_id=p["id"])
    assert done["status"] == "completed"

    deleted = await TOOL_REGISTRY["delete_plan"].handler(ctx, plan_id=p["id"])
    assert deleted == {"ok": True, "plan_id": p["id"]}

    async with session_factory() as session:
        assert await session.get(Plan, p["id"]) is None


async def test_schedule_and_cancel_reminder(session_factory, group_id: str) -> None:
    ctx, _ = await _make_ctx(session_factory, group_id)
    p = await TOOL_REGISTRY["create_plan_draft"].handler(ctx, title="Workout")

    fire_at = (datetime.now(UTC) + timedelta(hours=5)).isoformat()
    r = await TOOL_REGISTRY["schedule_reminder"].handler(
        ctx, plan_id=p["id"], fire_at=fire_at, message="go run"
    )
    assert r["message"] == "go run"
    assert r["status"] == "pending"

    async with session_factory() as session:
        rows = (await session.execute(select(Reminder))).scalars().all()
        assert len(rows) == 1
        # fire_at is stored (SQLite strips tz, but the column is DateTime(tz=True)).
        assert rows[0].fire_at is not None

    cancel = await TOOL_REGISTRY["cancel_reminder"].handler(
        ctx, reminder_id=r["id"]
    )
    assert cancel == {"ok": True, "reminder_id": r["id"]}
    async with session_factory() as session:
        rem = await session.get(Reminder, r["id"])
        assert rem is not None
        assert rem.status == ReminderStatus.cancelled


async def test_schedule_reminder_parses_shanghai_tz(session_factory, group_id: str) -> None:
    ctx, _ = await _make_ctx(session_factory, group_id)
    p = await TOOL_REGISTRY["create_plan_draft"].handler(ctx, title="Z")
    # 08:00 Asia/Shanghai == 00:00 UTC
    r = await TOOL_REGISTRY["schedule_reminder"].handler(
        ctx, plan_id=p["id"], fire_at="2099-01-02T08:00:00+08:00", message="m"
    )
    async with session_factory() as session:
        rem = await session.get(Reminder, r["id"])
        assert rem is not None
        # SQLite returns naive; the column stored UTC equivalent of 08:00+08 == 00:00 UTC.
        stored = rem.fire_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        assert stored.astimezone(UTC) == datetime(2099, 1, 2, 0, 0, tzinfo=UTC)


async def test_reply_and_ask_use_wechat_send(session_factory, group_id: str) -> None:
    ctx, sent = await _make_ctx(session_factory, group_id)

    r1 = await TOOL_REGISTRY["reply_in_group"].handler(ctx, text="hi")
    assert r1 == {"sent": True}
    r2 = await TOOL_REGISTRY["ask_user_in_group"].handler(ctx, question="when?")
    assert r2 == {"sent": True}
    assert sent == ["hi", "when?"]


async def test_record_note_appends_list(session_factory, group_id: str) -> None:
    ctx, _ = await _make_ctx(session_factory, group_id)
    p = await TOOL_REGISTRY["create_plan_draft"].handler(ctx, title="Notes target")

    r = await TOOL_REGISTRY["record_note"].handler(ctx, plan_id=p["id"], note="n1")
    assert r["metadata_json"]["notes"] == ["n1"]
    r = await TOOL_REGISTRY["record_note"].handler(ctx, plan_id=p["id"], note="n2")
    assert r["metadata_json"]["notes"] == ["n1", "n2"]


async def test_tool_schemas_shape() -> None:
    from planagent.agent.tools import tool_schemas

    schemas = tool_schemas()
    names = {s["function"]["name"] for s in schemas}
    expected = {
        "list_plans",
        "get_plan",
        "create_plan_draft",
        "update_plan",
        "mark_plan_complete",
        # PR-I: audit-preserving cancellation, alongside the permanent delete.
        "cancel_plan",
        "delete_plan",
        "schedule_reminder",
        "cancel_reminder",
        "reply_in_group",
        "ask_user_in_group",
        "record_note",
        # PR-H: cross-user whiteboard tools.
        "note_for_peer",
        "peek_peer_state",
        "send_to_peer_async",
        # PR-L: one-off scheduled nudges.
        "schedule_message_to_peer",
    }
    assert names == expected
    for s in schemas:
        assert s["type"] == "function"
        assert "parameters" in s["function"]
        params = s["function"]["parameters"]
        assert params["type"] == "object"


async def test_list_plans_scoped_to_group(session_factory, group_id: str) -> None:
    # Create a second group with its own plan, ensure default scoping.
    async with session_factory() as session:
        g2 = GroupContext(wechat_group_id="wx-group-other")
        session.add(g2)
        await session.commit()
        gid2 = g2.id

    ctx, _ = await _make_ctx(session_factory, group_id)
    await TOOL_REGISTRY["create_plan_draft"].handler(ctx, title="mine")
    ctx2, _ = await _make_ctx(session_factory, gid2)
    await TOOL_REGISTRY["create_plan_draft"].handler(ctx2, title="theirs")

    mine = await TOOL_REGISTRY["list_plans"].handler(ctx)
    assert {p["title"] for p in mine} == {"mine"}


async def test_plan_status_enum_coverage() -> None:
    # Sanity: PlanStatus enum must include all states we reference in prompts.
    values = {s.value for s in PlanStatus}
    assert {"draft", "active", "completed", "paused"} <= values
