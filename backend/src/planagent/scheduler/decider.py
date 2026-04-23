"""LLM-driven reminder decision.

`decide()` asks DeepSeek whether to remind for a given plan *now*, and if so,
when and with what text. No rule-based "if now > due" branches live here — the
prompt delivers context and the model answers with structured JSON.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from planagent.db.models import GroupMember, Plan, Reminder
from planagent.llm.deepseek import DeepSeekClient

BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ReminderDecision:
    should_remind: bool
    fire_at_local_iso: str | None
    message: str | None
    reason: str | None


def _pair(dt: datetime | None) -> dict[str, str] | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return {
        "local": dt.astimezone(BEIJING).isoformat(),
        "utc": dt.astimezone(UTC).isoformat(),
    }


def _render_plan(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "title": plan.title,
        "description": plan.description,
        "status": plan.status.value if hasattr(plan.status, "value") else str(plan.status),
        "start_at": _pair(plan.start_at),
        "due_at": _pair(plan.due_at),
        "recurrence_cron": plan.recurrence_cron,
        "expected_duration_per_session_min": plan.expected_duration_per_session_min,
        "priority": plan.priority,
        "owner_user_id": plan.owner_user_id,
    }


def _render_reminder(r: Reminder) -> dict[str, Any]:
    return {
        "fire_at": _pair(r.fire_at),
        "fired_at": _pair(r.fired_at),
        "status": r.status.value if hasattr(r.status, "value") else str(r.status),
        "message": r.message,
    }


def _render_members(members: list[GroupMember]) -> list[dict[str, Any]]:
    return [
        {
            "wechat_user_id": m.wechat_user_id,
            "display_name": m.display_name,
            "is_bot": m.is_bot,
        }
        for m in members
    ]


SYSTEM_PROMPT = (
    "You are the reminder scheduler for a WeChat-group plan manager. "
    "You receive a plan, the most recent reminders fired for it, group members, "
    "and the current Beijing time. Decide whether a reminder should be sent soon "
    "and, if yes, at exactly what local time and with what message. "
    "Weigh recurrence, start/due times, and recent reminder history so you do not spam — "
    "you (not any external rule) decide whether a recent reminder means we should hold off. "
    "Completed or cancelled plans never need reminders. "
    "Messages must be concise Chinese (or the plan's language), "
    "address the owner with '@<display_name>' when useful, and never invent facts not in the plan. "
    "Respond with a single JSON object matching exactly this schema:\n"
    "{\n"
    '  "should_remind": boolean,\n'
    '  "fire_at_local_iso": string|null,   // ISO-8601 with +08:00 offset\n'
    '  "message": string|null,\n'
    '  "reason": string|null\n'
    "}\n"
    "If should_remind is false, fire_at_local_iso and message may be null."
)


async def decide(
    plan: Plan,
    *,
    now_local: datetime,
    recent_reminders: list[Reminder],
    deepseek: DeepSeekClient,
    group_members: list[GroupMember] | None = None,
) -> ReminderDecision:
    payload = {
        "now_local_beijing": now_local.astimezone(BEIJING).isoformat(),
        "plan": _render_plan(plan),
        "recent_reminders": [_render_reminder(r) for r in recent_reminders[:3]],
        "group_members": _render_members(group_members or []),
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    def _call() -> Any:
        return deepseek.chat(
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )

    resp = await asyncio.to_thread(_call)
    content = resp.choices[0].message.content or "{}"
    data = json.loads(content)
    # Strict bool: `"false"` / `"no"` / `0` must NOT round-trip to True.
    # Accept only a JSON boolean; anything else is conservatively False.
    raw_should = data.get("should_remind", False)
    should_remind = raw_should is True
    return ReminderDecision(
        should_remind=should_remind,
        fire_at_local_iso=data.get("fire_at_local_iso"),
        message=data.get("message"),
        reason=data.get("reason"),
    )
