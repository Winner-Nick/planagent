"""DeepSeek tool-calling loop.

Flow (see PR-D spec):

1. Upsert GroupContext + GroupMember from inbound msg.
2. Append a ConversationTurn for the user's message.
3. Assemble messages: cached system prompt + prior turns + (already-persisted)
   current user turn.
4. Chat -> tool_calls? run each, persist tool turns, feed back, loop.
5. On plain content: persist assistant turn. If the LLM never called
   reply/ask, treat the content as an implicit reply_in_group.
6. Guardrail: MAX_ROUNDS tool-call rounds; on overflow, post a fallback.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent.agent.prompts import GroupSnapshot, make_prompt
from planagent.agent.tools import (
    TOOL_REGISTRY,
    ToolContext,
    WechatSend,
    tool_schemas,
)
from planagent.db.models import (
    ConversationRole,
    ConversationTurn,
    GroupContext,
    GroupMember,
    Plan,
    PlanStatus,
    Reminder,
    ReminderStatus,
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.wechat import protocol as wxp
from planagent.wechat.protocol import InboundMessage

log = logging.getLogger(__name__)

MAX_ROUNDS = 6
HISTORY_TURNS = 20
SHANGHAI = ZoneInfo("Asia/Shanghai")

SPOKEN_TOOL_NAMES = {"reply_in_group", "ask_user_in_group"}

FALLBACK_TEXT = "我需要人看一下：对话超过了我能处理的轮数。"


async def _upsert_group_and_member(
    session_factory: async_sessionmaker,
    *,
    wechat_group_id: str,
    wechat_user_id: str | None,
) -> tuple[str, str]:
    """Ensure GroupContext + GroupMember exist. Return (group_id, wechat_group_id)."""
    async with session_factory() as session:
        stmt = select(GroupContext).where(
            GroupContext.wechat_group_id == wechat_group_id
        )
        res = await session.execute(stmt)
        group = res.scalar_one_or_none()
        if group is None:
            group = GroupContext(wechat_group_id=wechat_group_id)
            session.add(group)
            await session.flush()
        group.last_seen_at = datetime.now(SHANGHAI)

        if wechat_user_id:
            mstmt = select(GroupMember).where(
                GroupMember.group_id == group.id,
                GroupMember.wechat_user_id == wechat_user_id,
            )
            mres = await session.execute(mstmt)
            member = mres.scalar_one_or_none()
            if member is None:
                session.add(
                    GroupMember(
                        group_id=group.id,
                        wechat_user_id=wechat_user_id,
                    )
                )
        await session.commit()
        return group.id, group.wechat_group_id


async def _load_snapshot(
    session_factory: async_sessionmaker, *, group_id: str
) -> GroupSnapshot:
    async with session_factory() as session:
        group = await session.get(GroupContext, group_id)
        assert group is not None

        mres = await session.execute(
            select(GroupMember).where(GroupMember.group_id == group_id)
        )
        members = [
            {
                "wechat_user_id": m.wechat_user_id,
                "display_name": m.display_name,
            }
            for m in mres.scalars().all()
        ]

        pres = await session.execute(
            select(Plan)
            .where(
                Plan.group_id == group_id,
                Plan.status.in_([PlanStatus.draft, PlanStatus.active, PlanStatus.paused]),
            )
            .order_by(Plan.created_at.desc())
        )
        plans_out = []
        for p in pres.scalars().all():
            rres = await session.execute(
                select(Reminder)
                .where(
                    Reminder.plan_id == p.id,
                    Reminder.status == ReminderStatus.pending,
                )
                .order_by(Reminder.fire_at.asc())
                .limit(1)
            )
            r = rres.scalar_one_or_none()
            # SQLite's DateTime(timezone=True) can hand back naive datetimes
            # depending on driver + version; normalize to UTC before
            # converting to the conversational tz so the LLM sees correct
            # times regardless of host TZ.
            next_fire_local: str | None = None
            if r is not None and r.fire_at is not None:
                fa = r.fire_at if r.fire_at.tzinfo is not None else r.fire_at.replace(tzinfo=UTC)
                next_fire_local = fa.astimezone(SHANGHAI).isoformat()
            plans_out.append(
                {
                    "id": p.id,
                    "title": p.title,
                    "status": p.status.value,
                    "next_fire_at": next_fire_local,
                }
            )

        return GroupSnapshot(
            group_id=group.id,
            wechat_group_id=group.wechat_group_id,
            group_name=group.name,
            members=members,
            plans=plans_out,
        )


async def _load_history(
    session_factory: async_sessionmaker, *, group_id: str, limit: int
) -> list[dict[str, Any]]:
    async with session_factory() as session:
        res = await session.execute(
            select(ConversationTurn)
            .where(ConversationTurn.group_id == group_id)
            .order_by(ConversationTurn.created_at.desc())
            .limit(limit)
        )
        turns = list(reversed(list(res.scalars().all())))
    messages: list[dict[str, Any]] = []
    for t in turns:
        if t.role == ConversationRole.user:
            messages.append({"role": "user", "content": t.content or ""})
        elif t.role == ConversationRole.assistant:
            entry: dict[str, Any] = {"role": "assistant"}
            if t.content:
                entry["content"] = t.content
            if t.tool_calls_json:
                tc = t.tool_calls_json.get("tool_calls") if isinstance(
                    t.tool_calls_json, dict
                ) else None
                if tc:
                    entry["tool_calls"] = tc
            if "content" not in entry and "tool_calls" not in entry:
                entry["content"] = ""
            messages.append(entry)
        elif t.role == ConversationRole.tool:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": t.tool_call_id or "",
                    "content": t.content or "",
                }
            )
    return messages


async def _append_turn(
    session_factory: async_sessionmaker,
    *,
    group_id: str,
    role: ConversationRole,
    content: str | None = None,
    user_id: str | None = None,
    tool_calls_json: dict[str, Any] | None = None,
    tool_call_id: str | None = None,
    context_token: str | None = None,
) -> None:
    async with session_factory() as session:
        session.add(
            ConversationTurn(
                group_id=group_id,
                role=role,
                user_id=user_id,
                content=content,
                tool_calls_json=tool_calls_json,
                tool_call_id=tool_call_id,
                context_token=context_token,
            )
        )
        await session.commit()


def _tool_calls_to_json(tool_calls: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        out.append(
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
        )
    return out


async def _run_tool_call(
    tc: Any, *, ctx: ToolContext
) -> dict[str, Any]:
    name = tc.function.name
    raw_args = tc.function.arguments or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    except json.JSONDecodeError as exc:
        return {"error": "invalid_json_arguments", "detail": str(exc)}
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return {"error": "unknown_tool", "name": name}
    try:
        return await tool.handler(ctx, **args)
    except TypeError as exc:
        return {"error": "bad_arguments", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 — handler errors must reach the LLM
        log.exception("tool %s failed", name)
        return {"error": "tool_exception", "detail": str(exc)}


async def handle_inbound(
    msg: InboundMessage,
    *,
    deepseek: DeepSeekClient,
    session_factory: async_sessionmaker,
    wechat_send: WechatSend,
    max_rounds: int = MAX_ROUNDS,
    history_turns: int = HISTORY_TURNS,
) -> None:
    wechat_group_id = wxp.group_id(msg)
    if wechat_group_id is None:
        # PR-D is group-only. Ignore 1:1 messages silently.
        return

    text = wxp.text_content(msg) or ""
    user_id = wxp.sender_id(msg)

    group_internal_id, _ = await _upsert_group_and_member(
        session_factory,
        wechat_group_id=wechat_group_id,
        wechat_user_id=user_id,
    )

    await _append_turn(
        session_factory,
        group_id=group_internal_id,
        role=ConversationRole.user,
        content=text,
        user_id=user_id,
        context_token=msg.context_token,
    )

    snapshot = await _load_snapshot(session_factory, group_id=group_internal_id)
    now = datetime.now(SHANGHAI)
    system_prompt = make_prompt(snapshot, now=now)

    history = await _load_history(
        session_factory, group_id=group_internal_id, limit=history_turns
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}, *history]

    ctx = ToolContext(
        session_factory=session_factory,
        group_id=group_internal_id,
        wechat_group_id=wechat_group_id,
        wechat_send=wechat_send,
    )

    schemas = tool_schemas()
    called_spoken_tool = False

    for _round_idx in range(max_rounds):
        resp = deepseek.chat(messages=messages, tools=schemas, tool_choice="auto")
        choice_msg = resp.choices[0].message
        tool_calls = getattr(choice_msg, "tool_calls", None) or []

        if tool_calls:
            assistant_entry: dict[str, Any] = {
                "role": "assistant",
                "content": choice_msg.content or None,
                "tool_calls": _tool_calls_to_json(tool_calls),
            }
            messages.append(assistant_entry)

            await _append_turn(
                session_factory,
                group_id=group_internal_id,
                role=ConversationRole.assistant,
                content=choice_msg.content,
                tool_calls_json={"tool_calls": assistant_entry["tool_calls"]},
            )

            for tc in tool_calls:
                if tc.function.name in SPOKEN_TOOL_NAMES:
                    called_spoken_tool = True
                result = await _run_tool_call(tc, ctx=ctx)
                payload = json.dumps(result, ensure_ascii=False, default=str)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": payload,
                    }
                )
                await _append_turn(
                    session_factory,
                    group_id=group_internal_id,
                    role=ConversationRole.tool,
                    content=payload,
                    tool_call_id=tc.id,
                )
            continue

        # No tool calls: this is the terminal assistant message.
        content = choice_msg.content or ""
        await _append_turn(
            session_factory,
            group_id=group_internal_id,
            role=ConversationRole.assistant,
            content=content,
        )
        if content.strip() and not called_spoken_tool:
            # Implicit reply into the group.
            await wechat_send(content)
            ctx.sent_texts.append(content)
        return

    # Ran out of rounds.
    log.warning("agent hit MAX_ROUNDS=%d without terminating", max_rounds)
    await wechat_send(FALLBACK_TEXT)
    await _append_turn(
        session_factory,
        group_id=group_internal_id,
        role=ConversationRole.assistant,
        content=FALLBACK_TEXT,
    )


__all__ = ["FALLBACK_TEXT", "MAX_ROUNDS", "handle_inbound"]
