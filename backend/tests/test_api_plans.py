from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC

import httpx
import pytest_asyncio

from planagent import db as db_mod
from planagent.db.models import GroupContext
from planagent.main import create_app, run_migrations


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    db_file = tmp_path / "plans.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("PLANAGENT_DB_URL", url)
    run_migrations(url)
    db_mod.init_engine(url)
    app = create_app()
    # lifespan not triggered by ASGITransport; engine already initialized above.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await db_mod.dispose_engine()


@pytest_asyncio.fixture
async def group_id(client: httpx.AsyncClient) -> str:
    sm = db_mod.get_sessionmaker()
    async with sm() as session:
        g = GroupContext(wechat_group_id="wx-group-1", name="Test Group")
        session.add(g)
        await session.commit()
        return g.id


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_plan_crud(client: httpx.AsyncClient, group_id: str) -> None:
    # create
    payload = {"group_id": group_id, "title": "Ship PR-A", "priority": 5}
    r = await client.post("/api/v1/plans", json=payload)
    assert r.status_code == 201, r.text
    plan = r.json()
    plan_id = plan["id"]
    assert plan["title"] == "Ship PR-A"
    assert plan["status"] == "draft"
    assert plan["priority"] == 5
    assert plan["metadata_json"] == {}

    # get
    r = await client.get(f"/api/v1/plans/{plan_id}")
    assert r.status_code == 200
    assert r.json()["id"] == plan_id

    # patch
    r = await client.patch(
        f"/api/v1/plans/{plan_id}",
        json={"status": "active", "description": "now active"},
    )
    assert r.status_code == 200
    updated = r.json()
    assert updated["status"] == "active"
    assert updated["description"] == "now active"

    # list
    r = await client.get("/api/v1/plans")
    assert r.status_code == 200
    assert any(p["id"] == plan_id for p in r.json())

    # delete
    r = await client.delete(f"/api/v1/plans/{plan_id}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/plans/{plan_id}")
    assert r.status_code == 404


async def test_get_missing_plan_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/plans/does-not-exist")
    assert r.status_code == 404


async def test_filters_group_id_and_status(
    client: httpx.AsyncClient, group_id: str
) -> None:
    # second group
    sm = db_mod.get_sessionmaker()
    async with sm() as session:
        g2 = GroupContext(wechat_group_id="wx-group-2")
        session.add(g2)
        await session.commit()
        other_gid = g2.id

    # create plans in both groups with varied status
    for gid, title, status_ in [
        (group_id, "A", "draft"),
        (group_id, "B", "active"),
        (other_gid, "C", "active"),
    ]:
        r = await client.post(
            "/api/v1/plans",
            json={"group_id": gid, "title": title, "status": status_},
        )
        assert r.status_code == 201

    r = await client.get("/api/v1/plans", params={"group_id": group_id})
    titles = sorted(p["title"] for p in r.json())
    assert titles == ["A", "B"]

    r = await client.get("/api/v1/plans", params={"status": "active"})
    titles = sorted(p["title"] for p in r.json())
    assert titles == ["B", "C"]

    r = await client.get(
        "/api/v1/plans", params={"group_id": group_id, "status": "active"}
    )
    titles = sorted(p["title"] for p in r.json())
    assert titles == ["B"]


async def test_create_plan_unknown_group_404(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/plans", json={"group_id": "nope", "title": "x"}
    )
    assert r.status_code == 404


async def test_patch_rejects_null_on_non_nullable_fields(
    client: httpx.AsyncClient, group_id: str
) -> None:
    r = await client.post(
        "/api/v1/plans", json={"group_id": group_id, "title": "immortal"}
    )
    plan_id = r.json()["id"]

    for field in ("title", "status", "priority", "metadata_json"):
        r = await client.patch(f"/api/v1/plans/{plan_id}", json={field: None})
        assert r.status_code == 422, (field, r.text)
        assert "cannot be null" in r.json()["detail"]

    # Nullable field still accepts null.
    r = await client.patch(f"/api/v1/plans/{plan_id}", json={"description": None})
    assert r.status_code == 200
    assert r.json()["description"] is None


async def test_plan_reminders(client: httpx.AsyncClient, group_id: str) -> None:
    from datetime import datetime, timedelta

    from planagent.db.models import Reminder

    r = await client.post(
        "/api/v1/plans", json={"group_id": group_id, "title": "with reminders"}
    )
    plan_id = r.json()["id"]

    sm = db_mod.get_sessionmaker()
    now = datetime.now(UTC)
    async with sm() as session:
        session.add(Reminder(plan_id=plan_id, fire_at=now + timedelta(hours=2), message="2h"))
        session.add(Reminder(plan_id=plan_id, fire_at=now + timedelta(hours=1), message="1h"))
        await session.commit()

    r = await client.get(f"/api/v1/plans/{plan_id}/reminders")
    assert r.status_code == 200
    msgs = [x["message"] for x in r.json()]
    assert msgs == ["1h", "2h"]

    r = await client.get("/api/v1/plans/missing/reminders")
    assert r.status_code == 404
