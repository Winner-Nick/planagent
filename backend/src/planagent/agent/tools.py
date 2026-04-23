"""Declarative tool registry for the DeepSeek agent.

Every natural-language decision flows through DeepSeek. This module only
provides mechanics: JSON schema, handlers that hit the real DB, and a way
to enqueue outbound WeChat sends via a caller-supplied coroutine.

Outbound policy: `reply_in_group` and `ask_user_in_group` invoke the
`wechat_send` coroutine stored on `ToolContext`. The actual transport
(ClawBotClient.send_text vs. an in-memory fake in tests) is the
orchestrator's concern; tools only care that it is awaitable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent.api.schemas import PlanUpdate
from planagent.db.models import Plan, PlanStatus, Reminder, ReminderStatus

# wechat_send(text: str) -> awaitable. Returns None; side-effect only.
WechatSend = Callable[[str], Awaitable[None]]


@dataclass
class ToolContext:
    """Request-scoped context threaded into every tool handler."""

    session_factory: async_sessionmaker
    group_id: str  # internal DB id
    wechat_group_id: str
    wechat_send: WechatSend
    sent_texts: list[str] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: Callable[..., Awaitable[dict[str, Any]]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


# --- Serialization helpers ----------------------------------------------------


def _serialize_plan(p: Plan) -> dict[str, Any]:
    return {
        "id": p.id,
        "group_id": p.group_id,
        "title": p.title,
        "description": p.description,
        "status": p.status.value if isinstance(p.status, PlanStatus) else p.status,
        "start_at": p.start_at.isoformat() if p.start_at else None,
        "due_at": p.due_at.isoformat() if p.due_at else None,
        "expected_duration_per_session_min": p.expected_duration_per_session_min,
        "recurrence_cron": p.recurrence_cron,
        "priority": p.priority,
        "owner_user_id": p.owner_user_id,
        "metadata_json": dict(p.metadata_json or {}),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _serialize_reminder(r: Reminder) -> dict[str, Any]:
    return {
        "id": r.id,
        "plan_id": r.plan_id,
        "fire_at": r.fire_at.isoformat() if r.fire_at else None,
        "fired_at": r.fired_at.isoformat() if r.fired_at else None,
        "message": r.message,
        "status": r.status.value if isinstance(r.status, ReminderStatus) else r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _parse_iso_to_utc(value: str) -> datetime:
    """Accept an ISO-8601 string with or without tz; return tz-aware UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        # LLM omitted tz — interpret as Asia/Shanghai (+08:00) since that's
        # the conversational locale declared in the system prompt.
        from zoneinfo import ZoneInfo

        dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return dt.astimezone(UTC)


# --- Handlers ----------------------------------------------------------------


