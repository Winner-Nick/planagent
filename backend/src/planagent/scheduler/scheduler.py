"""Periodic reminder scheduler.

A tick:
  1. Load draft/active plans.
  2. Ask the LLM per plan whether/when/what to remind.
  3. For each 'yes' decision whose fire_at falls inside the current tick window,
     materialize a Reminder (pending). If fire_at is already due, fire now.
  4. Sweep any pending reminders (from earlier ticks) whose fire_at is due.

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
    GroupContext,
    GroupMember,
    Plan,
    PlanStatus,
    Reminder,
    ReminderStatus,
)
from planagent.llm.deepseek import DeepSeekClient
from planagent.scheduler.decider import BEIJING, ReminderDecision, decide

log = logging.getLogger(__name__)

# Signature: (wechat_group_id, message, context_token_or_none) -> awaitable
WechatSend = Callable[[str, str, str | None], Awaitable[None]]

# Slack window past the tick interval — reminders due up to this far in the
# future are materialized on the current tick rather than deferred.
DEFAULT_SLACK_S = 30


@dataclass
class _PendingSend:
    reminder_id: str
    group_wechat_id: str
    context_token: str | None
    message: str


class Scheduler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        deepseek: DeepSeekClient,
        wechat_send: WechatSend,
        *,
        max_concurrency: int = 5,
        slack_s: int = DEFAULT_SLACK_S,
    ) -> None:
        self._sm = sessionmaker
        self._deepseek = deepseek
        self._send = wechat_send
        self._sem = asyncio.Semaphore(max_concurrency)
        self._slack = timedelta(seconds=slack_s)

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
            try:
                fire_at_utc = datetime.fromisoformat(dec.fire_at_local_iso).astimezone(UTC)
            except ValueError:
                log.warning(
                    "plan %s: un-parseable fire_at_local_iso=%r", plan.id, dec.fire_at_local_iso
                )
                continue
            if fire_at_utc > window_end:
                # Outside this tick's horizon — a future tick will revisit.
                continue
            await self._insert_reminder_if_absent(plan.id, fire_at_utc, dec.message)

        # Sweep due pending reminders (new + carried-over).
        to_send = await self._claim_due_reminders(now)
        for item in to_send:
            try:
                await self._send(item.group_wechat_id, item.message, item.context_token)
            except Exception:  # noqa: BLE001
                log.exception("wechat_send failed for reminder %s", item.reminder_id)

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
                .options(selectinload(Reminder.plan).selectinload(Plan.group))
            )
            reminders = list(result.scalars().all())
            group_token_cache: dict[str, str | None] = {}
            for r in reminders:
                r.status = ReminderStatus.sent
                r.fired_at = now
                plan = r.plan
                group: GroupContext = plan.group
                token = group_token_cache.get(group.id)
                if token is None and group.id not in group_token_cache:
                    token = group.last_context_token
                    group_token_cache[group.id] = token
                claimed.append(
                    _PendingSend(
                        reminder_id=r.id,
                        group_wechat_id=group.wechat_group_id,
                        context_token=token,
                        message=r.message,
                    )
                )
            await session.commit()
        return claimed
