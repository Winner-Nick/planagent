"""Real DeepSeek tests for the reminder decider — NO mocks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from planagent.db.models import Plan, PlanStatus
from planagent.llm.deepseek import DeepSeekClient
from planagent.scheduler.decider import decide

BEIJING = ZoneInfo("Asia/Shanghai")


@pytest.fixture(scope="module")
def deepseek() -> DeepSeekClient:
    return DeepSeekClient()


def _plan(**overrides) -> Plan:
    base = {
        "id": "plan-test",
        "group_id": "grp-test",
        "title": "Rust learning daily",
        "description": "Read chapter + write exercises for 30 minutes.",
        "status": PlanStatus.active,
        "priority": 0,
        "metadata_json": {},
    }
    base.update(overrides)
    now = datetime.now(UTC)
    # placeholder created_at/updated_at so dataclass-like access doesn't blow up
    p = Plan(**base)
    p.created_at = now
    p.updated_at = now
    return p


@pytest.mark.real_api
async def test_decider_due_soon_should_produce_structured_decision(
    deepseek: DeepSeekClient,
) -> None:
    now_utc = datetime.now(UTC)
    due_at = now_utc + timedelta(hours=2)
    plan = _plan(
        title="Ship PR-E today",
        description="Finish scheduler + tests before 18:00 Beijing.",
        start_at=now_utc + timedelta(minutes=30),
        due_at=due_at,
    )
    dec = await decide(
        plan,
        now_local=now_utc.astimezone(BEIJING),
        recent_reminders=[],
        deepseek=deepseek,
    )
    assert isinstance(dec.should_remind, bool)
    if dec.should_remind:
        assert dec.fire_at_local_iso, "expected an ISO fire_at when should_remind=True"
        fire_at = datetime.fromisoformat(dec.fire_at_local_iso)
        assert fire_at.tzinfo is not None
        # Sanity window: not wildly past, not beyond due_at + 6h.
        assert fire_at.astimezone(UTC) <= due_at + timedelta(hours=6)
        assert fire_at.astimezone(UTC) >= now_utc - timedelta(hours=1)
        assert isinstance(dec.message, str) and dec.message.strip()


@pytest.mark.real_api
async def test_decider_completed_plan_should_not_remind(deepseek: DeepSeekClient) -> None:
    now_utc = datetime.now(UTC)
    plan = _plan(
        status=PlanStatus.completed,
        title="Learn to bake sourdough",
        description="Already finished the program last month.",
        start_at=now_utc - timedelta(days=60),
        due_at=now_utc - timedelta(days=30),
    )
    dec = await decide(
        plan,
        now_local=now_utc.astimezone(BEIJING),
        recent_reminders=[],
        deepseek=deepseek,
    )
    assert dec.should_remind is False
