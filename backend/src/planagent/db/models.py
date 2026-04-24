from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class PlanStatus(str, enum.Enum):  # noqa: UP042
    draft = "draft"
    active = "active"
    completed = "completed"
    paused = "paused"
    # PR-I: added. `overdue` is scheduler-driven (non-recurring active plans
    # whose due_at passed the grace window become overdue); `cancelled` is
    # user-intent-driven (via the `cancel_plan` tool) so we preserve the row
    # for audit instead of deleting it.
    overdue = "overdue"
    cancelled = "cancelled"


class ReminderStatus(str, enum.Enum):  # noqa: UP042
    pending = "pending"
    sent = "sent"
    skipped = "skipped"
    cancelled = "cancelled"


class ConversationRole(str, enum.Enum):  # noqa: UP042
    user = "user"
    assistant = "assistant"
    tool = "tool"
    system = "system"


class PendingOutboundStatus(str, enum.Enum):  # noqa: UP042
    pending = "pending"
    delivered = "delivered"
    cancelled = "cancelled"


class CrossUserNoteKind(str, enum.Enum):  # noqa: UP042
    """Taxonomy for the cross-user whiteboard (PR-H).

    - info: passive tidbit (e.g. Peng mentions 辰辰最近压力大; 小计 stashes it
      so her next turn's prompt reflects that context).
    - nudge_request: the author explicitly wants the audience prodded about
      something (e.g. "记得催鹏鹏学 Rust"). Rendered louder on the whiteboard.
    - appreciate: a warm note meant to surface as positive reinforcement.
    """

    info = "info"
    nudge_request = "nudge_request"
    appreciate = "appreciate"