async def _list_plans(
    ctx: ToolContext, *, group_id: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    gid = group_id or ctx.group_id
    async with ctx.session_factory() as session:
        stmt = select(Plan).where(Plan.group_id == gid).order_by(Plan.created_at.desc())
        if status is not None:
            stmt = stmt.where(Plan.status == PlanStatus(status))
        res = await session.execute(stmt)
        return [_serialize_plan(p) for p in res.scalars().all()]


async def _get_plan(ctx: ToolContext, *, plan_id: str) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            return {"error": "plan_not_found", "plan_id": plan_id}
        return _serialize_plan(plan)


async def _create_plan_draft(
    ctx: ToolContext,
    *,
    title: str,
    group_id: str | None = None,
    owner_user_id: str | None = None,
) -> dict[str, Any]:
    gid = group_id or ctx.group_id
    async with ctx.session_factory() as session:
        plan = Plan(
            group_id=gid,
            title=title,
            status=PlanStatus.draft,
            owner_user_id=owner_user_id,
        )
        session.add(plan)
        await session.commit()
        await session.refresh(plan)
        return _serialize_plan(plan)


async def _update_plan(
    ctx: ToolContext, *, plan_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    # Validate the fields payload against PlanUpdate semantics.
    try:
        update = PlanUpdate.model_validate(fields)
    except Exception as exc:  # noqa: BLE001
        return {"error": "validation_error", "detail": str(exc)}
    data = update.model_dump(exclude_unset=True)
    async with ctx.session_factory() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            return {"error": "plan_not_found", "plan_id": plan_id}
        for k, v in data.items():
            if k == "metadata_json" and v is not None:
                merged = dict(plan.metadata_json or {})
                merged.update(v)
                setattr(plan, k, merged)
            else:
                setattr(plan, k, v)
        await session.commit()
        await session.refresh(plan)
        return _serialize_plan(plan)


async def _mark_plan_complete(ctx: ToolContext, *, plan_id: str) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            return {"error": "plan_not_found", "plan_id": plan_id}
        plan.status = PlanStatus.completed
        await session.commit()
        await session.refresh(plan)
        return _serialize_plan(plan)


async def _delete_plan(ctx: ToolContext, *, plan_id: str) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            return {"error": "plan_not_found", "plan_id": plan_id}
        await session.delete(plan)
        await session.commit()
        return {"ok": True, "plan_id": plan_id}


async def _schedule_reminder(
    ctx: ToolContext, *, plan_id: str, fire_at: str, message: str
) -> dict[str, Any]:
    fire_at_utc = _parse_iso_to_utc(fire_at)
    async with ctx.session_factory() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            return {"error": "plan_not_found", "plan_id": plan_id}
        rem = Reminder(plan_id=plan_id, fire_at=fire_at_utc, message=message)
        session.add(rem)
        await session.commit()
        await session.refresh(rem)
        return _serialize_reminder(rem)


async def _cancel_reminder(ctx: ToolContext, *, reminder_id: str) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        rem = await session.get(Reminder, reminder_id)
        if rem is None:
            return {"error": "reminder_not_found", "reminder_id": reminder_id}
        rem.status = ReminderStatus.cancelled
        await session.commit()
        return {"ok": True, "reminder_id": reminder_id}


async def _reply_in_group(ctx: ToolContext, *, text: str) -> dict[str, Any]:
    await ctx.wechat_send(text)
    ctx.sent_texts.append(text)
    return {"sent": True}


async def _ask_user_in_group(ctx: ToolContext, *, question: str) -> dict[str, Any]:
    await ctx.wechat_send(question)
    ctx.sent_texts.append(question)
    return {"sent": True}


async def _record_note(ctx: ToolContext, *, plan_id: str, note: str) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            return {"error": "plan_not_found", "plan_id": plan_id}
        meta = dict(plan.metadata_json or {})
        notes = list(meta.get("notes") or [])
        notes.append(note)
        meta["notes"] = notes
        plan.metadata_json = meta
        await session.commit()
        await session.refresh(plan)
        return _serialize_plan(plan)


# --- Shared enumerations / schema fragments ----------------------------------

_PLAN_STATUS_ENUM = [s.value for s in PlanStatus]

_PLAN_UPDATE_FIELDS_SCHEMA = {
    "type": "object",
    "description": (
        "Partial update of a Plan. Only include fields you want to change. "
        "Times must be ISO-8601 with timezone (prefer Asia/Shanghai +08:00)."
    ),
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "status": {"type": "string", "enum": _PLAN_STATUS_ENUM},
        "start_at": {"type": "string", "description": "ISO-8601 timestamp"},
        "due_at": {"type": "string", "description": "ISO-8601 timestamp"},
        "expected_duration_per_session_min": {"type": "integer", "minimum": 1},
        "recurrence_cron": {
            "type": "string",
            "description": "5-field crontab (m h dom mon dow). E.g. '0 20 * * 1-5'",
        },
        "priority": {"type": "integer"},
        "owner_user_id": {"type": "string"},
        "metadata_json": {
            "type": "object",
            "description": "Free-form metadata; merged (not replaced).",
            "additionalProperties": True,
        },
    },
    "additionalProperties": False,
}


# --- Registry ----------------------------------------------------------------

TOOL_REGISTRY: dict[str, Tool] = {
    "list_plans": Tool(
        name="list_plans",
        description="List plans for the current group, optionally filtered by status.",
        parameters_schema={
            "type": "object",
            "properties": {
                "group_id": {
                    "type": "string",
                    "description": "Internal group id. Defaults to the current group.",
                },
                "status": {"type": "string", "enum": _PLAN_STATUS_ENUM},
            },
        },
        handler=_list_plans,
    ),
    "get_plan": Tool(
        name="get_plan",
        description="Fetch one plan by id.",
        parameters_schema={
            "type": "object",
            "properties": {"plan_id": {"type": "string"}},
            "required": ["plan_id"],
        },
        handler=_get_plan,
    ),
    "create_plan_draft": Tool(
        name="create_plan_draft",
        description=(
            "Create a new plan in draft status. Use as soon as you've confirmed "
            "a plan title; fill remaining fields later via update_plan."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "group_id": {"type": "string"},
                "owner_user_id": {"type": "string"},
            },
            "required": ["title"],
        },
        handler=_create_plan_draft,
    ),
    "update_plan": Tool(
        name="update_plan",
        description="Patch fields on an existing plan.",
        parameters_schema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "fields": _PLAN_UPDATE_FIELDS_SCHEMA,
            },
            "required": ["plan_id", "fields"],
        },
        handler=_update_plan,
    ),
    "mark_plan_complete": Tool(
        name="mark_plan_complete",
        description="Mark a plan as completed.",
        parameters_schema={
            "type": "object",
            "properties": {"plan_id": {"type": "string"}},
            "required": ["plan_id"],
        },
        handler=_mark_plan_complete,
    ),
    "delete_plan": Tool(
        name="delete_plan",
        description="Delete a plan by id.",
        parameters_schema={
            "type": "object",
            "properties": {"plan_id": {"type": "string"}},
            "required": ["plan_id"],
        },
        handler=_delete_plan,
    ),
    "schedule_reminder": Tool(
        name="schedule_reminder",
        description=(
            "Schedule a single reminder for a plan. fire_at must be ISO-8601 "
            "with timezone (prefer Asia/Shanghai +08:00)."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "fire_at": {"type": "string", "description": "ISO-8601 timestamp"},
                "message": {"type": "string"},
            },
            "required": ["plan_id", "fire_at", "message"],
        },
        handler=_schedule_reminder,
    ),
    "cancel_reminder": Tool(
        name="cancel_reminder",
        description="Cancel a scheduled reminder.",
        parameters_schema={
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
        },
        handler=_cancel_reminder,
    ),
    "reply_in_group": Tool(
        name="reply_in_group",
        description=(
            "Send a natural-language reply into the current WeChat group. "
            "Use for confirmations, summaries, or any spoken turn."
        ),
        parameters_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=_reply_in_group,
    ),
    "ask_user_in_group": Tool(
        name="ask_user_in_group",
        description=(
            "Post a focused follow-up question into the group. Use when a "
            "required plan field (title, due/recurrence, duration, owner) is "
            "missing; do NOT guess."
        ),
        parameters_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
        handler=_ask_user_in_group,
    ),
    "record_note": Tool(
        name="record_note",
        description="Append a free-form note to a plan's metadata_json.notes list.",
        parameters_schema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["plan_id", "note"],
        },
        handler=_record_note,
    ),
}


def tool_schemas() -> list[dict[str, Any]]:
    """Return the list of tool schemas in OpenAI function-calling format."""
    return [t.schema() for t in TOOL_REGISTRY.values()]
