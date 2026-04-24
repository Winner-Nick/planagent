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
from planagent.db.models import (
    CrossUserNote,
    CrossUserNoteKind,
    PendingOutbound,
    PendingOutboundStatus,
    Plan,
    PlanStatus,
    Reminder,
    ReminderStatus,
)
from planagent.lib.friendly_time import friendly
from planagent.wechat.constants import (
    CHENCHEN,
    PENG,
    display_name_for,
)

# wechat_send(text: str) -> awaitable. Returns None; side-effect only.
WechatSend = Callable[[str], Awaitable[None]]


@dataclass
class ToolContext:
    """Request-scoped context threaded into every tool handler.

    PR-G adds `sender_user_id` / `peer_user_id` so `create_plan_draft` can
    default the plan owner to the current speaker without ever exposing raw
    user_ids to the LLM. The agent passes `owner="speaker"` (default) or
    `owner="peer"` by natural-language intent; the handler resolves the
    string to a wechat_user_id locally.
    """

    session_factory: async_sessionmaker
    group_id: str  # internal DB id
    wechat_group_id: str
    wechat_send: WechatSend
    sender_user_id: str | None = None
    peer_user_id: str | None = None
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


def _friendly_or_none(dt: datetime | None) -> str | None:
    """Render ``dt`` via `friendly()` if set, else None.

    `friendly()` uses the current wall clock in Shanghai as `now` by default;
    that's good enough for tool results — the LLM will regenerate user-facing
    text in the same turn, so a <1s drift vs. turn-start `now` is invisible.
    """
    if dt is None:
        return None
    return friendly(dt)


def _pick_plan_next_dt(p: Plan) -> datetime | None:
    """Best-effort "what time should the user think of for this plan" pick.

    start_at wins (it's the concrete fire point), else due_at. Recurrence
    cron is intentionally NOT expanded here — it belongs to the scheduler
    and varies per-owner; flattening it to a single friendly string would
    lie to the LLM about what the plan actually looks like.
    """
    return p.start_at or p.due_at