class GroupContext(Base):
    __tablename__ = "group_contexts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    wechat_group_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Kept for backward compatibility (pre-PR-F scheduler sends). PR-F moves the
    # authoritative per-user token onto BotSession.last_context_token.
    last_context_token: Mapped[str | None] = mapped_column(String(128), nullable=True)

    members: Mapped[list[GroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    plans: Mapped[list[Plan]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    turns: Mapped[list[ConversationTurn]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    bot_sessions: Mapped[list[BotSession]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"), nullable=False
    )
    wechat_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    group: Mapped[GroupContext] = relationship(back_populates="members")


class BotSession(Base):
    """One logical 1:1 ClawBot chat, emulating a member of the fake group.

    The ClawBot platform doesn't support real group chat on personal WeChat, so
    we fan out a "group" as N independent 1:1 sessions that share a logical
    `group_id`. Each BotSession has its own bot_token (one ClawBot per user),
    its own last_inbound_at / last_outbound_at (for the 24h keep-alive window),
    and its own `last_context_token` — the ClawBot protocol threads replies on
    that token, and it's scoped per user, NOT per group.
    """

    __tablename__ = "bot_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Credential-file friendly label (e.g. "peng", "chenchen"). Unique so the
    # bootstrap loader can upsert by filename.
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # Starts NULL — we don't know the user's wechat_user_id until their first
    # inbound message arrives. Runtime fills it in, and it becomes unique.
    wechat_user_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True, index=True
    )
    bot_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bot_token: Mapped[str] = mapped_column(String(512), nullable=False)
    baseurl: Mapped[str | None] = mapped_column(String(256), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_outbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_wakeup_ping_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Context token from this user's most recent inbound — required for any
    # scheduler-originated (no-live-message) send. Per-session because ClawBot
    # scopes context tokens to the conversation, not to our logical group.
    last_context_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    group: Mapped[GroupContext] = relationship(back_populates="bot_sessions")


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[PlanStatus] = mapped_column(
        Enum(PlanStatus, name="plan_status"), default=PlanStatus.draft, nullable=False
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_duration_per_session_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recurrence_cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    group: Mapped[GroupContext] = relationship(back_populates="plans")
    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ReminderStatus] = mapped_column(
        Enum(ReminderStatus, name="reminder_status"),
        default=ReminderStatus.pending,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    plan: Mapped[Plan] = relationship(back_populates="reminders")


class ConversationTurn(Base):
    """Single row in the per-group conversation log.

    PR-G split: `user_id` semantically means **speaker_user_id** (who said this
    turn / whose inbound triggered this assistant-or-tool row). `target_user_id`
    is the user the orchestrator was replying TO on this turn. For user rows
    those two are identical; for assistant/tool rows generated while handling
    speaker X, both are X. This lets history loading filter by the
    current speaker and avoid cross-user contamination.
    """

    __tablename__ = "conversation_turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[ConversationRole] = mapped_column(
        Enum(ConversationRole, name="conversation_role"), nullable=False
    )
    # Semantically "speaker_user_id" — keeping the physical name stable so
    # existing callers / queries don't break. Use the `speaker_user_id`
    # property below for readability.
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # PR-G: added. For role=user, mirrors `user_id`. For assistant/tool,
    # identifies the user this reply is addressed to (i.e. the speaker of
    # the inbound that this handler invocation is processing).
    target_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    group: Mapped[GroupContext] = relationship(back_populates="turns")

    @property
    def speaker_user_id(self) -> str | None:
        """Readability alias for `user_id` — the speaker on this turn."""
        return self.user_id


class PendingOutbound(Base):
    """Queued outbound text awaiting a user's wake-up window.

    Skeleton in PR-G: PR-H will route cross-user notifications through this
    table (e.g. Peng tells 小计 "告诉辰辰晚上一起复盘一下" and it lands here
    until 辰辰 next pings the bot). Defined now so PR-H doesn't need another
    migration.
    """

    __tablename__ = "pending_outbound"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"), nullable=False
    )
    target_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[PendingOutboundStatus] = mapped_column(
        Enum(PendingOutboundStatus, name="pending_outbound_status"),
        default=PendingOutboundStatus.pending,
        nullable=False,
    )


class ScheduledMessageStatus(str, enum.Enum):  # noqa: UP042
    """State machine for a one-off ScheduledMessage (PR-L).

    - pending: inserted by `schedule_message_to_peer`, awaiting fire_at.
    - sent: scheduler tick dispatched it to the target BotSession.
    - cancelled: reserved for a future cancel-tool; no writer today.
    """

    pending = "pending"
    sent = "sent"
    cancelled = "cancelled"


class ScheduledMessage(Base):
    """One-off cross-user nudge with a fire_at (PR-L).

    The design tension this model resolves: **"告诉对方 X 三分钟后"** is
    intent-wise a tiny scheduled message, not a plan. Before PR-L the LLM
    modeled it as `create_plan_draft(owner=peer) + schedule_reminder`,
    which polluted the plan board with one-off strings like "告诉辰辰她
    吃药了吗" and made the plan list semantically meaningless. PR-L splits
    the two concepts:

    - Plan + Reminder: persistent commitment (has a title worth tracking,
      may recur, shows on the plan board).
    - ScheduledMessage: fire-and-forget push at a specific wall-clock time,
      addressed to a specific user. Never appears on the plan board.

    The scheduler tick sweeps `status=pending AND fire_at<=now` rows and
    dispatches via the target's BotSession (same 1:1 transport as reminders).
    Delivery prefixes the text with "[{author} 让我转告] " so the receiver
    knows it's a forwarded nudge, not a spontaneous bot ping.
    """

    __tablename__ = "scheduled_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ScheduledMessageStatus] = mapped_column(
        Enum(ScheduledMessageStatus, name="scheduled_message_status"),
        default=ScheduledMessageStatus.pending,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class CrossUserNote(Base):
    """Cross-user whiteboard entry (PR-H).

    One human (`author_user_id`) dictates a note addressed to the other human
    (`audience_user_id`) via `note_for_peer`. The next time the audience
    speaks to 小计, the orchestrator surfaces unconsumed notes into the
    volatile whiteboard section of their prompt and stamps `consumed_at`.

    `text` is capped at 200 chars by policy (enforced at the tool layer, not
    the DB schema) so the whiteboard section stays under its global 400-char
    budget — see `prompts.Whiteboard.render`.
    """

    __tablename__ = "cross_user_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    audience_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[CrossUserNoteKind] = mapped_column(
        Enum(CrossUserNoteKind, name="cross_user_note_kind"),
        default=CrossUserNoteKind.info,
        nullable=False,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
