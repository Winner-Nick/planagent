"""Periodic reminder scheduler + LLM-driven keep-alive.

A tick:
  1. Load draft/active plans.
  2. Ask the LLM per plan whether/when/what to remind.
  3. For each 'yes' decision whose fire_at falls inside the current tick window,
     materialize a Reminder (pending). If fire_at is already due, fire now.
  4. Sweep any pending reminders (from earlier ticks) whose fire_at is due.
     Reminders fan out to every BotSession in the plan's group — each user
     gets the same text, threaded on THAT user's latest context_token.
  5. Ask the LLM per BotSession whether to send a keep-alive wake-up ping,
     so the ClawBot 24-hour outbound window stays open.

Idempotency: we never claim a Reminder to send without first flipping its
status from `pending` to `sent` (and stamping `fired_at`) inside a single
transaction. The "row lock" primitive on SQLite is the transaction itself —
a concurrent tick that observes `status != pending` skips the row. A crash
between commit and `wechat_send` loses that single send; this is
acknowledged as MVP behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from planagent.db.models import (
    BotSession,
    GroupContext,
    GroupMember,
    Plan,
    PlanStatus,
    Reminder,
    ReminderStatus,
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.scheduler.decider import BEIJING, ReminderDecision, decide
from planagent.scheduler.wakeup import decide_wakeup

log = logging.getLogger(__name__)

# Signature: (bot_token, to_user_id, text, context_token_or_none) -> awaitable
# This is the per-session send shape. The WeChat transport (ClawBotClient)
# doesn't know about "groups" — every send is 1:1.
WechatSend = Callable[[str, str, str, str | None], Awaitable[None]]

# Legacy (PR-E) signature, kept for a thin compatibility shim:
#   (wechat_group_id, message, context_token_or_none) -> awaitable
LegacyGroupSend = Callable[[str, str, str | None], Awaitable[None]]

# Slack window past the tick interval — reminders due up to this far in the
# future are materialized on the current tick rather than deferred.
DEFAULT_SLACK_S = 30


@dataclass
class _PendingSend:
    reminder_id: str
    bot_token: str
    to_user_id: str
    session_id: str | None  # BotSession.id if the reminder fans to a session
    context_token: str | None
    message: str


@dataclass
class _WakeupSend:
    session_id: str
    bot_token: str
    to_user_id: str
    context_token: str | None
    text: str


class Scheduler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        deepseek: DeepSeekClient,
        wechat_send: WechatSend | LegacyGroupSend,
        *,
        max_concurrency: int = 5,
        slack_s: int = DEFAULT_SLACK_S,
        enable_wakeup: bool = True,
    ) -> None:
        self._sm = sessionmaker
        self._deepseek = deepseek
        self._send = wechat_send
        self._sem = asyncio.Semaphore(max_concurrency)
        self._slack = timedelta(seconds=slack_s)
        self._enable_wakeup = enable_wakeup
        # Heuristic: a 4-arg callable is a per-session send; 3-arg is legacy.
        self._send_is_per_session = _send_arity(wechat_send) >= 4

    # Seam for tests.
    def _now(self) -> datetime:
        return datetime.now(UTC)

    async def run(
        self,
        *,
        interval_s: int = 300,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            try:
                await self.tick(interval_s=interval_s)
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)

    async def tick(self, *, interval_s: int = 300) -> None:
        now = self._now()
        window_end = now + timedelta(seconds=interval_s) + self._slack

        plans = await self._load_active_plans()
        members_by_group = await self._load_members(
            {p.group_id for p in plans}
        )

        # PR-G safety net (fixes bug #4): if a plan has start_at inside this
        # tick window but no pending Reminder near that time, auto-materialize
        # one. The persona prompt tells the agent to always schedule a
        # reminder alongside start_at/due_at, but we backstop in case the
        # LLM skipped. Materializing here is cheap and idempotent (same
        # fire_at bucket as `_insert_reminder_if_absent`).
        for p in plans:
            await self._ensure_start_at_reminder(p, now=now, window_end=window_end)

        # Ask the LLM per plan (bounded concurrency).
        async def _one(plan: Plan) -> tuple[Plan, ReminderDecision | None]:
            async with self._sem:
                try:
                    recent = await self._recent_reminders(plan.id)
                    dec = await decide(
                        plan,
                        now_local=now.astimezone(BEIJING),
                        recent_reminders=recent,
                        deepseek=self._deepseek,
                        group_members=members_by_group.get(plan.group_id, []),
                    )
                    return plan, dec
                except Exception:  # noqa: BLE001
                    log.exception("decide() failed for plan %s", plan.id)
                    return plan, None

        results = await asyncio.gather(*(_one(p) for p in plans))

        # Materialize new reminders the LLM wants in this window.
        for plan, dec in results:
            if dec is None or not dec.should_remind:
                continue
            if not dec.fire_at_local_iso or not dec.message:
                continue
            if not isinstance(dec.fire_at_local_iso, str):
                log.warning(
                    "plan %s: non-string fire_at_local_iso=%r", plan.id, dec.fire_at_local_iso
                )
                continue
            try:
                fire_at_utc = datetime.fromisoformat(dec.fire_at_local_iso).astimezone(UTC)
            except (ValueError, TypeError):
                log.warning(
                    "plan %s: un-parseable fire_at_local_iso=%r", plan.id, dec.fire_at_local_iso
                )
                continue
            if fire_at_utc > window_end:
                # Outside this tick's horizon — a future tick will revisit.
                continue
            await self._insert_reminder_if_absent(plan.id, fire_at_utc, dec.message)

        # Sweep due pending reminders (new + carried-over). Each due reminder
        # fans out to every BotSession in the plan's group.
        to_send = await self._claim_due_reminders(now)
        for item in to_send:
            try:
                await self._dispatch_send(
                    bot_token=item.bot_token,
                    to_user_id=item.to_user_id,
                    text=item.message,
                    context_token=item.context_token,
                )
            except Exception:  # noqa: BLE001
                log.exception("wechat_send failed for reminder %s", item.reminder_id)
            else:
                # Stamp last_outbound_at BEFORE the wake-up pass so the LLM
                # doesn't double-nudge right after we already sent a reminder.
                if item.session_id is not None:
                    await self._stamp_session_outbound(item.session_id, now)

        # Wake-up ping pass.
        if self._enable_wakeup:
            wakeups = await self._decide_wakeups(now)
            for w in wakeups:
                try:
                    await self._dispatch_send(
                        bot_token=w.bot_token,
                        to_user_id=w.to_user_id,
                        text=w.text,
                        context_token=w.context_token,
                    )
                    await self._stamp_wakeup_sent(w.session_id, now)
                except Exception:  # noqa: BLE001
                    log.exception("wakeup send failed for session %s", w.session_id)

    async def _dispatch_send(
        self,
        *,
        bot_token: str,
        to_user_id: str,
        text: str,
        context_token: str | None,
    ) -> None:
        if self._send_is_per_session:
            await self._send(bot_token, to_user_id, text, context_token)  # type: ignore[arg-type]
        else:
            # Legacy path: pretend the "group" is the to_user_id. PR-E tests
            # exercise this — newer code should supply a 4-arg send.
            await self._send(to_user_id, text, context_token)  # type: ignore[arg-type, call-arg]

    # --- DB helpers ------------------------------------------------------

    async def _load_active_plans(self) -> list[Plan]:
        async with self._sm() as session:
            result = await session.execute(
                select(Plan).where(
                    Plan.status.in_([PlanStatus.draft, PlanStatus.active])
                )
            )
            return list(result.scalars().all())

    async def _load_members(self, group_ids: set[str]) -> dict[str, list[GroupMember]]:
        if not group_ids:
            return {}
        async with self._sm() as session:
            result = await session.execute(
                select(GroupMember).where(GroupMember.group_id.in_(group_ids))
            )
            by_group: dict[str, list[GroupMember]] = {}
            for m in result.scalars().all():
                by_group.setdefault(m.group_id, []).append(m)
            return by_group

    async def _recent_reminders(self, plan_id: str) -> list[Reminder]:
        async with self._sm() as session:
            result = await session.execute(
                select(Reminder)
                .where(Reminder.plan_id == plan_id)
                .order_by(Reminder.fire_at.desc())
                .limit(3)
            )
            return list(result.scalars().all())

    async def _ensure_start_at_reminder(
        self, plan: Plan, *, now: datetime, window_end: datetime
    ) -> None:
        """Materialize a Reminder for plan.start_at if inside the tick window.

        This is a safety net for cases where the agent wrote `start_at` but
        forgot to call `schedule_reminder`. Covers both past-but-recent
        (fire within the last slack interval) and the near future up to
        `window_end`. No-op if a pending/sent reminder already exists near
        that fire time — bucketed by `_insert_reminder_if_absent`'s own
        dedupe logic.
        """
        start_at = plan.start_at
        if start_at is None:
            return
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=UTC)
        # Only nudge for imminent starts (not something 3 months out).
        if start_at < now - self._slack:
            return
        if start_at > window_end:
            return
        # Compose a pragmatic Chinese fallback message. The prompt asked the
        # agent to schedule its own better-worded one; this is the safety net.
        local = start_at.astimezone(BEIJING)
        msg = (
            f"（系统兜底）到 {local.strftime('%H:%M')} 啦，记得开始「{plan.title}」"
        )
        await self._insert_reminder_if_absent(plan.id, start_at, msg)

    async def _insert_reminder_if_absent(
        self, plan_id: str, fire_at_utc: datetime, message: str
    ) -> None:
        """Avoid duplicating a reminder the LLM re-proposes on successive ticks.

        Dedupe key: same plan_id + same fire_at (bucketed to the minute) among
        non-cancelled reminders.
        """
        bucket_lo = fire_at_utc - timedelta(seconds=60)
        bucket_hi = fire_at_utc + timedelta(seconds=60)
        async with self._sm() as session:
            existing = await session.execute(
                select(Reminder).where(
                    Reminder.plan_id == plan_id,
                    Reminder.fire_at >= bucket_lo,
                    Reminder.fire_at <= bucket_hi,
                    Reminder.status != ReminderStatus.cancelled,
                )
            )
            if existing.scalars().first() is not None:
                return
            session.add(
                Reminder(
                    plan_id=plan_id,
                    fire_at=fire_at_utc,
                    message=message,
                    status=ReminderStatus.pending,
                )
            )
            await session.commit()

    async def _claim_due_reminders(self, now: datetime) -> list[_PendingSend]:
        """Atomically flip due pending reminders to `sent` and return send jobs.

        Flipping before actual send is intentional: it prevents two ticks from
        sending the same reminder. The trade-off is the crash-between-commit-
        and-send window — documented at module level.

        Each reminder fans out to every BotSession in the plan's group that
        has a known `wechat_user_id`. A session without a wechat_user_id yet
        (never got an inbound) is skipped. If the scheduler is running with
        the legacy 3-arg `WechatSend` signature and no BotSession exists, we
        fall back to sending once to the group's wechat_group_id (PR-E shim).
        """
        claimed: list[_PendingSend] = []
        async with self._sm() as session:
            result = await session.execute(
                select(Reminder)
                .where(
                    Reminder.status == ReminderStatus.pending,
                    Reminder.fire_at <= now,
                    Reminder.fired_at.is_(None),
                )
                .options(
                    selectinload(Reminder.plan)
                    .selectinload(Plan.group)
                    .selectinload(GroupContext.bot_sessions)
                )
            )
            reminders = list(result.scalars().all())
            for r in reminders:
                r.status = ReminderStatus.sent
                r.fired_at = now
                plan = r.plan
                group: GroupContext = plan.group
                sessions_in_group = list(group.bot_sessions or [])
                if sessions_in_group:
                    for bs in sessions_in_group:
                        if not bs.wechat_user_id:
                            continue
                        claimed.append(
                            _PendingSend(
                                reminder_id=r.id,
                                bot_token=bs.bot_token,
                                to_user_id=bs.wechat_user_id,
                                session_id=bs.id,
                                context_token=bs.last_context_token,
                                message=r.message,
                            )
                        )
                elif not self._send_is_per_session:
                    # Legacy path (PR-E-era DB): only safe when a 3-arg
                    # wechat_send was wired, which expects (group, msg, ctx).
                    # Under a 4-arg per-session sender, an empty bot_token
                    # would silently 401 after the reminder was already
                    # flipped to `sent`, dropping the notification entirely.
                    claimed.append(
                        _PendingSend(
                            reminder_id=r.id,
                            bot_token="",
                            to_user_id=group.wechat_group_id,
                            session_id=None,
                            context_token=group.last_context_token,
                            message=r.message,
                        )
                    )
                else:
                    log.warning(
                        "reminder %s has no BotSession in group %s — skipping "
                        "send and leaving reminder as sent (no recipient)",
                        r.id,
                        group.id,
                    )
            await session.commit()
        return claimed

    async def _decide_wakeups(self, now: datetime) -> list[_WakeupSend]:
        """Iterate active BotSessions and ask the LLM whether to wake each."""
        async with self._sm() as session:
            res = await session.execute(
                select(BotSession).options(
                    selectinload(BotSession.group).selectinload(GroupContext.bot_sessions)
                )
            )
            all_sessions = list(res.scalars().all())

        decisions: list[_WakeupSend] = []
        for bs in all_sessions:
            if not bs.wechat_user_id:
                continue
            peer = _pick_peer(bs)
            try:
                dec = await decide_wakeup(
                    bs,
                    peer,
                    now_utc=now,
                    deepseek=self._deepseek,
                )
            except Exception:  # noqa: BLE001
                log.exception("decide_wakeup failed for session %s", bs.id)
                continue
            if not dec.should_ping or not dec.text:
                continue
            if not bs.last_context_token:
                # We have no way to thread the reply without a token; skip.
                log.info(
                    "wakeup decided for %s but no last_context_token — skipping",
                    bs.name,
                )
                continue
            decisions.append(
                _WakeupSend(
                    session_id=bs.id,
                    bot_token=bs.bot_token,
                    to_user_id=bs.wechat_user_id,
                    context_token=bs.last_context_token,
                    text=dec.text,
                )
            )
        return decisions

    async def _stamp_wakeup_sent(self, session_id: str, now: datetime) -> None:
        async with self._sm() as session:
            bs = await session.get(BotSession, session_id)
            if bs is not None:
                bs.last_wakeup_ping_at = now
                bs.last_outbound_at = now
                await session.commit()

    async def _stamp_session_outbound(self, session_id: str, now: datetime) -> None:
        async with self._sm() as session:
            bs = await session.get(BotSession, session_id)
            if bs is not None:
                bs.last_outbound_at = now
                await session.commit()


def _pick_peer(bs: BotSession) -> BotSession | None:
    """Pick the other session in the same logical group, if exactly one exists.

    For a 2-person fake group, this is unambiguous. For >2, we return the one
    that has gone silent the longest (most at-risk of dropping off) so the
    LLM's peer context is most useful.
    """
    if bs.group is None:
        return None
    others = [s for s in (bs.group.bot_sessions or []) if s.id != bs.id]
    if not others:
        return None
    if len(others) == 1:
        return others[0]

    def _staleness(s: BotSession) -> datetime:
        # sort key: oldest last_inbound_at first (None → epoch)
        return s.last_inbound_at or datetime.fromtimestamp(0, tz=UTC)

    return min(others, key=_staleness)


def _send_arity(fn: Callable[..., Awaitable[None]]) -> int:
    """Best-effort signature introspection. Tests use plain async fns; the
    bridge CLI passes bound methods. Fall back to 4 (per-session) if unknown.
    """
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return 4
    # Count positional-or-keyword parameters that aren't *args / **kwargs.
    count = 0
    for p in sig.parameters.values():
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            count += 1
    return count if count else 4
