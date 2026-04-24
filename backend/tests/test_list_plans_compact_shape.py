"""PR-M: ``list_plans`` must return a compact at-a-glance shape.

The previous full-Plan payload leaked raw UUIDs and ISO timestamps into
the LLM's context, which it would then parrot back at the user. This test
pins the new shape so a regression (e.g. someone reverting to
``_serialize_plan``) fails loudly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent import db as db_mod
from planagent.agent.tools import TOOL_REGISTRY, ToolContext
from planagent.db.models import GroupContext
from planagent.main import run_migrations
from planagent.wechat.constants import CHENCHEN, PENG


@pytest_asyncio.fixture
async def sm(tmp_path, monkeypatch) -> AsyncIterator[async_sessionmaker]:
    db_file = tmp_path / "list_plans_compact.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    try:
        yield db_mod.get_sessionmaker()
    finally:
        await db_mod.dispose_engine()


@pytest_asyncio.fixture
async def group_id(sm) -> str:
    async with sm() as session:
        g = GroupContext(wechat_group_id="wx-compact", name="Compact")
        session.add(g)
        await session.commit()
        return g.id


def _mk_ctx(sm, group_id: str, sender: str) -> ToolContext:
    async def _send(text: str) -> None:
        return None

    return ToolContext(
        session_factory=sm,
        group_id=group_id,
        wechat_group_id="wx-compact",
        wechat_send=_send,
        sender_user_id=sender,
        peer_user_id=CHENCHEN.wechat_user_id,
    )


async def test_list_plans_returns_compact_fields(sm, group_id) -> None:
    ctx_peng = _mk_ctx(sm, group_id, PENG.wechat_user_id)
    await TOOL_REGISTRY["create_plan_draft"].handler(
        ctx_peng, title="学 Rust", owner="speaker"
    )
    p2 = await TOOL_REGISTRY["create_plan_draft"].handler(
        ctx_peng, title="跑步", owner="peer"
    )
    # Give p2 a start_at so the friendly `next` field has something to render.
    future_iso = datetime(2099, 6, 1, 9, 0, tzinfo=UTC).isoformat()
    await TOOL_REGISTRY["update_plan"].handler(
        ctx_peng, plan_id=p2["id"], fields={"start_at": future_iso}
    )

    rows = await TOOL_REGISTRY["list_plans"].handler(ctx_peng)
    assert isinstance(rows, list) and len(rows) == 2

    # Shape: only compact keys exposed.
    allowed_keys = {"id", "title", "status", "next", "owner"}
    for r in rows:
        assert set(r.keys()) == allowed_keys, r

    # Owner shortening: peng / chenchen / None — never a raw wechat_user_id.
    owners = {r["owner"] for r in rows}
    assert owners <= {"peng", "chenchen", None}
    assert PENG.wechat_user_id not in owners
    assert CHENCHEN.wechat_user_id not in owners

    # No raw ISO or tz marker in the visible summary fields.
    for r in rows:
        for key in ("title", "status", "next", "owner"):
            val = r.get(key)
            if isinstance(val, str):
                assert "T" not in val or key == "id", (
                    f"{key}={val!r} looks like ISO"
                )
                assert "+08:00" not in val, f"{key}={val!r} leaks tz"
                assert "+00:00" not in val, f"{key}={val!r} leaks tz"

    # The `next` field either renders a colloquial form or is None.
    running_next = [r["next"] for r in rows if r["next"] is not None]
    # At least the one with start_at should have produced something.
    assert running_next, "expected at least one friendly `next` render"
    for val in running_next:
        assert "T" not in val
        assert "+" not in val


async def test_get_plan_still_returns_full_shape(sm, group_id) -> None:
    # list_plans trims; get_plan must still give the LLM the raw fields it
    # needs to call update_plan / schedule_reminder / etc. Guard against
    # accidentally trimming both.
    ctx = _mk_ctx(sm, group_id, PENG.wechat_user_id)
    created = await TOOL_REGISTRY["create_plan_draft"].handler(ctx, title="alpha")
    got = await TOOL_REGISTRY["get_plan"].handler(ctx, plan_id=created["id"])
    # Expected-full fields.
    for key in (
        "id",
        "title",
        "status",
        "start_at",
        "due_at",
        "priority",
        "metadata_json",
        "next_fire_at_friendly",
    ):
        assert key in got, f"get_plan missing {key!r}"
