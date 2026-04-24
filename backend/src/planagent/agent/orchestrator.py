"""DeepSeek tool-calling loop.

Flow (PR-G):

1. Upsert GroupContext + GroupMember from inbound msg.
2. Append a ConversationTurn for the user's message (speaker=target=X).
3. Load history FILTERED by this speaker: user turns where speaker==X plus
   assistant/tool turns where target==X. This is the fix for bug #5
   (cross-contamination): without the filter, a reply 小计 was composing for
   鹏鹏 would leak into 辰辰's next turn and vice versa.
4. Assemble messages: persona system prompt + filtered history.
5. Chat -> tool_calls? run each, persist tool turns (target=X), feed back.
6. Merge multiple reply_in_group / ask_user_in_group invocations across the
   loop into ONE final outbound send (joined with a natural separator).
   This enforces "one turn, one send" and fixes the symptom where 小计
   produces both a greeting + follow-up as two fragmented messages.
7. On plain content: persist assistant turn. If the LLM never called a spoken
   tool, treat the content as the reply.
8. **Guardrails fixing bug #2**: assistant messages that carry `tool_calls`
   have their `content` DROPPED both when persisted AND when fed back into
   the LLM loop. DeepSeek R1-style models love to jam chain-of-thought into
   `content` right before a tool call; dropping it hides that from both the
   user and subsequent rounds (so CoT from round N doesn't poison round N+1).

### Temperature dispatch

- First chat round of a handler: `DIALOGUE_TEMP = 0.7`. This is the "is the
  user chit-chatting or about to record a plan?" decision; a little warmth
  keeps small talk from sounding robotic.
- Any subsequent round (we got tool_calls on the previous round): `ACTION_TEMP
  = 0.0`. Once we're in tool-call land, determinism matters — field values
  and times must be exact.

### Invariant

history loaded for speaker X NEVER contains turns whose target was someone
else. The `_load_history` docstring repeats this for future readers.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent.agent.prompts import (
    ACTION_TEMP,
    DIALOGUE_TEMP,
    GroupSnapshot,
    Whiteboard,
    make_prompt,
)
from planagent.agent.tools import (
    TOOL_REGISTRY,
    ToolContext,
    WechatSend,
    tool_schemas,
)
from planagent.db.models import (
    BotSession,
    ConversationRole,
    ConversationTurn,
    CrossUserNote,
    CrossUserNoteKind,
    GroupContext,
    GroupMember,
    PendingOutbound,
    PendingOutboundStatus,
    Plan,
    PlanStatus,
    Reminder,
    ReminderStatus,
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.wechat import protocol as wxp
from planagent.wechat.constants import display_name_for, peer_wechat_user_id
from planagent.wechat.protocol import InboundMessage

log = logging.getLogger(__name__)

MAX_ROUNDS = 10
HISTORY_TURNS = 20
SHANGHAI = ZoneInfo("Asia/Shanghai")

SPOKEN_TOOL_NAMES = {"reply_in_group", "ask_user_in_group"}

FALLBACK_TEXT = "我需要人看一下：对话超过了我能处理的轮数。"

# Separator between concatenated spoken-tool texts when the LLM produced
# multiple within a single handler invocation. A Chinese-friendly soft join.
_SEND_MERGE_SEPARATOR = " "


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
                # Stamp display_name from the known-humans roster if we have
                # one. Otherwise leave NULL; it'll be whatever the bridge
                # bootstrap attached.
                display = display_name_for(wechat_user_id)
                session.add(
                    GroupMember(
                        group_id=group.id,
                        wechat_user_id=wechat_user_id,
                        display_name=display,
                    )
                )
            elif member.display_name is None:
                display = display_name_for(wechat_user_id)
                if display is not None:
                    member.display_name = display
        await session.commit()
        return group.id, group.wechat_group_id


async def _load_snapshot(
    session_factory: async_sessionmaker,
    *,
    group_id: str,
    speaker_wechat_user_id: str | None,
) -> GroupSnapshot:
    async with session_factory() as session:
        group = await session.get(GroupContext, group_id)
        assert group is not None

        mres = await session.execute(
            select(GroupMember).where(GroupMember.group_id == group_id)
        )
        members_rows = list(mres.scalars().all())
        members = [
            {
                "wechat_user_id": m.wechat_user_id,
                "display_name": (
                    m.display_name or display_name_for(m.wechat_user_id)
                ),
            }
            for m in members_rows
        ]

        # PR-I: include `overdue` alongside draft/active/paused so the volatile
        # speaker block shows the ⚠️ badge. Completed / cancelled rows stay
        # hidden (noisy; the whiteboard covers them via counts).
        pres = await session.execute(
            select(Plan)
            .where(
                Plan.group_id == group_id,
                Plan.status.in_(
                    [
                        PlanStatus.draft,
                        PlanStatus.active,
                        PlanStatus.paused,
                        PlanStatus.overdue,
                    ]
                ),
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
                    "owner_user_id": p.owner_user_id,
                }
            )

        speaker_name = display_name_for(speaker_wechat_user_id)
        if speaker_name is None and speaker_wechat_user_id:
            for m in members_rows:
                if (
                    m.wechat_user_id == speaker_wechat_user_id
                    and m.display_name
                ):
                    speaker_name = m.display_name
                    break

        return GroupSnapshot(
            group_id=group.id,
            wechat_group_id=group.wechat_group_id,
            group_name=group.name,
            members=members,
            plans=plans_out,
            speaker_wechat_user_id=speaker_wechat_user_id,
            speaker_display_name=speaker_name,
        )


async def _load_history_for_speaker(
    session_factory: async_sessionmaker,
    *,
    group_id: str,
    speaker_user_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Load chat history scoped to a single speaker (PR-G bug #5 fix).

    **Invariant (critical, do not relax)**: the returned message list NEVER
    contains turns whose `target_user_id` was someone other than
    `speaker_user_id`. In our two-person logical group this is the only
    thing preventing Peng's mid-conversation state (e.g. 小计 asking 鹏鹏
    "这个计划的截止时间？") from bleeding into the prompt assembled for
    Chenchen's next inbound.

    Selection rules:
    - role=user: include iff `user_id == speaker_user_id` (their own message).
    - role=assistant | tool: include iff `target_user_id == speaker_user_id`.
    - If speaker_user_id is None (shouldn't happen in prod, but defensive),
      we fall back to an empty list — no cross-speaker contamination possible.
    """
    if not speaker_user_id:
        return []

    async with session_factory() as session:
        stmt = (
            select(ConversationTurn)
            .where(
                ConversationTurn.group_id == group_id,
                or_(
                    # Current speaker's own inbound turns.
                    (ConversationTurn.role == ConversationRole.user)
                    & (ConversationTurn.user_id == speaker_user_id),
                    # Assistant / tool turns directed at this speaker.
                    (
                        ConversationTurn.role.in_(
                            [ConversationRole.assistant, ConversationRole.tool]
                        )
                    )
                    & (ConversationTurn.target_user_id == speaker_user_id),
                ),
            )
            .order_by(ConversationTurn.created_at.desc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        turns = list(reversed(list(res.scalars().all())))
    messages: list[dict[str, Any]] = []
    for t in turns:
        if t.role == ConversationRole.user:
            messages.append({"role": "user", "content": t.content or ""})
        elif t.role == ConversationRole.assistant:
            entry: dict[str, Any] = {"role": "assistant"}
            has_tool_calls = False
            if t.tool_calls_json:
                tc = t.tool_calls_json.get("tool_calls") if isinstance(
                    t.tool_calls_json, dict
                ) else None
                if tc:
                    entry["tool_calls"] = tc
                    has_tool_calls = True
            if has_tool_calls:
                # Hard rule: when an assistant turn has tool_calls, its
                # content is stripped. Even if something persisted a stray
                # reasoning blob, we refuse to feed it back into the LLM.
                entry["content"] = ""
            elif t.content:
                entry["content"] = t.content
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
    target_user_id: str | None = None,
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
                target_user_id=target_user_id,
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


async def _run_tool_call(tc: Any, *, ctx: ToolContext) -> dict[str, Any]:
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


class _DeferredSender:
    """Collect `wechat_send` calls across tool invocations; flush once at end.

    Fixes the "one turn → many messages" symptom. The persona prompt forbids
    multiple sends, but we also enforce it here so even a misbehaving model
    can't fragment a reply across two messages to the user.
    """

    def __init__(self, underlying: WechatSend) -> None:
        self._underlying = underlying
        self._buffer: list[str] = []

    async def __call__(self, text: str) -> None:
        if text is None:
            return
        stripped = text.strip()
        if not stripped:
            return
        # Dedup: if the new text is contained in a previous one (or vice
        # versa), keep only the longer. Without this, the LLM sometimes
        # calls ask_user_in_group twice with near-duplicate wording and
        # the user sees the same question asked twice.
        for i, prev in enumerate(self._buffer):
            if stripped in prev:
                return
            if prev in stripped:
                self._buffer[i] = stripped
                return
        self._buffer.append(stripped)

    @property
    def buffer(self) -> list[str]:
        return list(self._buffer)

    async def flush(self) -> str | None:
        """Send the merged text if anything was buffered. Returns the sent text."""
        if not self._buffer:
            return None
        merged = _SEND_MERGE_SEPARATOR.join(self._buffer)
        await self._underlying(merged)
        return merged

    async def fallback(self, text: str) -> None:
        """Queue a fallback message ONLY when the buffer is empty.

        When the agent produces any visible output, we don't want to trail
        it with an operator-facing "我需要人看一下" blurb. The fallback
        exists to make sure silent failures don't look like the bot froze;
        it shouldn't stomp on a real reply.
        """
        if self._buffer:
            return
        await self(text)


async def _build_whiteboard(
    session_factory: async_sessionmaker,
    *,
    group_id: str,
    speaker_wechat_user_id: str | None,
    snapshot: GroupSnapshot,
) -> tuple[Whiteboard, list[str]]:
    """Assemble the whiteboard for this speaker.

    Returns (whiteboard, consumable_note_ids). The caller is expected to
    stamp `consumed_at` on those ids after the prompt has been built, so
    a single inbound marks them as seen exactly once. Kept separate from
    rendering to preserve the "render → persist" ordering even if one step
    errors.
    """
    # Identify the peer by walking the members list. The snapshot already
    # carries display names (PR-G hook), so reuse rather than re-querying.
    peer_display: str | None = None
    peer_uid: str | None = None
    for m in snapshot.members:
        uid = m.get("wechat_user_id")
        if uid and uid != speaker_wechat_user_id:
            peer_uid = uid
            peer_display = m.get("display_name")
            break

    peer_last_inbound_at: datetime | None = None
    peer_open_plans = 0
    peer_overdue = 0
    peer_completed_today = 0
    plans_by_owner: dict[str, list[dict[str, Any]]] = {}
    unconsumed_notes: list[dict[str, Any]] = []
    consumable_ids: list[str] = []

    now_utc = datetime.now(UTC)
    # 00:00 Asia/Shanghai → UTC cutoff, for today-completion count.
    now_local = now_utc.astimezone(SHANGHAI)
    start_of_today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_today_utc = start_of_today_local.astimezone(UTC)
    async with session_factory() as session:
        if peer_uid:
            bs_res = await session.execute(
                select(BotSession).where(BotSession.wechat_user_id == peer_uid)
            )
            bs = bs_res.scalar_one_or_none()
            if bs is not None and bs.last_inbound_at is not None:
                la = bs.last_inbound_at
                if la.tzinfo is None:
                    la = la.replace(tzinfo=UTC)
                peer_last_inbound_at = la.astimezone(SHANGHAI)

            # PR-I: open = draft + active (what still needs active care).
            # overdue, paused and cancelled are counted / ignored separately.
            open_statuses = [PlanStatus.draft, PlanStatus.active]
            pres = await session.execute(
                select(Plan).where(
                    Plan.group_id == group_id,
                    Plan.owner_user_id == peer_uid,
                    Plan.status.in_(open_statuses),
                )
            )
            peer_open_plans = len(list(pres.scalars().all()))

            ores = await session.execute(
                select(Plan).where(
                    Plan.group_id == group_id,
                    Plan.owner_user_id == peer_uid,
                    Plan.status == PlanStatus.overdue,
                )
            )
            peer_overdue = len(list(ores.scalars().all()))

            cres = await session.execute(
                select(Plan).where(
                    Plan.group_id == group_id,
                    Plan.owner_user_id == peer_uid,
                    Plan.status == PlanStatus.completed,
                    Plan.updated_at >= start_of_today_utc,
                )
            )
            peer_completed_today = len(list(cres.scalars().all()))

        # Plans-by-owner summary for the rendered board. Keyed by display
        # name so the prompt reads naturally; lists are small (we filter
        # to open statuses at load time).
        for p in snapshot.plans:
            owner_uid = p.get("owner_user_id")
            if not owner_uid:
                continue
            # Resolve owner_uid → display name via the member roster.
            label: str | None = None
            if owner_uid == speaker_wechat_user_id:
                label = snapshot.speaker_display_name
            else:
                for m in snapshot.members:
                    if m.get("wechat_user_id") == owner_uid:
                        label = m.get("display_name")
                        break
            if not label:
                continue
            plans_by_owner.setdefault(label, []).append(
                {
                    "title": p.get("title"),
                    "status": p.get("status"),
                    "next_fire_at": p.get("next_fire_at"),
                }
            )

        if speaker_wechat_user_id:
            nres = await session.execute(
                select(CrossUserNote)
                .where(
                    CrossUserNote.group_id == group_id,
                    CrossUserNote.audience_user_id == speaker_wechat_user_id,
                    CrossUserNote.consumed_at.is_(None),
                )
                .order_by(CrossUserNote.created_at.asc())
            )
            for n in nres.scalars().all():
                created_local = ""
                if n.created_at is not None:
                    ca = n.created_at
                    if ca.tzinfo is None:
                        ca = ca.replace(tzinfo=UTC)
                    created_local = ca.astimezone(SHANGHAI).strftime("%H:%M")
                kind_val = n.kind.value if isinstance(n.kind, CrossUserNoteKind) else n.kind
                unconsumed_notes.append(
                    {
                        "kind": kind_val,
                        "text": n.text,
                        "created_at_local": created_local,
                    }
                )
                consumable_ids.append(n.id)

    wb = Whiteboard(
        peer_display_name=peer_display,
        peer_last_inbound_at=peer_last_inbound_at,
        peer_open_plans=peer_open_plans,
        peer_overdue_count=peer_overdue,
        peer_completed_today=peer_completed_today,
        unconsumed_notes=unconsumed_notes,
        plans_by_owner=plans_by_owner,
    )
    return wb, consumable_ids


async def _flush_pending_outbound(
    session_factory: async_sessionmaker,
    *,
    group_id: str,
    target_user_id: str,
    wechat_send: WechatSend,
) -> int:
    """Send any queued cross-user messages addressed to this speaker.

    Each pending row is delivered as its own outbound — the underlying
    WeChat client stamps a unique client_id per send (PR #9 fix), so the
    ClawBot dedup won't collapse consecutive deliveries. Rows are flipped
    to `delivered` only after the send returns; if the send throws, the
    row stays `pending` and we'll retry on a later inbound.
    """
    # Snapshot the rows first so we release the session before awaiting
    # the (slow) transport. Keeping the session open across an I/O await
    # would serialize every subsequent DB write in the handler.
    async with session_factory() as session:
        res = await session.execute(
            select(PendingOutbound)
            .where(
                PendingOutbound.group_id == group_id,
                PendingOutbound.target_user_id == target_user_id,
                PendingOutbound.status == PendingOutboundStatus.pending,
            )
            .order_by(PendingOutbound.created_at.asc())
        )
        rows = list(res.scalars().all())
        pending = [(r.id, r.text) for r in rows]

    delivered = 0
    for row_id, text in pending:
        try:
            await wechat_send(text)
        except Exception:
            log.exception("pending_outbound %s delivery failed", row_id)
            continue
        async with session_factory() as session:
            row = await session.get(PendingOutbound, row_id)
            if row is None:
                continue
            row.status = PendingOutboundStatus.delivered
            row.delivered_at = datetime.now(UTC)
            await session.commit()
        delivered += 1
    return delivered


async def _mark_notes_consumed(
    session_factory: async_sessionmaker, *, note_ids: list[str]
) -> None:
    if not note_ids:
        return
    async with session_factory() as session:
        for nid in note_ids:
            row = await session.get(CrossUserNote, nid)
            if row is None or row.consumed_at is not None:
                continue
            row.consumed_at = datetime.now(UTC)
        await session.commit()


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
    speaker_user_id = wxp.sender_id(msg)

    group_internal_id, _ = await _upsert_group_and_member(
        session_factory,
        wechat_group_id=wechat_group_id,
        wechat_user_id=speaker_user_id,
    )

    # Persist the inbound as the FIRST turn for this handler invocation. Note
    # both speaker and target are the same for user rows.
    await _append_turn(
        session_factory,
        group_id=group_internal_id,
        role=ConversationRole.user,
        content=text,
        user_id=speaker_user_id,
        target_user_id=speaker_user_id,
        context_token=msg.context_token,
    )

    # PR-H: before running the agent loop, flush any cross-user messages
    # queued for this speaker. These go out on the live `wechat_send` (not
    # through the DeferredSender) so each has its own client_id and arrives
    # before the agent's own reply to the current inbound.
    if speaker_user_id:
        await _flush_pending_outbound(
            session_factory,
            group_id=group_internal_id,
            target_user_id=speaker_user_id,
            wechat_send=wechat_send,
        )

    snapshot = await _load_snapshot(
        session_factory,
        group_id=group_internal_id,
        speaker_wechat_user_id=speaker_user_id,
    )
    # PR-H: assemble whiteboard with unconsumed peer→speaker notes + peer
    # activity snapshot. Consumption is deferred to AFTER a successful
    # outbound flush (see end of handler) — if DeepSeek or the transport
    # raises, the notes must remain unread so the next turn resurfaces
    # them. Losing cross-user context silently would be a worse failure
    # than redelivering a nudge.
    whiteboard, consumable_note_ids = await _build_whiteboard(
        session_factory,
        group_id=group_internal_id,
        speaker_wechat_user_id=speaker_user_id,
        snapshot=snapshot,
    )
    snapshot.whiteboard = whiteboard
    now = datetime.now(SHANGHAI)
    system_prompt = make_prompt(snapshot, now=now)

    history = await _load_history_for_speaker(
        session_factory,
        group_id=group_internal_id,
        speaker_user_id=speaker_user_id,
        limit=history_turns,
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}, *history]

    # Wrap the underlying sender so tool invocations queue rather than send
    # immediately; we flush exactly once at handler exit.
    deferred = _DeferredSender(wechat_send)

    ctx = ToolContext(
        session_factory=session_factory,
        group_id=group_internal_id,
        wechat_group_id=wechat_group_id,
        wechat_send=deferred,
        sender_user_id=speaker_user_id,
        peer_user_id=peer_wechat_user_id(speaker_user_id),
    )

    schemas = tool_schemas()
    called_spoken_tool = False

    for round_idx in range(max_rounds):
        # Temperature dispatch: first round is dialogue-warm; any later round
        # (we're in tool-call territory) is deterministic. See module docstring.
        temp = DIALOGUE_TEMP if round_idx == 0 else ACTION_TEMP
        resp = deepseek.chat(
            messages=messages,
            tools=schemas,
            tool_choice="auto",
            temperature=temp,
        )
        choice_msg = resp.choices[0].message
        tool_calls = getattr(choice_msg, "tool_calls", None) or []

        if tool_calls:
            # Chain-of-thought guard: when tool_calls are present, ignore the
            # LLM's `content` entirely. DeepSeek-R1-style reasoning often
            # lands there and would leak into both the persisted history and
            # (through the fed-back assistant entry) subsequent rounds.
            assistant_entry: dict[str, Any] = {
                "role": "assistant",
                "content": "",
                "tool_calls": _tool_calls_to_json(tool_calls),
            }
            messages.append(assistant_entry)

            await _append_turn(
                session_factory,
                group_id=group_internal_id,
                role=ConversationRole.assistant,
                content="",  # CoT drop: persist empty, not the raw content.
                target_user_id=speaker_user_id,
                tool_calls_json={"tool_calls": assistant_entry["tool_calls"]},
            )

            buffer_len_before_round = len(deferred.buffer)
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
                    target_user_id=speaker_user_id,
                    tool_call_id=tc.id,
                )
            # Early termination: break only if a spoken tool *actually*
            # produced buffered output this round. A spoken tool that
            # errored out (bad_arguments, whitespace-only text filtered
            # by the DeferredSender, …) doesn't count — the LLM still
            # needs a chance to recover, or the user gets silence.
            spoke_this_round = len(deferred.buffer) > buffer_len_before_round
            if called_spoken_tool and spoke_this_round:
                break
            continue

        # No tool calls: this is the terminal assistant message.
        content = choice_msg.content or ""
        await _append_turn(
            session_factory,
            group_id=group_internal_id,
            role=ConversationRole.assistant,
            content=content,
            target_user_id=speaker_user_id,
        )
        if content.strip() and not called_spoken_tool:
            # Implicit reply — goes through the deferred sender too so if the
            # LLM also spoke via a tool earlier, we still merge.
            await deferred(content)
        break
    else:
        # Ran out of rounds. Only surface a fallback if nothing was spoken
        # yet — a real partial reply is strictly better than boilerplate.
        log.warning("agent hit MAX_ROUNDS=%d without terminating", max_rounds)
        await deferred.fallback(FALLBACK_TEXT)
        await _append_turn(
            session_factory,
            group_id=group_internal_id,
            role=ConversationRole.assistant,
            content=FALLBACK_TEXT,
            target_user_id=speaker_user_id,
        )

    # Flush the accumulated outbound. One send per handler invocation.
    sent = await deferred.flush()
    if sent is not None:
        ctx.sent_texts.append(sent)

    # Only NOW mark whiteboard notes consumed — if we got here, the turn
    # has reached a terminal state (either `sent` is non-None or the LLM
    # opted to stay silent). Any exception from the loop or flush would
    # have propagated before this line, preserving the unread notes for
    # a retry on the next inbound.
    if consumable_note_ids:
        await _mark_notes_consumed(session_factory, note_ids=consumable_note_ids)


__all__ = ["FALLBACK_TEXT", "MAX_ROUNDS", "handle_inbound"]
