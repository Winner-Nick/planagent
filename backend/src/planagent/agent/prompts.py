"""System prompt assembly.

Layout is cache-friendly: the stable prefix (role, invariants, tool contract,
plan schema) comes first and never changes across turns. The volatile section
(current group, members, active plans, now()) comes last so DeepSeek's prefix
cache can hit on everything before the `VOLATILE CONTEXT` marker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from planagent.db.models import PlanStatus, ReminderStatus

VOLATILE_MARKER = "### VOLATILE CONTEXT ###"


# --- Stable prefix -----------------------------------------------------------

_ROLE_AND_INVARIANTS = """\
You are Planagent, a plan-manager assistant embedded in a WeChat group chat.
Group members talk to you in Chinese or English; answer in the language they used.

Core behavior:
- You act entirely through the provided tools. Do not freestyle plans in natural
  language when a tool exists for the action.
- When a user describes a new plan, call `create_plan_draft` as soon as a title
  is clear. Fill remaining fields with `update_plan` as information arrives.
- REQUIRED plan fields for activation: title, owner_user_id, and at least one of
  {due_at, recurrence_cron}, plus expected_duration_per_session_min when the
  user framed the plan in sessions (e.g. "每天 30 分钟").
- If any required field is still unknown, you MUST call `ask_user_in_group`
  with ONE focused question and then stop the turn. Never guess a value,
  never invent a default, never fabricate a due date or a cron.
- All user-visible times are Asia/Shanghai (UTC+08:00). When you emit
  `fire_at`, `start_at`, or `due_at`, use ISO-8601 with an explicit +08:00
  offset. The backend stores UTC; do not pre-convert.
- After making changes, if you want to say something to the group, call
  `reply_in_group`. A tool-less natural-language answer is allowed but will be
  treated as an implicit `reply_in_group`.
- When the user asks about plans, prefer `list_plans` / `get_plan` over
  guessing from prior context.
- When marking something done, call `mark_plan_complete`.
- One tool call per step is fine; the orchestrator will loop. You have at most
  6 rounds per user message — finish within that budget.
"""


def _plan_schema_fragment() -> str:
    """Serialized JSON description of the Plan model + enumerations.

    This block is deliberately small and STABLE: no timestamps, no group ids,
    no per-turn data. Keeping it frozen lets the API-side cache match.
    """
    payload = {
        "Plan": {
            "fields": {
                "id": "string (uuid, server-assigned)",
                "group_id": "string (defaults to current group)",
                "title": "string",
                "description": "string | null",
                "status": f"enum {[s.value for s in PlanStatus]}",
                "start_at": "ISO-8601 datetime with +08:00 tz | null",
                "due_at": "ISO-8601 datetime with +08:00 tz | null",
                "expected_duration_per_session_min": "integer minutes | null",
                "recurrence_cron": "5-field cron string | null",
                "priority": "integer (0 default)",
                "owner_user_id": "wechat user id | null",
                "metadata_json": "object (free-form, notes go under .notes[])",
            },
        },
        "Reminder": {
            "fields": {
                "id": "string (uuid)",
                "plan_id": "string",
                "fire_at": "ISO-8601 datetime with tz",
                "message": "string",
                "status": f"enum {[s.value for s in ReminderStatus]}",
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


STABLE_PREFIX = (
    _ROLE_AND_INVARIANTS
    + "\n\nData model (JSON):\n```json\n"
    + _plan_schema_fragment()
    + "\n```\n"
)


# --- Volatile context --------------------------------------------------------


@dataclass
class GroupSnapshot:
    group_id: str
    wechat_group_id: str
    group_name: str | None
    members: list[dict[str, Any]]  # {wechat_user_id, display_name}
    plans: list[dict[str, Any]]  # {id, title, status, next_fire_at}


def _render_volatile(snapshot: GroupSnapshot, now: datetime) -> str:
    members_line = (
        ", ".join(
            f"{m.get('display_name') or '?'}({m['wechat_user_id']})"
            for m in snapshot.members
        )
        or "(none recorded yet)"
    )
    if snapshot.plans:
        plan_lines = []
        for p in snapshot.plans:
            nxt = p.get("next_fire_at")
            plan_lines.append(
                f"- [{p['status']}] {p['title']} (id={p['id']})"
                + (f"  next_fire_at={nxt}" if nxt else "")
            )
        plans_block = "\n".join(plan_lines)
    else:
        plans_block = "(no plans yet)"

    return (
        f"{VOLATILE_MARKER}\n"
        f"now (Asia/Shanghai): {now.isoformat()}\n"
        f"group: {snapshot.group_name or '?'} "
        f"(internal_id={snapshot.group_id}, wechat_id={snapshot.wechat_group_id})\n"
        f"members: {members_line}\n"
        f"plans:\n{plans_block}\n"
    )


def make_prompt(snapshot: GroupSnapshot, *, now: datetime) -> str:
    """Build the full system prompt. Stable prefix first, volatile tail last."""
    return STABLE_PREFIX + "\n" + _render_volatile(snapshot, now)


def stable_prefix_bytes() -> bytes:
    """Exposed for cache-alignment assertions."""
    return STABLE_PREFIX.encode("utf-8")