def _serialize_plan(p: Plan) -> dict[str, Any]:
    next_dt = _pick_plan_next_dt(p)
    return {
        "id": p.id,
        "group_id": p.group_id,
        "title": p.title,
        "description": p.description,
        "status": p.status.value if isinstance(p.status, PlanStatus) else p.status,
        "start_at": p.start_at.isoformat() if p.start_at else None,
        "due_at": p.due_at.isoformat() if p.due_at else None,
        # PR-M: human-readable render of start_at/due_at. LLM MUST echo this
        # form to the user instead of the raw ISO fields above.
        "next_fire_at_friendly": _friendly_or_none(next_dt),
        "expected_duration_per_session_min": p.expected_duration_per_session_min,
        "recurrence_cron": p.recurrence_cron,
        "priority": p.priority,
        "owner_user_id": p.owner_user_id,
        "metadata_json": dict(p.metadata_json or {}),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


# PR-M: a small rotating pool of one-liner cheers. The handler picks one
# round-robin (see _cheer_counter) and returns it as METADATA — the LLM
# decides whether/how to surface it in the user-visible reply.
_CHEERS: tuple[str, ...] = (
    "抓住啦 ✓",
    "搞定 ✓",
    "拿下 ✓",
    "这件收工 ✓",
    "干净利落 ✓",
)
_cheer_counter: dict[str, int] = {"i": 0}


def _next_cheer() -> str:
    i = _cheer_counter["i"] % len(_CHEERS)
    _cheer_counter["i"] += 1
    return _CHEERS[i]


def _owner_short(owner_user_id: str | None) -> str | None:
    """Map a raw wechat_user_id to 'peng' / 'chenchen', else None.

    list_plans uses this to hand the LLM a stable short owner key without
    ever exposing the raw id.
    """
    if owner_user_id == PENG.wechat_user_id:
        return "peng"
    if owner_user_id == CHENCHEN.wechat_user_id:
        return "chenchen"
    return None


def _serialize_plan_compact(p: Plan) -> dict[str, Any]:
    """Skinny Plan dict for list views.

    - No raw ids exposed as free text (we keep `id` but the description on
      the tool steers the LLM to NOT echo it).
    - No ISO timestamps in the visible summary — only the friendly render.
    - `owner` is a short string ("peng"/"chenchen"/None) rather than the
      raw wechat_user_id.
    """
    next_dt = _pick_plan_next_dt(p)
    return {
        "id": p.id,
        "title": p.title,
        "status": p.status.value if isinstance(p.status, PlanStatus) else p.status,
        "next": _friendly_or_none(next_dt),
        "owner": _owner_short(p.owner_user_id),
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


def _normalize_cron(raw: str) -> tuple[str, str | None]:
    """Validate + normalize a cron string to 5-field form.

    Accepts:
      - 5-field: "m h dom mon dow" (passes through unchanged).
      - 6-field: "s m h dom mon dow" where the leading field is a plain
        numeric seconds value (0-59). The seconds field is dropped and a
        warning is surfaced to the caller. APScheduler + our Reminder
        timing use minute precision, so carrying seconds is both misleading
        (LLM sometimes writes `0 50 9 * * *` expecting "09:50:00" but the
        5-field parser would read it as "min=0 hour=50" — invalid) and
        unnecessary.

    Raises ValueError on any other shape.
    """
    if not isinstance(raw, str):
        raise ValueError(f"invalid cron: expected string, got {type(raw).__name__}")
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("invalid cron: empty string")
    fields = cleaned.split()
    if len(fields) == 5:
        return cleaned, None
    if len(fields) == 6:
        seconds = fields[0]
        # Only strip when the leading field is a clean integer in [0, 59].
        # Anything else (ranges, `*`, step) we reject — it likely means the
        # caller is in a non-standard 6-field dialect we don't want to guess.
        try:
            sec_int = int(seconds)
        except ValueError as exc:
            raise ValueError(
                f"invalid cron: 6-field form requires numeric seconds, got {seconds!r}"
            ) from exc
        if not 0 <= sec_int <= 59:
            raise ValueError(
                f"invalid cron: seconds field out of range 0-59, got {sec_int}"
            )
        normalized = " ".join(fields[1:])
        return normalized, "cron normalized: dropped leading seconds field"
    raise ValueError(
        f"invalid cron: expected 5 fields (m h dom mon dow), got {len(fields)}"
    )


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


async def _fetch_plan_in_group(session, ctx: ToolContext, plan_id: str) -> Plan | None:
    """Load a Plan by id only if it belongs to the current group.

    Cross-group reads/writes are treated as 'not found' — in a shared
    process serving multiple WeChat groups, foreign IDs (from
    hallucination or prompt injection) must never reach another group's
    data.
    """
    plan = await session.get(Plan, plan_id)
    if plan is None or plan.group_id != ctx.group_id:
        return None
    return plan


async def _list_plans(
    ctx: ToolContext, *, status: str | None = None
) -> list[dict[str, Any]]:
    # PR-M: compact shape only. The old full-Plan payload was causing the
    # LLM to recite raw UUIDs and ISO stamps back at the user. If the agent
    # needs the full fields it MUST call `get_plan(plan_id=...)` explicitly.
    async with ctx.session_factory() as session:
        stmt = (
            select(Plan)
            .where(Plan.group_id == ctx.group_id)
            .order_by(Plan.created_at.desc())
        )
        if status is not None:
            stmt = stmt.where(Plan.status == PlanStatus(status))
        res = await session.execute(stmt)
        return [_serialize_plan_compact(p) for p in res.scalars().all()]


async def _get_plan(ctx: ToolContext, *, plan_id: str) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        plan = await _fetch_plan_in_group(session, ctx, plan_id)
        if plan is None:
            return {"error": "plan_not_found", "plan_id": plan_id}
        return _serialize_plan(plan)


async def _create_plan_draft(
    ctx: ToolContext,
    *,
    title: str,
    owner: str = "speaker",
    # Back-compat for tests / callers still passing a raw wechat_user_id.
    # The LLM never sees this — schema exposes only `owner`.
    owner_user_id: str | None = None,
) -> dict[str, Any]:
    """Create a draft plan. Owner resolution:

    - owner="speaker" (default): ctx.sender_user_id — the human who just wrote in.
    - owner="peer": ctx.peer_user_id — the other known human in the logical group.
    - owner_user_id kwarg (back-compat): passed through verbatim.
    """
    if owner_user_id is not None:
        resolved_owner = owner_user_id
    elif owner == "peer":
        resolved_owner = ctx.peer_user_id
    else:
        # Default + "speaker" both resolve to the inbound sender.
        resolved_owner = ctx.sender_user_id
    async with ctx.session_factory() as session:
        plan = Plan(
            group_id=ctx.group_id,
            title=title,
            status=PlanStatus.draft,
            owner_user_id=resolved_owner,
        )
        session.add(plan)
        await session.commit()
        await session.refresh(plan)
        return _serialize_plan(plan)


async def _update_plan(
    ctx: ToolContext, *, plan_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    # PR-I: normalize cron BEFORE pydantic validation so a 6-field form
    # gets corrected to 5-field (with a warning surfaced to the agent),
    # and a malformed cron turns into a clean validation_error instead of
    # silently persisting bad state.
    cron_warning: str | None = None
    if isinstance(fields, dict) and "recurrence_cron" in fields:
        raw_cron = fields["recurrence_cron"]
        if raw_cron is not None:
            try:
                normalized, cron_warning = _normalize_cron(raw_cron)
            except ValueError as exc:
                return {"error": "validation_error", "detail": str(exc)}
            fields = {**fields, "recurrence_cron": normalized}
    # Validate the fields payload against PlanUpdate semantics.
    try:
        update = PlanUpdate.model_validate(fields)
    except Exception as exc:  # noqa: BLE001
        return {"error": "validation_error", "detail": str(exc)}
    data = update.model_dump(exclude_unset=True)
    async with ctx.session_factory() as session:
        plan = await _fetch_plan_in_group(session, ctx, plan_id)
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
        result = _serialize_plan(plan)
        if cron_warning:
            result = {**result, "warning": cron_warning}
        return result


async def _mark_plan_complete(ctx: ToolContext, *, plan_id: str) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        plan = await _fetch_plan_in_group(session, ctx, plan_id)
        if plan is None:
            return {"error": "plan_not_found", "plan_id": plan_id}
        plan.status = PlanStatus.completed
        await session.commit()
        await session.refresh(plan)
        # PR-M: ship a "cheer" hint alongside the raw ok. It's metadata —
        # the LLM may echo it, rephrase it, or drop it entirely. The point
        # is that confirming a completion should feel warm, not curt.
        return {
            "ok": True,
            "plan_id": plan.id,
            "title": plan.title,
            "status": plan.status.value,
            "cheer": _next_cheer(),
        }


async def _cancel_plan(ctx: ToolContext, *, plan_id: str) -> dict[str, Any]:
    """Mark a plan as `cancelled`. Row is preserved for audit.

    PR-I: prefer this over `delete_plan` when the user explicitly says
    "算了 / 取消吧 / 不做了". Deleting loses history; cancelling keeps it
    so the peer whiteboard / later "how many cancelled plans" queries are
    answerable. Status transition is purely local (no scheduler / reminder
    side-effects) — any pending reminders remain on disk but the overdue
    sweep ignores non-active plans so they won't auto-flip back.
    """
    async with ctx.session_factory() as session:
        plan = await _fetch_plan_in_group(session, ctx, plan_id)
        if plan is None:
            return {"error": "plan_not_found", "plan_id": plan_id}
        plan.status = PlanStatus.cancelled
        await session.commit()
        await session.refresh(plan)
        return _serialize_plan(plan)


async def _delete_plan(ctx: ToolContext, *, plan_id: str) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        plan = await _fetch_plan_in_group(session, ctx, plan_id)
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
        plan = await _fetch_plan_in_group(session, ctx, plan_id)
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
        # A reminder belongs to a group via its plan. Enforce ownership.
        plan = await session.get(Plan, rem.plan_id)
        if plan is None or plan.group_id != ctx.group_id:
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


# --- Cross-user whiteboard helpers (PR-H) ------------------------------------

# Short peer keys the LLM uses in natural conversation. Mapped to the known
# roster at call time. Keeping this local to tools.py keeps `constants` free
# of tool-facing aliases.
_PEER_BY_KEY: dict[str, str] = {
    PENG.cred_name: PENG.wechat_user_id,
    CHENCHEN.cred_name: CHENCHEN.wechat_user_id,
}

_CROSS_NOTE_TEXT_MAX = 200


def _resolve_peer_key(peer: str) -> str | None:
    """Map a peer alias ("peng"/"chenchen") to a wechat_user_id. Case-insensitive."""
    if not peer:
        return None
    return _PEER_BY_KEY.get(peer.strip().lower())


async def _note_for_peer(
    ctx: ToolContext,
    *,
    audience: str,
    kind: str = "info",
    text: str,
) -> dict[str, Any]:
    """Stash a note addressed to the other human in this logical group.

    The note is stored, NOT sent. The audience sees it on their next inbound
    turn as part of the volatile whiteboard section of the prompt.
    """
    audience_uid = _resolve_peer_key(audience)
    if not audience_uid:
        return {"error": "unknown_audience", "detail": audience}
    if not ctx.sender_user_id:
        return {"error": "no_sender_context"}
    if audience_uid == ctx.sender_user_id:
        return {"error": "audience_is_speaker"}
    try:
        kind_enum = CrossUserNoteKind(kind)
    except ValueError:
        return {
            "error": "invalid_kind",
            "detail": kind,
            "allowed": [k.value for k in CrossUserNoteKind],
        }
    stripped = (text or "").strip()
    if not stripped:
        return {"error": "empty_text"}
    if len(stripped) > _CROSS_NOTE_TEXT_MAX:
        return {
            "error": "text_too_long",
            "limit": _CROSS_NOTE_TEXT_MAX,
            "length": len(stripped),
        }
    async with ctx.session_factory() as session:
        note = CrossUserNote(
            group_id=ctx.group_id,
            author_user_id=ctx.sender_user_id,
            audience_user_id=audience_uid,
            kind=kind_enum,
            text=stripped,
        )
        session.add(note)
        await session.commit()
        await session.refresh(note)
        return {"ok": True, "note_id": note.id}


async def _peek_peer_state(ctx: ToolContext, *, peer: str) -> dict[str, Any]:
    """Introspect the other human's recent activity without polluting history.

    Returned fields are a momentary snapshot — never persisted into the
    conversation log. The LLM uses this to decide whether to nudge, wait,
    or ask the speaker something before touching peer-facing plans.
    """
    peer_uid = _resolve_peer_key(peer)
    if not peer_uid:
        return {"error": "unknown_peer", "detail": peer}
    if not ctx.sender_user_id:
        return {"error": "no_sender_context"}
    # Local import to avoid a cycle: BotSession lives in db.models but we
    # reference it only here.
    from zoneinfo import ZoneInfo

    from planagent.db.models import BotSession

    now_utc = datetime.now(UTC)
    # 00:00 Asia/Shanghai → UTC cutoff, for today-completion count.
    shanghai = ZoneInfo("Asia/Shanghai")
    now_local = now_utc.astimezone(shanghai)
    start_of_today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_today_utc = start_of_today_local.astimezone(UTC)
    async with ctx.session_factory() as session:
        bs_res = await session.execute(
            select(BotSession).where(BotSession.wechat_user_id == peer_uid)
        )
        bs = bs_res.scalar_one_or_none()
        last_inbound_at_iso: str | None = None
        if bs is not None and bs.last_inbound_at is not None:
            last_inbound_at_iso = bs.last_inbound_at.isoformat()

        # PR-I split: open = draft + active (things that still need the
        # agent's care). Paused is user-parked, not "open load"; overdue
        # is its own bucket; cancelled is audit-only and excluded here.
        open_statuses = [PlanStatus.draft, PlanStatus.active]
        open_rows = (
            await session.execute(
                select(Plan).where(
                    Plan.group_id == ctx.group_id,
                    Plan.owner_user_id == peer_uid,
                    Plan.status.in_(open_statuses),
                )
            )
        ).scalars().all()

        overdue_rows = (
            await session.execute(
                select(Plan).where(
                    Plan.group_id == ctx.group_id,
                    Plan.owner_user_id == peer_uid,
                    Plan.status == PlanStatus.overdue,
                )
            )
        ).scalars().all()

        completed_today_rows = (
            await session.execute(
                select(Plan).where(
                    Plan.group_id == ctx.group_id,
                    Plan.owner_user_id == peer_uid,
                    Plan.status == PlanStatus.completed,
                    Plan.updated_at >= start_of_today_utc,
                )
            )
        ).scalars().all()

        # Notes the speaker has sent the peer recently (last 10).
        notes_res = await session.execute(
            select(CrossUserNote)
            .where(
                CrossUserNote.group_id == ctx.group_id,
                CrossUserNote.author_user_id == ctx.sender_user_id,
                CrossUserNote.audience_user_id == peer_uid,
            )
            .order_by(CrossUserNote.created_at.desc())
            .limit(10)
        )
        recent_notes = [
            {
                "kind": n.kind.value if isinstance(n.kind, CrossUserNoteKind) else n.kind,
                "text": n.text,
                "consumed": n.consumed_at is not None,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in notes_res.scalars().all()
        ]

    return {
        "wechat_user_id": peer_uid,
        "display_name": display_name_for(peer_uid),
        "last_inbound_at_iso": last_inbound_at_iso,
        # Back-compat: `open_plans_count` retained with its historical meaning
        # (draft + active). PR-I adds explicit overdue / completed_today
        # buckets so the LLM can see the split.
        "open_plans_count": len(open_rows),
        "overdue_count": len(overdue_rows),
        "completed_today_count": len(completed_today_rows),
        "recent_notes_I_sent_them": recent_notes,
    }


async def _send_to_peer_async(
    ctx: ToolContext, *, peer: str, text: str
) -> dict[str, Any]:
    """Queue a message for the other human; delivered on their next inbound.

    We never try to push it right away: ClawBot is gated by a 24h inbound
    window and we cannot know whether the peer's window is open. The
    orchestrator flushes `pending_outbound` rows at the start of every
    inbound handler invocation.
    """
    peer_uid = _resolve_peer_key(peer)
    if not peer_uid:
        return {"error": "unknown_peer", "detail": peer}
    if not ctx.sender_user_id:
        return {"error": "no_sender_context"}
    if peer_uid == ctx.sender_user_id:
        return {"error": "peer_is_speaker"}
    stripped = (text or "").strip()
    if not stripped:
        return {"error": "empty_text"}
    async with ctx.session_factory() as session:
        row = PendingOutbound(
            group_id=ctx.group_id,
            target_user_id=peer_uid,
            author_user_id=ctx.sender_user_id,
            text=stripped,
            status=PendingOutboundStatus.pending,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return {
            "queued": True,
            "pending_id": row.id,
            "note": "will be delivered on peer's next inbound.",
        }


async def _record_note(ctx: ToolContext, *, plan_id: str, note: str) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        plan = await _fetch_plan_in_group(session, ctx, plan_id)
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
    # PR-G: `owner_user_id` removed — the LLM must never see or set raw
    # wechat_user_ids. Ownership is fixed at draft-time via create_plan_draft's
    # `owner` parameter ("speaker" / "peer"). Re-assigning ownership is
    # deferred to PR-H; there is no LLM path to change it in this PR.
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "status": {"type": "string", "enum": _PLAN_STATUS_ENUM},
        "start_at": {"type": "string", "description": "ISO-8601 timestamp"},
        "due_at": {"type": "string", "description": "ISO-8601 timestamp"},
        "expected_duration_per_session_min": {"type": "integer", "minimum": 1},
        "recurrence_cron": {
            "type": "string",
            "description": (
                "5 字段 cron: 分 时 日 月 周（m h dom mon dow）。"
                "例：'0 20 * * 1-5' = 工作日每晚 20:00。"
                "**不要写 6 字段（带秒）形式**，那种会被拒掉。"
            ),
        },
        "priority": {"type": "integer"},
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
        description=(
            "列出当前 group 的计划（概览版，不是完整字段）。"
            "返回数组，每项只含 {id, title, status, next, owner}，"
            "其中 next 已经是口语化时间（如「明天 09:50」），owner 是"
            "短名（peng / chenchen / null）。**不要把 id 念给用户听**，"
            "id 只用于下一步调工具。需要完整 Plan 字段时才调 get_plan。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
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
            "创建一个草稿状态的计划。只要标题明确就立刻调用；剩下的字段用 "
            "update_plan 慢慢补。**不用询问用户的 id**——当前说话人就是默认 "
            "owner。除非用户明确把计划指向对方（比如「辰辰她自己的英语打卡」），"
            "否则直接用默认值（owner='speaker'）。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "owner": {
                    "type": "string",
                    "enum": ["speaker", "peer"],
                    "description": (
                        "谁负责这个计划。speaker = 当前说话人（默认）；"
                        "peer = 群里的另一个人。"
                    ),
                    "default": "speaker",
                },
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
        description=(
            "把一个计划标记为已完成。返回 {ok, plan_id, title, status, cheer}；"
            "`cheer` 是一句你可以顺手带进回复里的小庆祝词（类似「搞定 ✓」），"
            "不是强制的，不合适就忽略。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {"plan_id": {"type": "string"}},
            "required": ["plan_id"],
        },
        handler=_mark_plan_complete,
    ),
    "cancel_plan": Tool(
        name="cancel_plan",
        description=(
            "把计划标记为 cancelled（用户说「算了 / 取消吧 / 不做了」时用）。"
            "会保留这一行用于审计 —— 优先于 delete_plan。delete_plan 是永久删除，"
            "失去历史；cancel_plan 只改状态。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {"plan_id": {"type": "string"}},
            "required": ["plan_id"],
        },
        handler=_cancel_plan,
    ),
    "delete_plan": Tool(
        name="delete_plan",
        description=(
            "永久删除一个计划（不可恢复）。**通常你想要的是 cancel_plan**，"
            "除非用户明确说「删掉 / 别再出现」或需要清理误创建的草稿。"
        ),
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
    "note_for_peer": Tool(
        name="note_for_peer",
        description=(
            "把一条备忘贴到共享白板上，留给群里另一个人下次上线时看。"
            "**不会立刻发给对方**，只会进对方下一次的 prompt 上下文。"
            "适用场景：当前说话人顺口提到对方最近的情况（info）、"
            "希望对方下次被轻轻推一下（nudge_request）、"
            "或者想留一句夸赞 / 感谢（appreciate）。"
            "text 限制 200 字以内；audience 是对方的短名（peng / chenchen）。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "audience": {
                    "type": "string",
                    "enum": ["peng", "chenchen"],
                    "description": "留给谁看。必须是对方，不是自己。",
                },
                "kind": {
                    "type": "string",
                    "enum": ["info", "nudge_request", "appreciate"],
                    "default": "info",
                    "description": (
                        "备忘类型：info=情报；nudge_request=请提醒对方；appreciate=夸赞。"
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "备忘正文，≤200 字。用对方能读懂的话写。",
                },
            },
            "required": ["audience", "text"],
        },
        handler=_note_for_peer,
    ),
    "peek_peer_state": Tool(
        name="peek_peer_state",
        description=(
            "快速看一眼对方（peer）的当前状态：最后一次活跃时间、活跃计划数、"
            "逾期计划数、以及当前说话人最近给对方留过的备忘。"
            "用来决定是不是该催一下、是不是别打扰。"
            "结果只在这一轮内可见，不会写进对话历史。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "enum": ["peng", "chenchen"],
                    "description": "想查的是哪一个人。",
                },
            },
            "required": ["peer"],
        },
        handler=_peek_peer_state,
    ),
    "send_to_peer_async": Tool(
        name="send_to_peer_async",
        description=(
            "给对方发一条消息，但不是实时送达——它进入 pending 队列，"
            "等对方下次开口时由后台自动送出（ClawBot 有 24 小时窗口限制）。"
            "适合：替当前说话人转话、跨用户提醒、当前说话人想对对方说一句但对方"
            "还没活跃。返回 pending_id，状态由 orchestrator 在对方 inbound 时翻转。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "enum": ["peng", "chenchen"],
                    "description": "发给谁（对方的短名）。",
                },
                "text": {
                    "type": "string",
                    "description": "要发给对方的完整文本，用你平时说话的口气。",
                },
            },
            "required": ["peer", "text"],
        },
        handler=_send_to_peer_async,
    ),
}


def tool_schemas() -> list[dict[str, Any]]:
    """Return the list of tool schemas in OpenAI function-calling format."""
    return [t.schema() for t in TOOL_REGISTRY.values()]
